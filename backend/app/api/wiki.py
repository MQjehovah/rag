import asyncio
import json
import logging
import uuid
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.search_common import get_visible_page_ids, visible_wiki_filter
from app.core.jwt_utils import get_current_user
from app.core.security import has_permission, require_permission
from app.core.rag import EmbeddingService
from app.core.wiki import build_wiki, ensure_default_space, refresh_stale_wiki, resolve_space_id
from app.core.wiki_embedding import embed_wiki_pages
from app.core.wiki_search import search_wiki
from app.models.database import Page, WikiPage, WikiSpace

router = APIRouter(prefix="/api/wiki", tags=["Wiki"])

logger = logging.getLogger(__name__)


def _wiki_visible(page: WikiPage, current_user) -> bool:
    if has_permission(current_user, "*"):
        return True
    groups = (current_user or {}).get("groups") or []
    return page.group_id is None or page.group_id in groups


_wiki_status: Dict[str, Any] = {
    "running": False,
    "processed": 0,
    "total": 0,
    "message": "",
}
_wiki_task: asyncio.Task | None = None


class WikiPageUpdate(BaseModel):
    content: str = ""
    summary: str = ""
    category: str = ""


class WikiGroupUpdate(BaseModel):
    group_id: str | None = None


class WikiSearchRequest(BaseModel):
    query: str
    top_k: int = 5


class SpaceCreate(BaseModel):
    name: str
    icon: str = ""
    description: str = ""


class SpaceUpdate(BaseModel):
    name: str | None = None
    icon: str | None = None
    description: str | None = None


class WikiSpaceMove(BaseModel):
    space_id: str | None = None


class WikiPageCreate(BaseModel):
    title: str = "无标题"
    space_id: str | None = None
    parent_id: str | None = None


class WikiPageMove(BaseModel):
    parent_id: str | None = None
    position: int = 0


def _space_visible(space: WikiSpace, current_user) -> bool:
    if has_permission(current_user, "wiki.admin"):
        return True
    return space.group_id is None or space.group_id in current_user["groups"]


# ---- 空间(必须在 /{page_id} 之前声明, 否则 "spaces" 会被当作 page_id) ----
@router.get("/spaces")
def list_spaces(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    spaces = db.query(WikiSpace).order_by(WikiSpace.position.asc(), WikiSpace.created_at.asc()).all()
    default_id = ensure_default_space(db).id
    spaces = [s for s in spaces if _space_visible(s, current_user) and s.id != default_id]
    # 计数(按空间)
    counts: Dict[str, int] = {}
    for (sid,) in db.query(WikiPage.space_id).filter(visible_wiki_filter(current_user)).all():
        counts[sid or ""] = counts.get(sid or "", 0) + 1
    return {
        "spaces": [
            {
                "id": s.id,
                "name": s.name,
                "icon": s.icon or "",
                "description": s.description or "",
                "group_id": s.group_id,
                "count": counts.get(s.id, 0),
            }
            for s in spaces
        ],
        "default_count": counts.get(default_id, 0) + counts.get("", 0),
        "total": sum(counts.values()),
    }


@router.post("/spaces")
def create_space(data: SpaceCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "wiki.admin")
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="空间名称不能为空")
    max_pos = db.query(WikiSpace).count()
    group_id = current_user["groups"][0] if current_user["groups"] else None
    space = WikiSpace(
        id=str(uuid.uuid4()),
        name=name, icon=data.icon or "", description=data.description or "",
        position=max_pos, group_id=group_id,
    )
    db.add(space)
    db.commit()
    db.refresh(space)
    return {"id": space.id, "name": space.name, "icon": space.icon or "", "description": space.description or "", "group_id": space.group_id, "count": 0}


@router.put("/spaces/{space_id}")
def update_space(space_id: str, data: SpaceUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "wiki.admin")
    space = db.query(WikiSpace).filter(WikiSpace.id == space_id).first()
    if not space or not _space_visible(space, current_user):
        raise HTTPException(status_code=404, detail="空间不存在")
    if data.name is not None:
        space.name = data.name.strip() or space.name
    if data.icon is not None:
        space.icon = data.icon
    if data.description is not None:
        space.description = data.description
    db.commit()
    return {"message": "已保存", "id": space.id}


