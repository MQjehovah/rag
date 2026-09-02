"""原始文档检索器（V4 Phase D-2 最终验收补漏）。

只检索 Page / PageChunk / Notebook，不检索 KnowledgeCard、KnowledgeCardBlock、
Card Claim 或旧 KO。

安全边界（fail closed）：
- 在读取正文、生成候选、Embedding、Reranker 之前先调用
  access_control.get_visible_page_ids()；
- 只允许可见 Page 的 PageChunk 进入候选集；
- 本地 Page：source_type 为空、Notebook 真实存在、权限域可确定才可检索；
- 远程 Page：source_type 非空、必须存在至少一条 state=="active" 的 SourceItem；
- 排除：退役/删除 Page、非 active SourceItem 的远程 Page、空白 Chunk、
  notebook_id 缺失或指向不存在 Notebook 的孤儿 Page。

复用现有能力：BM25、Dense cosine、RRF、Reranker、MMR。
降级：Embedding 不可用 → 仅 BM25；Reranker 不可用 → 融合分数；
LLM 不可用不影响检索（本模块不依赖 LLM）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace

import numpy as np
from sqlalchemy.orm import Session

from app.core import access_control
from app.core.retrieval.bm25 import BM25Index, _Doc, tokenize
from app.core.retrieval.candidates import CandidateType, RetrievalCandidate
from app.core.retrieval.fusion import mmr_diversify, rrf_fuse
from app.core.retrieval.rerank import IdentityReranker, Reranker
from app.core.retrieval.text_similarity import content_hash, content_similarity
from app.core.retrieval.wiki_retriever import meaningful_tokenize
from app.models.database import Notebook, Page, PageChunk, SourceConnection, SourceItem

logger = logging.getLogger(__name__)

# 同一 Page 最多保留的 Chunk 数（防止单一 Page 垄断结果）
MAX_CHUNKS_PER_PAGE = 3


def _dedupe(tokens: list[str]) -> list[str]:
    """保序去重。"""
    return list(dict.fromkeys(tokens))


@dataclass
class RawChunkHit:
    """单个原始文档 Chunk 命中。"""
    chunk_id: str
    page_id: str
    notebook_id: str | None
    page_title: str
    content: str
    chunk_index: int
    final_score: float
    bm25_score: float | None
    dense_score: float | None
    rerank_score: float | None
    source_type: str | None
    source_url: str | None
    retrieval_round: int
    matched_tokens: list[str] = field(default_factory=list)
    acl_diagnostic: str | None = None  # 安全枚举：company / group / admin


@dataclass
class RawRetrievalResult:
    """RawDocumentRetriever 单轮返回结构。"""
    hits: list[RawChunkHit] = field(default_factory=list)
    query: str = ""
    retrieval_round: int = 1
    visible_page_count: int = 0
    eligible_page_count: int = 0
    eligible_chunk_count: int = 0
    filtered_out_page_count: int = 0
    filtered_reason_counts: dict[str, int] = field(default_factory=dict)
    bm25_used: bool = False
    dense_used: bool = False
    reranker_used: bool = False
    degraded: list[str] = field(default_factory=list)
    # 权限快照（仅内部比较用，不进入 trace，不泄露 Page ID）
    visible_page_ids: set[str] = field(default_factory=set)
    acl_snapshot_changed: bool = False


def _active_source_item_pages(
    db: Session,
    page_ids: set[str],
    page_source_types: dict[str, str],
) -> set[str]:
    """返回「存在与 Page.source_type 相同 Connector 的 active SourceItem」的 page_id。

    必须 JOIN SourceConnection，且 SourceConnection.connector_key == Page.source_type。
    错误 Connector 的 active SourceItem 不得使 Page 可检索。仅 active 算有效；
    NULL/deleted/error/skipped 均不算。同一 Page 多条 SourceItem 时，只要一条
    active 且 Connector 匹配即有效，历史 deleted 项不屏蔽当前 active。
    """
    if not page_ids:
        return set()
    rows = (
        db.query(SourceItem.page_id, SourceConnection.connector_key)
        .join(SourceConnection, SourceConnection.id == SourceItem.connection_id)
        .filter(
            SourceItem.page_id.in_(page_ids),
            SourceItem.state == "active",
        )
        .all()
    )
    active: set[str] = set()
    for page_id, connector_key in rows:
        if page_id and page_source_types.get(page_id) == connector_key:
            active.add(page_id)
    return active


def _eligible_pages(
    db: Session, visible_page_ids: set[str]
) -> tuple[dict[str, dict], dict[str, int]]:
    """过滤出可进入候选集的 Page 元数据。

    返回 (page_id -> meta, 过滤原因计数)。规则（fail closed）：
    - 远程 Page（source_type 非空）必须存在同 Connector 的 active SourceItem；
    - notebook_id 缺失或指向不存在 Notebook → 排除（含管理员）；
    - 本地 Page（source_type 空）不检查 SourceItem。
    """
    if not visible_page_ids:
        return {}, {}
    pages = db.query(Page).filter(Page.id.in_(visible_page_ids)).all()
    page_source_types = {p.id: p.source_type for p in pages}
    active_pages = _active_source_item_pages(db, visible_page_ids, page_source_types)

    notebook_ids = {p.notebook_id for p in pages if p.notebook_id}
    notebooks = {
        n.id: n for n in db.query(Notebook).filter(Notebook.id.in_(notebook_ids)).all()
    }

    eligible: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for p in pages:
        # 远程数据源 Page：必须有 active SourceItem
        if p.source_type:
            if p.id not in active_pages:
                counts["retired_remote_page"] = counts.get("retired_remote_page", 0) + 1
                continue
        # 无法确定权限域：notebook_id 缺失
        if not p.notebook_id:
            counts["missing_notebook"] = counts.get("missing_notebook", 0) + 1
            continue
        nb = notebooks.get(p.notebook_id)
        if nb is None:
            counts["notebook_not_found"] = counts.get("notebook_not_found", 0) + 1
            continue

        scope = access_control.scope_from_group_id(nb.group_id)
        eligible[p.id] = {
            "title": p.title,
            "notebook_id": p.notebook_id,
            "source_type": p.source_type,
            "source_url": p.source_url or p.source_path or (
                f"{p.source_type}:{p.source_id}" if p.source_type else None
            ),
            "acl_diagnostic": scope.kind,  # company / group / admin
        }
    return eligible, counts


def _load_chunks(db: Session, page_ids: set[str]) -> list[PageChunk]:
    """只加载可见/有效 Page 的非空 Chunk（排除空白/纯换行）。"""
    if not page_ids:
        return []
    chunks = (
        db.query(PageChunk)
        .filter(PageChunk.page_id.in_(page_ids))
        .order_by(PageChunk.page_id, PageChunk.chunk_index)
        .all()
    )
    return [c for c in chunks if (c.content or "").strip()]


def _bm25_candidates(
    chunks: list[PageChunk],
    question: str,
    *,
    titles: dict[str, str],
    top_k: int,
) -> list[RetrievalCandidate]:
    """ACL 范围内 Chunk 级 BM25。"""
    docs = [
        _Doc(
            doc_id=c.id,
            content=c.content,
            tokens=tokenize(f"{((titles.get(c.page_id, '') or '') + ' ') * 4}{c.content}"),
            candidate_type=CandidateType.CHUNK,
            source_page_id=c.page_id,
        )
        for c in chunks
    ]
    index = BM25Index()
    index.build(docs)
    results = index.search(question, top_k)
    return [
        RetrievalCandidate(
            candidate_type=CandidateType.CHUNK,
            candidate_id=doc.doc_id,
            content=doc.content,
            source_page_id=doc.source_page_id,
            sparse_score=round(score, 4),
        ).with_source("bm25")
        for doc, score in results
    ]


def _dense_candidates(
    chunks: list[PageChunk],
    query_embedding: list[float],
    *,
    top_k: int,
) -> tuple[list[RetrievalCandidate], str | None]:
    """ACL 范围内 Chunk 级 Dense（cosine）。

    返回 (候选, 降级原因)。降级原因（None=成功）：
    - embedding_empty：查询向量全零；
    - no_indexed_vectors：所有 Chunk 无有效向量；
    - embedding_dimension_mismatch：有向量但维度均不匹配。
    """
    query = np.asarray(query_embedding, dtype=float)
    query_norm = np.linalg.norm(query)
    if query_norm == 0:
        return [], "embedding_empty"

    ranked: list[tuple[float, PageChunk]] = []
    saw_dim_mismatch = False
    for c in chunks:
        if not c.embedding:
            continue
        try:
            vector = np.asarray(json.loads(c.embedding), dtype=float)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        norm = np.linalg.norm(vector)
        if norm == 0:
            continue
        if vector.shape != query.shape:
            saw_dim_mismatch = True
            continue
        score = float(np.dot(query, vector) / (query_norm * norm))
        ranked.append((score, c))

    if not ranked:
        if saw_dim_mismatch:
            return [], "embedding_dimension_mismatch"
        return [], "no_indexed_vectors"

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [
        RetrievalCandidate(
            candidate_type=CandidateType.CHUNK,
            candidate_id=c.id,
            content=c.content,
            source_page_id=c.page_id,
            dense_score=round(score, 6),
        ).with_source("dense")
        for score, c in ranked[:top_k]
    ], None


def _cap_per_page(
    candidates: list[RetrievalCandidate],
    max_per_page: int = MAX_CHUNKS_PER_PAGE,
) -> list[RetrievalCandidate]:
    """限制同一 Page 的 Chunk 数，防止单一 Page 垄断结果。"""
    result: list[RetrievalCandidate] = []
    per_page: dict[str, int] = {}
    for cand in candidates:
        pid = cand.source_page_id or ""
        if per_page.get(pid, 0) >= max_per_page:
            continue
        per_page[pid] = per_page.get(pid, 0) + 1
        result.append(cand)
    return result


class RawDocumentRetriever:
    """原始文档检索器（一轮召回 + 融合 + 重排 + 去冗余）。"""

    name = "raw_document"

    def __init__(
        self,
        db: Session,
        *,
        reranker: Reranker | None = None,
        top_k: int = 20,
    ):
        self.db = db
        self.reranker = reranker or IdentityReranker()
        self.top_k = top_k

    def retrieve(
        self,
        db: Session,
        question: str,
        current_user: dict,
        *,
        query_embedding: list[float] | None = None,
        retrieval_round: int = 1,
    ) -> RawRetrievalResult:
        result = RawRetrievalResult(
            query=question,
            retrieval_round=retrieval_round,
        )

        # 1. ACL 先行
        visible_page_ids = access_control.get_visible_page_ids(db, current_user)
        result.visible_page_count = len(visible_page_ids)
        result.visible_page_ids = set(visible_page_ids)  # 权限快照（内部比较用）
        if not visible_page_ids:
            return result

        # 2. 有效 Page（fail closed）
        eligible, counts = _eligible_pages(db, visible_page_ids)
        result.eligible_page_count = len(eligible)
        result.filtered_reason_counts = counts
        result.filtered_out_page_count = sum(counts.values())
        if not eligible:
            return result

        # 3. 只加载有效 Page 的非空 Chunk
        chunks = _load_chunks(db, set(eligible.keys()))
        result.eligible_chunk_count = len(chunks)
        if not chunks:
            return result

        titles = {pid: meta["title"] for pid, meta in eligible.items()}

        # 4. BM25（始终可用）
        bm25_cands = _bm25_candidates(chunks, question, titles=titles, top_k=self.top_k)
        result.bm25_used = bool(bm25_cands)

        # 5. Dense（真实判断：有查询向量 + 兼容 Chunk 向量才算 used）
        dense_cands: list[RetrievalCandidate] = []
        if query_embedding is None:
            result.degraded.append("embedding_unavailable")
        else:
            dense_cands, dense_reason = _dense_candidates(
                chunks, query_embedding, top_k=self.top_k
            )
            if dense_reason is not None:
                result.degraded.append(dense_reason)
            else:
                result.dense_used = True

        # 6. RRF 融合
        fused_map = rrf_fuse([bm25_cands, dense_cands])
        fused = list(fused_map.values())
        if not fused:
            return result

        # 7. Reranker（真实判断 used，失败保留 Fusion 原排序）
        reranked, reranker_used, reranker_degraded = self._apply_reranker(question, fused)
        result.reranker_used = reranker_used
        if not reranker_used:
            # 清除任何被写过的 rerank_score，保证 used=False 时 hit.rerank_score 全为 None
            for cand in reranked:
                cand.rerank_score = None
            result.degraded.append(reranker_degraded)

        # 8. 以最终分驱动 MMR + Page 配额
        for cand in reranked:
            if cand.rerank_score is not None:
                cand.fused_score = cand.rerank_score
        diversified = mmr_diversify(reranked, top_n=self.top_k)
        final = _cap_per_page(diversified)

        # 9. 构造 RawChunkHit
        query_tokens = _dedupe(meaningful_tokenize(question))
        for cand in final:
            meta = eligible.get(cand.source_page_id or "", {})
            content_token_set = set(meaningful_tokenize(cand.content))
            matched = [t for t in query_tokens if t in content_token_set]
            result.hits.append(RawChunkHit(
                chunk_id=cand.candidate_id,
                page_id=cand.source_page_id or "",
                notebook_id=meta.get("notebook_id"),
                page_title=meta.get("title") or "",
                content=cand.content,
                chunk_index=_chunk_index_for(chunks, cand.candidate_id),
                final_score=round(cand.fused_score or 0.0, 6),
                bm25_score=round(cand.sparse_score, 4) if cand.sparse_score is not None else None,
                dense_score=round(cand.dense_score, 6) if cand.dense_score is not None else None,
                rerank_score=round(cand.rerank_score, 6) if cand.rerank_score is not None else None,
                source_type=meta.get("source_type"),
                source_url=meta.get("source_url"),
                retrieval_round=retrieval_round,
                matched_tokens=matched,
                acl_diagnostic=meta.get("acl_diagnostic"),
            ))

        # 稳定排序：final_score 降序 → page_id → chunk_index
        result.hits.sort(key=lambda h: (-h.final_score, h.page_id, h.chunk_index))
        return result

    def _apply_reranker(
        self, question: str, fused: list[RetrievalCandidate]
    ) -> tuple[list[RetrievalCandidate], bool, str]:
        """真实重排：只有 rerank_with_status().used 且结果完整才视为成功。

        返回 (ranked, used, degraded)：
        - IdentityReranker：按 fused_score 排序，used=False，reranker_unavailable；
        - 无 rerank_with_status：fail closed，不执行（不先执行再宣称失败）；
        - rerank_with_status 抛异常：捕获降级，used=False；
        - 结果不完整（partial）：used=False，reranker_invalid_response；
        - 成功：按 rerank_score 排序，used=True。
        """
        reranker = self.reranker
        if isinstance(reranker, IdentityReranker):
            return self._fusion_sorted(fused), False, "reranker_unavailable"

        if not hasattr(reranker, "rerank_with_status"):
            # 无状态协议：fail closed，不执行，直接保留 Fusion 排序
            return self._fusion_sorted(fused), False, "reranker_unavailable"

        try:
            status = reranker.rerank_with_status(question, fused)
        except Exception:  # noqa: BLE001
            return self._fusion_sorted(fused), False, "reranker_unavailable"

        used = bool(getattr(status, "used", False))
        if not used:
            return self._fusion_sorted(fused), False, _reranker_degraded(status)

        # used=True 但必须验证结果完整性（不完整 index 不能伪造其余候选为已重排）
        results = getattr(status, "results", None) or []
        indices = {int(item["index"]) for item in results if "index" in item}
        if len(indices) != len(fused) or indices != set(range(len(fused))):
            return self._fusion_sorted(fused), False, "reranker_invalid_response"

        ranked = sorted(fused, key=lambda c: c.rerank_score or 0.0, reverse=True)
        return ranked, True, ""

    @staticmethod
    def _fusion_sorted(candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
        """按 fused_score 降序，不写 rerank_score。"""
        return sorted(candidates, key=lambda c: c.fused_score, reverse=True)


def _chunk_index_for(chunks: list[PageChunk], chunk_id: str) -> int:
    for c in chunks:
        if c.id == chunk_id:
            return c.chunk_index
    return 0


def _reranker_degraded(status) -> str:
    """把 RerankResult.error_code 映射为安全错误枚举（不返回响应正文/密钥）。"""
    code = getattr(status, "error_code", None) or ""
    if code == "AUTH_FAILED":
        return "reranker_unauthorized"
    if code == "TIMEOUT":
        return "reranker_timeout"
    if code in ("INVALID_RESPONSE", "BAD_REQUEST", "MODEL_NOT_LOADED", "MODEL_UID_INVALID"):
        return "reranker_invalid_response"
    return "reranker_unavailable"


# ---------------------------------------------------------------------------
# 两轮合并（不修改原始对象）
# ---------------------------------------------------------------------------

def merge_rounds(
    round1: list[RawChunkHit],
    round2: list[RawChunkHit],
    *,
    max_per_page: int = MAX_CHUNKS_PER_PAGE,
) -> list[RawChunkHit]:
    """合并两轮结果：chunk_id 去重、内容 hash 去重、相邻折叠、Page 配额、稳定排序。

    全程不原地修改 round1/round2 的 RawChunkHit 对象。
    """
    # 1. chunk_id 去重（保留更高分，replace 复制避免别名）
    by_id: dict[str, RawChunkHit] = {}
    for h in round1 + round2:
        existing = by_id.get(h.chunk_id)
        if existing is None or h.final_score > existing.final_score:
            by_id[h.chunk_id] = replace(h)
    hits = list(by_id.values())

    # 2. 相同内容 hash 去重（NFKC + strip + 空白归一化）
    by_hash: dict[str, RawChunkHit] = {}
    for h in hits:
        key = content_hash(h.content)
        existing = by_hash.get(key)
        if existing is None or h.final_score > existing.final_score:
            by_hash[key] = h
    hits = list(by_hash.values())

    # 3. 同一 Page 相邻高度重复 Chunk 折叠（保留真正更高分者）
    hits = _fold_adjacent_duplicates(hits)

    # 4. 同一 Page 不垄断
    hits = _cap_hits_per_page(hits, max_per_page=max_per_page)

    # 5. 稳定排序
    hits.sort(key=lambda h: (-(h.final_score or 0.0), h.page_id, h.chunk_index))
    return hits


def _fold_adjacent_duplicates(hits: list[RawChunkHit]) -> list[RawChunkHit]:
    """同 Page 且 chunk_index 相邻且内容高度相似 → 折叠为一个，保留更高分者。

    全程 replace 复制，不修改传入对象。
    """
    by_page: dict[str, list[RawChunkHit]] = {}
    for h in hits:
        by_page.setdefault(h.page_id, []).append(h)
    result: list[RawChunkHit] = []
    for _page_id, members in by_page.items():
        members = sorted(members, key=lambda h: h.chunk_index)
        kept: list[RawChunkHit] = []
        for h in members:
            folded_into: RawChunkHit | None = None
            for prev in kept:
                if (
                    abs(prev.chunk_index - h.chunk_index) <= 1
                    and content_similarity(prev.content, h.content) >= 0.8
                ):
                    folded_into = prev
                    break
            if folded_into is not None:
                # 保留真正更高分的命中（完整分数，不降半）
                if h.final_score > folded_into.final_score:
                    kept[kept.index(folded_into)] = replace(h)
            else:
                kept.append(replace(h))
        result.extend(kept)
    return result


def _cap_hits_per_page(hits: list[RawChunkHit], max_per_page: int) -> list[RawChunkHit]:
    result: list[RawChunkHit] = []
    per_page: dict[str, int] = {}
    ordered = sorted(hits, key=lambda h: (-(h.final_score or 0.0), h.page_id, h.chunk_index))
    for h in ordered:
        pid = h.page_id
        if per_page.get(pid, 0) >= max_per_page:
            continue
        per_page[pid] = per_page.get(pid, 0) + 1
        result.append(h)
    return result
