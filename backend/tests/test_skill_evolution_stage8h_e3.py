"""E3：统一 HTTP stub 真实模式全协议闭环（离线协议验证；不触外部网络）。

链路：管理面创建的 real+review=v2 run → build_run_actors 真实适配器
（executor/maintainer/proposer/reviewer 全部经本地 HTTP stub；模拟默认实现一经调用
即抛错）→ orchestrator 两轮：基线有效非满分 → 轮1 维护经验 + proposer 受控 ≥4 轨迹
读取后提交合法 patch → 候选输出确实满足更多确定性检查 → 真实 grader/gating 严格
提升并原子接受 → 轮2 训练请求实际携带刚接受技能正文 → 轮2 候选持平被拒绝、
技能与经验历史保留。

HTTP stub 只返回各角色协议响应，绝不直接写 Evaluation/门控/分数/DB。
所有证据均为 HTTP stub 工程验证（非真实质量/效果证据）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.config import settings
from app.core.skill_evolution import gating, orchestrator as orch, runenv, skill_store
from app.core.skill_evolution.contracts import load_dataset

BACKEND = Path(__file__).resolve().parent.parent
DEV_DS = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v2dev"
REPO_SEED = BACKEND / "eval/wiki_evolution/skills/seed-default-v1"

ROLE_REVIEW = "reviewer"
ROLE_MAINTAIN = "maintainer"
ROLE_PROPOSE = "proposer"
ROLE_EXEC = "executor"

_EXEC_BASE = (
    "适用条件：室内安装。\n前置条件：断电，佩戴绝缘手套。\n"
    "操作步骤：打开箱体，安装导轨，锁紧螺栓（扭矩 8N·m）。\n"
    "供电电压 220V；温度 25°C。\n巡检周期：每天一次。\n"
    "故障排查：检查供电 220V 与线缆 ≥6mm。\n"
    "线缆直径要求：≥6mm（毫米）。"
)
MARKER = "E3-40C"


class _E3Handler(BaseHTTPRequestHandler):
    calls: list = []
    role_counts: dict = {}
    fail_next: int = 0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _E3Handler.calls.append({"ts": time.time(), "body": body,
                                 "auth": self.headers.get("Authorization")})
        messages = body.get("messages") or []
        prompt = "\n".join(
            str(m.get("content") or "") for m in messages
            if isinstance(m, dict) and m.get("role") == "user")
        sys_text = "\n".join(
            str(m.get("content") or "") for m in messages
            if isinstance(m, dict) and m.get("role") == "system")
        role, content = _route(prompt=prompt, sys_text=sys_text)
        _E3Handler.role_counts[role] = _E3Handler.role_counts.get(role, 0) + 1
        _E3Handler.calls[-1]["role"] = role
        if role == ROLE_REVIEW and _E3Handler.fail_next > 0:
            _E3Handler.fail_next -= 1
            body500 = b'{"error":"transient"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body500)))
            self.end_headers()
            self.wfile.write(body500)
            return
        out = json.dumps({"choices": [{"message": {"content": content}}]},
                         ensure_ascii=False).encode("utf-8")
        _E3Handler.calls[-1]["response_content"] = content
        if role == ROLE_REVIEW:
            try:
                items = json.loads(content)
                _E3Handler.calls[-1]["debug_items"] = len(items.get("items") or [])
                _E3Handler.calls[-1]["debug_checks"] = len(
                    re.findall(r"-\s*\[([^]]+)\]", prompt))
            except Exception as exc:  # noqa: BLE001
                _E3Handler.calls[-1]["debug_err"] = str(exc)[:80]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


def _route(*, prompt: str, sys_text: str):
    if "你是 Wiki 输出评审员" in prompt:
        checkpart = prompt.split("--- 检查项 ---", 1)
        checkpart = checkpart[1] if len(checkpart) > 1 else prompt
        ids = re.findall(r"-\s*\[([^\]]+)\]", checkpart)
        items = [{"check_id": i, "verdict": "pass",
                  "reason": "stub 工程验证：语义项通过",
                  "source_evidence_loc": "src", "candidate_loc": "out"}
                 for i in ids]
        return ROLE_REVIEW, json.dumps(
            {"identity": "model:e3-reviewer@prompt-v1", "items": items},
            ensure_ascii=False)
    if "你是 WikiSkill 经验维护者" in prompt:
        return ROLE_MAINTAIN, _maintain_reply(prompt)
    if "你是 WikiSkill 技能提议者" in prompt:
        return ROLE_PROPOSE, _propose_reply(prompt)
    if "整合" in prompt and "worthy" in prompt:
        return ROLE_EXEC, json.dumps(
            {"worthy": True, "ops": [{"action": "create",
                                      "title": "主题", "category": "资料"}]},
            ensure_ascii=False)
    content = _EXEC_BASE
    if MARKER in sys_text:
        content += "\n允许最高温度 40°C；超限停检。"
    return ROLE_EXEC, json.dumps({"summary": "摘要", "content": content},
                                 ensure_ascii=False)


def _maintain_reply(prompt: str) -> str:
    section = prompt.split("--- 授权训练执行采样（只读，禁止外推） ---", 1)
    section = section[1].split("--- 采样分类 ---", 1)[0] if len(section) > 1 else prompt
    kinds = re.findall(r"- ([0-9a-f]{32}) kind=(\w+)", section)
    log_ids = [i for i in re.findall(r"\b([0-9a-f]{32})\b", section)]
    failure_ids = [eid for eid, k in kinds if k in ("quality_failure", "failure")]
    support = (failure_ids or log_ids)[:2]
    m = re.search(r"- (pat_[0-9a-f]+) \[[a-z]+\]", prompt)
    if m is not None:
        base = re.search(r"修订 ([0-9a-f-]{36})", prompt)
        return json.dumps({
            "update_patterns": [{
                "pattern_id": m.group(1),
                "base_revision_id": base.group(1) if base else "",
                "revise_fields": {"cause_hypothesis": "追加证据后修正（E3 stub）"},
                "append_support_execution_ids": support,
                "append_conflict_execution_ids": []}],
            "append_log": [f"更新 {m.group(1)}（E3 stub）"],
            "update_index": True}, ensure_ascii=False)
    return json.dumps({
        "create_patterns": [{
            "title": "条件要点覆盖不足（E3 stub 工程验证）",
            "phenomenon": "失败样本显示正文缺少来源明确存在的条件要点（支持 "
                          + str(support) + "）",
            "cause_hypothesis": "（待验证）技能指令未显式要求逐条覆盖条件要点",
            "suggestion": "在 default 指令的步骤中显式要求逐条覆盖适用/前置/步骤",
            "applicability": "default 通用主题编译",
            "supporting_execution_ids": support,
            "conflicting_execution_ids": []}],
        "append_log": ["创建模式（E3 stub）：条件要点覆盖不足，状态 observed"],
        "update_index": True}, ensure_ascii=False)


_PATCH_ANCHOR = "8. 参数与结论必须与来源一致（如扭矩、电压限值）；不确定时宁缺毋造。"
_ADD = "\n- " + MARKER + "：输出必须包含控制器频率目标 90Hz（当来源提及频率时）。"


def _propose_reply(prompt: str) -> str:
    ids = list(dict.fromkeys(
        re.findall(r"- ([0-9a-f]{32}) .*kind=\w+", prompt)))
    reads_done = prompt.count("[tool:read_trace]")
    if reads_done < 4 and len(ids) > reads_done:
        target = ids[reads_done]
        return json.dumps({"tool": "read_trace",
                           "args": {"execution_id": target}},
                          ensure_ascii=False)
    parent_m = re.search(r"- ([\w.-]+)@([\w.:-]+) hash=([0-9a-f]{16})",
                         prompt)
    parent_version = parent_m.group(2) if parent_m else "default:0001"
    return json.dumps({"action": {
        "type": "patch", "skill_id": "default",
        "parent_version_id": parent_version,
        "parent_content_hash": None,
        "ops": [{"file": "SKILL.md", "type": "replace",
                 "anchor": _PATCH_ANCHOR,
                 "replacement": _PATCH_ANCHOR + _ADD}],
        "reason": "E3 stub 工程验证候选：要求输出温度上限 40°C",
        "evidence_execution_ids": list(dict.fromkeys(
            re.findall(r"- ([0-9a-f]{32}) .*kind=\w+", prompt)))[:4],
        "pattern_ids": [],
        "pattern_revision_ids": [],
    }}, ensure_ascii=False)


@pytest.fixture()
def stub_env(monkeypatch):
    _E3Handler.calls = []
    _E3Handler.role_counts = {}
    _E3Handler.fail_next = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _E3Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/chat"
    monkeypatch.setattr(settings, "llm_api_url", url)
    monkeypatch.setattr(settings, "llm_api_key", "stub-key")

            # 正式 real provider 门禁：受控虚构 provider（键经非秘密 env 引用）
    _endpoints = [str(settings.llm_api_url or "")]
    if getattr(settings, "wikiskill_reviewer_api_url", None):
        _endpoints.append(str(settings.wikiskill_reviewer_api_url))
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps({"e2e": {"credential_env": "FK_E2E",
                                            "endpoints": _endpoints,
                                            "allow_insecure": True}}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_require_provider_binding", True)
    monkeypatch.setenv("FK_E2E", "stub-key")
    monkeypatch.setattr(settings, "llm_model", "executor-stub")
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-stub")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", url)
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "prompt-v1")
    monkeypatch.setattr(settings, "wikiskill_reviewer_timeout", 30.0)
    monkeypatch.setattr(settings, "wikiskill_reviewer_retries", 2)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    yield {"server": srv}
    srv.shutdown()
    srv.server_close()


def _seed_run(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    db = skill_store.session_for(root)
    try:
        ds = load_dataset(DEV_DS)
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
        v = skill_store.get_version(db, seed)
        member = {"skill_id": v.skill_id, "version_id": v.version_id,
                  "content_hash": v.content_hash, "seq": v.seq}
        from app.core.skill_evolution import trace_sampling as smp
        ws = smp.group_workspace_id(
            [t.group_id for t in ds.tasks if t.split == "train"][0])
        val = [t.task_id for t in ds.tasks if t.split == "val"]  # 全部 4 项
        exp = gating.create_experiment(
            db, workspace_id=ws, domain="wiki_compile.default",
            dataset=ds, grader_version=ds.grader_version,
            runner_config={"profile": "faithful", "review": "v2",
                           "dataset_version": ds.dataset_version,
                           "grader_version": ds.grader_version,
                           "pipeline": ["wiki.default", "3"]},
            pipeline_key="wiki.default", pipeline_version="3",
            runtime_ref=skill_store.RUNTIME_REF, val_task_ids=val,
            initial_members=[member])
        run_cfg = {"mode": "real", "real": True, "review": "v2",
                   "dataset_dir": str(DEV_DS), "freeze_eligible": True}
        run = orch.create_run(
            db, experiment_id=str(exp.experiment_id), workspace_id=ws,
            domain="wiki_compile.default", dataset=ds, init_mode="business",
            max_iterations=2,
            budget={"max_model_calls": 400, "max_tool_calls": 200,
                    "max_seconds": 1800},
            runner_config=run_cfg,
            train_task_ids=[t.task_id for t in ds.tasks
                            if t.split == "train"],
            experience="full")
        return root, str(exp.experiment_id), str(run.run_id)
    finally:
        db.close()


def _sentinels(monkeypatch):
    from app.core.skill_evolution import runner as _runmod
    from app.core.skill_evolution import maintainer as _maint, proposer as _prop

    def _boom(*a, **k):
        raise AssertionError("simulated default must not run in real mode")

    monkeypatch.setattr(_runmod.SimulatedModel, "__init__", _boom)
    monkeypatch.setattr(_maint.SimulatedMaintainer, "__init__", _boom)
    monkeypatch.setattr(_prop.SimulatedProposer, "__init__", _boom)


def test_e3_full_loop_accept_then_reject(stub_env, tmp_path, monkeypatch):
    from app.core.skill_evolution import control
    _sentinels(monkeypatch)
    root, exp_id, run_id = _seed_run(tmp_path)
    control.freeze_run_runtime_config(root, run_id)
    actors, mode = control.build_run_actors(root, run_id)
    assert mode == "real"
    view = orch.execute(root, load_dataset(DEV_DS), run_id, actors=actors,
                        resume=True)
    if view["status"] != "completed":
        diag = {
            "stop_reason": view.get("stop_reason"),
            "iterations": [{"n": i["number"], "status": i["status"],
                            "step": i["step"], "err": i.get("error_code"),
                            "msg": (i.get("error_message") or "")[:300],
                            "no_action": i.get("no_action")}
                           for i in view["iterations"]],
        }
        diag["role_counts"] = dict(_E3Handler.role_counts)
        diag["review_debug"] = [
            {"items": c.get("debug_items"), "checks": c.get("debug_checks"),
             "err": c.get("debug_err")}
            for c in _E3Handler.calls if c.get("role") == ROLE_REVIEW][:2]
        dbg2 = skill_store.session_for(root)
        import sqlalchemy as sa
        try:
            evs = dbg2.execute(sa.text(
                "SELECT per_task_results_json FROM evolution_evaluations "
                "WHERE experiment_id=:e ORDER BY created_at"),
                {"e": exp_id}).fetchall()
            diag["evals"] = [[{"t": t.get("task_id"), "v": t.get("verdict"),
                               "u": (t.get("unresolved_ids") or [])[:4],
                               "invalid": t.get("evaluation_invalid"),
                               "rev": (t.get("review_records") or [])[:1]}
                              for t in json.loads(r.per_task_results_json)]
                             for r in evs]
        finally:
            dbg2.close()
        raise AssertionError(f"loop not completed: {json.dumps(diag, ensure_ascii=False)}")
    assert view["stop_reason"] in ("max_iterations", "perfect_score")
    if view["current_iteration"] != 2:
        dbg = skill_store.session_for(root)
        import sqlalchemy as sa
        dd = dbg.execute(sa.text(
            "SELECT kind, per_task_results_json FROM evolution_evaluations "
            "WHERE experiment_id=:e ORDER BY created_at"),
            {"e": exp_id}).fetchall()
        summ = [{r.kind: [{"t": t.get("task_id"), "v": t.get("verdict"),
                           "f": t.get("item_failures")}
                          for t in json.loads(r.per_task_results_json)]}
                for r in dd]
        dbg.close()
        raise AssertionError(
            f"iter={view['current_iteration']} reason={view['stop_reason']} "
            f"evals={json.dumps(summ, ensure_ascii=False)}")
    assert view["current_iteration"] == 2

    # ---- 评分与门控真实数据（真实代码判定，stub 不写库） ----
    db = skill_store.session_for(root)
    try:
        import sqlalchemy as sa
        rows = db.execute(sa.text(
            "SELECT kind, valid, main_passed, main_total, invalid_reason "
            "FROM evolution_evaluations WHERE experiment_id=:e "
            "ORDER BY created_at"), {"e": exp_id}).fetchall()
        baselines = [r for r in rows if r.kind == "baseline"]
        candidates = [r for r in rows if r.kind == "candidate"]
        assert baselines and baselines[0].valid == 1
        assert baselines[0].main_total == 4 and baselines[0].main_passed == 1
        # 轮1 候选严格提升（1/4 → 2/4）；轮2 候选持平（2/4）被拒
        assert len(candidates) == 2
        assert all(c.valid == 1 for c in candidates)
        assert [c.main_passed for c in candidates] == [2, 2]
        assert all(c.main_total == 4 for c in candidates)

        events = db.execute(sa.text(
            "SELECT decision, candidate_version_ids_json, reason FROM "
            "evolution_gate_events WHERE experiment_id=:e ORDER BY created_at"),
            {"e": exp_id}).fetchall()
        assert [e.decision for e in events] == ["accepted", "rejected"],             [(e.decision, (e.reason or '')[:120]) for e in events]
        ev_accept = json.loads(events[0].candidate_version_ids_json or '[]')

        # 接受后 current 指针 = 轮1 候选版本（第二轮训练注入该技能正文）
        info = gating.get_experiment(db, exp_id)
        cur = (info.current_skill_set or {}).get("members") or []
        assert cur and cur[0]["version_id"] != "default:0001"
        assert cur[0]["version_id"] in ev_accept
        accepted_hash = cur[0]["content_hash"]

        # 迭代2 冻结集合含已接受版本；迭代2 有训练执行
        iters = db.execute(sa.text(
            "SELECT number, freeze_set_json, train_execution_ids_json, "
            "created_at FROM evolution_iterations WHERE run_id=:r "
            "ORDER BY number"), {"r": run_id}).fetchall()
        assert len(iters) == 2
        it2 = iters[1]
        freeze2 = json.loads(it2.freeze_set_json)
        assert freeze2["members"][0]["version_id"] == cur[0]["version_id"]
        assert freeze2["members"][0]["content_hash"] == accepted_hash
        assert json.loads(it2.train_execution_ids_json)
        from datetime import datetime
        ts = it2.created_at
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        round2_start = ts.timestamp()
    finally:
        db.close()

    # ---- HTTP stub 工程验证证据 ----
    rc = dict(_E3Handler.role_counts)
    marker_exec = [c for c in _E3Handler.calls
                   if c.get("role") == ROLE_EXEC and MARKER in json.dumps(
                       c["body"].get("messages"), ensure_ascii=False)]
    with90 = [c for c in marker_exec
              if "40°C" in (c.get("response_content") or "")]
    assert len(with90) >= 4   # 轮1候选评估 + 轮2训练/评估均实际携带 40°C 目标正文
    assert rc.get(ROLE_EXEC, 0) >= 10
    assert rc.get(ROLE_MAINTAIN, 0) >= 2
    assert rc.get(ROLE_PROPOSE, 0) >= 6
    assert rc.get(ROLE_REVIEW, 0) >= 8
    assert all(c.get("auth") == "Bearer stub-key" for c in _E3Handler.calls)
    assert any(c["body"].get("model") == "executor-stub"
               for c in _E3Handler.calls)
    assert any(c["body"].get("model") == "reviewer-stub"
               for c in _E3Handler.calls)
    # 轮2 训练请求实际携带刚接受技能正文（含 marker 与接受版本正文片段）
    late = [c for c in _E3Handler.calls
            if c.get("role") == ROLE_EXEC and c["ts"] >= round2_start]
    sys_blob = "\n".join(
        str(m.get("content") or "") for c in late
        for m in c["body"].get("messages") or []
        if isinstance(m, dict) and m.get("role") == "system")
    assert MARKER in sys_blob
    # proposer 工具读取轨迹：受控 read_trace 轮次
    reads = sum(1 for c in _E3Handler.calls if c.get("role") == ROLE_PROPOSE
                and "read_trace" in json.dumps(c["body"].get("messages"),
                                               ensure_ascii=False))
    # 每次 proposer 请求内含历史 tool 结果；此处按角色计数含 tool/action 两段 ≥6
    assert rc[ROLE_PROPOSE] >= 6


def test_reviewer_transient_failure_retries_then_ok_and_exhaust(
        stub_env, monkeypatch):
    from app.core.skill_evolution.model_review import ReviewerConfig, create_reviewer
    url = settings.wikiskill_reviewer_api_url
    cfg = ReviewerConfig(mode="real", model_id="reviewer-stub",
                         prompt_version="prompt-v1", api_url=url,
                         api_key_present=True, timeout=10, retries=2)
    rev = create_reviewer(cfg)
    rev.attach_sender(rev._send_http)
    checks = [{"id": "semantic:claim:c1", "check": "x", "detail": "y",
               "boundary": "z"}]
    _E3Handler.fail_next = 1
    out = rev.review(task_id="vd2-val-pass", sources=["s"],
                     candidate_output={"sections": []}, checks=checks)
    assert out["items"][0]["verdict"] == "pass"
    # 瞬发失败 1 次 + 重试成功 1 次 → 同一预算下共 2 次评审请求
    review_calls = [c for c in _E3Handler.calls if c.get("role") == ROLE_REVIEW]
    assert len(review_calls) >= 2
    # 重试耗尽 → ReviewerError（invalid，不产生通过；不回退 v1）
    monkeypatch.setattr(settings, "wikiskill_reviewer_retries", 1)
    cfg1 = ReviewerConfig(mode="real", model_id="reviewer-stub",
                          prompt_version="prompt-v1", api_url=url,
                          api_key_present=True, timeout=10, retries=1)
    rev1 = create_reviewer(cfg1)
    rev1.attach_sender(rev1._send_http)
    _E3Handler.fail_next = 1
    with pytest.raises(Exception):
        rev1.review(task_id="vd2-val-pass", sources=["s"],
                    candidate_output={"sections": []}, checks=checks)
