"""确定性补充查询生成器（V4 Phase D-2 封板）。

仅 Round 1 不足时执行。输入原始问题、Wiki missing_aspects、
Round 1 missing_aspects、Round 1 已命中标题/token；输出补充查询。

硬约束：
- 补充查询规范化后的 token 集合必须与原查询不同；
- 必须至少引入一个原查询没有的新有效 token；
- added_terms 禁止出现原查询已包含的 token；
- missing_aspects 中若本就是原查询词，不能重新追加；
- 找不到新 token 时 can_supplement=False，不伪造新 token。

GLM 可作为可选增强，但默认及 GLM 不可用时必须确定性生成；
增强结果用规范化 token 集合比较（不是字符串比较），不合格则回退确定性查询。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from app.core.retrieval.wiki_retriever import meaningful_tokenize

logger = logging.getLogger(__name__)

# 兜底新增词（无任何缺失/扩展 token 时，保证补充查询不同于原查询）
_FALLBACK_TERMS = ["详细信息", "排查方法"]

# 通用扩展词库（在意图聚焦之后、兜底之前尝试）
_EXPAND_TERMS = ["原理", "背景", "说明", "规范", "标准", "注意事项", "常见问题"]

# 意图聚焦扩展：命中关键词 → 追加的聚焦词（均为 meaningful token，可解释）
_INTENT_FOCUS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("故障", "异常", "报错", "失败", "排查", "不工作", "失灵"), ("原因", "排查", "处理", "解决")),
    (("安装", "部署"), ("步骤", "配置", "条件")),
    (("参数", "规格", "容量", "型号"), ("型号", "数值", "单位")),
    (("操作", "使用", "命令"), ("命令", "步骤", "示例")),
]


def _dedupe(tokens: list[str]) -> list[str]:
    """保序去重。"""
    return list(dict.fromkeys(tokens))


@dataclass
class SupplementalQuery:
    supplemental_query: str
    original_tokens: list[str] = field(default_factory=list)   # 原查询去重 token（保序）
    focus_terms: list[str] = field(default_factory=list)        # 意图聚焦候选词（可解释）
    added_terms: list[str] = field(default_factory=list)        # 实际引入的新 token（不含原词）
    reason: str = ""
    can_supplement: bool = True                                 # 是否能产生语义变化


def _single_token_items(items: list[str]) -> list[str]:
    """从 missing_aspects 提取「恰好一个 meaningful token」的项。

    描述性短语（如 "匹配不足，需要原始文档检索"）会被拆成多个 token，跳过。
    """
    out: list[str] = []
    for item in items:
        tokens = meaningful_tokenize(item or "")
        if len(tokens) == 1:
            out.append(tokens[0])
    return out


def _intent_focus_terms(original_tokens: list[str]) -> list[str]:
    """根据原查询 token 命中意图关键词，返回聚焦候选词（去重、保序）。"""
    token_set = set(original_tokens)
    focus: list[str] = []
    for keywords, terms in _INTENT_FOCUS:
        if any(k in token_set for k in keywords):
            for t in terms:
                if t not in focus:
                    focus.append(t)
    return focus


def build_supplemental_query(
    original_question: str,
    *,
    wiki_missing_aspects: list[str] | None = None,
    round1_missing_aspects: list[str] | None = None,
    round1_matched_tokens: list[str] | None = None,
    llm_enhance: Callable[[str, list[str]], str] | None = None,
) -> SupplementalQuery:
    """确定性生成补充查询（保证语义变化，而非重复加权）。"""
    original = (original_question or "").strip()
    original_tokens = _dedupe(meaningful_tokenize(original))
    original_set = set(original_tokens)

    wiki_missing = list(wiki_missing_aspects or [])
    round1_missing = list(round1_missing_aspects or [])
    round1_matched = list(round1_matched_tokens or [])

    # 1. 缺失方面：只取不在原查询中的单个 token
    missing_candidates = _dedupe(_single_token_items(wiki_missing + round1_missing))
    missing_new = [t for t in missing_candidates if t not in original_set]

    # 2. 意图聚焦词（排除原查询词）
    focus_terms = _dedupe(_intent_focus_terms(original_tokens))
    focus_new = [t for t in focus_terms if t not in original_set]

    # 3. 通用扩展词库（排除原查询词）
    expand_new = [
        t for t in _dedupe(
            tt for term in _EXPAND_TERMS for tt in meaningful_tokenize(term)
        )
        if t not in original_set
    ]

    # 4. 兜底词（排除原查询词）
    fallback_new = [
        t for t in _dedupe(
            tt for term in _FALLBACK_TERMS for tt in meaningful_tokenize(term)
        )
        if t not in original_set
    ]

    # 选择来源：缺失 > 意图聚焦 > 扩展词库 > 兜底
    if missing_new:
        added = missing_new
        source = "missing"
    elif focus_new:
        added = focus_new
        source = "focus"
    elif expand_new:
        added = expand_new
        source = "expand"
    elif fallback_new:
        added = fallback_new
        source = "fallback"
    else:
        added = []
        source = "none"

    can_supplement = bool(added)
    supplemental = f"{original} {' '.join(added)}".strip() if original else " ".join(added)

    # LLM 增强：用规范化 token 集合比较（非字符串比较），不合格回退确定性查询
    if llm_enhance is not None:
        try:
            enhanced = (llm_enhance(original, added) or "").strip()
            if enhanced:
                enhanced_tokens = _dedupe(meaningful_tokenize(enhanced))
                enhanced_new = [t for t in enhanced_tokens if t not in original_set]
                if enhanced_new:
                    supplemental = enhanced
                    added = enhanced_new
                    source = "llm"
                    can_supplement = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("supplement llm enhance failed: %s", exc)

    if not can_supplement:
        added = []
        supplemental = original

    reason = {
        "missing": f"聚焦缺失方面：{'、'.join(added)}",
        "focus": f"按意图扩展：{'、'.join(added)}",
        "expand": f"扩展词库：{'、'.join(added)}",
        "fallback": f"兜底扩展：{'、'.join(added)}",
        "llm": f"LLM 增强：{'、'.join(added)}",
        "none": "无可用补充词",
    }[source]

    return SupplementalQuery(
        supplemental_query=supplemental,
        original_tokens=original_tokens,
        focus_terms=focus_terms,
        added_terms=added,
        reason=reason,
        can_supplement=can_supplement,
    )
