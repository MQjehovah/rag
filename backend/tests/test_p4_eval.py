"""P4 评测 harness 测试。

覆盖：加载评测集、evaluate_cases 指标计算、Recall@10、引用准确率、
no_answer 不计入 recall、检索异常降级。
"""
from __future__ import annotations

import json

from app.core.eval_retrieval import evaluate_cases, load_eval_set
from app.core.retrieval.candidates import RetrievalCandidate


def _cand(content, page_id=None):
    return RetrievalCandidate(
        candidate_type="chunk", candidate_id="c", content=content, source_page_id=page_id,
    )


def _eval_set():
    return {
        "name": "test",
        "cases": [
            {"id": "c1", "category": "fact", "question": "q1", "answerable": True,
             "expected_keywords": ["电池"], "expected_source_fragment": "手册"},
            {"id": "c2", "category": "no_answer", "question": "q2", "answerable": False,
             "expected_keywords": [], "expected_source_fragment": None},
        ],
    }


def test_load_eval_set(tmp_path):
    p = tmp_path / "eval.json"
    p.write_text(json.dumps({"name": "t", "cases": []}, ensure_ascii=False), encoding="utf-8")
    data = load_eval_set(p)
    assert data["name"] == "t"


def test_evaluate_cases_recall():
    eval_set = _eval_set()
    # c1 命中电池，c2 no_answer
    def retrieve_fn(question):
        return [_cand("Titan 电池充电步骤")] if question == "q1" else []
    report = evaluate_cases(eval_set, retrieve_fn)
    # 1 个 answerable case 命中 → recall 100%
    assert report.recall_at_10 == 1.0
    assert report.results[0].keyword_hit is True
    assert report.results[1].recall_hit is False  # no_answer 不计入


def test_evaluate_cases_miss():
    eval_set = _eval_set()
    def retrieve_fn(question):
        return [_cand("无关内容")]
    report = evaluate_cases(eval_set, retrieve_fn)
    assert report.recall_at_10 == 0.0


def test_citation_accuracy():
    eval_set = {
        "name": "t",
        "cases": [
            {"id": "c1", "category": "fact", "question": "q", "answerable": True,
             "expected_keywords": ["电池"], "expected_source_fragment": "用户手册"},
        ],
    }
    def retrieve_fn(question):
        return [_cand("电池充电", page_id="p1")]
    def title_resolver(pid):
        return "Titan810 用户手册" if pid == "p1" else ""
    report = evaluate_cases(eval_set, retrieve_fn, title_resolver)
    assert report.citation_accuracy == 1.0  # 关键词命中且来源匹配


def test_retrieve_exception_degrades():
    eval_set = _eval_set()
    def retrieve_fn(question):
        raise RuntimeError("down")
    report = evaluate_cases(eval_set, retrieve_fn)
    assert report.results[0].retrieved_count == 0
    assert report.recall_at_10 == 0.0
