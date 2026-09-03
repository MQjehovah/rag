"""Phase 5.1：多 Page 两阶段聚合编排 driver。

旧 build_wiki_from_pages(dedupe_synthesis=True) 的两阶段语义：
  阶段 1（识别）：对每个 Page 调 process_page_wiki(synthesize=False) —— 只做主题识别 +
  来源协调（建 draft Wiki / 更新 source_page_ids / 置 dirty），**不 append Revision**；
  收集受影响的 wiki 集合（affected_wiki_ids）与每 Page 的 identification_input_hash。
  阶段 2（聚合）：对受影响 wiki 去重，逐个**只聚合发布一次**。
  阶段 3：按 identification hash 并发窗口保护清 Page dirty。

本 driver 在 Phase 5.1 下等价复刻：识别阶段复用已冻结的旧识别路径（legacy 识别 = 决策与
关系写入，非"自动发布"，不违反「发布走 CompileRun」）；聚合发布阶段走 wiki.default
CompileRun（manual_rebuild run，真实 pipeline 聚合、含 Map-Reduce），由 Pipeline Manager
统一事务管理。

目标产物与旧两阶段语义一致：最终每个 Wiki 只发布一次聚合 Revision，不产生多个中间
Revision。固定样本等价见 test_phase51_multi_page.py。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.models.database import Page, WikiPage, get_session

logger = logging.getLogger(__name__)

_PIPELINE_KEY = "wiki.default"


def _identify_pages(
    db,
    pages: list[Page],
    llm,
    *,
    commit: bool = True,
) -> tuple[dict[str, set[str]], dict[str, str], list[str]]:
    """阶段 1：逐 Page 识别（legacy process_page_wiki synthesize=False）。

    返回 (page_to_synthesize, page_identification_hash, affected_wiki_ids)，
    与旧 build_wiki_from_pages 阶段 1 同构。识别副作用（draft/reconcile/dirty）已提交。
    """
    from app.core.knowledge_compiler_v3.wiki_page_builder import process_page_wiki

    page_to_synthesize: dict[str, set[str]] = {}
    page_identification_hash: dict[str, str] = {}
    affected_wiki_ids: set[str] = set()

    for page in pages:
        try:
            outcome = asyncio.run(
                process_page_wiki(db, page.id, llm, commit=commit, synthesize=False)
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
            elif status == "service_unavailable":
                # 保持 dirty，聚合阶段对该 Page 关联 wiki 不发布；由后续 recover 重试。
                continue
            elif status == "invalid_response":
                continue
            elif status in ("deleted", "scope_changed", "stale_input", "no_scope", "no_workspace"):
                continue
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("wiki identify failed page=%s", page.id)

    return page_to_synthesize, page_identification_hash, sorted(affected_wiki_ids)


def _run_manual_rebuild_run(db, wiki_id: str) -> str:
    """阶段 2：对单个受影响 Wiki 建 wiki.default manual_rebuild run 并同步执行。

    返回 run 状态；pipeline publish 完成聚合发布（真实 Revision 写入、置 published/dirty）。
    """
    from app.core.wiki_pipeline.executor import create_run, execute_run

    wiki = db.get(WikiPage, wiki_id)
    if wiki is None or not wiki.workspace_id:
        return "skipped"
    run = create_run(
        db,
        pipeline_key=_PIPELINE_KEY,
        trigger_type="manual_rebuild",
        trigger_object_id=wiki.id,
        wiki_page_id=wiki.id,
        workspace_id=wiki.workspace_id,
        supersede_same_trigger=True,
    )
    db.commit()
    executed = execute_run(db, run.id)
    db.expire_all()
    return executed.status


def two_phase_build(db, pages, *, llm_json=None, commit: bool = True) -> dict:
    """多 Page 两阶段聚合：识别（legacy）→ 逐 wiki manual_rebuild CompileRun 聚合。

    返回 stats，键与旧 build_wiki_from_pages 一致：
    {created, updated, skipped, failed, service_unavailable, not_worthy, invalid_response}。
    """
    from app.core.knowledge_compiler_v3.wiki_page_builder import call_wiki_llm_json

    llm = llm_json or call_wiki_llm_json
    stats = {
        "created": 0, "updated": 0, "skipped": 0, "failed": 0,
        "service_unavailable": 0, "not_worthy": 0, "invalid_response": 0,
    }
    if not pages:
        return stats

    page_to_synthesize, page_hash, affected = _identify_pages(db, pages, llm, commit=commit)

    wiki_results: dict[str, str] = {}
    for wiki_id in affected:
        try:
            wiki_results[wiki_id] = _run_manual_rebuild_run(db, wiki_id)
        except Exception:  # noqa: BLE001
            db.rollback()
            wiki_results[wiki_id] = "failed"
            logger.exception("multi-page rebuild failed wiki=%s", wiki_id)

    # 阶段 3：并发窗口保护 —— 全部目标 wiki 聚合成功后才清 Page dirty（与旧语义一致）。
    for page_id, wiki_ids in page_to_synthesize.items():
        if not wiki_ids:
            continue
        all_ok = all(wiki_results.get(wid) == "succeeded" for wid in wiki_ids)
        fresh = db.get(Page, page_id)
        if fresh is None:
            continue
        if not all_ok:
            fresh.wiki_dirty = True
            fresh.wiki_last_error = "wiki_synthesis_failed"
            stats["failed"] += 1
            continue
        from app.core.knowledge_compiler_v3.wiki_page_builder import (
            _page_input_hash,
            _page_scope,
            _page_text,
            _scope_to_acl_json,
        )
        scope = _page_scope(db, fresh)
        cur_acl = _scope_to_acl_json(scope) if scope else None
        cur_hash = _page_input_hash(
            fresh.title or "", _page_text(db, fresh), fresh.notebook_id or "", cur_acl or ""
        )
        if cur_hash == page_hash.get(page_id):
            fresh.wiki_dirty = False
            fresh.wiki_compiled_content_hash = page_hash.get(page_id) or ""
            fresh.wiki_last_error = None
            stats["updated"] += 1
        else:
            fresh.wiki_dirty = True
            fresh.wiki_last_error = "input_changed_during_synthesis"
            stats["failed"] += 1
    if commit:
        db.commit()
    return stats
