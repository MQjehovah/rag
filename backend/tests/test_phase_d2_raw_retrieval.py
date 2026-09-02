"""V4 Phase D-2：原始文档最多两轮检索测试。

覆盖：Wiki sufficient 不调用 Raw、Round 1/2 判定、最多两轮、跨组/退役隔离、
降级（Embedding/Reranker/LLM）、两轮去重、Page 不垄断、不 import Card/KO、
trace 不包含越权候选信息。
"""
from __future__ import annotations

import inspect
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.retrieval.orchestrator import RetrievalOrchestrator
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


def _seed_page(db, page_id, title, chunks, group="engineering", source_type=None, source_state=None):
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
            page_id=page_id, state=source_state or "active",
        ))
    db.flush()


def _seed_wiki(db, page_id, title, content, acl_scope='{"groups": ["engineering"]}'):
    page = WikiPage(id=page_id, title=title, summary="", acl_scope=acl_scope, status="published")
    db.add(page); db.flush()
    rev = WikiRevision(id=f"{page_id}-rev", wiki_page_id=page_id, title=title, summary="", status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id=f"{page_id}-sec", revision_id=rev.id, section_type="facts", heading="正文", content=content, order_index=1))
    page.current_revision_id = rev.id
    db.flush()


def _hit(chunk_id, page_id, content, final_score=1.0):
    return RawChunkHit(
        chunk_id=chunk_id, page_id=page_id, notebook_id=f"nb-{page_id}",
        page_title=page_id, content=content, chunk_index=0,
        final_score=final_score, bm25_score=final_score, dense_score=None,
        rerank_score=None, source_type=None, source_url=None, retrieval_round=1,
    )


class _FakeRawRetriever:
    """按轮次返回预设命中的 fake retriever（用于编排逻辑测试）。"""

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


# ---------------------------------------------------------------------------
# RawDocumentRetriever 单元
# ---------------------------------------------------------------------------

def test_raw_retriever_basic_hit(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定到机架并拧紧螺栓"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.hits, "应有命中"
    hit = result.hits[0]
    assert hit.page_id == "p1"
    assert hit.page_title == "水箱安装"
    assert hit.bm25_score is not None
    assert hit.retrieval_round == 1


def test_cross_group_chunk_never_enters(db):
    _seed_page(db, "p-sales", "销售主题", ["销售内容与报价规则"], group="sales")
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "销售报价", _user(["engineering"]))
    assert result.hits == []


def test_admin_sees_all_pages(db):
    _seed_page(db, "p-eng", "工程主题", ["工程内容与部署规范"], group="engineering")
    _seed_page(db, "p-sales", "销售主题", ["销售内容与报价规则"], group="sales")
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "工程 销售", _user(["admins"]))
    page_ids = {h.page_id for h in result.hits}
    assert "p-eng" in page_ids
    assert "p-sales" in page_ids


def test_retired_source_item_not_retrieved(db):
    """失效远程 Page（无同 Connector active SourceItem）不得被检索。

    J-1 最终返工：失效远程 Page 在 ACL 层（get_visible_page_ids）即被排除，
    不进入 raw_retriever 的 eligible 阶段；旧实现是在 raw_retriever 内过滤并
    计入 filtered_out_page_count，新语义在更早的统一入口排除。
    """
    _seed_page(db, "p1", "水箱安装", ["水箱固定到机架并拧紧螺栓"], source_type="gitlab", source_state="deleted")
    db.commit()
    from app.core import access_control
    visible = access_control.get_visible_page_ids(db, _user(["engineering"]))
    assert "p1" not in visible, "失效远程 Page 必须从可见集合排除"
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.hits == []


