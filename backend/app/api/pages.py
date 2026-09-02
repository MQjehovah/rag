from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
import uuid
import hashlib
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

from app.models.database import Page, Notebook, PageChunk, get_session, get_engine
from app.models.schema import (
    PageCreate,
    PageListItem,
    PageListResponse,
    PageResponse,
    PageUpdate,
    SourcePageImport,
)
from app.core.rag import EmbeddingService, VectorStore
from app.core.content_quality import ensure_text_content
from app.core import access_control
from app.core.jwt_utils import get_current_user
from app.api.deps import get_db
from app.config import settings

router = APIRouter(prefix="/api/pages", tags=["笔记"])

_embedding_service = None

def get_embedding_service():
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service

async def background_index_page(page_id: str, title: str, content: str, *, schedule_graph: bool = True):
    """索引 Page 并写入 PageChunk。

    schedule_graph：是否在 PageChunk 提交后调度图谱重建。
    - 普通 Page create/update/index/source-import 传 True（默认），在 Chunk 提交后调度。
    - 统一 source executor 传 False，由 executor 在 Evidence 同步完成后只调度一次，
      避免「Evidence 完成前提前构建 + 重复调度」。
    """
    try:
        emb_svc = EmbeddingService()
        engine = get_engine(settings.database_url)
        from app.models.database import get_session as _get_session
        db = _get_session(engine)

        chunks = await emb_svc.encode_chunks(content, title)
        if chunks:
            vec_store = VectorStore(db)
            await vec_store.add_page_chunks(page_id, chunks)

            keywords = EmbeddingService.extract_keywords(
                (title or "") + " " + (content or ""), 20
            )
            page = db.query(Page).filter(Page.id == page_id).first()
            if page:
                page.keywords = ",".join(keywords)
                page.content_hash = page.current_content_hash
                page.indexed_content_hash = page.current_content_hash
                page.index_dirty = False
                db.commit()
            # V4 Phase J-3：PageChunk 成功提交后才调度图谱重建（索引失败不调度，
            # 避免用旧 Chunk 构建新图谱）。executor 场景由调用方在 Evidence 后调度。
            if schedule_graph:
                try:
                    from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_page_graph_rebuild
                    schedule_page_graph_rebuild(page_id)
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "graph rebuild schedule failed page=%s: %s", page_id, exc
                    )

        db.close()
        await emb_svc.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Auto-index failed for {page_id}: {e}")

def _check_page_access(page, current_user, db):
    # Phase B：统一到 access_control；无归属页面 fail closed。
    if not access_control.can_view_page(db, current_user, page):
        raise HTTPException(status_code=403, detail="无权访问该笔记")

