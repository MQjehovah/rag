"""Phase 5：默认 Wiki 编译流水线（wiki.default v1）与 stage 编排。

把旧 Builder（wiki_page_builder）的无 commit 写原语 + 纯计算编排进统一 Pipeline：

- 七 stage：resolve_context → topic_route → synthesize_default → validate_default
  → publish_default（唯一产品写）→ finalize_compile_outcome（Run 失败语义）
  → schedule_graph。
- 阶段顺序/可重试/allows_publish 契约见 Phase 5 契约；validate/publish
  retryable=False；publish_default allows_publish=True（唯一可返回 output_revision_id）。
- stage 全程只拿 StageSession（add/flush/query/get/delete，禁 commit/rollback）；
  事务边界由 Pipeline Manager 独占；LLM/图谱经 ctx["llm_runner"] /
  ctx["graph_runner"]（executor 注入，测试可替换，默认走真实实现）。
- 旧 Builder 内部批量 `Query.delete()`（_sync_version_sources /
  reconcile_page_wiki_membership / remove_source_page_from_wikis）在 StageQuery 上被禁
  （Phase 5 阻塞点）：本文件提供等价 stage 安全实现（逐行 delete），不改旧函数。

已知边界（Phase 5.1）：
- page_deleted 触发在 Page 行仍存在时走 remove_source 语义；行已删 → not applicable。
- publish_default 把产物态失败（LLM 不可用/非法/stale/部分目标失败/workspace 不匹配）
  记为 Publish Manifest；finalize_compile_outcome 据此使 Run 真实 failed（不再虚假
  succeeded）。Page/Wiki 保持 dirty + 安全 last_error，不覆盖已发布 Revision
  （产品安全由 dirty 刷新兜底）。
- 幂等重放守卫命中（run.output_revision_id 已回填，retry 重放 publish）时不产 manifest；
  schedule_graph 读上一 attempt 的 Artifact manifest 恢复全部图谱目标。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from types import SimpleNamespace

from app.core import access_control
from app.core.knowledge_compiler_v3 import versioning
from app.core.knowledge_compiler_v3.wiki_page_builder import (
    CONTEXT_CHAR_LIMIT,
    DEFAULT_CATEGORY,
    LLMServiceUnavailable,
    MAX_TOPICS_PER_PAGE,
    MIN_CONTENT_CHARS,
    VersionedContent,
    WIKI_INGEST_PROMPT,
    _append_revision,
    _append_versioned_revision,
    _find_similar_wiki,
    _has_any_version_evidence,
    _identify_topics,
    _index_text,
    _load_scope_wikis_from_snapshot,
    _llm_outcome,
    _page_input_hash,
    _page_scope,
    _page_text,
    _page_version,
    _parse_source_pages,
    _scope_to_acl_json,
    _set_source_pages,
    _synthesize_content,
    _synthesize_versioned,
    call_wiki_llm_json,
    normalize_wiki_title,
)
from app.core.wiki_pipeline.pipelines.dto import (
    FAIL_INVALID_RESPONSE,
    FAIL_PARTIAL_SYNTHESIS,
    FAIL_SERVICE_UNAVAILABLE,
    FAIL_STALE_INPUT,
    FAIL_VALIDATION_FAILED,
    FAIL_WORKSPACE_MISMATCH,
    OUTCOME_ARCHIVED,
    OUTCOME_KEEP_DIRTY,
    OUTCOME_NOT_APPLICABLE,
    OUTCOME_NOT_WORTHY,
    OUTCOME_NOOP,
    OUTCOME_PUBLISHED,
    ResolveContext,
    SynthesisResult,
    TopicDecision,
    ValidationReport,
)
from app.core.wiki_pipeline.registry import (
    FailureTransition,
    PipelineDef,
    StageDef,
    register_pipeline,
    unregister_pipeline,
)
from app.core.wiki_workspace.routing import page_workspace_id
from app.models.database import (
    KnowledgeCompileArtifact as Artifact,
    Notebook,
    Page,
    WikiPage,
    WikiSection,
    WikiVersionSource,
)

logger = logging.getLogger(__name__)

PIPELINE_KEY = "wiki.default"
PIPELINE_VERSION = "1"

# Publish Manifest 产物（wiki.default 唯一持久化 Artifact）。
ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST = "wiki_publish_manifest"
ARTIFACT_SCHEMA_WIKI_PUBLISH = "wiki-publish/v1"
MANIFEST_OBJECT_TYPE = "wiki_publish"

# stage 契约 flag 参照（与 fake 对齐；全部非 cachable）。
_STAGE_FLAGS = {
    "resolve_context": {"retryable": True, "allows_publish": False},
    "topic_route": {"retryable": True, "allows_publish": False},
    "synthesize_default": {"retryable": True, "allows_publish": False},
    "validate_default": {"retryable": False, "allows_publish": False},
    "publish_default": {"retryable": False, "allows_publish": True},
    "finalize_compile_outcome": {"retryable": False, "allows_publish": False},
    "schedule_graph": {"retryable": True, "allows_publish": False},
}

STAGE_KEYS = tuple(_STAGE_FLAGS)


# ---------------------------------------------------------------------------
# runner 解析（ctx 注入优先；缺省走真实实现）
# ---------------------------------------------------------------------------


def _llm_runner(ctx) -> "Callable":
    """返回同步 llm_runner(messages, context, timeout) -> dict。"""
    runner = ctx.get("llm_runner")
    if runner is not None:
        return runner
    return _default_llm_runner


def _default_llm_runner(messages, context: str = "", timeout: float = 120.0) -> dict:
    """真实 LLM runner：async call_wiki_llm_json 经 asyncio.run 同步化。

    worker pump 在 ThreadPoolExecutor 线程执行（无运行中事件循环），asyncio.run 安全；
    stage 内不得自行开事件循环/事务，仅此一个异步汇入点。
    """
    return asyncio.run(call_wiki_llm_json(messages, context=context, timeout=timeout))


def _graph_runner(ctx):
    runner = ctx.get("graph_runner")
    if runner is not None:
        return runner
    return _default_graph_runner


def _default_graph_runner(*, wiki_page_id=None, page_id=None) -> bool:
    """真实图谱 runner：**同步等待真实 rebuild 完成**（Phase 5.1）。

    用独立短生命周期 engine/session 调 v4_graph_builder.rebuild_wiki_graph /
    rebuild_page_graph（幂等派生数据；内部自带事务 commit）。任一目标抛异常 →
    向上抛（由 schedule_graph stage 捕获记 GRAPH_BUILD_FAILED，不回滚已发布
    Revision）。返回 True = 全部目标真实重建完成。绝不以"提交后台任务"当成功。
    """
    from app.config import settings
    from app.models.database import get_engine, get_session, init_db
    from app.core.knowledge_compiler_v3 import v4_graph_builder as _v4

    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"graph init_db failed: {exc}") from exc
        db = get_session(engine)
        try:
            if wiki_page_id:
                _v4.rebuild_wiki_graph(db, wiki_page_id, commit=True)
            if page_id:
                _v4.rebuild_page_graph(db, page_id, commit=True)
        finally:
            db.close()
    finally:
        engine.dispose()
    return True


def _synthesis_async_llm(ctx):
    """把同步 runner 适配成旧 Builder 期望的 async llm（await llm(messages, context)）。"""

    async def _call(messages, context: str = "", timeout: float = 120.0) -> dict:
        return _llm_runner(ctx)(messages, context=context, timeout=timeout)

    return _call


# ---------------------------------------------------------------------------
# 纯辅助（无 commit）
# ---------------------------------------------------------------------------


def _scope_from_acl_json(acl_json: str | None) -> "access_control.AccessScope":
    return access_control.scope_from_acl(acl_json or "")


def _context_ok(context: dict) -> bool:
    return bool(context.get("applicable"))


def _content_guard_sig(db, wiki, page_ids) -> str | None:
    """发布前内容稳定性签名（synthesize→publish 跨 stage 窗口的外部变化检测）。

    只覆盖来源 Page 内容 / locked 块 / 当前 revision / acl / workspace；不包含
    wiki.source_page_ids 成员关系（publish 的 _identify_topics 会在窗口内确定性
    改写成员关系，纳入则必然误判 stale）。
    """
    page_hashes: dict[str, str] = {}
    for pid in page_ids:
        p = db.get(Page, pid)
        if p is None:
            page_hashes[pid] = ""
            continue
        scope = _page_scope(db, p)
        acl = _scope_to_acl_json(scope) if scope else None
        page_hashes[pid] = _page_input_hash(
            p.title or "", _page_text(db, p), p.notebook_id or "", acl or ""
        )
    locked_hash = ""
    cur_rev = (wiki.current_revision_id if wiki is not None else None) or None
    if cur_rev:
        locked = (
            db.query(WikiSection)
            .filter(
                WikiSection.revision_id == cur_rev,
                WikiSection.locked.is_(True),
            )
            .all()
        )
        payload = [
            {"section_type": s.section_type, "content": s.content or ""}
            for s in locked
        ]
        locked_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    sig = {
        "page_hashes": page_hashes,
        "locked_hash": locked_hash,
        "current_revision_id": cur_rev,
        "acl_scope": (wiki.acl_scope if wiki is not None else None),
        "workspace_id": (wiki.workspace_id if wiki is not None else None),
    }
    return hashlib.sha256(
        json.dumps(sig, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# stage-safe 等价写原语（旧 Builder 用 Query.delete 批量删 → StageQuery 禁 delete）
# ---------------------------------------------------------------------------


def _safe_sync_version_sources(db, wiki: WikiPage, pages: list[Page]) -> None:
    """等价 _sync_version_sources：逐行 delete + add（StageSession 可用）。"""
    scope = wiki.acl_scope
    for row in (
        db.query(WikiVersionSource)
        .filter(WikiVersionSource.wiki_page_id == wiki.id)
        .all()
    ):
        db.delete(row)
    for p in pages:
        p_scope = _page_scope(db, p)
        if p_scope is None or _scope_to_acl_json(p_scope) != scope:
            continue  # 防跨 scope 合并
        if page_workspace_id(db, p) != wiki.workspace_id:
            continue  # 防跨 workspace 合并
        info = _page_version(db, p)
        db.add(WikiVersionSource(
            id=str(uuid.uuid4()),
            wiki_page_id=wiki.id,
            version_label=info["version_label"],
            page_id=p.id,
            chunk_id=None,
            source_item_id=None,
            acl_scope=scope,
        ))


def _safe_reconcile_membership(db, page_id: str, target_wiki_ids: set[str]) -> dict:
    """等价 reconcile_page_wiki_membership（目标集外含该 page 的 wiki 解除来源）。"""
    retained_target_ids: set[str] = set()
    dirty_remaining_wiki_ids: set[str] = set()
    archived_wiki_ids: set[str] = set()

    rows = db.query(WikiPage).filter(WikiPage.source_page_ids.isnot(None)).all()
    for wp in rows:
        ids = _parse_source_pages(wp.source_page_ids)
        if page_id not in ids:
            continue
        if wp.id in target_wiki_ids:
            retained_target_ids.add(wp.id)
            continue
        for vs in (
            db.query(WikiVersionSource)
            .filter(
                WikiVersionSource.wiki_page_id == wp.id,
                WikiVersionSource.page_id == page_id,
            )
            .all()
        ):
            db.delete(vs)
        ids = [i for i in ids if i != page_id]
        if ids:
            _set_source_pages(wp, ids)
            wp.dirty = True
            wp.status = "draft"  # 安全：重建成功前排除正式检索
            dirty_remaining_wiki_ids.add(wp.id)
        else:
            wp.source_page_ids = "[]"
            wp.status = "archived"
            wp.dirty = False
            archived_wiki_ids.add(wp.id)
    return {
        "retained_target_ids": retained_target_ids,
        "dirty_remaining_wiki_ids": dirty_remaining_wiki_ids,
        "archived_wiki_ids": archived_wiki_ids,
    }


def _safe_remove_source_page(db, page_id: str) -> dict:
    """等价 remove_source_page_from_wikis（唯一来源 archived；多来源移除+dirty）。"""
    dirty_remaining_wiki_ids: set[str] = set()
    archived_wiki_ids: set[str] = set()
    rows = db.query(WikiPage).filter(WikiPage.source_page_ids.isnot(None)).all()
    for wp in rows:
        ids = _parse_source_pages(wp.source_page_ids)
        if page_id not in ids:
            continue
        for vs in (
            db.query(WikiVersionSource)
            .filter(
                WikiVersionSource.wiki_page_id == wp.id,
                WikiVersionSource.page_id == page_id,
            )
            .all()
        ):
            db.delete(vs)
        ids = [i for i in ids if i != page_id]
        if ids:
            _set_source_pages(wp, ids)
            wp.dirty = True
            wp.status = "draft"
            dirty_remaining_wiki_ids.add(wp.id)
        else:
            wp.source_page_ids = "[]"
            wp.status = "archived"
            wp.dirty = False
            archived_wiki_ids.add(wp.id)
    return {
        "dirty_remaining_wiki_ids": dirty_remaining_wiki_ids,
        "archived_wiki_ids": archived_wiki_ids,
    }


# ---------------------------------------------------------------------------
# 发布写辅助
# ---------------------------------------------------------------------------


def _refresh_wiki_latest_version(db, wiki: WikiPage) -> None:
    """同步 latest_version（版本化发布后，镜像 rebuild_wiki_from_sources 收尾）。"""
    rows = (
        db.query(WikiSection.version_label)
        .filter(WikiSection.revision_id == wiki.current_revision_id)
        .all()
    )
    labels = [
        r[0]
        for r in rows
        if r[0] and r[0] not in (versioning.IS_COMMON_LABEL, versioning.UNVERSIONED_LABEL)
    ]
    wiki.latest_version = versioning.latest_version(labels)


def _publish_wiki_from_entry(db, wiki: WikiPage, pages: list[Page], entry: dict) -> str | None:
    """把 synthesize 产物写为 Wiki 的下一个 current Revision（镜像 rebuild 尾部）。

    返回 output revision id；content_sig 不匹配（跨 stage 外部变化）→ 返回 None 且
    保持 dirty，不写 Revision。
    """
    if entry is None:
        return None
    sig = entry.get("content_sig")
    page_ids = entry.get("source_page_ids") or [p.id for p in pages]
    if sig is not None and sig != _content_guard_sig(db, wiki, page_ids):
        logger.warning("wiki publish stale content wiki=%s", wiki.id)
        wiki.dirty = True
        return None

    if entry.get("versioned"):
        vc_kwargs = entry.get("vc") or {}
        vc = VersionedContent(
            summary=vc_kwargs.get("summary") or "",
            common=vc_kwargs.get("common") or "",
            versions=vc_kwargs.get("versions") or {},
            diff_notices=vc_kwargs.get("diff_notices") or {},
            unversioned=vc_kwargs.get("unversioned") or "",
            requested_labels=tuple(vc_kwargs.get("requested_labels") or ()),
        )
        rev = _append_versioned_revision(db, wiki, vc, edit_type="auto")
    else:
        content = entry.get("content")
        if not content:
            return None  # 无正文不发布（keep dirty 由调用方决定）
        rev = _append_revision(db, wiki, content, entry.get("summary") or "", edit_type="auto")
    _safe_sync_version_sources(db, wiki, pages)
    if entry.get("versioned"):
        _refresh_wiki_latest_version(db, wiki)
    wiki.status = "published"
    wiki.dirty = False
    return rev.id


# ---------------------------------------------------------------------------
# Publish Manifest（阶段产物：finalize_compile_outcome / schedule_graph 判定依据）
# ---------------------------------------------------------------------------


def _finish_publish(ctx: dict, result: dict) -> dict:
    """publish 分支统一出口：归一 outcome → manifest，写 state 并附加产物字段。

    幂等重放守卫命中（本 run 已发布、retry 重放）不产 artifact —— schedule_graph
    改读上一 attempt 的 manifest Artifact 恢复图谱目标；其余分支一律产审计 manifest。
    """
    state = ctx.setdefault("state", {})
    manifest = _publish_manifest(state)
    state["manifest"] = manifest
    enriched = dict(result)
    enriched["artifact_type"] = ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST
    enriched["schema_version"] = ARTIFACT_SCHEMA_WIKI_PUBLISH
    enriched["object_type"] = MANIFEST_OBJECT_TYPE
    enriched["object_id"] = _manifest_object_id(manifest)
    enriched["payload"] = manifest
    return enriched


def _publish_manifest(state: dict) -> dict:
    """把 publish outcome + context 归一为 Publish Manifest payload（只存 ID/状态）。

    outcome/fail_code 判定表见 _classify_publish_note；fail_code 决定 finalize 是否使
    Run failed。graph_targets 只在真实发布（published）时生成，供 schedule_graph 同步
    重建与 retry 恢复全部目标（wiki + page）。
    """
    publish = state.get("publish") or {}
    context = state.get("context") or {}
    note = publish.get("note") or ""
    outcome, fail_code = _classify_publish_note(note, publish)
    wiki_ids = list(publish.get("wiki_page_ids") or [])
    page_id = context.get("page_id")
    graph_targets: list[dict] = []
    if outcome == OUTCOME_PUBLISHED:
        graph_targets = [
            {"kind": "wiki", "wiki_page_id": wid} for wid in wiki_ids
        ]
        if context.get("trigger_type") == "page_changed" and page_id:
            graph_targets.append({"kind": "page", "page_id": page_id})
    return {
        "outcome": outcome,
        "fail_code": fail_code,
        "retryable": _manifest_retryable(fail_code),
        "page_id": page_id,
        "wiki_page_ids": wiki_ids,
        "revision_ids": list(publish.get("revisions") or []),
        "archived_wiki_ids": list(publish.get("archived_wiki_ids") or []),
        "dirty_wiki_ids": list(publish.get("dirty_wiki_ids") or []),
        "graph_targets": graph_targets,
        "input_hash": context.get("input_hash") or "",
        "note": note,
    }


def _manifest_object_id(manifest: dict) -> str | None:
    return manifest.get("page_id") or (
        manifest["wiki_page_ids"][0] if manifest.get("wiki_page_ids") else None
    )


def _classify_publish_note(note: str, publish: dict) -> tuple[str, str | None]:
    """note/outcome → (outcome 归一, fail_code)。fail_code 命中即 Run 应反映失败。

    判定表（首次命中生效）：
    - published / rebuild_published                          → published
    - wiki_*/rebuild 归档 note                              → archived
    - partial_synthesis                                      → keep_dirty + PARTIAL_SYNTHESIS
    - no_scope/no_workspace_binding/workspace_mismatch/
      scope_changed_during_llm/unknown_scope                → keep_dirty + WORKSPACE_MISMATCH
    - input_changed_during_llm|_synthesis/rebuild_stale      → keep_dirty + STALE_INPUT
    - decision_service_unavailable_keep_dirty、
      rebuild_keep_dirty:service_unavailable                → keep_dirty + SERVICE_UNAVAILABLE
    - decision_invalid_response_keep_dirty、
      rebuild_keep_dirty:invalid_response（含未知子因兜底）  → keep_dirty + INVALID_RESPONSE
    - not_worthy*                                           → not_worthy
    - page_deleted 语义（真实移除分支）                       → archived / noop
    - page_deleted_no_write / 目标不存在等                   → not_applicable
    - 其它（含幂等重放 skip、noop_decision_*）               → noop
    """
    n = note or ""
    archived_count = publish.get("archived") or 0
    if n in ("published", "rebuild_published"):
        return OUTCOME_PUBLISHED, None
    if n in ("wiki_no_workspace_archived", "wiki_no_pages_archived",
             "rebuild_scope_mismatch_archived"):
        return OUTCOME_ARCHIVED, None
    if n == "partial_synthesis":
        return OUTCOME_KEEP_DIRTY, FAIL_PARTIAL_SYNTHESIS
    if n in ("no_workspace_binding", "workspace_mismatch", "no_scope",
             "scope_changed_during_llm", "unknown_scope"):
        return OUTCOME_KEEP_DIRTY, FAIL_WORKSPACE_MISMATCH
    if n in ("input_changed_during_llm", "input_changed_during_synthesis",
             "rebuild_stale_keep_dirty"):
        return OUTCOME_KEEP_DIRTY, FAIL_STALE_INPUT
    if n == "decision_service_unavailable_keep_dirty" or \
            n.startswith("rebuild_keep_dirty:service_unavailable"):
        return OUTCOME_KEEP_DIRTY, FAIL_SERVICE_UNAVAILABLE
    if n == "decision_invalid_response_keep_dirty" or \
            n.startswith("rebuild_keep_dirty:invalid_response"):
        return OUTCOME_KEEP_DIRTY, FAIL_INVALID_RESPONSE
    if n.startswith("rebuild_keep_dirty"):
        return OUTCOME_KEEP_DIRTY, FAIL_INVALID_RESPONSE
    if n.startswith("not_worthy"):
        return OUTCOME_NOT_WORTHY, None
    if n in ("page_deleted_remove_source", "page_deleted_during_publish"):
        return (OUTCOME_ARCHIVED, None) if archived_count else (OUTCOME_NOOP, None)
    if n == "page_deleted_no_write":
        return OUTCOME_NOT_APPLICABLE, None
    if n in ("page_not_found", "wiki_not_found") or n.startswith("unsupported_trigger"):
        return OUTCOME_NOT_APPLICABLE, None
    return OUTCOME_NOOP, None


def _manifest_retryable(fail_code: str | None) -> bool:
    if fail_code in (FAIL_SERVICE_UNAVAILABLE, FAIL_INVALID_RESPONSE,
                     FAIL_STALE_INPUT, FAIL_PARTIAL_SYNTHESIS):
        return True
    if fail_code in (FAIL_WORKSPACE_MISMATCH, FAIL_VALIDATION_FAILED):
        return False
    return True


def _compile_fail_message(fail_code: str | None) -> str:
    """finalize 失败码 → 中文文案（局部映射；不动 registry.stage_error_message）。"""
    return {
        FAIL_SERVICE_UNAVAILABLE: "知识编译失败：LLM 服务不可用（可重试）",
        FAIL_INVALID_RESPONSE: "知识编译失败：LLM 返回非法响应（可重试）",
        FAIL_STALE_INPUT: "知识编译失败：源内容在编译期间发生变化（可重试）",
        FAIL_WORKSPACE_MISMATCH: "知识编译失败：工作区归属缺失或不匹配（不可自动重试）",
        FAIL_VALIDATION_FAILED: "知识编译失败：发布前校验未通过",
        FAIL_PARTIAL_SYNTHESIS: "知识编译失败：部分目标未发布成功（可重试）",
        "GRAPH_BUILD_FAILED": "知识编译失败：图谱重建失败（可重试）",
    }.get(fail_code or "", "知识编译失败")


# ---------------------------------------------------------------------------
# Stage 1: resolve_context
# ---------------------------------------------------------------------------


def _stage_resolve_context(db, run, stage_row, ctx) -> dict:
    """解析 run 目标（Page/Workspace/Binding/Scope）+ 确定性 input_hash，写 ctx 状态。"""
    state = ctx.setdefault("state", {})
    trigger = run.trigger_type

    if trigger == "manual_rebuild":
        context = _resolve_context_manual(db, run)
    elif trigger in ("page_changed", "page_deleted"):
        context = _resolve_context_page(db, run)
    else:
        context = ResolveContext(
            applicable=False,
            reason=f"unsupported_trigger={trigger}",
            trigger_type=trigger,
        ).to_dict()
    state["context"] = context
    return {
        "ok": True,
        "metrics": {
            "stage": "resolve_context",
            "applicable": context["applicable"],
            "reason": context["reason"],
            "page_id": context.get("page_id"),
            "wiki_page_id": context.get("wiki_page_id"),
            "workspace_id": context.get("workspace_id"),
        },
    }


def _resolve_context_manual(db, run) -> dict:
    """manual_rebuild：目标 wiki 的确定性重建输入（来源空 → publish 归档兜底）。"""
    wiki = db.get(WikiPage, run.wiki_page_id) if run.wiki_page_id else None
    if wiki is None:
        return ResolveContext(
            applicable=False, reason="wiki_not_found",
            trigger_type=run.trigger_type, wiki_page_id=run.wiki_page_id,
        ).to_dict()
    if not wiki.workspace_id:
        # 无 workspace 归属 → publish 归档（fail closed，镜像 rebuild_wiki_from_sources）。
        return ResolveContext(
            applicable=True, reason="wiki_no_workspace_archive",
            trigger_type=run.trigger_type, wiki_page_id=wiki.id,
            workspace_id=None, scope_acl_json=wiki.acl_scope,
            title=wiki.title or "", input_hash=run.input_hash or "",
            source_page_ids=tuple(_parse_source_pages(wiki.source_page_ids)),
        ).to_dict()
    return ResolveContext(
        applicable=True, reason="",
        trigger_type=run.trigger_type, wiki_page_id=wiki.id,
        workspace_id=wiki.workspace_id, scope_acl_json=wiki.acl_scope,
        title=wiki.title or "", input_hash=run.input_hash or "",
        source_page_ids=tuple(_parse_source_pages(wiki.source_page_ids)),
    ).to_dict()


def _resolve_context_page(db, run) -> dict:
    """page_changed/page_deleted：读 Page → Notebook scope → workspace → input_hash。"""
    page = db.get(Page, run.trigger_object_id) if run.trigger_object_id else None
    if page is None:
        return ResolveContext(
            applicable=False, reason="page_not_found",
            trigger_type=run.trigger_type, page_id=run.trigger_object_id,
            page_exists=False,
        ).to_dict()
    scope = _page_scope(db, page)
    if scope is None:
        return ResolveContext(
            applicable=False, reason="no_scope",
            trigger_type=run.trigger_type, page_id=page.id, page_exists=True,
        ).to_dict()
    scope_acl = _scope_to_acl_json(scope)
    content_text = _page_text(db, page)
    # Phase 5.1：只读解析 active binding（不 auto-create binding/workspace）。
    # Workspace binding 应在创建 Run 前由正常路由完成；缺 binding → fail closed。
    workspace_id = page_workspace_id(db, page)
    if not workspace_id:
        return ResolveContext(
            applicable=False, reason="no_workspace_binding",
            trigger_type=run.trigger_type, page_id=page.id, notebook_id=page.notebook_id,
            scope_acl_json=scope_acl, title=page.title or "无标题",
            content_text=content_text, page_exists=True,
        ).to_dict()
    if run.workspace_id is not None and workspace_id != run.workspace_id:
        return ResolveContext(
            applicable=False, reason="workspace_mismatch",
            trigger_type=run.trigger_type, page_id=page.id, page_exists=True,
        ).to_dict()
    input_hash = _page_input_hash(
        page.title or "", content_text, page.notebook_id or "", scope_acl
    )
    return ResolveContext(
        applicable=True, reason="",
        trigger_type=run.trigger_type,
        page_id=page.id, workspace_id=workspace_id, notebook_id=page.notebook_id,
        scope_acl_json=scope_acl, title=page.title or "无标题",
        content_text=content_text, input_hash=input_hash,
        source_sync_run_id=run.source_sync_run_id, page_exists=True,
    ).to_dict()


# ---------------------------------------------------------------------------
# Stage 2: topic_route
# ---------------------------------------------------------------------------


def _stage_topic_route(db, run, stage_row, ctx) -> dict:
    """主题路由：读 ContextDTO + workspace 目录，LLM worthy/create/update → TopicDecision。

    只调 LLM（经 llm_runner），不写 Wiki/Revision；删除/无 scope 触发不跑 LLM。
    """
    state = ctx.setdefault("state", {})
    context = state.get("context") or {}
    trigger = run.trigger_type

    if not _context_ok(context):
        decision = TopicDecision(status="not_applicable", note=context.get("reason") or "not_applicable")
    elif trigger == "page_deleted":
        decision = TopicDecision(
            status="page_deleted" if context.get("page_exists") else "not_applicable",
            note="deletion trigger resolved on deletion",
        )
    elif trigger == "manual_rebuild":
        decision = TopicDecision(status="rebuild_wiki", note="rebuild dirty wiki")
    else:
        decision = _route_page_topic(db, run, context, ctx)

    state["topic"] = decision.to_dict()
    return {
        "ok": True,
        "metrics": {
            "stage": "topic_route",
            "status": decision.status,
            "op_count": len(decision.ops),
        },
    }


def _route_page_topic(db, run, context: dict, ctx) -> TopicDecision:
    """page_changed：内容过短 → not_worthy；否则 LLM ingest → 合法 op 校验。"""
    content_text = context.get("content_text") or ""
    if len(content_text.strip()) < MIN_CONTENT_CHARS:
        return TopicDecision(status="not_worthy", note="content_too_short")

    workspace_id = context.get("workspace_id")
    scope_acl = context.get("scope_acl_json")
    if not workspace_id or not scope_acl:
        return TopicDecision(status="not_applicable", note="missing_workspace_or_scope")

    index = _index_text(_load_scope_wikis_from_snapshot(db, scope_acl, workspace_id))
    prompt = WIKI_INGEST_PROMPT.format(
        index=index,
        title=context.get("title") or "无标题",
        content=content_text[:CONTEXT_CHAR_LIMIT],
    )
    try:
        result = _llm_runner(ctx)(
            [{"role": "user", "content": prompt}], context="wiki-ingest-page"
        )
    except LLMServiceUnavailable as exc:
        logger.warning("wiki topic llm unavailable page=%s: %s", context.get("page_id"), exc)
        return TopicDecision(status="service_unavailable", note="llm_service_unavailable")
    except Exception as exc:  # noqa: BLE001
        logger.warning("wiki topic llm error page=%s: %s", context.get("page_id"), exc)
        return TopicDecision(status="service_unavailable", note="llm_error")

    outcome = _llm_outcome(result)
    status_map = {
        "success": "create_update",
        "not_worthy": "not_worthy",
        "invalid_response": "invalid_response",
    }
    status = status_map.get(outcome.status, "invalid_response")
    if outcome.status == "service_unavailable":
        status = "service_unavailable"
    return TopicDecision(status=status, ops=list(outcome.ops), note="")


# ---------------------------------------------------------------------------
# 合成计划（read-only，镜像 _identify_topics 的确定性匹配，不写）
# ---------------------------------------------------------------------------


def _plan_targets(db, context: dict, decision: TopicDecision) -> list[dict]:
    """把合法 ops 归一为发布目标（read-only；写入由 publish 的 _identify_topics 落地）。

    返回按 norm_title 定位的 target；create（目录无相似）source=[page_id]，
    update（命中相似）source=既有来源 ∪ page_id。
    """
    page_id = context.get("page_id")
    workspace_id = context.get("workspace_id")
    scope_acl = context.get("scope_acl_json")
    targets: list[dict] = []
    wikis = _load_scope_wikis_from_snapshot(db, scope_acl, workspace_id) if (workspace_id and scope_acl) else {}
    for op in decision.ops[:MAX_TOPICS_PER_PAGE]:
        action = op.get("action")
        title = (op.get("title") or "").strip()
        if not title or action not in ("create", "update"):
            continue
        category = (op.get("category") or "").strip()[:128]
        norm = normalize_wiki_title(title)
        existing = _find_similar_wiki(norm, wikis)
        if existing is not None:
            existing_id = existing["id"]
            final_title = existing.get("title") or title
            wiki_row = db.get(WikiPage, existing_id)
            source_ids = list(_parse_source_pages(wiki_row.source_page_ids) if wiki_row else [])
            if page_id and page_id not in source_ids:
                source_ids.append(page_id)
            targets.append({
                "action": "update",
                "norm_title": norm,
                "title": final_title,
                "category": category or existing.get("category") or DEFAULT_CATEGORY,
                "existing_id": existing_id,
                "source_page_ids": sorted({i for i in source_ids if i}),
            })
        else:
            final_category = category or DEFAULT_CATEGORY
            source_ids = [page_id] if page_id else []
            targets.append({
                "action": "create",
                "norm_title": norm,
                "title": title,
                "category": final_category,
                "existing_id": None,
                "source_page_ids": source_ids,
            })
    return targets


def _load_pages(db, ids: list[str]) -> list[Page]:
    pages: list[Page] = []
    for pid in ids:
        p = db.get(Page, pid)
        if p is not None:
            pages.append(p)
    return pages


def _standin_wiki(target: dict, db, workspace_id: str | None) -> "SimpleNamespace":
    """为合成提示词构造轻量 wiki 对象（真实行读 current_revision_id 供版本化保留）。"""
    existing_id = target.get("existing_id")
    if existing_id:
        row = db.get(WikiPage, existing_id)
        if row is not None:
            return SimpleNamespace(
                id=row.id, title=row.title or "无标题",
                category=row.category or DEFAULT_CATEGORY,
                current_revision_id=row.current_revision_id,
                acl_scope=row.acl_scope, workspace_id=row.workspace_id,
            )
    return SimpleNamespace(
        id=None, title=target.get("title") or "无标题",
        category=target.get("category") or DEFAULT_CATEGORY,
        current_revision_id=None, acl_scope=None, workspace_id=workspace_id,
    )


# ---------------------------------------------------------------------------
# Stage 3: synthesize_default
# ---------------------------------------------------------------------------


def _stage_synthesize_default(db, run, stage_row, ctx) -> dict:
    """对决策目标合成正文（普通/版本化，复用 _synthesize_* + llm_runner）。"""
    state = ctx.setdefault("state", {})
    context = state.get("context") or {}
    decision_state = state.get("topic") or {}
    decision = TopicDecision(**decision_state)

    if not _context_ok(context):
        synthesis = SynthesisResult(status="not_required", note="context_not_applicable")
        state["synthesis"] = synthesis.to_dict()
        return {"ok": True, "metrics": {"stage": "synthesize_default", "status": synthesis.status}}

    if decision.status in ("not_applicable", "page_deleted", "not_worthy",
                           "invalid_response", "service_unavailable"):
        synthesis = SynthesisResult(status="not_required", note=f"decision_{decision.status}")
        state["synthesis"] = synthesis.to_dict()
        return {"ok": True, "metrics": {"stage": "synthesize_default", "status": synthesis.status}}

    if decision.status == "rebuild_wiki":
        result = _synthesize_rebuild(db, context, ctx)
    elif decision.status == "create_update":
        result = _synthesize_create_update(db, context, decision, ctx)
    else:
        result = SynthesisResult(status="not_required", note=f"unsupported_decision_{decision.status}")

    state["synthesis"] = result.to_dict()
    return {
        "ok": True,
        "metrics": {
            "stage": "synthesize_default",
            "status": result.status,
            "target_count": len(result.targets),
            "ok_count": sum(1 for t in result.targets if result.synthesized.get(t["norm_title"], {}).get("published_ready")),
        },
    }


def _synthesize_rebuild(db, context: dict, ctx) -> SynthesisResult:
    """manual_rebuild：把 dirty wiki 的来源 Pages 整体合成一个正文。"""
    wiki_id = context.get("wiki_page_id")
    wiki = db.get(WikiPage, wiki_id) if wiki_id else None
    if wiki is None:
        return SynthesisResult(status="not_required", note="wiki_not_found")
    pages = _load_pages(db, list(context.get("source_page_ids") or ()))
    if not pages:
        return SynthesisResult(status="not_required", note="no_pages")
    norm = normalize_wiki_title(wiki.title or "")
    target = {
        "action": "rebuild", "norm_title": norm, "title": wiki.title or "无标题",
        "category": wiki.category or DEFAULT_CATEGORY, "existing_id": wiki.id,
        "source_page_ids": sorted({p.id for p in pages}),
    }
    entry = _synthesize_entry(db, target, pages, wiki, context, ctx)
    targets = [target]
    if entry is None:
        return SynthesisResult(status="not_required", note="synthesis_failed", targets=targets)
    ok = bool(entry.get("published_ready"))
    return SynthesisResult(
        status="success" if ok else "invalid_response",
        targets=targets,
        synthesized={norm: entry},
        note="" if ok else "synthesis_failed",
    )


def _synthesize_create_update(db, context: dict, decision: TopicDecision, ctx) -> SynthesisResult:
    """page_changed create/update：按 read-only 计划逐目标合成。"""
    targets = _plan_targets(db, context, decision)
    if not targets:
        return SynthesisResult(status="not_required", note="no_targets")
    synthesized: dict = {}
    ok_count = 0
    for target in targets:
        pages = _load_pages(db, target.get("source_page_ids") or [])
        standin = _standin_wiki(target, db, context.get("workspace_id"))
        # guard 只在既有 wiki（update）需要：外部变化检测（fresh create 无需）。
        entry = _synthesize_entry(db, target, pages, standin, context, ctx)
        if entry is not None and entry.get("published_ready"):
            ok_count += 1
            synthesized[target["norm_title"]] = entry
        elif entry is not None:
            synthesized[target["norm_title"]] = entry  # 记录失败原因供 publish 判定
    status = "success" if ok_count else ("not_required" if not targets else "invalid_response")
    return SynthesisResult(status=status, targets=targets, synthesized=synthesized)


def _synthesize_entry(db, target: dict, pages: list[Page], wiki_obj, context: dict, ctx) -> dict | None:
    """合成单个目标正文；返回 entry（None = 无来源不可合成）。

    entry 结构：{title, category, action, norm_title, source_page_ids, content,
    summary, versioned, vc(dict), guard, published_ready}。
    """
    if not pages:
        return None
    try:
        versioned = _has_any_version_evidence(db, pages)
    except Exception:  # noqa: BLE001
        versioned = False
    # content_sig 只对既有 wiki（update/rebuild）需要：fresh create 无前值可比。
    content_sig = None
    real = None
    if target.get("existing_id"):
        real = db.get(WikiPage, target["existing_id"])
        if real is not None:
            content_sig = _content_guard_sig(
                db, real, sorted({p.id for p in pages})
            )

    try:
        if versioned:
            vc = asyncio.run(_synthesize_versioned(
                db, wiki_obj, pages, _synthesis_async_llm(ctx)
            ))
            if vc is None:
                return {
                    "title": target.get("title") or "", "category": target.get("category") or "",
                    "action": target.get("action") or "", "norm_title": target.get("norm_title") or "",
                    "source_page_ids": [p.id for p in pages], "versioned": True,
                    "vc": None, "content": None, "summary": "", "content_sig": content_sig,
                    "published_ready": False, "fail_reason": "invalid_response",
                }
            entry = {
                "title": target.get("title") or "", "category": target.get("category") or "",
                "action": target.get("action") or "", "norm_title": target.get("norm_title") or "",
                "source_page_ids": [p.id for p in pages],
                "versioned": True, "content_sig": content_sig, "published_ready": True,
                "content": None, "summary": vc.summary,
                "vc": {
                    "summary": vc.summary, "common": vc.common,
                    "versions": dict(vc.versions), "diff_notices": dict(vc.diff_notices),
                    "unversioned": vc.unversioned, "requested_labels": list(vc.requested_labels),
                },
            }
            return entry
        content, summary = asyncio.run(_synthesize_content(
            db, wiki_obj, pages, "", _synthesis_async_llm(ctx)
        ))
        if not content:
            return {
                "title": target.get("title") or "", "category": target.get("category") or "",
                "action": target.get("action") or "", "norm_title": target.get("norm_title") or "",
                "source_page_ids": [p.id for p in pages], "versioned": False,
                "vc": None, "content": None, "summary": "", "content_sig": content_sig,
                "published_ready": False, "fail_reason": "invalid_response",
            }
        return {
            "title": target.get("title") or "", "category": target.get("category") or "",
            "action": target.get("action") or "", "norm_title": target.get("norm_title") or "",
            "source_page_ids": [p.id for p in pages], "versioned": False,
            "vc": None, "content": content, "summary": summary, "content_sig": content_sig,
            "published_ready": True,
        }
    except LLMServiceUnavailable as exc:
        logger.warning("wiki synthesis unavailable target=%s: %s", target.get("norm_title"), exc)
        return {
            "title": target.get("title") or "", "category": target.get("category") or "",
            "action": target.get("action") or "", "norm_title": target.get("norm_title") or "",
            "source_page_ids": [p.id for p in pages], "versioned": versioned,
            "vc": None, "content": None, "summary": "", "content_sig": content_sig,
            "published_ready": False, "fail_reason": "service_unavailable",
        }


# ---------------------------------------------------------------------------
# Stage 4: validate_default
# ---------------------------------------------------------------------------


def _stage_validate_default(db, run, stage_row, ctx) -> dict:
    """发布前校验：input_hash 重算对比 / 正文非空 / workspace 未变 → ValidationReport。

    校验失败不直接判 run 失败（见模块 docstring：产品失败态由 publish 落 dirty）。
    """
    state = ctx.setdefault("state", {})
    context = state.get("context") or {}
    decision = state.get("topic") or {}
    synthesis = state.get("synthesis") or {}

    issues: list[str] = []
    input_hash_unchanged = True
    workspace_unchanged = True
    has_content = False
    revision_targets = 0

    if _context_ok(context):
        # workspace 稳定：resolve_context 已按 run.workspace 校验过，这里复核逻辑态。
        if run.workspace_id and context.get("workspace_id") and \
                run.workspace_id != context.get("workspace_id"):
            workspace_unchanged = False
            issues.append("workspace_mismatch")

        if decision.get("status") in ("create_update",) and context.get("page_id"):
            page = db.get(Page, context["page_id"])
            if page is not None:
                try:
                    scope = _page_scope(db, page)
                    scope_acl = _scope_to_acl_json(scope) if scope else None
                    content_text = _page_text(db, page)
                    fresh_hash = _page_input_hash(
                        page.title or "", content_text, page.notebook_id or "", scope_acl or ""
                    )
                    if fresh_hash != context.get("input_hash"):
                        input_hash_unchanged = False
                        issues.append("input_changed")
                except Exception:  # noqa: BLE001
                    issues.append("page_recheck_failed")

    for norm, entry in (synthesis.get("synthesized") or {}).items():
        if entry and entry.get("published_ready"):
            has_content = True
            revision_targets += 1
        elif entry and entry.get("versioned"):
            vc = entry.get("vc") or {}
            if vc.get("summary") or vc.get("versions"):
                has_content = True

    if not issues:
        for norm, entry in (synthesis.get("synthesized") or {}).items():
            if entry and entry.get("fail_reason") in ("invalid_response", "service_unavailable"):
                issues.append(f"entry_not_published:{norm}:{entry.get('fail_reason')}")
    ok = not issues

    report = ValidationReport(
        ok=ok, issues=issues,
        input_hash_unchanged=input_hash_unchanged,
        workspace_unchanged=workspace_unchanged,
        has_content=has_content,
        revision_targets=revision_targets,
    )
    state["validation"] = report.to_dict()
    return {
        "ok": True,
        "metrics": {
            "stage": "validate_default",
            "report_ok": report.ok,
            "issue_count": len(report.issues),
            "revision_targets": report.revision_targets,
        },
    }


# ---------------------------------------------------------------------------
# Stage 5: publish_default（唯一产品写）
# ---------------------------------------------------------------------------


def _stage_publish_default(db, run, stage_row, ctx) -> dict:
    """发布：把合成结果经旧写原语落 Revision + 置 Page/Wiki 状态（唯一产品写）。

    所有分支（幂等重放守卫除外）经 _finish_publish 归一为 Publish Manifest：
    outcome/fail_code/retryable/wiki_page_ids/revision_ids/archived|dirty_wiki_ids/
    graph_targets/input_hash，写 state["manifest"] 并产 wiki_publish_manifest Artifact。
    """
    state = ctx.setdefault("state", {})
    context = state.get("context") or {}
    decision = state.get("topic") or {}
    synthesis = state.get("synthesis") or {}

    outcome = {
        "applied": False, "created": 0, "updated": 0, "archived": 0,
        "kept_dirty": 0, "revisions": [], "wiki_page_ids": [],
        "archived_wiki_ids": [], "dirty_wiki_ids": [],
        "output_revision_id": None, "note": "",
    }
    state["publish"] = outcome

    def _metrics() -> dict:
        return {
            "stage": "publish_default",
            "note": outcome["note"],
            "applied": outcome["applied"],
            "created": outcome["created"], "updated": outcome["updated"],
            "archived": outcome["archived"], "kept_dirty": outcome["kept_dirty"],
            "revision_count": len(outcome.get("revisions") or []),
        }

    if not _context_ok(context):
        outcome["note"] = context.get("reason") or "not_applicable"
        return _finish_publish(ctx, {"ok": True, "metrics": _metrics()})

    trigger = context.get("trigger_type") or run.trigger_type

    # —— 幂等守卫：本 run 已在更早 attempt 成功发布过 Revision（run.output_revision_id
    # 已回填）→ 本次 publish 视为已完成，绝不重复 append Revision（防止
    # publish 成功后下游 stage（如 schedule_graph）失败 → retry 重放 publish 造成
    # 重复发布 / 重复 Revision）。守卫命中不产 manifest（读上一 attempt Artifact 恢复
    # 图谱目标）。page_deleted 是无 Revision 终态，不适用本守卫。
    if trigger != "page_deleted" and run.output_revision_id:
        outcome["note"] = "already_published_skip_republish"
        outcome["output_revision_id"] = run.output_revision_id
        return {"ok": True, "metrics": _metrics()}

    status = decision.get("status") or "not_applicable"

    if trigger == "page_deleted":
        return _finish_publish(ctx, _publish_page_deleted(db, context, status, ctx))
    if status in ("service_unavailable", "invalid_response"):
        _keep_page_dirty(db, context, status)
        outcome["kept_dirty"] = 1
        outcome["note"] = f"decision_{status}_keep_dirty"
        return _finish_publish(ctx, {"ok": True, "metrics": _metrics()})
    if status == "not_worthy":
        return _finish_publish(ctx, _publish_not_worthy(db, context, ctx))
    if status == "rebuild_wiki":
        return _finish_publish(ctx, _publish_rebuild(db, run, context, synthesis, outcome, ctx))
    if status == "create_update":
        return _finish_publish(ctx, _publish_create_update(db, run, context, decision, synthesis, outcome, ctx))
    outcome["note"] = f"noop_decision_{status}"
    return _finish_publish(ctx, {"ok": True, "metrics": _metrics()})


def _publish_page_deleted(db, context: dict, decision_status: str, ctx) -> dict:
    """page_deleted：Page 行已删 → 无动作；仍在 → remove_source 语义（唯一来源 archived）。"""
    outcome = ctx["state"]["publish"]
    if not context.get("page_exists"):
        outcome.update({"applied": False, "note": "page_deleted_no_write"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}
    removal = _safe_remove_source_page(db, context.get("page_id") or "")
    outcome.update({
        "applied": True, "archived": len(removal["archived_wiki_ids"]),
        "kept_dirty": len(removal["dirty_remaining_wiki_ids"]),
        "archived_wiki_ids": sorted(removal["archived_wiki_ids"]),
        "dirty_wiki_ids": sorted(removal["dirty_remaining_wiki_ids"]),
        "note": "page_deleted_remove_source",
    })
    return {
        "ok": True,
        "metrics": {
            "stage": "publish_default", "note": outcome["note"],
            "archived": outcome["archived"], "kept_dirty": outcome["kept_dirty"],
        },
    }


def _publish_not_worthy(db, context: dict, ctx) -> dict:
    """not_worthy / 内容过短：解除该 Page 的全部 Wiki 来源 + 清 dirty（镜像 _finalize_page）。"""
    page_id = context.get("page_id")
    outcome = ctx["state"]["publish"]
    if not page_id:
        outcome.update({"applied": False, "note": "not_worthy_no_page"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}
    membership = _safe_reconcile_membership(db, page_id, set())
    page = db.get(Page, page_id)
    if page is not None:
        page.wiki_dirty = False
        page.wiki_compiled_content_hash = context.get("input_hash") or ""
        page.wiki_last_error = None
    outcome.update({
        "applied": True, "archived": len(membership["archived_wiki_ids"]),
        "kept_dirty": len(membership["dirty_remaining_wiki_ids"]),
        "archived_wiki_ids": sorted(membership["archived_wiki_ids"]),
        "dirty_wiki_ids": sorted(membership["dirty_remaining_wiki_ids"]),
        "note": "not_worthy_cleanup",
    })
    return {
        "ok": True,
        "metrics": {
            "stage": "publish_default", "note": outcome["note"],
            "archived": outcome["archived"], "kept_dirty": outcome["kept_dirty"],
        },
    }


def _keep_page_dirty(db, context: dict, status: str) -> None:
    """LLM 服务不可用 / 非法响应：保持 Page dirty + 记录 last_error（不覆盖 Revision）。"""
    page_id = context.get("page_id")
    if not page_id:
        return
    page = db.get(Page, page_id)
    if page is not None:
        page.wiki_dirty = True
        page.wiki_last_error = status


def _publish_rebuild(db, run, context, synthesis, outcome: dict, ctx) -> dict:
    """manual_rebuild：dirty wiki 整体重合成发布；空来源/无来源 → archived。"""
    wiki_id = context.get("wiki_page_id")
    wiki = db.get(WikiPage, wiki_id) if wiki_id else None
    if wiki is None:
        outcome["note"] = "wiki_not_found_no_write"
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    if not wiki.workspace_id or not context.get("workspace_id"):
        # 无 workspace 归属 → archived（fail closed）。
        wiki.status = "archived"
        wiki.dirty = False
        outcome.update({"applied": True, "archived": 1,
                        "archived_wiki_ids": [wiki.id], "note": "wiki_no_workspace_archived"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    if not wiki.dirty:
        outcome["note"] = "wiki_not_dirty_no_write"
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    pages = _load_pages(db, list(context.get("source_page_ids") or ()))
    if not pages:
        wiki.status = "archived"
        wiki.dirty = False
        outcome.update({"applied": True, "archived": 1,
                        "archived_wiki_ids": [wiki.id], "note": "wiki_no_pages_archived"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    norm = normalize_wiki_title(wiki.title or "")
    entry = (synthesis.get("synthesized") or {}).get(norm)
    if not entry or not entry.get("published_ready"):
        _keep_page_dirty_from_wiki(db, context, wiki, entry, ctx)
        outcome["kept_dirty"] = 1
        outcome["dirty_wiki_ids"] = [wiki.id]
        outcome["note"] = f"rebuild_keep_dirty:{entry.get('fail_reason') if entry else 'no_synthesis'}"
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    if not _check_wiki_scope_ok(db, wiki, pages):
        wiki.status = "archived"
        wiki.dirty = False
        outcome.update({"applied": True, "archived": 1,
                        "archived_wiki_ids": [wiki.id], "note": "rebuild_scope_mismatch_archived"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    rev_id = _publish_wiki_from_entry(db, wiki, pages, entry)
    if rev_id is None:
        outcome["kept_dirty"] = 1
        outcome["dirty_wiki_ids"] = [wiki.id]
        outcome["note"] = "rebuild_stale_keep_dirty"
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    outcome.update({
        "applied": True, "updated": 1, "revisions": [rev_id],
        "wiki_page_ids": [wiki.id], "output_revision_id": rev_id,
        "note": "rebuild_published",
    })
    return {
        "ok": True,
        "output_revision_id": rev_id,
        "metrics": {"stage": "publish_default", "note": "rebuild_published",
                    "updated": 1, "output_revision_id": rev_id},
    }


def _check_wiki_scope_ok(db, wiki: WikiPage, pages: list[Page]) -> bool:
    """校验每个来源 Page 的 scope/workspace 与 wiki 一致（fail closed，镜像 refresh）。"""
    for p in pages:
        scope = _page_scope(db, p)
        if scope is None or _scope_to_acl_json(scope) != wiki.acl_scope:
            return False
        if page_workspace_id(db, p) != wiki.workspace_id:
            return False
    return True


def _keep_page_dirty_from_wiki(db, context: dict, wiki, entry: dict | None, ctx) -> None:
    """rebuild 目标失败：来源 Page 全部置 dirty + 错误（保持可见 Revision 不变）。"""
    for pid in context.get("source_page_ids") or ():
        page = db.get(Page, pid)
        if page is None:
            continue
        reason = (entry or {}).get("fail_reason")
        page.wiki_dirty = True
        page.wiki_last_error = reason or "wiki_synthesis_failed"


def _publish_create_update(db, run, context, decision, synthesis, outcome: dict, ctx) -> dict:
    """page_changed create/update：识别写入 + 来源协调 + 逐目标发布 Revision。"""
    page_id = context.get("page_id")
    if not page_id:
        outcome["note"] = "no_page_id"
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    page = db.get(Page, page_id)
    if page is None:
        # 页面在识别与发布间被删：解除残留来源即可（镜像删除语义），不新建。
        removal = _safe_remove_source_page(db, page_id)
        outcome.update({
            "applied": True, "archived": len(removal["archived_wiki_ids"]),
            "kept_dirty": len(removal["dirty_remaining_wiki_ids"]),
            "archived_wiki_ids": sorted(removal["archived_wiki_ids"]),
            "dirty_wiki_ids": sorted(removal["dirty_remaining_wiki_ids"]),
            "note": "page_deleted_during_publish",
        })
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    # —— 并发窗口保护（镜像 process_page_wiki 的 LLM 后重算对比）——
    fresh_scope = _page_scope(db, page)
    fresh_acl = _scope_to_acl_json(fresh_scope) if fresh_scope else None
    fresh_text = _page_text(db, page)
    fresh_ws = page_workspace_id(db, page)
    if not fresh_ws:
        page.wiki_dirty = True
        page.wiki_last_error = "no_workspace_binding"
        outcome.update({"applied": True, "kept_dirty": 1, "note": "no_workspace_binding"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}
    if (
        page.notebook_id != context.get("notebook_id")
        or fresh_acl != context.get("scope_acl_json")
        or fresh_ws != context.get("workspace_id")
    ):
        membership = _safe_reconcile_membership(db, page_id, set())
        page.wiki_dirty = True
        page.wiki_last_error = "scope_changed_during_llm"
        outcome.update({
            "applied": True, "kept_dirty": 1,
            "archived_wiki_ids": sorted(membership["archived_wiki_ids"]),
            "dirty_wiki_ids": sorted(membership["dirty_remaining_wiki_ids"]),
            "note": "scope_changed_during_llm",
        })
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}
    fresh_hash = _page_input_hash(
        page.title or "", fresh_text, page.notebook_id or "", fresh_acl or ""
    )
    if fresh_hash != context.get("input_hash"):
        page.wiki_dirty = True
        page.wiki_last_error = "input_changed_during_llm"
        outcome.update({"applied": True, "kept_dirty": 1, "note": "input_changed_during_llm"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    # —— 识别写入（复用旧 _identify_topics）——
    workspace_id = context.get("workspace_id") or ""
    scope_acl = context.get("scope_acl_json")
    scope = _scope_from_acl_json(scope_acl)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        page.wiki_dirty = True
        page.wiki_last_error = "unknown_scope"
        outcome.update({"applied": True, "kept_dirty": 1, "note": "unknown_scope"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}
    wikis = _load_scope_wikis_from_snapshot(db, scope_acl, workspace_id)
    created, updated, target_ids = _identify_topics(
        db, scope, decision.get("ops") or [], wikis, page_id, workspace_id
    )
    _safe_reconcile_membership(db, page_id, target_ids)

    # —— 逐目标发布（按 norm_title 对齐 synthesis 产物）——
    revisions: list[str] = []
    published_wiki_ids: list[str] = []
    dirty_wiki_ids: list[str] = []
    expected = 0
    for norm, entry in (synthesis.get("synthesized") or {}).items():
        if not entry:
            continue
        expected += 1
        d = _load_scope_wikis_from_snapshot(db, scope_acl, workspace_id)
        hit = _find_similar_wiki(norm, d)
        wiki = db.get(WikiPage, hit["id"]) if hit else None
        if wiki is None:
            continue
        pages = _load_pages(db, entry.get("source_page_ids") or [])
        if not pages:
            continue
        if not _check_wiki_scope_ok(db, wiki, pages):
            wiki.dirty = True
            wiki.status = "draft"
            if wiki.id not in dirty_wiki_ids:
                dirty_wiki_ids.append(wiki.id)
            continue
        rev_id = _publish_wiki_from_entry(db, wiki, pages, entry)
        if rev_id is not None:
            revisions.append(rev_id)
            published_wiki_ids.append(wiki.id)
        elif wiki.id not in dirty_wiki_ids:
            dirty_wiki_ids.append(wiki.id)

    # —— Page 状态收尾（镜像 process_page_wiki）——
    all_ok = len(revisions) == expected and expected > 0
    page = db.get(Page, page_id)
    if page is None:
        outcome.update({"note": "page_deleted_during_synthesis"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}
    if not all_ok:
        page.wiki_dirty = True
        page.wiki_last_error = "wiki_synthesis_failed"
        outcome.update({"applied": True, "created": created, "updated": updated,
                        "kept_dirty": 1, "revisions": revisions,
                        "wiki_page_ids": published_wiki_ids,
                        "dirty_wiki_ids": dirty_wiki_ids,
                        "note": "partial_synthesis"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": "partial_synthesis"}}

    cur_scope = _page_scope(db, page)
    cur_acl = _scope_to_acl_json(cur_scope) if cur_scope else None
    cur_hash = _page_input_hash(
        page.title or "", _page_text(db, page), page.notebook_id or "", cur_acl or ""
    )
    if cur_hash == context.get("input_hash"):
        page.wiki_dirty = False
        page.wiki_compiled_content_hash = context.get("input_hash") or ""
        page.wiki_last_error = None
    else:
        page.wiki_dirty = True
        page.wiki_last_error = "input_changed_during_synthesis"
        outcome.update({"applied": True, "created": created, "updated": updated,
                        "kept_dirty": 1, "revisions": revisions,
                        "wiki_page_ids": published_wiki_ids,
                        "dirty_wiki_ids": dirty_wiki_ids,
                        "note": "input_changed_during_synthesis"})
        return {"ok": True, "metrics": {"stage": "publish_default", "note": outcome["note"]}}

    output_revision_id = revisions[0] if len(revisions) == 1 else (revisions[-1] if revisions else None)
    outcome.update({
        "applied": True, "created": created, "updated": updated,
        "revisions": revisions, "wiki_page_ids": published_wiki_ids,
        "output_revision_id": output_revision_id, "note": "published",
    })
    return {
        "ok": True,
        "output_revision_id": output_revision_id,
        "metrics": {"stage": "publish_default", "note": "published",
                    "created": created, "updated": updated,
                    "revision_count": len(revisions)},
    }


# ---------------------------------------------------------------------------
# Stage 6: finalize_compile_outcome（Run 失败语义）
# ---------------------------------------------------------------------------


def _stage_finalize_compile_outcome(db, run, stage_row, ctx) -> dict:
    """依据 Publish Manifest 判定 Run 级编译结果：LLM/编译失败不再虚假 succeeded。

    - fail_code is None（published / not_worthy / archived / noop / not_applicable）→
      ok=True，Run 走 succeeded；
    - SERVICE_UNAVAILABLE / INVALID_RESPONSE / STALE_INPUT / PARTIAL_SYNTHESIS /
      VALIDATION_FAILED → ok=False（error_code=fail_code）→ Run failed；
    - WORKSPACE_MISMATCH → ok=False（retryable=False，workspace fail closed）。

    publish 已把 Page 置 dirty + 不覆盖 Revision（产品安全）；此处只反映 Run 状态。
    """
    state = ctx.setdefault("state", {})
    manifest = state.get("manifest") or _publish_manifest(state)
    fail_code = manifest.get("fail_code")
    metrics = {
        "stage": "finalize_compile_outcome",
        "outcome": manifest.get("outcome"),
        "fail_code": fail_code,
        "retryable": manifest.get("retryable"),
    }
    if fail_code is None:
        return {"ok": True, "metrics": metrics}
    return {
        "ok": False,
        "error_code": fail_code,
        "retryable": bool(manifest.get("retryable", True)),
        "error_message": _compile_fail_message(fail_code),
    }


# ---------------------------------------------------------------------------
# Stage 7: schedule_graph（读持久化 Manifest 同步真实重建）
# ---------------------------------------------------------------------------


def _read_manifest(db, run_id: str) -> dict | None:
    """读本 run 最近一条 wiki_publish_manifest Artifact（跨 attempt 保留）。

    幂等重放 attempt 因 publish 守卫未新写 manifest → 回退读上一 attempt 的产物，
    保证 schedule_graph 重试时能恢复全部图谱目标。
    """
    row = (
        db.query(Artifact)
        .filter(
            Artifact.run_id == run_id,
            Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
        )
        .order_by(Artifact.created_at.desc())
        .first()
    )
    if row is None or not row.payload_json:
        return None
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _stage_schedule_graph(db, run, stage_row, ctx) -> dict:
    """依据持久化 Publish Manifest 对全部 graph_target 同步真实重建图谱。

    publish 成功 → 读 Artifact manifest（重放 attempt 时 publish 幂等守卫未产新
    manifest → 读上一 attempt 的）；对每个 target 用 graph_runner 真实构建：
    {"kind": "wiki", "wiki_page_id"} → runner(wiki_page_id=...)；
    {"kind": "page", "page_id"} → runner(page_id=...)。

    任一目标抛异常 → ok=False GRAPH_BUILD_FAILED（retryable=True）→ Run failed，
    不回滚已发布 Revision；全部完成 → ok=True（metrics 记录 rebuilt 目标数）。
    无 manifest / 无 target → ok=True skipped。
    """
    state = ctx.setdefault("state", {})
    manifest = _read_manifest(db, run.id) or state.get("manifest") or {}
    targets = manifest.get("graph_targets") or []
    if not targets:
        return {
            "ok": True,
            "metrics": {"stage": "schedule_graph", "skipped": True, "targets": 0},
        }

    runner = _graph_runner(ctx)
    rebuilt: list[str] = []
    try:
        for target in targets:
            if target.get("kind") == "wiki" and target.get("wiki_page_id"):
                runner(wiki_page_id=target["wiki_page_id"])
                rebuilt.append(f"wiki:{target['wiki_page_id']}")
            elif target.get("kind") == "page" and target.get("page_id"):
                runner(page_id=target["page_id"])
                rebuilt.append(f"page:{target['page_id']}")
    except Exception:  # noqa: BLE001
        logger.exception("graph build failed run=%s", run.id)
        return {"ok": False, "error_code": "GRAPH_BUILD_FAILED", "retryable": True}
    return {
        "ok": True,
        "metrics": {"stage": "schedule_graph", "rebuilt": rebuilt, "targets": len(rebuilt)},
    }


# ---------------------------------------------------------------------------
# pipeline 定义与注册
# ---------------------------------------------------------------------------


def _stage_defs() -> list[StageDef]:
    executors = {
        "resolve_context": _stage_resolve_context,
        "topic_route": _stage_topic_route,
        "synthesize_default": _stage_synthesize_default,
        "validate_default": _stage_validate_default,
        "publish_default": _stage_publish_default,
        "finalize_compile_outcome": _stage_finalize_compile_outcome,
        "schedule_graph": _stage_schedule_graph,
    }
    descriptions = {
        "resolve_context": "解析 Page/Workspace/Scope/input_hash（无写）",
        "topic_route": "主题路由：LLM worthy/create/update（不写 Wiki/Revision）",
        "synthesize_default": "对目标合成正文（普通/版本化，LLM 经 runner）",
        "validate_default": "发布前校验（input_hash/正文/workspace）",
        "publish_default": "唯一产品写：Revision + source sync + Page/Wiki 状态 + Manifest",
        "finalize_compile_outcome": "按 Manifest 判定 Run 级编译结果（失败语义真实化）",
        "schedule_graph": "读 Manifest 对 wiki/page 目标同步真实图谱重建（幂等）",
    }
    stages: list[StageDef] = []
    for key in STAGE_KEYS:
        flags = _STAGE_FLAGS[key]
        stages.append(StageDef(
            key=key,
            version="1",
            retryable=flags["retryable"],
            cachable=False,
            execute=executors[key],
            failure_transition=FailureTransition.FAIL,
            description=descriptions[key],
            allows_publish=flags["allows_publish"],
        ))
    return stages


def register_default_pipeline() -> None:
    """注册 wiki.default v1（allow_null_workspace=False）。

    同 key+version 已注册 → registry 抛 PipelineError（拒绝静默覆盖）。
    """
    pipeline = PipelineDef(
        key=PIPELINE_KEY,
        version=PIPELINE_VERSION,
        stages=_stage_defs(),
        allow_null_workspace=False,
    )
    register_pipeline(pipeline)


def unregister_default_pipeline() -> None:
    """测试清理用：移除 wiki.default 注册。"""
    unregister_pipeline(PIPELINE_KEY)
