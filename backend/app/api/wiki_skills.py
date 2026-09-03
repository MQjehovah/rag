"""Phase 6：Skill Registry API + Wiki Skill 覆盖/解锁（受控，不泄内部实现）。

- GET  /api/wiki-skills        → 全部 Skill 安全摘要（登录用户可访问）
- GET  /api/wiki-skills/{key}  → 单 Skill 安全摘要（未知 → 404）
- POST /api/wiki/{wiki_id}/skill-override  → admin/wiki_editor（编辑权限）精确设置并锁定 Skill
- POST /api/wiki/{wiki_id}/skill-unlock    → 解除 Skill 锁定（保留当前 skill/version）

安全字段过滤：
- 不返回 instructions 全文 / extraction schema 原文 / blueprint schema 原文 /
  runtime_key / Python 路径 / Loader 内部错误 / Prompt / Secret；
- unknown Skill / 内部 Registry 异常一律收敛为受控 400/404（不返回内部异常文本）；
- override/unlock 不修改 Workspace/ACL/Topic，不调用 legacy builder；
- override 会创建持久化 manual_rebuild CompileRun（pipeline 使用 Registry active
  version，默认 v3），仅入队不阻塞 LLM。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core import access_control
from app.core.jwt_utils import get_current_user
from app.core.wiki_skills import registry as skill_registry
from app.core.wiki_skills.registry import SkillError
from app.core.wiki_skills.schemas import SKILL_DECISION_SCHEMA, SkillDecision
from app.models.database import WikiPage

router = APIRouter(prefix="/api/wiki-skills", tags=["Wiki Skills"])
wiki_skill_router = APIRouter(prefix="/api/wiki", tags=["Wiki Skills"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 安全序列化（白名单字段；不泄 instructions/schema/runtime/内部错误）
# ---------------------------------------------------------------------------


def _serialize_skill_summary(key: str, info: dict) -> dict:
    active = info.get("active_version")
    versions = list(info.get("versions") or {})
    active_desc = (info.get("versions") or {}).get(active) if active else None
    return {
        "key": key,
        "active_version": active,
        "versions": versions,
        "label": (active_desc or {}).get("label", "") if active_desc else "",
        "description": (active_desc or {}).get("description", "") if active_desc else "",
        "availability": "available" if active else "unavailable",
    }


def _require_skill_editor(db: Session, current_user: dict, wiki_id: str) -> WikiPage:
    """编辑权限：admin 或对该 Wiki 有编辑权限的 wiki_editor；普通用户拒绝（403）。

    不可见/无权沿用 wiki.py._require_edit_wiki 既有语义（存在行且无编辑权 → 403），
    不泄露不同用户间的可见性差异。
    """
    page = db.query(WikiPage).filter(WikiPage.id == wiki_id).first()
    if page is None:
        raise HTTPException(status_code=404, detail="主题页不存在")
    if not access_control.can_edit_wiki(db, current_user, page):
        raise HTTPException(status_code=403, detail="无权编辑该 Wiki")
    return page


def _skill_audit_decision(
    *, wiki: WikiPage, skill_key: str, skill_version: str,
    selected_by: str, locked: bool, reason_code: str, confidence: float,
) -> dict:
    """安全结构化 SkillDecision 审计摘要（无正文/Prompt/ACL/内部信息）。"""
    return SkillDecision(
        target_key=wiki.id or "",
        wiki_page_id=wiki.id,
        selected_skill=skill_key,
        selected_version=skill_version,
        selected_by=selected_by,
        confidence=confidence,
        status="locked" if locked else "selected",
        reason_code=reason_code,
        previous_skill=wiki.content_skill,
        previous_version=wiki.skill_version,
        locked=locked,
    ).to_dict()


# ---------------------------------------------------------------------------
# 公开查询
# ---------------------------------------------------------------------------


@router.get("")
def list_skills(current_user=Depends(get_current_user)):
    """登录用户可见的全部 Skill 安全摘要。"""
    return {"skills": [
        _serialize_skill_summary(key, info)
        for key, info in sorted(skill_registry.snapshot().items())
    ]}


@router.get("/{key}")
def get_skill(key: str, current_user=Depends(get_current_user)):
    """单 Skill 安全摘要；未知 Skill → 404（不返回内部 Registry 异常）。"""
    snapshot = skill_registry.snapshot()
    info = snapshot.get(key)
    if info is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return _serialize_skill_summary(key, info)


# ---------------------------------------------------------------------------
# Wiki Skill 覆盖 / 解锁
# ---------------------------------------------------------------------------


class SkillOverridePayload(BaseModel):
    skill_key: str
    skill_version: str
    lock: bool = True
    recompile: bool = False


class SkillUnlockPayload(BaseModel):
    recompile: bool = False


def _active_pipeline_version() -> str | None:
    """Pipeline Registry 当前 active version；无 active pipeline → None（不猜测 v2）。"""
    from app.core.wiki_pipeline import registry as pregs
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default import PIPELINE_KEY

    pipeline = pregs.get_pipeline(PIPELINE_KEY)
    if pipeline is None:
        return None
    return pipeline.version


def _create_skill_rebuild_run(db: Session, wiki: WikiPage, current_user: dict) -> dict | None:
    """recompile=true：创建持久化 manual_rebuild CompileRun（仅入队，不阻塞 LLM）。

    - pipeline_version 取 Pipeline Registry 当前 active version 并显式固化；
    - Registry 无 active pipeline → 返回 None（调用方回滚 → 受控 409），不猜测 v2；
    - active=v2（紧急回滚）且目标 wiki 为 api_reference → 受控拒绝（返回 None，
      调用方回滚 → 409），绝不创建必然错误的 Run；
    - 失败（workspace 缺失/注册缺失等）返回 None，由调用方收敛为受控错误；
      不调用 legacy builder。
    """
    if not wiki.workspace_id:
        return None
    from app.core.wiki_pipeline import executor
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default import PIPELINE_KEY

    active = _active_pipeline_version()
    if active is None:
        return None
    if active == "2" and (wiki.content_skill or None) == "api_reference":
        return None

    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        pipeline_version=active,
        trigger_type="manual_rebuild",
        trigger_object_id=wiki.id,
        workspace_id=wiki.workspace_id,
        wiki_page_id=wiki.id,
        supersede_same_trigger=True,
        created_by=current_user.get("id") or current_user.get("username"),
    )
    return {
        "run_id": run.id,
        "pipeline_key": run.pipeline_key,
        "pipeline_version": run.pipeline_version,
        "trigger_type": run.trigger_type,
        "status": run.status,
    }


def _http_skill_error(exc: SkillError) -> HTTPException:
    """Registry 受控错误 → 固定 400/404（不返回内部异常文本）。"""
    message = str(exc)
    if "not_found" in message or "skill_not_found" in message:
        return HTTPException(status_code=404, detail="Skill 不存在")
    return HTTPException(status_code=400, detail="Skill 参数不合法")


@wiki_skill_router.post("/{wiki_id}/skill-override")
def skill_override(
    wiki_id: str,
    payload: SkillOverridePayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """精确覆盖并（默认）锁定 Wiki Skill。

    - 精确验证 skill_key/version 已注册（否则 400/404 受控错误）；
    - 更新 WikiPage skill 字段 + skill_decision_json 安全审计摘要；
    - 不修改 Workspace/ACL/Topic；
    - recompile=false：只更新选择与锁定，不重写 Revision、不调 legacy builder；
    - recompile=true：创建持久化 manual_rebuild CompileRun（active pipeline
      version），仅入队。
    """
    wiki = _require_skill_editor(db, current_user, wiki_id)
    try:
        if not skill_registry.has(payload.skill_key, payload.skill_version):
            raise skill_registry.SkillNotFound(
                f"skill_not_found={payload.skill_key}:{payload.skill_version}"
            )
    except SkillError as exc:
        db.rollback()
        raise _http_skill_error(exc)

    audit = _skill_audit_decision(
        wiki=wiki,
        skill_key=payload.skill_key,
        skill_version=payload.skill_version,
        selected_by="manual",
        locked=bool(payload.lock),
        reason_code="MANUAL_OVERRIDE",
        confidence=1.0,
    )
    wiki.content_skill = payload.skill_key
    wiki.skill_version = payload.skill_version
    wiki.skill_selected_by = "manual"
    wiki.skill_confidence = 1.0
    wiki.skill_locked = bool(payload.lock)
    wiki.skill_decision_json = json.dumps(audit, ensure_ascii=False, sort_keys=True)

    run_summary = None
    if payload.recompile:
        try:
            run_summary = _create_skill_rebuild_run(db, wiki, current_user)
        except Exception:  # noqa: BLE001
            db.rollback()
            raise HTTPException(status_code=409, detail="无法创建重建任务")
        if run_summary is None:
            # 无 Workspace / create_run 未返回 Run：全部回滚（Skill 字段不提交、
            # 不留下残缺 CompileRun），返回受控 409。
            db.rollback()
            raise HTTPException(status_code=409, detail="无法创建重建任务")
    db.commit()
    return {
        "message": "Skill 已设置",
        "wiki_id": wiki.id,
        "content_skill": wiki.content_skill,
        "skill_version": wiki.skill_version,
        "skill_selected_by": wiki.skill_selected_by,
        "skill_locked": bool(wiki.skill_locked),
        "run": run_summary,
    }


@wiki_skill_router.post("/{wiki_id}/skill-unlock")
def skill_unlock(
    wiki_id: str,
    payload: SkillUnlockPayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """解除 Skill 锁定：skill_locked=false，保留当前 content_skill/version。

    写安全 SkillDecision 审计摘要；后续自动编译重新执行 Router。
    可选 recompile=true 提交 v2 manual_rebuild Run。
    """
    wiki = _require_skill_editor(db, current_user, wiki_id)
    audit = _skill_audit_decision(
        wiki=wiki,
        skill_key=wiki.content_skill or "default",
        skill_version=wiki.skill_version or "1",
        selected_by="manual",
        locked=False,
        reason_code="MANUAL_UNLOCK",
        confidence=1.0,
    )
    wiki.skill_locked = False
    wiki.skill_decision_json = json.dumps(audit, ensure_ascii=False, sort_keys=True)

    run_summary = None
    if payload.recompile:
        try:
            run_summary = _create_skill_rebuild_run(db, wiki, current_user)
        except Exception:  # noqa: BLE001
            db.rollback()
            raise HTTPException(status_code=409, detail="无法创建重建任务")
        if run_summary is None:
            # 无 Workspace / create_run 未返回 Run：全部回滚（解锁状态不提交、
            # 不留下残缺 CompileRun），返回受控 409。
            db.rollback()
            raise HTTPException(status_code=409, detail="无法创建重建任务")
    db.commit()
    return {
        "message": "Skill 已解锁",
        "wiki_id": wiki.id,
        "content_skill": wiki.content_skill,
        "skill_version": wiki.skill_version,
        "skill_locked": bool(wiki.skill_locked),
        "run": run_summary,
    }
