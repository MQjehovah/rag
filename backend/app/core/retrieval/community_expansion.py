"""Community Expansion（V4 Phase D-3）。

真正语义 Community：Page/Chunk/Evidence → 实体关系图 → 单层 Louvain 聚类
→ Community → 定位成员 Page/Chunk，扩展出可追溯的原始 Chunk 证据（RawChunkHit）。

不依赖 Card/KO，全程只读（不写 CanonicalEntity），有界（不加载整个权限域全部 Chunk）。

ACL：
- 用规范化 key（company/group:<名>/admin），None 与 __public__ 同属 company，严格隔离；
- 扩展执行时重新获取 ACL 快照，用最新可见 Page 集合过滤 seed 与扩展结果；
- 权限变化 fail closed：失权 seed 一律剔除，任何早退分支都返回明确的过滤结果
  （authorized_seed_hits 可能为空，绝不用旧结果回退）。

边界（真实生效）：
- max_communities 限制真实语义 Community 数；
- max_chunks_per_community 与 top_k 在每个 Community 内部真实生效；
- 去重、Page 配额、相似折叠。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core import access_control
from app.core.knowledge_compiler_v3.page_graph import build_page_communities
from app.core.retrieval.raw_retriever import (
    MAX_CHUNKS_PER_PAGE,
    RawChunkHit,
    _dedupe,
    _eligible_pages,
    _load_chunks,
    merge_rounds,
)
from app.core.retrieval.wiki_retriever import meaningful_tokenize

logger = logging.getLogger(__name__)

DEFAULT_MAX_COMMUNITIES = 3
DEFAULT_MAX_CHUNKS_PER_COMMUNITY = 3
DEFAULT_TOP_K = 20


@dataclass
class CommunityExpansionResult:
    """Community 扩展结果。"""
    hits: list[RawChunkHit] = field(default_factory=list)
    authorized_seed_hits: list[RawChunkHit] = field(default_factory=list)  # ACL 过滤后的 seed（明确，可为空）
    eligible_community_count: int = 0
    expanded_community_count: int = 0
    candidate_count_before: int = 0
    candidate_count_after: int = 0
    skipped_reason: str = ""
    degraded_reason: str = ""


class CommunityExpander:
    """Page/Chunk/Evidence 图驱动的语义社区扩展器（只读、有界）。"""

    def __init__(
        self,
        db: Session,
        *,
        max_communities: int = DEFAULT_MAX_COMMUNITIES,
        max_chunks_per_community: int = DEFAULT_MAX_CHUNKS_PER_COMMUNITY,
        top_k: int = DEFAULT_TOP_K,
    ):
        self.db = db
        self.max_communities = max_communities
        self.max_chunks_per_community = max_chunks_per_community
        self.top_k = top_k

    def expand(
        self,
        db: Session,
        current_user: dict,
        question: str,
        seed_hits: list[RawChunkHit],
    ) -> CommunityExpansionResult:
        # 重新获取 ACL 快照（执行前）
        visible = access_control.get_visible_page_ids(db, current_user)
        if not visible:
            return CommunityExpansionResult(
                authorized_seed_hits=[],  # 明确空，不回退旧结果
                candidate_count_before=len(seed_hits),
                skipped_reason="no_visible_pages",
            )

        eligible, _counts = _eligible_pages(db, visible)
        if not eligible:
            return CommunityExpansionResult(
                authorized_seed_hits=[],
                candidate_count_before=len(seed_hits),
                skipped_reason="no_eligible_pages",
            )

        # 用最新可见 Page 集合过滤 seed（权限变化 fail closed）
        authorized = [h for h in seed_hits if h.page_id in eligible]

        if not authorized:
            return CommunityExpansionResult(
                authorized_seed_hits=[],
                candidate_count_before=0,
                skipped_reason="no_authorized_seed",
            )

        seed_chunk_ids = {h.chunk_id for h in authorized}

        # 构建真实语义 Community（有界：从 seed 实体名定位，不加载整个权限域）
        communities = build_page_communities(
            db, set(eligible.keys()), seed_hits=authorized, top_k=self.top_k
        )

        if not communities:
            return CommunityExpansionResult(
                authorized_seed_hits=authorized,
                candidate_count_before=len(seed_chunk_ids),
                degraded_reason="no_communities",
            )

        seed_page_ids = {h.page_id for h in authorized}
        related = [
            c for c in communities
            if seed_page_ids & set(c.member_page_ids)
        ]
        # 稳定排序：按 fingerprint（acl_scope + entity_ids + member_page_ids）
        related.sort(key=lambda c: c.fingerprint)

        eligible_community_count = len(related)
        if not related:
            return CommunityExpansionResult(
                authorized_seed_hits=authorized,
                candidate_count_before=len(seed_chunk_ids),
                eligible_community_count=0,
                degraded_reason="no_related_community",
            )

        # 收集相关 Community 的成员页（有界：只这些页，不加载整个权限域）
        member_page_ids: set[str] = set()
        for c in related[: self.max_communities]:
            member_page_ids.update(c.member_page_ids)
        member_page_ids -= seed_page_ids  # 排除 seed 页（只扩展新文档）
        if not member_page_ids:
            return CommunityExpansionResult(
                authorized_seed_hits=authorized,
                candidate_count_before=len(seed_chunk_ids),
                eligible_community_count=eligible_community_count,
                degraded_reason="no_member_pages",
            )

        # 只加载相关 Community 成员页的 chunk（有界）
        member_chunks = _load_chunks(db, member_page_ids)
        keywords = self._keywords(question, authorized)

        added: list[RawChunkHit] = []
        expanded_count = 0
        added_ids: set[str] = set(seed_chunk_ids)

        for c in related[: self.max_communities]:
            # 该 Community 成员页的 chunk，排除 seed chunk
            comm_chunks = [
                ck for ck in member_chunks
                if ck.page_id in c.member_page_ids and ck.page_id not in seed_page_ids
            ]
            if not comm_chunks:
                continue
            scored = self._score_chunks(comm_chunks, keywords)
            scored.sort(key=lambda t: (-t[1], t[0].page_id, t[0].chunk_index))
            # top_k 真实生效：先取 top_k，再截断 max_chunks_per_community
            added_count = 0
            for chunk, _score in scored[: self.top_k]:
                if added_count >= self.max_chunks_per_community:
                    break
                if chunk.id in added_ids:
                    continue
                added.append(self._to_hit(chunk, eligible, keywords))
                added_ids.add(chunk.id)
                added_count += 1
            if added_count:
                expanded_count += 1

        if not added:
            return CommunityExpansionResult(
                authorized_seed_hits=authorized,
                candidate_count_before=len(seed_chunk_ids),
                eligible_community_count=eligible_community_count,
                expanded_community_count=0,
                degraded_reason="no_related_chunks",
            )

        # 去重 + Page 配额 + 相似折叠
        final_added = merge_rounds(added, [], max_per_page=MAX_CHUNKS_PER_PAGE)
        all_hits = merge_rounds(authorized, final_added, max_per_page=MAX_CHUNKS_PER_PAGE)

        return CommunityExpansionResult(
            hits=final_added,
            authorized_seed_hits=authorized,
            eligible_community_count=eligible_community_count,
            expanded_community_count=expanded_count,
            candidate_count_before=len(seed_chunk_ids),
            candidate_count_after=len({h.chunk_id for h in all_hits}),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _keywords(question: str, seed_hits: list[RawChunkHit]) -> list[str]:
        tokens: list[str] = _dedupe(meaningful_tokenize(question))
        for h in seed_hits:
            for t in h.matched_tokens:
                if t not in tokens:
                    tokens.append(t)
        return tokens

    @staticmethod
    def _score_chunks(chunks, keywords: list[str]) -> list[tuple]:
        kw = set(keywords)
        scored = []
        for c in chunks:
            overlap = len(kw & set(meaningful_tokenize(c.content)))
            if overlap > 0:
                scored.append((c, overlap))
        return scored

    @staticmethod
    def _to_hit(chunk, eligible: dict, keywords: list[str]) -> RawChunkHit:
        meta = eligible.get(chunk.page_id, {})
        content_tokens = set(meaningful_tokenize(chunk.content))
        matched = [t for t in keywords if t in content_tokens]
        return RawChunkHit(
            chunk_id=chunk.id,
            page_id=chunk.page_id,
            notebook_id=meta.get("notebook_id"),
            page_title=meta.get("title") or "",
            content=chunk.content,
            chunk_index=chunk.chunk_index,
            final_score=float(len(matched)),
            bm25_score=None,
            dense_score=None,
            rerank_score=None,
            source_type=meta.get("source_type"),
            source_url=meta.get("source_url"),
            retrieval_round=0,
            matched_tokens=matched,
            acl_diagnostic=meta.get("acl_diagnostic"),
        )
