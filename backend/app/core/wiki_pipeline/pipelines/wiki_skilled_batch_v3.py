"""Phase 7C.3-C：wiki.default v3 混合 Skill batch_rebuild（default / api_reference / 迁移）。

目标：在一个 batch_rebuild CompileRun 内让不同 Wiki 目标各自按已持久化的
SkillDecision 编译/发布，不整批套用一个 Skill；支持 default→api_reference 迁移。

职责（本文件）：
- mixed batch DTO / target planning（workspace+scope+normalized_title 稳定身份）；
- 逐 target 合成（default 复用 wiki_default 低层 helper；api_reference 复用
  api_reference compiler + 来源集合完整性；迁移先 shadow compile）；
- 逐 target 校验（ready/failed/not_worthy，TargetResult JSON-safe）；
- 累积发布 Manifest（published/reused/failed 汇总 + target_results + graph_targets）；
- partial retry 幂等恢复（读历史 wiki_publish_manifest，已成功且 input_hash 未变
  则 reused 不重发；已成功但输入已变 → BATCH_STALE fail closed）；
- membership reconcile 统一一次、Page dirty 清理、global fatal 零发布。

约束：
- 不调用 _legacy_*；不自行 commit/rollback/取 Connection（在 publish stage
  SAVEPOINT/fence 边界内）；
- Manifest/TargetResult/structure_json 不存正文/Prompt/excerpt/ACL/Token/路径/异常。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from app.core.knowledge_compiler_v3.wiki_page_builder import (
    _set_source_pages,
)
from app.core.wiki_pipeline import registry as pipe_registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    ARTIFACT_SCHEMA_WIKI_PUBLISH,
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    _finish_publish,
    _publish_wiki_from_entry,
    _synthesize_entry,
    _plan_batch_targets,
    _safe_reconcile_membership,
)
from app.models.database import (
    KnowledgeCompileArtifact,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
)

logger = logging.getLogger(__name__)

# batch run-level 受控失败码（registry 提供固定文案）。
_CODE_GLOBAL_FATAL = {
    "workspace_mismatch": "WORKSPACE_MISMATCH",
    "no_scope": "WORKSPACE_MISMATCH",
    "no_workspace": "WORKSPACE_MISMATCH",
    "batch_input_artifact_missing": "VALIDATION_FAILED",
    "batch_no_pages": "VALIDATION_FAILED",
    "page_not_found": "PAGE_STALE",
    "service_unavailable": "SERVICE_UNAVAILABLE",
    "invalid_response": "INVALID_RESPONSE",
}
_DEFAULT_FAIL_CODES = {
    "service_unavailable": "SERVICE_UNAVAILABLE",
    "invalid_response": "INVALID_RESPONSE",
}
_API_FAIL = "VALIDATION_FAILED"
_MIG_FAIL = "MIGRATION_VALIDATION_FAILED"


def _safe_error(code: str, *, retryable: bool = False) -> dict:
    return {
        "ok": False,
        "error_code": code,
        "error_message": pipe_registry.stage_error_message(code),
        "retryable": retryable,
    }


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Target planning（只读）
# ---------------------------------------------------------------------------


def _batch_key(scope_acl: str, norm: str) -> str:
    return f"{scope_acl or ''}\x1f{norm}"


def _normalize_title(title: str) -> str:
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        normalize_wiki_title,
    )

    return normalize_wiki_title(title or "")


def _existing_wiki_by_norm(db, workspace_id, norm_title: str):
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        _existing_wiki_by_norm as _v3_existing,
    )

    return _v3_existing(db, workspace_id, norm_title)


def _decode_decision(raw) -> Any | None:
    from app.core.wiki_skills.schemas import SkillDecision

    if not isinstance(raw, dict):
        return None
    try:
        return SkillDecision.from_dict(raw)
    except ValueError:
        return None


def _read_skill_decisions(db, run_id: str) -> list:
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        _read_skill_decision_payload,
    )

    payload = _read_skill_decision_payload(db, run_id)
    decisions = [_decode_decision(d) for d in (payload or {}).get("decisions") or []]
    return [d for d in decisions if d is not None]


def _match_composite(decisions, key: str):
    """按完整 composite key（scope\x1fnorm）精确匹配（新 Artifact）。"""
    for d in decisions:
        if (d.target_key or "") == key:
            return d
    return None


def _legacy_norm_match(decisions, norm_counts: dict, norm: str):
    """旧 Artifact（target_key=纯 norm）兼容匹配：仅当该 norm 全局唯一且无歧义。"""
    if norm_counts.get(norm) != 1:
        return None  # 同标题跨 scope → fail closed，不猜测
    hits = [d for d in decisions
            if _normalize_title(d.target_key or "") == norm]
    return hits[0] if len(hits) == 1 else None


def _resolve_target_decision(dec, target: dict, db, run) -> dict | None:
    """按 target 匹配持久化决策并求分支（返回补全后的 target 或 None=failed）。"""
    from app.core.wiki_skills import registry as skill_registry
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        _proposal_in_candidates,
    )

    if dec is None:
        return None
    status = dec.status or ""
    norm = target["norm_title"]
    wiki = None
    if target.get("existing_wiki_id"):
        wiki = db.get(WikiPage, target["existing_wiki_id"])
    migration = status == "migration_proposed"

    skill = dec.selected_skill
    if not migration:
        if skill not in ("default", "api_reference"):
            return None
        if not skill_registry.has(skill, dec.selected_version or ""):
            return None
        target.update({
            "branch": skill, "migration": False,
            "proposed_skill": None, "proposed_version": None,
            "decision": dec.to_dict(),
        })
        return target

    # migration_proposed：default → api_reference 显式契约（复用 Phase 7C.3-B）。
    if not dec.proposed_skill or not dec.proposed_version:
        return None
    if dec.selected_skill != "default" or dec.proposed_skill != "api_reference":
        return None
    if dec.locked or (wiki is not None and bool(wiki.skill_locked)):
        return None
    if not skill_registry.has(dec.proposed_skill, dec.proposed_version):
        return None
    if not _proposal_in_candidates(dec):
        return None
    if wiki is None:
        return None
    if (wiki.content_skill or None) != dec.selected_skill or \
            (wiki.skill_version or None) != dec.selected_version:
        return None
    target.update({
        "branch": "api_reference", "migration": True,
        "proposed_skill": dec.proposed_skill, "proposed_version": dec.proposed_version,
        "decision": dec.to_dict(),
    })
    return target


def _plan_batch_targets_v3(db, context: dict, batch_pages: list[dict],
                           decisions: list,
                           previous: dict[str, dict] | None = None) -> tuple[list[dict], str | None]:
    """从 topic 决策去重规划 batch targets（复用 wiki_default 低层 planning）。

    返回 (targets 排序, fatal_code)。target 含稳定 target_key（scope\x1fnorm）、
    action/existing_wiki_id/title/category/source_page_ids/page_ids/决策/分支/input_hash。
    """
    create_pages = [d for d in batch_pages if d.get("status") == "create_update"]
    planned = _plan_batch_targets(db, context, create_pages)
    if not planned:
        return [], None

    norm_counts: dict[str, int] = {}
    for t in planned:
        norm_counts[t["norm_title"]] = norm_counts.get(t["norm_title"], 0) + 1

    targets: list[dict] = []
    for t in planned:
        norm = t["norm_title"]
        key = _batch_key(t.get("scope_acl_json") or "", norm)
        entry = {
            "key": key,
            "norm_title": norm,
            "scope_acl_json": t.get("scope_acl_json"),
            "workspace_id": context.get("workspace_id"),
            "action": t["action"],
            "title": t["title"],
            "category": t["category"],
            "existing_wiki_id": t.get("existing_id"),
            "source_page_ids": sorted({p for p in t.get("source_page_ids") or []}),
            "page_ids": sorted({p for p in t.get("page_ids") or []}),
        }
        dec = _match_composite(decisions, key)
        if dec is None:
            # 兼容：纯 norm 旧 Artifact 且无跨 scope 歧义时才允许回退。
            dec = _legacy_norm_match(decisions, norm_counts, norm)
        entry = _resolve_target_decision(dec, entry, db, None)
        if entry is None:
            entry = {
                "key": key, "norm_title": norm,
                "scope_acl_json": t.get("scope_acl_json"),
                "workspace_id": context.get("workspace_id"),
                "action": t["action"], "title": t["title"], "category": t["category"],
                "existing_wiki_id": t.get("existing_id"),
                "source_page_ids": sorted({p for p in t.get("source_page_ids") or []}),
                "page_ids": sorted({p for p in t.get("page_ids") or []}),
                "branch": None, "migration": False,
                "proposed_skill": None, "proposed_version": None,
                "decision": None, "resolve_error": "MIGRATION_TARGET_INVALID"
                if dec is not None else "SKILL_DECISION_MISSING",
            }
            targets.append(entry)
            continue
        entry["input_hash"] = _target_input_hash(db, entry, previous)
        targets.append(entry)
    targets.sort(key=lambda t: t["key"])
    return targets, None


def _logical_action_existing(target: dict, previous: dict[str, dict]) -> tuple[str, str | None]:
    """本 Run 先 create 后 update 的再规划归一为逻辑 create（retry 稳定）。"""
    key = target.get("key") or ""
    rec = previous.get(key)
    if rec and rec.get("action") == "create" and target.get("existing_wiki_id") and \
            rec.get("wiki_page_id") == target.get("existing_wiki_id"):
        return "create", None
    return (target.get("action") or ""), target.get("existing_wiki_id")


def _target_input_hash(db, target: dict, previous: dict[str, dict] | None = None) -> str:
    """target input_hash = 全部真实输入确定性指纹。

    覆盖：target_key/branch/selected skill+version/proposed/migration/existing_wiki_id/
    排序后全部 source_page_ids + 每个 source Page 当前 full hash。
    历史来源变化（含非本批贡献的历史来源页）→ hash 变化（BATCH_STALE）。
    """
    from app.core.wiki_pipeline.pipelines.wiki_default import _page_full_hash

    previous = previous or {}
    _action, logical_existing = _logical_action_existing(target, previous)
    decision = _decode_decision(target.get("decision")) if target.get("decision") else None
    parts = [
        "key=" + (target.get("key") or ""),
        "branch=" + str(target.get("branch") or ""),
        "migration=" + ("1" if target.get("migration") else "0"),
        "existing=" + str(logical_existing or ""),
        "selected=" + ((decision.selected_skill or "") if decision else "")
        + ":" + ((decision.selected_version or "") if decision else ""),
        "proposed=" + str(target.get("proposed_skill") or "")
        + ":" + str(target.get("proposed_version") or ""),
    ]
    for pid in sorted(set(target.get("source_page_ids") or [])):
        parts.append(f"{pid}:{_page_full_hash(db, pid)}")
    return _hash_text("|".join(parts))


def _plan_entry_canonical(target: dict, previous: dict[str, dict] | None = None) -> dict:
    previous = previous or {}
    action, existing = _logical_action_existing(target, previous)
    decision = _decode_decision(target.get("decision")) if target.get("decision") else None
    return {
        "target_key": target.get("key") or "",
        "action": action,
        "existing_wiki_id": existing or "",
        "source_page_ids": sorted(set(target.get("source_page_ids") or [])),
        "branch": target.get("branch") or "",
        "selected_skill": (decision.selected_skill if decision else None),
        "selected_version": (decision.selected_version if decision else None),
        "proposed_skill": (decision.proposed_skill if decision else None),
        "proposed_version": (decision.proposed_version if decision else None),
        "migration": bool(target.get("migration")),
    }


def _compute_plan_hash(targets: list[dict],
                       previous: dict[str, dict] | None = None) -> str:
    """plan_hash：targets 排序后的规范 JSON SHA-256（不依赖 DB/注册顺序）。"""
    canonical = [
        _plan_entry_canonical(t, previous)
        for t in sorted(targets, key=lambda x: x.get("key") or "")
    ]
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return _hash_text(raw)


# ---------------------------------------------------------------------------
# TargetResult / 合成
# ---------------------------------------------------------------------------


def _target_result(key: str, target: dict, status: str, *, code: str = "",
                   source_page_ids=None, input_hash="", migration_applied=False,
                   safe=True) -> dict:
    return {
        "target_key": key,
        "branch": target.get("branch") if target else "",
        "status": status,
        "safe_error_code": code,
        "source_page_ids": sorted(source_page_ids or target.get("source_page_ids") or []),
        "input_hash": input_hash or target.get("input_hash") or "",
        "migration_applied": migration_applied,
    }


def _page_full_hash_ok(db, pid: str, expected: str) -> bool:
    from app.core.wiki_pipeline.pipelines.wiki_default import _page_full_hash

    return _page_full_hash(db, pid) == expected


def _synthesize_default_target(db, context, ctx, target: dict) -> dict:
    """复用 wiki_default 低层 _synthesize_entry（不复制 LLM 算法）。

    update 目标以 adapter 提供 existing_id，使 content_sig 在 synthesize 期生成；
    synthesize 后断言 update entry.content_sig 非空（否则 fail closed）。
    """
    pages = []
    for pid in target.get("source_page_ids") or []:
        p = db.get(Page, pid)
        if p is not None:
            pages.append(p)
    if not pages:
        return {"ready": False, "code": "VALIDATION_FAILED"}
    from types import SimpleNamespace

    existing_id = target.get("existing_wiki_id")
    v1_target = {
        "action": target.get("action") or "",
        "norm_title": target.get("norm_title") or "",
        "title": target.get("title") or "无标题",
        "category": target.get("category"),
        "existing_id": existing_id,
        "source_page_ids": sorted(set(target.get("source_page_ids") or [])),
    }
    standin = SimpleNamespace(
        id=existing_id,
        title=target.get("title") or "无标题",
        category=target.get("category"),
        current_revision_id=(
            db.get(WikiPage, existing_id).current_revision_id
            if existing_id else None
        ),
        acl_scope=target.get("scope_acl_json"),
        workspace_id=target.get("workspace_id"),
    )
    entry = _synthesize_entry(db, v1_target, pages, standin, context, ctx)
    if entry is None:
        return {"ready": False, "code": "VALIDATION_FAILED", "entry": entry}
    if not entry.get("published_ready"):
        reason = entry.get("fail_reason") or "invalid_response"
        return {"ready": False, "code": _DEFAULT_FAIL_CODES.get(reason, "INVALID_RESPONSE"),
                "entry": entry}
    if (target.get("action") == "update" and existing_id) and not entry.get("content_sig"):
        # update 目标 content guard 缺失 → 不得发布（fail closed）。
        return {"ready": False, "code": "VALIDATION_FAILED", "entry": entry}
    return {"ready": True, "entry": entry}


def _synthesize_api_target(db, target: dict, migration: bool) -> dict:
    """api_reference/migration 目标：来源集合完整性 + Evidence coverage（shadow）。"""
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        _compile_api_from_pages,
    )

    compiled = _compile_api_from_pages(db, target.get("source_page_ids") or [])
    if not compiled.get("publishable"):
        code = _MIG_FAIL if migration else _API_FAIL
        return {"ready": False, "code": code, "compiled": compiled}
    return {"ready": True, "compiled": compiled}


def synthesize_batch_v3(db, run, ctx) -> dict:
    """batch synthesize：规划 target + 逐 target 合成/编译，写 state（无产品写）。

    返回 ok=True 的 stage 结果；target 级失败只记录 TargetResult，不使 stage 失败。
    """
    state = ctx.setdefault("state", {})
    context = state.get("context") or {}
    batch = state.setdefault("batch", {})
    batch_pages = batch.get("pages") or []

    fatal = None
    for d in batch_pages:
        if d.get("status") in ("service_unavailable", "invalid_response",
                               "no_scope", "no_workspace"):
            fatal = d.get("status")
            break
    if not context.get("applicable"):
        fatal = context.get("reason") or "batch_input_artifact_missing"
    if fatal is not None:
        batch["fatal"] = fatal
        state.setdefault("v3", {})["batch"] = {
            "fatal": _CODE_GLOBAL_FATAL.get(fatal, "VALIDATION_FAILED"),
            "targets": [], "results": {}, "payloads": {}, "meta": {},
        }
        return {"ok": True, "metrics": {"stage": "synthesize_by_skill",
                                        "batch": True, "fatal": True}}

    decisions = _read_skill_decisions(db, run.id)
    previous = _previous_target_records(db, run.id)
    targets, _fatal = _plan_batch_targets_v3(db, context, batch_pages, decisions,
                                             previous=previous)
    results: dict[str, dict] = {}
    payloads: dict[str, dict] = {}

    for target in targets:
        key = target["key"]
        if target.get("branch") is None:
            results[key] = _target_result(
                key, target, "failed",
                code=target.get("resolve_error") or "SKILL_DECISION_MISSING")
            continue
        migration = bool(target.get("migration"))
        branch = target["branch"]
        if branch == "default":
            out = _synthesize_default_target(db, context, ctx, target)
            payloads[key] = out
            if out.get("ready"):
                results[key] = _target_result(key, target, "ready")
            else:
                results[key] = _target_result(key, target, "failed",
                                              code=out.get("code", "VALIDATION_FAILED"))
        elif branch == "api_reference":
            out = _synthesize_api_target(db, target, migration)
            payloads[key] = out
            if out.get("ready"):
                results[key] = _target_result(key, target, "ready",
                                              migration_applied=migration)
            else:
                results[key] = _target_result(key, target, "failed",
                                              code=out.get("code", _API_FAIL),
                                              migration_applied=migration)
        else:
            results[key] = _target_result(key, target, "failed",
                                          code="SKILL_NOT_SUPPORTED")

    meta = {"target_count": len(targets),
            "plan_hash": _compute_plan_hash(targets, previous)}
    # plan_hash 一致性门禁（retry 时）：历史 Manifest 缺失/形状非法/与本次计划不同 →
    # BATCH_PLAN_CHANGED（不复用/不发布/不 reconcile）。
    manifests = _read_all_manifests(db, run.id)
    if manifests:
        prev_manifest = manifests[-1]
        prev_hash = prev_manifest.get("plan_hash")
        prev_trs = prev_manifest.get("target_results")
        if not isinstance(prev_hash, str) or not isinstance(prev_trs, list) or \
                not all(isinstance(t, dict) and t.get("target_key")
                        for t in prev_trs) or prev_hash != meta["plan_hash"]:
            return _safe_error("BATCH_PLAN_CHANGED")

    state.setdefault("v3", {})["batch"] = {
        "fatal": None, "targets": targets, "results": results,
        "payloads": payloads, "meta": meta, "plan_hash": meta["plan_hash"],
    }
    ready = sum(1 for r in results.values() if r["status"] == "ready")
    return {
        "ok": True,
        "metrics": {
            "stage": "synthesize_by_skill", "batch": True,
            "target_count": len(targets), "ready_count": ready,
            "failed_count": len(targets) - ready,
        },
    }


def validate_batch_v3(db, run, ctx) -> dict:
    """batch validate：stage 恒 ok（逐 target 判定已在 synthesize 完成）。"""
    state = ctx.setdefault("state", {})
    bstate = (state.get("v3") or {}).get("batch") or {}
    return {
        "ok": True,
        "metrics": {
            "stage": "validate_by_skill", "batch": True,
            "fatal": bstate.get("fatal"),
            "ready_count": sum(1 for r in (bstate.get("results") or {}).values()
                               if r.get("status") == "ready"),
        },
    }


# ---------------------------------------------------------------------------
# 历史 manifest（partial retry 恢复）
# ---------------------------------------------------------------------------


def _read_all_manifests(db, run_id: str) -> list[dict]:
    rows = (
        db.query(KnowledgeCompileArtifact)
        .filter(
            KnowledgeCompileArtifact.run_id == run_id,
            KnowledgeCompileArtifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
        )
        .order_by(KnowledgeCompileArtifact.created_at.asc())
        .all()
    )
    out = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _previous_target_records(db, run_id: str) -> dict[str, dict]:
    """跨 attempt 合并历史 target_results（target_key → 最新记录）。"""
    merged: dict[str, dict] = {}
    for manifest in _read_all_manifests(db, run_id):
        for tr in manifest.get("target_results") or []:
            if not isinstance(tr, dict):
                continue
            key = tr.get("target_key")
            if not key:
                continue
            merged[key] = tr
    return merged


# ---------------------------------------------------------------------------
# 发布原语（default / api_reference / migration）与统一 membership reconcile
# ---------------------------------------------------------------------------


def _create_wiki_row(db, target: dict, workspace_id: str, *, dirty: bool = False) -> WikiPage:
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        _is_hash64,
    )

    wiki = WikiPage(
        id=str(uuid.uuid4()),
        title=(target.get("title") or "无标题")[:255],
        summary="",
        acl_scope=target.get("scope_acl_json"),
        category=(target.get("category") or "未分类")[:128],
        status="draft",
        source_page_ids=json.dumps(
            sorted(target.get("source_page_ids") or []), ensure_ascii=False),
        dirty=dirty,
        locked=False,
        workspace_id=workspace_id,
    )
    db.add(wiki)
    db.flush()
    return wiki


def _publish_default_target(db, ctx, target: dict, payload: dict) -> str | None:
    """发布单个 default 目标（复用 wiki_default._publish_wiki_from_entry）。"""
    state = ctx["state"]
    context = state.get("context") or {}
    entry = payload.get("entry") or {}
    workspace_id = target.get("workspace_id") or context.get("workspace_id")
    if not workspace_id:
        return None
    wiki = None
    if target.get("existing_wiki_id"):
        wiki = db.get(WikiPage, target["existing_wiki_id"])
    created = False
    if wiki is None:
        wiki = _create_wiki_row(db, target, workspace_id)
        created = True
    else:
        _set_source_pages(wiki, sorted(target.get("source_page_ids") or []))
    pages = []
    for pid in target.get("source_page_ids") or []:
        p = db.get(Page, pid)
        if p is not None:
            pages.append(p)
    rev_id = _publish_wiki_from_entry(db, wiki, pages, entry)
    if rev_id is None:
        # 新建目标发布失败：删除刚创建但未发布的 Wiki（不留孤儿 draft）。
        if created:
            db.delete(wiki)
            db.flush()
            if target.get("existing_wiki_id") == wiki.id:
                target["existing_wiki_id"] = None
        return None
    wiki.current_revision_id = rev_id
    wiki.status = "published"
    wiki.dirty = False
    wiki.source_page_ids = json.dumps(
        sorted(target.get("source_page_ids") or []), ensure_ascii=False)
    # 镜像 v2 default wrapper：对成功发布的 wiki 原子写 Skill 字段。
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        _apply_skill_fields,
        _decode_decision,
    )

    decision = _decode_decision(target.get("decision"))
    if decision is not None:
        _apply_skill_fields(db, wiki, decision)
    if created:
        target["existing_wiki_id"] = wiki.id
    return rev_id


def _publish_api_target(db, ctx, run, target: dict, payload: dict, migration: bool) -> tuple[str | None, dict]:
    """发布单个 api_reference / migration 目标（复用 v3 原子写与 protected 合并）。"""
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        _apply_skill_fields,
        _migration_applied_decision,
        _persist_api_revision,
        _reverify_api_publish,
        _reverify_migration_state,
        _decode_decision,
    )
    from app.core.wiki_skills.schemas import SkillDecision

    state = ctx["state"]
    context = state.get("context") or {}
    workspace_id = target.get("workspace_id") or context.get("workspace_id")
    decision = _decode_decision(target.get("decision"))
    compiled = payload.get("compiled") or {}
    if not decision or not compiled or not workspace_id:
        return None, {}
    if migration:
        skill_key = target.get("proposed_skill") or ""
        skill_version = target.get("proposed_version") or ""
        if skill_key != "api_reference" or not skill_version:
            return None, {}
    else:
        skill_key = decision.selected_skill or ""
        skill_version = decision.selected_version or ""
        if skill_key != "api_reference" or not skill_version:
            return None, {}

    # 发布前重验覆盖目标全部来源 Page（与 input_hash 覆盖集合一致）。
    plan = {"page_ids": sorted(set(target.get("source_page_ids") or []))}
    rev = _reverify_api_publish(db, run, {"workspace_id": workspace_id}, plan, compiled)
    if rev is not None:
        return None, {"code": rev}

    wiki = None
    if target.get("existing_wiki_id"):
        wiki = db.get(WikiPage, target["existing_wiki_id"])
    created = wiki is None
    if wiki is None:
        wiki = _create_wiki_row(db, target, workspace_id)
        target["existing_wiki_id"] = wiki.id

    if migration:
        mcode = _reverify_migration_state(db, wiki, decision)
        if mcode is not None:
            if created:
                db.delete(wiki)
                db.flush()
                target["existing_wiki_id"] = None
            return None, {"code": mcode}

    revision_id = str(uuid.uuid4())
    new_rev = _persist_api_revision(
        db, context, plan, compiled, wiki, skill_key, skill_version, revision_id)
    wiki.current_revision_id = new_rev.id
    wiki.status = "published"
    wiki.dirty = False
    wiki.source_page_ids = json.dumps(
        sorted(target.get("source_page_ids") or []), ensure_ascii=False)
    wiki.acl_scope = target.get("scope_acl_json") or wiki.acl_scope
    applied = None
    if migration:
        applied = _migration_applied_decision(decision, wiki)
        _apply_skill_fields(db, wiki, applied)
    else:
        _apply_skill_fields(db, wiki, decision)
    return new_rev.id, {"migration_applied": bool(migration)}


def publish_batch_v3(db, run, ctx) -> dict:
    """batch publish：唯一产品写（SAVEPOINT/fence 内）。

    全局 fatal → 整批零发布；partial → 成功 target 发布 + 失败 target 零 Revision；
    retry：已成功 target（input_hash 未变）reused 不重发；已成功但输入已变 → BATCH_STALE。
    """
    state = ctx.setdefault("state", {})
    context = state.get("context") or {}
    bstate = (state.get("v3") or {}).get("batch") or {}
    fatal = bstate.get("fatal")
    targets = bstate.get("targets") or []
    results = dict(bstate.get("results") or {})
    payloads = bstate.get("payloads") or {}
    batch = state.get("batch") or {}
    batch_pages = batch.get("pages") or []

    outcome = {
        "applied": False, "created": 0, "updated": 0, "archived": 0,
        "kept_dirty": 0, "revisions": [], "wiki_page_ids": [],
        "archived_wiki_ids": [], "dirty_wiki_ids": [],
        "output_revision_id": None, "note": "",
    }
    state["publish"] = outcome

    if fatal is not None:
        # 全局 fatal：整批零发布，Run failed（SAVEPOINT 回滚本次任何写）。
        return _safe_error(fatal,
                           retryable=(fatal in ("SERVICE_UNAVAILABLE", "INVALID_RESPONSE")))

    if not targets:
        outcome["note"] = "noop_batch"
        return _finish_publish(ctx, {"ok": True, "metrics": {"stage": "publish_by_skill",
                                                             "note": outcome["note"]}})

    previous = _previous_target_records(db, run.id)
    # 0) plan_hash 一致性：历史 Manifest 缺失/形状非法 → fail closed；与本次 plan 不同
    #    → BATCH_PLAN_CHANGED（不复用/不发布/不 reconcile）。
    manifests = _read_all_manifests(db, run.id)
    if manifests:
        prev_manifest = manifests[-1]
        prev_hash = prev_manifest.get("plan_hash")
        prev_trs = prev_manifest.get("target_results")
        if not isinstance(prev_hash, str) or not isinstance(prev_trs, list) or \
                not all(isinstance(t, dict) and t.get("target_key")
                        for t in prev_trs):
            return _safe_error("BATCH_PLAN_CHANGED")
        plan_hash = bstate.get("plan_hash")
        if not plan_hash or prev_hash != plan_hash:
            return _safe_error("BATCH_PLAN_CHANGED")

    # 1) stale 检查：已成功 target 输入变化 → 整批 fail closed（不得在旧 Run 覆盖）。
    for target in targets:
        key = target["key"]
        rec = previous.get(key)
        if not rec or rec.get("outcome") not in ("published", "reused"):
            continue
        if target.get("input_hash") != rec.get("input_hash"):
            return _safe_error("BATCH_STALE")

    published: dict[str, dict] = {}   # key -> {wiki_page_id, revision_id, reused, ...}

    try:
        # 2) 逐 target 发布（先 reused 判定，再写）。
        for target in targets:
            key = target["key"]
            rec = previous.get(key)
            if rec and rec.get("outcome") in ("published", "reused") and \
                    rec.get("input_hash") == target.get("input_hash"):
                # reused：不重发，恢复既有 wiki/revision。
                published[key] = {
                    "wiki_page_id": rec.get("wiki_page_id"),
                    "revision_id": rec.get("revision_id"),
                    "input_hash": target.get("input_hash") or rec.get("input_hash"),
                    "reused": True,
                    "migration_applied": bool(rec.get("migration_applied")),
                    "source_page_ids": sorted(set(target.get("source_page_ids") or [])),
                }
                results[key] = _target_result(
                    key, target, "ready", input_hash=target.get("input_hash"),
                    migration_applied=bool(rec.get("migration_applied")))
                continue

            status = results.get(key, {}).get("status")
            if status != "ready":
                continue  # 合成/校验失败的目标已在 results 记为 failed

            branch = target.get("branch")
            migration = bool(target.get("migration"))
            try:
                if branch == "default":
                    rev_id = _publish_default_target(db, ctx, target, payloads.get(key) or {})
                    if rev_id is None:
                        results[key] = _target_result(key, target, "failed",
                                                      code="VALIDATION_FAILED")
                        continue
                    published[key] = {
                        "wiki_page_id": target.get("existing_wiki_id"),
                        "revision_id": rev_id,
                        "input_hash": target.get("input_hash") or "",
                        "reused": False,
                        "migration_applied": False,
                        "source_page_ids": sorted(set(target.get("source_page_ids") or [])),
                    }
                elif branch == "api_reference":
                    rev_id, extra = _publish_api_target(db, ctx, run, target,
                                                        payloads.get(key) or {}, migration)
                    if rev_id is None:
                        code = extra.get("code") or _API_FAIL
                        if migration and code not in ("PAGE_STALE", "EVIDENCE_STALE",
                                                      "WORKER_LOST", "WORKSPACE_MISMATCH"):
                            code = _MIG_FAIL
                        results[key] = _target_result(key, target, "failed", code=code,
                                                      migration_applied=migration)
                        continue
                    published[key] = {
                        "wiki_page_id": target.get("existing_wiki_id"),
                        "revision_id": rev_id,
                        "input_hash": target.get("input_hash") or "",
                        "reused": False,
                        "migration_applied": bool(extra.get("migration_applied")),
                        "source_page_ids": sorted(set(target.get("source_page_ids") or [])),
                    }
                else:
                    results[key] = _target_result(key, target, "failed",
                                                  code="SKILL_NOT_SUPPORTED")
            except Exception:  # noqa: BLE001  —— 未预期异常整体回滚
                logger.exception("batch publish target failed run=%s key=%s", run.id, key)
                raise

        # 3) 统一 membership reconcile（全部 target 结果后一次计算）。
        _reconcile_batch_membership(db, state, batch_pages, targets, published, results)

        # 4) 累积 target_results（published/reused/failed；排序去重）。
        final_results = _assemble_target_results(targets, results, published)
        partial = any(tr["outcome"] == "failed" for tr in final_results)

        if partial:
            outcome.update({"applied": True, "kept_dirty": 1, "note": "batch_partial"})
            manifest = _build_batch_manifest(ctx, outcome, published, final_results,
                                             partial=True,
                                             plan_hash=bstate.get("plan_hash"))
        else:
            outcome["note"] = "published"
            outcome["applied"] = True
            outcome.update({
                "revisions": [p["revision_id"] for p in published.values()
                              if p.get("revision_id")],
                "wiki_page_ids": [p["wiki_page_id"] for p in published.values()
                                  if p.get("wiki_page_id")],
            })
            manifest = _build_batch_manifest(ctx, outcome, published, final_results,
                                             partial=False,
                                             plan_hash=bstate.get("plan_hash"))

        result = {"ok": True, "metrics": {
            "stage": "publish_by_skill", "batch": True,
            "published": sum(1 for tr in final_results if tr["outcome"] == "published"),
            "reused": sum(1 for tr in final_results if tr["outcome"] == "reused"),
            "failed": sum(1 for tr in final_results if tr["outcome"] == "failed"),
        }}
        enriched = _finish_publish(ctx, result)
        enriched["payload"] = manifest
        state["manifest"] = manifest
        return enriched
    except Exception:  # noqa: BLE001
        logger.exception("batch publish failed run=%s", run.id)
        return _safe_error("BATCH_PUBLISH_FAILED")


def _assemble_target_results(targets, results, published) -> list[dict]:
    """把 results（ready/failed/reused 语义）+ published 汇总为最终 TargetResult。"""
    action_by_key = {t["key"]: t.get("action") or "" for t in targets}
    keys = sorted({t["key"] for t in targets} | set(results.keys()))
    out: list[dict] = []
    for key in keys:
        tr = dict(results.get(key) or {})
        action = action_by_key.get(key) or ""
        if key in published:
            p = published[key]
            out.append({
                "target_key": key,
                "branch": tr.get("branch", ""),
                "action": action,
                "outcome": "reused" if p.get("reused") else "published",
                "status": "ready",
                "wiki_page_id": p.get("wiki_page_id"),
                "revision_id": p.get("revision_id"),
                "safe_error_code": "",
                "input_hash": p.get("input_hash") or tr.get("input_hash", ""),
                "migration_applied": bool(p.get("migration_applied")),
            })
        elif tr.get("status") == "failed":
            out.append({
                "target_key": key,
                "branch": tr.get("branch", ""),
                "action": action,
                "outcome": "failed",
                "status": "failed",
                "wiki_page_id": None,
                "revision_id": None,
                "safe_error_code": tr.get("safe_error_code") or "VALIDATION_FAILED",
                "input_hash": tr.get("input_hash", ""),
                "migration_applied": bool(tr.get("migration_applied")),
            })
    out.sort(key=lambda t: t.get("target_key") or "")
    return out


def _reconcile_batch_membership(db, state, batch_pages, targets, published, results):
    """统一一次 membership reconcile（不同分支不各自独立删关系）。

    - 成功（published/reused）target → 加入其 wiki；
    - 失败既有 target → 保留旧 membership（wiki 不变，不解除）；
    - 失败新建 target → 不创建 wiki、不新增 membership；
    - not_worthy 页 → 解除旧来源并清 dirty。
    """
    from app.core.wiki_pipeline.pipelines.wiki_default import _page_full_hash

    page_by_id = {p.get("page_id"): p for p in batch_pages}
    target_by_key = {t["key"]: t for t in targets}

    def _wiki_for_target(key: str) -> str | None:
        t = target_by_key.get(key)
        if t is None:
            return None
        if key in published:
            return published[key].get("wiki_page_id")
        # 未发布：失败既有 target 保留旧 membership；新建失败无 wiki。
        return t.get("existing_wiki_id") or None

    page_targets: dict[str, list[str]] = {}
    for key, t in target_by_key.items():
        for pid in t.get("page_ids") or []:
            page_targets.setdefault(pid, []).append(key)

    context = state.get("context") or {}
    page_hashes = context.get("page_input_hashes") or {}

    for pid, pdec in page_by_id.items():
        status = pdec.get("status")
        if status == "page_deleted":
            continue
        desired: set[str] = set()
        if status == "create_update":
            for key in page_targets.get(pid, []):
                wid = _wiki_for_target(key)
                if wid:
                    desired.add(wid)
        _safe_reconcile_membership(db, pid, desired)

        page = db.get(Page, pid)
        if page is None:
            continue
        if status == "not_worthy":
            page.wiki_dirty = False
            page.wiki_compiled_content_hash = page_hashes.get(pid) or ""
            page.wiki_last_error = None
            continue
        if status != "create_update":
            continue
        keys = page_targets.get(pid, [])
        all_ok = keys and all(k in published for k in keys)
        if all_ok and _page_full_hash(db, pid) == page_hashes.get(pid):
            page.wiki_dirty = False
            page.wiki_compiled_content_hash = page_hashes.get(pid) or ""
            page.wiki_last_error = None
        else:
            page.wiki_dirty = True
            page.wiki_last_error = "batch_partial"


def _build_batch_manifest(ctx, outcome, published, final_results, *, partial: bool,
                          plan_hash: str | None) -> dict:
    state = ctx.get("state") or {}
    context = state.get("context") or {}
    page_id = context.get("page_id")
    if partial:
        outcome_code, fail_code, retryable = "keep_dirty", "BATCH_PARTIAL", True
    else:
        outcome_code, fail_code, retryable = ("published", None, True) if published \
            else ("not_worthy", None, True)

    graph_targets = []
    for p in published.values():
        if p.get("wiki_page_id"):
            graph_targets.append({"kind": "wiki", "wiki_page_id": p["wiki_page_id"]})
        for spid in sorted(set(p.get("source_page_ids") or [])):
            graph_targets.append({"kind": "page", "page_id": spid})
    graph_targets = sorted(
        graph_targets,
        key=lambda x: (x["kind"], x.get("wiki_page_id") or x.get("page_id") or ""),
    )
    # 去重（同 wiki 同 page 不重复）。
    dedup: list[dict] = []
    seen: set = set()
    for g in graph_targets:
        ident = (g["kind"], g.get("wiki_page_id") or g.get("page_id"))
        if ident in seen:
            continue
        seen.add(ident)
        dedup.append(g)
    graph_targets = dedup
    wiki_ids = sorted({p["wiki_page_id"] for p in published.values()
                       if p.get("wiki_page_id")})
    revision_ids = sorted({p["revision_id"] for p in published.values()
                           if p.get("revision_id")})
    return {
        "outcome": outcome_code,
        "fail_code": fail_code,
        "retryable": retryable,
        "page_id": page_id,
        "wiki_page_ids": wiki_ids,
        "revision_ids": revision_ids,
        "archived_wiki_ids": [],
        "dirty_wiki_ids": [],
        "graph_targets": graph_targets,
        "input_hash": context.get("input_hash") or "",
        "plan_hash": plan_hash or "",
        "note": outcome.get("note") or "",
        "target_results": final_results,
    }
