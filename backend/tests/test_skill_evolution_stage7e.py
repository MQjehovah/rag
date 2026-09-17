"""Grader v2（prototype-2）纠偏验收：保守汇总契约与能力边界（通用，无样例特判）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.core.skill_evolution.grader_v2 import (
    ClaimSpec,
    CoverageItem,
    DeclaredConflict,
    V2Spec,
    evaluate,
)

BACKEND = Path(__file__).resolve().parent.parent
DEV = BACKEND / "eval/wiki_evolution/calibration/v2-dev.json"
RESULTS = BACKEND / "eval/wiki_evolution/calibration/v2-dev-results.json"
ACC = BACKEND / "eval/wiki_evolution/calibration/v2-acceptance-review.json"


def _spec(task_id="generic-1", claims=(), coverage=(), conflicts=()):
    return V2Spec(task_id=task_id, claims=list(claims), coverage=list(coverage),
                  conflicts=list(conflicts))


def test_module_has_no_sample_special_casing():
    src = (BACKEND / "app/core/skill_evolution/grader_v2.py").read_text(
        encoding="utf-8")
    for token in ('"S1"', '"S2"', '"S3"', "'S1'", "sample_id"):
        assert token not in src
    assert "resolutions" in src  # 通用外部判定通道


def test_aggregation_pass_fail_review_invalid():
    # 无解析 → 全 pass 数值也有语义待审 → needs_review，无晋升主分数
    spec = _spec(claims=[ClaimSpec("c1", "电压", "等于", 60, unit="V")],
                 coverage=[CoverageItem("扭矩")])
    cand = {"sections": [{"content": "电压 60 V；扭矩复核完成。",
                          "section_type": "facts"}]}
    r = evaluate(cand, spec)
    assert r["verdict"] == "needs_review" and r["promotable"] is False
    assert r["required_total"] == 2
    # 人工/模型对全部 C 级项给 pass → pass 且可晋升
    resolutions = {it["id"]: "pass" for it in r["items"]
                   if it["status"] == "needs_review"}
    r2 = evaluate(cand, spec, resolutions=resolutions)
    assert r2["verdict"] == "pass" and r2["promotable"] is True
    # 任一 C 级解析为 fail → 任务 fail（不因 pending 变 pass、不从分母剔除）
    fail_one = {k: ("fail" if k.startswith("semantic:c1") else "pass")
                for k in resolutions}
    r3 = evaluate(cand, spec, resolutions=fail_one)
    assert r3["verdict"] == "fail" and r3["promotable"] is False
    assert r3["required_total"] == 2   # 分母不缩水
    # 无 sections → invalid
    r4 = evaluate({}, spec)
    assert r4["verdict"] == "invalid" and r4["promotable"] is False


def test_numeric_and_unit_equivalence_boundaries():
    # 单位错误（60 mV ≠ 60 V）→ A 级可判 fail；注明归属语义未校验
    spec = _spec(claims=[ClaimSpec("c1", "电压限值", "等于", 60, unit="V")])
    r = evaluate({"sections": [{"content": "电压限值 60 mV。",
                                "section_type": "facts"}]}, spec)
    assert r["verdict"] == "fail" and r["promotable"] is False
    claim_item = next(it for it in r["items"] if it["id"] == "claim:c1")
    assert "归属" in claim_item["boundary"]  # 不声称证明主体/来源归属
    # 数值等价（0.10 mm == 0.1 mm，单位归一化）
    spec2 = _spec(claims=[ClaimSpec("c1", "过盈", "等于", 0.1, unit="mm")])
    r2 = evaluate({"sections": [{"content": "过盈 0.10 mm。",
                                 "section_type": "facts"}]}, spec2)
    assert r2["verdict"] == "needs_review"   # 格式通过、语义待审


def test_same_param_multiple_values_not_auto_conflict():
    text = {"sections": [{"content": "平原 134°C 维持 4 分钟；高海拔 134°C 维持 6 分钟。",
                          "section_type": "facts"}]}
    claim = ClaimSpec("c1", "灭菌维持", "等于", 4, unit="min")
    # 条件未声明相同 → 不自动判矛盾（不同条件下同参数可合法不同值）
    spec_unknown = _spec(claims=[claim], coverage=[CoverageItem("高海拔")],
                         conflicts=[DeclaredConflict("c1", same_condition=False)])
    r = evaluate(text, spec_unknown)
    assert not any(it["id"].startswith("conflict:c1") and it["status"] == "fail"
                   for it in r["items"])
    # 明确声明同条件且出现多值 → 判矛盾
    spec_same = _spec(claims=[claim], conflicts=[
        DeclaredConflict("c1", same_condition=True)])
    r2 = evaluate(text, spec_same)
    conflict = next(it for it in r2["items"] if it["id"] == "conflict:c1")
    assert conflict["status"] == "fail"


def test_negation_and_coverage_do_not_fake_semantics():
    # 关键词齐全但否定相反：格式项 pass，但语义维度 → needs_review（不误通过）
    spec = _spec(claims=[ClaimSpec("c1", "作业", "需", "断电", unit="")],
                 coverage=[CoverageItem("操作步骤")])
    cand = {"sections": [{"content": "前置条件：断电并验电（不应当执行）。"
                                       "操作步骤：不执行复核。", "section_type": "facts"}]}
    r = evaluate(cand, spec)
    assert r["verdict"] == "needs_review" and r["promotable"] is False
    # 覆盖短语“已提及”只给提及级 pass；实质覆盖仍需语义待审
    assert any(it["id"] == "coverage:操作步骤" and it["status"] == "pass"
               for it in r["items"])
    assert any(it["id"].startswith("semantic:coverage:") for it in r["items"])


def test_calibration_dual_column_report_and_acceptance_pending():
    sys.path.insert(0, str(BACKEND / "tools"))
    from run_calibration_v2 import run
    report = run()
    tally = report["tally"]
    # v1（真实引擎+代理参考）缺陷复现归因 v1；v2 无误通过/误拒绝（保守待审）
    assert report["samples_total"] == 12
    assert tally.get("v1误通过（缺陷复现）", 0) >= 6
    assert tally.get("v1误拒绝", 0) >= 1
    assert tally.get("v1正确拒绝", 0) >= 4
    assert "v2误通过" not in tally and "v2误拒绝" not in tally
    assert tally.get("v2待审（保守，非误通过）", 0) >= 8
    saved = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert saved["tally"] == tally
    # 统一指标与“零错误”声明红线
    m = report["metrics"]
    assert m["v2_mispass"] == 0 and m["v2_misreject"] == 0
    assert m["failure_rate"] == 0.0 and 0 < m["needs_review_rate"] < 1
    assert report["no_quality_claim"]
    for sid, row in saved["rows"].items():
        assert row["v1_engine"].startswith("real:")
        assert row["v1_reference_proxy"] is True and row["v1_limitation"]
    # 验收样例集独立、未宣称已完成人工验收
    acc = json.loads(ACC.read_text(encoding="utf-8"))
    assert acc["status"] == "human_labels_approved"  # 2026-09-08 人工标签批准
    assert acc["used_for_development"] is False
    dev_text = json.dumps(json.loads(DEV.read_text(encoding="utf-8")),
                          ensure_ascii=False)
    acc_text = json.dumps(acc, ensure_ascii=False)
    # 验收样例不复用开发集文本（抽样整段不重复即视为不同编写）
    assert acc_text not in dev_text
