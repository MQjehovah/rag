"""Phase 6：默认 Wiki 编译流水线 v2（wiki.default version 2，skill-aware）。

v2 Stage 顺序：
    resolve_context → topic_route → skill_route → synthesize_default →
    validate_default → publish_default → finalize_compile_outcome → schedule_graph

要求（封板）：
- v1（wiki_default.py）与 v2 同时注册；已排队的 v1 Run 精确执行 v1；新 Run 默认 v2；
- active version 显式（registry.set_active_version），不依赖注册顺序；
- v1 的 stage key/version/flags/execute 身份保持不变；
- skill_route 只读（不写 WikiPage/Revision、不改 TopicOperation/Workspace/ACL、
  不 commit），产出 skill_decision Artifact（skill-decision/v1），决策写入 state；
- page_deleted：不调 Skill LLM，产安全 not_applicable decision；
- publish_default 复用 v1 publish（不复制），由增强 wrapper 在同一 publish 事务内
  对成功发布的 Wiki 原子写 Skill 字段（失败不覆盖当前 Skill/Revision）。

本模块只组合 v1 Stage + 新 skill_route + v2 Pipeline 定义/注册，不复制 v1 实现。
"""
from __future__ import annotations

import json
import logging

from app.core.wiki_skills import registry as skill_registry
from app.core.wiki_skills import service as skill_service
from app.core.wiki_skills.schemas import (
    SKILL_DECISION_SCHEMA,
    SkillContext,
    SkillDecision,
)
from app.core.wiki_pipeline import registry as pipe_registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    PIPELINE_KEY,
    _STAGE_FLAGS,
    _stage_finalize_compile_outcome,
    _stage_publish_default as _V1_STAGE_PUBLISH_DEFAULT,
    _stage_resolve_context,
    _stage_schedule_graph,
    _stage_synthesize_default,
    _stage_topic_route,
    _stage_validate_default,
    register_default_pipeline,
)
from app.models.database import (
    Page,
    WikiPage,
)

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "2"

# skill_route 产物（Phase 6 Artifact 契约）。
ARTIFACT_TYPE_SKILL_DECISION = "skill_decision"
ARTIFACT_SCHEMA_SKILL_DECISION = SKILL_DECISION_SCHEMA

# 每 target 摘要上限（SkillContext 构造用；router LLM 摘要上限独立于 config）。
_SKILL_SUMMARY_CHARS = 1500

_V2_STAGE_KEYS = (
    "resolve_context",
    "topic_route",
    "skill_route",
    "synthesize_default",
    "validate_default",
    "publish_default",
    "finalize_compile_outcome",
    "schedule_graph",
)

_DESCRIPTIONS = {
    "resolve_context": "解析 Page/Workspace/Scope/input_hash（无写）",
    "topic_route": "主题路由：LLM worthy/create/update（不写 Wiki/Revision）",
    "skill_route": "Skill 路由：确定性信号 + Registry（多候选可 LLM），产出 SkillDecision（无写）",
    "synthesize_default": "对目标合成正文（普通/版本化，LLM 经 runner）",
    "validate_default": "发布前校验（input_hash/正文/workspace）",
    "publish_default": "唯一产品写：Revision + source sync + Page/Wiki 状态 + Skill 字段 + Manifest",
    "finalize_compile_outcome": "按 Manifest 判定 Run 级编译结果（失败语义真实化）",
    "schedule_graph": "读 Manifest 对 wiki/page 目标同步真实图谱重建（幂等）",
}

# SKILL_PERSIST_FAILED 服务端固定文案（safe，不落原始异常）。
_SKILL_PERSIST_FAILED_MESSAGE = "知识编译失败：Skill 字段持久化失败（不可自动重试）"


# ---------------------------------------------------------------------------
# skill_route stage：每个 Topic target 一条 SkillDecision
# ---------------------------------------------------------------------------


def _page_summary(page: Page, limit: int = _SKILL_SUMMARY_CHARS) -> str:
    """单页最小摘要（供信号/路由；不落完整正文）。"""
    if page is None:
        return ""
    text = page.content or ""
    if len(text) > limit:
        text = text[:limit]
    return text


