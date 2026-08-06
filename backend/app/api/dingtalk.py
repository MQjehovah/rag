from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, List

from app.models.database import Notebook, get_session, get_engine, init_db
from app.core.dingtalk import DingTalkClient
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.core.dingtalk_sync_service import DingTalkSyncService, DingTalkSyncState
from app.core.jwt_utils import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/dingtalk", tags=["钉钉同步"])

_SYNC_STORAGE = DingTalkLocalStorage()
_SYNC_STORAGE.ensure_directories()
SYNC_STATE = DingTalkSyncState(_SYNC_STORAGE.root / "sync-task.json")
SYNC_SERVICE = DingTalkSyncService(SYNC_STATE)


class SyncRequest(BaseModel):
    notebook_id: Optional[str] = None
    notebook_name: str = "钉钉知识库"
    space_id: Optional[str] = None


class SyncSelectedRequest(BaseModel):
    notebook_id: Optional[str] = None
    notebook_name: str = "钉钉知识库"
    docs: List[dict]


class RetrySyncRequest(BaseModel):
    task_id: Optional[str] = None


def _resolve_notebook_name(
    notebook_id: Optional[str],
    notebook_name: str,
) -> str:
    """兼容按ID选择笔记本，同时让后台任务只接收稳定的名称。"""
    if not notebook_id:
        return str(notebook_name or "钉钉知识库").strip() or "钉钉知识库"
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        notebook = db.get(Notebook, notebook_id)
        if notebook is None:
            raise HTTPException(status_code=404, detail="目标笔记本不存在")
        return notebook.name
    finally:
        db.close()
        engine.dispose()


async def _run_sync_task(
    mode: str,
    notebook_name: str,
    space_id: Optional[str] = None,
    selected_docs: Optional[List[dict]] = None,
    retry_spec: Optional[dict] = None,
) -> None:
    await SYNC_SERVICE.run(
        mode=mode,
        notebook_name=notebook_name,
        space_id=space_id,
        selected_docs=selected_docs,
        retry_spec=retry_spec,
    )


@router.get("/docs")
async def list_docs(
    space_id: Optional[str] = None,
    current_user=Depends(get_current_user),
):
    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=400, detail="请先配置钉钉参数")

    client = DingTalkClient()
    try:
        docs = await client.list_all_docs(space_id)
        manifest = DingTalkLocalStorage().read_manifest()
        local_entries = {
            str(entry.get("document_id") or ""): entry
            for entry in manifest["documents"]
        }
        for document in docs:
            entry = local_entries.get(str(document.get("id") or ""), {})
            document["sync"] = {
                "source_status": entry.get("source_status") or "new",
                "download_status": entry.get("download_status") or "pending",
                "conversion_status": entry.get("conversion_status") or "pending",
                "rag_status": entry.get("rag_status") or "not_ready",
                "pipeline_status": entry.get("pipeline_status") or "new",
                "last_seen_at": entry.get("last_seen_at"),
                "source_encryption": entry.get("source_encryption") or "",
                "conversion_error": entry.get("conversion_error") or "",
                "conversion_warnings": entry.get("conversion_warnings") or [],
            }
        return {
            "total": len(docs),
            "docs": docs,
            "inventory_complete": bool(client._last_inventory_complete),
            "scope_space_ids": client._last_inventory_scope_ids,
        }
    finally:
        await client.close()


