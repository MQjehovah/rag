"""Grader v2 工程接线验收（离线；不触发真实模型/供应商）。

覆盖：任务级 v2 评估入口（确定性 + 语义评审 resolution）、pass/fail/needs_review/
invalid 语义、评审器协议（HTTP stub：合法/非法/截断/证据引用缺失）、参考答案与私有
评审反馈不外泄（评审输入只含资料+输出+检查）、评审模型独立显式配置（绝不沿用执行
模型）、注册表状态与未校准自动晋升被服务端阻止、预算计数入口。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core.skill_evolution import grader_registry as greg
from app.core.skill_evolution import grader_v2, model_review, review_eval


def _task(root: Path, task_id: str = "t1"):
    return SimpleNamespace(task_id=task_id, reference_ref="ref.json",
                           sources=(SimpleNamespace(file="a.md"),))


def _dataset(root: Path, *, claims_marker: str | None = None):
    src_dir = root / "sources"
    ref_dir = root / "references"
    src_dir.mkdir(parents=True)
    ref_dir.mkdir(parents=True)
    source = "电压 220V；温度 25°C；内容编号 X1。\n操作步骤：安装本体。"
    if claims_marker:
        source = claims_marker
    (src_dir / "a.md").write_text(source, encoding="utf-8")
    ref = {
        "t1": {
            "v2_spec": {
                "claims": [{"cid": "c1", "subject": "电压", "predicate": "为",
                            "object": "220", "unit": "V"},
                           {"cid": "c2", "subject": "温度", "predicate": "为",
                            "object": "25", "unit": "°C"}],
                "coverage": [{"topic": "操作步骤"}],
            },
        },
    }
    (ref_dir / "ref.json").write_text(json.dumps(ref, ensure_ascii=False),
                                      encoding="utf-8")
    return source


def _candidate(text: str) -> dict:
    return {"sections": [{"section_type": "facts",
                          "version_label": "unversioned", "content": text}]}


def _resolve_all_first():
    """一次性取得任务未决检查 id 的辅助（测试两段式）。"""
    import tempfile
    root = Path(tempfile.mkdtemp())
    _dataset(root)
    task = _task(root)
    cand = _candidate("电压 220V；温度 25°C；安装本体。\n操作步骤：先断电。")
    spec = review_eval.load_v2_spec(root, task)
    base = grader_v2.evaluate(cand, spec)
    ids = [it["id"] for it in base["items"] if it["status"] == "needs_review"]
    return ids


class _RecorderReviewer(model_review.StubSemanticReviewer):
    def __init__(self, script, prompt_version="stub-v1"):
        super().__init__(script, prompt_version=prompt_version)
        self.calls = []

    def review(self, *, task_id, sources, candidate_output, checks):
        self.calls.append({
            "task_id": task_id, "sources": list(sources),
            "candidate_output": candidate_output, "checks": list(checks),
        })
        return super().review(task_id=task_id, sources=sources,
                              candidate_output=candidate_output, checks=checks)


def _pass_script(ids, task_id="t1"):
    return {task_id: {"items": [
        {"check_id": i, "verdict": "pass", "reason": "与来源一致",
         "source_evidence_loc": "a.md:1", "candidate_loc": "正文"}
        for i in ids]}}


def test_grade_task_v2_pass_and_no_leak(tmp_path):
    _dataset(tmp_path)
    task = _task(tmp_path)
    cand = _candidate("电压 220V；温度 25°C。\n操作步骤：先断电再安装。\n内容编号 X1。")
    ids = _resolve_all_first()
    rec = _RecorderReviewer(_pass_script(ids))
    out = review_eval.grade_task_v2(dataset_dir=tmp_path, task=task,
                                    candidate=cand, reviewer=rec)
    assert out["verdict"] == "pass"
    assert out["promotable"] is True
    assert out["review_requests"] == 1
    assert "stub" in (out["reviewer_identity"] or "")
    call = rec.calls[0]
    assert call["task_id"] == "t1"
    blob = json.dumps(call, ensure_ascii=False)
    # 参考答案/私有反馈不外泄：评审只见 资料+输出+检查
    assert "best_score" not in blob and "baseline" not in blob
    assert "参考" not in json.dumps(call["checks"], ensure_ascii=False)


def test_grade_task_v2_deterministic_fail(tmp_path):
    _dataset(tmp_path)
    task = _task(tmp_path)
    cand = _candidate("电压 380V。\n操作步骤：拆箱。")  # 期望 220V 缺失
    ids = _resolve_all_first()
    out = review_eval.grade_task_v2(
        dataset_dir=tmp_path, task=task, candidate=cand,
        reviewer=model_review.StubSemanticReviewer(_pass_script(ids)))
    assert out["verdict"] == "fail"
    assert out["promotable"] is False


def test_grade_task_v2_needs_review_unresolved(tmp_path):
    _dataset(tmp_path)
    task = _task(tmp_path)
    cand = _candidate("电压 220V；温度 25°C。\n操作步骤：先断电。")
    ids = _resolve_all_first()
    script = {"t1": {"items": [{"check_id": i, "verdict": "needs_review",
                                "reason": "需人工复核"}
                               for i in ids]}}
    out = review_eval.grade_task_v2(
        dataset_dir=tmp_path, task=task, candidate=cand,
        reviewer=model_review.StubSemanticReviewer(script))
    assert out["verdict"] == "needs_review"
    assert out["promotable"] is False
    assert out["unresolved_ids"]


def test_grade_task_v2_reviewer_failure_is_invalid_not_fallback(tmp_path):
    _dataset(tmp_path)
    task = _task(tmp_path)
    cand = _candidate("电压 220V；温度 25°C。\n操作步骤：先断电。")
    out = review_eval.grade_task_v2(
        dataset_dir=tmp_path, task=task, candidate=cand, reviewer=None)
    # 无评审器 → 语义项未决 → needs_review（不通过、不缩分母）
    assert out["verdict"] in ("needs_review",)
    # 评审器抛错（非法结构/服务失败）→ invalid
    def _boom(*a, **k):
        raise model_review.ReviewerError("评审服务不可用")
    class _Boom(model_review.SemanticReviewer):
        identity = "model:boom@v1"
        def review(self, **k):
            return _boom()
    out2 = review_eval.grade_task_v2(
        dataset_dir=tmp_path, task=task, candidate=cand,
        reviewer=_Boom())
    assert out2["verdict"] == "invalid"
    assert "不回退 v1" in out2["evaluation_invalid"]
    assert out2["promotable"] is False


def test_v2_spec_missing_rejects_no_fallback(tmp_path):
    task = _task(tmp_path)
    ref_dir = tmp_path / "references"
    ref_dir.mkdir(parents=True)
    (ref_dir / "ref.json").write_text(json.dumps(
        {"t1": {"expected_points": []}}), encoding="utf-8")  # 只有 v1 形状
    with pytest.raises(review_eval.V2EvalError) as ei:
        review_eval.grade_task_v2(
            dataset_dir=tmp_path, task=task,
            candidate=_candidate("任意内容"), reviewer=None)
    assert "不自动退回 v1" in str(ei.value)


class _ReviewStub(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _ReviewStub.calls.append(body)
        content = json.dumps({
            "identity": "review:stub-http",
            "items": [{"check_id": "semantic:claim:c1", "verdict": "pass",
                       "reason": "核对一致", "source_evidence_loc": "a.md:1",
                       "candidate_loc": "正文"}],
        }, ensure_ascii=False)
        resp = json.dumps({"choices": [{"message": {"content": content}}]},
                          ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):
        pass


def test_chat_reviewer_real_config_and_budget_wrap(monkeypatch, tmp_path):
    _ReviewStub.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ReviewStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    monkeypatch.setattr(settings, "llm_api_key", "stub-key")
    # 受控端点名单（默认 = llm/reviewer 配置 host）：本测试 stub 需注册为 reviewer
    # 端点，否则发送前被 endpoint 允许名单拒绝（任意地址不得搭配全局凭据）。
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url",
                        f"http://127.0.0.1:{port}/chat")
    try:
        # 独立显式配置；缺 model/api/prompt → fail closed，绝不沿用执行模型
        cfg = model_review.ReviewerConfig(mode="real")
        with pytest.raises(model_review.ReviewerError) as ei:
            model_review.create_reviewer(cfg)
        assert "model_id" in str(ei.value)
        cfg2 = model_review.ReviewerConfig(
            mode="real", model_id="reviewer-m", prompt_version="pv1",
            api_url=f"http://127.0.0.1:{port}/chat")
        rev = model_review.create_reviewer(cfg2)
        assert rev.identity == "model:reviewer-m@pv1"
        rev.attach_sender(rev._send_http)
        out = rev.review(task_id="t1", sources=["电压 220V"],
                         candidate_output={"sections": []},
                         checks=[{"id": "semantic:claim:c1", "check": "x",
                                  "detail": "y", "boundary": "z"}])
        assert out["items"][0]["verdict"] == "pass"
        sent = _ReviewStub.calls[0]
        assert sent["model"] == "reviewer-m"   # 独立于执行模型
        assert sent.get("messages") and sent["messages"][0]["role"] == "user"
    finally:
        srv.shutdown()
        srv.server_close()


def test_registry_states_server_enforced(monkeypatch):
    v1 = greg.grader_state("wiki-default-grader/v1")
    assert v1["calibration"] == "not_applicable"
    assert v1["allow_business_promotion"] is True
    v2 = greg.grader_state(greg.GRADER_V2)
    assert v2["ready"] is True                    # 工程可运行
    assert v2["calibration"] == "engineering_only"  # 汇总态未校准
    assert v2["allow_business_promotion"] is False
    # 两个独立状态：人工标签已批准（2026-09-08）与 真实校准（相互不代替）
    assert v2["labels_approved"] is True
    assert v2["real_calibration"] is False
    assert "labels_approved" in greg.grader_state(greg.GRADER_V2)
    assert "real_calibration" in greg.grader_state(greg.GRADER_V2)
    # 服务端阻止（非文档提示）
    with pytest.raises(greg.GraderRegistryError) as ei:
        greg.require_business_promotable(greg.GRADER_V2)
    msg = str(ei.value)
    assert "独立状态" in msg and "阻止" in msg
    # 只建立人工标签（参考标准）仍不能解除晋升限制（真实校准缺失）
    saved = greg._REGISTRY[greg.GRADER_V2]
    try:
        patched = {**saved, "labels_approved": True, "real_calibration": False}
        greg._REGISTRY[greg.GRADER_V2] = patched
        with pytest.raises(greg.GraderRegistryError):
            greg.require_business_promotable(greg.GRADER_V2)
    finally:
        greg._REGISTRY[greg.GRADER_V2] = saved
    greg.require_business_promotable("wiki-default-grader/v1")  # 不抛
    # 执行模型配置存在也不影响评审独立校验（fail closed）
    monkeypatch.setattr(settings, "llm_model", "executor-model")
    cfg = model_review.ReviewerConfig(mode="real", prompt_version="pv",
                                      api_url="http://x")
    with pytest.raises(model_review.ReviewerError):
        model_review.create_reviewer(cfg)  # model_id 仍缺 → 拒绝
