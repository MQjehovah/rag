
"""T1.2 评测集数据格式与分布校验。

只测文件本身：字段完整性、类型分布、ID 唯一性。不依赖后端。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest


EVAL_PATH = Path(__file__).resolve().parent / "eval_dataset.jsonl"

REQUIRED_FIELDS = {
    "id",
    "question",
    "type",
    "expected_answer_hint",
    "expected_ko_type",
    "product",
    "version",
}

EXPECTED_TYPE_DISTRIBUTION = {
    "fact": 20,
    "deployment": 15,
    "diagnostic": 15,
    "version": 10,
    "decision": 10,
}


def _load_records():
    records = []
    with EVAL_PATH.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AssertionError(f"第 {line_no} 行不是合法 JSON: {exc}")
    return records


def test_eval_file_exists():
    assert EVAL_PATH.is_file(), f"评测集文件不存在：{EVAL_PATH}"


def test_eval_dataset_has_minimum_size():
    records = _load_records()
    assert len(records) >= 50, f"评测集应 ≥50 题，当前 {len(records)} 题"


def test_each_record_has_required_fields():
    records = _load_records()
    for rec in records:
        missing = REQUIRED_FIELDS - set(rec.keys())
        assert not missing, f"题目 {rec.get('id')} 缺少字段：{missing}"


def test_ids_are_unique():
    records = _load_records()
    ids = [r["id"] for r in records]
    duplicates = [k for k, v in Counter(ids).items() if v > 1]
    assert not duplicates, f"评测集 ID 重复：{duplicates}"


def test_type_distribution_meets_plan():
    records = _load_records()
    counter = Counter(r["type"] for r in records)
    for t, expected in EXPECTED_TYPE_DISTRIBUTION.items():
        actual = counter.get(t, 0)
        assert actual >= expected, (
            f"类型 `{t}` 至少 {expected} 题，实际 {actual} 题"
        )


def test_questions_are_non_empty():
    records = _load_records()
    for rec in records:
        assert rec["question"].strip(), f"题目 {rec['id']} question 为空"


def test_questions_are_distinct():
    records = _load_records()
    questions = [r["question"].strip() for r in records]
    duplicates = [k for k, v in Counter(questions).items() if v > 1]
    assert not duplicates, f"评测集中有重复问题：{duplicates}"


def test_expected_ko_type_aligns_with_type():
    records = _load_records()
    type_to_ko = {
        "fact": {"fact"},
        "deployment": {"procedure"},
        "diagnostic": {"diagnostic"},
        "version": {"fact"},
        "decision": {"decision"},
    }
    for rec in records:
        allowed = type_to_ko.get(rec["type"], set())
        assert rec["expected_ko_type"] in allowed, (
            f"题目 {rec['id']} type={rec['type']} 但 expected_ko_type={rec['expected_ko_type']}"
        )
