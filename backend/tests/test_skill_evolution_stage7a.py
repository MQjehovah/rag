"""WikiSkill 阶段 7A —— 真实链路接入与效果实验准备（离线）。

验收：真实适配器结构化输出/工具循环/失败分类契约、禁止模拟回退、配置错误无密钥、
数据分组与参考答案隔离、四组开关与隔离、测试集不参与回路、成本缺失值、
配对统计/重复样本处理、心跳与租约风险、invalid-resume 语义。全部离线，不发真实调用。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.skill_evolution import (
    experiment7 as ex7,
    orchestrator as orch,
    runenv,
    skill_store,
)
from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution.real_adapters import (
    ModelBackendError,
    resolve_real_config,
)
from app.core.skill_evolution.trace import iter_events
from app.models.evolution import (
    ITER_PAUSED,
    ITER_STEP_DONE,
    ITER_STEP_EVAL,
    RUN_RUNNING,
    EvolutionIteration,
    EvolutionRun,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DS_V3 = BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v3"
DS_V4 = BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v4"
REPO_SEED = BACKEND_ROOT / "eval/wiki_evolution/skills/seed-default-v1"


@pytest.fixture(autouse=True)
def _isolate_registries_and_runners():
    from app.core.wiki_pipeline import executor, registry

    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    executor.reset_external_runners()
    registry.REGISTRY.clear()


# ---------------------------------------------------------------------------
# 真实适配契约（离线 stub；禁止模拟回退；无行为标记依赖）
# ---------------------------------------------------------------------------


def test_executor_override_contract_and_no_reference_leak(tmp_path, monkeypatch):
    ds = load_dataset(DS_V3)
    root = runenv.ensure_experiment_root(tmp_path / "root")
    calls = []

    def stub(messages, context="", timeout=120.0):
        calls.append(messages)
        assert '"expected_points"' not in json.dumps(messages, ensure_ascii=False)
        assert "STRICT-V1" not in json.dumps(messages, ensure_ascii=False)
        return {"summary": "stub", "content": "正文：扭矩 25 N·m；电压 60V。"}

    task = ds.task("v3-val-a")
    res = run_one(root, ds, task, profile="faithful",
                  skills=FrozenSkillSet.disabled(),
                  llm_runner_override=stub)
    assert calls and res["meta"]["run_status"] == "succeeded"
    assert res["meta"]["candidate"]["revision_id"]


def test_no_simulated_fallback_on_real_failure(tmp_path):
    ds = load_dataset(DS_V3)
    root = runenv.ensure_experiment_root(tmp_path / "root")

    def broken(messages, context="", timeout=120.0):
        from app.core.knowledge_compiler_v3.wiki_page_builder import LLMServiceUnavailable
        raise LLMServiceUnavailable("real endpoint down")

    task = ds.task("v3-val-b")
    res = run_one(root, ds, task, profile="faithful",
                  skills=FrozenSkillSet.disabled(), llm_runner_override=broken)
    # 真实失败不得回退到模拟成功：run 必须失败且归为基础设施。
    assert res["meta"]["run_status"] == "failed"
    assert res["meta"]["outcome"]["failure_kind"] == "infra_or_model"


def test_real_config_validation_no_secrets(monkeypatch):
    # 凭据终审契约：raw override['llm_api_key'] 一律拒绝；兼容模式只读 settings key。
    monkeypatch.setattr("app.config.settings.llm_api_url", "")
    monkeypatch.setattr("app.config.settings.llm_api_key", "")
    monkeypatch.setattr("app.config.settings.llm_model", "")
    with pytest.raises(ModelBackendError, match="llm_api_url"):
        resolve_real_config("executor")
    # 兼容模式：settings key 为空 → 明确缺失错误（raw override 不得顶替）。
    with pytest.raises(ModelBackendError) as ei:
        resolve_real_config("executor",
                            override={"llm_api_url": "https://x", "model": "m"})
    assert "llm_api_key" in str(ei.value)
    assert "secret" not in str(ei.value).lower()
    # raw override 明文密钥 → fail-closed（不得预检通过；错误不回显 key 值）。
    raw_sentinel = "RAW-SECRET-MUST-NOT-LEAK"
    with pytest.raises(ModelBackendError) as ei:
        resolve_real_config("executor",
                            override={"llm_api_url": "https://x",
                                      "model": "m",
                                      "llm_api_key": raw_sentinel})
    assert "raw_credential_override_forbidden" in str(ei.value)
    assert raw_sentinel not in str(ei.value)
    # 兼容模式成功路径只依赖 settings.llm_api_key（不存 key、api_key_present=True）。
    monkeypatch.setattr("app.config.settings.llm_api_key", "s3cret-compat")
    rec = resolve_real_config("executor", override={
        "llm_api_url": "https://x", "model": "m-x",
        "mode": "real"}).to_record()
    assert rec["mode"] == "real" and rec["api_key_present"] is True
    assert "s3cret-compat" not in str(rec)


# ---------------------------------------------------------------------------
# 心跳 / 租约 / 旧 worker 禁止回写
# ---------------------------------------------------------------------------


def _run_row(db, run_id):
    from datetime import datetime, timedelta
    row = EvolutionRun(
        run_id=run_id, experiment_id="exp-dummy", workspace_id="ws_x",
        domain="wiki_compile.default", dataset_version="d",
        init_mode="paper", config_json="{}",
        initial_skill_set_json="{}", max_iterations=3,
        current_iteration=0, status=RUN_RUNNING,
        used_model_calls=0, used_tool_calls=0, used_estimated_chars=0,
        pause_requested=False, cancel_requested=False,
        lease_token="tok-old",
        lease_expires_at=datetime.now() + timedelta(seconds=600),
        lease_owner="w1")
    db.add(row)
    db.commit()
    return row


def test_heartbeat_and_old_worker_blocked(tmp_path):
    from datetime import datetime, timedelta
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        _run_row(db, "run-hb")
        row = db.get(EvolutionRun, "run-hb")
        assert orch.heartbeat(db, "run-hb", "tok-old") is True
        # 旧 token 续租失败；过期后旧 worker 写终态被拒
        row.lease_expires_at = datetime.now() - timedelta(seconds=5)
        db.commit()
        assert orch.heartbeat(db, "run-hb", "tok-old") is False
        from app.core.skill_evolution.orchestrator import LeaseConflict
        with pytest.raises(LeaseConflict):
            orch._commit_guarded(db, row, "tok-old")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# invalid 评估暂停与 resume 语义（显式记录：跳过进入下一轮，不重跑、不沿用分数）
# ---------------------------------------------------------------------------


def test_invalid_resume_advances_not_reeval(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        row = _run_row(db, "run-inv")
        it = EvolutionIteration(
            iteration_id="iter-inv", run_id="run-inv", number=1,
            status=ITER_PAUSED, step=ITER_STEP_EVAL,
            freeze_set_json="{}", experiment_status_rev=1,
            train_execution_ids_json="[]", no_action=False,
            evaluation_id="eval-inv", gate_event_id="gate-inv",
            error_code="EVAL_INVALID",
            error_message="evaluation invalid (recorded; resume advances)")
        db.add(it)
        db.commit()
        outcome = orch._run_iteration(root, load_dataset(DS_V3),
                                      row, it, "tok-old", orch.Actors(), db)
        assert outcome == "done"
        db.refresh(it)
        assert it.status == "done" and it.step == ITER_STEP_DONE
    finally:
        db.close()


def test_model_call_counter(tmp_path):
    from datetime import datetime
    root = runenv.ensure_experiment_root(tmp_path / "root")
    run_dir = root / "runs" / "e000"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"kind": "model_call", "context": "wiki-synthesis",
                    "response": {"content": "x"}})
        + "\n" + json.dumps({"kind": "model_call",
                             "error": {"type": "X", "message": "e"}})
        + "\n", encoding="utf-8")
    assert orch._count_model_calls(root, ["e000"]) == 2


# ---------------------------------------------------------------------------
# 数据集 v4 / 参考答案隔离 / 协议与统计
# ---------------------------------------------------------------------------


def test_v4_dataset_structure_and_isolation():
    ds = load_dataset(DS_V4)
    train_ids = [t.task_id for t in ds.tasks if t.split == "train"]
    assert len(train_ids) >= 4 and len(set(train_ids)) == len(train_ids)
    assert len(ds.tasks) >= 20
    groups = ds.group_split_map()
    assert groups["doc-family-v4e-train"] == "train"
    manifest = json.loads((DS_V4 / "manifest.json").read_text(encoding="utf-8"))
    assert "never exposed" in manifest["exposure"]
    assert manifest["file_hashes"]
    # 参考答案独立目录，模型/工具路径不包含 reference 内容（运行提示中校验，见契约测试）
    assert (DS_V4 / "references/wiki-default-v4.json").is_file()


def test_protocol_configs_and_isolation(tmp_path):
    c = ex7.default_protocol_config("C", 1, init_mode="paper")
    d = ex7.default_protocol_config("D", 1, init_mode="paper")
    assert c.experience == "none" and d.experience == "full"
    assert c.evolve == d.evolve and c.iterations == d.iterations
    a = ex7.default_protocol_config("A", 1)
    assert a.evolve is False
    ds = load_dataset(DS_V4)
    assert ex7.check_plan(ds, d) == []
    bad = ex7.ProtocolConfig(protocol="D", replicate=1, init_mode="paper",
                             model_mode="real", model_ref=None)
    assert any("model_ref" in e for e in ex7.check_plan(ds, bad))
    plan = ex7.build_plan(tmp_path / "root", "wiki-default-v4",
                          runs={"A": 1, "B": 1, "C": 1, "D": 1})
    assert len(plan) == 4
    for p in plan:
        cfgf = Path(p["dir"]) / "protocol.json"
        assert cfgf.is_file() and json.loads(cfgf.read_text(encoding="utf-8"))
    frozen = ex7.freeze_skill_set(
        tmp_path / "root", Path(plan[0]["dir"]), {"members": []},
        run_id="run_iso", experiment_id="exp_iso",
        protocol=plan[0]["protocol"], replicate=plan[0]["replicate"])
    with pytest.raises(FileExistsError):
        ex7.freeze_skill_set(
            tmp_path / "root", Path(plan[0]["dir"]), {"members": []},
            run_id="run_iso", experiment_id="exp_iso",
            protocol=plan[0]["protocol"], replicate=plan[0]["replicate"])
    assert frozen.is_file()


def test_evaluate_frozen_test_never_touches_train_or_val(monkeypatch, tmp_path):
    ds = load_dataset(DS_V4)
    seen = []
    calls = {"run": 0}

    def fake_run(root, dataset, task, profile="faithful", skills=None,
                 llm_runner_override=None):
        seen.append(task.split)
        calls["run"] += 1
        return {"execution_id": "e-" + task.task_id,
                "meta": {"outcome": {"run_ok": True, "published": True,
                                     "failure_kind": None},
                         "run_status": "succeeded",
                         "candidate": {"revision_id": "r1"}},
                "candidate": {"revision_id": "r1", "sections": [],
                              "wiki_status": "published"}}

    def fake_grade(**kw):
        return {"verdict": "pass",
                "item_results": [{"passed": True}, {"passed": True}]}

    monkeypatch.setattr(ex7, "run_one", fake_run)
    monkeypatch.setattr(ex7, "grade_task", fake_grade)
    report = ex7.evaluate_frozen_test(tmp_path / "root", ds,
                                      FrozenSkillSet.empty())
    assert set(seen) == {"test"}
    assert report["total"] == 8 and report["passed"] == 8


def test_paired_bootstrap_groups_and_duplicates():
    group_diffs = {"g1": [0.5, 0.5], "g2": [0.1], "g3": [-0.2, 0.1, 0.1]}
    out = ex7.paired_bootstrap(group_diffs, n_boot=500, seed=7)
    # 组内重复运行先平均：g1 → 0.5 单一组样本，不把 0.5,0.5 当两个独立样本
    assert out["group_means"]["g1"] == pytest.approx(0.5)
    assert out["groups"] == 3 and out["mean"] is not None
    assert out["ci"][0] <= out["mean"] <= out["ci"][1]


def test_budget_estimate_cost_unknown():
    ds = load_dataset(DS_V4)
    n_train = sum(1 for t in ds.tasks if t.split == "train")
    n_val = sum(1 for t in ds.tasks if t.split == "val")
    cfg = ex7.default_protocol_config("D", 1)
    est = ex7.estimate_budget(cfg, n_train, n_val)
    assert est["cost"] is None and est["token_usage"] is None
    assert est["estimated_model_calls"] > 0
    assert "propose" in est["formula"]
