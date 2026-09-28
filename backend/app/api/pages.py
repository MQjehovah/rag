from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import or_, select, text, func
from typing import Optional
import uuid
import secrets
import time
from datetime import datetime
import logging
from collections import Counter

from app.core.security import has_permission
from app.core.visibility import ACL_NOTEBOOK, notebook_visible_condition, resource_visible_db
from app.models.database import Page, Notebook, PageChunk, PageRevision, PageComment, get_engine
from app.models.schema import PageCreate, PageUpdate, PageMove, PageViewUpdate, CommentCreate, PageResponse, PageListItem, PageListResponse
from app.core.rag import EmbeddingService, VectorStore
from app.core.hybrid import HybridIndex
from app.core.entity_graph import EntityGraphStore
from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pages", tags=["笔记"])

_embedding_service = None
_last_index_time = {}
_last_wiki_time = {}


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
    from app.core.rag import embedding_spec_for_notebook
    try:
        # 1) Read the latest content in a short read transaction.
        db = _get_session(engine)
        try:
            page = db.query(Page).filter(Page.id == page_id).first()
            if not page:
                return
            title = page.title or ""
            content = page.content or ""
            spec = embedding_spec_for_notebook(db, page.notebook_id)
        finally:
            db.close()

        profile_id = spec.get("id")
        emb_svc = EmbeddingService(spec)
        if not (title or content).strip():
            await emb_svc.close()
            return

        # 2) Embedding call - no DB lock held while waiting on Ollama.
        chunks = await emb_svc.encode_chunks(content, title)

        # 3) Replace chunks, commit immediately.
        db = _get_session(engine)
        try:
            if chunks:
                await VectorStore(db).add_page_chunks(page_id, chunks, profile_id=profile_id)
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

        # 6) Incremental wiki compile for this note (throttled), so note
        # edits keep the distilled wiki fresh without burning tokens on
        # autosave bursts.
        now = time.time()
        if now - _last_wiki_time.get(page_id, 0.0) > 60.0:
            _last_wiki_time[page_id] = now
            try:
                from app.core.compile_dispatch import dispatch_note_compile
                await dispatch_note_compile(page_id)
            except Exception as e:
                logger.warning(f"Compile dispatch failed for {page_id}: {e}")
    except Exception as e:
        logger.error(f"Auto-index failed for {page_id}: {e}")
    finally:
        await emb_svc.close()

def _visible_notebook_ids(current_user):
    """当前用户可见笔记本的 id 子查询(新可见性谓词)。"""
    return select(Notebook.id).where(notebook_visible_condition(current_user))


def _page_within_visible_notebook(current_user):
    """列表过滤:未归属笔记本的页面保持公共(notebook_id IS NULL)。"""
    return or_(
        Page.notebook_id.is_(None),
        Page.notebook_id.in_(_visible_notebook_ids(current_user)),
    )


def _check_page_access(page, current_user, db):
    if has_permission(current_user, "page.manage"):
        return
    if page.notebook_id:
        nb = db.query(Notebook).filter(Notebook.id == page.notebook_id).first()
        if nb and not resource_visible_db(db, current_user, ACL_NOTEBOOK, nb):
            raise HTTPException(status_code=403, detail="无权访问该笔记")

def _check_page_access_by_nb(notebook_id, current_user, db):
    if has_permission(current_user, "page.manage"):
        return
    if notebook_id:
        nb = db.query(Notebook).filter(Notebook.id == notebook_id).first()
        if nb and not resource_visible_db(db, current_user, ACL_NOTEBOOK, nb):
            raise HTTPException(status_code=403, detail="无权访问该笔记")