@router.post("/sync-selected")
async def start_sync_selected(
    req: SyncSelectedRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
):
    if not req.docs:
        raise HTTPException(status_code=400, detail="请选择要同步的文档")

    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=400, detail="请先配置 DINGTALK_APP_KEY 和 DINGTALK_APP_SECRET")

    document_ids = [str(doc.get("id") or "").strip() for doc in req.docs]
    if any(not document_id for document_id in document_ids):
        raise HTTPException(status_code=400, detail="选中文档包含缺少ID的项目")
    if len(document_ids) != len(set(document_ids)):
        raise HTTPException(status_code=400, detail="选中文档包含重复ID")

    notebook_name = _resolve_notebook_name(req.notebook_id, req.notebook_name)
    try:
        task = SYNC_STATE.begin(
            mode="selected",
            notebook_name=notebook_name,
            space_id=None,
            total=len(req.docs),
            request={
                "original_mode": "selected",
                "notebook_name": notebook_name,
                "space_id": None,
                "selected_docs": req.docs,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(
        _run_sync_task,
        "selected",
        notebook_name,
        None,
        req.docs,
    )
    return {
        "message": "同步已启动",
        "task_id": task["task_id"],
        "status": task,
    }


@router.post("/sync")
async def start_sync(
    req: SyncRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
):
    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=400, detail="请先配置 DINGTALK_APP_KEY 和 DINGTALK_APP_SECRET")

    notebook_name = _resolve_notebook_name(req.notebook_id, req.notebook_name)
    try:
        task = SYNC_STATE.begin(
            mode="full",
            notebook_name=notebook_name,
            space_id=req.space_id,
            request={
                "original_mode": "full",
                "notebook_name": notebook_name,
                "space_id": req.space_id,
                "selected_docs": [],
            },
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(
        _run_sync_task,
        "full",
        notebook_name,
        req.space_id,
        None,
    )
    return {
        "message": "全量同步已启动",
        "task_id": task["task_id"],
        "status": task,
    }


@router.post("/retry")
async def retry_sync(
    req: RetrySyncRequest,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
):
    previous = SYNC_STATE.snapshot()
    if req.task_id and req.task_id != previous.get("task_id"):
        raise HTTPException(status_code=404, detail="待重试任务不存在")
    try:
        retry_spec = SYNC_SERVICE.build_retry_spec()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    retry_counts = {
        **dict(retry_spec.get("counts") or {}),
        "restart": bool(retry_spec.get("restart")),
    }
    retry_total = len(retry_spec.get("all_document_ids") or [])
    if retry_spec.get("restart"):
        retry_total = int(previous.get("total") or 0)
    request = {
        "original_mode": retry_spec["original_mode"],
        "notebook_name": retry_spec["notebook_name"],
        "space_id": retry_spec.get("space_id"),
        "selected_docs": retry_spec.get("selected_docs") or [],
    }
    try:
        task = SYNC_STATE.begin(
            mode="retry",
            notebook_name=retry_spec["notebook_name"],
            space_id=retry_spec.get("space_id"),
            total=retry_total,
            request=request,
            retry_count=int(previous.get("retry_count") or 0) + 1,
            parent_task_id=previous.get("task_id"),
            retry_plan=retry_counts,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    background_tasks.add_task(
        _run_sync_task,
        "retry",
        retry_spec["notebook_name"],
        retry_spec.get("space_id"),
        retry_spec.get("selected_docs") or [],
        retry_spec,
    )
    return {
        "message": "失败项重试已启动",
        "task_id": task["task_id"],
        "retry_plan": retry_counts,
        "status": task,
    }


@router.get("/status")
async def get_sync_status(
    task_id: Optional[str] = None,
    current_user=Depends(get_current_user),
):
    status = SYNC_STATE.snapshot()
    if task_id and task_id != status.get("task_id"):
        raise HTTPException(status_code=404, detail="同步任务不存在或服务已重启")
    if not status.get("last_sync"):
        manifest = DingTalkLocalStorage().read_manifest()
        status["last_sync"] = str(manifest.get("last_inventory_at") or "")
    if status.get("recoverable"):
        try:
            retry_spec = SYNC_SERVICE.build_retry_spec()
            status["retry_plan"] = {
                **dict(retry_spec.get("counts") or {}),
                "restart": bool(retry_spec.get("restart")),
            }
        except (RuntimeError, ValueError):
            pass
    return status


@router.get("/spaces")
async def list_spaces(current_user=Depends(get_current_user)):
    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=400, detail="请先配置钉钉参数")

    client = DingTalkClient()
    try:
        spaces = await client.list_workspaces()
        return spaces
    finally:
        await client.close()
