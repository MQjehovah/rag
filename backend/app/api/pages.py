from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import or_, text
from typing import List, Optional
import uuid
import time
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

from app.models.database import Page, Notebook, PageChunk, get_engine
from app.models.schema import PageCreate, PageUpdate, PageResponse, PageListItem, PageListResponse
from app.core.rag import EmbeddingService, VectorStore
from app.core.hybrid import HybridIndex
from app.core.entity_graph import EntityGraphStore
from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/pages", tags=["笔记"])

_embedding_service = None
_last_index_time = {}


def _should_index(page_id: str, cooldown: float = 15.0) -> bool:
    """Throttle auto-indexing so autosave bursts do not queue an LLM/embedding
    job for every keystroke burst."""
    now = time.time()
    if now - _last_index_time.get(page_id, 0.0) < cooldown:
        return False
    _last_index_time[page_id] = now
    return True

def get_embedding_service():
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service

async def background_index_page(page_id: str):
    """Index a page without holding a DB transaction across slow calls.

    Embedding/LLM calls can take tens of seconds; keeping a write transaction
    open for the whole time locks the whole SQLite database ("database is
    locked" for every other request).  Each stage commits as soon as its DB
    writes are done, so the lock window is only a few milliseconds per stage.
    """
    engine = get_engine(settings.database_url)
    from app.models.database import get_session as _get_session
    emb_svc = EmbeddingService()
    try:
        # 1) Read the latest content in a short read transaction.
        db = _get_session(engine)
        try:
            page = db.query(Page).filter(Page.id == page_id).first()
            if not page:
                return
            title = page.title or ""
            content = page.content or ""
        finally:
            db.close()

        if not (title or content).strip():
            return

        # 2) Embedding call - no DB lock held while waiting on Ollama.
        chunks = await emb_svc.encode_chunks(content, title)

        # 3) Replace chunks, commit immediately.
        db = _get_session(engine)
        try:
            if chunks:
                await VectorStore(db).add_page_chunks(page_id, chunks)
            db.commit()
        finally:
            db.close()

        # 4) Keywords + BM25 index, commit immediately.
        keywords = EmbeddingService.extract_keywords(title + " " + content, 20)
        db = _get_session(engine)
        try:
            page = db.query(Page).filter(Page.id == page_id).first()
            if page:
                page.keywords = ",".join(keywords)
            HybridIndex(db).index_page(page_id, title, content, page.keywords)
            db.commit()
        finally:
            db.close()

        # 5) Entity graph (LLM call), then commit.
        db = _get_session(engine)
        try:
            await EntityGraphStore(db).extract_and_store(page_id, title, content)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Auto-index failed for {page_id}: {e}")
    finally:
        await emb_svc.close()

def _check_page_access(page, current_user, db):
    if "__local_admin__" in current_user["groups"]:
        return
    if page.notebook_id:
        nb = db.query(Notebook).filter(Notebook.id == page.notebook_id).first()
        if nb and nb.group_id and nb.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记")

def _check_page_access_by_nb(notebook_id, current_user, db):
    if "__local_admin__" in current_user["groups"]:
        return
    if notebook_id:
        row = db.execute(
            text("SELECT group_id FROM notebooks WHERE id = :nid"),
            {"nid": notebook_id},
        ).fetchone()
        if row and row[0] and row[0] not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记")

