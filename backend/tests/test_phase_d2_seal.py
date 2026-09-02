"""V4 Phase D-2 封板验收测试。

覆盖：async 生产接线、Embedding/Reranker 真实状态、Connector 匹配、
ACL 快照、相似 Chunk 充分性、Round 2 跳过、临时目录。
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.embedding.results import EmbeddingBatchResult, RerankResult
from app.core.retrieval.orchestrator import (
    RetrievalOrchestrator,
    build_default_retrieval_orchestrator,
    _embedding_degraded,
)
from app.core.retrieval.query_supplement import build_supplemental_query
from app.core.retrieval.raw_retriever import (
    RawChunkHit,
    RawDocumentRetriever,
    RawRetrievalResult,
)
from app.core.retrieval.raw_sufficiency_judge import judge_raw_sufficiency
from app.models.database import (
    Notebook,
    Page,
    PageChunk,
    SourceConnection,
    SourceItem,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _user(groups):
    return {"id": "u1", "username": "u", "groups": groups}


def _seed_page(db, page_id, title, chunks, group="engineering", source_type=None):
    nb = Notebook(id=f"nb-{page_id}", name=f"nb-{page_id}", group_id=group)
    db.add(nb); db.flush()
    page = Page(
        id=page_id, notebook_id=nb.id, title=title, content="",
        source_type=source_type, source_url=f"url-{page_id}" if source_type else None,
    )
    db.add(page); db.flush()
    for i, txt in enumerate(chunks):
        db.add(PageChunk(id=f"{page_id}-c{i}", page_id=page_id, chunk_index=i, content=txt, content_type="text"))
    if source_type is not None:
        conn = SourceConnection(id=f"conn-{page_id}", connector_key=source_type, name=f"conn-{page_id}")
        db.add(conn); db.flush()
        db.add(SourceItem(
            id=f"si-{page_id}", connection_id=conn.id, external_id=f"ext-{page_id}",
            page_id=page_id, state="active",
        ))
    db.flush()


def _hit(chunk_id, page_id, content, final_score=1.0, chunk_index=0, retrieval_round=1):
    return RawChunkHit(
        chunk_id=chunk_id, page_id=page_id, notebook_id=f"nb-{page_id}",
        page_title=page_id, content=content, chunk_index=chunk_index,
        final_score=final_score, bm25_score=final_score, dense_score=None,
        rerank_score=None, source_type=None, source_url=None,
        retrieval_round=retrieval_round,
    )


def _result(hits, *, visible_page_ids=None, filtered_out_page_count=0, degraded=None, retrieval_round=1, query=""):
    return RawRetrievalResult(
        hits=hits, query=query, retrieval_round=retrieval_round,
        visible_page_ids=set(visible_page_ids or []),
        filtered_out_page_count=filtered_out_page_count,
        degraded=list(degraded or []),
    )


class _FakeRawRetriever:
    """按轮次返回预设 RawRetrievalResult（编排逻辑测试用）。"""

    def __init__(self, results_by_round: dict[int, RawRetrievalResult]):
        self.results_by_round = results_by_round
        self.queries: list[str] = []

    def retrieve(self, db, question, current_user, *, query_embedding=None, retrieval_round=1):
        self.queries.append(question)
        r = self.results_by_round.get(retrieval_round)
        if r is None:
            return RawRetrievalResult(query=question, retrieval_round=retrieval_round)
        return r


class _MockAsyncEmbedder:
    """Mock 异步 embedder：按调用序返回预设 EmbeddingBatchResult，记录 close。"""

    def __init__(self, results: list[EmbeddingBatchResult]):
        self.results = results
        self.calls: list[str] = []
        self.closed = 0

    async def __call__(self, query):
        self.calls.append(query)
        idx = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[idx]

    async def close(self):
        self.closed += 1


# ---------------------------------------------------------------------------
# 一、async 生产接线
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_orchestrator_runs_in_event_loop(db):
    embedder = _MockAsyncEmbedder([
        EmbeddingBatchResult(embeddings=[[1.0, 0.0]], used=True),
        EmbeddingBatchResult(embeddings=[[1.0, 0.0]], used=True),
    ])
    fake = _FakeRawRetriever({
        1: _result([_hit("c1", "p1", "水箱安装方法")]),
        2: _result([_hit("c2", "p2", "故障排查指南")]),
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake, async_embedder=embedder)
    out = await orch.retrieve_async("水箱故障", _user(["engineering"]))
    assert out.raw_calls == 2
    assert embedder.calls == ["水箱故障", "水箱故障 原因 排查 处理 解决"][:2] or len(embedder.calls) == 2
    assert embedder.closed == 1


def test_no_asyncio_run_in_production_path():
    import app.core.retrieval.orchestrator as oc
    src = inspect.getsource(oc)
    assert "asyncio.run(" not in src


@pytest.mark.asyncio
async def test_embedding_first_success_second_timeout(db):
    embedder = _MockAsyncEmbedder([
        EmbeddingBatchResult(embeddings=[[1.0, 0.0, 0.0]], used=True),
        EmbeddingBatchResult(used=False, error_code="TIMEOUT"),
    ])
    fake = _FakeRawRetriever({
        1: _result([_hit("c1", "p1", "水箱安装方法")]),
        2: _result([_hit("c2", "p2", "故障排查指南")]),
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake, async_embedder=embedder)
    out = await orch.retrieve_async("水箱故障", _user(["engineering"]))
    assert embedder.calls and len(embedder.calls) == 2
    assert embedder.closed == 1
    assert "embedding_timeout" in out.raw_results.degraded


def test_factory_returns_async_capable_orchestrator(db):
    orch = build_default_retrieval_orchestrator(
        db,
        async_embedder=_MockAsyncEmbedder([]),
    )
    assert hasattr(orch, "retrieve_async")
    assert orch.async_embedder is not None


def test_embedding_degraded_mapping():
    assert _embedding_degraded(EmbeddingBatchResult(embeddings=[[1.0]], used=True)) == []
    assert _embedding_degraded(EmbeddingBatchResult(used=False, error_code="TIMEOUT")) == ["embedding_timeout"]
    assert _embedding_degraded(EmbeddingBatchResult(used=False, error_code="AUTH_FAILED")) == ["embedding_unauthorized"]
    assert _embedding_degraded(EmbeddingBatchResult(used=False, error_code="INVALID_RESPONSE")) == ["embedding_empty"]


# ---------------------------------------------------------------------------
# 二、Reranker 伪状态
# ---------------------------------------------------------------------------

def test_identity_reranker_no_rerank_score(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.reranker_used is False
    assert all(h.rerank_score is None for h in result.hits)


def test_stateless_reranker_no_rerank_score(db):
    class _Stateless:
        name = "stateless"
        def rerank(self, question, candidates):
            for c in candidates:
                c.rerank_score = 0.99
            return candidates

    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])
    db.commit()
    result = RawDocumentRetriever(db, reranker=_Stateless()).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.reranker_used is False
    assert all(h.rerank_score is None for h in result.hits)


def test_rerank_status_raises_safe_degrade(db):
    class _Throwing:
        name = "throwing"
        def rerank_with_status(self, question, candidates):
            raise RuntimeError("boom")

    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])
    db.commit()
    result = RawDocumentRetriever(db, reranker=_Throwing()).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.reranker_used is False
    assert result.hits
    assert all(h.rerank_score is None for h in result.hits)


def test_partial_rerank_results_not_used(db):
    class _Partial:
        name = "partial"
        def rerank_with_status(self, question, candidates):
            if candidates:
                candidates[0].rerank_score = 0.9
            return RerankResult(used=True, degraded=False, results=[{"index": 0, "relevance_score": 0.9}])

    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓", "螺栓规格说明"])
    db.commit()
    result = RawDocumentRetriever(db, reranker=_Partial()).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.reranker_used is False
    assert "reranker_invalid_response" in result.degraded
    assert all(h.rerank_score is None for h in result.hits)


# ---------------------------------------------------------------------------
# 三、Connector 匹配
# ---------------------------------------------------------------------------

def test_gitlab_page_with_dingtalk_source_not_retrieved(db):
    nb = Notebook(id="nb-p1", name="nb", group_id="engineering")
    db.add(nb); db.flush()
    page = Page(id="p1", notebook_id=nb.id, title="水箱安装", content="", source_type="gitlab")
    db.add(page); db.flush()
    db.add(PageChunk(id="p1-c0", page_id="p1", chunk_index=0, content="水箱固定螺栓"))
    conn = SourceConnection(id="conn-ding", connector_key="dingtalk", name="dingtalk")
    db.add(conn); db.flush()
    db.add(SourceItem(id="si-1", connection_id="conn-ding", external_id="e1", page_id="p1", state="active"))
    db.commit()
    user = _user(["engineering"])
    # J-1：错误 Connector 的 active SourceItem 不得使 Page 有效，该 Page 在 ACL 层
    # 即被排除，故断言 ACL 快照已排除，而非旧 filtered_reason_counts["retired_remote_page"]。
    assert "p1" not in access_control.get_visible_page_ids(db, user)
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", user)
    assert result.hits == []


def test_same_connector_active_and_deleted_retrieved(db):
    nb = Notebook(id="nb-p1", name="nb", group_id="engineering")
    db.add(nb); db.flush()
    page = Page(id="p1", notebook_id=nb.id, title="水箱安装", content="", source_type="gitlab")
    db.add(page); db.flush()
    db.add(PageChunk(id="p1-c0", page_id="p1", chunk_index=0, content="水箱固定螺栓"))
    conn = SourceConnection(id="conn-git", connector_key="gitlab", name="gitlab")
    db.add(conn); db.flush()
    db.add(SourceItem(id="si-deleted", connection_id="conn-git", external_id="e-deleted", page_id="p1", state="deleted"))
    db.add(SourceItem(id="si-active", connection_id="conn-git", external_id="e-active", page_id="p1", state="active"))
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.hits != []


# ---------------------------------------------------------------------------
# 四、统计与 ACL 快照
# ---------------------------------------------------------------------------

def test_filtered_count_not_doubled(db):
    fake = _FakeRawRetriever({
        1: _result([_hit("c1", "p1", "水箱安装方法")], filtered_out_page_count=3),
        2: _result([_hit("c2", "p2", "故障排查指南")], filtered_out_page_count=3),
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake)
    out = orch.retrieve("水箱故障", _user(["engineering"]))
    assert out.raw_calls == 2
    assert out.raw_results.filtered_out_page_count == 3  # 快照，不 sum


def test_acl_snapshot_change_fail_closed(db):
    fake = _FakeRawRetriever({
        1: _result([_hit("c1", "p1", "水箱安装方法")], visible_page_ids={"p1", "p2"}),
        2: _result([_hit("c2", "p2", "故障排查指南")], visible_page_ids={"p1"}),
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake)
    out = orch.retrieve("水箱故障", _user(["engineering"]))
    assert out.trace["raw"]["acl_snapshot_changed"] is True
    assert all(h.page_id != "p2" for h in out.raw_results.hits)


# ---------------------------------------------------------------------------
# 五、相似 Chunk 与单 token 充分性
# ---------------------------------------------------------------------------

def test_similar_chunks_not_complementary():
    # 两个高度相似 Chunk（sim>=0.8），第二个含「故障」，但因相似度高不算互补证据
    h1 = _hit("c1", "p1", "水箱固定到机架并拧紧螺栓", chunk_index=0)
    h2 = _hit("c2", "p1", "水箱固定到机架并拧紧螺栓故障", chunk_index=1)
    verdict = judge_raw_sufficiency([h1, h2], "水箱 螺栓 故障 检查")
    assert verdict.sufficient is False
    assert len(verdict.supporting_chunk_ids) <= 1


def test_single_common_token_not_sufficient(db):
    _seed_page(db, "p1", "水箱安装", ["水箱安装步骤"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "安装", _user(["engineering"]))
    verdict = judge_raw_sufficiency(result.hits, "安装")
    assert verdict.sufficient is False
    assert verdict.reason == "single_token_insufficient"


# ---------------------------------------------------------------------------
# 六、Round 2 跳过
# ---------------------------------------------------------------------------

def test_supplement_cannot_produce_new_token():
    original = "原因 排查 处理 解决 步骤 配置 条件 型号 数值 单位 命令 示例 原理 背景 说明 规范 标准 注意事项 常见问题 详细信息 方法"
    sup = build_supplemental_query(original, round1_missing_aspects=[])
    assert sup.can_supplement is False
    assert sup.added_terms == []


def test_skip_round2_when_no_supplement(db):
    class _NoSupplement:
        supplemental_query = ""
        can_supplement = False
        added_terms = []
        reason = ""

    fake = _FakeRawRetriever({1: _result([_hit("c1", "p1", "水箱安装方法")])})
    orch = RetrievalOrchestrator(db, raw_retriever=fake, supplementer=lambda *a, **k: _NoSupplement())
    out = orch.retrieve("水箱故障", _user(["engineering"]))
    assert out.raw_calls == 1
    assert out.trace["raw"]["round2_skipped_reason"] == "no_semantic_supplement"


# ---------------------------------------------------------------------------
# 七、临时目录
# ---------------------------------------------------------------------------

def test_temp_dir_in_pytest_runtime():
    d = Path(tempfile.gettempdir())
    assert ".pytest_runtime" in d.parts
    assert d.exists()
