"""Reranker 接口抽象（P4-BE-06，V3 计划 9.1）。

复用现有 Chunk Reranker 并支持 CardBlock/Evidence。外部 rerank 服务
（bms-cn.xzrobot.com）当前故障，故抽象为接口：
- CrossEncoderReranker：调用外部 reranker API（恢复后填充实现）
- IdentityReranker：无 reranker 时按 fused_score 原样返回（降级）

纯接口 + 降级实现，不抛异常。
"""
from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.core.retrieval.candidates import RetrievalCandidate

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    """重排接口：candidates → 按 rerank_score 降序排序并回填 rerank_score。"""

    name: str

    def rerank(self, question: str, candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
        ...


class IdentityReranker:
    """降级重排：无 reranker 时按 fused_score 原样返回。"""

    name = "identity"

    def rerank(self, question: str, candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
        # 降级路径：只按 fused_score 排序，不写 rerank_score。
        return sorted(candidates, key=lambda c: c.fused_score, reverse=True)


class CrossEncoderReranker:
    """外部 Cross-Encoder 重排（P13-MODEL-03 修复降级语义）。

    失败时不再伪造统一分数：rerank_with_status 返回 used=False，
    调用方保留 Fusion 原排序，不生成虚假 relevance_score。
    """

    name = "cross_encoder"

    def __init__(self, api_url: str = "", model: str = ""):
        self.api_url = api_url
        self.model = model

    def rerank(self, question: str, candidates: list[RetrievalCandidate]) -> list[RetrievalCandidate]:
        """兼容旧调用：成功时按 rerank_score 排序，失败时保留 Fusion 原排序。

        失败不再写 rerank_score（不伪装 Identity 分数），只按 fused_score 原样返回。
        """
        result = self.rerank_with_status(question, candidates)
        if not result.used:
            # 失败降级：保留 Fusion 原排序，不生成虚假 rerank_score
            return sorted(candidates, key=lambda c: c.fused_score, reverse=True)
        return sorted(candidates, key=lambda c: c.rerank_score or 0.0, reverse=True)

    def rerank_with_status(self, question: str, candidates: list[RetrievalCandidate]) -> "RerankResult":
        """带状态重排（P13-MODEL-03）：返回 RerankResult。

        used=True 才写 rerank_score；失败返回 used=False，不写分数。
        """
        import time as _time
        from app.core.embedding.results import RerankResult, classify_http_error

        start = _time.monotonic()
        if not self.api_url:
            return RerankResult(used=False, degraded=True, error_code="BAD_REQUEST", error_message="未配置 api_url")

        try:
            response = httpx.post(
                self.api_url,
                json={
                    "model": self.model,
                    "query": question,
                    "documents": [candidate.content for candidate in candidates],
                    "top_k": len(candidates),
                },
                timeout=30.0,
            )
            if response.status_code != 200:
                error_code = classify_http_error(response.status_code, response.text)
                return RerankResult(used=False, degraded=True, model_uid=self.model,
                                    error_code=error_code, error_message=response.text[:200],
                                    latency_ms=int((_time.monotonic() - start) * 1000))
            results = response.json().get("results", [])
            by_index = {
                int(item["index"]): float(item.get("relevance_score", 0.0))
                for item in results if "index" in item
            }
            # 不完整 index 列表 → 判 partial/invalid，不伪造其余候选为已重排
            if len(by_index) < len(candidates):
                return RerankResult(
                    used=False, degraded=True, model_uid=self.model,
                    error_code="INVALID_RESPONSE", error_message="partial results",
                    latency_ms=int((_time.monotonic() - start) * 1000),
                )
            for index, candidate in enumerate(candidates):
                candidate.rerank_score = by_index[index]
            return RerankResult(
                results=[{"index": i, "relevance_score": by_index[i]} for i in range(len(candidates))],
                used=True, degraded=False, model_uid=self.model,
                latency_ms=int((_time.monotonic() - start) * 1000),
            )
        except httpx.ConnectError:
            return RerankResult(used=False, degraded=True, model_uid=self.model,
                                error_code="NETWORK_UNREACHABLE", error_message="连接失败",
                                latency_ms=int((_time.monotonic() - start) * 1000))
        except Exception as exc:
            return RerankResult(used=False, degraded=True, model_uid=self.model,
                                error_code="SERVER_ERROR", error_message=str(exc)[:200],
                                latency_ms=int((_time.monotonic() - start) * 1000))


def get_reranker(api_url: str = "", model: str = "") -> Reranker:
    """工厂：有 api_url 用 CrossEncoder，否则 Identity。"""
    if api_url:
        return CrossEncoderReranker(api_url, model)
    return IdentityReranker()