@router.post("", response_model=PageResponse)
def create_page(data: PageCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if data.notebook_id:
        nb = db.query(Notebook).filter(Notebook.id == data.notebook_id).first()
        if nb and "__local_admin__" not in current_user["groups"]:
            if nb.group_id and nb.group_id not in current_user["groups"]:
                raise HTTPException(status_code=403, detail="无权在该笔记本创建笔记")
    page = Page(id=str(uuid.uuid4()), title=data.title, content=data.content, notebook_id=data.notebook_id)
    db.add(page)
    db.commit()
    db.refresh(page)
    if page.content and _should_index(page.id):
        background_tasks.add_task(background_index_page, page.id)
    return page

@router.get("", response_model=PageListResponse)
def list_pages(
    notebook_id: Optional[str] = None,
    unassigned: bool = False,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    cols = (Page.id, Page.title, Page.notebook_id, Page.created_at, Page.updated_at)
    query = db.query(*cols)
    if unassigned:
        query = query.filter(Page.notebook_id.is_(None))
    elif notebook_id:
        query = query.filter(Page.notebook_id == notebook_id)
    if "__local_admin__" not in current_user["groups"]:
        visible_nb_ids = db.query(Notebook.id).filter(
            or_(Notebook.group_id.in_(current_user["groups"]), Notebook.group_id.is_(None))
        ).subquery()
        query = query.filter(
            or_(Page.notebook_id.is_(None), Page.notebook_id.in_(visible_nb_ids))
        )

    total = query.count()
    rows = query.order_by(Page.updated_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return PageListResponse(
        items=[PageListItem(
            id=r.id,
            title=r.title,
            notebook_id=r.notebook_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        ) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )

@router.get("/{page_id}", response_model=PageResponse)
def get_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = db.execute(
        text("SELECT id, title, content, notebook_id, created_at, updated_at FROM pages WHERE id = :pid"),
        {"pid": page_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access_by_nb(row[3], current_user, db)
    return PageResponse(
        id=row[0], title=row[1], content=row[2],
        notebook_id=row[3], created_at=row[4], updated_at=row[5],
    )

@router.put("/{page_id}", response_model=PageResponse)
def update_page(page_id: str, data: PageUpdate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)

    if data.title is not None:
        page.title = data.title
    if data.content is not None:
        page.content = data.content
    if data.notebook_id is not None:
        page.notebook_id = data.notebook_id
    page.updated_at = datetime.now()

    db.commit()
    db.refresh(page)
    if _should_index(page.id):
        background_tasks.add_task(background_index_page, page.id)
    return page

@router.delete("/{page_id}")
def delete_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)

    HybridIndex(db).delete_page(page_id)
    EntityGraphStore(db).delete_page(page_id)
    db.query(PageChunk).filter(PageChunk.page_id == page_id).delete()
    db.delete(page)
    db.commit()

    return {"message": "删除成功"}

@router.post("/{page_id}/index")
async def index_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)

    title = page.title or ""
    content = page.content or ""
    db.commit()  # close the read transaction before the slow calls

    try:
        emb_svc = EmbeddingService()
        try:
            chunks = await emb_svc.encode_chunks(content, title)
            if chunks:
                await VectorStore(db).add_page_chunks(page.id, chunks)
            db.commit()

            keywords = EmbeddingService.extract_keywords(title + " " + content, 20)
            page = db.query(Page).filter(Page.id == page_id).first()
            if page:
                page.keywords = ",".join(keywords)
            HybridIndex(db).index_page(page_id, title, content, page.keywords)
            db.commit()

            await EntityGraphStore(db).extract_and_store(page_id, title, content)
            db.commit()
            return {"message": f"索引成功，共 {len(chunks)} 个分块"}
        finally:
            await emb_svc.close()
    except Exception as e:
        logger.error(f"Index failed for {page_id}: {e}")
        raise HTTPException(status_code=500, detail=f"索引失败: {str(e)}")


@router.post("/reindex-all")
async def reindex_all(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")

    pages = db.query(Page).filter(Page.content.isnot(None), Page.content != "").all()

    if not pages:
        return {"message": "没有需要索引的笔记", "indexed": 0, "total": 0}

    emb_svc = EmbeddingService()
    vec_store = VectorStore(db)
    success = 0
    errors = 0

    for page in pages:
        try:
            vec_store.delete_page_chunks(page.id)
            db.commit()
            chunks = await emb_svc.encode_chunks(page.content, page.title, enrich_context=False)
            if chunks:
                await vec_store.add_page_chunks(page.id, chunks)
            keywords = EmbeddingService.extract_keywords(
                (page.title or "") + " " + (page.content or ""), 20
            )
            page.keywords = ",".join(keywords)
            HybridIndex(db).index_page(page.id, page.title, page.content, page.keywords)
            db.commit()
            success += 1
        except Exception as e:
            logger.error(f"Reindex failed for {page.id}: {e}")
            errors += 1

    await emb_svc.close()
    return {"message": f"索引完成: {success} 成功, {errors} 失败", "indexed": success, "errors": errors, "total": len(pages)}
