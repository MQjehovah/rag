"""Phase 7C.3-A/3-B：wiki.default version 3 单目标发布闭环（默认 Skill + API Reference + 迁移）。

Stage 顺序：
    resolve_context → topic_route → skill_route → synthesize_by_skill →
    validate_by_skill → publish_by_skill → finalize_compile_outcome → schedule_graph

Phase 7C.3-A 范围（封板）：
- pipeline key 仍为 wiki.default、version="3"，只支持单目标（一个 Wiki / 一个
  Workspace）：page_changed / manual_rebuild；
- default 分支复用 v1/v2 已有实现（不复制默认 Wiki 编译逻辑）；
- API Reference 分支调用已完成的 api_reference compiler（db_adapter →
  compile_api_reference），在 Executor 的 SAVEPOINT / lease fence / 统一提交边界内
  原子发布（Revision + Section + Binding + WikiPage + dirty）；
- default 与 api_reference 分派由已持久化的 skill_decision Artifact 决定，
  绝不根据正文再次猜测 Skill；
- locked Wiki 必须沿用锁定的精确 skill_key + skill_version；
- 不支持的 Skill/版本/触发类型 fail closed（固定安全错误码），不静默回退 default；
- 不调用 _legacy_*、不自行 commit/rollback、不获取底层 Connection。

Phase 7C.3-B 范围（封板）：
- migration_proposed 显式目标契约（proposed_skill/proposed_version）；
- default → api_reference 单目标 shadow compile（失败零产品写，Run failed）；
- protected/manual Section 复制保留与冲突规则（key 冲突 protected 胜出；NULL key
  全保留；order_index 连续重排；历史 Binding 快照复制）；
- shadow compile + validate 通过后在 publish_by_skill 内原子切换 Skill
  （selected_by=migration；reason_code=MIGRATION_APPLIED；原 proposal 保留在
  skill_decision Artifact，不覆盖审计历史）。

未实现（本轮明确不支持，遇即受控 not-supported/failed）：
- batch_rebuild / mixed batch；page_deleted；
- api_reference → default 及任意未知方向迁移（MIGRATION_DIRECTION_NOT_SUPPORTED）；
- 生产 startup 注册（不修改 main.py；active 仍为 v2）；前端修改。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid

from app.core.wiki_pipeline import registry as pipe_registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    PIPELINE_KEY,
    _finish_publish,
    _stage_finalize_compile_outcome,
    _stage_resolve_context,
    _stage_schedule_graph,
    _stage_synthesize_default,
    _stage_topic_route,
    _stage_validate_default,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
    _stage_publish_skilled as _V2_STAGE_PUBLISH_SKILLED,
    _stage_skill_route,
)
from app.core.wiki_skills.api_reference import compiler as api_compiler_mod
from app.core.wiki_skills.api_reference.compiler import ApiCompileResult
from app.core.wiki_skills.api_reference.db_adapter import (
    build_api_source_documents_with_diagnostics,
)
from app.core.wiki_skills.api_reference.identity import (
    build_endpoint_section_key,
)
from app.core.wiki_skills.schemas import SkillDecision
from app.models.database import (
    EvidenceItem,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    WikiWorkspace,
)

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "3"

# skill_route 产物类型（与 v2 一致，本模块按同一 Artifact 读取）。
ARTIFACT_TYPE_SKILL_DECISION = "skill_decision"

# 本版本支持的触发类型（单目标）。
_SUPPORTED_TRIGGERS = ("page_changed", "manual_rebuild")

_V3_STAGE_KEYS = (
    "resolve_context",
    "topic_route",
    "skill_route",
    "synthesize_by_skill",
    "validate_by_skill",
    "publish_by_skill",
    "finalize_compile_outcome",
    "schedule_graph",
)

_V3_DESCRIPTIONS = {
    "resolve_context": "解析 Page/Workspace/Scope/input_hash（无写）",
    "topic_route": "主题路由（只读；不写 Wiki/Revision）",
    "skill_route": "Skill 路由：产出持久化 SkillDecision（无写）",
    "synthesize_by_skill": "按已持久化 SkillDecision 分派：default 复用 v1 / api_reference 内存编译",
    "validate_by_skill": "按分派结果校验（default 复用 v1；api_reference 依据编译校验）",
    "publish_by_skill": "唯一产品写：default 复用 v2 / api_reference 原子发布 + Manifest",
    "finalize_compile_outcome": "按 Manifest 判定 Run 级编译结果（失败语义真实化）",
    "schedule_graph": "读 Manifest 对 wiki/page 目标同步真实图谱重建（幂等）",
}

_HASH64_RE = re.compile(r"^[0-9a-f]{64}$")

# 文档级 Section 的 field_path 前缀（与 evidence/coverage 规约一致）。
_DOC_FIELD_PREFIX = {
    "overview": "overview",
    "authentication": "authentication.",
    "common_conventions": "common_headers.",
    "data_models": "data_models.",
    "error_codes": "common_errors.",
    "version_notes": "version_notes.",
}
# usage_type=example 不落 DB（DB CHECK 只允许 support/conflict）。
_DB_USAGE_TYPES = ("support", "conflict")


# ---------------------------------------------------------------------------
# 基础工具（纯函数/只读；不 commit）
# ---------------------------------------------------------------------------


def _safe_error(code: str, *, retryable: bool = False) -> dict:
    """固定安全失败结果：error_code + registry 统一安全文案（唯一来源）。

    绝不携带内部异常文本/正文/路径；未知错误码由 registry 收敛为统一兜底文案。
    """
    return {
        "ok": False,
        "error_code": code,
        "error_message": pipe_registry.stage_error_message(code),
        "retryable": retryable,
    }


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _is_hash64(value) -> bool:
    return isinstance(value, str) and bool(_HASH64_RE.match(value))


def _normalize_title(title: str) -> str:
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        normalize_wiki_title,
    )

    return normalize_wiki_title(title or "")


def _existing_wiki_by_norm(db, workspace_id, norm_title: str):
    """只读：workspace 内标题规范相等的既有 Wiki。"""
    if not workspace_id or not norm_title:
        return None
    rows = db.query(WikiPage).filter(WikiPage.workspace_id == workspace_id).all()
    for w in rows:
        if _normalize_title(w.title or "") == norm_title:
            return w
    return None


def _read_skill_decision_payload(db, run_id: str) -> dict | None:
    """读本 run 最近一条已持久化的 skill_decision Artifact（跨 attempt 保留）。"""
    from app.models.database import KnowledgeCompileArtifact

    row = (
        db.query(KnowledgeCompileArtifact)
        .filter(
            KnowledgeCompileArtifact.run_id == run_id,
            KnowledgeCompileArtifact.artifact_type == ARTIFACT_TYPE_SKILL_DECISION,
        )
        .order_by(KnowledgeCompileArtifact.created_at.desc())
        .first()
    )
    if row is None or not row.payload_json:
        return None
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _lease_fence_ok(db, run) -> bool:
    """发布前租约预检（尽力而为；最终权威 fence 由 Executor 边界保证）。

    stage 内不持有独立 Connection/不自行提交；只 refresh 当前 run 行做只读检查。
    queued（测试直调）/未领取场景不误判。
    """
    if run is None:
        return True
    try:
        db.refresh(run)
    except Exception:  # noqa: BLE001
        return False
    if run.status not in ("running", "queued"):
        return False
    if run.lease_expires_at is None:
        return True  # 未领取：由 executor claim/claim fence 负责
    from datetime import datetime

    return run.lease_expires_at > datetime.utcnow()


# ---------------------------------------------------------------------------
# v3 分派（default / api_reference 由已持久化 SkillDecision 决定）
# ---------------------------------------------------------------------------


def _op_title_for_norm(topic: dict, norm: str) -> str:
    for op in topic.get("ops") or []:
        title = (op.get("title") or "").strip()
        if title and _normalize_title(title) == norm:
            return title
    return norm


def _count_page_changed_targets(topic: dict) -> int:
    """page_changed：去重后的合法 create/update 目标数（规范标题维度）。"""
    seen: set[str] = set()
    for op in topic.get("ops") or []:
        action = op.get("action")
        title = (op.get("title") or "").strip()
        if action not in ("create", "update") or not title:
            continue
        seen.add(_normalize_title(title))
    return len(seen)


def _decode_decision(raw) -> SkillDecision | None:
    if not isinstance(raw, dict):
        return None
    try:
        return SkillDecision.from_dict(raw)
    except ValueError:
        return None


def _match_decision(decisions: list[SkillDecision], *, wiki_id: str = "",
                    norm_title: str = "") -> SkillDecision | None:
    """在已持久化决策中按 wiki id / 规范标题精确匹配（不猜测）。"""
    for d in decisions:
        if d.target_key == wiki_id:
            return d
    if norm_title:
        for d in decisions:
            if _normalize_title(d.target_key or "") == norm_title:
                return d
    return None


def _build_v3_plan(db, run, ctx) -> tuple[dict | None, dict | None]:
    """构造 v3 单目标分派计划（只读；返回 (plan, None) 或 (None, safe_error)）。"""
    state = ctx.setdefault("state", {})
    context = state.get("context") or {}
    topic = state.get("topic") or {}
    trigger = run.trigger_type or context.get("trigger_type")

    if trigger not in _SUPPORTED_TRIGGERS:
        return None, _safe_error("TRIGGER_NOT_SUPPORTED")

    payload = _read_skill_decision_payload(db, run.id)
    decisions = [_decode_decision(d) for d in (payload or {}).get("decisions") or []]
    decisions = [d for d in decisions if d is not None]

    if trigger == "manual_rebuild":
        wiki_id = context.get("wiki_page_id")
        wiki = db.get(WikiPage, wiki_id) if wiki_id else None
        if wiki is None or not context.get("applicable"):
            # 无 wiki / 上下文不可用：交由 default 路径处理 no-op 语义（与 v2 一致）。
            return {"branch": "default", "mode": None, "trigger": trigger,
                    "wiki_id": None, "norm_title": "", "title": "",
                    "page_ids": [], "decision": None}, None
        decision = _match_decision(decisions, wiki_id=wiki.id,
                                   norm_title=_normalize_title(wiki.title or ""))
        page_ids = sorted({p for p in (context.get("source_page_ids") or ()) if p})
        plan = {"branch": None, "mode": "rebuild", "trigger": trigger,
                "wiki_id": wiki.id, "norm_title": _normalize_title(wiki.title or ""),
                "title": wiki.title or "", "page_ids": page_ids, "decision": None}
    else:  # page_changed
        target_count = _count_page_changed_targets(topic)
        if target_count == 0:
            # 无发布目标（not_worthy / invalid / service_unavailable 等）：
            # 交由 default 路径处理 keep-dirty/noop 语义（与 v2 一致）。
            return {"branch": "default", "mode": None, "trigger": trigger,
                    "wiki_id": None, "norm_title": "", "title": "",
                    "page_ids": [], "decision": None}, None
        if target_count > 1:
            return None, _safe_error("MULTI_TARGET_NOT_SUPPORTED")
        norm = sorted({
            _normalize_title(op.get("title") or "")
            for op in topic.get("ops") or []
            if op.get("action") in ("create", "update")
            and (op.get("title") or "").strip()
        })[0]
        workspace_id = context.get("workspace_id")
        existing = _existing_wiki_by_norm(db, workspace_id, norm)
        decision = _match_decision(decisions, norm_title=norm)
        if existing is not None:
            source_ids = set()
            if existing.source_page_ids:
                try:
                    source_ids.update(
                        str(x) for x in json.loads(existing.source_page_ids or "[]")
                    )
                except (TypeError, ValueError):
                    source_ids = set()
            if context.get("page_id"):
                source_ids.add(context["page_id"])
            page_ids = sorted({p for p in source_ids if p})
            mode = "update"
        else:
            page_ids = sorted({context["page_id"]} if context.get("page_id") else {})
            mode = "create"
        plan = {"branch": None, "mode": mode, "trigger": trigger,
                "wiki_id": existing.id if existing else None,
                "norm_title": norm,
                "title": (existing.title if existing else _op_title_for_norm(topic, norm)),
                "page_ids": page_ids, "decision": None}
        if existing is None and not page_ids:
            return None, _safe_error("PAGE_STALE")

    if decision is None:
        # 有明确发布目标但无对应已持久化决策 → fail closed（禁止猜测分派）。
        if plan["mode"] == "rebuild" and plan["wiki_id"] and not decisions:
            # 历史兼容分支：skill_route 对无可用上下文时的重建不产 decision，属
            # 不可用上下文，交由 default 路径 no-op（wiki_not_found/no-op）。
            return {"branch": "default", "mode": None, "trigger": trigger,
                    "wiki_id": None, "norm_title": "", "title": "",
                    "page_ids": [], "decision": None}, None
        return None, _safe_error("SKILL_DECISION_MISSING")

    status = decision.status or ""
    if status == "migration_proposed":
        # Phase 7C.3-B：显式迁移契约（只读校验，产物写留到 publish）。
        return _resolve_migration_plan(db, plan, decision)

    skill = decision.selected_skill
    if skill not in ("default", "api_reference"):
        return None, _safe_error("SKILL_NOT_SUPPORTED")
    from app.core.wiki_skills import registry as skill_registry

    if not skill_registry.has(skill, decision.selected_version or ""):
        # 决策引用的精确 skill_version 未注册 → fail closed（绝不猜测/降级）。
        return None, _safe_error("SKILL_NOT_SUPPORTED")

    plan["branch"] = skill
    plan["migration"] = False
    plan["proposed_skill"] = None
    plan["proposed_version"] = None
    plan["decision"] = decision.to_dict()
    return plan, None


def _proposal_in_candidates(decision: SkillDecision) -> bool:
    """proposed 精确组合必须存在于本次 candidates（不猜测）。"""
    from collections.abc import Mapping

    if not decision.proposed_skill or not decision.proposed_version:
        return False
    for c in decision.candidates:
        if not isinstance(c, Mapping):
            continue
        if c.get("skill_key") == decision.proposed_skill and \
                c.get("skill_version") == decision.proposed_version:
            return True
    return False


def _resolve_migration_plan(db, plan: dict, decision: SkillDecision):
    """migration_proposed → 校验显式迁移契约（只读）。

    fail closed：缺显式目标 / 目标与当前相同 / 目标未注册 / 目标不在 candidates /
    方向非 default→api_reference / locked / DB 当前 Skill 与 selected 不一致。
    """
    from app.core.wiki_skills import registry as skill_registry

    if not decision.proposed_skill or not decision.proposed_version:
        return None, _safe_error("MIGRATION_TARGET_MISSING")
    if (decision.proposed_skill == decision.selected_skill and
            decision.proposed_version == decision.selected_version):
        return None, _safe_error("MIGRATION_TARGET_INVALID")
    if decision.selected_skill != "default" or decision.proposed_skill != "api_reference":
        # 本轮只支持 default 精确版本 → api_reference 精确版本；其它方向 fail closed。
        return None, _safe_error("MIGRATION_DIRECTION_NOT_SUPPORTED")
    if not skill_registry.has(decision.proposed_skill, decision.proposed_version):
        return None, _safe_error("MIGRATION_TARGET_INVALID")
    if not _proposal_in_candidates(decision):
        return None, _safe_error("MIGRATION_TARGET_INVALID")
    if decision.locked:
        return None, _safe_error("MIGRATION_DIRECTION_NOT_SUPPORTED")

    wiki = db.get(WikiPage, plan.get("wiki_id")) if plan.get("wiki_id") else None
    if wiki is None:
        return None, _safe_error("MIGRATION_TARGET_INVALID")
    if bool(wiki.skill_locked):
        return None, _safe_error("MIGRATION_DIRECTION_NOT_SUPPORTED")
    if (wiki.content_skill or None) != decision.selected_skill or \
            (wiki.skill_version or None) != decision.selected_version:
        return None, _safe_error("MIGRATION_TARGET_INVALID")

    plan["branch"] = decision.proposed_skill
    plan["migration"] = True
    plan["proposed_skill"] = decision.proposed_skill
    plan["proposed_version"] = decision.proposed_version
    plan["decision"] = decision.to_dict()
    return plan, None


def _load_or_build_plan(db, run, ctx) -> tuple[dict | None, dict | None]:
    """优先用本 attempt state 缓存的分派计划（synthesize 已计算）。"""
    state = ctx.setdefault("state", {})
    plan = (state.get("v3") or {}).get("plan")
    if plan is not None:
        return plan, None
    plan, error = _build_v3_plan(db, run, ctx)
    if plan is None:
        return None, error
    state.setdefault("v3", {})["plan"] = plan
    return plan, None


# ---------------------------------------------------------------------------
# API Reference：内存编译 → 确定性 persist 计划（只读 db，产物入 state）
# ---------------------------------------------------------------------------


def _api_bindings_for_section(ir, spec) -> list[tuple[str, str, str]]:
    """按 Blueprint 规划把 IR field-level 绑定映射为该 Section 的 DB 绑定。

    返回稳定排序、去重后的 (field_path, usage_type, evidence_id)；
    usage_type=example 不落 DB（DB CHECK 只允许 support/conflict）。
    """
    role = spec.section_role
    source = ()
    if role == "endpoint":
        ep = next(
            (
                e for e in ir.endpoints
                if build_endpoint_section_key(e.method, e.path, e.version_scope)
                == spec.section_key
            ),
            None,
        )
        if ep is not None:
            source = ep.evidence_bindings
    elif role in _DOC_FIELD_PREFIX:
        prefix = _DOC_FIELD_PREFIX[role]
        if role == "overview":
            source = [b for b in ir.evidence_bindings if b.field_path == prefix]
        else:
            source = [
                b for b in ir.evidence_bindings
                if b.field_path.startswith(prefix)
            ]
    rows: set[tuple[str, str, str]] = set()
    for binding in source:
        if binding.usage_type not in _DB_USAGE_TYPES:
            continue
        for eid in binding.evidence_ids:
            rows.add((binding.field_path, binding.usage_type, eid))
    return sorted(rows)


def _derive_persist_sections(result: ApiCompileResult) -> list[dict]:
    """compile 产物 → 确定性 persist Section 计划（结构 JSON-safe，无 excerpt）。"""
    ir = result.ir
    blueprint = result.blueprint
    rendered_by_key = {s.section_key: s for s in result.sections}
    out: list[dict] = []
    for order, spec in enumerate(blueprint.sections):
        rendered = rendered_by_key.get(spec.section_key)
        content = rendered.content if rendered is not None else ""
        out.append({
            "order_index": order,
            "section_key": spec.section_key,
            "heading": spec.heading,
            "content": content,
            "content_hash": _hash_text(content),
            "validation_status": (
                "pass" if (rendered is None or rendered.validation_status == "pass")
                else "fail"
            ),
            "structure": spec.to_dict(),
            "bindings": _api_bindings_for_section(ir, spec),
        })
    return out


def _compile_api_from_pages(db, requested_page_ids: list[str]) -> dict:
    """Page 集 → ApiSourceDocument（db_adapter）→ 内存编译。

    返回 JSON-safe 摘要 + 编译期 Evidence 快照（供 publish 前重验）。返回
    dict（result 为 ApiCompileResult 内存对象；sections 为 persist 计划）。

    来源集合完整性：requested_page_ids 与 db_adapter 实际产出 ApiSourceDocument
    的来源页必须精确相等；任一请求页缺少可用 active Evidence（无 Evidence /
    stale-rejected-only / 全部非法）→ source_set_complete=False 且 publishable=False，
    绝不丢弃无 Evidence 的 Page 后继续发布。
    """
    requested = sorted({p for p in requested_page_ids if p})
    pages = []
    page_snapshot: dict[str, str] = {}
    for pid in requested:
        p = db.get(Page, pid)
        if p is None:
            continue
        pages.append(p)
        page_snapshot[pid] = p.content_hash or ""
    docs, issues = build_api_source_documents_with_diagnostics(db, pages)
    issue_codes = tuple(sorted({i.code for i in issues}))
    compiled_page_ids = sorted({d.source_page_id for d in docs})
    source_set_complete = compiled_page_ids == requested

    evidence_snapshot: dict[str, dict] = {}
    for doc in docs:
        for record in doc.evidence:
            eid = str(record.get("evidence_id") or "")
            content_hash = str(record.get("content_hash") or "")
            if eid and _is_hash64(content_hash):
                evidence_snapshot[eid] = {
                    "source_page_id": str(record.get("source_page_id") or ""),
                    "content_hash": content_hash,
                }

    result = api_compiler_mod.compile_api_reference(docs)
    sections = _derive_persist_sections(result)
    publishable = bool(
        result.is_publishable and docs and not issue_codes and source_set_complete
    )
    return {
        "result": result,
        "publishable": publishable,
        "issue_codes": list(issue_codes),
        "validation_status": result.validation_report.status,
        "validation_issue_codes": sorted({
            i.code for i in result.validation_report.issues
        }),
        "sections": sections,
        "requested_page_ids": requested,
        "page_ids": compiled_page_ids,
        "source_set_complete": source_set_complete,
        "page_snapshot": page_snapshot,
        "evidence_snapshot": evidence_snapshot,
        "model_usage": {
            k: result.model_usage[k] for k in ("llm_calls", "estimated_input_tokens",
                                               "estimated_output_tokens")
        },
    }


def _stage_synthesize_v3(db, run, stage_row, ctx) -> dict:
    """synthesize_by_skill：default 复用 v1；api_reference 内存编译（无产品写）。"""
    state = ctx.setdefault("state", {})
    plan, error = _build_v3_plan(db, run, ctx)
    if error is not None:
        return error
    state.setdefault("v3", {})["plan"] = plan

    if plan["branch"] == "api_reference":
        try:
            compiled = _compile_api_from_pages(db, plan.get("page_ids") or [])
        except Exception:  # noqa: BLE001
            logger.exception("api_reference compile failed run=%s", run.id)
            return _safe_error("VALIDATION_FAILED", retryable=True)
        state.setdefault("v3", {})["api"] = compiled
        return {
            "ok": True,
            "metrics": {
                "stage": "synthesize_by_skill",
                "branch": "api_reference",
                "mode": plan.get("mode"),
                "publishable": compiled["publishable"],
                "validation_status": compiled["validation_status"],
                "source_set_complete": compiled["source_set_complete"],
                "requested_page_count": len(compiled["requested_page_ids"]),
                "compiled_page_count": len(compiled["page_ids"]),
                "issue_codes": compiled["issue_codes"],
                "section_count": len(compiled["sections"]),
                "evidence_count": len(compiled["evidence_snapshot"]),
            },
        }

    # default：完全复用 v1 synthesize（同一函数对象，保证 v2/v3 等价）。
    return _stage_synthesize_default(db, run, stage_row, ctx)


def _stage_validate_v3(db, run, stage_row, ctx) -> dict:
    """validate_by_skill：default 复用 v1；api_reference 依据编译校验结果。

    api_reference 仅在 compiled.publishable=True 时通过；否则（校验未 pass /
    存在阻断 issue / 来源集合不完整）真实失败并让 publish stage 级联 skipped：
    - 迁移（plan.migration=True）→ MIGRATION_VALIDATION_FAILED；
    - 普通 api 更新/重建 → VALIDATION_FAILED。
    """
    state = ctx.setdefault("state", {})
    plan, error = _load_or_build_plan(db, run, ctx)
    if error is not None:
        return error
    if plan["branch"] == "api_reference":
        compiled = (state.get("v3") or {}).get("api") or {}
        if not compiled:
            return _safe_error("VALIDATION_FAILED")
        if compiled.get("publishable") is not True:
            if plan.get("migration"):
                return _safe_error("MIGRATION_VALIDATION_FAILED")
            return _safe_error("VALIDATION_FAILED")
        return {
            "ok": True,
            "metrics": {
                "stage": "validate_by_skill",
                "branch": "api_reference",
                "migration": bool(plan.get("migration")),
                "publishable": bool(compiled.get("publishable")),
                "validation_status": compiled.get("validation_status"),
                "source_set_complete": compiled.get("source_set_complete"),
                "issue_codes": compiled.get("issue_codes") or [],
            },
        }
    return _stage_validate_default(db, run, stage_row, ctx)


# ---------------------------------------------------------------------------
# API Reference：发布前 Evidence 重验（当前事务内只读检查）
# ---------------------------------------------------------------------------


def _reverify_api_publish(db, run, context, plan, compiled) -> str | None:
    """写 Revision 前在当前事务重新检查 Evidence / Page / Workspace / lease。

    任一不满足返回固定错误码；满足返回 None。
    """
    workspace_id = context.get("workspace_id")
    if not workspace_id:
        return "WORKSPACE_MISMATCH"
    ws = db.get(WikiWorkspace, workspace_id)
    if ws is None or (ws.status or "") != "active":
        return "WORKSPACE_MISMATCH"

    if not _lease_fence_ok(db, run):
        return "WORKER_LOST"

    page_snapshot = compiled.get("page_snapshot") or {}
    evidence_snapshot = compiled.get("evidence_snapshot") or {}
    for pid, expected_hash in page_snapshot.items():
        page = db.get(Page, pid)
        if page is None or not _is_hash64(page.content_hash):
            return "PAGE_STALE"
        if page.content_hash != expected_hash:
            return "PAGE_STALE"
        from app.core.wiki_workspace.routing import page_workspace_id

        if page_workspace_id(db, page) != workspace_id:
            return "WORKSPACE_MISMATCH"

        if pid in (plan.get("page_ids") or []):
            for eid, snap in evidence_snapshot.items():
                if snap.get("source_page_id") != pid:
                    continue
                ei = db.get(EvidenceItem, eid)
                if ei is None or (ei.status or "") != "active":
                    return "EVIDENCE_STALE"
                if str(ei.source_page_id or "") != pid:
                    return "EVIDENCE_STALE"
                if not _is_hash64(ei.content_hash) or ei.content_hash != snap.get("content_hash"):
                    return "EVIDENCE_STALE"
                if not _is_hash64(ei.source_doc_hash) or \
                        ei.source_doc_hash != page.content_hash:
                    return "EVIDENCE_STALE"
                try:
                    locator = json.loads(ei.locator_json or "{}")
                except (TypeError, ValueError):
                    return "EVIDENCE_STALE"
                if not isinstance(locator, dict):
                    return "EVIDENCE_STALE"
    return None


def _protected_section(sec) -> bool:
    """protected/manual Section 判定（与既有阻断判定同一三条件）。"""
    return sec.merge_policy == "protected" or bool(sec.locked) or sec.content_origin == "manual"


def _load_protected_snapshot(db, revision_id: str) -> list[dict]:
    """读取旧 Revision 中需保留的 protected/manual Section（含历史 Binding 快照）。

    按 (order_index, section_key or "", id) 稳定排序；输入查询顺序变化不影响结果。
    只返回仍存在的 Binding（Evidence 物理删除已被 FK CASCADE 清空，不伪造恢复）。
    """
    if not revision_id:
        return []
    rows = db.query(WikiSection).filter(WikiSection.revision_id == revision_id).all()
    ordered = sorted(
        rows,
        key=lambda s: (s.order_index or 0, s.section_key or "", s.id or ""),
    )
    out: list[dict] = []
    for sec in ordered:
        if not _protected_section(sec):
            continue
        binds = db.query(WikiSectionEvidenceBinding).filter(
            WikiSectionEvidenceBinding.section_id == sec.id).all()
        binds = sorted(binds, key=lambda b: (b.id or ""))
        out.append({
            "sec": sec,
            "key": sec.section_key or "",
            "bindings": binds,
        })
    return out


def _compose_merged_sections(auto_sections: list[dict],
                             protected_entries: list[dict]) -> list[dict]:
    """protected 与新生成 Section 的冲突合并（确定性）。

    - 自动 Section 按 Blueprint 位置排序；
    - key 相同 → protected 胜出（占据新 Blueprint 对应位置），自动不写入；
    - 无对应新 key 的 protected / section_key=NULL 的全部追加在自动 Section 后，
      保持旧相对顺序；
    - 最终 order_index 由调用方从 0 连续重排。
    """
    autos = sorted(
        list(auto_sections or []),
        key=lambda a: int(a.get("order_index") or 0),
    )
    by_key: dict[str, dict] = {}
    for entry in protected_entries:
        if entry.get("key"):
            by_key.setdefault(entry["key"], entry)
    used: set[str] = set()
    merged: list[dict] = []
    for item in autos:
        key = item.get("section_key") or ""
        entry = by_key.get(key)
        if key and entry is not None and key not in used:
            merged.append({"kind": "protected", "entry": entry})
            used.add(key)
        else:
            merged.append({"kind": "auto", "item": item})
    for entry in protected_entries:
        key = entry.get("key") or ""
        if key and key in used:
            continue
        merged.append({"kind": "protected", "entry": entry})
    return merged


# ---------------------------------------------------------------------------
# API Reference：原子持久化（Revision + Section + Binding + WikiPage + dirty）
# ---------------------------------------------------------------------------


def _write_auto_section(db, new_rev_id: str, item: dict, order_index: int,
                        skill_key: str, skill_version: str,
                        evidence_snapshot: dict) -> WikiSection:
    """写入一条新生成的 API Section 及其字段级 Binding。"""
    structure_text = json.dumps(
        item.get("structure") or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    section = WikiSection(
        id=str(uuid.uuid4()),
        revision_id=new_rev_id,
        section_type="facts",
        heading=(item.get("heading") or "")[:255] or None,
        content=item.get("content") or "",
        order_index=order_index,
        locked=False,
        version_label=None,
        version_sort_key=None,
        is_common=False,
        content_origin="auto",
        merge_policy="auto",
        version_confidence=None,
        version_status=None,
        diff_notice=None,
        section_key=item.get("section_key"),
        skill_key=skill_key,
        skill_version=skill_version,
        content_hash=item.get("content_hash"),
        validation_status="pass",
        structure_json=structure_text,
    )
    db.add(section)
    db.flush()
    for field_path, usage_type, evidence_id in item.get("bindings") or ():
        snap = evidence_snapshot.get(evidence_id) or {}
        ev_hash = snap.get("content_hash") or ""
        if not _is_hash64(ev_hash):
            raise ValueError("binding evidence snapshot missing")
        db.add(WikiSectionEvidenceBinding(
            id=str(uuid.uuid4()),
            section_id=section.id,
            evidence_id=evidence_id,
            field_path=str(field_path)[:255],
            usage_type=usage_type,
            evidence_content_hash=ev_hash,
        ))
    return section


def _write_protected_section(db, new_rev_id: str, entry: dict,
                             order_index: int) -> WikiSection:
    """原样复制 protected/manual Section（新 ID）并复制历史 Binding 快照。"""
    sec = entry["sec"]
    content = sec.content or ""
    content_hash = sec.content_hash
    if not _is_hash64(content_hash):
        content_hash = _hash_text(content)
    new_sec = WikiSection(
        id=str(uuid.uuid4()),
        revision_id=new_rev_id,
        section_type=sec.section_type or "facts",
        heading=sec.heading,
        content=content,
        order_index=order_index,
        locked=bool(sec.locked),
        version_label=sec.version_label,
        version_sort_key=sec.version_sort_key,
        is_common=sec.is_common,
        content_origin=sec.content_origin or "auto",
        merge_policy=sec.merge_policy or "auto",
        version_confidence=sec.version_confidence,
        version_status=sec.version_status,
        diff_notice=sec.diff_notice,
        section_key=sec.section_key,
        skill_key=sec.skill_key,
        skill_version=sec.skill_version,
        content_hash=content_hash,
        validation_status=sec.validation_status,
        structure_json=sec.structure_json,
    )
    db.add(new_sec)
    db.flush()
    for b in entry["bindings"]:
        # Evidence 仍存在才可能有该行（FK CASCADE 已清掉物理删除的引用）。
        db.add(WikiSectionEvidenceBinding(
            id=str(uuid.uuid4()),
            section_id=new_sec.id,
            evidence_id=b.evidence_id,
            field_path=b.field_path,
            usage_type=b.usage_type,
            evidence_content_hash=b.evidence_content_hash,
        ))
    return new_sec


def _persist_api_revision(db, context, plan, compiled, wiki: WikiPage,
                          skill_key: str, skill_version: str,
                          revision_id: str) -> WikiRevision:
    """在 stage 事务内追加 API Revision：自动 Sections + protected 复制合并 + Bindings。

    - 新 Wiki 无旧 Revision → 只有自动 Sections；
    - 更新/重建/迁移 → 复用同一 protected 合并函数（避免两套规则）；
    - order_index 从 0 连续重排；非空 section_key 仍唯一（冲突时 protected 胜出）。
    """
    auto_sections = compiled.get("sections") or []
    evidence_snapshot = compiled.get("evidence_snapshot") or {}
    old_revision_id = wiki.current_revision_id
    protected = (_load_protected_snapshot(db, old_revision_id)
                 if old_revision_id else [])
    merged = _compose_merged_sections(auto_sections, protected)
    if not merged:
        raise ValueError("no sections to persist")

    contents = []
    for row in merged:
        if row["kind"] == "auto":
            contents.append(row["item"].get("content") or "")
        else:
            contents.append(row["entry"]["sec"].content or "")
    new_rev = WikiRevision(
        id=revision_id,
        wiki_page_id=wiki.id,
        parent_revision_id=old_revision_id,
        title=wiki.title or "",
        summary="",
        source_hash=_hash_text("\x1f".join(contents)),
        status="published",
        edit_type="auto",
        updated_by=None,
    )
    db.add(new_rev)
    db.flush()

    for idx, row in enumerate(merged):
        if row["kind"] == "auto":
            _write_auto_section(db, new_rev.id, row["item"], idx,
                                skill_key, skill_version, evidence_snapshot)
        else:
            _write_protected_section(db, new_rev.id, row["entry"], idx)
    return new_rev


def _apply_skill_fields(db, wiki: WikiPage, decision: SkillDecision) -> None:
    """同一事务内更新 WikiPage Skill 字段（不改 skill_locked）。"""
    wiki.content_skill = decision.selected_skill
    wiki.skill_version = decision.selected_version
    wiki.skill_selected_by = decision.selected_by
    wiki.skill_confidence = decision.confidence
    wiki.skill_decision_json = json.dumps(
        decision.to_dict(), ensure_ascii=False, sort_keys=True
    )


def _api_publish_manifest_skill(decision: SkillDecision) -> dict:
    """Manifest 内 JSON-safe skill 摘要（不含正文/Prompt/excerpt/路径）。"""
    return {
        "skill_key": decision.selected_skill,
        "skill_version": decision.selected_version,
        "selected_by": decision.selected_by,
        "confidence": decision.confidence,
        "status": decision.status,
    }


def _migration_applied_decision(proposal: SkillDecision, wiki: WikiPage) -> SkillDecision:
    """迁移已应用的 SkillDecision（写 wiki.skill_decision_json）。

    selected=proposed；previous=原当前值；selected_by=migration；status=selected；
    reason_code=MIGRATION_APPLIED。不含 proposed 字段（status != migration_proposed）。
    """
    return SkillDecision(
        target_key=proposal.target_key or (wiki.id if wiki else ""),
        wiki_page_id=proposal.wiki_page_id or (wiki.id if wiki else None),
        selected_skill=proposal.proposed_skill,
        selected_version=proposal.proposed_version,
        selected_by="migration",
        confidence=proposal.confidence,
        status="selected",
        reason_code="MIGRATION_APPLIED",
        matched_signals=proposal.matched_signals,
        candidates=proposal.candidates,
        previous_skill=proposal.previous_skill or proposal.selected_skill,
        previous_version=proposal.previous_version or proposal.selected_version,
        locked=False,
    )


def _migration_manifest_skill(proposal: SkillDecision, wiki: WikiPage) -> dict:
    """迁移成功 Manifest 的 skill 摘要（previous/skill/migration_applied/reason）。"""
    return {
        "previous_skill": proposal.previous_skill or proposal.selected_skill,
        "previous_version": proposal.previous_version or proposal.selected_version,
        "skill_key": proposal.proposed_skill,
        "skill_version": proposal.proposed_version,
        "migration_applied": True,
        "reason": proposal.reason_code or "MIGRATION_PROPOSED",
    }


def _reverify_migration_state(db, wiki: WikiPage, proposal: SkillDecision) -> str | None:
    """迁移发布前复验：DB 当前 Skill 与 proposal 的 selected 仍一致、未锁定。"""
    if bool(wiki.skill_locked):
        return "MIGRATION_DIRECTION_NOT_SUPPORTED"
    if (wiki.content_skill or None) != proposal.selected_skill or \
            (wiki.skill_version or None) != proposal.selected_version:
        return "MIGRATION_TARGET_INVALID"
    return None


def _stage_publish_v3(db, run, stage_row, ctx) -> dict:
    """publish_by_skill：唯一产品写。

    default 分支 → 完全复用 v2 publish wrapper（保持 v2/v3 等价）；
    api_reference 分支 → 发布前重验 + 原子持久化（SAVEPOINT/lease/统一提交
    边界内，不自行 commit/rollback）。
    """
    state = ctx.setdefault("state", {})
    plan, error = _load_or_build_plan(db, run, ctx)
    if error is not None:
        return error

    if plan["branch"] == "default":
        return _V2_STAGE_PUBLISH_SKILLED(db, run, stage_row, ctx)

    # ---- api_reference 分支（含 migration 原子切换）----
    context = state.get("context") or {}
    compiled = (state.get("v3") or {}).get("api") or {}
    decision = _decode_decision(plan.get("decision"))
    if decision is None or not compiled:
        return _safe_error("SKILL_DECISION_MISSING")

    migration = bool(plan.get("migration"))
    if migration:
        # 迁移：发布用 proposed（api_reference）Skill；决策为原 proposal。
        skill_key = plan.get("proposed_skill") or ""
        skill_version = plan.get("proposed_version") or ""
        if skill_key != "api_reference" or not skill_version:
            return _safe_error("MIGRATION_TARGET_INVALID")
    else:
        skill_key = decision.selected_skill or ""
        skill_version = decision.selected_version or ""
        if skill_key != "api_reference" or not skill_version:
            return _safe_error("SKILL_NOT_SUPPORTED")

    # 编译结果不可发布（诊断/校验失败/无事实内容）→ 零发布，保持 dirty。
    # （正常流程 validate 已拦截；此处为发布侧防御。）
    if not compiled.get("publishable"):
        if migration:
            return _safe_error("MIGRATION_VALIDATION_FAILED")
        return _safe_error("VALIDATION_FAILED")

    trigger = plan.get("trigger") or run.trigger_type

    # 幂等守卫：本 run 更早 attempt 已成功发布（output_revision_id 已回填）→
    # 绝不重复 append Revision（graph retry 防重）。不产新 Manifest。
    outcome = {
        "applied": False, "created": 0, "updated": 0, "archived": 0,
        "kept_dirty": 0, "revisions": [], "wiki_page_ids": [],
        "archived_wiki_ids": [], "dirty_wiki_ids": [],
        "output_revision_id": None, "note": "",
    }
    state["publish"] = outcome
    if run.output_revision_id:
        outcome["note"] = "already_published_skip_republish"
        return {"ok": True, "metrics": {"stage": "publish_by_skill",
                                        "note": outcome["note"]}}

    # 发布前 Evidence 重验（当前事务内重新读库；编译对象不可信）。
    stale_code = _reverify_api_publish(db, run, context, plan, compiled)
    if stale_code is not None:
        # 零发布：不创建 Revision/不清理 dirty/不写 Manifest；固定安全错误码。
        return _safe_error(stale_code, retryable=(stale_code in ("PAGE_STALE", "EVIDENCE_STALE", "WORKER_LOST")))

    wiki_id = plan.get("wiki_id")
    wiki = db.get(WikiPage, wiki_id) if wiki_id else None
    if migration:
        # 迁移必须基于既有 wiki 且状态一致（零写校验）。
        if wiki is None:
            return _safe_error("MIGRATION_TARGET_INVALID")
        state_code = _reverify_migration_state(db, wiki, decision)
        if state_code is not None:
            return _safe_error(state_code)
    elif plan["mode"] == "rebuild" or wiki is not None:
        if wiki is None:
            return _safe_error("PAGE_STALE")
    if plan["mode"] == "rebuild" and not wiki.dirty:
        outcome["note"] = "wiki_not_dirty_no_write"
        return {"ok": True, "metrics": {"stage": "publish_by_skill",
                                        "note": outcome["note"]}}

    try:
        page_ids = sorted({p for p in (plan.get("page_ids") or []) if p})
        # 新建目标（page_changed create）：先建 WikiPage（与 default _create_wiki 一致：
        # workspace_id 写入端强制非空），再写 Revision。
        if wiki is None:
            if not context.get("workspace_id"):
                return _safe_error("WORKSPACE_MISMATCH")
            wiki = WikiPage(
                id=str(uuid.uuid4()),
                title=(plan.get("title") or context.get("title") or "无标题")[:255],
                summary="",
                acl_scope=context.get("scope_acl_json"),
                status="draft",
                source_page_ids=json.dumps(page_ids, ensure_ascii=False),
                dirty=False,
                locked=False,
                workspace_id=context.get("workspace_id"),
            )
            db.add(wiki)
            db.flush()

        revision_id = str(uuid.uuid4())
        new_rev = _persist_api_revision(
            db, context, plan, compiled, wiki, skill_key, skill_version, revision_id
        )
        # WikiPage：current_revision_id / status / dirty / source 成员 / Skill 字段。
        wiki.current_revision_id = new_rev.id
        wiki.status = "published"
        wiki.dirty = False
        wiki.source_page_ids = json.dumps(page_ids, ensure_ascii=False)
        wiki.acl_scope = context.get("scope_acl_json") or wiki.acl_scope
        if migration:
            applied = _migration_applied_decision(decision, wiki)
            _apply_skill_fields(db, wiki, applied)
        else:
            _apply_skill_fields(db, wiki, decision)

        # 按现有规则清理触发 Page 的 dirty（镜像 default create_update 成功语义）。
        if trigger == "page_changed" and context.get("page_id"):
            page = db.get(Page, context["page_id"])
            if page is not None and page.content_hash and \
                    page.content_hash == (compiled.get("page_snapshot") or {}).get(context["page_id"]):
                page.wiki_dirty = False
                page.wiki_compiled_content_hash = context.get("input_hash") or ""
                page.wiki_last_error = None

        outcome.update({
            "applied": True,
            "created": 1 if plan["mode"] == "create" else 0,
            "updated": 1 if plan["mode"] in ("update", "rebuild") else 0,
            "revisions": [new_rev.id],
            "wiki_page_ids": [wiki.id],
            "output_revision_id": new_rev.id,
            "note": "published",
        })
    except Exception:  # noqa: BLE001
        logger.exception("api_reference publish failed run=%s", run.id)
        return _safe_error("API_PERSIST_FAILED")

    result = {
        "ok": True,
        "output_revision_id": new_rev.id,
        "metrics": {
            "stage": "publish_by_skill",
            "branch": "api_reference",
            "mode": plan.get("mode"),
            "migration": migration,
            "note": outcome["note"],
            "wiki_page_ids": [wiki.id],
            "revision_count": len(outcome["revisions"]),
        },
    }
    enriched = _finish_publish(ctx, result)
    # Manifest 附加 skill 信息（JSON-safe；graph/finalize 只读既有字段）。
    if migration:
        enriched["payload"]["skill"] = _migration_manifest_skill(decision, wiki)
    else:
        enriched["payload"]["skill"] = _api_publish_manifest_skill(decision)
    return enriched


# ---------------------------------------------------------------------------
# v3 Pipeline 定义与注册（注册不改变 active version，active 仍由 v2 持有）
# ---------------------------------------------------------------------------


def _stage_defs_v3():
    executors = {
        "resolve_context": _stage_resolve_context,
        "topic_route": _stage_topic_route,
        "skill_route": _stage_skill_route,
        "synthesize_by_skill": _stage_synthesize_v3,
        "validate_by_skill": _stage_validate_v3,
        "publish_by_skill": _stage_publish_v3,
        "finalize_compile_outcome": _stage_finalize_compile_outcome,
        "schedule_graph": _stage_schedule_graph,
    }
    flags = {
        "resolve_context": {"retryable": True, "allows_publish": False},
        "topic_route": {"retryable": True, "allows_publish": False},
        "skill_route": {"retryable": True, "allows_publish": False},
        "synthesize_by_skill": {"retryable": True, "allows_publish": False},
        "validate_by_skill": {"retryable": False, "allows_publish": False},
        "publish_by_skill": {"retryable": False, "allows_publish": True},
        "finalize_compile_outcome": {"retryable": False, "allows_publish": False},
        "schedule_graph": {"retryable": True, "allows_publish": False},
    }
    stages = []
    for key in _V3_STAGE_KEYS:
        f = flags[key]
        stages.append(pipe_registry.StageDef(
            key=key,
            version="1",
            retryable=f["retryable"],
            cachable=False,
            execute=executors[key],
            failure_transition=pipe_registry.FailureTransition.FAIL,
            description=_V3_DESCRIPTIONS[key],
            allows_publish=f["allows_publish"],
        ))
    return stages


def _candidate_pipeline_v3() -> pipe_registry.PipelineDef:
    return pipe_registry.PipelineDef(
        key=PIPELINE_KEY,
        version=PIPELINE_VERSION,
        stages=_stage_defs_v3(),
        allow_null_workspace=False,
    )


def _stage_def_equal(a, b) -> bool:
    if a.key != b.key or a.version != b.version:
        return False
    if a.retryable != b.retryable or a.cachable != b.cachable:
        return False
    if a.allows_publish != b.allows_publish:
        return False
    if a.cache_type != b.cache_type or a.cache_schema_version != b.cache_schema_version:
        return False
    if a.failure_transition != b.failure_transition:
        return False
    if tuple(a.cache_key_stage_keys) != tuple(b.cache_key_stage_keys):
        return False
    if a.execute is not b.execute:
        return False
    return True


def _pipeline_matches(existing, candidate) -> bool:
    if existing.key != candidate.key or existing.version != candidate.version:
        return False
    if existing.allow_null_workspace != candidate.allow_null_workspace:
        return False
    if len(existing.stages) != len(candidate.stages):
        return False
    for a, b in zip(existing.stages, candidate.stages):
        if not _stage_def_equal(a, b):
            return False
    return True


def register_default_pipeline_v3() -> None:
    """注册 wiki.default v3（幂等；冲突 raise 中止）。**不修改 active**（仍为 v2）。"""
    existing = pipe_registry.get_pipeline(PIPELINE_KEY, PIPELINE_VERSION)
    candidate = _candidate_pipeline_v3()
    if existing is not None:
        if _pipeline_matches(existing, candidate):
            logger.info("wiki.default pipeline v3 already registered (idempotent)")
            return
        raise pipe_registry.PipelineError(
            f"pipeline_definition_conflict={PIPELINE_KEY}:{PIPELINE_VERSION} "
            "definition differs"
        )
    pipe_registry.register_pipeline(candidate)


def register_default_pipeline_v3_for_test() -> None:
    """测试辅助：注册 v3 并用 replace_for_test 语义覆盖（配合多版本共存测试）。"""
    pipe_registry.replace_for_test(_candidate_pipeline_v3())


def unregister_default_pipeline_v3() -> None:
    """测试清理：仅移除 wiki.default v3（不动 v1/v2）。"""
    versions = pipe_registry.REGISTRY.get(PIPELINE_KEY)
    if versions:
        versions.pop(PIPELINE_VERSION, None)
