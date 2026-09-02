"""P4-BE-06/11：trace + rerank 测试。

覆盖：trace 记录各阶段、summary、IdentityReranker 降级、工厂。
"""
from __future__ import annotations

from app.core.retrieval.candidates import CandidateType, RetrievalCandidate
from app.core.retrieval.rerank import IdentityReranker, get_reranker
from app.core.retrieval.trace import RetrievalTrace


def _cand(cid, score=0.8):
    c = RetrievalCandidate(candidate_type="chunk", candidate_id=cid, content=f"c{cid}")
    c.fused_score = score
    return c


def test_trace_records_stages():
    trace = RetrievalTrace(question="水箱容量", intent="fact_lookup")
    trace.record("chunk_bm25", [_cand("c1", 0.9), _cand("c2", 0.5)])
    trace.record("fused", [_cand("c1", 0.7)])
    assert len(trace.stages) == 2
    assert trace.stages[0].candidate_count == 2
    assert trace.stages[1].candidate_count == 1


def test_trace_summary():
    trace = RetrievalTrace(question="q")
    trace.record("recall1", [_cand("c1")])
    s = trace.summary()
    assert s["question"] == "q"
    assert s["stages"][0]["count"] == 1


def test_trace_to_dict_serializable():
    trace = RetrievalTrace(question="q")
    trace.record("recall1", [_cand("c1")])
    d = trace.to_dict()
    assert d["stages"][0]["candidates"][0]["candidate_id"] == "c1"


def test_identity_reranker_sorts_by_fused_score():
    cands = [_cand("c1", 0.3), _cand("c2", 0.9), _cand("c3", 0.5)]
    result = IdentityReranker().rerank("q", cands)
    assert [c.candidate_id for c in result] == ["c2", "c3", "c1"]
    # 降级路径不写 rerank_score（Phase D-2 封板：不伪造伪状态）
    assert all(c.rerank_score is None for c in result)


def test_get_reranker_identity_without_url():
    assert get_reranker("").name == "identity"


def test_get_reranker_cross_encoder_with_url():
    assert get_reranker("http://rerank.local").name == "cross_encoder"
