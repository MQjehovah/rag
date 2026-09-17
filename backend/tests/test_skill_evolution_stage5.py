"""WikiSkill 阶段 5 —— 评估、严格门控与实验技能集合切换验收测试（第十节 1–13）。

端到端演示（模块级 world5）走真实任务执行 + grader + 门控事件，候选由真实
proposer 工具循环生成；单元部分覆盖集合契约/apply/任务清单校验/影响摘要边界。
门控纯逻辑的持平/退化/无效由 world5 实际事件断言（非预设分数 fixture）。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.skill_evolution import proposer as prop
from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.gating import (
    CandidateMismatch,
    ExperimentConfigMismatch,
    GateError,
    apply_proposal_to_members,
    build_set_from_members,
    create_experiment,
    evaluate_and_gate,
    gate_history,
    get_experiment,
    run_baseline,
    skill_impact_summary,
    validate_task_list,
)
from app.core.skill_evolution.injector import (
    EMPTY_SET_HASH,
    MAX_SET_TEXT_CHARS,
    FrozenSkillSet,
    SkillSetError,
)
from tests._stage5_helpers import env_with_v3, make_proposal

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DS_V3 = BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v3"
DS_V2 = BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v2"
REPO_SEED = BACKEND_ROOT / "eval/wiki_evolution/skills/seed-default-v1"


def _row(skill_id, version_id, content_hash, seq, skill_md="x"):
    return SimpleNamespace(skill_id=skill_id, version_id=version_id,
                           content_hash=content_hash, seq=seq, skill_md=skill_md)


@pytest.fixture(autouse=True)
def _isolate_registries_and_runners():
    from app.core.wiki_pipeline import executor, registry

    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    executor.reset_external_runners()
    registry.REGISTRY.clear()


# ---------------------------------------------------------------------------
# 模块级 world5：完整门控链一次跑通（基线与四个门控）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world5(tmp_path_factory):
    from app.core.skill_evolution.contracts import load_dataset
    root = runenv.ensure_experiment_root(tmp_path_factory.mktemp("st5") / "root")
    db = skill_store.session_for(root)
    try:
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
    finally:
        db.close()
    ds = load_dataset(DS_V3)
    _, ws = env_with_v3(root)
    env = {"root": root, "ds": ds, "ws": ws}

    strict_pid, strict_sum = make_proposal(env, "STRICT-V1",
                                           parent="default:0001", extra="strict1")
    strict_vid = strict_sum.candidate_version_id
    pids = {}
    for label, marker, extra in (("tie", "STRICT-V1-ALT", "tie"),
                                 ("weak", "WEAK-SKIP-CONDITIONS", "weak"),
                                 ("force", "FORCE-UNAVAILABLE", "force")):
        pid, summ = make_proposal(env, marker, parent=strict_vid, extra=extra)
        pids[label] = pid
    pids["strict"] = strict_pid

    db = skill_store.session_for(root)
    try:
        v = skill_store.get_version(db, seed)
        members = [{"skill_id": v.skill_id, "version_id": v.version_id,
                    "content_hash": v.content_hash, "seq": v.seq}]
        exp = create_experiment(
            db, workspace_id="ws_st5", domain="wiki_compile.default",
            dataset=ds, grader_version=ds.grader_version,
            runner_config={"profile": "faithful"}, pipeline_key="wiki.default",
            pipeline_version="3", runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=["v3-val-a", "v3-val-b"], initial_members=members)
    finally:
        db.close()
    outcomes = {}
    outcomes["baseline"] = run_baseline(root, ds, exp.experiment_id)
    for label in ("strict", "tie", "weak", "force"):
        outcomes[label] = evaluate_and_gate(root, ds, exp.experiment_id,
                                            pids[label])
    return {"root": root, "ds": ds, "exp": exp.experiment_id, "pids": pids,
            "outcomes": outcomes, "seed": seed, "ws": "ws_st5"}


# -- 1：集合契约（空/单/多；去重；顺序；哈希；上限） --------------------------


def test_skill_set_contracts():
    empty = FrozenSkillSet.empty()
    assert empty.mode == "empty" and empty.set_hash == EMPTY_SET_HASH

    rows = [_row("default", "default:0001", "h1", 1),
            _row("audit", "audit:0001", "h2", 1)]
    multi = FrozenSkillSet.from_versions(rows)
    assert multi.mode == "versions"
    # 固定顺序（skill_id 字典序：audit 在前）
    assert multi.version_ids == ("audit:0001", "default:0001")
    assert sum(1 for s in multi.members if s["skill_id"] == "default") == 1
    # 每 skill 最多一个版本
    dup = [_row("default", "default:0001", "h1", 1),
           _row("default", "default:0002", "h9", 2)]
    with pytest.raises(SkillSetError, match="重复"):
        FrozenSkillSet.from_versions(dup)
    # 哈希包含有序成员与版本内容
    a = FrozenSkillSet.from_versions([_row("default", "default:0001", "h1", 1)])
    b = FrozenSkillSet.from_versions([_row("default", "default:0001", "h2", 1)])
    assert a.set_hash != b.set_hash
    # 超长明确失败（不静默截断）
    huge = _row("default", "default:0001", "h3", 1,
                skill_md="长" * (MAX_SET_TEXT_CHARS + 1))
    with pytest.raises(SkillSetError, match="超限"):
        FrozenSkillSet.from_versions([huge])


# -- 2：create 新增 / patch 替换，其它成员不变 ---------------------------------


def test_apply_proposal_keeps_other_members():
    base = [{"skill_id": "default", "version_id": "default:0001",
             "content_hash": "h1"}]
    create = SimpleNamespace(action="create", skill_id="audit",
                             candidate_version_id="audit:0001",
                             candidate_content_hash="h9")
    out = apply_proposal_to_members(base, create)
    assert {m["skill_id"] for m in out} == {"default", "audit"}
    dup_create = SimpleNamespace(action="create", skill_id="default",
                                 candidate_version_id="d", candidate_content_hash="x")
    with pytest.raises(CandidateMismatch, match="已含|同名冲突"):
        apply_proposal_to_members(out, dup_create)
    patch = SimpleNamespace(action="patch", skill_id="default",
                            parent_version_id="default:0001",
                            candidate_version_id="default:0002",
                            candidate_content_hash="h2")
    out2 = apply_proposal_to_members(base, patch)
    assert out2 == [{"skill_id": "default", "version_id": "default:0002",
                     "content_hash": "h2"}]
    stale_patch = SimpleNamespace(action="patch", skill_id="default",
                                  parent_version_id="default:0099",
                                  candidate_version_id="v", candidate_content_hash="x")
    with pytest.raises(CandidateMismatch, match="不在实验基础集合"):
        apply_proposal_to_members(base, stale_patch)


# -- 3/6/7：任务与配置边界 ------------------------------------------------


def test_task_and_config_guards(world5):
    ds = world5["ds"]
    with pytest.raises(GateError, match="为空"):
        validate_task_list(ds, [])
    with pytest.raises(GateError, match="重复"):
        validate_task_list(ds, ["v3-val-a", "v3-val-a"])
    with pytest.raises(GateError, match="禁止进入门控"):
        validate_task_list(world5["ds"], ["v3-train-01"])
    # test split（v2 dataset）拒绝
    from app.core.skill_evolution.contracts import load_dataset
    ds2 = load_dataset(DS_V2)
    with pytest.raises(GateError, match="禁止进入门控"):
        validate_task_list(ds2, ["v2-test-01"])


def test_baseline_and_candidate_same_config_and_tasks(world5):
    from app.models.evolution import EvolutionEvaluation
    db = skill_store.session_for(world5["root"])
    try:
        rows = db.query(EvolutionEvaluation).filter(
            EvolutionEvaluation.experiment_id == world5["exp"]).all()
        cfgs = {r.kind: r.config_json for r in rows}
        assert cfgs["baseline"] == cfgs["candidate"]
        exp = get_experiment(db, world5["exp"])
        assert exp.runner_config["val_task_ids"] == ["v3-val-a", "v3-val-b"]
    finally:
        db.close()


# -- 4/5/8：门控结果表与 invalid 不晋升 -------------------------------------


def test_gate_outcomes_strict(world5):
    o = world5["outcomes"]
    assert o["baseline"]["score"]["passed"] == 0
    assert o["strict"]["gate"]["decision"] == "accepted"
    assert o["tie"]["gate"]["decision"] == "rejected"
    assert "tie" in o["tie"]["gate"]["reason"]
    assert o["weak"]["gate"]["decision"] == "rejected"
    assert "regression" in o["weak"]["gate"]["reason"]
    assert o["force"]["gate"]["decision"] == "invalid"
    # invalid 不晋升：best 仍是 strict 的 1/2
    db = skill_store.session_for(world5["root"])
    try:
        info = get_experiment(db, world5["exp"])
        assert info.best_score["passed"] == 1 and info.best_score["total"] == 2
        assert info.current_skill_set["version_ids"] == ["default:0002"]
    finally:
        db.close()


def test_candidate_parent_mismatch_rejected(world5):
    db = skill_store.session_for(world5["root"])
    try:
        info = get_experiment(db, world5["exp"])
    finally:
        db.close()
    base = info.current_skill_set.get("members") or []
    stale = SimpleNamespace(action="patch", skill_id="default",
                            parent_version_id="default:0001",
                            candidate_version_id="x", candidate_content_hash="y")
    with pytest.raises(CandidateMismatch, match="不在实验基础集合"):
        apply_proposal_to_members(base, stale)


# -- 9/10：原子性与幂等 ------------------------------------------------------


def test_accept_atomic_and_duplicate_gate_idempotent(world5):
    db = skill_store.session_for(world5["root"])
    try:
        before = get_experiment(db, world5["exp"])
        events = gate_history(db, world5["exp"])
        accepted = [e for e in events if e["decision"] == "accepted"]
        assert len(accepted) == 1
        assert accepted[0]["status_rev_after"] == accepted[0]["status_rev_before"] + 1
        # 重复 gate（strict 再次评估）→ 幂等命中既有事件，不重复晋升
        ds = world5["ds"]
    finally:
        db.close()
    res = evaluate_and_gate(world5["root"], ds, world5["exp"],
                            world5["pids"]["strict"])
    assert res["gate"]["idempotent_hit"] is True
    assert res["gate"]["decision"] == "accepted"
    db = skill_store.session_for(world5["root"])
    try:
        after = get_experiment(db, world5["exp"])
        assert after.status_rev == before.status_rev
        assert after.best_score == before.best_score
    finally:
        db.close()


def test_business_binding_and_other_experiment_unchanged(world5, tmp_path):
    db = skill_store.session_for(world5["root"])
    try:
        bindings_before = skill_store.list_bindings(db)
        info = get_experiment(db, world5["exp"])
        # 新建第二个实验（同一 workspace）不干扰第一个
        v = skill_store.get_version(db, world5["seed"])
        members = [{"skill_id": v.skill_id, "version_id": v.version_id,
                    "content_hash": v.content_hash, "seq": v.seq}]
        exp2 = create_experiment(
            db, workspace_id="ws_st5", domain="wiki_compile.default",
            dataset=world5["ds"], grader_version=world5["ds"].grader_version,
            runner_config={"profile": "faithful"}, pipeline_key="wiki.default",
            pipeline_version="3", runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=["v3-val-a", "v3-val-b"], initial_members=members)
        info2 = get_experiment(db, exp2.experiment_id)
        assert info2.current_skill_set == info2.initial_skill_set
        assert get_experiment(db, world5["exp"]).status_rev == info.status_rev
        assert skill_store.list_bindings(db) == bindings_before  # 业务绑定不变
    finally:
        db.close()


# -- 11：拒绝后候选/经验/差异仍存在 ------------------------------------------


def test_rejected_keeps_candidates_and_history(world5):
    db = skill_store.session_for(world5["root"])
    try:
        for vid in ("default:0002", "default:0003", "default:0004", "default:0005"):
            row = skill_store.get_version(db, vid)
            assert row.skill_md and row.purpose_md
        events = gate_history(db, world5["exp"])
        decisions = {e["decision"] for e in events}
        assert {"accepted", "rejected", "invalid"} <= decisions
        # 拒绝后实验集合仍为 default:0002
        info = get_experiment(db, world5["exp"])
        assert info.current_skill_set["version_ids"] == ["default:0002"]
    finally:
        db.close()


# -- 12：影响摘要只含允许的汇总 ----------------------------------------------


def test_skill_impact_summary_bounded(world5):
    db = skill_store.session_for(world5["root"])
    try:
        items = skill_impact_summary(db, "default")
        assert items
        allowed = {"event_id", "decision", "reason", "experiment_id",
                   "workspace_id", "dataset_version", "grader_version",
                   "candidate_score", "best_score", "candidate_version_ids",
                   "proposal_id", "evaluation_valid", "created_at"}
        for it in items:
            assert set(it) <= allowed
            assert "answer" not in str(it) and "reference" not in str(it)
    finally:
        db.close()