def _existing_wiki_by_norm(db, workspace_id, norm_title: str):
    """只读：workspace 内标题规范相等的既有 Wiki（读 current skill 用）。"""
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        normalize_wiki_title,
    )

    if not workspace_id or not norm_title:
        return None
    rows = db.query(WikiPage).filter(WikiPage.workspace_id == workspace_id).all()
    for w in rows:
        if normalize_wiki_title(w.title or "") == norm_title:
            return w
    return None


def _target_keys_from_batch_pages(state: dict) -> list[dict]:
    """batch topic 决策 → 去重 Topic target（scope×norm title 唯一，read-only）。"""
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        normalize_wiki_title,
    )

    pages = (state.get("batch") or {}).get("pages") or []
    targets: dict[str, dict] = {}
    for dec in pages:
        pid = dec.get("page_id")
        for op in dec.get("ops") or []:
            title = (op.get("title") or "").strip()
            if not title or op.get("action") not in ("create", "update"):
                continue
            norm = normalize_wiki_title(title)
            key = f"{dec.get('scope_acl_json') or ''}\x1f{norm}"
            t = targets.setdefault(key, {
                "key": key,
                "scope_acl_json": dec.get("scope_acl_json") or "",
                "norm_title": norm,
                "title": title,
                "page_ids": [],
                "wiki_page_id": None,
                "existing": None,
            })
            if pid and pid not in t["page_ids"]:
                t["page_ids"].append(pid)
    return list(targets.values())


def _build_skill_contexts(
    db, run, context: dict, topic: dict, state: dict
) -> list[SkillContext]:
    """按 trigger 构造每个 Topic target 的 SkillContext（只读，不写）。"""
    trigger = context.get("trigger_type") or run.trigger_type
    workspace_id = context.get("workspace_id")
    status = topic.get("status") or "not_applicable"

    if status != "create_update" and trigger not in ("manual_rebuild", "batch_rebuild"):
        return []

    if trigger == "manual_rebuild":
        wiki_id = context.get("wiki_page_id")
        wiki = db.get(WikiPage, wiki_id) if wiki_id else None
        if wiki is None:
            return []
        page_ids = list(context.get("source_page_ids") or ())
        summaries = []
        for pid in page_ids:
            p = db.get(Page, pid)
            if p is not None:
                summaries.append({
                    "source_page_id": pid,
                    "summary": _page_summary(p),
                })
        return [SkillContext(
            workspace_id=workspace_id,
            wiki_page_id=wiki.id,
            target_key=wiki.id,
            title=wiki.title or "",
            content_kind="generic_text",
            source_page_ids=tuple(page_ids),
            source_summaries=tuple(summaries),
            current_skill=wiki.content_skill,
            current_version=wiki.skill_version,
            skill_locked=bool(wiki.skill_locked),
        )]

    if trigger == "page_changed":
        page_id = context.get("page_id")
        page = db.get(Page, page_id) if page_id else None
        ctxs = []
        seen: set[str] = set()
        for op in topic.get("ops") or []:
            title = (op.get("title") or "").strip()
            if not title or op.get("action") not in ("create", "update"):
                continue
            norm = _normalize_title(title)
            if norm in seen:
                continue
            seen.add(norm)
            existing = _existing_wiki_by_norm(db, workspace_id, norm)
            summaries = []
            if page is not None:
                summaries.append({
                    "source_page_id": page_id,
                    "summary": _page_summary(page),
                })
            ctxs.append(SkillContext(
                workspace_id=workspace_id,
                wiki_page_id=(existing.id if existing else None),
                target_key=norm,
                title=title,
                content_kind="generic_text",
                source_page_ids=tuple([page_id]) if page_id else (),
                source_summaries=tuple(summaries),
                current_skill=(existing.content_skill if existing else None),
                current_version=(existing.skill_version if existing else None),
                skill_locked=bool(existing.skill_locked) if existing else False,
            ))
        return ctxs

    if trigger == "batch_rebuild":
        ctxs = []
        for t in _target_keys_from_batch_pages(state):
            norm = t["norm_title"]
            existing = _existing_wiki_for_scope(
                db, workspace_id, t.get("scope_acl_json") or "", norm)
            summaries = []
            for pid in t["page_ids"]:
                p = db.get(Page, pid)
                if p is not None:
                    summaries.append({
                        "source_page_id": pid,
                        "summary": _page_summary(p),
                    })
            ctxs.append(SkillContext(
                workspace_id=workspace_id,
                wiki_page_id=(existing.id if existing else None),
                target_key=t["key"],
                title=t["title"],
                content_kind="generic_text",
                source_page_ids=tuple(t["page_ids"]),
                source_summaries=tuple(summaries),
                current_skill=(existing.content_skill if existing else None),
                current_version=(existing.skill_version if existing else None),
                skill_locked=bool(existing.skill_locked) if existing else False,
            ))
        return ctxs

    return []


