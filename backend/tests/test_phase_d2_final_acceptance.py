"""V4 Phase D-2 最终验收补漏测试。

覆盖：补充查询语义变化、Round 2 结果返回上游、生产接线（Mock）、
Embedding/Reranker 真实状态、SourceItem/Notebook fail-closed、
中文去重、Raw sufficiency 收紧、trace 不泄露越权信息。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.embedding.results import RerankResult
from app.core.retrieval.orchestrator import (
    RetrievalOrchestrator,
    build_default_retrieval_orchestrator,
)
from app.core.retrieval.query_supplement import build_supplemental_query
from app.core.retrieval.raw_retriever import (
    RawChunkHit,
    RawDocumentRetriever,
    RawRetrievalResult,
    merge_rounds,
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


def _mk_hit(chunk_id, page_id, content, final_score=1.0, chunk_index=0, retrieval_round=1):
    return RawChunkHit(
        chunk_id=chunk_id, page_id=page_id, notebook_id=f"nb-{page_id}",
        page_title=page_id, content=content, chunk_index=chunk_index,
        final_score=final_score, bm25_score=final_score, dense_score=None,
        rerank_score=None, source_type=None, source_url=None,
        retrieval_round=retrieval_round,
    )


class _FakeRawRetriever:
    """按轮次返回预设命中（编排逻辑测试用）。"""

    def __init__(self, hits_by_round: dict[int, list[RawChunkHit]]):
        self.hits_by_round = hits_by_round
        self.queries: list[str] = []

    def retrieve(self, db, question, current_user, *, query_embedding=None, retrieval_round=1):
        self.queries.append(question)
        return RawRetrievalResult(
            hits=list(self.hits_by_round.get(retrieval_round, [])),
            query=question,
            retrieval_round=retrieval_round,
        )


class _FakeReranker:
    """按 used 返回成功/失败的 mock reranker（不联网）。"""

    name = "fake_reranker"

    def __init__(self, used: bool = True, error_code: str | None = None):
        self.used = used
        self.error_code = error_code
        self.calls = 0

    def rerank_with_status(self, question, candidates):
        self.calls += 1
        if self.used:
            for i, c in enumerate(candidates):
                c.rerank_score = 1.0 - i * 0.1
            return RerankResult(
                used=True, degraded=False,
                results=[{"index": i, "relevance_score": 1.0 - i * 0.1} for i in range(len(candidates))],
            )
        return RerankResult(used=False, degraded=True, error_code=self.error_code or "TIMEOUT")


# ---------------------------------------------------------------------------
# 一、补充查询语义变化
# ---------------------------------------------------------------------------

def test_supplement_not_repeat_original():
    sup = build_supplemental_query("水箱故障", round1_missing_aspects=["故障"])
    assert sup.supplemental_query != "水箱故障 故障"
    assert sup.supplemental_query != "水箱故障"
    assert "故障" not in sup.added_terms  # 故障已在原查询
    assert sup.added_terms


def test_supplement_adds_new_meaningful_token():
    sup = build_supplemental_query("水箱故障", round1_missing_aspects=["故障"])
    orig_set = set(sup.original_tokens)
    assert any(t not in orig_set for t in sup.added_terms)


def test_llm_enhance_same_original_falls_back():
    def bad_enhance(original, added):
        return original  # 只重复原查询

    sup = build_supplemental_query(
        "水箱故障", round1_missing_aspects=["故障"], llm_enhance=bad_enhance,
    )
    assert sup.supplemental_query != "水箱故障"
    assert sup.added_terms


# ---------------------------------------------------------------------------
# 二、Round 2 结果返回上游
# ---------------------------------------------------------------------------

def test_round2_hits_in_final_raw_results(db):
    fake = _FakeRawRetriever({
        1: [_mk_hit("c1", "p1", "水箱安装方法")],
        2: [_mk_hit("c2", "p2", "故障排查指南")],
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake)
    out = orch.retrieve("水箱故障", _user(["engineering"]))
    assert out.raw_calls == 2
    ids = {h.chunk_id for h in out.raw_results.hits}
    assert "c2" in ids  # Round 2 新命中
    assert "c1" in ids


def test_round2_result_differs_from_round1(db):
    fake = _FakeRawRetriever({
        1: [_mk_hit("c1", "p1", "水箱安装方法")],
        2: [_mk_hit("c2", "p2", "故障排查指南")],
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake)
    out = orch.retrieve("水箱故障", _user(["engineering"]))
    assert out.raw_round_results[0] is not out.raw_round_results[1]
    assert out.raw_results.hits != out.raw_round_results[0].hits
    # 最终 raw_results.hits == 合并后的两轮结果
    assert out.raw_results.hits == merge_rounds(
        out.raw_round_results[0].hits, out.raw_round_results[1].hits,
    )


# ---------------------------------------------------------------------------
# 三、生产接线（Mock）
# ---------------------------------------------------------------------------

def test_factory_injects_mock_embedder_and_reranker(db):
    calls: list[str] = []

    def embedder(query):
        calls.append(query)
        return [1.0, 0.0]

    reranker = _FakeReranker(used=True)
    orch = build_default_retrieval_orchestrator(db, embedder=embedder, reranker=reranker)
    assert orch.embedder is embedder
    assert orch.raw_retriever.reranker is reranker


def test_round1_round2_generate_embedding_separately(db):
    calls: list[str] = []

    def embedder(query):
        calls.append(query)
        return [1.0, 0.0]

    fake = _FakeRawRetriever({
        1: [_mk_hit("c1", "p1", "水箱安装方法")],
        2: [_mk_hit("c2", "p2", "故障排查指南")],
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake, embedder=embedder)
    out = orch.retrieve("水箱故障", _user(["engineering"]))
    assert out.raw_calls == 2
    assert len(calls) == 2
    assert calls[0] == "水箱故障"  # Round 1 用原问题
    assert calls[1] != "水箱故障"  # Round 2 用补充查询


def test_reranker_mock_success_used_true(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])
    db.commit()
    reranker = _FakeReranker(used=True)
    result = RawDocumentRetriever(db, reranker=reranker).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.reranker_used is True
    assert "reranker_unavailable" not in result.degraded


def test_reranker_failure_uses_fusion(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])
    db.commit()
    reranker = _FakeReranker(used=False, error_code="TIMEOUT")
    result = RawDocumentRetriever(db, reranker=reranker).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.reranker_used is False
    assert "reranker_timeout" in result.degraded
    # 失败不写虚假 rerank_score（保留 Fusion 原排序）
    assert all(h.rerank_score is None for h in result.hits)
    assert all(h.final_score > 0 for h in result.hits)


# ---------------------------------------------------------------------------
# 四、Embedding 真实状态
# ---------------------------------------------------------------------------

def test_dense_false_when_no_chunk_vectors(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])  # chunk 无 embedding
    db.commit()
    result = RawDocumentRetriever(db).retrieve(
        db, "水箱螺栓", _user(["engineering"]), query_embedding=[1.0, 0.0, 0.0],
    )
    assert result.dense_used is False
    assert "no_indexed_vectors" in result.degraded


def test_dense_false_on_dimension_mismatch(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])
    chunk = db.query(PageChunk).filter_by(page_id="p1").first()
    chunk.embedding = json.dumps([1.0, 0.0, 0.0, 0.0])  # 4 维
    db.commit()
    result = RawDocumentRetriever(db).retrieve(
        db, "水箱螺栓", _user(["engineering"]), query_embedding=[1.0, 0.0, 0.0],  # 3 维
    )
    assert result.dense_used is False
    assert "embedding_dimension_mismatch" in result.degraded


# ---------------------------------------------------------------------------
# 五、SourceItem / Notebook fail-closed
# ---------------------------------------------------------------------------

def test_source_item_null_state_not_retrieved(db):
    # 远程 Page + SourceItem.state 为 NULL（不可检索）
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"], source_type="gitlab")
    db.query(SourceItem).filter_by(page_id="p1").update({"state": None})
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.hits == []


def test_remote_page_without_source_item_not_retrieved(db):
    # 远程 Page（source_type 非空）但无任何 SourceItem
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"], source_type="gitlab")
    db.query(SourceItem).delete()
    db.commit()
    user = _user(["engineering"])
    # J-1：失效远程 Page（无 active SourceItem）在 ACL 层即被排除，不再进入 Raw
    # 检索的 eligible 计数分支，故此处断言 ACL 快照已排除该 Page，而非旧的
    # filtered_reason_counts["retired_remote_page"] 计数（该分支已上移到 access_control）。
    assert "p1" not in access_control.get_visible_page_ids(db, user)
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", user)
    assert result.hits == []


def test_page_with_deleted_and_active_source_item_retrieved(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"], source_type="gitlab")
    db.query(SourceItem).filter_by(page_id="p1").update({"state": "deleted"})
    db.add(SourceItem(
        id="si-active", connection_id="conn-p1", external_id="e-active",
        page_id="p1", state="active",
    ))
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.hits != []


def test_orphan_page_not_retrieved_even_admin(db):
    # 临时关闭 FK 构造孤儿 page（notebook_id 指向不存在 Notebook）
    sqlite_conn = db.connection().connection.driver_connection
    sqlite_conn.execute("PRAGMA foreign_keys=OFF")
    page = Page(id="p1", notebook_id="nonexistent-nb", title="水箱安装", content="")
    db.add(page); db.flush()
    db.add(PageChunk(id="p1-c0", page_id="p1", chunk_index=0, content="水箱固定螺栓"))
    db.commit()
    sqlite_conn.execute("PRAGMA foreign_keys=ON")
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["admins"]))
    assert result.hits == []
    assert result.filtered_reason_counts.get("notebook_not_found", 0) >= 1


def test_blank_chunk_not_candidate(db):
    _seed_page(db, "p1", "水箱安装", ["   \n\t  ", "水箱固定螺栓"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.hits
    assert all((h.content or "").strip() for h in result.hits)


# ---------------------------------------------------------------------------
# 六、中文去重
# ---------------------------------------------------------------------------

def test_adjacent_duplicate_folded_keep_high():
    h1 = _mk_hit("c1", "p1", "水箱固定到机架并拧紧螺栓确保牢固", final_score=0.5, chunk_index=0)
    h2 = _mk_hit("c2", "p1", "水箱固定到机架并拧紧螺栓确保牢靠", final_score=0.9, chunk_index=1)
    merged = merge_rounds([h1], [h2])
    assert len(merged) == 1
    assert merged[0].final_score == 0.9
    assert merged[0].chunk_id == "c2"


def test_merge_rounds_does_not_mutate_originals():
    h1 = _mk_hit("c1", "p1", "水箱固定螺栓", final_score=0.5, chunk_index=0)
    h2 = _mk_hit("c1", "p1", "水箱固定螺栓", final_score=0.9, chunk_index=0)
    r1, r2 = [h1], [h2]
    merge_rounds(r1, r2)
    assert r1[0].final_score == 0.5  # 未修改
    assert r2[0].final_score == 0.9


# ---------------------------------------------------------------------------
# 七、Raw sufficiency 收紧
# ---------------------------------------------------------------------------

def test_single_complete_chunk_sufficient(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓并拧紧"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    verdict = judge_raw_sufficiency(result.hits, "水箱螺栓")
    assert verdict.sufficient is True
    assert verdict.reason == "single_chunk_complete"
    assert verdict.supporting_chunk_ids


def test_two_unrelated_chunks_half_coverage_insufficient():
    h1 = _mk_hit("c1", "p1", "水箱安装方法")
    h2 = _mk_hit("c2", "p2", "电池容量说明")
    verdict = judge_raw_sufficiency([h1, h2], "水箱故障")
    assert verdict.sufficient is False
    assert verdict.coverage < 0.75
    assert "故障" in verdict.missing_tokens


def test_duplicate_chunks_not_complementary():
    h1 = _mk_hit("c1", "p1", "水箱安装方法")
    h2 = _mk_hit("c2", "p1", "水箱安装方法")
    verdict = judge_raw_sufficiency([h1, h2], "水箱螺栓")
    assert verdict.sufficient is False
    # 第二个重复 Chunk 未新增 token，不算互补
    assert len(verdict.supporting_chunk_ids) <= 1


# ---------------------------------------------------------------------------
# 八、trace 不泄露越权信息
# ---------------------------------------------------------------------------

def test_trace_no_unauthorized_and_no_group_id(db):
    _seed_page(db, "p-eng", "工程主题", ["工程内容与部署规范"], group="engineering")
    _seed_page(db, "p-sales", "机密销售", ["销售机密报价"], group="sales")
    db.commit()
    orch = RetrievalOrchestrator(db)
    out = orch.retrieve("工程部署", _user(["engineering"]))
    trace_str = json.dumps(out.trace, ensure_ascii=False)
    assert "p-sales" not in trace_str
    assert "销售机密报价" not in trace_str
    assert "机密销售" not in trace_str
    assert "sales" not in trace_str  # 组名不泄露
    # acl_diagnostic 只能是安全枚举
    for h in (out.raw_results.hits if out.raw_results else []):
        assert h.acl_diagnostic in ("company", "group", "admin")
