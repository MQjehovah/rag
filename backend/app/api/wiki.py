import asyncio
import json
import logging
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.core.wiki import build_wiki
from app.models.database import Page, WikiPage

router = APIRouter(prefix="/api/wiki", tags=["Wiki"])

logger = logging.getLogger(__name__)

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


@router.get("")
def list_wiki(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    pages = db.query(WikiPage).order_by(WikiPage.category, WikiPage.title).all()
    categories: Dict[str, list] = {}
    for p in pages:
        cat = p.category or "未分类"
        categories.setdefault(cat, []).append({
            "id": p.id,
            "title": p.title,
            "summary": p.summary or "",
            "updated_at": p.updated_at,
        })
    return {
        "total": len(pages),
        "categories": [
            {"name": name, "pages": items}
            for name, items in sorted(categories.items(), key=lambda x: x[0])
        ],
        "running": _wiki_status.get("running", False),
    }


@router.get("/rebuild-status")
def wiki_status(current_user=Depends(get_current_user)):
    return _wiki_status


@router.get("/{page_id}")
def get_wiki_page(
    page_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    try:
        note_ids = json.loads(page.source_note_ids or "[]")
    except Exception:
        note_ids = []
    sources = []
    if note_ids:
        rows = db.query(Page.id, Page.title).filter(Page.id.in_(note_ids)).all()
        sources = [{"id": r[0], "title": r[1]} for r in rows]
    return {
        "id": page.id,
        "title": page.title,
        "category": page.category or "未分类",
        "content": page.content or "",
        "summary": page.summary or "",
        "sources": sources,
        "updated_at": page.updated_at,
    }


@router.put("/{page_id}")
def update_wiki_page(
    page_id: str,
    data: WikiPageUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Human fine-tuning of a wiki page.  The next compile merge pass sees
    this edited content and preserves it."""
    page = db.query(WikiPage).filter(WikiPage.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    if data.content is not None:
        page.content = data.content
    if data.summary is not None:
        page.summary = data.summary
    if data.category is not None:
        page.category = data.category or "未分类"
    db.commit()
    return {"message": "已保存", "id": page.id, "updated_at": page.updated_at}


@router.post("/rebuild")
async def rebuild_wiki(current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
    global _wiki_task
    if _wiki_status.get("running"):
        return {"started": False, "running": True, "message": "Wiki 编译已在运行"}
    _wiki_status.update({"running": True, "processed": 0, "total": 0, "message": "启动编译..."})
    _wiki_task = asyncio.create_task(build_wiki(_wiki_status))
    return {"started": True, "running": True}