def _existing_wiki_for_scope(db, workspace_id, scope_acl_json: str,
                             norm_title: str):
    """batch 既有 Wiki 精确查找：workspace_id + acl_scope + normalized title。"""
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        normalize_wiki_title,
    )

    if not workspace_id or not norm_title:
        return None
    rows = (
        db.query(WikiPage)
        .filter(
            WikiPage.workspace_id == workspace_id,
            WikiPage.acl_scope == (scope_acl_json or ""),
        )
        .all()
    )
    for w in rows:
        if normalize_wiki_title(w.title or "") == norm_title:
            return w
    return None


def _normalize_title(title: str) -> str:
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        normalize_wiki_title,
    )

    return normalize_wiki_title(title)


def _stage_skill_route(db, run, stage_row, ctx) -> dict:
    """Skill 路由：只读构造 SkillContext → service.decide → SkillDecision Artifact。

    - 不修改 TopicOperation / Workspace / ACL；不写 WikiPage / Revision；不 commit；
    - page_deleted / 上下文不可用：不调 Skill LLM，产安全 not_applicable decision；
    - 生产只有 default：直接 default（无 LLM 调用）；
    - decisions 按 target_key 排序后写入 Artifact 与 state（供 publish 消费）。
    """
    state = ctx.setdefault("state", {})
    context = state.get("context") or {}
    topic = state.get("topic") or {}
    trigger = context.get("trigger_type") or run.trigger_type

    applicable = bool(context.get("applicable"))
    if not applicable or trigger == "page_deleted":
        # 删除/不可用上下文：not_applicable 安全 decision（无 selected skill）。
        decision = SkillDecision(
            target_key=(context.get("page_id") or context.get("wiki_page_id") or ""),
            wiki_page_id=context.get("wiki_page_id"),
            selected_skill=None,
            selected_version=None,
            selected_by="auto",
            confidence=0.0,
            status="not_applicable",
            reason_code="SKILL_NOT_APPLICABLE",
            previous_skill=context.get("current_skill"),
            previous_version=context.get("current_version"),
            locked=bool(context.get("skill_locked")),
        )
        decisions = [decision]
    else:
        skill_contexts = _build_skill_contexts(db, run, context, topic, state)
        decisions = []
        for sctx in skill_contexts:
            try:
                d = skill_service.decide(sctx)
            except Exception as exc:  # noqa: BLE001
                # Skill 路由绝不使编译失败：default fallback / not_applicable。
                logger.warning(
                    "skill route fallback target=%s: %s", sctx.target_key, exc
                )
                d = _route_error_decision(sctx)
            decisions.append(d)
        if not decisions:
            # 无目标：空 decisions（publish 无发布目标时无需 Skill 字段）。
            decisions = []

    # 稳定排序 + JSON-safe payload（不存正文/Prompt/ACL）。
    decisions.sort(key=lambda d: d.target_key)
    state["skill"] = {
        "decisions": [d.to_dict() for d in decisions],
    }
    payload = {
        "schema_version": SKILL_DECISION_SCHEMA,
        "decisions": [d.to_dict() for d in decisions],
    }
    metrics = {
        "stage": "skill_route",
        "trigger": trigger,
        "applicable": applicable,
        "decision_count": len(decisions),
        "default_count": sum(
            1 for d in decisions if d.selected_skill == "default"
        ),
        "not_applicable_count": sum(
            1 for d in decisions if d.status == "not_applicable"
        ),
    }
    return {
        "ok": True,
        "artifact_type": ARTIFACT_TYPE_SKILL_DECISION,
        "schema_version": ARTIFACT_SCHEMA_SKILL_DECISION,
        "object_type": "skill_decision",
        "payload": payload,
        "metrics": metrics,
    }


