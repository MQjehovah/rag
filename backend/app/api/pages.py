from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
import uuid
from datetime import datetime

from app.models.database import Page, Notebook, PageChunk, get_session, get_engine, init_db
from app.models.schema import PageCreate, PageUpdate, PageResponse, PageListItem, PageListResponse
from app.core.rag import EmbeddingService, VectorStore
from app.core.jwt_utils import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/pages", tags=["笔记"])

_engine = None
_session = None
_embedding_service = None

def get_db():
    global _engine, _session
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session

def get_embedding_service():
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service

async def background_index_page(page_id: str, title: str, content: str):
    try:
        emb_svc = EmbeddingService()
        engine = get_engine(settings.database_url)
        from app.models.database import get_session as _get_session
        db = _get_session(engine)

        chunks = await emb_svc.encode_chunks(content, title)
        if chunks:
            vec_store = VectorStore(db)
            await vec_store.add_page_chunks(page_id, title, chunks)

            keywords = EmbeddingService.extract_keywords(
                (title or "") + " " + (content or ""), 20
            )
            page = db.query(Page).filter(Page.id == page_id).first()
            if page:
                page.keywords = ",".join(keywords)
                db.commit()

        db.close()
        await emb_svc.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Auto-index failed for {page_id}: {e}")

def _check_page_access(page, current_user, db):
    if "__local_admin__" in current_user["groups"]:
        return
    if page.notebook_id:
        nb = db.query(Notebook).filter(Notebook.id == page.notebook_id).first()
        if nb and nb.group_id and nb.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记")

@router.post("", response_model=PageResponse)
async def create_page(data: PageCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if data.notebook_id:
        nb = db.query(Notebook).filter(Notebook.id == data.notebook_id).first()
        if nb and "__local_admin__" not in current_user["groups"]:
            if nb.group_id and nb.group_id not in current_user["groups"]:
                raise HTTPException(status_code=403, detail="无权在该笔记本创建笔记")
    page = Page(id=str(uuid.uuid4()), title=data.title, content=data.content, notebook_id=data.notebook_id)
    db.add(page)
    db.commit()
    db.refresh(page)
    if page.content and page.content.strip():
        background_tasks.add_task(background_index_page, page.id, page.title, page.content)
    return page

@router.get("", response_model=PageListResponse)
async def list_pages(
    notebook_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = db.query(Page)
    if notebook_id:
        query = query.filter(Page.notebook_id == notebook_id)
    if "__local_admin__" not in current_user["groups"]:
        visible_nb_ids = db.query(Notebook.id).filter(
            or_(Notebook.group_id.in_(current_user["groups"]), Notebook.group_id.is_(None))
        ).subquery()
        query = query.filter(Page.notebook_id.in_(visible_nb_ids))

    total = query.count()
    items = query.order_by(Page.updated_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return PageListResponse(
        items=[PageListItem(
            id=p.id,
            title=p.title,
            notebook_id=p.notebook_id,
            created_at=p.created_at,
            updated_at=p.updated_at,
        ) for p in items],
        total=total,
        page=page,
        page_size=page_size,
    )

@router.get("/{page_id}", response_model=PageResponse)
async def get_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    return page

@router.put("/{page_id}", response_model=PageResponse)
async def update_page(page_id: str, data: PageUpdate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
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
    if page.content and page.content.strip():
        background_tasks.add_task(background_index_page, page.id, page.title, page.content)
    return page

@router.delete("/{page_id}")
async def delete_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)

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

    try:
        emb_svc = EmbeddingService()
        vec_store = VectorStore(db)
        chunks = await emb_svc.encode_chunks(page.content, page.title)

        if chunks:
            await vec_store.add_page_chunks(page.id, page.title, chunks)
            keywords = EmbeddingService.extract_keywords(
                (page.title or "") + " " + (page.content or ""), 20
            )
            page.keywords = ",".join(keywords)
            db.commit()
            await emb_svc.close()
            return {"message": f"索引成功，共 {len(chunks)} 个分块"}
        else:
            await emb_svc.close()
            return {"message": "内容为空，未创建索引"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"索引失败: {str(e)}")
