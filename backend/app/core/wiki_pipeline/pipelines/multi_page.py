"""Phase 5.2：多 Page batch 聚合编排 driver（单一 wiki.default batch CompileRun）。

旧 build_wiki_from_pages(dedupe_synthesis=True) 的两阶段语义现在完全由 wiki.default 的
`batch_rebuild` trigger 在**单个 batch CompileRun** 内完成：

- `create_batch_run`（wiki_default.py）→ 写 wiki_batch_input 输入 Artifact →
  execute_run（真实 7-stage：resolve_context 读 artifact 恢复 page_ids / topic_route
  逐页 LLM / synthesize 汇总去重目标 / publish 每受影响 wiki 一次聚合 Revision）。
- 不再逐 page 调用 legacy process_page_wiki 提交 draft/membership（batch 阶段 1 无副作用）。
- 一个 Wiki 只产一个最终 Revision（无中间 Revision）；多来源超阈值真实 Map-Reduce。

目标产物语义与旧两阶段一致（固定样本等价见 test_phase52_batch.py /
test_phase51_multi_page.py batch 化）。

为兼容旧等价测试，保留 `_legacy_identify_pages`（私有，仅测试显式调用）；生产 driver
`batch_build` 一律走 batch run。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.models.database import KnowledgeCompileArtifact as Artifact
from app.models.database import Page, WikiPage

logger = logging.getLogger(__name__)

_PIPELINE_KEY = "wiki.default"


def _legacy_identify_pages(
    db,
    pages: list[Page],
    llm,
    *,
    commit: bool = True,
) -> tuple[dict[str, set[str]], dict[str, str], list[str]]:
    """[私有/仅等价测试] legacy 阶段 1：逐 Page _legacy_process_page_wiki(synthesize=False)。

    Phase 5.2 生产 driver 不再使用（batch run 在 topic_route 汇总）；保留供旧等价
    测试显式调用对比。副作用与旧 build_wiki_from_pages 阶段 1 同构。
    """
    from app.core.knowledge_compiler_v3.wiki_page_builder import _legacy_process_page_wiki

    page_to_synthesize: dict[str, set[str]] = {}
    page_identification_hash: dict[str, str] = {}
    affected_wiki_ids: set[str] = set()

    for page in pages:
        try:
            outcome = asyncio.run(
                _legacy_process_page_wiki(db, page.id, llm, commit=commit, synthesize=False)
            )
            status = outcome.get("status")
            if status == "identified":
                to_syn = outcome.get("to_synthesize") or set()
                page_to_synthesize[page.id] = set(to_syn)
                page_identification_hash[page.id] = outcome.get("identification_input_hash") or ""
                affected_wiki_ids.update(to_syn)
            elif status == "not_worthy":
                dirty = outcome.get("dirty_remaining_wiki_ids") or set()
                affected_wiki_ids.update(dirty)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("wiki legacy identify failed page=%s", page.id)

    return page_to_synthesize, page_identification_hash, sorted(affected_wiki_ids)


def _batch_stats_from_run(db, run, pre_wiki_ids: set[str], page_ids: list[str]) -> dict:
    """把 batch run 结果归一为旧 build_wiki_from_pages 返回键的 stats。"""
    from app.core.wiki_pipeline.pipelines.wiki_default import (
        ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    )

    stats = {
        "created": 0, "updated": 0, "skipped": 0, "failed": 0,
        "service_unavailable": 0, "not_worthy": 0, "invalid_response": 0,
    }
    if run.status != "succeeded":
        code = run.safe_error_code or ""
        stats["failed"] += 1
        if code == "SERVICE_UNAVAILABLE":
            stats["service_unavailable"] += 1
            stats["skipped"] += 1
        elif code == "INVALID_RESPONSE":
            stats["invalid_response"] += 1
        return stats
    row = (
        db.query(Artifact)
        .filter(
            Artifact.run_id == run.id,
            Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
        )
        .order_by(Artifact.created_at.desc())
        .first()
    )
    manifest = {}
    if row is not None and row.payload_json:
        try:
            manifest = json.loads(row.payload_json)
        except (TypeError, ValueError):
            manifest = {}
    wiki_ids = manifest.get("wiki_page_ids") or []
    created = sum(1 for wid in wiki_ids if wid not in pre_wiki_ids)
    stats["created"] = created
    stats["updated"] = max(0, len(wiki_ids) - created)
    note = manifest.get("note") or ""
    if manifest.get("outcome") == "not_worthy" or note.startswith("not_worthy"):
        stats["not_worthy"] = len(page_ids)
    return stats


def batch_build(db, workspace_id: str, page_ids, *, llm_json=None, runner=None,
                commit: bool = True) -> dict:
    """多 Page batch 聚合：建 batch run → execute_run → manifest 归一 stats。

    与旧 build_wiki_from_pages 返回键兼容：
    {created, updated, skipped, failed, service_unavailable, not_worthy, invalid_response}。

    runner 注入遵循 executor 契约（configure_external_runners 在调用前配置好；
    缺省走真实 LLM）。llm_json（async）仅当 executor 未注入 llm runner 时被包装兜底。
    commit 参数保留兼容（batch run 由 executor 分 stage 事务管理，不再逐 page commit）。
    """
    from app.core.wiki_pipeline import executor as executor_mod
    from app.core.wiki_pipeline.executor import execute_run
    from app.core.wiki_pipeline.pipelines.wiki_default import create_batch_run

    ids = sorted({p for p in page_ids})
    if not ids:
        return {
            "created": 0, "updated": 0, "skipped": 0, "failed": 0,
            "service_unavailable": 0, "not_worthy": 0, "invalid_response": 0,
        }
    if llm_json is not None and runner is None and executor_mod._LLM_RUNNER is None:  # noqa: SLF001
        def _sync_wrap(messages, context: str = "", timeout: float = 120.0) -> dict:
            return asyncio.run(llm_json(messages, context=context, timeout=timeout))
        executor_mod.configure_external_runners(llm_runner=_sync_wrap)
    if runner is not None and executor_mod._LLM_RUNNER is None:  # noqa: SLF001
        executor_mod.configure_external_runners(llm_runner=runner)

    pre_wiki_ids = {
        w.id for w in db.query(WikiPage).filter(WikiPage.workspace_id == workspace_id).all()
    }
    run = create_batch_run(db, workspace_id=workspace_id, page_ids=ids)
    db.commit()
    executed = execute_run(db, run.id)
    db.expire_all()
    return _batch_stats_from_run(db, executed, pre_wiki_ids, ids)


def two_phase_build(db, pages, *, llm_json=None, commit: bool = True) -> dict:
    """向后兼容别名：多 Page 两阶段聚合（现走 batch run，语义等价旧两阶段）。

    workspace 由各 Page 归属推导（单 workspace 约束，镜像 batch 语义）。
    """
    from app.core.wiki_workspace.routing import page_workspace_id

    if not pages:
        return {
            "created": 0, "updated": 0, "skipped": 0, "failed": 0,
            "service_unavailable": 0, "not_worthy": 0, "invalid_response": 0,
        }
    ws_ids = {page_workspace_id(db, p) for p in pages}
    if len(ws_ids) != 1:
        raise ValueError(
            f"two_phase_build: 多 Page 必须归属同一 workspace（得到 {ws_ids}）"
        )
    ws_id = next(iter(ws_ids))
    if ws_id is None:
        raise ValueError("two_phase_build: pages 未归属 workspace（fail closed）")
    return batch_build(
        db, ws_id, [p.id for p in pages], llm_json=llm_json, commit=commit
    )