def test_embedding_unavailable_degrades_to_bm25(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定到机架并拧紧螺栓"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.bm25_used is True
    assert result.dense_used is False
    assert "embedding_unavailable" in result.degraded


def test_reranker_unavailable_uses_fusion(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定到机架并拧紧螺栓"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    assert result.reranker_used is False
    assert "reranker_unavailable" in result.degraded
    assert all(h.final_score > 0 for h in result.hits)


def test_dense_used_when_embedding_available(db):
    import numpy as np
    page = _seed_page(db, "p1", "水箱安装", ["水箱固定到机架并拧紧螺栓"])
    # 给 chunk 写一个向量
    chunk = db.query(PageChunk).filter_by(page_id="p1").first()
    chunk.embedding = json.dumps([1.0, 0.0, 0.0])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(
        db, "水箱螺栓", _user(["engineering"]), query_embedding=[1.0, 0.0, 0.0],
    )
    assert result.dense_used is True
    assert "embedding_unavailable" not in result.degraded


def test_page_monopoly_limited(db):
    # page A 10 个 chunk 都命中，page B 1 个高分 chunk
    _seed_page(db, "pA", "主题A", [f"水箱螺栓 {i}" for i in range(10)], group="engineering")
    _seed_page(db, "pB", "主题B", ["水箱螺栓 水箱螺栓 水箱螺栓"], group="engineering")
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    from app.core.retrieval.raw_retriever import MAX_CHUNKS_PER_PAGE
    a_count = sum(1 for h in result.hits if h.page_id == "pA")
    assert a_count <= MAX_CHUNKS_PER_PAGE
    assert any(h.page_id == "pB" for h in result.hits)


# ---------------------------------------------------------------------------
# Raw SufficiencyJudge
# ---------------------------------------------------------------------------

def test_judge_no_hit_insufficient():
    verdict = judge_raw_sufficiency([], "水箱螺栓")
    assert verdict.sufficient is False
    assert verdict.reason == "no_raw_hit"


def test_judge_sufficient_two_complementary_chunks(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])
    _seed_page(db, "p2", "螺栓规格", ["螺栓规格说明"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱螺栓", _user(["engineering"]))
    verdict = judge_raw_sufficiency(result.hits, "水箱螺栓")
    assert verdict.sufficient is True


def test_judge_insufficient_low_coverage(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定方法"])
    db.commit()
    result = RawDocumentRetriever(db).retrieve(db, "水箱故障", _user(["engineering"]))
    verdict = judge_raw_sufficiency(result.hits, "水箱故障")
    assert verdict.sufficient is False
    assert "故障" in verdict.missing_tokens


# ---------------------------------------------------------------------------
# QuerySupplement
# ---------------------------------------------------------------------------

def test_supplement_deterministic_without_llm():
    # "螺栓" 已存在于原查询 "水箱螺栓"，不能作为 added_terms
    sup = build_supplemental_query(
        "水箱螺栓", round1_missing_aspects=["螺栓"],
    )
    assert "螺栓" not in sup.added_terms
    assert sup.supplemental_query != "水箱螺栓"
    assert sup.added_terms  # 至少引入一个新 token


def test_supplement_fallback_not_repeat():
    sup = build_supplemental_query("水箱螺栓", round1_missing_aspects=[])
    assert sup.supplemental_query != "水箱螺栓"
    assert sup.added_terms
    # 兜底也不得重复原查询 token
    assert "水箱" not in sup.added_terms
    assert "螺栓" not in sup.added_terms


# ---------------------------------------------------------------------------
# merge_rounds
# ---------------------------------------------------------------------------

def test_merge_rounds_dedup_by_chunk_id():
    from app.core.retrieval.raw_retriever import RawChunkHit
    h1 = RawChunkHit(chunk_id="c1", page_id="p1", notebook_id="n1", page_title="t", content="a", chunk_index=0, final_score=0.8, bm25_score=0.8, dense_score=None, rerank_score=None, source_type=None, source_url=None, retrieval_round=1)
    h2 = RawChunkHit(chunk_id="c1", page_id="p1", notebook_id="n1", page_title="t", content="a", chunk_index=0, final_score=0.9, bm25_score=0.9, dense_score=None, rerank_score=None, source_type=None, source_url=None, retrieval_round=2)
    merged = merge_rounds([h1], [h2])
    assert len(merged) == 1
    assert merged[0].final_score == 0.9


# ---------------------------------------------------------------------------
# Orchestrator 集成
# ---------------------------------------------------------------------------

def test_wiki_sufficient_raw_zero_calls(db):
    _seed_wiki(db, "w1", "水箱安装", "水箱固定到机架并拧紧螺栓")
    db.commit()
    orch = RetrievalOrchestrator(db)
    out = orch.retrieve("水箱安装", _user(["engineering"]))
    assert out.mode == "wiki_hit"
    assert out.raw_calls == 0
    assert out.trace["mode"] == "wiki_hit"


def test_wiki_insufficient_round1_sufficient(db):
    _seed_page(db, "p1", "水箱安装", ["水箱固定螺栓"])
    _seed_page(db, "p2", "螺栓规格", ["螺栓规格说明"])
    db.commit()
    orch = RetrievalOrchestrator(db)
    out = orch.retrieve("水箱螺栓", _user(["engineering"]))
    assert out.mode == "raw_hit"
    assert out.raw_calls == 1


def test_round1_insufficient_then_round2(db):
    # 编排逻辑：Round 1 只命中“水箱”（缺“故障”），Round 2 补充命中“故障”后 sufficient
    fake = _FakeRawRetriever({
        1: [_hit("c1", "p1", "水箱安装方法")],
        2: [_hit("c2", "p2", "故障排查指南")],
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake)
    out = orch.retrieve("水箱故障", _user(["engineering"]))
    assert out.raw_calls == 2
    assert out.mode == "raw_hit"


def test_two_rounds_still_insufficient(db):
    # 编排逻辑：两轮都只命中“水箱”，仍缺“故障”
    fake = _FakeRawRetriever({
        1: [_hit("c1", "p1", "水箱安装方法")],
        2: [_hit("c1", "p1", "水箱安装方法")],
    })
    orch = RetrievalOrchestrator(db, raw_retriever=fake)
    out = orch.retrieve("水箱故障", _user(["engineering"]))
    assert out.raw_calls == 2
    assert out.mode == "need_community_expansion"


def test_trace_contains_no_unauthorized(db):
    _seed_page(db, "p-eng", "工程主题", ["工程内容与部署规范"], group="engineering")
    _seed_page(db, "p-sales", "机密销售", ["销售机密报价"], group="sales")
    db.commit()
    orch = RetrievalOrchestrator(db)
    out = orch.retrieve("工程部署", _user(["engineering"]))
    trace_str = json.dumps(out.trace, ensure_ascii=False)
    assert "p-sales" not in trace_str
    assert "销售机密报价" not in trace_str
    assert "机密销售" not in trace_str


def test_no_card_imports():
    import app.core.retrieval.raw_retriever as rr
    import app.core.retrieval.raw_sufficiency_judge as rsj
    import app.core.retrieval.query_supplement as qs
    import app.core.retrieval.orchestrator as oc
    for mod in (rr, rsj, qs, oc):
        src = inspect.getsource(mod)
        import_lines = [ln for ln in src.splitlines() if ln.startswith(("from ", "import "))]
        joined = "\n".join(import_lines)
        for forbidden in ("KnowledgeCard", "KnowledgeCardBlock", "KnowledgeCommunity", "KnowledgeObject", "KnowledgeClaim"):
            assert forbidden not in joined, f"{mod.__name__} 不应 import {forbidden}"