@router.delete("/spaces/{space_id}")
def delete_space(space_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "wiki.admin")
    space = db.query(WikiSpace).filter(WikiSpace.id == space_id).first()
    if not space or not _space_visible(space, current_user):
        raise HTTPException(status_code=404, detail="空间不存在")
    db.query(WikiPage).filter(WikiPage.space_id == space_id).update({WikiPage.space_id: None})
    db.delete(space)
    db.commit()
    return {"message": "已删除(页面已移至默认空间)"}


@router.put("/{page_id}/space")
def move_wiki_space(page_id: str, data: WikiSpaceMove, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page or not _wiki_visible(page, current_user):
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    page.space_id = resolve_space_id(db, data.space_id)
    db.commit()
    return {"message": "已移动", "id": page.id, "space_id": page.space_id}


@router.post("")
def create_wiki_page(data: WikiPageCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """手工新建 wiki 页(可指定空间与父页面, 支持多级)。"""
    parent_id = data.parent_id
    if parent_id:
        parent = db.query(WikiPage).filter(WikiPage.id == parent_id).first()
        if not parent:
            parent_id = None
    space_id = resolve_space_id(db, data.space_id)
    group_id = None
    space = db.query(WikiSpace).filter(WikiSpace.id == space_id).first()
    if space:
        group_id = space.group_id
    if group_id is None and current_user["groups"]:
        group_id = current_user["groups"][0]
    q = db.query(WikiPage).filter(
        WikiPage.parent_id == parent_id if parent_id else WikiPage.parent_id.is_(None)
    )
    q = q.filter(WikiPage.space_id == space_id)
    pos = q.count()
    page = WikiPage(
        id=str(uuid.uuid4()), title=(data.title or "无标题"), content="", summary="",
        space_id=space_id, parent_id=parent_id, position=pos, group_id=group_id,
    )
    db.add(page)
    db.commit()
    db.refresh(page)
    return {
        "id": page.id, "title": page.title, "summary": "", "space_id": page.space_id,
        "parent_id": page.parent_id, "position": page.position, "category": page.category or "未分类",
    }


@router.put("/{page_id}/move")
def move_wiki_page(page_id: str, data: WikiPageMove, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page or not _wiki_visible(page, current_user):
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    parent_id = data.parent_id
    if parent_id:
        if parent_id == page.id:
            raise HTTPException(status_code=400, detail="不能移动到自身")
        parent = db.query(WikiPage).filter(WikiPage.id == parent_id).first()
        if not parent:
            raise HTTPException(status_code=400, detail="目标父页面无效")
        cur = parent
        guard = 0
        while cur is not None and guard < 1000:
            if cur.id == page.id:
                raise HTTPException(status_code=400, detail="不能移动到自己的子页面下")
            cur = db.query(WikiPage).filter(WikiPage.id == cur.parent_id).first() if cur.parent_id else None
            guard += 1
    page.parent_id = parent_id
    db.flush()
    q = db.query(WikiPage).filter(WikiPage.id != page.id)
    q = q.filter(WikiPage.parent_id == parent_id) if parent_id else q.filter(WikiPage.parent_id.is_(None))
    siblings = q.order_by(WikiPage.position.asc(), WikiPage.title.asc()).all()
    pos = max(0, min(int(data.position), len(siblings)))
    siblings.insert(pos, page)
    for i, p in enumerate(siblings):
        p.position = i
    db.commit()
    return {"message": "ok"}


@router.get("")
def list_wiki(space_id: str | None = None, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    query = db.query(WikiPage).filter(visible_wiki_filter(current_user))
    if space_id == "default":
        query = query.filter(WikiPage.space_id == ensure_default_space(db).id)
    elif space_id:
        query = query.filter(WikiPage.space_id == space_id)
    pages = query.order_by(WikiPage.position.asc(), WikiPage.title).all()
    categories: Dict[str, list] = {}
    items: list = []
    for p in pages:
        cat = p.category or "未分类"
        entry = {
            "id": p.id,
            "title": p.title,
            "summary": p.summary or "",
            "category": cat,
            "group_id": p.group_id,
            "pipeline_id": p.pipeline_id,
            "space_id": p.space_id,
            "parent_id": p.parent_id,
            "position": p.position or 0,
            "updated_at": p.updated_at,
        }
        items.append(entry)
        categories.setdefault(cat, []).append(entry)
    return {
        "total": len(pages),
        "items": items,
        "categories": [
            {"name": name, "pages": items_}
            for name, items_ in sorted(categories.items(), key=lambda x: x[0])
        ],
        "running": _wiki_status.get("running", False),
    }


@router.get("/rebuild-status")
def wiki_status(current_user=Depends(get_current_user)):
    return _wiki_status


# 注意:必须定义在 @router.get("/{page_id}") 之前,否则单段的 /{page_id}
# 会先匹配到 /search,导致语义搜索接口被吞掉。
@router.post("/search")
async def search_wiki_endpoint(
    data: WikiSearchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """语义检索当前用户可见的 wiki 页面。"""
    query = (data.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="查询内容不能为空")
    svc = EmbeddingService()
    try:
        emb = await svc.encode(query)
    finally:
        await svc.close()
    hits = await asyncio.to_thread(search_wiki, db, emb, data.top_k, current_user)
    return {
        "results": [
            {
                "id": h["id"],
                "title": h["title"],
                "summary": h["summary"],
                "category": h["category"],
                "score": h["score"],
            }
            for h in hits
        ],
        "total": len(hits),
    }


@router.delete("/{page_id}")
def delete_wiki_page(page_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page or not _wiki_visible(page, current_user):
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    db.query(WikiPage).filter(WikiPage.parent_id == page_id).update({WikiPage.parent_id: None})
    db.delete(page)
    db.commit()
    return {"message": "已删除"}


@router.get("/{page_id}")
def get_wiki_page(
    page_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page or not _wiki_visible(page, current_user):
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    try:
        note_ids = json.loads(page.source_note_ids or "[]")
    except Exception:
        note_ids = []
    sources = []
    if note_ids:
        visible_note_ids = get_visible_page_ids(db, current_user)
        rows = db.query(Page.id, Page.title).filter(Page.id.in_(note_ids)).all()
        sources = [{"id": r[0], "title": r[1]} for r in rows if r[0] in visible_note_ids]
    return {
        "id": page.id,
        "title": page.title,
        "category": page.category or "未分类",
        "content": page.content or "",
        "summary": page.summary or "",
        "group_id": page.group_id,
        "pipeline_id": page.pipeline_id,
        "space_id": page.space_id,
        "sources": sources,
        "updated_at": page.updated_at,
    }


@router.put("/{page_id}")
async def update_wiki_page(
    page_id: str,
    data: WikiPageUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Human fine-tuning of a wiki page.  The next compile merge pass sees
    this edited content and preserves it."""
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page or not _wiki_visible(page, current_user):
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    if data.content is not None:
        page.content = data.content
    if data.summary is not None:
        page.summary = data.summary
    if data.category is not None:
        page.category = data.category or "未分类"
    db.commit()
    # 人工编辑后刷新该页向量,否则编辑内容不会被语义检索命中
    await embed_wiki_pages(db.get_bind(), [page.id])
    return {"message": "已保存", "id": page.id, "updated_at": page.updated_at}


@router.put("/{page_id}/group")
def set_wiki_group(
    page_id: str,
    data: WikiGroupUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """人工指定 wiki 页面的归属组;仅管理员。None 表示公共。"""
    require_permission(current_user, "wiki.admin")
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    page.group_id = (data.group_id or "").strip() or None
    db.commit()
    return {"message": "已保存", "id": page.id, "group_id": page.group_id}


@router.post("/rebuild")
async def rebuild_wiki(current_user=Depends(get_current_user)):
    require_permission(current_user, "wiki.admin")
    global _wiki_task
    if _wiki_status.get("running"):
        return {"started": False, "running": True, "message": "Wiki 编译已在运行"}
    _wiki_status.update({"running": True, "processed": 0, "total": 0, "message": "启动编译..."})
    _wiki_task = asyncio.create_task(build_wiki(_wiki_status))
    return {"started": True, "running": True}


@router.post("/reindex-embeddings")
async def reindex_wiki_embeddings(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """管理员为存量 wiki 页补齐缺失向量(幂等)。"""
    require_permission(current_user, "wiki.admin")
    return await embed_wiki_pages(db.get_bind())


@router.post("/refresh-stale")
async def refresh_stale_endpoint(current_user=Depends(get_current_user)):
    """Differentiated rebuild: only re-distill pages whose source notes
    changed since they were last compiled."""
    require_permission(current_user, "wiki.admin")
    global _wiki_task
    if _wiki_status.get("running"):
        return {"started": False, "running": True, "message": "已有编译任务在运行"}
    _wiki_status.update(running=True, processed=0, total=0, message="检查过期页面...")
    _wiki_task = asyncio.create_task(refresh_stale_wiki(_wiki_status))
    return {"started": True, "running": True}
