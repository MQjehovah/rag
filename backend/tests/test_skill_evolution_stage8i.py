"""审计缺陷 6 反例测试：语义评审输出解析/校验/截断/记录（stage8i）。

覆盖：非法 JSON（含尾随垃圾/未闭合/非对象/fenced）、NameError 回归、item 类型/
未知/重复/缺失 check_id、伪造/越界位置（NONEXISTENT:999 拒绝）、引文不匹配、
合法“无位置”判定、长文本不静默截断（超限明确 invalid）、评审记录保留与
尝试计数/配置指纹、记录不出现在优化角色上下文。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.skill_evolution import model_review as mr, review_eval
from app.core.skill_evolution.grader_v2 import ST_PASS, ST_FAIL, ST_REVIEW


# ---------------------------------------------------------------------------
# 1) JSON 解析契约
# ---------------------------------------------------------------------------


def test_json_parser_strict():
    assert mr._parse_json_object('{"a":1}') == {"a": 1}
    assert mr._parse_json_object('```json\n{"a":1}\n```') == {"a": 1}
    # 尾随文本 → 拒绝（不允许括号扫描容忍）
    with pytest.raises(mr.ReviewerError):
        mr._parse_json_object('{"a":1} trailing junk')
    # 未闭合 / 非对象 / 空
    with pytest.raises(mr.ReviewerError):
        mr._parse_json_object('{"a":')
    with pytest.raises(mr.ReviewerError):
        mr._parse_json_object('[1,2]')
    with pytest.raises(mr.ReviewerError):
        mr._parse_json_object('')
    # fenced 块非法
    with pytest.raises(mr.ReviewerError):
        mr._parse_json_object('```json\n{"a":\n```')


def test_json_parse_no_nameerror_regression():
    """历史缺陷：except _json.JSONDecodeError 的 NameError 已移除。"""
    # 确保任何解析错误路径都抛 ReviewerError 而非 NameError
    for bad in ('not json', '{"a":1}xx', '```\n{"a"\n```'):
        try:
            mr._parse_json_object(bad)
            raise AssertionError("应拒绝")
        except mr.ReviewerError:
            pass


# ---------------------------------------------------------------------------
# 2) item 校验：类型 / 未知 / 重复 / 缺失
# ---------------------------------------------------------------------------


def _ok(check_id="semantic:claim:c1"):
    return {"identity": "r1", "items": [
        {"check_id": check_id, "verdict": "pass", "reason": "ok"}]}


def test_item_validation_types_unknown_duplicate_missing():
    ok = mr.check_review_result(_ok(),
                                expected_check_ids=["semantic:claim:c1"])
    assert ok["items"][0]["verdict"] == "pass"
    # 未知 check_id
    with pytest.raises(mr.ReviewerError, match="未知"):
        mr.check_review_result(_ok("other"),
                               expected_check_ids=["semantic:claim:c1"])
    # 重复
    dup = {"identity": "r", "items": [
        {"check_id": "a", "verdict": "pass", "reason": "x"},
        {"check_id": "a", "verdict": "fail", "reason": "x"}]}
    with pytest.raises(mr.ReviewerError, match="重复"):
        mr.check_review_result(dup, expected_check_ids=["a", "b"])
    # 缺失必需结果 → 不默认 pass
    with pytest.raises(mr.ReviewerError, match="缺少"):
        mr.check_review_result(_ok("a"), expected_check_ids=["a", "b"])
    # 类型错误
    bad_item = {"identity": "r", "items": [
        {"check_id": 5, "verdict": "pass", "reason": "x"}]}
    with pytest.raises(mr.ReviewerError):
        mr.check_review_result(bad_item, expected_check_ids=["a"])
    no_reason = {"identity": "r", "items": [
        {"check_id": "a", "verdict": "pass"}]}
    with pytest.raises(mr.ReviewerError):
        mr.check_review_result(no_reason, expected_check_ids=["a"])


# ---------------------------------------------------------------------------
# 3) 位置/引文契约
# ---------------------------------------------------------------------------


def test_locator_contract():
    sources = ["电压 220V。\n操作步骤：断电。", "巡检周期：每天。"]
    cand = [{"version_label": "unversioned",
             "content": "电压 220V。\n步骤 1：断电。"}]
    good = {"identity": "r", "items": [
        {"check_id": "a", "verdict": "pass", "reason": "x",
         "source_loc": "src:0#0-10",
         "quote": "电压 220V"}]}
    assert mr.check_review_result(good, expected_check_ids=["a"],
                                  sources=sources,
                                  candidate_sections=cand)["items"][0][
                                      "quote"] == "电压 220V"
    # NONEXISTENT / 越界 / 语法非法 / 引文不匹配
    for bad_loc in ("NONEXISTENT:999", "src:999#0-1", "src:0#90-95",
                    "src", "sec:0#0-99", "src:-1#0-1", "src:1#0-99"):
        bad = {"identity": "r", "items": [
            {"check_id": "a", "verdict": "pass", "reason": "x",
             "source_loc": bad_loc}]}
        with pytest.raises(mr.ReviewerError):
            mr.check_review_result(bad, expected_check_ids=["a"],
                                   sources=sources, candidate_sections=cand)
    # candidate 越界
    badc = {"identity": "r", "items": [
        {"check_id": "a", "verdict": "pass", "reason": "x",
         "cand_loc": "sec:7#0-1"}]}
    with pytest.raises(mr.ReviewerError):
        mr.check_review_result(badc, expected_check_ids=["a"],
                               sources=sources, candidate_sections=cand)
    # 引文不在范围 → 拒绝
    mismatch = {"identity": "r", "items": [
        {"check_id": "a", "verdict": "pass", "reason": "x",
         "source_loc": "src:0#0-10", "quote": "巡检周期"}]}
    with pytest.raises(mr.ReviewerError, match="引文"):
        mr.check_review_result(mismatch, expected_check_ids=["a"],
                               sources=sources, candidate_sections=cand)


def test_missing_content_judgement_allows_no_fabricated_locator():
    """“缺失内容”类判定允许无位置（不强制伪造输出位置）。"""
    ok = {"identity": "r", "items": [
        {"check_id": "semantic:claim:c1", "verdict": "needs_review",
         "reason": "正文未见目标值（可能是同义改写或真遗漏）"}]}
    out = mr.check_review_result(
        ok, expected_check_ids=["semantic:claim:c1"], sources=["x"],
        candidate_sections=[{"content": "任意"}] )
    assert out["items"][0]["verdict"] == "needs_review"


# ---------------------------------------------------------------------------
# 4) 不静默截断；评审记录保留与重试
# ---------------------------------------------------------------------------


def test_review_prompt_no_silent_truncation():
    big = "x" * 70_000
    with pytest.raises(mr.ReviewerInputLimitError, match="不静默截断"):
        mr.build_review_prompt(task_id="t", sources=[big],
                               candidate_output={"sections": []},
                               checks=[{"id": "c", "check": "y"}])
    # 默认上限内不截断：完整源文本必须出现在提示词
    small = "目标 40°C。" * 200
    prompt = mr.build_review_prompt(
        task_id="t", sources=[small],
        candidate_output={"sections": [{"content": "正文"}]},
        checks=[{"id": "c", "check": "y"}])
    # 完整源文本在提示词中；提示词以最后检查行收尾（未截断任何片段）
    assert small in prompt
    assert prompt.rstrip().endswith("（边界：）")


def test_review_records_and_retry_counts():
    class _FailingSender:
        def __init__(self):
            self.calls = 0

        def __call__(self, messages, context="", timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise mr.ReviewerError("瞬时失败")
            return _ok()["items"] and {"identity": "model:m@p",
                                       "items": [{"check_id": "semantic:c1",
                                                  "verdict": "pass",
                                                  "reason": "ok"}]}

    cfg = mr.ReviewerConfig(mode="real", model_id="m", prompt_version="p",
                            api_url="http://127.0.0.1:1/x",
                            api_key_present=True, timeout=5, retries=3)
    rev = mr.create_reviewer(cfg)
    rev.attach_sender(_FailingSender())
    out = rev.review(task_id="t", sources=["s"],
                     candidate_output={"sections": []},
                     checks=[{"id": "semantic:c1", "check": "x",
                              "detail": "y", "boundary": "z"}])
    assert out["items"][0]["verdict"] == "pass"
    assert rev.last_attempts == 2        # 失败 1 次 + 成功 1 次（同一预算重试）
    assert cfg.fingerprint()
    # 重试耗尽 → ReviewerError
    class _AlwaysFail:
        def __call__(self, *a, **k):
            raise mr.ReviewerError("持续失败")
    cfg1 = mr.ReviewerConfig(mode="real", model_id="m", prompt_version="p",
                             api_url="http://127.0.0.1:1/x",
                             api_key_present=True, timeout=5, retries=1)
    rev1 = mr.create_reviewer(cfg1)
    rev1.attach_sender(_AlwaysFail())
    with pytest.raises(mr.ReviewerError):
        rev1.review(task_id="t", sources=["s"],
                    candidate_output={"sections": []},
                    checks=[{"id": "c", "check": "x"}])
    assert rev1.last_attempts == 1 and rev1.last_error


def _tmp_dataset(tmp_path):
    src_dir = tmp_path / "sources"
    ref_dir = tmp_path / "references"
    src_dir.mkdir(parents=True)
    ref_dir.mkdir(parents=True)
    (src_dir / "a.md").write_text("供电电压 220V；温度上限 40°C。\n操作步骤：断电。",
                                  encoding="utf-8")
    (ref_dir / "ref.json").write_text(json.dumps({
        "t1": {"v2_spec": {"claims": [
            {"cid": "v", "subject": "供电电压", "predicate": "为",
             "object": "220", "unit": "V"},
            {"cid": "tl", "subject": "温度上限", "predicate": "≤",
             "object": 40, "unit": "°C"}],
            "coverage": [{"topic": "操作步骤"}], "conflicts": []}}},
        ensure_ascii=False), encoding="utf-8")


def test_review_records_stored_into_grade_result(tmp_path):
    _tmp_dataset(tmp_path)
    task = SimpleNamespace(task_id="t1", reference_ref="ref.json",
                           sources=(SimpleNamespace(file="a.md"),))
    cand = {"sections": [{"section_type": "facts",
                          "version_label": "unversioned",
                          "content": "供电电压 220V。\n操作步骤：断电。\n"
                                     "温度上限 40°C。"}]}
    from app.core.skill_evolution import grader_v2
    spec = review_eval.load_v2_spec(tmp_path, task)
    base = grader_v2.evaluate(cand, spec)
    ids = [it["id"] for it in base["items"] if it["status"] == ST_REVIEW]
    stub = mr.StubSemanticReviewer(
        {"t1": {"items": [{"check_id": i, "verdict": "pass", "reason": "r"}
                          for i in ids]}})
    out = review_eval.grade_task_v2(dataset_dir=tmp_path, task=task,
                                    candidate=cand, reviewer=stub)
    assert out["verdict"] == "pass"
    assert out["review_records"]
    rec = out["review_records"][0]
    assert rec["reviewer_identity"].startswith("model:stub@")
    # 评审记录不进入优化角色上下文（token 只存在于评估/记录模块）
    allowed = {"review_eval.py", "model_review.py", "review_store.py"}
    role_files = ("runner.py", "proposer.py", "maintainer.py", "adapter.py",
                  "real_adapters.py", "gating.py", "orchestrator.py")
    base_dir = Path(__file__).resolve().parent.parent / "app/core/skill_evolution"
    for fn in role_files:
        text = (base_dir / fn).read_text(encoding="utf-8")
        assert "review_records" not in text, f"{fn} 泄漏评审记录引用"
