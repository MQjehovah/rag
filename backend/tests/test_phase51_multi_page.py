"""Phase 5.1：多 Page 两阶段聚合 + 真实 Map-Reduce 覆盖。

two_phase_build（legacy 识别 + wiki.default manual_rebuild CompileRun 聚合）应与旧
build_wiki_from_pages(dedupe_synthesis=True) 等价：
- 多个 Page 汇入同一 Wiki 只产生一次聚合 Revision（不产生中间 Revision）；
- 超长多来源真实触发 Map-Reduce（wiki-batch-summary ×N + wiki-mapreduce ×1）；
- 固定样本：最终 Wiki / current Revision / Section / source_page_ids 语义一致。

runner 注意：wiki.default synthesize 在 stage 内 `asyncio.run(_synthesize_content(...))`，
其 await 的 llm 是同步 runner。故注入给 executor 的 llm_runner 必须是**同步返回 dict**
（内部不调用 asyncio.run，否则与外层 loop 嵌套冲突）。legacy 识别阶段直接 await async llm。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.wiki_pipeline import executor, registry
from app.core.wiki_pipeline.pipelines.multi_page import two_phase_build
from app.core.wiki_pipeline.pipelines.wiki_default import (
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    Notebook,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)

_SYNTH_BODY = "聚合正文：包含所有来源知识点"


@pytest.fixture(autouse=True)
def _isolate():
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    unregister_default_pipeline()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _mk_page(db, page_id, title, content, notebook_id="nb-1", group_id="engineering"):
    if db.get(Notebook, notebook_id) is None:
        db.add(Notebook(id=notebook_id, name="库", group_id=group_id))
        db.flush()
    ws = ensure_notebook_workspace(db, db.get(Notebook, notebook_id))
    if db.get(Page, page_id) is None:
        db.add(Page(id=page_id, notebook_id=notebook_id, title=title, content=content))
        db.flush()
    db.commit()
    return ws.id, db.get(Page, page_id)


def _graph_noop(**kw):
    return True


def _single_wiki(db):
    return db.query(WikiPage).first()


# ---------------------------------------------------------------------------
# fake LLM：同返回 dict，一为 async（legacy await），一为 sync（pipeline runner）
# ---------------------------------------------------------------------------


def _mk_fake_llm(*, ingest_ops=None, body=_SYNTH_BODY, exc=None, contexts=None,
                 long_batch=False, mapreduce_body=None):
    def _decide(context):
        if contexts is not None:
            contexts.append(context)
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": ingest_ops or []}
        if context == "wiki-synthesis":
            return {"summary": "聚合摘要", "content": body}
        if context == "wiki-batch-summary":
            return {"summary": ("批次摘要内容" * 200) if long_batch else "批次摘要"}
        if context == "wiki-mapreduce":
            return {"summary": "最终", "content": mapreduce_body or body}
        return {"worthy": True, "ops": ingest_ops or []}

    async def _async_llm(messages, context="", timeout=120.0):
        if exc is not None:
            raise exc
        return _decide(context)

    def _sync_llm(messages, context="", timeout=120.0):
        if exc is not None:
            raise exc
        return _decide(context)

    return _async_llm, _sync_llm


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_two_page_single_wiki_single_publish(db):
    register_default_pipeline()
    _mk_page(db, "p1", "来源一", "水箱维护知识A足够长内容用于合成构建。")
    _, p2 = _mk_page(db, "p2", "来源二", "水箱维护知识B足够长内容用于合成构建。",
                     notebook_id="nb-1")
    async_llm, sync_llm = _mk_fake_llm(
        ingest_ops=[{"action": "update", "title": "水箱维护流程"}])
    executor.configure_external_runners(llm_runner=sync_llm, graph_runner=_graph_noop)

    stats = two_phase_build(db, [db.get(Page, "p1"), p2], llm_json=async_llm)

    assert stats["failed"] == 0
    wiki = _single_wiki(db)
    assert wiki is not None
    assert wiki.status == "published"
    assert wiki.dirty is False
    ids = json.loads(wiki.source_page_ids or "[]")
    assert set(ids) == {"p1", "p2"}
    assert db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count() == 1
    rev = db.get(WikiRevision, wiki.current_revision_id)
    facts = db.query(WikiSection).filter(
        WikiSection.revision_id == rev.id, WikiSection.section_type == "facts"
    ).first()
    assert facts is not None and _SYNTH_BODY in (facts.content or "")
    for pid in ("p1", "p2"):
        p = db.get(Page, pid)
        assert p.wiki_dirty is False
        assert p.wiki_compiled_content_hash


def test_many_pages_one_synthesis_call_for_wiki(db):
    register_default_pipeline()
    pages = []
    for i in range(6):
        _, pg = _mk_page(db, f"p{i+1}", f"来源{i+1}", f"聚合内容{i+1}足够长用于构建合成。",
                         notebook_id="nb-1")
        pages.append(pg)
    contexts: list[str] = []
    async_llm, sync_llm = _mk_fake_llm(
        ingest_ops=[{"action": "update", "title": "聚合主题"}], contexts=contexts)
    executor.configure_external_runners(llm_runner=sync_llm, graph_runner=_graph_noop)

    two_phase_build(db, pages, llm_json=async_llm)

    syn_calls = [c for c in contexts if c == "wiki-synthesis"]
    assert len(syn_calls) == 1, f"期望 1 次聚合合成，实际 {len(syn_calls)}"


def test_mapreduce_triggered_many_sources(db):
    register_default_pipeline()
    pages = []
    for i in range(8):
        _, pg = _mk_page(db, f"s{i}", f"来源{i}", f"来源{i}的独特超长正文内容。" * 400,
                         notebook_id="nb-1")
        pages.append(pg)
    contexts: list[str] = []
    async_llm, sync_llm = _mk_fake_llm(
        ingest_ops=[{"action": "update", "title": "超长主题"}], contexts=contexts,
        long_batch=True, mapreduce_body="mapreduce 最终正文")
    executor.configure_external_runners(llm_runner=sync_llm, graph_runner=_graph_noop)

    two_phase_build(db, pages, llm_json=async_llm)

    assert contexts.count("wiki-mapreduce") == 1, "应真实触发一次最终 Map-Reduce"
    assert contexts.count("wiki-batch-summary") >= 2, "多来源应分批 batch-summary"
    wiki = _single_wiki(db)
    if wiki is not None and wiki.status == "published":
        rev = db.get(WikiRevision, wiki.current_revision_id)
        facts = db.query(WikiSection).filter(
            WikiSection.revision_id == rev.id, WikiSection.section_type == "facts"
        ).first()
        assert facts is not None and "mapreduce 最终正文" in (facts.content or "")


def test_single_page_fallback(db):
    register_default_pipeline()
    _mk_page(db, "p1", "单页", "单页足够长内容用于构建主题识别与聚合。")
    async_llm, sync_llm = _mk_fake_llm(
        ingest_ops=[{"action": "create", "title": "单页主题"}])
    executor.configure_external_runners(llm_runner=sync_llm, graph_runner=_graph_noop)
    stats = two_phase_build(db, [db.get(Page, "p1")], llm_json=async_llm)
    assert stats["failed"] == 0
    wiki = _single_wiki(db)
    assert wiki is not None and wiki.status == "published"
