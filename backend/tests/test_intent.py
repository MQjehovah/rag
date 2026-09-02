"""T6.5 意图分类单元测试。

覆盖：规则层命中（六意图）、边界（版本差异 vs 对比）、LLM 兜底、
LLM 失败回退、准确率 ≥ 80%。纯函数测试，无 DB/网络依赖。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.retrieval import TaskIntent, TaskPlanner
from app.core.retrieval import intent as intent_module

planner = TaskPlanner()

# 评测集 type → 期望意图（W12 修复：用评测集实测准确率，而非自造用例）
EVAL_PATH = Path(__file__).parent / "eval" / "eval_dataset.jsonl"
EVAL_TYPE_TO_INTENT = {
    "fact": TaskIntent.FACT_LOOKUP,
    "deployment": TaskIntent.PROCEDURE,
    "diagnostic": TaskIntent.DIAGNOSTIC,
    "decision": TaskIntent.DECISION,
    "version": TaskIntent.VERSION_DIFF,
}


def _load_eval_cases() -> list[tuple[str, TaskIntent]]:
    """加载评测集，映射为 (question, expected_intent) 用例。"""
    cases = []
    with EVAL_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            expected = EVAL_TYPE_TO_INTENT.get(rec.get("type"))
            if expected is not None:
                cases.append((rec["question"], expected))
    return cases


def test_eval_dataset_accuracy_at_least_80_percent():
    """评测集 70 题规则层分类准确率 ≥ 80%（W12 修复：此前 44.3%）。"""
    cases = _load_eval_cases()
    assert len(cases) >= 50, f"评测集过小：{len(cases)}"
    hits = sum(1 for q, e in cases if planner.classify_sync(q) == e)
    rate = hits / len(cases)
    assert rate >= 0.8, f"评测集准确率 {rate:.0%} ({hits}/{len(cases)}) < 80%"


# (question, expected_intent)。全部应被规则层命中（准确率期望 100%）。
_INTENT_CASES: list[tuple[str, TaskIntent]] = [
    # FACT_LOOKUP
    ("Titan 810 是什么类型的设备?", TaskIntent.FACT_LOOKUP),
    ("Titan 810 标准电池容量是多少?", TaskIntent.FACT_LOOKUP),
    ("部署工具中的 Select Map 是什么功能?", TaskIntent.FACT_LOOKUP),
    ("Titan 810 的电池接口在哪个位置?", TaskIntent.FACT_LOOKUP),
    ("Titan 810 支持哪些充电方式?", TaskIntent.FACT_LOOKUP),
    # PROCEDURE
    ("如何部署 Skywalker50 机器人?", TaskIntent.PROCEDURE),
    ("Titan 810 的安装步骤是什么?", TaskIntent.PROCEDURE),
    ("怎么配置海外售后流程?", TaskIntent.PROCEDURE),
    ("机器人充电站设置流程是怎样的?", TaskIntent.PROCEDURE),
    ("如何给 Titan 810 更换电池?", TaskIntent.PROCEDURE),
    # DIAGNOSTIC
    ("机器人启动不了怎么办?", TaskIntent.DIAGNOSTIC),
    ("充电时设备报错如何排查?", TaskIntent.DIAGNOSTIC),
    ("为什么 Titan 810 无法连接网络?", TaskIntent.DIAGNOSTIC),
    ("设备出现故障告警如何处理?", TaskIntent.DIAGNOSTIC),
    ("充电桩打不开是什么原因?", TaskIntent.DIAGNOSTIC),
    # DECISION
    ("我应该选哪款机器人适合仓库?", TaskIntent.DECISION),
    ("仓库部署应该选择哪种方案?", TaskIntent.DECISION),
    ("推荐一款适合超市的清洁机器人?", TaskIntent.DECISION),
    ("Titan 810 和 Skywalker50 我该选哪款?", TaskIntent.DECISION),
    # VERSION_DIFF
    ("升级到 V2.4 有什么影响?", TaskIntent.VERSION_DIFF),
    ("Titan 810 新版本更新了哪些内容?", TaskIntent.VERSION_DIFF),
    ("从 V2.3 升级有什么风险?", TaskIntent.VERSION_DIFF),
    ("V2.4 版本差异大吗?", TaskIntent.VERSION_DIFF),
    # COMPARISON
    ("Titan 810 和 Skywalker50 有什么区别?", TaskIntent.COMPARISON),
    ("比较一下国内和海外售后流程的差别?", TaskIntent.COMPARISON),
    ("对比一下两款机器人的续航差别?", TaskIntent.COMPARISON),
    ("两款清洁机器人哪个好?", TaskIntent.COMPARISON),
]


def test_rule_classify_all_cases():
    """所有构造用例都被规则层命中且分类正确（期望 100%）。"""
    for question, expected in _INTENT_CASES:
        actual = planner.classify_sync(question)
        assert actual == expected, f"{question}: 期望 {expected.value}, 实际 {actual}"


def test_rule_accuracy_at_least_80_percent():
    """意图分类准确率 ≥ 80%（T6.5 验收）。"""
    hits = sum(1 for q, e in _INTENT_CASES if planner.classify_sync(q) == e)
    rate = hits / len(_INTENT_CASES)
    assert rate >= 0.8, f"准确率 {rate:.0%} < 80%"


def test_empty_question_returns_none():
    assert planner.classify_sync("") is None
    assert planner.classify_sync("   ") is None


def test_boundary_version_diff_vs_comparison():
    """"版本有什么区别"归 COMPARISON；明确"升级"归 VERSION_DIFF。"""
    assert planner.classify_sync("V2.3 和 V2.4 版本有什么区别?") == TaskIntent.COMPARISON
    assert planner.classify_sync("升级到 V2.4 有什么影响?") == TaskIntent.VERSION_DIFF


@pytest.mark.asyncio
async def test_classify_llm_fallback(monkeypatch):
    """规则不命中时走 LLM 兜底。"""
    async def fake_llm(question):
        return TaskIntent.DIAGNOSTIC
    monkeypatch.setattr(intent_module, "_llm_classify", fake_llm)
    result = await planner.classify("一段完全不包含规则关键词的文本")
    assert result == TaskIntent.DIAGNOSTIC


@pytest.mark.asyncio
async def test_classify_llm_failure_defaults_to_fact(monkeypatch):
    """LLM 兜底失败（返回 None）时回退 FACT_LOOKUP。"""
    async def fake_llm(question):
        return None
    monkeypatch.setattr(intent_module, "_llm_classify", fake_llm)
    result = await planner.classify("一段完全不包含规则关键词的文本")
    assert result == TaskIntent.FACT_LOOKUP


@pytest.mark.asyncio
async def test_classify_llm_exception_defaults_to_fact(monkeypatch):
    """LLM 抛异常也回退 FACT_LOOKUP。"""
    async def fake_llm(question):
        raise RuntimeError("llm down")
    monkeypatch.setattr(intent_module, "_llm_classify", fake_llm)
    result = await planner.classify("一段完全不包含规则关键词的文本")
    assert result == TaskIntent.FACT_LOOKUP


def test_route_table_covers_all_intents():
    """路由表覆盖全部 6 个意图，且每个有主检索 Card type。"""
    from app.core.retrieval.intent import _ROUTE_TABLE
    for intent in TaskIntent:
        route = _ROUTE_TABLE[intent]
        assert route.primary_card_types, f"{intent.value} 缺主检索 Card type"


def test_plan_returns_intent_and_route():
    plan = planner.route(TaskIntent.FACT_LOOKUP)
    assert plan.primary_card_types == ["fact"]
    assert plan.output_shape == "direct_answer"
