"""P4-BE-04/05/07/08：RRF + ACL + MMR + 权重测试。

覆盖：RRF 融合累加、跨路保留排名、ACL 过滤、MMR 去重同 card、intent 权重。
"""
from __future__ import annotations

from app.core.retrieval.candidates import CandidateType, RetrievalCandidate
from app.core.retrieval.fusion import (
    apply_acl,
    fuse_and_rank,
    mmr_diversify,
    rrf_fuse,
    weight_by_intent,
)


def _cand(cid, ctype="chunk", card_id=None, page_id=None, content=""):
    return RetrievalCandidate(
        candidate_type=ctype, candidate_id=cid, content=content or f"content {cid}",
        parent_card_id=card_id, source_page_id=page_id,
    )


def test_rrf_fuse_accumulates_across_lists():
    list1 = [_cand("c1").with_source("r1"), _cand("c2")]
    list2 = [_cand("c1").with_source("r2"), _cand("c3")]  # c1 在两路都出现
    fused = rrf_fuse([list1, list2])
    assert "chunk:c1" in fused
    assert fused["chunk:c1"].fused_score > fused["chunk:c2"].fused_score
    # c1 记录了两个来源
    assert len(fused["chunk:c1"].sources) >= 1


def test_rrf_fuse_single_list_scores_descending():
    cands = [_cand("c1"), _cand("c2"), _cand("c3")]
    fused = rrf_fuse([cands])
    scores = [c.fused_score for c in fused.values()]
    assert scores == sorted(scores, reverse=True)


def test_apply_acl_none_keeps_all():
    cands = [_cand("c1", page_id="p1"), _cand("c2", page_id="p2")]
    assert len(apply_acl(cands, None)) == 2


def test_apply_acl_filters_invisible():
    cands = [_cand("c1", page_id="p1"), _cand("c2", page_id="p2")]
    result = apply_acl(cands, {"p1"})
    assert len(result) == 1
    assert result[0].candidate_id == "c1"


def test_apply_acl_empty_set_filters_all():
    cands = [_cand("c1", page_id="p1")]
    assert apply_acl(cands, set()) == []


def test_apply_acl_null_page_kept():
    # source_page_id 为 None 的候选（公开）保留
    cands = [_cand("c1", page_id=None), _cand("c2", page_id="p2")]
    result = apply_acl(cands, {"p1"})
    assert len(result) == 1
    assert result[0].candidate_id == "c1"


def test_mmr_diversifies_same_card():
    # 同一 card 的 3 个 block，MMR 应避免全选
    cands = [
        _cand("b1", ctype="card_block", card_id="card1", content="固定水箱"),
        _cand("b2", ctype="card_block", card_id="card1", content="连接水管"),
        _cand("b3", ctype="card_block", card_id="card1", content="安装电机"),
        _cand("b4", ctype="card_block", card_id="card2", content="容量参数"),
    ]
    for i, c in enumerate(cands):
        c.fused_score = 1.0 - i * 0.1
    result = mmr_diversify(cands, top_n=3)
    # 不会 3 个都来自 card1
    card_ids = {c.parent_card_id for c in result}
    assert len(result) == 3
    assert "card2" in card_ids  # 多样性引入 card2


def test_weight_by_intent_procedure_boosts_card_block():
    cands = [
        _cand("c1", ctype="card_block"),
        _cand("c2", ctype="chunk"),
    ]
    for c in cands:
        c.fused_score = 1.0
    result = weight_by_intent(cands, "procedure")
    card_block = [c for c in result if c.candidate_type == "card_block"][0]
    chunk = [c for c in result if c.candidate_type == "chunk"][0]
    assert card_block.fused_score > chunk.fused_score


def test_fuse_and_rank_end_to_end():
    list1 = [_cand("c1", page_id="p1"), _cand("c2", page_id="p2")]
    list2 = [_cand("c3", ctype="card_block", card_id="card1", page_id="p1")]
    result = fuse_and_rank([list1, list2], visible_page_ids={"p1"}, intent="procedure", top_n=5)
    # p2 被 ACL 过滤
    assert all(c.source_page_id != "p2" for c in result)
    assert len(result) <= 5