@router.post("", response_model=PageResponse)
def create_page(data: PageCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if data.notebook_id:
        nb = db.query(Notebook).filter(Notebook.id == data.notebook_id).first()
        if nb and not has_permission(current_user, "page.manage"):
            if not resource_visible_db(db, current_user, ACL_NOTEBOOK, nb):
                raise HTTPException(status_code=403, detail="无权在该笔记本创建笔记")
    parent_id = data.parent_id
    if parent_id:
        parent = db.query(Page).filter(Page.id == parent_id).first()
        if not parent or parent.notebook_id != data.notebook_id:
            parent_id = None
    max_pos = db.query(func.max(Page.position)).filter(
        Page.notebook_id == data.notebook_id
    ).scalar()
    page = Page(
        id=str(uuid.uuid4()), title=data.title, content=data.content,
        notebook_id=data.notebook_id, icon=data.icon or '', cover=data.cover or '',
        cover_offset=int(data.cover_offset or 50),
        parent_id=parent_id, position=(max_pos or 0) + 1,
    )
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
    tag: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    cols = (Page.id, Page.title, Page.notebook_id, Page.parent_id, Page.position, Page.created_at, Page.updated_at)
    query = db.query(*cols).filter(Page.deleted_at.is_(None))
    if unassigned:
        query = query.filter(Page.notebook_id.is_(None))
    elif notebook_id:
        query = query.filter(Page.notebook_id == notebook_id)
    if tag:
        query = query.filter(Page.keywords.like(f"%{tag}%"))
    if not has_permission(current_user, "page.manage"):
        query = query.filter(_page_within_visible_notebook(current_user))

    total = query.count()
    rows = query.order_by(Page.position.asc(), Page.created_at.asc()).offset((page - 1) * page_size).limit(page_size).all()

    return PageListResponse(
        items=[PageListItem(
            id=r.id,
            title=r.title,
            notebook_id=r.notebook_id,
            parent_id=r.parent_id,
            position=r.position or 0,
            created_at=r.created_at,
            updated_at=r.updated_at,
        ) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/tags")
def get_tags(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Aggregate note keywords into a tag cloud (visible to the user)."""
    if has_permission(current_user, "page.manage"):
        rows = db.query(Page.keywords).filter(Page.deleted_at.is_(None)).all()
    else:
        rows = db.query(Page.keywords).filter(
            Page.deleted_at.is_(None),
            _page_within_visible_notebook(current_user),
        ).all()
    counter = Counter()
    for (kw_str,) in rows:
        if not kw_str:
            continue
        for kw in kw_str.split(","):
            kw = kw.strip()
            if len(kw) >= 2:
                counter[kw] += 1
    tags = [{"tag": k, "count": v} for k, v in counter.most_common(100)]
    return {"tags": tags}

@router.get("/tree")
def page_tree(notebook_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """返回某笔记本下的全部页面(含父子关系与排序),用于页面树。"""
    nb = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not nb:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if not has_permission(current_user, "page.manage") and not resource_visible_db(db, current_user, ACL_NOTEBOOK, nb):
        raise HTTPException(status_code=403, detail="无权访问该笔记本")
    rows = db.query(Page.id, Page.title, Page.parent_id, Page.position, Page.view_type, Page.status, Page.updated_at).filter(
        Page.notebook_id == notebook_id,
        Page.deleted_at.is_(None),
    ).order_by(Page.position.asc(), Page.created_at.asc()).all()
    return {
        "items": [
            {
                "id": r[0],
                "title": r[1] or "无标题",
                "parent_id": r[2],
                "position": r[3] or 0,
                "view_type": r[4] or "doc",
                "status": r[5] or "",
                "updated_at": r[6],
            }
            for r in rows
        ]
    }

@router.get("/trash")
def list_trash(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """回收站: 已软删除的页面(按用户可见范围)。"""
    q = db.query(Page.id, Page.title, Page.notebook_id, Page.deleted_at).filter(Page.deleted_at.isnot(None))
    if not has_permission(current_user, "page.manage"):
        q = q.filter(_page_within_visible_notebook(current_user))
    rows = q.order_by(Page.deleted_at.desc()).all()
    return {
        "items": [
            {
                "id": r[0],
                "title": r[1] or "无标题",
                "notebook_id": r[2],
                "deleted_at": r[3],
            }
            for r in rows
        ]
    }

@router.get("/{page_id}", response_model=PageResponse)
def get_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = db.execute(
        text(
            "SELECT id, title, content, notebook_id, icon, cover, parent_id, position, share_token, "
            "cover_offset, view_type, status, created_at, updated_at "
            "FROM pages WHERE id = :pid AND deleted_at IS NULL"
        ),
        {"pid": page_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access_by_nb(row[3], current_user, db)
    return PageResponse(
        id=row[0], title=row[1], content=row[2], notebook_id=row[3],
        icon=row[4] or '', cover=row[5] or '', parent_id=row[6], position=row[7] or 0,
        share_token=row[8], cover_offset=row[9] if row[9] is not None else 50,
        view_type=row[10] or 'doc', status=row[11] or '',
        created_at=row[12], updated_at=row[13],
    )

@router.get("/{page_id}/backlinks")
def page_backlinks(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """反向链接: 内容中出现 [[本页标题]] 的其它页面。"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    title = (page.title or "").strip()
    if not title:
        return {"items": []}
    pattern = f"%[[{title}]]%"
    q = db.query(Page.id, Page.title).filter(
        Page.deleted_at.is_(None),
        Page.id != page.id,
        Page.content.like(pattern),
    )
    if not has_permission(current_user, "page.manage"):
        q = q.filter(_page_within_visible_notebook(current_user))
    rows = q.limit(50).all()
    return {"items": [{"id": r[0], "title": r[1] or "无标题"} for r in rows]}

@router.get("/{page_id}/revisions")
def page_revisions(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """历史版本列表(最多 50 条)。"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    rows = (
        db.query(PageRevision)
        .filter(PageRevision.page_id == page_id)
        .order_by(PageRevision.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "title": r.title or "无标题",
                "editor": r.editor or "",
                "created_at": r.created_at,
                "size": len(r.content or ""),
            }
            for r in rows
        ]
    }

@router.get("/{page_id}/revisions/{rev_id}")
def page_revision(page_id: str, rev_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    rev = db.query(PageRevision).filter(PageRevision.id == rev_id, PageRevision.page_id == page_id).first()
    if not rev:
        raise HTTPException(status_code=404, detail="版本不存在")
    return {
        "id": rev.id,
        "title": rev.title or "无标题",
        "content": rev.content or "",
        "editor": rev.editor or "",
        "created_at": rev.created_at,
    }

@router.post("/{page_id}/revisions/{rev_id}/restore", response_model=PageResponse)
def restore_revision(page_id: str, rev_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    rev = db.query(PageRevision).filter(PageRevision.id == rev_id, PageRevision.page_id == page_id).first()
    if not rev:
        raise HTTPException(status_code=404, detail="版本不存在")
    page.title = rev.title or page.title
    page.content = rev.content or ""
    page.updated_at = datetime.now()
    db.commit()
    db.refresh(page)
    if _should_index(page.id):
        background_tasks.add_task(background_index_page, page.id)
    return page

@router.post("/{page_id}/share")
def share_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """生成(或返回已有)公开只读分享令牌。"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    if not page.share_token:
        page.share_token = secrets.token_urlsafe(18)
        db.commit()
    return {"token": page.share_token}

@router.delete("/{page_id}/share")
def unshare_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    page.share_token = None
    db.commit()
    return {"message": "已取消分享"}

def _comment_user(db: Session, current_user) -> tuple:
    uid = current_user.get("id") if isinstance(current_user, dict) else None
    name = ""
    if isinstance(current_user, dict):
        name = current_user.get("display_name") or current_user.get("username") or ""
    return (uid or ""), name


@router.get("/{page_id}/comments")
def list_comments(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    rows = (
        db.query(PageComment)
        .filter(PageComment.page_id == page_id)
        .order_by(PageComment.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": c.id,
                "author_id": c.author_id or "",
                "author": c.author_name or "匿名",
                "content": c.content or "",
                "resolved": bool(c.resolved),
                "created_at": c.created_at,
            }
            for c in rows
        ]
    }


@router.post("/{page_id}/comments")
def add_comment(page_id: str, data: CommentCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    content = (data.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="评论内容不能为空")
    uid, name = _comment_user(db, current_user)
    c = PageComment(
        id=str(uuid.uuid4()), page_id=page_id, author_id=uid,
        author_name=name, content=content,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "author_id": c.author_id, "author": c.author_name or "匿名",
            "content": c.content, "resolved": False, "created_at": c.created_at}


@router.delete("/{page_id}/comments/{comment_id}")
def delete_comment(page_id: str, comment_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    c = db.query(PageComment).filter(PageComment.id == comment_id, PageComment.page_id == page_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="评论不存在")
    if not has_permission(current_user, "*") and c.author_id != (current_user.get("id") if isinstance(current_user, dict) else None):
        raise HTTPException(status_code=403, detail="只能删除自己的评论")
    db.delete(c)
    db.commit()
    return {"message": "已删除"}


@router.post("/{page_id}/comments/{comment_id}/resolve")
def resolve_comment(page_id: str, comment_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    c = db.query(PageComment).filter(PageComment.id == comment_id, PageComment.page_id == page_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="评论不存在")
    c.resolved = not bool(c.resolved)
    db.commit()
    return {"resolved": bool(c.resolved)}


@router.put("/{page_id}/view")
def set_page_view(page_id: str, data: PageViewUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    if data.view_type not in ("doc", "table", "board", "calendar"):
        raise HTTPException(status_code=400, detail="不支持的视图类型")
    page.view_type = data.view_type
    page.updated_at = datetime.now()
    db.commit()
    return {"view_type": page.view_type}

@router.put("/{page_id}", response_model=PageResponse)
def update_page(page_id: str, data: PageUpdate, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)

    if data.title is not None:
        page.title = data.title
    if data.content is not None:
        page.content = data.content
    if data.notebook_id is not None:
        # 改归属时既要校验目标笔记本存在，也要用与读取一致的组可见性规则
        # 校验目标笔记本，避免把笔记写进无权访问的他组笔记本（管理员豁免）。
        target_nb = db.query(Notebook).filter(Notebook.id == data.notebook_id).first()
        if not target_nb:
            raise HTTPException(status_code=403, detail="目标笔记本不存在")
        _check_page_access_by_nb(data.notebook_id, current_user, db)
        page.notebook_id = data.notebook_id
    if data.icon is not None:
        page.icon = data.icon
    if data.cover is not None:
        page.cover = data.cover
    if data.cover_offset is not None:
        page.cover_offset = max(0, min(100, int(data.cover_offset)))
    if data.status is not None:
        page.status = data.status
    page.updated_at = datetime.now()

    # 历史版本: 内容变化时按 2 分钟节流存快照, 每页最多保留 50 条
    if data.title is not None or data.content is not None:
        last = (
            db.query(PageRevision)
            .filter(PageRevision.page_id == page.id)
            .order_by(PageRevision.created_at.desc())
            .first()
        )
        now = datetime.now()
        if last is None or (
            last.content != (page.content or "")
            and (now - (last.created_at or now)).total_seconds() > 120
        ):
            editor_name = ""
            if isinstance(current_user, dict):
                editor_name = current_user.get("display_name") or current_user.get("username") or ""
            db.add(PageRevision(
                id=str(uuid.uuid4()),
                page_id=page.id,
                title=page.title or "",
                content=page.content or "",
                editor=editor_name,
                created_at=now,
            ))
            db.flush()
            old = (
                db.query(PageRevision.id)
                .filter(PageRevision.page_id == page.id)
                .order_by(PageRevision.created_at.desc())
                .offset(50)
                .all()
            )
            for (rid,) in old:
                db.query(PageRevision).filter(PageRevision.id == rid).delete()

    db.commit()
    db.refresh(page)
    if _should_index(page.id):
        background_tasks.add_task(background_index_page, page.id)
    return page

@router.post("/{page_id}/move")
def move_page(page_id: str, data: PageMove, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """移动/重排页面: 设置父页面并在同级中插入到指定位置(含防环校验)。"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)

    parent_id = data.parent_id
    if parent_id:
        if parent_id == page.id:
            raise HTTPException(status_code=400, detail="不能移动到自身")
        parent = db.query(Page).filter(Page.id == parent_id).first()
        if not parent or parent.notebook_id != page.notebook_id:
            raise HTTPException(status_code=400, detail="目标父页面无效")
        cur = parent
        guard = 0
        while cur is not None and guard < 1000:
            if cur.id == page.id:
                raise HTTPException(status_code=400, detail="不能移动到自己的子页面下")
            cur = db.query(Page).filter(Page.id == cur.parent_id).first() if cur.parent_id else None
            guard += 1

    page.parent_id = parent_id
    db.flush()

    q = db.query(Page).filter(Page.notebook_id == page.notebook_id, Page.id != page.id)
    q = q.filter(Page.parent_id == parent_id) if parent_id else q.filter(Page.parent_id.is_(None))
    siblings = q.order_by(Page.position.asc(), Page.created_at.asc()).all()
    pos = max(0, min(int(data.position), len(siblings)))
    siblings.insert(pos, page)
    for i, p in enumerate(siblings):
        p.position = i
    page.updated_at = datetime.now()
    db.commit()
    return {"message": "ok"}

@router.delete("/{page_id}")
def delete_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """软删除: 移到回收站(可在回收站恢复或彻底删除)。"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page or page.deleted_at is not None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    page.deleted_at = datetime.now()
    db.commit()
    return {"message": "已移到回收站"}

@router.post("/{page_id}/restore")
def restore_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    page.deleted_at = None
    # 父页面若仍在回收站, 恢复到根级避免不可见
    if page.parent_id:
        parent = db.query(Page).filter(Page.id == page.parent_id).first()
        if parent and parent.deleted_at is not None:
            page.parent_id = None
    db.commit()
    return {"message": "已恢复"}

@router.delete("/{page_id}/purge")
def purge_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """彻底删除(含向量/词项/图谱)。"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)
    HybridIndex(db).delete_page(page_id)
    EntityGraphStore(db).delete_page(page_id)
    db.query(PageChunk).filter(PageChunk.page_id == page_id).delete()
    db.query(PageRevision).filter(PageRevision.page_id == page_id).delete()
    db.query(PageComment).filter(PageComment.page_id == page_id).delete()
    db.delete(page)
    db.commit()
    return {"message": "已彻底删除"}

@router.post("/{page_id}/index")
async def index_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    _check_page_access(page, current_user, db)

    title = page.title or ""
    content = page.content or ""
    from app.core.rag import embedding_spec_for_notebook
    spec = embedding_spec_for_notebook(db, page.notebook_id)
    profile_id = spec.get("id")
    db.commit()  # close the read transaction before the slow calls

    try:
        emb_svc = EmbeddingService(spec)
        try:
            chunks = await emb_svc.encode_chunks(content, title)
            if chunks:
                await VectorStore(db).add_page_chunks(page.id, chunks, profile_id=profile_id)
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
    if not has_permission(current_user, "page.manage"):
        raise HTTPException(status_code=403, detail="仅管理员可执行")

    pages = db.query(Page).filter(
        Page.content.isnot(None), Page.content != "", Page.deleted_at.is_(None)
    ).all()

    if not pages:
        return {"message": "没有需要索引的笔记", "indexed": 0, "total": 0}

    from app.core.rag import embedding_spec_for_notebook
    vec_store = VectorStore(db)
    svc_cache = {}
    success = 0
    errors = 0

    for page in pages:
        try:
            spec = embedding_spec_for_notebook(db, page.notebook_id)
            key = spec.get("id")
            emb_svc = svc_cache.get(key)
            if emb_svc is None:
                emb_svc = EmbeddingService(spec)
                svc_cache[key] = emb_svc
            vec_store.delete_page_chunks(page.id)
            db.commit()
            chunks = await emb_svc.encode_chunks(page.content, page.title, enrich_context=False)
            if chunks:
                await vec_store.add_page_chunks(page.id, chunks, profile_id=key)
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

    for svc in svc_cache.values():
        await svc.close()
    return {"message": f"索引完成: {success} 成功, {errors} 失败", "indexed": success, "errors": errors, "total": len(pages)}
