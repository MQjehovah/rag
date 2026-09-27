"""公开(免登录)只读接口: 通过分享令牌访问已发布页面。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.database import Page

router = APIRouter(prefix="/api/public", tags=["公开分享"])


@router.get("/pages/{token}")
def public_page(token: str, db: Session = Depends(get_db)):
    page = db.query(Page).filter(
        Page.share_token == token, Page.deleted_at.is_(None)
    ).first()
    if not page:
        raise HTTPException(status_code=404, detail="分享不存在或已取消")
    return {
        "title": page.title or "无标题",
        "content": page.content or "",
        "icon": page.icon or "",
        "cover": page.cover or "",
        "updated_at": page.updated_at,
    }
