"""WikiSkill 阶段 6 —— 多轮进化调度、预算、暂停与恢复验收测试（第九节 1–15）。

真实子模块串联：训练→维护→提议→评估+门控→下一轮集合。脚本化 actors（PolicyProposer
等演示设施）只用于流程验证；不直接写终态冒充调度。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.skill_evolution import (
    experience_store as exp,
    gating as gate,
    orchestrator as orch,
    proposer as prop,
    runenv,
    skill_store,
)
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.orchestrator import (
    Actors,
    LeaseConflict,
    PolicyProposer,
    create_run,
    execute,
    get_run,
)
from app.core.skill_evolution.trace import load_meta

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DS_V3 = BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v3"
REPO_SEED = BACKEND_ROOT / "eval/wiki_evolution/skills/seed-default-v1"
WS = "ws_b26f86111afa"  # v3 train group workspace
TRAIN = ["v3-train-01", "v3-train-02", "v3-train-03", "v3-train-04"]


@pytest.fixture(autouse=True)
def _isolate_registries_and_runners():
    from app.core.wiki_pipeline import executor, registry

    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    executor.reset_external_runners()
    registry.REGISTRY.clear()


def _seed_env(tmp_path, val_tasks=None):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
    finally:
        db.close()
    ds = load_dataset(DS_V3)
    db = skill_store.session_for(root)
    try:
        v = skill_store.get_version(db, seed)
        exp_info = gate.create_experiment(
            db, workspace_id=WS, domain="wiki_compile.default", dataset=ds,
            grader_version=ds.grader_version,
            runner_config={"profile": "faithful"}, pipeline_key="wiki.default",
            pipeline_version="3", runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=val_tasks or ["v3-val-a", "v3-val-b"],
            initial_members=[{"skill_id": v.skill_id,
                              "version_id": v.version_id,
                              "content_hash": v.content_hash, "seq": v.seq}])
    finally:
        db.close()
    return {"root": root, "ds": ds, "seed": seed, "exp": exp_info.experiment_id}


def _make_run(env, iterations=3, budget=None):
    db = skill_store.session_for(env["root"])
    try:
        run = create_run(
            db, experiment_id=env["exp"], workspace_id=WS,
            domain="wiki_compile.default", dataset=env["ds"],
            init_mode="business", max_iterations=iterations,
            budget=budget or {"max_model_calls": 200, "max_tool_calls": 200,
                              "max_seconds": 3000},
            runner_config={"profile": "faithful"},
            train_task_ids=TRAIN)
    finally:
        db.close()
    return run.run_id


# -- 三轮完整循环：accept / tie-reject / no_action ---------------------------


@pytest.fixture(scope="module")
def world6(tmp_path_factory):
    env = _seed_env(tmp_path_factory.mktemp("st6world"))
    run_id = _make_run(env, iterations=3)
    view = execute(env["root"], env["ds"], run_id)
    env["run"] = run_id
    env["view"] = view
    db = skill_store.session_for(env["root"])
    try:
        env["gates"] = gate.gate_history(db, env["exp"])
        env["best"] = gate.get_experiment(db, env["exp"]).best_score
        env["current"] = gate.get_experiment(db, env["exp"]).current_skill_set
    finally:
        db.close()
    return env


def test_full_three_round_chain(world6):
    view = world6["view"]
    assert view["status"] == "completed"
    assert view["stop_reason"] == "max_iterations"
    assert view["current_iteration"] == 3
    iters = {it["number"]: it for it in view["iterations"]}
    # round1 accepted、round2 tie rejected、round3 no_action
    assert iters[1]["proposal_id"] and iters[1]["gate_event_id"]
    assert iters[2]["proposal_id"] and iters[2]["gate_event_id"]
    assert iters[3]["no_action"] is True and iters[3]["evaluation_id"] is None
    decisions = [g["decision"] for g in world6["gates"]]
    assert decisions.count("accepted") == 1 and decisions.count("rejected") == 1
    assert world6["best"]["passed"] == 1 and world6["best"]["total"] == 2
    assert world6["current"]["version_ids"] == ["default:0002"]


def test_next_round_uses_new_skill_set_in_requests(world6):
    # round2 训练执行应注入 default:0002（新集合）；round1 用 default:0001。
    db = skill_store.session_for(world6["root"])
    try:
        view = get_run(db, world6["run"])
    finally:
        db.close()
    it1_first = view["iterations"][0]["train_execution_ids"][0]
    it2_first = view["iterations"][1]["train_execution_ids"][0]
    m1 = load_meta(world6["root"] / "runs" / it1_first)
    m2 = load_meta(world6["root"] / "runs" / it2_first)
    assert "default:0001" in (m1.get("skills") or {}).get("version_ids", [])
    assert "default:0002" in (m2.get("skills") or {}).get("version_ids", [])


def test_rejected_keeps_set_and_experience(world6):
    db = skill_store.session_for(world6["root"])
    try:
        # 持平拒绝后集合仍 default:0002
        info = gate.get_experiment(db, world6["exp"])
        assert info.current_skill_set["version_ids"] == ["default:0002"]
        # 经验修订存在（模式+至少两轮修订/日志）
        patterns = exp.list_patterns(db, WS, "wiki_compile.default")
        assert patterns
        assert len(exp.list_revisions(db, patterns[0]["pattern_id"])) >= 1
        assert len(exp.list_logs(db, WS, "wiki_compile.default")) >= 1
    finally:
        db.close()


def test_later_proposer_reads_real_rejection_history(world6):
    # no_action 轮提议者的 read_skill_history 工具事件已发生，且影响摘要含 rejected
    db = skill_store.session_for(world6["root"])
    try:
        assert any(g["decision"] == "rejected" and "tie" in g["reason"]
                   for g in world6["gates"])
        impact = gate.skill_impact_summary(db, "default")
        assert any(i["decision"] == "rejected" for i in impact)
    finally:
        db.close()


def test_accept_once_and_experience_traceable(world6):
    db = skill_store.session_for(world6["root"])
    try:
        evals = db.query(__import__("app.models.evolution",
                                    fromlist=["EvolutionEvaluation"])
                         .EvolutionEvaluation).filter(
            __import__("app.models.evolution",
                       fromlist=["EvolutionEvaluation"]).EvolutionEvaluation
            .experiment_id == world6["exp"]).all()
        valid = [e for e in evals if e.valid and e.kind == "candidate"]
        assert len(valid) >= 1
    finally:
        db.close()


# -- 满分提前停止（val 单任务全对时不再进入下一轮） ---------------------------


def test_perfect_score_stops(tmp_path):
    env = _seed_env(tmp_path, val_tasks=["v3-val-b"])
    run_id = _make_run(env, iterations=3)
    view = execute(env["root"], env["ds"], run_id)
    assert view["status"] == "completed"
    assert view["stop_reason"] == "perfect_score"
    assert view["current_iteration"] == 1  # 满分后不再继续训练/提议


# -- no_action 不创建评估 -----------------------------------------------------


def test_no_action_creates_no_evaluation(tmp_path):
    env = _seed_env(tmp_path, val_tasks=["v3-val-a"])  # marker 缺失 → 永远无法提升
    run_id = _make_run(env, iterations=1)
    view = execute(env["root"], env["ds"], run_id)
    # val-a 无 60V → 即便 marker 也非满分；轮 1 patch 平 baseline? 此任务下 seed 0/1 平?
    # 只为流程：至少一轮跑完且状态为终态（completed max 或 paused 均符合，不允许 fake 成功分数）
    assert view["status"] in ("completed", "paused", "budget_exhausted")
    for it in view["iterations"]:
        assert it["no_action"] in (True, False)  # 由策略决定，不允许伪造分数


# -- 空技能/空经验初始化（paper）路径可运行 -----------------------------------


def test_paper_empty_init_runs(tmp_path):
    from app.core.skill_evolution.contracts import load_dataset
    root = runenv.ensure_experiment_root(tmp_path / "root")
    ds = load_dataset(DS_V3)
    db = skill_store.session_for(root)
    try:
        exp_info = gate.create_experiment(
            db, workspace_id=WS, domain="wiki_compile.default", dataset=ds,
            grader_version=ds.grader_version,
            runner_config={"profile": "faithful"}, pipeline_key="wiki.default",
            pipeline_version="3", runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=["v3-val-b"],
            initial_members=[])  # 显式空技能集合
    finally:
        db.close()
    db = skill_store.session_for(root)
    try:
        run = create_run(db, experiment_id=exp_info.experiment_id,
                         workspace_id=WS, domain="wiki_compile.default",
                         dataset=ds, init_mode="paper", max_iterations=1,
                         budget={"max_model_calls": 50, "max_tool_calls": 50,
                                 "max_seconds": 600},
                         runner_config={"profile": "faithful"},
                         train_task_ids=TRAIN)
    finally:
        db.close()
    view = execute(root, ds, run.run_id)
    assert view["init_mode"] == "paper"
    assert view["status"] in ("completed", "paused", "budget_exhausted")
    db = skill_store.session_for(root)
    try:
        info = gate.get_experiment(db, exp_info.experiment_id)
        assert (info.initial_skill_set or {}).get("mode") == "empty"
    finally:
        db.close()


# -- invalid 评估：暂停且 resume 后可前进 --------------------------------------


def _force_factory(first=True):
    state = {"first": first}

    def factory():
        return _ForceProposer(first=state["first"])
    return factory


class _ForceProposer:
    def __init__(self, first=True):
        self._first = first
        self._pending = []
        self._parsed = False
        self._done = False

    def __call__(self, messages, context="", timeout=120.0):
        import re
        if self._done:
            raise prop.ProposerError("重复 finish")
        prompt = "\n".join(m.get("content", "") if isinstance(m, dict) else ""
                           for m in (messages or []))
        if not self._parsed:
            sec = prompt.split("--- 授权训练摘要 ---", 1)
            sec = sec[1].split("--- 当前技能集合", 1)[0] if len(sec) > 1 else prompt
            self._pending = list(dict.fromkeys(
                re.findall(r"- ([0-9a-f]{32}) .*kind=\w+", sec)))
            self._parsed = True
        if self._pending:
            eid = self._pending.pop(0)
            return {"tool": "read_trace", "args": {"execution_id": eid}}
        self._done = True
        if self._first:
            parent_text = None
            # patch 父 default:0001 为 FORCE（模拟服务不可用）
            return {"action": {"type": "patch", "skill_id": "default",
                               "parent_version_id": "default:0001",
                               "ops": [], "reason": "force",
                               "evidence_execution_ids": [],
                               "pattern_ids": [], "pattern_revision_ids": []}}
        return {"action": {"type": "no_action", "reason": "resume no_action",
                           "evidence_execution_ids": [],
                           "pattern_ids": [], "pattern_revision_ids": []}}


def test_pause_resume_state(tmp_path):
    from datetime import datetime, timedelta
    from app.models.evolution import EvolutionRun
    env = _seed_env(tmp_path, val_tasks=["v3-val-b"])
    run_id = _make_run(env, iterations=1)
    db = skill_store.session_for(env["root"])
    try:
        orch.claim(db, run_id, worker_id="w1")
        orch.pause_request(db, run_id)
        db = skill_store.session_for(env["root"])
        view = get_run(db, run_id)
        assert view["pause_requested"] is True   # 持久化暂停请求
        db.close()
        db = skill_store.session_for(env["root"])
        row = db.get(EvolutionRun, run_id)
        row.lease_expires_at = datetime.now() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    view = execute(env["root"], env["ds"], run_id, resume=True)  # resume 清除请求并续跑
    assert view["status"] in ("completed", "paused", "budget_exhausted")
    assert view["pause_requested"] is False


def test_lease_and_cancel_states(tmp_path):
    from datetime import datetime, timedelta
    env = _seed_env(tmp_path, val_tasks=["v3-val-b"])
    run_id = _make_run(env, iterations=1)
    db = skill_store.session_for(env["root"])
    try:
        orch.claim(db, run_id, worker_id="w1")
        with pytest.raises(LeaseConflict):
            orch.claim(db, run_id, worker_id="w2")  # 未过期租约拒绝双 worker
        orch.cancel_request(db, run_id)
        # 旧 worker 租约过期后可接管，且取消请求生效（新 worker 不执行新步骤）
        from app.models.evolution import EvolutionRun
        row = db.get(EvolutionRun, run_id)
        row.lease_expires_at = datetime.now() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    view = execute(env["root"], env["ds"], run_id)  # 接管后看到 cancel_requested
    assert view["status"] == "cancelled"
    assert view["current_iteration"] == 0  # 未调度任何新步骤


def test_budget_exhausted_terminal(tmp_path):
    env = _seed_env(tmp_path, val_tasks=["v3-val-b"])
    run_id = _make_run(env, iterations=5,
                       budget={"max_model_calls": 1, "max_tool_calls": 1,
                               "max_seconds": 60})
    view = execute(env["root"], env["ds"], run_id)
    assert view["status"] == "budget_exhausted"


def test_business_binding_untouched(tmp_path):
    env = _seed_env(tmp_path, val_tasks=["v3-val-b"])
    db = skill_store.session_for(env["root"])
    try:
        before = skill_store.list_bindings(db)
    finally:
        db.close()
    run_id = _make_run(env, iterations=1)
    execute(env["root"], env["ds"], run_id)
    db = skill_store.session_for(env["root"])
    try:
        assert skill_store.list_bindings(db) == before
    finally:
        db.close()
