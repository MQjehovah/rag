"""原始文档检索充分性判断器（V4 Phase D-2 封板）。

确定性规则，不依赖 GLM/LLM。保守：无法确定是否足够时返回 insufficient。

规则：
1. 无命中 → insufficient（no_raw_hit）。
2. 无有效 query token → insufficient（no_effective_query_token）。
3. query token 去重；标题与正文分别参与匹配，但不得只凭标题空壳判充分。
4. 单 token 普通词（如「安装」）不因正文出现一次就 sufficient；
   单 token 精确标题匹配或英文领域词才可充分（与 D-1 保守原则一致）。
5. 单个 Chunk 若正文完整、明确覆盖多 token 问题 → 可单独 sufficient。
6. 多 Chunk 互补：每个 Supporting Chunk 正文必须真实贡献新的 query token，
   且与已选 Supporting Chunk 的内容相似度低于阈值才算新互补证据。
7. union coverage 阈值 0.75；低于阈值 → insufficient。
8. 不依赖 final_score（RRF 任意候选通常 > 0），不使用分数伪阈值。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.retrieval.raw_retriever import RawChunkHit
from app.core.retrieval.text_similarity import content_similarity
from app.core.retrieval.wiki_retriever import meaningful_tokenize

# union coverage 阈值（保守）
COVERAGE_THRESHOLD = 0.75
# 内容相似度阈值：达到该值视为重复/高度相似，不算互补
SIMILARITY_THRESHOLD = 0.8


def _dedupe(tokens: list[str]) -> list[str]:
    return list(dict.fromkeys(tokens))


def _norm_title(title: str) -> str:
    """规范化标题（有意义 token 拼接），用于单 token 精确标题匹配。"""
    return "".join(meaningful_tokenize(title or ""))


def _is_domain_term(token: str) -> bool:
    """英文领域词启发式：全 ASCII 且含字母（专有名词特征，如 MobaXterm）。"""
    return token.isascii() and any(c.isalpha() for c in token)


@dataclass
class RawSufficiencyVerdict:
    sufficient: bool
    reason: str
    confidence: float
    matched_tokens: list[str] = field(default_factory=list)
    missing_tokens: list[str] = field(default_factory=list)
    coverage: float = 0.0
    supporting_chunk_ids: list[str] = field(default_factory=list)


def judge_raw_sufficiency(
    hits: list[RawChunkHit],
    question: str,
    *,
    retrieval_round: int = 1,
) -> RawSufficiencyVerdict:
    """判断原始文档检索结果是否充分（确定性，无 LLM）。"""
    if not hits:
        return RawSufficiencyVerdict(
            sufficient=False,
            reason="no_raw_hit",
            confidence=0.0,
            missing_tokens=["原始文档无命中"],
        )

    query_tokens = _dedupe(meaningful_tokenize(question))
    if not query_tokens:
        return RawSufficiencyVerdict(
            sufficient=False,
            reason="no_effective_query_token",
            confidence=0.0,
            missing_tokens=["查询无有效关键词"],
        )

    # 每个 hit 的正文/标题 token 集合
    hit_info: list[tuple[RawChunkHit, set[str], set[str]]] = []
    for h in hits:
        body = set(meaningful_tokenize(h.content))
        title = set(meaningful_tokenize(h.page_title or ""))
        hit_info.append((h, body, title))

    # ---- 单 token：保守，不因正文出现一次就 sufficient ----
    if len(query_tokens) == 1:
        token = query_tokens[0]
        # 1) 精确标题匹配：page_title 规范化 == token 且正文含该 token
        for h, body, _title in hit_info:
            if _norm_title(h.page_title) == token and token in body:
                return RawSufficiencyVerdict(
                    sufficient=True,
                    reason="single_token_exact_title",
                    confidence=0.8,
                    matched_tokens=[token],
                    missing_tokens=[],
                    coverage=1.0,
                    supporting_chunk_ids=[h.chunk_id],
                )
        # 2) 英文领域词：正文含该词即可充分（专有名词，如 MobaXterm）
        if _is_domain_term(token):
            for h, body, _title in hit_info:
                if token in body:
                    return RawSufficiencyVerdict(
                        sufficient=True,
                        reason="single_token_domain_term",
                        confidence=0.75,
                        matched_tokens=[token],
                        missing_tokens=[],
                        coverage=1.0,
                        supporting_chunk_ids=[h.chunk_id],
                    )
        return RawSufficiencyVerdict(
            sufficient=False,
            reason="single_token_insufficient",
            confidence=0.0,
            matched_tokens=[],
            missing_tokens=[token],
            coverage=0.0,
            supporting_chunk_ids=[],
        )

    # ---- 多 token：单 Chunk 完整覆盖可单独充分 ----
    for h, body, _title in hit_info:
        if body and all(t in body for t in query_tokens):
            return RawSufficiencyVerdict(
                sufficient=True,
                reason="single_chunk_complete",
                confidence=0.9,
                matched_tokens=list(query_tokens),
                missing_tokens=[],
                coverage=1.0,
                supporting_chunk_ids=[h.chunk_id],
            )

    # ---- 贪心互补：每个 Supporting Chunk 必须真实新增 token 且不重复/相似 ----
    covered: set[str] = set()
    matched_all: list[str] = []
    supporting: list[RawChunkHit] = []
    for h, body, title in hit_info:
        scope = body | title  # 标题参与匹配，正文作为充分证据
        new_tokens = [t for t in query_tokens if t not in covered and t in scope]
        body_new = [t for t in new_tokens if t in body]
        if not body_new:
            # 标题空壳或未新增正文 token，不算互补贡献
            continue
        # 与已选 Supporting Chunk 高度相似 → 不算新的互补证据
        if any(content_similarity(h.content, s.content) >= SIMILARITY_THRESHOLD for s in supporting):
            continue
        covered.update(new_tokens)
        matched_all.extend(t for t in new_tokens if t not in matched_all)
        supporting.append(h)
        if len(covered) / len(query_tokens) >= COVERAGE_THRESHOLD:
            break

    coverage = len(covered) / len(query_tokens)
    matched = [t for t in query_tokens if t in covered]
    missing = [t for t in query_tokens if t not in covered]

    if coverage >= COVERAGE_THRESHOLD and supporting:
        confidence = round(min(0.9, 0.5 + 0.4 * coverage), 4)
        return RawSufficiencyVerdict(
            sufficient=True,
            reason="complementary_coverage",
            confidence=confidence,
            matched_tokens=matched,
            missing_tokens=[],
            coverage=round(coverage, 4),
            supporting_chunk_ids=[s.chunk_id for s in supporting],
        )

    return RawSufficiencyVerdict(
        sufficient=False,
        reason="insufficient_coverage",
        confidence=round(coverage, 4),
        matched_tokens=matched,
        missing_tokens=missing or ["匹配不足，需要补充检索"],
        coverage=round(coverage, 4),
        supporting_chunk_ids=[s.chunk_id for s in supporting],
    )