def _check_page_manage(current_user):
    # Phase B 补漏：原始 Page 的创建/导入/修改/删除/索引仅管理员。
    if not access_control.can_manage_pages(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可管理原始资料")

def _check_page_access_by_nb(notebook_id, current_user, db):
    if access_control.is_admin(current_user):
        return
    if notebook_id:
        notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
        if notebook is not None and not access_control.can_view_notebook(db, current_user, notebook):
            raise HTTPException(status_code=403, detail="无权访问该笔记")

@router.post("", response_model=PageResponse)
def create_page(data: PageCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    try:
        ensure_text_content(data.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _check_page_manage(current_user)
    if data.notebook_id:
        nb = db.query(Notebook).filter(Notebook.id == data.notebook_id).first()
        if nb and not access_control.can_view_notebook(db, current_user, nb):
            raise HTTPException(status_code=403, detail="无权在该笔记本创建笔记")
    page = Page(
        id=str(uuid.uuid4()),
        title=data.title,
        content=data.content,
        content_hash=hashlib.sha256((data.content or "").encode("utf-8")).hexdigest(),
        index_dirty=True,
        notebook_id=data.notebook_id,
        wiki_dirty=True,
    )
    db.add(page)
    db.commit()
    db.refresh(page)
    # V4 Phase F：Page 新建后，同 scope 债务有界重验证（失败不影响主流程）
    try:
        from app.core.retrieval.debt_service import notify_knowledge_changed_for_page
        notify_knowledge_changed_for_page(db, page.id)
    except Exception:  # noqa: BLE001
        pass
    # V4 Phase G：索引 + Wiki 刷新，不再调度 Card 编译。
    # V4 Phase J-3：图谱重建在 background_index_page 内 PageChunk 提交后触发，
    # 不在此处提前调度（避免用旧 Chunk 构建新图谱）。
    background_tasks.add_task(background_index_page, page.id, page.title, page.content)
    # V4 Phase C：异步调度 Wiki 增量刷新（提交后调用，不阻塞请求）
    background_tasks.add_task(_schedule_wiki_refresh, page.id, True)
    return page

def _schedule_wiki_refresh(page_id: str, changed: bool) -> None:
    """V4 Phase C：同步调度 Wiki 增量刷新（提交后调用，幂等，不阻塞请求）。

    schedule_page_refresh 是同步函数（提交到有界线程池），BackgroundTasks 可直接执行。
    """
    from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import schedule_page_refresh
    schedule_page_refresh(page_id, changed=changed)


def _schedule_graph_rebuild(page_id: str) -> None:
    """V4 Phase J-3：异步调度图谱重建（幂等，失败不影响 Page 主流程）。"""
    try:
        from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_page_graph_rebuild
        schedule_page_graph_rebuild(page_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph rebuild schedule failed page=%s: %s", page_id, exc)


@router.get("", response_model=PageListResponse)
def list_pages(
    notebook_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    cols = (Page.id, Page.title, Page.notebook_id, Page.created_at, Page.updated_at)
    query = db.query(*cols)
    if notebook_id:
        query = query.filter(Page.notebook_id == notebook_id)
    if not access_control.is_admin(current_user):
        visible_page_ids = access_control.get_visible_page_ids(db, current_user)
        if not visible_page_ids:
            return PageListResponse(items=[], total=0, page=page, page_size=page_size)
        query = query.filter(Page.id.in_(visible_page_ids))

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


@router.post("/source-import", response_model=PageResponse)
def import_source_page(
    data: SourcePageImport,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    background_tasks: BackgroundTasks = None,
):
    """管理员专用的来源文档幂等写入接口。"""
    if not access_control.can_manage_pages(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可导入来源文档")

    try:
        ensure_text_content(data.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    actual_hash = hashlib.sha256(data.content.encode("utf-8")).hexdigest()
    if data.published_content_hash != actual_hash:
        raise HTTPException(status_code=400, detail="发布内容哈希与正文不一致")

    notebook = db.query(Notebook).filter(Notebook.id == data.notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="目标知识库不存在")

    identity_page = db.query(Page).filter(
        Page.source_type == data.source_type,
        Page.source_id == data.source_id,
    ).first()
    mapped_page = db.query(Page).filter(Page.id == data.page_id).first() if data.page_id else None
    if mapped_page is not None:
        _check_page_access(mapped_page, current_user, db)
    if identity_page is not None and mapped_page is not None and identity_page.id != mapped_page.id:
        raise HTTPException(status_code=409, detail="来源文档ID与指定远程页面冲突")

    page = identity_page or mapped_page
    if page is not None and page.source_type and (
        page.source_type != data.source_type or page.source_id != data.source_id
    ):
        raise HTTPException(status_code=409, detail="指定页面已绑定其他来源文档")
    if page is None:
        page = Page(id=str(uuid.uuid4()))
        db.add(page)

    content_changed = page.content != data.content
    title_changed = page.title != data.title
    notebook_changed = page.notebook_id != data.notebook_id
    page.notebook_id = data.notebook_id
    page.title = data.title
    page.content = data.content
    page.source_type = data.source_type
    page.source_id = data.source_id
    page.source_path = data.source_path
    page.source_space_id = data.source_space_id
    page.source_url = data.source_url
    page.source_file_hash = data.source_file_hash
    page.source_file_size = data.source_file_size
    page.source_mime_type = data.source_mime_type
    page.source_content = data.content
    page.source_content_hash = actual_hash
    page.source_markdown_hash = data.source_markdown_hash
    page.source_pipeline_version = data.source_pipeline_version
    page.content_hash = actual_hash
    page.last_synced_at = datetime.now()
    if content_changed or not page.indexed_content_hash:
        page.index_dirty = True
    # V4 Phase C：内容/标题/notebook 变化都算 Wiki changed
    wiki_changed = content_changed or title_changed or notebook_changed or page.wiki_dirty is None or page.wiki_dirty
    if wiki_changed:
        page.wiki_dirty = True

    db.commit()
    db.refresh(page)
    if background_tasks is not None and (content_changed or not page.indexed_content_hash):
        background_tasks.add_task(background_index_page, page.id, page.title, page.content)
    # V4 Phase C：source-import 调度 Wiki 增量刷新
    if background_tasks is not None:
        background_tasks.add_task(_schedule_wiki_refresh, page.id, wiki_changed)
    return page

@router.get("/{page_id}", response_model=PageResponse)
def get_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    return page

@router.put("/{page_id}", response_model=PageResponse)
def update_page(page_id: str, data: PageUpdate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_manage(current_user)
    _check_page_access(page, current_user, db)

    if data.content is not None:
        try:
            ensure_text_content(data.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    source_content_changed = (
        page.source_type == "dingtalk"
        and (
            (data.content is not None and data.content != page.content)
            or (data.title is not None and data.title != page.title)
        )
    )
    if source_content_changed and not data.allow_source_edit:
        raise HTTPException(
            status_code=409,
            detail="钉钉同步文档默认只读；如需修改，请先明确启用源码编辑",
        )

    changed = False
    notebook_changed = False
    if data.title is not None:
        changed = changed or data.title != page.title
        page.title = data.title
    if data.content is not None:
        changed = changed or data.content != page.content
        page.content = data.content
        page.content_hash = hashlib.sha256(
            (data.content or "").encode("utf-8")
        ).hexdigest()
    if data.notebook_id is not None:
        notebook_changed = notebook_changed or data.notebook_id != page.notebook_id
        page.notebook_id = data.notebook_id
    if changed:
        page.index_dirty = True
        page.updated_at = datetime.now()

    # V4 Phase C：正文/标题变化，或 notebook/权限域变化，都算 Wiki changed
    wiki_changed = changed or notebook_changed
    if wiki_changed:
        page.wiki_dirty = True

    # V4 Phase J-3：notebook 变化 → 先同事务移除旧 scope 图谱 provenance。
    # 失败必须 rollback（不得提交新 notebook_id），返回明确失败响应。
    if notebook_changed:
        try:
            from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
            remove_page_graph(db, page.id, commit=False)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("remove page graph before notebook change failed page=%s: %s", page.id, exc)
            raise HTTPException(status_code=500, detail="图谱失效失败，无法完成 Notebook 归属变更")

    db.commit()
    db.refresh(page)
    # V4 Phase F：Page 更新后，同 scope 债务有界重验证（失败不影响主流程）
    try:
        from app.core.retrieval.debt_service import notify_knowledge_changed_for_page
        notify_knowledge_changed_for_page(db, page.id)
    except Exception:  # noqa: BLE001
        pass
    # V4 Phase G：内容变更后自动重建索引（不再调度知识编译）；图谱重建在
    # background_index_page 内 PageChunk 提交后触发。
    if changed:
        background_tasks.add_task(background_index_page, page.id, page.title, page.content)
    # V4 Phase C：内容/标题/权限域变化后异步调度 Wiki 增量刷新
    if wiki_changed:
        background_tasks.add_task(_schedule_wiki_refresh, page.id, True)
    # V4 Phase J-3：仅 notebook 变化（无内容变化）时，调度新 scope 图谱重建。
    if notebook_changed and not changed:
        background_tasks.add_task(_schedule_graph_rebuild, page.id)
    return page

@router.delete("/{page_id}")
def delete_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_manage(current_user)
    _check_page_access(page, current_user, db)

    # V4 Phase J-3：先移除图谱 provenance（在删 PageChunk 前收集受影响关系/实体/Community，
    # 避免 FK CASCADE 先删 provenance 导致无法收集），再删 Chunk/Page，最后统一 commit。
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    remove_page_graph(db, page_id, commit=False)
    db.query(PageChunk).filter(PageChunk.page_id == page_id).delete()
    db.delete(page)
    db.commit()

    # V4 Phase C：删除来源 Page → 移除 Wiki 来源并标记 dirty（同步快操作，不调用 LLM）
    from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import schedule_page_deleted
    schedule_page_deleted(page_id)

    return {"message": "删除成功"}

@router.post("/{page_id}/index")
async def index_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_manage(current_user)
    _check_page_access(page, current_user, db)

    try:
        emb_svc = EmbeddingService()
        vec_store = VectorStore(db)
        chunks = await emb_svc.encode_chunks(page.content, page.title)

        if chunks:
            await vec_store.add_page_chunks(page.id, chunks)
            keywords = EmbeddingService.extract_keywords(
                (page.title or "") + " " + (page.content or ""), 20
            )
            page.keywords = ",".join(keywords)
            page.content_hash = page.current_content_hash
            page.indexed_content_hash = page.current_content_hash
            page.index_dirty = False
            db.commit()
            # V4 Phase F：索引完成后，同 scope 债务有界重验证（失败不影响主流程）
            try:
                from app.core.retrieval.debt_service import notify_knowledge_changed_for_page
                notify_knowledge_changed_for_page(db, page_id)
            except Exception:  # noqa: BLE001
                pass
            # V4 Phase J-3：PageChunk 提交后调度图谱重建。
            try:
                from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_page_graph_rebuild
                schedule_page_graph_rebuild(page_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("graph rebuild schedule failed page=%s: %s", page_id, exc)
            await emb_svc.close()
            return {"message": f"索引成功，共 {len(chunks)} 个分块"}
        else:
            await emb_svc.close()
            return {"message": "内容为空，未创建索引"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"索引失败: {str(e)}")


@router.post("/reindex-all")
async def reindex_all(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if not access_control.can_manage_pages(current_user):
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
            chunks = await emb_svc.encode_chunks(page.content, page.title)
            if chunks:
                await vec_store.add_page_chunks(page.id, chunks)
            keywords = EmbeddingService.extract_keywords(
                (page.title or "") + " " + (page.content or ""), 20
            )
            page.keywords = ",".join(keywords)
            page.content_hash = page.current_content_hash
            page.indexed_content_hash = page.current_content_hash
            page.index_dirty = False
            db.commit()
            success += 1
        except Exception as e:
            logger.error(f"Reindex failed for {page.id}: {e}")
            errors += 1

    await emb_svc.close()
    return {"message": f"索引完成: {success} 成功, {errors} 失败", "indexed": success, "errors": errors, "total": len(pages)}
