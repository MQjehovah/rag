"""Wiki 检索充分性判断器（V4 Phase D-1 最终验收补丁）。

保守判断：不确定时返回 need_raw_retrieval，不提前停止检索。

规则（确定性，无 LLM 依赖）：
1. 无命中 → insufficient（no_wiki_hit）。
2. 无有效 query token → insufficient（no_effective_query_token）。
3. 命中 Wiki 无合法 Revision 正文 → insufficient（empty_wiki_content）。
4. exact_title_match（norm_q == norm_title）→ 唯一可单独判定 sufficient 的标题情形。
5. 查询仅 1 个有效 token 且非精确标题 → 一律 insufficient（single_token_insufficient）。
6. 多 token：distinct matched tokens >= 2 且 coverage >= 阈值 → sufficient；
   否则 insufficient（insufficient_coverage），missing_aspects 返回未命中 token。
7. title_contained（标题仅包含在更长问题中）不作为充分条件，必须继续判断 coverage。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.core.retrieval.wiki_retriever import WikiRetrievalResult

# 多 token 充分性的保守阈值
COVERAGE_THRESHOLD = 0.75
MIN_DISTINCT_MATCHED = 2


@dataclass
class SufficiencyVerdict:
    sufficient: bool
    reason: str
    confidence: float
    missing_aspects: list[str] = field(default_factory=list)


def judge_wiki_sufficiency(
    result: WikiRetrievalResult,
    question: str,
    *,
    llm_judge: Callable | None = None,
) -> SufficiencyVerdict:
    """判断 Wiki 结果是否充分（保守，不确定返回 need_raw_retrieval）。"""
    hits = result.hits
    if not hits:
        return SufficiencyVerdict(
            sufficient=False,
            reason="no_wiki_hit",
            confidence=0.0,
            missing_aspects=["wiki 中无匹配主题"],
        )

    best = hits[0]

    # 无有效 query token
    if best.query_token_count == 0:
        return SufficiencyVerdict(
            sufficient=False,
            reason="no_effective_query_token",
            confidence=0.0,
            missing_aspects=["查询无有效关键词"],
        )

    # 命中 Wiki 无有效正文（content 为空）
    if not (best.content or "").strip():
        return SufficiencyVerdict(
            sufficient=False,
            reason="empty_wiki_content",
            confidence=0.0,
            missing_aspects=["Wiki 正文为空"],
        )

    # 精确标题完全匹配：唯一可单独判定 sufficient 的标题情形
    if best.exact_title_match:
        confidence = round(min(0.95, 0.85 + 0.1 * best.coverage), 4)
        return SufficiencyVerdict(
            sufficient=True,
            reason="exact_title_match",
            confidence=confidence,
            missing_aspects=[],
        )

    matched_count = len(best.matched_tokens)  # 已去重

    # 单有效 token 且非精确标题 → 一律 insufficient（删除 single_term_in_title 自动充分规则）
    if best.query_token_count == 1:
        return SufficiencyVerdict(
            sufficient=False,
            reason="single_token_insufficient",
            confidence=round(best.coverage, 4),
            missing_aspects=list(best.query_tokens) or ["单个词不足以定位主题"],
        )

    # 多 token：distinct matched >= 2 且 coverage >= 阈值
    if matched_count >= MIN_DISTINCT_MATCHED and best.coverage >= COVERAGE_THRESHOLD:
        confidence = round(min(0.9, 0.6 + 0.3 * best.coverage), 4)
        return SufficiencyVerdict(
            sufficient=True,
            reason="multi_token_coverage",
            confidence=confidence,
            missing_aspects=[],
        )

    # 其余（含 title_contained 但 coverage 不足）→ 保守 insufficient
    return SufficiencyVerdict(
        sufficient=False,
        reason="insufficient_coverage",
        confidence=round(best.coverage, 4),
        missing_aspects=list(best.missing_tokens) or ["匹配不足，需要原始文档检索"],
    )


class SufficiencyJudge:
    """SufficiencyJudge 类封装。"""

    def __init__(self, *, llm_judge: Callable | None = None):
        self._llm_judge = llm_judge

    def judge(self, result: WikiRetrievalResult, question: str) -> SufficiencyVerdict:
        return judge_wiki_sufficiency(result, question, llm_judge=self._llm_judge)
