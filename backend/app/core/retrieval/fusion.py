"""多路融合：RRF + ACL + MMR + Intent 权重（P4-BE-04/05/07/08，V3 计划 9.1）。

- RRF（Reciprocal Rank Fusion）：对每路候选按其在该路的排名做倒数融合，
  保留每路独立排名（不归一化各路分数，避免不同路的分数分布不可比）。
- ACL：在融合前按 visible_page_ids 过滤（召回阶段尽早应用，避免 Top-K 后
  过滤导致召回损失，P4-BE-05）。
- MMR：融合后去重，避免同一 Card 的多个相邻 Block 占满上下文（P4-BE-07）。
- Intent 权重：不同候选类型按 intent 加权（P4-BE-08）。

纯函数：rrf_fuse(ranked_lists, k) / apply_acl / mmr_diversify / weight_by_intent。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.retrieval.candidates import CandidateType, RetrievalCandidate

# RRF 常数（经典值 60）
RRF_K = 60

# 候选类型默认权重（intent 会覆盖）
_DEFAULT_TYPE_WEIGHTS = {
    CandidateType.CHUNK: 1.0,
    CandidateType.CARD_BLOCK: 1.2,
    CandidateType.EVIDENCE: 1.0,
    CandidateType.ENTITY: 0.8,
    CandidateType.COMMUNITY: 0.7,
}

# intent → 类型权重微调
_INTENT_TYPE_WEIGHTS = {
    "procedure": {CandidateType.CARD_BLOCK: 1.5, CandidateType.CHUNK: 1.0},
    "fact_lookup": {CandidateType.CHUNK: 1.3, CandidateType.EVIDENCE: 1.2},
    "diagnostic": {CandidateType.CARD_BLOCK: 1.4, CandidateType.CHUNK: 1.1},
    "decision": {CandidateType.CARD_BLOCK: 1.3, CandidateType.COMMUNITY: 1.0},
}


def rrf_fuse(ranked_lists: list[list[RetrievalCandidate]], k: int = RRF_K) -> dict[str, RetrievalCandidate]:
    """RRF 融合多路候选，返回 {candidate_id: candidate}（已算 fused_score）。

    每路候选按顺序（排名）赋分 1/(k + rank)，rank 从 0 起。
    同一候选在多个路出现 → 分数累加，并记录 sources。
    """
    fused: dict[str, RetrievalCandidate] = {}
    for candidates in ranked_lists:
        for rank, cand in enumerate(candidates):
            score = 1.0 / (k + rank)
            key = f"{cand.candidate_type}:{cand.candidate_id}"
            if key in fused:
                existing = fused[key]
                existing.fused_score += score
                existing.sources.extend(
                    s for s in cand.sources if s not in existing.sources
                )
            else:
                cand.fused_score = score
                fused[key] = cand
    return fused


def apply_acl(
    candidates: list[RetrievalCandidate],
    visible_page_ids: set[str] | None,
) -> list[RetrievalCandidate]:
    """ACL 过滤：None=不过滤（管理员），空集=全部过滤。"""
    if visible_page_ids is None:
        return candidates
    if not visible_page_ids:
        return []
    return [
        c for c in candidates
        if (
            c.source_page_id in visible_page_ids
            or (c.source_page_id is None and c.parent_card_id is None)
        )
    ]


def weight_by_intent(
    candidates: list[RetrievalCandidate],
    intent: str,
) -> list[RetrievalCandidate]:
    """按 intent 调整候选类型权重。"""
    weights = _INTENT_TYPE_WEIGHTS.get(intent, {})
    for cand in candidates:
        w = weights.get(cand.candidate_type, _DEFAULT_TYPE_WEIGHTS.get(cand.candidate_type, 1.0))
        cand.fused_score *= w
    return candidates


def _text_similarity(a: RetrievalCandidate, b: RetrievalCandidate) -> float:
    """简单 Jaccard 相似度（MMR 去重用，避免依赖 embedding）。"""
    ta = set((a.content or "").split())
    tb = set((b.content or "").split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def mmr_diversify(
    candidates: list[RetrievalCandidate],
    top_n: int = 10,
    lambda_: float = 0.7,
) -> list[RetrievalCandidate]:
    """MMR 去重：避免同一 Card 多个相邻 Block 占满上下文。

    lambda_ 越大越偏向相关性，越小越偏向多样性。
    相邻 Block 判定：同 parent_card_id 或高文本重叠。
    """
    if not candidates:
        return []
    # 按 fused_score 降序排序
    pool = sorted(candidates, key=lambda c: c.fused_score, reverse=True)
    selected: list[RetrievalCandidate] = []
    while pool and len(selected) < top_n:
        best_idx = 0
        best_score = -1.0
        for i, cand in enumerate(pool):
            relevance = cand.fused_score
            redundancy = 0.0
            for sel in selected:
                # 同 parent_card_id → 高冗余；否则看文本重叠
                if cand.parent_card_id and cand.parent_card_id == sel.parent_card_id:
                    redundancy = max(redundancy, 0.9)
                else:
                    redundancy = max(redundancy, _text_similarity(cand, sel))
            mmr = lambda_ * relevance - (1 - lambda_) * redundancy
            if mmr > best_score:
                best_score = mmr
                best_idx = i
        selected.append(pool.pop(best_idx))
    return selected


def apply_card_acl(
    candidates: list[RetrievalCandidate],
    visible_card_ids: set[str] | None,
) -> list[RetrievalCandidate]:
    """按 Published Card 可见性过滤。None=不过滤，空集=全部过滤。"""
    if visible_card_ids is None:
        return candidates
    if not visible_card_ids:
        return []
    return [c for c in candidates if c.parent_card_id in visible_card_ids]


def fuse_and_rank(
    ranked_lists: list[list[RetrievalCandidate]],
    *,
    visible_page_ids: set[str] | None = None,
    visible_card_ids: set[str] | None = None,
    intent: str | None = None,
    top_n: int = 10,
) -> list[RetrievalCandidate]:
    """完整融合管线：RRF → ACL → 权重 → MMR。"""
    fused_map = rrf_fuse(ranked_lists)
    candidates = list(fused_map.values())
    if visible_card_ids is not None:
        candidates = apply_card_acl(candidates, visible_card_ids)
    else:
        candidates = apply_acl(candidates, visible_page_ids)
    if intent:
        candidates = weight_by_intent(candidates, intent)
    candidates = sorted(candidates, key=lambda c: c.fused_score, reverse=True)
    return mmr_diversify(candidates, top_n=top_n)
