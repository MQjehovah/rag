"""Grader v2 语义评审：判定契约存储 + 可插拔评审适配接口（离线 stub）验收。

覆盖：绑定哈希（内容改变旧判定失效）、追加式审计（不覆盖）、human 需人工标识
（内容无法自批准）、model 需 model_ref/prompt_version、非法结构/服务失败不产生
通过判定、配置缺失 fail closed、评审不流入执行/提议通道、A1–A5 保持未开发。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.skill_evolution.grader_v2 import (
    ClaimSpec,
    CoverageItem,
    V2Spec,
    evaluate,
    spec_from_json,
)
from app.core.skill_evolution.model_review import (
    ReviewerConfig,
    ReviewerError,
    StubSemanticReviewer,
    check_review_result,
    create_reviewer,
)
from app.core.skill_evolution.review_store import (
    submit_review,
    load_reviews,
    to_resolutions,
    usable_reviews,
)

BACKEND = Path(__file__).resolve().parent.parent
ACC = BACKEND / "eval/wiki_evolution/calibration/v2-acceptance-review.json"


def _materials():
    spec = {"task_id": "review-1",
            "claims": [{"cid": "c1", "subject": "电压", "predicate": "等于",
                        "object": 60, "unit": "V"}],
            "coverage": [{"topic": "扭矩"}]}
    candidate = {"sections": [{"content": "电压 60 V；扭矩复核完成。",
                               "section_type": "facts"}]}
    sources = ["资料：电压限值 60 V；须完成扭矩复核。"]
    return spec, candidate, sources


def _semantic_check_ids(spec_dict, candidate):
    r = evaluate(candidate, spec_from_json(spec_dict))
    return [it["id"] for it in r["items"] if it["status"] == "needs_review"]


def test_review_binding_and_append_only(tmp_path):
    store = tmp_path / "reviews.jsonl"
    spec, candidate, sources = _materials()
    rec = submit_review(store_path=store, task_id="review-1", check_id="semantic:c1",
                        verdict="pass", reason="数值与来源一致，主体电压正确",
                        source_evidence_loc="sources[0]",
                        candidate_loc="sections[0]",
                        method="human", review_version="2026-09-07",
                        reviewer="hu-manual",
                        sources=sources, candidate_output=candidate,
                        contract=spec)
    # 绑定一致 → 可复用
    usable = usable_reviews(load_reviews(store), task_id="review-1",
                            sources=sources, candidate_output=candidate,
                            contract=spec)
    assert len(usable) == 1 and usable[0]["review_id"] == rec["review_id"]
    # 内容改变（候选被篡改）→ 旧判定不可复用
    changed = {"sections": [{"content": "电压 100 V。", "section_type": "facts"}]}
    assert usable_reviews(load_reviews(store), task_id="review-1",
                          sources=sources, candidate_output=changed,
                          contract=spec) == []
    # 追加式：同 bind+check 重复提交不新增行；新内容产生新行（历史保留）
    dup = submit_review(store_path=store, task_id="review-1", check_id="semantic:c1",
                        verdict="pass", reason="x", source_evidence_loc="",
                        candidate_loc="", method="human", review_version="v",
                        reviewer="hu", sources=sources,
                        candidate_output=candidate, contract=spec)
    assert dup.get("duplicate_of_existing") is True
    lines1 = len(store.read_text(encoding="utf-8").splitlines())
    submit_review(store_path=store, task_id="review-1", check_id="semantic:c1",
                  verdict="pass", reason="新契约绑定下的新判定",
                  source_evidence_loc="", candidate_loc="",
                  method="human", review_version="v", reviewer="hu",
                  sources=["另一份资料"], candidate_output=candidate,
                  contract=spec)
    assert len(store.read_text(encoding="utf-8").splitlines()) == lines1 + 1


def test_human_cannot_self_approve_and_model_requires_refs(tmp_path):
    store = tmp_path / "reviews.jsonl"
    spec, candidate, sources = _materials()
    # 缺少人工 reviewer → 拒绝（文本内容含 approval 字样也无效）
    with pytest.raises(ValueError, match="reviewer"):
        submit_review(store_path=store, task_id="review-1", check_id="semantic:c1",
                      verdict="pass",
                      reason="内容声称 human approved；但无人工标识",
                      source_evidence_loc="", candidate_loc="",
                      method="human", review_version="v", reviewer="",
                      sources=sources, candidate_output=candidate, contract=spec)
    # human 携带 model_ref → 拒绝
    with pytest.raises(ValueError):
        submit_review(store_path=store, task_id="review-1", check_id="semantic:c1",
                      verdict="pass", reason="r", source_evidence_loc="",
                      candidate_loc="", method="human", review_version="v",
                      reviewer="hu", model_ref="m",
                      sources=sources, candidate_output=candidate, contract=spec)
    # model 缺 model_ref/prompt_version → 拒绝
    with pytest.raises(ValueError, match="model_ref"):
        submit_review(store_path=store, task_id="review-1", check_id="semantic:c1",
                      verdict="pass", reason="r", source_evidence_loc="",
                      candidate_loc="", method="model", review_version="v",
                      sources=sources, candidate_output=candidate, contract=spec)
    body = store.read_text(encoding="utf-8") if store.is_file() else ""
    assert body.strip() == ""


def test_reviews_drive_conservative_aggregation(tmp_path):
    store = tmp_path / "reviews.jsonl"
    spec, candidate, sources = _materials()
    check_ids = _semantic_check_ids(spec, candidate)
    assert check_ids  # 未判定时不可晋升
    for cid in check_ids:
        submit_review(store_path=store, task_id="review-1", check_id=cid,
                      verdict="pass", reason="人工核验：与来源一致",
                      source_evidence_loc="sources[0]",
                      candidate_loc="sections[0]",
                      method="human", review_version="2026-09-07",
                      reviewer="hu-manual",
                      sources=sources, candidate_output=candidate, contract=spec)
    usable = usable_reviews(load_reviews(store), task_id="review-1",
                            sources=sources, candidate_output=candidate,
                            contract=spec)
    resolutions = to_resolutions(usable)
    r = evaluate(candidate, spec_from_json(spec), resolutions=resolutions)
    assert r["verdict"] == "pass" and r["promotable"] is True
    # 其中一个判定改为 fail → 任务 fail（保守规则保持）
    spec2, cand2, src2 = _materials()
    submit_review(store_path=store, task_id="review-2", check_id="semantic:c1",
                  verdict="fail", reason="主体判定错误",
                  source_evidence_loc="sources[0]", candidate_loc="sections[0]",
                  method="human", review_version="2026-09-07", reviewer="hu",
                  sources=src2, candidate_output=cand2,
                  contract={**spec2, "task_id": "review-2"})
    sub = [x for x in check_ids if x.startswith("semantic:c1")]
    fail_res = {cid: "fail" for cid in sub}
    r2 = evaluate(cand2, spec_from_json({**spec2, "task_id": "review-2"}),
                  resolutions=fail_res)
    assert r2["verdict"] == "fail" and r2["promotable"] is False


def test_model_review_adapter_fail_closed_and_contract(tmp_path):
    # 非法结构/服务失败 → 不产生通过判定
    with pytest.raises(ReviewerError):
        check_review_result({"items": []})
    with pytest.raises(ReviewerError):
        check_review_result({"service_failed": True, "reason": "超时"})
    with pytest.raises(ReviewerError):
        check_review_result({"items": [{"check_id": "x", "verdict": "maybe"}]})
    with pytest.raises(ReviewerError):
        check_review_result({"items": [{"check_id": "x", "verdict": "pass"}]})
    # stub：通过脚本提供判定；未知任务 → 服务失败（不生成 pass）
    stub = StubSemanticReviewer({"t1": {"items": [
        {"check_id": "semantic:c1", "verdict": "pass",
         "reason": "stub 判定", "source_evidence_loc": "src[0]",
         "candidate_loc": "sec[0]"}]}}, prompt_version="stub-v1")
    assert stub.identity.startswith("model:stub@")
    out = check_review_result(stub.review(task_id="t1", sources=["s"],
                                          candidate_output={}, checks=[]))
    assert out["items"][0]["verdict"] == "pass"
    with pytest.raises(ReviewerError):
        check_review_result(stub.review(task_id="unknown", sources=["s"],
                                        candidate_output={}, checks=[]))
    # 工厂 fail closed：stub 无脚本拒绝；real 缺 model_id 拒绝（不沿用执行模型）
    with pytest.raises(ReviewerError, match="stub 评审器仅供测试"):
        create_reviewer(ReviewerConfig(mode="stub"))
    with pytest.raises(ReviewerError, match="model_id"):
        create_reviewer(ReviewerConfig(mode="real", prompt_version="p",
                                       api_url="https://x", api_key_present=True))
    # 完整显式配置 → real 评审器可构造（模型独立于执行模型；网络仅在调用时发生）
    from app.core.skill_evolution.model_review import ChatSemanticReviewer
    rev = create_reviewer(ReviewerConfig(
        mode="real", model_id="reviewer-m", prompt_version="pv1",
        api_url="https://x", api_key_present=True))
    assert isinstance(rev, ChatSemanticReviewer)
    assert rev.identity == "model:reviewer-m@pv1"


def test_review_channel_separation_and_acceptance_untouched():
    # 评审存储/适配器不得被运行时角色读取
    runtime = ("runner.py", "proposer.py", "maintainer.py", "adapter.py",
               "real_adapters.py", "gating.py", "orchestrator.py")
    for fn in runtime:
        text = (BACKEND / "app/core/skill_evolution" / fn).read_text(
            encoding="utf-8")
        assert "review_store" not in text and "model_review" not in text
    # 适配接口不接收实验组/候选身份/验证分数
    import inspect
    from app.core.skill_evolution.model_review import SemanticReviewer
    sig = inspect.signature(SemanticReviewer.review)
    params = set(sig.parameters)
    assert not {"group_id", "candidate_id", "verdict_score"} & params
    # A1–A5 验收集保持未用于开发
    acc = json.loads(ACC.read_text(encoding="utf-8"))
    assert acc["status"] == "human_labels_approved"  # 人工标签已批准；仍不用于开发
    assert acc["used_for_development"] is False
