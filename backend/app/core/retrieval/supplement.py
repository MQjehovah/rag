"""补充检索 + 二次 Judge（P4-BE-09 后半 + P4-BE-10，V3 计划 9.1 链路末端）。

第一次检索 + Judge 不通过且 gap 明确（supplement=True）时：
用 gap 构造一次补充查询 → 第二次检索 → 再次 Judge → 决定回答或拒答。

第二次检索后必须再次 Judge（P4-BE-10）：不能仅凭补充检索结果直接回答，
防止补充检索引入的新候选未被审判。

依赖注入设计：run_with_supplement 接收检索函数 + judge，与具体检索实现解耦，
可复用于现有检索（retrieve_pages）与未来统一管线（run_pipeline）。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from app.core.retrieval.judge import KnowledgeJudge, build_supplement_query, derive_gap
from app.core.retrieval.sources import RetrievedKnowledge

logger = logging.getLogger(__name__)

# 检索函数：async (question, user_context) -> list[RetrievedKnowledge]
RetrieveFn = Callable[[str, Optional[dict]], Awaitable[list[RetrievedKnowledge]]]


async def run_with_supplement(
    retrieve_fn: RetrieveFn,
    question: str,
    *,
    judge: Optional[KnowledgeJudge] = None,
    user_context: Optional[dict] = None,
) -> dict:
    """第一次检索+judge →（gap）补充检索 → 二次 judge。

    Args:
        retrieve_fn: 异步检索函数，返回 RetrievedKnowledge 列表。
        question: 用户问题。
        judge: KnowledgeJudge 实例（缺省新建）。
        user_context: 用户上下文（scope 检查用）。

    Returns:
        {
            "results": 最终候选,
            "judgment": 最终 judgment（补充后为二次 judge 结果）,
            "supplemented": 是否做了补充检索,
            "supplement_query": 补充查询（未补充为 None）,
            "first_judgment": 第一次 judgment,
        }
    """
    judge = judge or KnowledgeJudge()

    # 第一次检索 + judge
    results = await retrieve_fn(question, user_context)
    first_judgment = judge.judge(results, user_context)

    if first_judgment["passed"]:
        return {
            "results": results,
            "judgment": first_judgment,
            "supplemented": False,
            "supplement_query": None,
            "first_judgment": first_judgment,
        }

    # 推导 gap，决定是否补充检索
    gap = first_judgment.get("gap")
    if not gap or not gap.get("supplement"):
        return {
            "results": results,
            "judgment": first_judgment,
            "supplemented": False,
            "supplement_query": None,
            "first_judgment": first_judgment,
        }

    # 补充检索（最多一次）
    supplement_query = build_supplement_query(question, gap)
    try:
        supplemented_results = await retrieve_fn(supplement_query, user_context)
    except Exception as exc:
        logger.warning(f"supplement retrieve failed: {exc}")
        supplemented_results = []

    # 合并 + 二次 Judge（P4-BE-10：必须再次 Judge）
    merged = _merge_results(results, supplemented_results)
    second_judgment = judge.judge(merged, user_context)

    return {
        "results": merged,
        "judgment": second_judgment,
        "supplemented": True,
        "supplement_query": supplement_query,
        "first_judgment": first_judgment,
    }


def _merge_results(original: list[RetrievedKnowledge], supplemented: list[RetrievedKnowledge]) -> list[RetrievedKnowledge]:
    """合并两次检索结果，按 card_id 去重。

    同 card_id 时后出现的（补充检索结果）覆盖先出现的（第一次结果）——
    补充检索返回的是更完整版本（如带证据的 procedure），覆盖无证据的旧版。
    """
    merged: dict[str, RetrievedKnowledge] = {}
    for item in list(original) + list(supplemented):
        merged[item.card_id] = item
    return list(merged.values())
