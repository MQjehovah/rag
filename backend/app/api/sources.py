"""数据源 API（P9，V3 计划 4.11）。

- GET    /api/sources                    → 连接列表 + 配置状态
- GET    /api/sources/connectors         → connector 详情（含 setup_blockers）
- POST   /api/sources/connections        → 创建连接（管理员）
- GET    /api/sources/connections/{id}   → 连接详情（不回显 Secret）
- PATCH  /api/sources/connections/{id}   → 更新连接（管理员）
- POST   /api/sources/connections/{id}/test → 测试连接
- POST   /api/sources/connections/{id}/sync → 触发同步（管理员）
- POST   /api/sources/runs/{id}/cancel   → 取消任务（管理员）
- POST   /api/sources/runs/{id}/retry    → 重试失败项（管理员）
- GET    /api/sources/runs               → 任务列表
- GET    /api/sources/runs/{id}          → 任务详情
- GET    /api/sources/runs/{id}/errors   → 任务错误
- GET    /api/sources/items              → SourceItem 列表

所有写接口要求管理员；响应不含密码/Token/Secret。
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core import access_control
from app.core.feature_flags import feature_enabled
from app.core.jwt_utils import get_current_user, is_admin_user
from app.models.database import (
    Notebook,
    SourceConnection,
    SourceItem,
    SourceSyncError,
    SourceSyncRun,
)
from app.sources.bootstrap import get_connector_setup_status
from app.sources.registry import get_connector, registered_keys
from app.sources.worker import request_cancel

router = APIRouter(prefix="/api/sources", tags=["数据源"])

# 敏感字段：任何嵌套层级都不得回显
_REDACT_KEYS = {
    "client_secret", "access_token", "refresh_token", "authorization",
    "secret", "token", "api_key", "app_secret", "app_key", "password",
    "secret_ref", "private_token",
}


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


def _redact(value):
    """递归过滤敏感键，防止凭证泄露到 API 响应。"""
    if isinstance(value, dict):
        return {
            str(k): ("[REDACTED]" if str(k).lower() in _REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class ConnectionCreatePayload(BaseModel):
    connector_key: str
    name: str
    config_json: dict = {}
    target_notebook_id: Optional[str] = None
    default_acl_json: dict = {}


class ConnectionUpdatePayload(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    config_json: Optional[dict] = None
    default_acl_json: Optional[dict] = None
    target_notebook_id: Optional[str] = None


class SyncPayload(BaseModel):
    mode: str = "incremental"  # incremental/backfill/full_reconcile
    pilot_limit: Optional[int] = None  # 显式试点数量（None=不限制）
    selected_space_ids: Optional[list[str]] = None
    retry_failed_only: bool = False


def _serialize_connection(c: SourceConnection) -> dict:
    return {
        "id": c.id,
        "connector_key": c.connector_key,
        "name": c.name,
        "enabled": bool(c.enabled),
        "config_json": json.dumps(_redact(_json_config(c.config_json)), ensure_ascii=False),
        "secret_ref": bool(c.secret_ref),  # 只回「是否配置」，不回显内容
        "target_notebook_id": c.target_notebook_id,
        "last_success_at": c.last_success_at.isoformat() if c.last_success_at else None,
        "last_error_at": c.last_error_at.isoformat() if c.last_error_at else None,
    }


def _json_config(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _validate_target_notebook(db: Session, notebook_id: str | None) -> None:
    """校验目标 Notebook 权限域合法（J-1 最终返工）。

    用 access_control.scope_from_notebook 判断：company / admin / 一个或多个
    业务组均合法；unknown（权限域无法确定）非法。不得用 notebook.group_id
    是否为空判断——None 是合法 company。
    """
    if not notebook_id:
        return
    notebook = db.get(Notebook, notebook_id)
    if notebook is None:
        raise HTTPException(status_code=400, detail="目标知识库不存在")
    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        raise HTTPException(status_code=400, detail="目标知识库权限域无法确定（unknown），请先配置权限")


def _serialize_target_notebook(db: Session, notebook: Notebook) -> dict:
    """序列化目标 Notebook（J-1：附带完整可访问组集合与可见范围标签）。

    - groups：Notebook.group_id + notebook_groups 表额外授权组的合并集合（去重排序）。
    - scope_label：company / group:<组名,...> / admin / unknown 的可读标签，
      供管理员在「文件夹权限映射」中只读展示 Notebook 可见范围。
    - group_id：主权限组（兼容旧前端逻辑）。
    """
    groups = sorted(access_control.notebook_access_groups(db, notebook))
    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_COMPANY:
        scope_label = "全公司"
    elif scope.kind == access_control.SCOPE_ADMIN:
        scope_label = "仅管理员"
    elif scope.kind == access_control.SCOPE_GROUP:
        scope_label = "组：" + "、".join(sorted(scope.groups))
    else:
        scope_label = "未确定（fail closed）"
    return {
        "id": notebook.id,
        "name": notebook.name,
        "group_id": notebook.group_id,
        "groups": groups,
        "scope_label": scope_label,
    }


def _connector_flag(connector_key: str) -> str | None:
    return {
        "dingtalk": "dingtalk_connector_enabled",
        "gitlab": "gitlab_connector_enabled",
    }.get(connector_key)


def _require_source_enabled(db: Session, connector_key: str | None = None) -> None:
    if not feature_enabled(db, "source_hub_enabled"):
        raise HTTPException(status_code=409, detail="统一数据源入口尚未启用")
    if connector_key:
        flag = _connector_flag(connector_key)
        if flag and not feature_enabled(db, flag):
            raise HTTPException(status_code=409, detail=f"{connector_key} Connector 尚未启用")


def _serialize_run(r: SourceSyncRun) -> dict:
    return {
        "id": r.id,
        "connection_id": r.connection_id,
        "mode": r.mode,
        "status": r.status,
        "stage": r.stage,
        "progress": r.progress,
        "discovered_count": r.discovered_count,
        "created_count": r.created_count,
        "updated_count": r.updated_count,
        "unchanged_count": r.unchanged_count,
        "deleted_count": r.deleted_count,
        "failed_count": r.failed_count,
        "cancel_requested": bool(r.cancel_requested),
        "llm_degraded": bool(r.llm_degraded),
        "degraded_reason": r.degraded_reason,
        "heartbeat_at": r.heartbeat_at.isoformat() if r.heartbeat_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "error_summary": r.error_summary,
    }


def _serialize_item(i: SourceItem) -> dict:
    return {
        "id": i.id,
        "connection_id": i.connection_id,
        "external_id": i.external_id,
        "state": i.state,
        "page_id": i.page_id,
        "content_hash": i.content_hash,
        "last_synced_at": i.last_synced_at.isoformat() if i.last_synced_at else None,
        "last_error": i.last_error,
    }


@router.get("")
def list_sources(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    setup = get_connector_setup_status(db)
    connectors = sorted(set(registered_keys()) | set(setup.keys()))
    connections = db.query(SourceConnection).all()
    target_notebooks = db.query(Notebook).order_by(Notebook.name).all()
    return {
        "connectors": connectors,
        "connector_setup": setup,
        "connector_enabled": {
            key: (feature_enabled(db, flag) if (flag := _connector_flag(key)) else True)
            for key in connectors
        },
        "source_hub_enabled": feature_enabled(db, "source_hub_enabled"),
        "target_notebooks": [_serialize_target_notebook(db, nb) for nb in target_notebooks],
        "connections": [_serialize_connection(c) for c in connections],
    }


@router.get("/connectors")
def list_connectors(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """connector 详情：中文名/启用/已配置/已连接/setup_blockers/同步模式。"""
    _require_admin(current_user)
    return {"connectors": get_connector_setup_status(db)}


@router.post("/connections")
def create_connection(
    payload: ConnectionCreatePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    if payload.connector_key not in registered_keys():
        raise HTTPException(status_code=400, detail=f"未知 connector: {payload.connector_key}")
    # J-1 最终返工：钉钉文件归属完全由文件夹映射决定，不强制 target_notebook_id；
    # 其他 Connector 缺少 target_notebook_id → 400。目标 Notebook 用
    # scope_from_notebook 校验（company/admin/多组合法；unknown 非法）。
    if payload.connector_key != "dingtalk" and not payload.target_notebook_id:
        raise HTTPException(status_code=400, detail="该数据源必须指定目标知识库")
    if payload.target_notebook_id:
        _validate_target_notebook(db, payload.target_notebook_id)
    # 防止重复创建同名连接（connector_key + name 唯一）
    dup = db.query(SourceConnection).filter(
        SourceConnection.connector_key == payload.connector_key,
        SourceConnection.name == payload.name,
    ).first()
    if dup is not None:
        raise HTTPException(status_code=409, detail=f"已存在同名连接：{payload.name}")
    # 钉钉凭证只来自环境变量，绝不写入 config_json
    config_json = _redact(payload.config_json)
    conn = SourceConnection(
        id=str(uuid.uuid4()),
        connector_key=payload.connector_key,
        name=payload.name,
        config_json=json.dumps(config_json, ensure_ascii=False),
        target_notebook_id=payload.target_notebook_id,
        default_acl_json=json.dumps(payload.default_acl_json, ensure_ascii=False),
        created_by=current_user.get("id"),
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return _serialize_connection(conn)


@router.get("/connections/{conn_id}")
def get_connection(
    conn_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    conn = db.query(SourceConnection).filter(SourceConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="连接不存在")
    return _serialize_connection(conn)


@router.patch("/connections/{conn_id}")
def update_connection(
    conn_id: str,
    payload: ConnectionUpdatePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    conn = db.query(SourceConnection).filter(SourceConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="连接不存在")
    if payload.name is not None:
        conn.name = payload.name
    if payload.enabled is not None:
        conn.enabled = payload.enabled
    if payload.config_json is not None:
        conn.config_json = json.dumps(payload.config_json, ensure_ascii=False)
    if payload.default_acl_json is not None:
        conn.default_acl_json = json.dumps(payload.default_acl_json, ensure_ascii=False)
    if "target_notebook_id" in payload.model_fields_set:
        # J-1 最终返工：非钉钉 Connector 不能清空 target_notebook_id；
        # 目标 Notebook 用 scope_from_notebook 校验。
        if conn.connector_key != "dingtalk" and not payload.target_notebook_id:
            raise HTTPException(status_code=400, detail="该数据源必须指定目标知识库")
        if payload.target_notebook_id:
            _validate_target_notebook(db, payload.target_notebook_id)
        conn.target_notebook_id = payload.target_notebook_id
    db.commit()
    db.refresh(conn)
    return _serialize_connection(conn)


@router.post("/connections/{conn_id}/test")
async def test_connection(
    conn_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    conn = db.query(SourceConnection).filter(SourceConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="连接不存在")
    _require_source_enabled(db, conn.connector_key)
    config = _json_config(conn.config_json)
    config["connection_id"] = conn.id
    connector = get_connector(conn.connector_key, config)
    result = await connector.test_connection()
    return {
        "ok": bool(result.ok),
        "message": result.message,
        "error_code": result.error_code,
    }


@router.post("/connections/{conn_id}/sync")
def trigger_sync(
    conn_id: str,
    payload: SyncPayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    if payload.mode not in ("incremental", "backfill", "full_reconcile"):
        raise HTTPException(status_code=400, detail=f"未知同步模式: {payload.mode}")
    conn = db.query(SourceConnection).filter(SourceConnection.id == conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="连接不存在")
    _require_source_enabled(db, conn.connector_key)
    if not conn.enabled:
        raise HTTPException(status_code=409, detail="连接已停用")
    if conn.connector_key == "dingtalk":
        from app.sources.dingtalk import is_legacy_sync_running
        if is_legacy_sync_running():
            raise HTTPException(status_code=409, detail="旧钉钉入口正在同步，禁止重复执行")
    active = db.query(SourceSyncRun).filter(
        SourceSyncRun.connection_id == conn_id,
        SourceSyncRun.status.in_(("queued", "running")),
    ).first()
    if active:
        raise HTTPException(status_code=409, detail=f"该连接已有活动任务：{active.id}")

    # run 级参数存 cursor_before_json，executor 从 run + connection 合并读取
    params: dict = {}
    if payload.pilot_limit is not None:
        params["pilot_limit"] = max(0, int(payload.pilot_limit))
    if payload.selected_space_ids:
        params["selected_space_ids"] = [str(s) for s in payload.selected_space_ids if str(s).strip()]
    params["retry_failed_only"] = bool(payload.retry_failed_only)

    run = SourceSyncRun(
        id=str(uuid.uuid4()),
        connection_id=conn_id,
        mode=payload.mode,
        status="queued",
        cursor_before_json=json.dumps(params, ensure_ascii=False) if params else None,
        created_by=current_user.get("id"),
    )
    db.add(run)
    db.commit()
    return {"run_id": run.id, "status": "queued", "mode": payload.mode}


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    if not request_cancel(db, run_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"message": "已请求取消"}


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """只重试失败且可重试的条目，不重新同步全部成功文档。"""
    _require_admin(current_user)
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 可重试范围：SourceSyncError.retryable 的 external_id
    errors = db.query(SourceSyncError).filter(
        SourceSyncError.run_id == run_id,
        SourceSyncError.retryable.is_(True),
    ).all()
    retry_ids = [e.external_id for e in errors if e.external_id]
    if not retry_ids:
        raise HTTPException(status_code=400, detail="该任务没有可重试的失败项")

    conn = db.get(SourceConnection, run.connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="连接不存在")
    active = db.query(SourceSyncRun).filter(
        SourceSyncRun.connection_id == conn.id,
        SourceSyncRun.status.in_(("queued", "running")),
    ).first()
    if active:
        raise HTTPException(status_code=409, detail=f"该连接已有活动任务：{active.id}")

    retry_run = SourceSyncRun(
        id=str(uuid.uuid4()),
        connection_id=conn.id,
        mode=run.mode,
        status="queued",
        cursor_before_json=json.dumps({"retry_scope": retry_ids}, ensure_ascii=False),
        created_by=current_user.get("id"),
    )
    db.add(retry_run)
    db.commit()
    return {"run_id": retry_run.id, "status": "queued", "retry_count": len(retry_ids)}


@router.get("/runs")
def list_runs(
    connection_id: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    query = db.query(SourceSyncRun).order_by(SourceSyncRun.started_at.desc())
    if connection_id:
        query = query.filter(SourceSyncRun.connection_id == connection_id)
    runs = query.limit(limit).all()
    return {"runs": [_serialize_run(r) for r in runs]}


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serialize_run(run)


@router.get("/runs/{run_id}/errors")
def list_run_errors(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    errors = db.query(SourceSyncError).filter(SourceSyncError.run_id == run_id).all()
    return {"errors": [
        {"external_id": e.external_id, "stage": e.stage, "error_code": e.error_code,
         "error_message": e.error_message, "retryable": e.retryable}
        for e in errors
    ]}


@router.get("/items")
def list_items(
    connection_id: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    query = db.query(SourceItem).order_by(SourceItem.last_synced_at.desc())
    if connection_id:
        query = query.filter(SourceItem.connection_id == connection_id)
    items = query.limit(limit).all()
    return {"items": [_serialize_item(i) for i in items]}
