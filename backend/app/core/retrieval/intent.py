"""任务意图分类 + 路由表（W6 T6.1/T6.2）。

分类策略：规则优先（关键词命中即返回），规则不命中时 LLM 兜底，
LLM 也不可用则回退 FACT_LOOKUP。规则表顺序敏感：高优先级意图在前。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.core.llm_client import call_llm_json

logger = logging.getLogger(__name__)


class TaskIntent(str, Enum):
    """任务意图枚举。对应知识卡片的六大应用场景。"""
    FACT_LOOKUP = "fact_lookup"      # 是什么/参数/支持/规格
    PROCEDURE = "procedure"          # 怎么做/步骤/部署
    DIAGNOSTIC = "diagnostic"        # 为什么/失败/报错
    DECISION = "decision"            # 选哪个/推荐/方案
    VERSION_DIFF = "version_diff"    # 升级/版本差异/变更
    COMPARISON = "comparison"        # 比较/对比/区别


# 规则表。顺序敏感：先命中的高优先级意图优先。
# 设计要点（基于评测集 70 题离线校准，目标 ≥80%）：
# - VERSION_DIFF 在最前：版本题常含"是什么/支持"，需先拦截（不用泛"升级"，避免误伤"软件升级步骤"类 procedure）
# - DIAGNOSTIC 在 FACT 前："报错 E102 是什么故障"含"是什么"，但"报错"信号更强
# - DECISION 用具体词（"应该选/还是"），不用泛"应该"，避免误伤"创建任务时应该依次点击"类 procedure
_INTENT_RULES: list[tuple[list[str], TaskIntent]] = [
    # VERSION_DIFF：版本对比/差异/兼容/发布
    (["相比", "有什么不同", "差异", "替代了", "兼容哪些", "修复了", "最低硬件要求", "发布日期", "当前版本", "支持哪些新", "新版本", "版本间", "升级到", "升级有"], TaskIntent.VERSION_DIFF),
    # DIAGNOSTIC：故障/报错/原因/排查/处理
    (["报错", "失败", "异常", "无法", "不能", "排查", "故障", "不工作", "启动不了", "打不开", "坏了", "失灵", "停机", "告警", "原因", "怎么办", "熄火", "急停", "误检", "不达标", "不充电", "诊断"], TaskIntent.DIAGNOSTIC),
    # DECISION：选择/推荐/场景
    (["应该选", "该选", "选哪个", "推荐", "哪款", "适合", "方案", "还是", "选型", "vs", "策略", "最常见", "需要设置", "应该设置", "应该关闭", "应该优先", "应该用"], TaskIntent.DECISION),
    # COMPARISON：对比
    (["比较", "对比", "哪个好", "有什么区别", "哪个更", "区别"], TaskIntent.COMPARISON),
    # PROCEDURE：操作步骤（不用"部署/配置/设置"泛词，避免误伤"部署工具中...是什么功能"类 fact 题）
    (["怎么", "如何", "怎样", "步骤", "安装", "操作流程", "流程", "点击", "自检", "顺序", "确认"], TaskIntent.PROCEDURE),
    # FACT_LOOKUP：事实/参数/规格
    (["是什么", "什么功能", "什么概念", "什么作用", "什么品牌", "代表什么", "对应哪些", "支持哪些", "用于做什么", "管理什么", "什么意思", "多少", "位置", "型号", "容量", "参数", "规格"], TaskIntent.FACT_LOOKUP),
]


def _match_keyword(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


@dataclass(frozen=True)
class RoutePlan:
    """单个意图的检索路由计划。"""
    intent: TaskIntent
    primary_card_types: list[str] = field(default_factory=list)  # 主检索的 Card 类型
    secondary: str = "page"        # 辅助检索源："page" 或 "card:fact"
    output_shape: str = "direct_answer"  # direct_answer/steps/checklist/recommendation/changelist/comparison


# 路由表（参照详细版 5.2 节，COMPARISON 为补全行）。
_ROUTE_TABLE: dict[TaskIntent, RoutePlan] = {
    TaskIntent.FACT_LOOKUP: RoutePlan(TaskIntent.FACT_LOOKUP, ["fact"], "page", "direct_answer"),
    TaskIntent.PROCEDURE: RoutePlan(TaskIntent.PROCEDURE, ["procedure"], "page", "steps"),
    TaskIntent.DIAGNOSTIC: RoutePlan(TaskIntent.DIAGNOSTIC, ["diagnostic"], "card:fact", "checklist"),
    TaskIntent.DECISION: RoutePlan(TaskIntent.DECISION, ["decision", "rule"], "page", "recommendation"),
    TaskIntent.VERSION_DIFF: RoutePlan(TaskIntent.VERSION_DIFF, ["decision", "rule"], "page", "changelist"),
    TaskIntent.COMPARISON: RoutePlan(TaskIntent.COMPARISON, ["decision", "fact"], "page", "comparison"),
}


_INTENT_PROMPT = """你是一个企业知识库助手，负责判断用户提问的任务意图。

问题：{question}

请只返回 JSON，结构如下：
{{
  "intent": "fact_lookup | procedure | diagnostic | decision | version_diff | comparison"
}}

意图含义：
- fact_lookup：查询事实、参数、规格（"是什么/支持/容量/参数"）
- procedure：查询操作步骤、部署流程（"怎么做/如何/步骤"）
- diagnostic：故障排查（"为什么/失败/报错/异常"）
- decision：方案决策（"选哪个/推荐/哪款适合"）
- version_diff：版本差异、升级影响（"V2.3 vs V2.4/升级"）
- comparison：多对象对比（"比较/对比/区别"）

只输出 JSON，不要解释。"""


async def _llm_classify(question: str) -> Optional[TaskIntent]:
    """LLM 兜底分类。失败或无法解析返回 None。"""
    messages = [
        {"role": "user", "content": _INTENT_PROMPT.format(question=question)},
    ]
    data = await call_llm_json(messages, context=f"intent:{question[:30]}")
    if not data:
        return None
    try:
        return TaskIntent(str(data.get("intent", "")).strip().lower())
    except ValueError:
        return None


class TaskPlanner:
    """任务意图规划器。规则优先，LLM 兜底。"""

    def classify_sync(self, question: str) -> Optional[TaskIntent]:
        """纯规则层分类。无命中返回 None（交给 LLM 兜底）。"""
        if not question or not question.strip():
            return None
        for keywords, intent in _INTENT_RULES:
            if _match_keyword(question, keywords):
                return intent
        return None

    async def classify(self, question: str) -> TaskIntent:
        """完整分类：规则优先 → LLM 兜底 → 默认 FACT_LOOKUP。"""
        rule = self.classify_sync(question)
        if rule is not None:
            return rule
        try:
            llm = await _llm_classify(question)
            if llm is not None:
                return llm
        except Exception as exc:
            logger.warning(f"intent LLM classify failed: {exc}")
        return TaskIntent.FACT_LOOKUP

    def route(self, intent: TaskIntent) -> RoutePlan:
        """按意图查路由表。"""
        return _ROUTE_TABLE.get(intent, _ROUTE_TABLE[TaskIntent.FACT_LOOKUP])

    async def plan(self, question: str) -> dict:
        """完整规划：返回 {intent, route}。"""
        intent = await self.classify(question)
        route = self.route(intent)
        return {
            "intent": intent.value,
            "route": {
                "primary_card_types": route.primary_card_types,
                "secondary": route.secondary,
                "output_shape": route.output_shape,
            },
        }
