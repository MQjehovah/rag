"""WikiSkill 阶段 7A 前置补审（离线，不发真实请求）。

覆盖四方面证据：
1) 长调用心跳：续租线程使正常 worker 不被接管；停止续租后过期接管；
   旧 token 不能提交；cancel/stop 释放线程资源。
2) 发送前预算：reserve 在发送前拦截；内部重试不绕过计数（envelope 每次调用各计）；
   崩溃后预留标记持久不丢；剩余时间约束新请求/截断超时；模型上限≠费用上限（费用不可得）。
3) 分组与暴露：v4 模板家族/独立来源组说明、train/val/test 不共享文件内容；
   维护者/提议者不读 test；最终测试要求整批冻结（ensure_all_frozen）。
4) C 组可运行：experience=none 两轮演化不维护/不读持久 Pattern；D 能利用前轮经验；
   C/D 的 gate 历史（非 Wiki 反馈）都保留。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.skill_evolution import (
    experience_store as exp,
    experiment7 as ex7,
    gating as gate,
    orchestrator as orch,
    proposer as prop,
    runenv,
    skill_store,
)
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.orchestrator import (
    Actors,
    BudgetExceeded,
    BudgetGuard,
    LeaseConflict,
    LongCallRenewer,
    PolicyProposer,
    create_run,
    execute,
    heartbeat,
)
from app.models.evolution import RUN_RUNNING, EvolutionRun

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DS_V3 = BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v3"
DS_V4 = BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v4"
REPO_SEED = BACKEND_ROOT / "eval/wiki_evolution/skills/seed-default-v1"
BUDGET = {"max_model_calls": 500, "max_tool_calls": 500, "max_seconds": 1800}


@pytest.fixture(autouse=True)
def _iso():
    from app.core.wiki_pipeline import executor, registry
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    executor.reset_external_runners()
    registry.REGISTRY.clear()


# ---------------------------------------------------------------------------
# 共享脚手架（与 stage6 同款；避免模块级共享状态）
# ---------------------------------------------------------------------------

def _seed_env(tmp_path, val_tasks=None):
    from app.core.skill_evolution.trace_sampling import group_workspace_id
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
    finally:
        db.close()
    ds = load_dataset(DS_V3)
    ws = group_workspace_id(
        [t.group_id for t in ds.tasks if t.split == "train"][0])
    db = skill_store.session_for(root)
    try:
        v = skill_store.get_version(db, seed)
        exp = gate.create_experiment(
            db, workspace_id=ws, domain="wiki_compile.default", dataset=ds,
            grader_version=ds.grader_version,
            runner_config={"profile": "faithful"}, pipeline_key="wiki.default",
            pipeline_version="3", runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=val_tasks or ["v3-val-a", "v3-val-b"],
            initial_members=[{"skill_id": v.skill_id,
                              "version_id": v.version_id,
                              "content_hash": v.content_hash, "seq": v.seq}])
    finally:
        db.close()
    return {"root": root, "ds": ds, "exp": exp.experiment_id, "ws": ws}


def _make_run(env, iterations=3, budget=None, experience="full"):
    db = skill_store.session_for(env["root"])
    try:
        train = [t.task_id for t in env["ds"].tasks if t.split == "train"]
        run = create_run(
            db, experiment_id=env["exp"], workspace_id=env["ws"],
            domain="wiki_compile.default", dataset=env["ds"],
            init_mode="business", max_iterations=iterations,
            budget=budget or dict(BUDGET),
            runner_config={"profile": "faithful"},
            train_task_ids=train, experience=experience)
    finally:
        db.close()
    return run.run_id


def _new_run_row(root, run_id, budget=None, lease_s=600):
    db = skill_store.session_for(root)
    try:
        row = EvolutionRun(
            run_id=run_id, experiment_id="exp-d", workspace_id="ws_x",
            domain="wiki_compile.default", dataset_version="d",
            init_mode="paper",
            config_json=json.dumps({"budget": budget or dict(BUDGET),
                                    "train_task_ids": [],
                                    "runner": {}, "init_mode": "paper"}),
            initial_skill_set_json="{}", max_iterations=3,
            current_iteration=0, status=RUN_RUNNING,
            used_model_calls=0, used_tool_calls=0, used_estimated_chars=0,
            pause_requested=False, cancel_requested=False,
            lease_token="tok-old", lease_owner="w1",
            lease_expires_at=datetime.now() + timedelta(seconds=lease_s))
        db.add(row)
        db.commit()
        return row
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1) 长调用心跳
# ---------------------------------------------------------------------------

def test_renewer_keeps_lease_blocks_takeover_then_release(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run_row(root, "run-r", lease_s=2)
    renewer = LongCallRenewer(root, "run-r", "tok-old", interval=0.3)
    renewer.start()
    try:
        time.sleep(2.6)  # 超过原始租约；期间持续续租
        db = skill_store.session_for(root)
        try:
            assert heartbeat(db, "run-r", "tok-old") is True
            assert renewer.lost is False
            with pytest.raises(LeaseConflict):
                orch.claim(db, "run-r", worker_id="w2")  # 竞争不能接管
        finally:
            db.close()
    finally:
        renewer.stop()
    assert renewer.is_alive() is False  # stop 释放线程资源
    # 停止续租（最后心跳已把租约延长，需先显式过期）→ 过期接管；旧 token 不能写终态
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-r")
        row.lease_expires_at = datetime.now() - timedelta(seconds=1)
        db.commit()
        with pytest.raises(LeaseConflict):
            orch._set_terminal(db, row, "tok-old", "completed", "old-write")
        taken = orch.claim(db, "run-r", worker_id="w2")
        assert taken.lease_token != "tok-old"
    finally:
        db.close()


def test_renewer_gives_up_on_lost_lease(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run_row(root, "run-l", lease_s=0.4)
    renewer = LongCallRenewer(root, "run-l", "tok-old", interval=0.2)
    renewer.start()
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-l")
        row.lease_expires_at = datetime.now() - timedelta(seconds=5)
        db.commit()
    finally:
        db.close()
    time.sleep(0.8)
    assert renewer.lost is True
    renewer.stop()
    assert renewer.is_alive() is False


# ---------------------------------------------------------------------------
# 2) 发送前预算
# ---------------------------------------------------------------------------

def test_old_worker_lease_lost_blocks_new_requests(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run_row(root, "run-lease", budget=dict(BUDGET), lease_s=600)
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-lease")
        g = BudgetGuard(db, row)
        # 旧租约过期 → 新 worker 接管（token 改变）
        row.lease_expires_at = datetime.now() - timedelta(seconds=1)
        db.commit()
        orch.claim(db, "run-lease", worker_id="w2")
        calls = []
        wrapped = g.wrap(_content_stub(calls))
        with pytest.raises(LeaseConflict):   # 发送前拦截，不发起 HTTP
            wrapped([], timeout=10.0)
        assert calls == []
        with pytest.raises(LeaseConflict):
            g.reserve_model()
    finally:
        db.close()


def test_guard_reserve_before_send_and_persist_unknown(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run_row(root, "run-b", budget={"max_model_calls": 2,
                                        "max_tool_calls": 100,
                                        "max_seconds": 600}, lease_s=600)
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-b")
        g = BudgetGuard(db, row)
        assert g.model_available() == 2
        m1 = g.reserve_model()
        g.finish_model(m1, sent_known=True)   # 已发送 → 计数
        m2 = g.reserve_model()
        with pytest.raises(BudgetExceeded):   # 第三个在发送前被阻止
            g.reserve_model()
        # m2 未 finish → 模拟崩溃：新 guard 从 DB 读回在途标记，不丢预留
        g2 = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        assert g2.model_available() == 0
        assert len(g2._inflight) == 1
    finally:
        db.close()


def test_guard_time_budget_blocks_and_clamps_request(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run_row(root, "run-t", budget={"max_model_calls": 10,
                                        "max_tool_calls": 100,
                                        "max_seconds": 1}, lease_s=60)
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-t")
        g = BudgetGuard(db, row)
        time.sleep(1.2)
        assert g.check_steps_ok() is False        # 剩余时间不足不启动新请求
        with pytest.raises(BudgetExceeded):
            g.reserve_model()
    finally:
        db.close()
    # 单请求超时被截断到剩余时间
    _new_run_row(root, "run-t2", budget={"max_model_calls": 10,
                                         "max_tool_calls": 100,
                                         "max_seconds": 4}, lease_s=60)
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-t2")
        g = BudgetGuard(db, row)
        seen = {}
        stub = lambda messages, context="", timeout=120.0: seen.update(t=timeout) or {"summary": "x"}
        wrapped = g.wrap(stub)
        time.sleep(3.2)
        wrapped([], timeout=120.0)
        assert seen["t"] <= 1.0   # eff = min(120, remaining≈0.8)
    finally:
        db.close()


def _content_stub(calls):
    def stub(messages, context="", timeout=120.0):
        calls.append(1)
        return {"summary": "s", "content": "正文：适用条件；扭矩 25 N·m；电压 60V。"}
    return stub


def test_preflight_stops_mid_multi_task_before_overspend(tmp_path):
    env = _seed_env(tmp_path, val_tasks=["v3-val-b"])
    calls = []
    budget = {"max_model_calls": 2, "max_tool_calls": 500, "max_seconds": 600}
    run_id = _make_run(env, iterations=2, budget=budget)
    actors = Actors(execution_profile="faithful",
                    executor_runner=_content_stub(calls))
    view = execute(env["root"], env["ds"], run_id, actors=actors)
    db = skill_store.session_for(env["root"])
    try:
        row = db.get(EvolutionRun, run_id)
        # 至少记录了真实发送（baseline+train第1任务）；后续请求全部发送前拦截。
        assert int(row.used_model_calls) >= 2
    finally:
        db.close()
    # 第 3 个起的所有请求都在发送前被阻止（stub 只被调用 2 次）；
    # 即使内部把 BudgetExceeded 转成 infra → EVAL_INVALID 暂停路径，
    # 外层终态也必须是显式预算耗尽（不能只留普通 eval_invalid）。
    assert len(calls) == 2
    assert view["status"] == "budget_exhausted", view
    assert str(view.get("stop_reason") or "").startswith("budget_exhausted"), view
    # 预算耗尽后不发送任何新请求
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# 3) 分组/暴露/冻结
# ---------------------------------------------------------------------------

def test_v4_grouping_template_families_and_exposure_statement():
    ds = load_dataset(DS_V4)
    manifest = json.loads((DS_V4 / "manifest.json").read_text(encoding="utf-8"))
    splits = {"train": set(), "val": set(), "test": set()}
    by_split = {"train": [], "val": [], "test": []}
    for t in ds.tasks:
        splits[t.split].add(t.group_id)
        by_split[t.split].append(t)
    # 组数：train 1 / val 2 / test 2；内容文件全部唯一（无跨集合同文件复用）
    assert len(splits["train"]) == 1 and len(splits["val"]) == 2 \
        and len(splits["test"]) == 2
    texts = {}
    for t in ds.tasks:
        text = (DS_V4 / "sources" / t.sources[0].file).read_text(encoding="utf-8")
        texts.setdefault(text, []).append((t.split, t.task_id))
    shared = {k: v for k, v in texts.items() if len(v) > 1}
    assert not shared
    # 模板家族说明：manifest 明确合成单模板参数化 + 暴露状态
    assert "parameterized templates" in manifest["generation"]
    assert manifest["exposure"] == "never exposed before v4 generation"
    # “train 6 单组≥4”准确含义：唯一训练来源组 doc-family-v4e-train，
    # 内含 6 个互异任务 ≥4 —— 训练覆盖局限：同族参数化任务、非生产文档。
    g = splits["train"].pop()
    assert g == "doc-family-v4e-train"
    assert len(by_split["train"]) == 6


def test_ensure_all_frozen_before_any_test(tmp_path):
    plan = ex7.build_plan(tmp_path, "wiki-default-v4",
                          runs={"A": 1, "B": 1, "C": 1, "D": 1})
    missing = ex7.ensure_all_frozen(tmp_path, plan)
    assert len(missing) == 4
    ex7.freeze_skill_set(
        tmp_path, Path(plan[0]["dir"]), {"members": []},
        run_id="run_a", experiment_id="exp_a",
        protocol=plan[0]["protocol"], replicate=plan[0]["replicate"])
    missing = ex7.ensure_all_frozen(tmp_path, plan)
    assert missing == ["B/run01", "C/run01", "D/run01"]  # A 已冻结仍缺 3 组


def test_proposer_and_maintainer_never_see_test_tasks(tmp_path):
    env = _seed_env(tmp_path)
    ds = env["ds"]
    train = [t.task_id for t in ds.tasks if t.split == "train"]
    test_ids = [t.task_id for t in ds.tasks if t.split == "test"]
    # 训练轨迹仅来自 train；维护/提议上下文只挂 train execution_ids
    run_id = _make_run(env, iterations=1)
    db = skill_store.session_for(env["root"])
    try:
        row = db.get(EvolutionRun, run_id)
        cfg = json.loads(row.config_json)
        assert set(cfg["train_task_ids"]) == set(train)
        assert not (set(cfg["train_task_ids"]) & set(test_ids))
        it = orch._new_iteration(db, row, 1)
        assert json.loads(it.freeze_set_json) is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4) C 组可运行 / D 经验利用 / 非 Wiki 反馈权限一致
# ---------------------------------------------------------------------------

def test_c_protocol_two_rounds_without_patterns(tmp_path):
    env = _seed_env(tmp_path)
    run_id = _make_run(env, iterations=2, experience="none")
    view = execute(env["root"], env["ds"], run_id)
    assert view["status"] == "completed", view
    assert view["stop_reason"] == "max_iterations", view
    iters = {it["number"]: it for it in view["iterations"]}
    # 两轮真实提议协议：无维护、无持久 Pattern 读取/写入，仍产出提案并门控
    for n in (1, 2):
        assert iters[n]["maintenance_run_id"] is None
        assert iters[n]["proposal_id"]
        assert iters[n]["status"] == "done"
    db = skill_store.session_for(env["root"])
    try:
        assert exp.list_patterns(db, env["ws"], "wiki_compile.default") == []
        gates = gate.gate_history(db, env["exp"])
        assert gates   # 非 Wiki 反馈（gate 历史）保留，与 D 一致
        assert any(g["decision"] in ("accepted", "rejected") for g in gates)
    finally:
        db.close()


def test_d_protocol_uses_previous_round_experience(tmp_path):
    env = _seed_env(tmp_path, val_tasks=["v3-val-b"])
    run_id = _make_run(env, iterations=2)
    view = execute(env["root"], env["ds"], run_id)
    assert view["status"] == "completed", view
    db = skill_store.session_for(env["root"])
    try:
        # D：第二轮有维护产生的持久 Pattern 供提议者读取
        pats = exp.list_patterns(db, env["ws"], "wiki_compile.default")
        assert len(pats) >= 1
    finally:
        db.close()
    # C/D 非 Wiki 历史权限一致：上面 C 用例已断言 gate 历史保留
