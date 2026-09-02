"""P13-MODEL-02/03：Embedding/Reranker 降级语义测试。"""
from __future__ import annotations

import asyncio

from app.core.retrieval.candidates import CandidateType, RetrievalCandidate
from app.core.retrieval.rerank import CrossEncoderReranker


def _cand(cid, score):
    c = RetrievalCandidate(candidate_type="chunk", candidate_id=cid, content=f"c{cid}")
    c.fused_score = score
    return c


def test_reranker_failure_no_fake_score(monkeypatch):
    """Reranker 失败 → 不写 rerank_score（不伪装 Identity 分数）。"""
    import httpx as h

    class _Resp:
        status_code = 500
        text = "server error"

    def fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr("app.core.retrieval.rerank.httpx.post", fake_post)

    reranker = CrossEncoderReranker(api_url="http://rerank.local", model="m")
    cands = [_cand("c1", 0.9), _cand("c2", 0.3)]
    result = reranker.rerank_with_status("q", cands)

    assert result.used is False
    assert result.degraded is True
    assert result.error_code == "SERVER_ERROR"
    # 候选的 rerank_score 未被写入
    for c in cands:
        assert c.rerank_score is None


def test_reranker_success_writes_score(monkeypatch):
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.8}, {"index": 1, "relevance_score": 0.2}]}

    def fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr("app.core.retrieval.rerank.httpx.post", fake_post)

    reranker = CrossEncoderReranker(api_url="http://rerank.local", model="m")
    cands = [_cand("c1", 0.5), _cand("c2", 0.5)]
    result = reranker.rerank_with_status("q", cands)

    assert result.used is True
    assert cands[0].rerank_score == 0.8
    assert cands[1].rerank_score == 0.2


def test_reranker_unconfigured_degraded():
    reranker = CrossEncoderReranker(api_url="", model="")
    cands = [_cand("c1", 0.9)]
    result = reranker.rerank_with_status("q", cands)
    assert result.used is False
    assert result.error_code == "BAD_REQUEST"


def test_reranker_rerank_preserves_fusion_order_on_failure(monkeypatch):
    """旧 rerank() 接口在失败时保留 Fusion 原排序，不写假分数。"""
    class _Resp:
        status_code = 500
        text = "error"

    def fake_post(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr("app.core.retrieval.rerank.httpx.post", fake_post)

    reranker = CrossEncoderReranker(api_url="http://rerank.local", model="m")
    cands = [_cand("c1", 0.9), _cand("c2", 0.3)]
    result = reranker.rerank("q", cands)

    # 保持 fused_score 降序，且 rerank_score 未写
    assert [c.candidate_id for c in result] == ["c1", "c2"]
    assert all(c.rerank_score is None for c in result)
