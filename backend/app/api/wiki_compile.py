"""Phase 4.1：KnowledgeCompileRun 管理 API（全部 admin-only）。

- GET  /api/wiki-compile/runs                 → 分页列表（摘要，不含敏感字段）
- GET  /api/wiki-compile/runs/{id}            → 详情（run + stage 摘要 + artifact 摘要）
- POST /api/wiki-compile/runs/{id}/retry      → failed & attempt<max → 复用 requeue；
  其余（attempt 用尽/superseded/终态）→ 409
- POST /api/wiki-compile/runs/{id}/cancel     → queued 直接 cancelled；running 设 cancel_requested

敏感字段过滤（白名单序列化，永不返回）：
- 不返回 payload_json 原文、Prompt/正文、Token/ACL 内部数据、内部异常堆栈、
  request_fingerprint/input_hash 等内部列、lease/worker 归属。
- run/stage 的 error 字段只回 safe_*（净化后）或二次截断的摘要；
  error_summary 由 safe 消息生成（截 300）；metrics_json 仅回显白名单键。

错误分层：executor 内部异常 detail 只写日志；API 侧 _http_error 按错误码前缀
收敛为固定文案（400/404/409 映射见 _ERROR_HTTP_STATUS）。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import require_admin
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline.executor import CompileRunError
from app.models.database import (
    KnowledgeCompileArtifact as CompileArtifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
)

router = APIRouter(prefix="/api/wiki-compile", tags=["Wiki 编译任务"])

logger = logging.getLogger(__name__)

# metrics_json 允许回显的摘要键（白名单；其余一律丢弃，防内部细节外泄）。
_METRICS_WHITELIST = {
    "cached", "source_artifact_id", "reused_from_artifact_id",
    "stage", "component_key", "component_version",
}

_MAX_ERROR_MESSAGE = 200
_MAX_ERROR_SUMMARY = 300

# CompileRunError 错误码前缀 → (HTTP 状态码, 固定文案)。
_ERROR_HTTP_STATUS = {
    "workspace_not_found": (404, "编译工作区不存在"),
    "wiki_page_not_found": (404, "Wiki 页面不存在"),
    "source_sync_run_not_found": (404, "来源同步任务不存在"),
    "workspace_not_active": (409, "编译工作区已归档"),
    "wiki_page_workspace_mismatch": (409, "Wiki 页面与工作区归属不一致"),
    "invalid_trigger_targets": (400, "触发目标参数不合法"),
    "idempotency_conflict": (409, "幂等键冲突：同一 key 对应不同请求"),
    "retry_attempts_exhausted": (409, "重试次数已达上限"),
    "run_superseded_use_create": (409, "任务已被新任务抢占，请重新创建"),
    "run_not_found": (404, "编译任务不存在"),
    "run_not_retryable_status": (409, "任务状态不可重试"),
    "run_not_cancellable_status": (409, "任务状态不可取消"),
    "retry_requires_failed_status": (409, "仅失败状态可重试"),
    "run_already_running": (409, "任务已在执行"),
    "pipeline_not_registered": (400, "编译流水线未注册"),
    "pipeline_version_not_registered": (400, "编译流水线版本未注册"),
    "invalid_trigger_type": (400, "触发类型不合法"),
    "run_not_executable": (409, "任务状态不可执行"),
}


def _error_code(message: str) -> str:
    """提取 CompileRunError 消息的错误码前缀（[:= ] 分隔取首 token）。"""
    return message.split(":", 1)[0].split("=", 1)[0].split(" ", 1)[0]


def _iso(value):
    return value.isoformat() if value is not None else None


def _clip(value: str | None, limit: int) -> str | None:
    """二次截断（API 侧兜底），永不返回超长原始文本。"""
    if not value:
        return value
    return value if len(value) <= limit else value[:limit] + "…"


def _metrics_summary(metrics_json: str | None) -> dict:
    if not metrics_json:
        return {}
    try:
        parsed = json.loads(metrics_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items() if k in _METRICS_WHITELIST}


def _serialize_run(run: CompileRun) -> dict:
    return {
        "id": run.id,
        "pipeline_key": run.pipeline_key,
        "pipeline_version": run.pipeline_version,
        "trigger_type": run.trigger_type,
        "trigger_object_id": run.trigger_object_id,
        "source_sync_run_id": run.source_sync_run_id,
        "workspace_id": run.workspace_id,
        "wiki_page_id": run.wiki_page_id,
        "status": run.status,
        "current_stage": run.current_stage,
        "output_revision_id": run.output_revision_id,
        # error 字段只回 safe（净化后）；error_summary 由 safe 消息生成并截断。
        "error_summary": _clip(run.error_summary, _MAX_ERROR_SUMMARY),
        "safe_error_code": run.safe_error_code,
        "safe_error_message": _clip(run.safe_error_message, _MAX_ERROR_MESSAGE),
        "attempt": run.attempt,
        "max_attempts": run.max_attempts,
        "cancel_requested": bool(run.cancel_requested),
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "heartbeat_at": _iso(run.heartbeat_at),
    }


def _serialize_stage(row: StageRun) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "stage_key": row.stage_key,
        "stage_order": row.stage_order,
        "status": row.status,
        "attempt": row.attempt,
        "retryable": row.retryable,
        "component_key": row.component_key,
        "component_version": row.component_version,
        "parent_stage_run_id": row.parent_stage_run_id,
        # Phase 4.2：error 字段只读 safe（服务端固定映射文案）；legacy
        # error_code/error_message 列不再经 API 暴露（不回退原始）。
        "error_code": row.safe_error_code or "",
        "error_message": _clip(row.safe_error_message, _MAX_ERROR_MESSAGE),
        "safe_error_code": row.safe_error_code,
        "safe_error_message": _clip(row.safe_error_message, _MAX_ERROR_MESSAGE),
        "metrics_summary": _metrics_summary(row.metrics_json),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "created_at": _iso(row.created_at),
    }


def _serialize_artifact(art: CompileArtifact) -> dict:
    return {
        "id": art.id,
        "run_id": art.run_id,
        "stage_run_id": art.stage_run_id,
        "artifact_type": art.artifact_type,
        "schema_version": art.schema_version,
        "object_type": art.object_type,
        "object_id": art.object_id,
        "content_hash": art.content_hash,
        "created_at": _iso(art.created_at),
    }


def _http_error(exc: CompileRunError) -> HTTPException:
    """按错误码前缀收敛为固定文案（绝不复用内部 detail / repr / 路径 / token）。"""
    code = _error_code(str(exc))
    status, detail = _ERROR_HTTP_STATUS.get(code, (400, "编译任务操作失败"))
    return HTTPException(status_code=status, detail=detail)


@router.get("/runs")
def list_runs(
    status: str | None = Query(default=None),
    pipeline_key: str | None = Query(default=None),
    workspace_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    query = db.query(CompileRun)
    if status:
        query = query.filter(CompileRun.status == status)
    if pipeline_key:
        query = query.filter(CompileRun.pipeline_key == pipeline_key)
    if workspace_id is not None:
        query = query.filter(CompileRun.workspace_id == workspace_id)
    total = query.count()
    rows = (
        query.order_by(CompileRun.created_at.desc(), CompileRun.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "runs": [_serialize_run(r) for r in rows],
    }


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    run = db.query(CompileRun).filter(CompileRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="编译任务不存在")
    stages = (
        db.query(StageRun)
        .filter(StageRun.run_id == run_id)
        .order_by(StageRun.stage_order, StageRun.attempt)
        .all()
    )
    artifacts = (
        db.query(CompileArtifact)
        .filter(CompileArtifact.run_id == run_id)
        .order_by(CompileArtifact.created_at, CompileArtifact.id)
        .all()
    )
    body = _serialize_run(run)
    body["stages"] = [_serialize_stage(s) for s in stages]
    body["artifacts"] = [_serialize_artifact(a) for a in artifacts]
    return body


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    """failed & attempt<max → 复用 requeue；attempt 用尽/superseded/终态 → 409。

    attempt 由下一次 claim 原子 +1；不得自动新建 run 绕开 attempt 上限。
    """
    try:
        run = executor.retry_run(
            db, run_id,
            created_by=current_user.get("id") or current_user.get("username"),
        )
    except CompileRunError as exc:
        db.rollback()
        raise _http_error(exc)
    db.commit()
    return {"run_id": run.id, "status": run.status, "attempt": run.attempt, "message": "已重新入队"}


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
):
    """queued → 直接 cancelled；running → 设 cancel_requested（worker 安全检查点级联）。"""
    try:
        run = executor.cancel_run(db, run_id)
    except CompileRunError as exc:
        db.rollback()
        raise _http_error(exc)
    db.commit()
    if run.status == "cancelled":
        return {"run_id": run.id, "status": "cancelled", "cancel_requested": False, "message": "已取消"}
    return {"run_id": run.id, "status": run.status, "cancel_requested": bool(run.cancel_requested), "message": "已请求取消"}
