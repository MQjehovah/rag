"""阶段 8C/8D：演化运行控制 + 晋升/回退的管理员端点（写路径）。

原则：
- 与只读控制台同源的服务端受控根映射；所有写操作都经 control / business_ops 服务，
  只接受 根标识 + 库内标识（run_id/experiment_id/version_id），绝不接受文件路径；
- 独立功能开关 wikiskill_evolution_admin_enabled（默认 False）；管理员鉴权同上；
- API 不长时间同步等待演化：start/resume 只 spawn worker 并立即返回；
- 取消/暂停/恢复均不删除记录、不回退已接受技能（见 control 模块注释）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, Field
import sqlalchemy as sa

from app.api.deps import get_db
from app.config import settings
from app.core import access_control
from app.core.jwt_utils import get_current_user
from app.core.skill_evolution import business_ops as bops
from app.core.skill_evolution import control

router = APIRouter(prefix="/api/evolution-admin", tags=["evolution-admin"])

_ID_RE = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"


def _require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if not access_control.is_admin(current_user):
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN,
                            detail="需要管理员权限")
    return current_user


def _feature_gate() -> None:
    if not settings.wikiskill_evolution_admin_enabled:
        raise HTTPException(
            status_code=503,
            detail="演化运行控制未启用"
                   "（wikiskill_evolution_admin_enabled=false）")


def _roots() -> dict[str, Path]:
    raw = (settings.wikiskill_console_roots or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500,
                            detail="实验根配置非法（非 JSON）") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="实验根配置必须是对象")
    out: dict[str, Path] = {}
    for kid, val in data.items():
        if not kid or any(c not in _ID_RE for c in kid) or len(kid) > 64:
            raise HTTPException(status_code=500, detail=f"根标识非法: {kid!r}")
        p = Path(str(val))
        if not p.is_absolute() or not p.is_dir():
            raise HTTPException(status_code=500,
                                detail=f"实验根不存在或非绝对目录: {kid!r}")
        out[kid] = p
    return out


def _root_dir(root: str) -> Path:
    roots = _roots()
    if root not in roots:
        raise HTTPException(status_code=404, detail="未知实验根标识")
    return roots[root]


def _http(exc: control.ControlError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


class CreateBody(BaseModel):
    root: str
    dataset_version: str
    model_mode: str = "simulated"
    init_mode: str = "business"
    max_iterations: int = Field(3, ge=1, le=20)
    max_model_calls: int = Field(90, ge=2)
    max_tool_calls: int = Field(40, ge=1)
    max_seconds: int = Field(3600, ge=1)
    experience: str = "full"
    notes: str = ""
    review: str | None = None   # "v2" → 数据集评分器必须为 v2（评审经独立模型）


class ActionBody(BaseModel):
    root: str
    actor: str = "console-api"
    # 真实模式每次启动/恢复的显式确认（参数须与运行记录一致；模拟模式忽略）。
    explicit_confirm: bool = False
    confirm_dataset_version: str | None = None
    confirm_max_iterations: int | None = None
    confirm_max_model_calls: int | None = None
    confirm_max_tool_calls: int | None = None
    confirm_max_seconds: int | None = None
    confirm_config_fingerprint: str | None = None
    confirm_reviewer_fingerprint: str | None = None


def _confirm_or_none(body: ActionBody) -> dict | None:
    if not body.explicit_confirm:
        return None
    return {"explicit": True,
            "dataset_version": body.confirm_dataset_version,
            "max_iterations": body.confirm_max_iterations,
            "max_model_calls": body.confirm_max_model_calls,
            "max_tool_calls": body.confirm_max_tool_calls,
            "max_seconds": body.confirm_max_seconds,
            "config_fingerprint": body.confirm_config_fingerprint,
            "reviewer_fingerprint": body.confirm_reviewer_fingerprint}


@router.post("/experiments")
def create_experiment(body: CreateBody,
                      _: dict = Depends(_require_admin)) -> dict:
    _feature_gate()
    root = _root_dir(body.root)
    try:
        return control.create_experiment_and_run(
            root, dataset_version=body.dataset_version,
            init_mode=body.init_mode, max_iterations=body.max_iterations,
            budget={"max_model_calls": body.max_model_calls,
                    "max_tool_calls": body.max_tool_calls,
                    "max_seconds": body.max_seconds},
            model_mode=body.model_mode, experience=body.experience,
            notes=body.notes, review=body.review)
    except control.ControlError as exc:
        raise _http(exc) from exc


@router.get("/runs/{run_id}")
def get_run_control(run_id: str, root: str = Query(...),
                    _: dict = Depends(_require_admin)) -> dict:
    _feature_gate()
    try:
        root_dir = _root_dir(root)
        view = control.run_state(root_dir, run_id)
        view["model_mode"] = control.model_mode_of(root_dir, run_id)
        view.update(control.runtime_fingerprints_of(root_dir, run_id))
        return view
    except control.ControlError as exc:
        raise _http(exc) from exc


@router.get("/runs/{run_id}/start-preview")
def start_preview(run_id: str, root: str = Query(...),
                  _: dict = Depends(_require_admin)) -> dict:
    """无网络配置预览：模型/端点 host/输出限制/重试/数据范围/预算/完整指纹。
    不展示密钥、不发探测请求；确认指纹后 start 绑定，配置漂移将被拒绝。"""
    _feature_gate()
    try:
        return control.start_preview(_root_dir(root), run_id)
    except control.ControlError as exc:
        raise _http(exc) from exc


@router.post("/runs/{run_id}/start")
def start_run(run_id: str, body: ActionBody,
              _: dict = Depends(_require_admin)) -> dict:
    _feature_gate()
    try:
        return control.start(_root_dir(body.root), run_id,
                             actor=body.actor or "console-api",
                             confirm=_confirm_or_none(body))
    except control.ControlError as exc:
        raise _http(exc) from exc


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str, body: ActionBody,
               _: dict = Depends(_require_admin)) -> dict:
    _feature_gate()
    try:
        return control.resume(_root_dir(body.root), run_id,
                              actor=body.actor or "console-api",
                              confirm=_confirm_or_none(body))
    except control.ControlError as exc:
        raise _http(exc) from exc


@router.post("/runs/{run_id}/pause")
def pause_run(run_id: str, body: ActionBody,
              _: dict = Depends(_require_admin)) -> dict:
    _feature_gate()
    try:
        return control.pause(_root_dir(body.root), run_id,
                             actor=body.actor or "console-api")
    except control.ControlError as exc:
        raise _http(exc) from exc


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, body: ActionBody,
               _: dict = Depends(_require_admin)) -> dict:
    _feature_gate()
    try:
        return control.cancel(_root_dir(body.root), run_id,
                              actor=body.actor or "console-api")
    except control.ControlError as exc:
        raise _http(exc) from exc


@router.get("/meta")
def admin_meta(_: dict = Depends(_require_admin)) -> dict:
    """数据集目录 + 评分器状态（机器可读字段；创建/确认对话框展示）。"""
    from app.config import settings as _s
    from app.core.skill_evolution.grader_registry import GRADER_V2
    _feature_gate()
    resolved: list[dict] = []
    for d in control.list_datasets():
        try:
            ds = control.load_dataset_spec(d)
            gi = control.grader_ready(ds.grader_version)
        except control.ControlError:
            gi = {"grader_version": "?", "purpose": "未知", "ready": False,
                  "description": "数据集不可读"}
        gv = gi.get("grader_version")
        required_review = "v2" if gv == GRADER_V2 else None
        resolved.append({
            "dataset_version": d,
            "grader_version": gv,
            "grader_ready": bool(gi.get("ready")),
            "grader_role": gi.get("role"),
            "grader_calibration": gi.get("calibration"),
            "labels_approved": bool(gi.get("labels_approved")),
            "real_calibration": bool(gi.get("real_calibration")),
            "required_review": required_review,
            "allow_business_promotion": bool(gi.get("allow_business_promotion")),
            "grader_purpose": gi.get("purpose"),
            "grader_description": gi.get("description"),
        })
    return {
        "datasets": resolved,
        "grader_note": ("v1 仅标注为机制验证用途；v2 语义评审已人工标签批准"
                        "（labels_approved=true，human-project-owner，2026-09-08）"
                        "但真实校准未完成（real_calibration=false，"
                        "calibration=engineering_only），不得用于正式业务晋升。"),
        "real_start_enabled": bool(_s.wikiskill_evolution_admin_real_enabled),
        "real_link_verified": False,
        "effect_verified": False,
    }


# ---------------------------------------------------------------------------
# 8D：业务晋升与回退（business 绑定写在业务库；实验证据读实验根）
# ---------------------------------------------------------------------------


class PromoteBody(BaseModel):
    root: str
    experiment_id: str
    workspace_id: str
    allow_simulated: bool = False
    actor: str = "console-api"
    # 显式预期令牌（首次绑定 = rev=0 + NO_BINDING_HASH；有绑定 = 当前 rev/规范哈希）
    expected_set_hash: str | None = None
    expected_rev: int | None = None
    # 提交绑定“预览时”的精确目标集合（skill_id/version_id/content_hash/seq）
    target_members: list[dict] | None = None
    idempotency_key: str | None = None


class RollbackBody(BaseModel):
    workspace_id: str
    actor: str = "console-api"
    expected_set_hash: str | None = None
    expected_rev: int | None = None
    idempotency_key: str | None = None


def _business_ops_error(exc: Exception) -> HTTPException:
    from app.core.skill_evolution.business_ops import BusinessOpsError
    if isinstance(exc, BusinessOpsError):
        return HTTPException(status_code=exc.status_code, detail=exc.message)
    return HTTPException(status_code=500, detail=str(exc))


def _experiment_row(db_exp, experiment_id: str):
    from types import SimpleNamespace
    row = db_exp.execute(sa.text(
        "SELECT * FROM evolution_experiments WHERE experiment_id=:e"),
        {"e": experiment_id}).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="实验不存在")
    ns = SimpleNamespace()
    for k in row._mapping.keys():
        setattr(ns, k, row._mapping[k])
    return ns


@router.get("/business/state")
def business_state(workspace_id: str = Query(...),
                   db=Depends(get_db),
                   _: dict = Depends(_require_admin)) -> dict:
    """业务库当前作用域绑定状态（含 schema 是否已迁移 + 生效历史）。"""
    _feature_gate()
    try:
        return bops.business_state(db, workspace_id)
    except Exception as exc:  # noqa: BLE001
        raise _business_ops_error(exc) from exc


@router.get("/experiments/{experiment_id}/promotion/preview")
def promotion_preview(experiment_id: str, root: str = Query(...),
                      workspace_id: str = Query(...),
                      db=Depends(get_db),
                      _: dict = Depends(_require_admin)) -> dict:
    """晋升前展示：来源实验/精确版本/评估有效性/模拟或真实证据/目标作用域。"""
    _feature_gate()
    from app.core.skill_evolution import skill_store
    db_exp = skill_store.session_for(_root_dir(root))
    try:
        exp = _experiment_row(db_exp, experiment_id)
        try:
            preview = bops.promotion_preview(db_exp, exp,
                                             workspace_id=workspace_id)
            preview["business_provisioned"] = bops.schema_provisioned(db)
            try:
                bstate = bops.business_state(db, workspace_id)
            except Exception:  # noqa: BLE001
                bstate = {"no_binding": True, "expected_rev": 0,
                          "expected_set_hash": None}
            preview["promotion_env"] = bops.promotion_env()
            preview["binding"] = {
                "no_binding": bstate.get("no_binding", True),
                "expected_rev": bstate.get("expected_rev"),
                "expected_set_hash": bstate.get("expected_set_hash"),
                "current_set_hash": bstate.get("current_set_hash"),
                "current": bstate.get("current"),
            }
            return preview
        except Exception as exc:  # noqa: BLE001
            raise _business_ops_error(exc) from exc
    finally:
        db_exp.close()


@router.post("/business/promote")
def business_promote(body: PromoteBody, db=Depends(get_db),
                     _: dict = Depends(_require_admin)) -> dict:
    _feature_gate()
    from app.core.skill_evolution import skill_store
    db_exp = skill_store.session_for(_root_dir(body.root))
    try:
        exp = _experiment_row(db_exp, body.experiment_id)
        try:
            return bops.promote(
                db, db_exp, exp, workspace_id=body.workspace_id,
                created_by=body.actor or "console-api",
                allow_simulated=bool(body.allow_simulated),
                expected_set_hash=body.expected_set_hash,
                expected_rev=body.expected_rev,
                target_members=body.target_members,
                idempotency_key=body.idempotency_key,
                explicit_cas=True)
        except Exception as exc:  # noqa: BLE001
            raise _business_ops_error(exc) from exc
    finally:
        db_exp.close()


@router.post("/business/rollback")
def business_rollback(body: RollbackBody, db=Depends(get_db),
                      _: dict = Depends(_require_admin)) -> dict:
    _feature_gate()
    try:
        return bops.rollback(db, workspace_id=body.workspace_id,
                             created_by=body.actor or "console-api",
                             expected_set_hash=body.expected_set_hash,
                             expected_rev=body.expected_rev,
                             idempotency_key=body.idempotency_key,
                             explicit_cas=True)
    except Exception as exc:  # noqa: BLE001
        raise _business_ops_error(exc) from exc