def _route_error_decision(sctx: SkillContext) -> SkillDecision:
    """Skill 路由异常兜底：default fallback / not_applicable（不使编译失败）。"""
    default_info = skill_registry.get_active("default")
    if default_info is None:
        return SkillDecision(
            target_key=sctx.target_key,
            wiki_page_id=sctx.wiki_page_id,
            selected_skill=None, selected_version=None,
            selected_by="auto", confidence=0.0,
            status="not_applicable", reason_code="SKILL_ROUTE_ERROR",
            previous_skill=sctx.current_skill,
            previous_version=sctx.current_version,
            locked=sctx.skill_locked,
        )
    return SkillDecision(
        target_key=sctx.target_key,
        wiki_page_id=sctx.wiki_page_id,
        selected_skill=default_info.key,
        selected_version=default_info.version,
        selected_by="default_fallback", confidence=1.0,
        status="fallback", reason_code="SKILL_ROUTE_ERROR",
        previous_skill=sctx.current_skill,
        previous_version=sctx.current_version,
        locked=sctx.skill_locked,
    )


# ---------------------------------------------------------------------------
# publish_default 增强 wrapper：复用 v1 publish + 原子写 Skill 字段
# ---------------------------------------------------------------------------


def _match_decision(state: dict, wiki: WikiPage) -> SkillDecision | None:
    """按 wiki id / 规范标题匹配其决策（batch 每 target 独立决策）。"""
    decisions = (state.get("skill") or {}).get("decisions") or []
    if not decisions:
        return None
    norm = _normalize_title(wiki.title or "") if wiki else ""
    for raw in decisions:
        if not isinstance(raw, dict):
            continue
        if raw.get("target_key") == wiki.id:
            try:
                return SkillDecision.from_dict(raw)
            except ValueError:
                return None
    for raw in decisions:
        if not isinstance(raw, dict):
            continue
        if _normalize_title(str(raw.get("target_key") or "")) == norm:
            try:
                return SkillDecision.from_dict(raw)
            except ValueError:
                return None
    return None


def _default_decision(wiki: WikiPage) -> SkillDecision:
    """找不到匹配决策的兜底：default fallback（Phase 6 只上线 default）。"""
    default_info = skill_registry.get_active("default")
    if default_info is None:
        raise RuntimeError("default skill not registered")
    return SkillDecision(
        target_key=(wiki.id if wiki else ""),
        wiki_page_id=(wiki.id if wiki else None),
        selected_skill=default_info.key,
        selected_version=default_info.version,
        selected_by="default_fallback",
        confidence=1.0,
        status="fallback",
        reason_code="SKILL_MATCH_FALLBACK",
        previous_skill=(wiki.content_skill if wiki else None),
        previous_version=(wiki.skill_version if wiki else None),
        locked=bool(wiki.skill_locked) if wiki else False,
    )


def _apply_skill_to_wiki(db, wiki: WikiPage, decision: SkillDecision) -> None:
    """同一 publish 事务内更新 WikiPage Skill 字段（不改 skill_locked：锁定由人工控制）。"""
    wiki.content_skill = decision.selected_skill
    wiki.skill_version = decision.selected_version
    wiki.skill_selected_by = decision.selected_by
    wiki.skill_confidence = decision.confidence
    wiki.skill_decision_json = json.dumps(
        decision.to_dict(), ensure_ascii=False, sort_keys=True
    )


def _v2_unsupported_decision(state: dict) -> bool:
    """v2 回滚安全：本 pipeline 只能发布 default；api_reference / 迁移 → fail closed。

    忽略 not_applicable（无 selected skill）；任何 selected 非 default 或
    migration_proposed（选中 default 但 propose 其它）→ True。
    """
    from app.core.wiki_skills.schemas import SkillDecision

    for raw in (state.get("skill") or {}).get("decisions") or []:
        try:
            d = SkillDecision.from_dict(raw)
        except ValueError:
            continue
        if d.selected_skill is None:
            continue
        if d.selected_skill != "default" or d.status == "migration_proposed":
            return True
    return False


