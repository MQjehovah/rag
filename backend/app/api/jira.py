import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import settings
from app.core.jira import sync_jira
from app.core.jwt_utils import get_current_user
from app.models.database import JiraIssue, Page

router = APIRouter(prefix="/api/jira", tags=["Jira"])

_jira_status: Dict[str, Any] = {
    "running": False,
    "processed": 0,
    "total": 0,
    "message": "",
}
_jira_task: asyncio.Task | None = None


@router.get("/status")
def jira_status(current_user=Depends(get_current_user)):
    return _jira_status


@router.get("/stats")
def jira_stats(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    synced = db.query(JiraIssue).count()
    return {
        "synced_issues": synced,
        "enabled": settings.jira_enabled,
        "url": settings.jira_url,
    }


@router.post("/sync")
async def jira_sync(current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
    if not settings.jira_enabled or not settings.jira_url:
        raise HTTPException(status_code=400, detail="Jira 未配置")
    global _jira_task
    if _jira_status.get("running"):
        return {"started": False, "running": True, "message": "Jira 同步已在运行"}
    _jira_status.update(running=True, processed=0, total=0, message="启动 Jira 同步...")
    _jira_task = asyncio.create_task(sync_jira(_jira_status))
    return {"started": True, "running": True}
