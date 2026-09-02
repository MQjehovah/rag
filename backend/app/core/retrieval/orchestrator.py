"""Wiki 优先分层检索编排器（V4 Phase D-2 封板）。

调用链：
    POST 用户问题
      → ACL（WikiRetriever 内部先过滤）
      → Wiki Retriever
      → Wiki SufficiencyJudge
          ├─ sufficient：mode=wiki_hit（不调用原始文档检索）
          └─ insufficient：
              → Raw Retrieval Round 1
              → Raw SufficiencyJudge
                  ├─ sufficient：mode=raw_hit
                  └─ insufficient：
                      → 生成补充查询（语义变化，非重复加权）
                      → 可补充时 Raw Retrieval Round 2
                      → 合并、去重、重新排序
                      → sufficient：mode=raw_hit（返回最终合并结果）
                      → 仍不足/无法补充：mode=need_community_expansion

本阶段最多两轮，严禁第三轮。不接入正式 Chat API，不实现 Community 扩展。

生产异步入口 retrieve_async：Round 1/2 在同一事件循环、同一异步调用链执行，
Embedding 用 EmbeddingService.encode_with_status，finally 中 close。
同步 retrieve 仅用于纯 BM25 / 同步 Mock / 确定性单元测试。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from app.config import settings
from app.core.retrieval.community_expansion import (
    CommunityExpander,
    CommunityExpansionResult,
)
from app.core.retrieval.query_supplement import build_supplemental_query
from app.core.retrieval.raw_retriever import (
    RawChunkHit,
    RawDocumentRetriever,
    RawRetrievalResult,
    merge_rounds,
)
from app.core.retrieval.raw_sufficiency_judge import (
    RawSufficiencyVerdict,
    judge_raw_sufficiency,
)
from app.core.retrieval.sufficiency_judge import SufficiencyJudge, SufficiencyVerdict
from app.core.retrieval.wiki_retriever import WikiRetriever, WikiRetrievalResult

logger = logging.getLogger(__name__)

MAX_RAW_ROUNDS = 2

# 降级原因稳定排序（Phase E 前端展示需要确定性顺序）
_DEGRADED_ORDER = [
    "embedding_unavailable",
    "embedding_timeout",
    "embedding_unauthorized",
    "embedding_empty",
    "no_indexed_vectors",
    "embedding_dimension_mismatch",
    "reranker_unavailable",
    "reranker_timeout",
    "reranker_unauthorized",
    "reranker_invalid_response",
]


@dataclass
class OrchestrationOutcome:
    """编排结果。"""
    mode: str  # wiki_hit / raw_hit / need_community_expansion
    wiki_results: WikiRetrievalResult | None = None
    verdict: SufficiencyVerdict | None = None
    raw_results: RawRetrievalResult | None = None          # 最终合并结果（供回答/引用）
    raw_round_results: list[RawRetrievalResult] = field(default_factory=list)  # 每轮原始结果
    raw_verdict: RawSufficiencyVerdict | None = None
    raw_calls: int = 0
    trace: dict = field(default_factory=dict)


def _stable_degraded(deg_list: list[str]) -> list[str]:
    """降级原因去重 + 稳定排序。"""
    seen: set[str] = set()
    out: list[str] = []
    for d in deg_list:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    order = {name: i for i, name in enumerate(_DEGRADED_ORDER)}
    out.sort(key=lambda x: order.get(x, len(order)))
    return out


def _embedding_degraded(emb_result) -> list[str]:
    """把 EmbeddingBatchResult 映射为安全错误枚举（不返回响应正文/密钥）。"""
    if emb_result is None:
        return ["embedding_unavailable"]
    if getattr(emb_result, "used", False) and getattr(emb_result, "embeddings", None):
        return []
    code = getattr(emb_result, "error_code", None) or ""
    if code == "AUTH_FAILED":
        return ["embedding_unauthorized"]
    if code == "TIMEOUT":
        return ["embedding_timeout"]
    if code == "INVALID_RESPONSE":
        return ["embedding_empty"]
    return ["embedding_unavailable"]


class DefaultAsyncEmbedder:
    """生产异步 Embedder：惰性创建 EmbeddingService，支持 close。

    每次 close 后再次调用会重新创建（避免跨事件循环复用 AsyncClient）。
    配置缺失时直接返回 used=False（降级 BM25）。
    """

    def __init__(self, *, configured: bool, service_factory=None):
        from app.core.embedding.results import EmbeddingBatchResult
        from app.core.rag import EmbeddingService

        self._configured = configured
        self._service_factory = service_factory or EmbeddingService
        self._service = None
        self._EmbeddingBatchResult = EmbeddingBatchResult

    async def __call__(self, query: str):
        if not self._configured:
            return self._EmbeddingBatchResult(
                used=False, degraded=True, error_code="NETWORK_UNREACHABLE",
            )
        if self._service is None:
            self._service = self._service_factory()
        return await self._service.encode_with_status(query)

    async def close(self) -> None:
        if self._service is not None:
            await self._service.close()
            self._service = None


class RetrievalOrchestrator:
    """分层检索编排器（Phase D-2 仅 Wiki 层 + 原始文档最多两轮）。"""

    def __init__(
        self,
        db: Session,
        *,
        wiki_retriever: WikiRetriever | None = None,
        judge: SufficiencyJudge | None = None,
        raw_retriever: RawDocumentRetriever | None = None,
        raw_judge: Callable[..., RawSufficiencyVerdict] | None = None,
        supplementer: Callable[..., Any] | None = None,
        embedder: Callable[[str], list[float] | None] | None = None,
        async_embedder: Callable[[str], Awaitable[Any]] | None = None,
        community_expander: CommunityExpander | None = None,
    ):
        self.db = db
        self.wiki_retriever = wiki_retriever or WikiRetriever(db)
        self.judge = judge or SufficiencyJudge()
        self.raw_retriever = raw_retriever or RawDocumentRetriever(db)
        self.raw_judge = raw_judge or judge_raw_sufficiency
        self.supplementer = supplementer or build_supplemental_query
        self.embedder = embedder                    # 同步（纯 BM25 / 同步测试）
        self.async_embedder = async_embedder        # 异步（生产）
        self.community_expander = community_expander if community_expander is not None else CommunityExpander(db)

    # ------------------------------------------------------------------
    # 同步入口：纯 BM25 / 同步 Mock / 确定性单元测试
    # ------------------------------------------------------------------

    def retrieve(self, question: str, current_user: dict) -> OrchestrationOutcome:
        trace: dict[str, Any] = {"layers": [], "mode": None}

        wiki_results, verdict = self._wiki_step(question, current_user, trace)
        if verdict.sufficient:
            trace["mode"] = "wiki_hit"
            trace["community"] = {
                "community_attempted": False,
                "community_used": False,
                "skipped_reason": "wiki_sufficient",
            }
            return OrchestrationOutcome(
                mode="wiki_hit", wiki_results=wiki_results, verdict=verdict,
                raw_calls=0, trace=trace,
            )

        raw_rounds: list[dict] = []
        round1 = self._run_raw_round(question, current_user, round_number=1)
        raw_rounds.append(round1)
        merged_hits = list(round1["hits"])
        raw_verdict = self.raw_judge(merged_hits, question, retrieval_round=1)

        acl_snapshot_changed = False
        if not raw_verdict.sufficient:
            supplement = self.supplementer(
                question,
                wiki_missing_aspects=verdict.missing_aspects,
                round1_missing_aspects=raw_verdict.missing_tokens,
                round1_matched_tokens=_flatten_matched(round1["hits"]),
            )
            supplement_query = getattr(supplement, "supplemental_query", None) or supplement
            can_supplement = bool(getattr(supplement, "can_supplement", True))
            if can_supplement and supplement_query and supplement_query != question:
                round2 = self._run_raw_round(supplement_query, current_user, round_number=2)
                raw_rounds.append(round2)
                merged_hits = merge_rounds(round1["hits"], round2["hits"])
                acl_snapshot_changed = (
                    round1["result"].visible_page_ids != round2["result"].visible_page_ids
                )
                if acl_snapshot_changed:
                    allowed = round1["result"].visible_page_ids & round2["result"].visible_page_ids
                    merged_hits = [h for h in merged_hits if h.page_id in allowed]
                raw_verdict = self.raw_judge(merged_hits, question, retrieval_round=2)

        merged_hits, raw_verdict, community_trace = self._community_step(
            question, current_user, merged_hits, raw_verdict
        )
        return self._finalize(
            question, wiki_results, verdict, raw_rounds, merged_hits,
            raw_verdict, acl_snapshot_changed, trace, community_trace,
        )

    # ------------------------------------------------------------------
    # 异步入口：生产（FastAPI），Embedding 真实接线，同一事件循环
    # ------------------------------------------------------------------

    async def retrieve_async(self, question: str, current_user: dict) -> OrchestrationOutcome:
        try:
            return await self._retrieve_async_impl(question, current_user)
        finally:
            closer = getattr(self.async_embedder, "close", None)
            if closer is not None:
                await closer()

    async def _retrieve_async_impl(self, question: str, current_user: dict) -> OrchestrationOutcome:
        trace: dict[str, Any] = {"layers": [], "mode": None}

        wiki_results, verdict = self._wiki_step(question, current_user, trace)
        if verdict.sufficient:
            trace["mode"] = "wiki_hit"
            trace["community"] = {
                "community_attempted": False,
                "community_used": False,
                "skipped_reason": "wiki_sufficient",
            }
            return OrchestrationOutcome(
                mode="wiki_hit", wiki_results=wiki_results, verdict=verdict,
                raw_calls=0, trace=trace,
            )

        raw_rounds: list[dict] = []
        round1 = await self._run_raw_round_async(question, current_user, round_number=1)
        raw_rounds.append(round1)
        merged_hits = list(round1["hits"])
        raw_verdict = self.raw_judge(merged_hits, question, retrieval_round=1)

        acl_snapshot_changed = False
        if not raw_verdict.sufficient:
            supplement = self.supplementer(
                question,
                wiki_missing_aspects=verdict.missing_aspects,
                round1_missing_aspects=raw_verdict.missing_tokens,
                round1_matched_tokens=_flatten_matched(round1["hits"]),
            )
            supplement_query = getattr(supplement, "supplemental_query", None) or supplement
            can_supplement = bool(getattr(supplement, "can_supplement", True))
            if can_supplement and supplement_query and supplement_query != question:
                round2 = await self._run_raw_round_async(supplement_query, current_user, round_number=2)
                raw_rounds.append(round2)
                merged_hits = merge_rounds(round1["hits"], round2["hits"])
                acl_snapshot_changed = (
                    round1["result"].visible_page_ids != round2["result"].visible_page_ids
                )
                if acl_snapshot_changed:
                    allowed = round1["result"].visible_page_ids & round2["result"].visible_page_ids
                    merged_hits = [h for h in merged_hits if h.page_id in allowed]
                raw_verdict = self.raw_judge(merged_hits, question, retrieval_round=2)

        merged_hits, raw_verdict, community_trace = self._community_step(
            question, current_user, merged_hits, raw_verdict
        )
        return self._finalize(
            question, wiki_results, verdict, raw_rounds, merged_hits,
            raw_verdict, acl_snapshot_changed, trace, community_trace,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _wiki_step(self, question, current_user, trace):
        wiki_results = self.wiki_retriever.retrieve(self.db, question, current_user)
        trace["layers"].append({
            "layer": "wiki",
            "visible_wiki_count": wiki_results.visible_wiki_count,
            "published_wiki_count": wiki_results.published_wiki_count,
            "hit_count": len(wiki_results.hits),
        })
        verdict = self.judge.judge(wiki_results, question)
        trace["wiki_verdict"] = {
            "sufficient": verdict.sufficient,
            "reason": verdict.reason,
            "confidence": verdict.confidence,
            "missing_aspects": verdict.missing_aspects,
        }
        return wiki_results, verdict

    def _run_raw_round(self, query, current_user, *, round_number) -> dict:
        """同步 raw 轮：纯 BM25 或同步 embedder（不调 asyncio.run）。"""
        query_embedding: list[float] | None = None
        if self.embedder is not None:
            try:
                query_embedding = self.embedder(query)
            except Exception:  # noqa: BLE001
                query_embedding = None
        result = self.raw_retriever.retrieve(
            self.db, query, current_user,
            query_embedding=query_embedding, retrieval_round=round_number,
        )
        return self._round_dict(round_number, query, result)

    async def _run_raw_round_async(self, query, current_user, *, round_number) -> dict:
        """异步 raw 轮：用 async_embedder 真实获取 query embedding。"""
        query_embedding: list[float] | None = None
        embedding_degraded: list[str] = []
        if self.async_embedder is not None:
            try:
                emb_result = await self.async_embedder(query)
            except Exception:  # noqa: BLE001
                emb_result = None
            query_embedding, embedding_degraded = self._embedding_to_vector(emb_result)

        result = self.raw_retriever.retrieve(
            self.db, query, current_user,
            query_embedding=query_embedding, retrieval_round=round_number,
        )
        if embedding_degraded:
            # 用 embedding 层具体原因替换 RawDocumentRetriever 的笼统 embedding_unavailable
            result.degraded = embedding_degraded + [
                d for d in result.degraded if d != "embedding_unavailable"
            ]
        return self._round_dict(round_number, query, result)

    @staticmethod
    def _embedding_to_vector(emb_result) -> tuple[list[float] | None, list[str]]:
        degraded = _embedding_degraded(emb_result)
        if degraded:
            return None, degraded
        return getattr(emb_result, "embeddings", [None])[0], []

    @staticmethod
    def _round_dict(round_number: int, query: str, result: RawRetrievalResult) -> dict:
        retrievers = ["bm25"]
        if result.dense_used:
            retrievers.append("dense")
        if result.reranker_used:
            retrievers.append("reranker")
        return {
            "round": round_number,
            "query": query,
            "candidate_count": len(result.hits),
            "retrievers": retrievers,
            "degraded": list(result.degraded),
            "visible_page_count": result.visible_page_count,
            "eligible_page_count": result.eligible_page_count,
            "hits": result.hits,
            "result": result,
        }

    def _community_step(
        self,
        question,
        current_user,
        merged_hits,
        raw_verdict,
    ) -> tuple[list[RawChunkHit], RawSufficiencyVerdict, dict]:
        """Raw 两轮仍不足后执行 Community Expansion，扩展后重新判断充分性。

        不产生第三轮 Raw Retrieval；扩展返回的证据仍是原始 Chunk。
        Community 不可用/无匹配/异常时安全降级，保留原 Raw 结果。
        """
        if raw_verdict.sufficient:
            return merged_hits, raw_verdict, {
                "community_attempted": False,
                "community_used": False,
                "skipped_reason": "raw_sufficient",
            }

        try:
            expansion: CommunityExpansionResult = self.community_expander.expand(
                self.db, current_user, question, merged_hits
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("community expansion failed: %s", exc)
            return merged_hits, raw_verdict, {
                "community_attempted": True,
                "community_used": False,
                "degraded_reason": "community_expansion_error",
            }

        base_trace = {
            "community_attempted": True,
            "community_used": bool(expansion.hits),
            "eligible_community_count": expansion.eligible_community_count,
            "expanded_community_count": expansion.expanded_community_count,
            "candidate_count_before": expansion.candidate_count_before,
            "candidate_count_after": expansion.candidate_count_after,
            "skipped_reason": expansion.skipped_reason,
            "degraded_reason": expansion.degraded_reason,
        }

        # ACL 过滤后的 seed 是明确结果，绝不用旧 merged_hits 回退。
        # 全部失权 → authorized_seed_hits 为空 → 最终结果为空。
        authorized_seed = expansion.authorized_seed_hits
        if not expansion.hits:
            return authorized_seed, raw_verdict, base_trace

        new_merged = merge_rounds(authorized_seed, expansion.hits)
        new_verdict = self.raw_judge(new_merged, question, retrieval_round=2)
        return new_merged, new_verdict, base_trace

    def _finalize(
        self,
        question,
        wiki_results,
        verdict,
        raw_rounds,
        merged_hits,
        raw_verdict,
        acl_snapshot_changed,
        trace,
        community_trace,
    ) -> OrchestrationOutcome:
        final_raw_result = self._build_final_raw_result(
            [r["result"] for r in raw_rounds], merged_hits, question,
            acl_snapshot_changed=acl_snapshot_changed,
        )
        round2_skipped_reason = ""
        if len(raw_rounds) < 2 and not raw_verdict.sufficient:
            round2_skipped_reason = "no_semantic_supplement"

        raw_trace = {
            "calls": len(raw_rounds),
            "rounds": [
                {
                    "round": r["round"],
                    "query": r["query"],
                    "candidate_count": r["candidate_count"],
                    "retrievers": r["retrievers"],
                    "degraded": r["degraded"],
                    "visible_page_count": r["visible_page_count"],
                    "eligible_page_count": r["eligible_page_count"],
                }
                for r in raw_rounds
            ],
            "merge_before": sum(r["candidate_count"] for r in raw_rounds),
            "merge_after": len(merged_hits),
            "round2_skipped_reason": round2_skipped_reason,
            "acl_snapshot_changed": acl_snapshot_changed,
            "filtered_reason_counts": final_raw_result.filtered_reason_counts if final_raw_result else {},
            "raw_verdict": {
                "sufficient": raw_verdict.sufficient,
                "reason": raw_verdict.reason,
                "confidence": raw_verdict.confidence,
                "matched_tokens": raw_verdict.matched_tokens,
                "missing_tokens": raw_verdict.missing_tokens,
                "coverage": raw_verdict.coverage,
                "supporting_chunk_ids": raw_verdict.supporting_chunk_ids,
            },
        }
        trace["raw"] = raw_trace
        trace["community"] = community_trace

        if raw_verdict.sufficient:
            trace["mode"] = "raw_hit"
            return OrchestrationOutcome(
                mode="raw_hit",
                wiki_results=wiki_results,
                verdict=verdict,
                raw_results=final_raw_result,
                raw_round_results=[r["result"] for r in raw_rounds],
                raw_verdict=raw_verdict,
                raw_calls=len(raw_rounds),
                trace=trace,
            )

        trace["mode"] = "need_community_expansion"
        return OrchestrationOutcome(
            mode="need_community_expansion",
            wiki_results=wiki_results,
            verdict=verdict,
            raw_results=final_raw_result,
            raw_round_results=[r["result"] for r in raw_rounds],
            raw_verdict=raw_verdict,
            raw_calls=len(raw_rounds),
            trace=trace,
        )

    @staticmethod
    def _build_final_raw_result(
        round_results: list[RawRetrievalResult],
        merged_hits: list[RawChunkHit],
        question: str,
        *,
        acl_snapshot_changed: bool,
    ) -> RawRetrievalResult | None:
        """把两轮结果合并成最终 RawRetrievalResult（hits 为合并后结果）。

        可见/有效/过滤 Page 数表示权限快照（用最后一轮），不是执行次数累计；
        每轮独立统计保留在 raw_round_results/trace.rounds。
        """
        if not round_results:
            return None
        last = round_results[-1]
        return RawRetrievalResult(
            hits=merged_hits,
            query=question,
            retrieval_round=last.retrieval_round,
            visible_page_count=last.visible_page_count,       # 快照，不 sum
            eligible_page_count=last.eligible_page_count,
            eligible_chunk_count=last.eligible_chunk_count,
            filtered_out_page_count=last.filtered_out_page_count,  # 快照，不 sum
            filtered_reason_counts=dict(last.filtered_reason_counts),  # 不 merge
            bm25_used=any(r.bm25_used for r in round_results),
            dense_used=any(r.dense_used for r in round_results),
            reranker_used=any(r.reranker_used for r in round_results),
            degraded=_stable_degraded([d for r in round_results for d in r.degraded]),
            visible_page_ids=set(last.visible_page_ids),
            acl_snapshot_changed=acl_snapshot_changed,
        )


def build_default_retrieval_orchestrator(
    db: Session,
    *,
    reranker=None,
    embedder: Callable[[str], list[float] | None] | None = None,
    async_embedder: Callable[[str], Awaitable[Any]] | None = None,
) -> RetrievalOrchestrator:
    """生产工厂：注入真实 EmbeddingService（异步）与 get_reranker。

    - async_embedder：DefaultAsyncEmbedder（惰性创建 EmbeddingService，用后 close）；
    - reranker：get_reranker(settings.reranker_api_url, settings.reranker_model)；
    - 返回可异步调用的编排器（retrieve_async），不再伪装成同步可用。
    embedder/async_embedder/reranker 可注入（单元测试用 Mock，不调用真实联网服务）。
    """
    from app.core.retrieval.rerank import get_reranker

    if reranker is None:
        reranker = get_reranker(settings.reranker_api_url, settings.reranker_model)

    if async_embedder is None:
        configured = bool(settings.embedding_api_url and settings.embedding_model)
        async_embedder = DefaultAsyncEmbedder(configured=configured)

    return RetrievalOrchestrator(
        db,
        raw_retriever=RawDocumentRetriever(db, reranker=reranker),
        embedder=embedder,
        async_embedder=async_embedder,
    )


def _flatten_matched(hits: list[RawChunkHit]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        for t in h.matched_tokens:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out