def _stage_publish_skilled(db, run, stage_row, ctx) -> dict:
    """v2 publish：先完整复用 v1 publish_default，再对成功发布的 Wiki 原子写 Skill。

    Phase 7D 回滚安全：v2 遇到 api_reference/迁移决策 → fail closed
    （SKILL_NOT_SUPPORTED_BY_PIPELINE_VERSION，零 Revision、零 Skill 写）。

    成功发布（Revision 已写入且状态 published）→ 同一事务内为该 Wiki 写对应
    decision 的 Skill 字段。validation/stale/publish 失败分支不发布 Revision →
    不触碰原 Skill/Revision。任何 Skill 字段写入异常 → stage 失败（savepoint 回滚
    包括本次 publish 全部写入，保持原子；不覆盖原值）。
    """
    state = ctx.get("state") or {}
    if _v2_unsupported_decision(state):
        return {
            "ok": False,
            "error_code": "SKILL_NOT_SUPPORTED_BY_PIPELINE_VERSION",
            "error_message": pipe_registry.stage_error_message(
                "SKILL_NOT_SUPPORTED_BY_PIPELINE_VERSION"),
            "retryable": False,
        }
    result = _V1_STAGE_PUBLISH_DEFAULT(db, run, stage_row, ctx)
    if not result.get("ok"):
        return result

    publish = state.get("publish") or {}
    wiki_ids = list(publish.get("wiki_page_ids") or [])
    note = publish.get("note") or ""
    if note in ("published", "rebuild_published", "partial_synthesis") and wiki_ids:
        try:
            for wid in wiki_ids:
                wiki = db.get(WikiPage, wid)
                if wiki is None:
                    continue
                decision = _match_decision(state, wiki)
                if decision is None or decision.selected_skill is None:
                    decision = _default_decision(wiki)
                _apply_skill_to_wiki(db, wiki, decision)
        except Exception:  # noqa: BLE001
            logger.exception("skill persist failed run=%s", run.id)
            return {
                "ok": False,
                "error_code": "SKILL_PERSIST_FAILED",
                "retryable": False,
                "error_message": _SKILL_PERSIST_FAILED_MESSAGE,
            }
    return result


# ---------------------------------------------------------------------------
# v2 Pipeline 定义与注册
# ---------------------------------------------------------------------------


def _stage_defs_v2():
    executors = {
        "resolve_context": _stage_resolve_context,
        "topic_route": _stage_topic_route,
        "skill_route": _stage_skill_route,
        "synthesize_default": _stage_synthesize_default,
        "validate_default": _stage_validate_default,
        "publish_default": _stage_publish_skilled,
        "finalize_compile_outcome": _stage_finalize_compile_outcome,
        "schedule_graph": _stage_schedule_graph,
    }
    flags = dict(_STAGE_FLAGS)
    flags["skill_route"] = {"retryable": True, "allows_publish": False}
    stages = []
    for key in _V2_STAGE_KEYS:
        f = flags[key]
        stages.append(pipe_registry.StageDef(
            key=key,
            version="1",
            retryable=f["retryable"],
            cachable=False,
            execute=executors[key],
            failure_transition=pipe_registry.FailureTransition.FAIL,
            description=_DESCRIPTIONS[key],
            allows_publish=f["allows_publish"],
        ))
    return stages


def _candidate_pipeline_v2() -> pipe_registry.PipelineDef:
    return pipe_registry.PipelineDef(
        key=PIPELINE_KEY,
        version=PIPELINE_VERSION,
        stages=_stage_defs_v2(),
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


def register_default_pipeline_v2() -> None:
    """注册 wiki.default v2 并显式设 active=v2（幂等；冲突 raise 中止启动）。"""
    existing = pipe_registry.get_pipeline(PIPELINE_KEY, PIPELINE_VERSION)
    candidate = _candidate_pipeline_v2()
    if existing is not None:
        if _pipeline_matches(existing, candidate):
            logger.info("wiki.default pipeline v2 already registered (idempotent)")
            pipe_registry.set_active_version(PIPELINE_KEY, PIPELINE_VERSION)
            return
        raise pipe_registry.PipelineError(
            f"pipeline_definition_conflict={PIPELINE_KEY}:{PIPELINE_VERSION} "
            "definition differs"
        )
    pipe_registry.register_pipeline(candidate)
    pipe_registry.set_active_version(PIPELINE_KEY, PIPELINE_VERSION)


def register_skilled_default_pipeline() -> None:
    """注册 v1（幂等）+ v2，显式 active=v2。重复调用幂等。"""
    register_default_pipeline()
    register_default_pipeline_v2()


def unregister_skilled_default_pipeline() -> None:
    """测试清理：移除 wiki.default（v1+v2）。"""
    pipe_registry.unregister_pipeline(PIPELINE_KEY)


def register_v2_for_test() -> None:
    """测试辅助：仅注册 v2（配合 v1 后注册验证 active 不依赖顺序）。"""
    register_default_pipeline_v2()
