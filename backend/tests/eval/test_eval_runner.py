"""T1.3 基线评测脚本的纯函数单元测试。

只测计算逻辑（不依赖运行中的后端）：
- split_hints / result_is_hit
- compute_recall_at_k / compute_citation_precision
- aggregate / build_report
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_baseline.py"


def _load_eval_module():
    spec = importlib.util.spec_from_file_location("eval_baseline_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_baseline_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ev():
    return _load_eval_module()


def test_split_hints_splits_by_slash(ev):
    assert ev.split_hints("电池接口/固定螺钉") == ["电池接口", "固定螺钉"]


def test_split_hints_drops_empty(ev):
    assert ev.split_hints("/电池/") == ["电池"]


def test_split_hints_empty_string(ev):
    assert ev.split_hints("") == []


def test_result_is_hit_matches_substring(ev):
    result = {"title": "TITAN810 部署手册", "content": "标号 1 是电池接口"}
    assert ev.result_is_hit(result, ["电池接口"]) is True


def test_result_is_hit_case_insensitive(ev):
    result = {"title": "Titan 810", "content": "robot cleaner"}
    assert ev.result_is_hit(result, ["titan"]) is True


def test_result_is_hit_no_hints_returns_false(ev):
    assert ev.result_is_hit({"title": "x", "content": "y"}, []) is False


def test_result_is_hit_normalizes_whitespace(ev):
    result = {"title": "TITAN 810 部署", "content": "电池 接口"}
    assert ev.result_is_hit(result, ["电池接口"]) is True


def test_compute_recall_at_k_hit_within_top(ev):
    results = [
        {"title": "无关键", "content": "无关键"},
        {"title": "电池接口", "content": ""},
    ]
    assert ev.compute_recall_at_k(results, ["电池接口"], 5) is True


def test_compute_recall_at_k_miss_when_beyond_k(ev):
    results = [
        {"title": "无关键", "content": "无关键"},
        {"title": "无关键2", "content": "无关键"},
        {"title": "无关键3", "content": "无关键"},
        {"title": "无关键4", "content": "无关键"},
        {"title": "无关键5", "content": "无关键"},
        {"title": "电池接口", "content": ""},
    ]
    assert ev.compute_recall_at_k(results, ["电池接口"], 5) is False


def test_compute_recall_at_k_no_hints(ev):
    results = [{"title": "x", "content": "y"}]
    assert ev.compute_recall_at_k(results, [], 5) is False


def test_compute_citation_precision_full(ev):
    results = [
        {"title": "电池接口", "content": ""},
        {"title": "固定螺钉", "content": ""},
    ]
    assert ev.compute_citation_precision(results, ["电池接口", "固定螺钉"]) == 1.0


def test_compute_citation_precision_partial(ev):
    results = [
        {"title": "电池接口", "content": ""},
        {"title": "无关键", "content": ""},
    ]
    assert ev.compute_citation_precision(results, ["电池接口"]) == 0.5


def test_compute_citation_precision_empty(ev):
    assert ev.compute_citation_precision([], ["电池接口"]) == 0.0


def test_aggregate_groups_by_type(ev):
    rows = [
        {"id": "q1", "type": "fact", "latency_ms": 100.0, "recall@5": True, "recall@10": True,
         "citation_precision": 1.0},
        {"id": "q2", "type": "fact", "latency_ms": 200.0, "recall@5": False, "recall@10": True,
         "citation_precision": 0.5},
        {"id": "q3", "type": "diagnostic", "latency_ms": 300.0, "recall@5": False, "recall@10": False,
         "citation_precision": 0.0, "error": "timeout"},
    ]
    m = ev.aggregate(rows)
    assert m["fact"]["count"] == 2
    assert m["fact"]["recall@5"] == 0.5
    assert m["diagnostic"]["count"] == 1
    assert m["diagnostic"]["errors"] == 1
    assert m["总体"]["count"] == 3
    assert m["总体"]["recall@10"] == 2 / 3


def test_aggregate_handles_empty(ev):
    m = ev.aggregate([])
    assert m["总体"] == {"count": 0}


def test_build_report_contains_required_sections(ev):
    rows = [
        {"id": "q1", "type": "fact", "question": "电池接口在哪?", "latency_ms": 100.0,
         "recall@5": True, "recall@10": True, "citation_precision": 1.0,
         "top_titles": ["电池接口"]},
    ]
    metrics = ev.aggregate(rows)
    report = ev.build_report(rows, metrics, 0.0, "http://127.0.0.1:8000", 10)
    assert "# 基线评测报告" in report
    assert "## 总体指标" in report
    assert "## 按题型分布" in report
    assert "## 失败样例" in report
    assert "## 后续建议" in report
    assert "Recall@5" in report


def test_build_report_flags_low_recall(ev):
    rows = [
        {"id": "q1", "type": "fact", "question": "?", "latency_ms": 100.0,
         "recall@5": False, "recall@10": False, "citation_precision": 0.0},
        {"id": "q2", "type": "fact", "question": "?", "latency_ms": 100.0,
         "recall@5": False, "recall@10": False, "citation_precision": 0.0},
    ]
    metrics = ev.aggregate(rows)
    report = ev.build_report(rows, metrics, 0.0, "http://x", 10)
    assert "向量召回或分词链路" in report
