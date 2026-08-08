import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.jwt_utils import get_current_user
from app.sources import SOURCES

router = APIRouter(prefix="/api/sources", tags=["数据源"])

_statuses: Dict[str, Dict[str, Any]] = {}
_tasks: Dict[str, asyncio.Task] = {}


class SyncRequest(BaseModel):
    mode: str = "incremental"
    days: int = 0


def _status(key: str) -> Dict[str, Any]:
    if key not in _statuses:
        _statuses[key] = {"running": False, "processed": 0, "total": 0, "message": ""}
    return _statuses[key]


@router.get("")
def list_sources(current_user=Depends(get_current_user)):
    return {
        "sources": [
            {
                "key": s.key,
                "name": s.name,
                "description": s.description,
                "enabled": s.enabled(),
                "config": s.config_summary(),
                "status": _status(s.key),
            }
            for s in SOURCES.values()
        ]
    }


@router.post("/{key}/test")
async def test_source(key: str, current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
    src = SOURCES.get(key)
    if not src:
        raise HTTPException(status_code=404, detail="数据源不存在")
    ok, msg = await src.test()
    return {"ok": ok, "message": msg}


@router.post("/{key}/sync")
async def sync_source(key: str, body: SyncRequest = None, current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
    src = SOURCES.get(key)
    if not src:
        raise HTTPException(status_code=404, detail="数据源不存在")
    if not src.enabled():
        raise HTTPException(status_code=400, detail="数据源未配置")
    st = _status(key)
    if st.get("running"):
        return {"started": False, "running": True, "message": "同步已在运行"}
    st.update(running=True, processed=0, total=0, message=f"启动 {src.name} 同步...")
    params = {"mode": body.mode if body else "incremental", "days": body.days if body else 0}
    _tasks[key] = asyncio.create_task(src.sync(st, params))
    return {"started": True, "running": True, "mode": params["mode"], "days": params["days"]}


@router.post("/{key}/cancel")
async def cancel_source(key: str, current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
    task = _tasks.get(key)
    st = _status(key)
    if not task or not st.get("running"):
        return {"cancelled": False, "message": "没有正在运行的同步"}
    task.cancel()
    st["running"] = False
    st["message"] = "同步已取消"
    return {"cancelled": True, "message": "同步已取消"}


@router.get("/{key}/status")
def source_status(key: str, current_user=Depends(get_current_user)):
    return _status(key)
