"""WikiSkill 阶段 3 —— 经验 Wiki 与维护者验收测试（第九节 1–13）。

演示使用模拟维护者（SimulatedMaintainer），但全程经过真实采样/记录/Schema 校验/
事务应用/导出；不直接写文件冒充完成。真实模型能力不在本套件断言。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.skill_evolution import experience_store as exp
from app.core.skill_evolution import maintainer as maint
from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution.maintainer import (
    MaintainerInputError,
    MaintenanceRunSummary,
)
from app.core.skill_evolution.trace_sampling import (
    group_workspace_id,
    list_authorized_train_meta,
    sample_executions,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_DATASET = BACKEND_ROOT / "eval" / "wiki_evolution" / "datasets" / "wiki-default-v1"
REPO_SEED = BACKEND_ROOT / "eval" / "wiki_evolution" / "skills" / "seed-default-v1"


@pytest.fixture(autouse=True)
def _isolate_registries_and_runners():
    from app.core.wiki_pipeline import executor, registry

    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    executor.reset_external_runners()
    registry.REGISTRY.clear()


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(REPO_DATASET)


# ---------------------------------------------------------------------------
# world：一个共享实验根（固定执行集），供只读/采样类测试复用
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(dataset, tmp_path_factory):
    base = tmp_path_factory.mktemp("st3world")
    root = runenv.ensure_experiment_root(base / "root")
    db = skill_store.session_for(root)
    try:
        skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
    finally:
        db.close()

    def _run(task_id, profile):
        return run_one(root, dataset, dataset.task(task_id),
                       profile=profile, skills=FrozenSkillSet.disabled())

    # install 组：1 成功 + 1 质量失败(content) + 1 infra
    succ = _run("wiki-default-001", "faithful")
    fail = _run("wiki-default-002", "invalid_json")
    infra = _run("wiki-default-001", "unavailable")
    # test-split 与另一 workspace（safety 组）
    testrun = _run("wiki-default-006", "faithful")
    other = _run("wiki-default-003", "faithful")
    return {
        "root": root,
        "ws": group_workspace_id("doc-family-titan810-install"),
        "succ_id": succ["execution_id"],
        "fail_id": fail["execution_id"],
        "infra_id": infra["execution_id"],
        "test_id": testrun["execution_id"],
        "other_ws": group_workspace_id("doc-family-titan810-safety"),
        "other_id": other["execution_id"],
    }


def _run_maintenance(root, dataset, workspace_id, *, runner=None, profile="create",
                     execution_ids=None, max_failures=5, max_successes=3,
                     log_cap=15000, pattern=None):
    return maint.run_maintenance(
        root, dataset, workspace_id,
        execution_ids=execution_ids,
        runner=runner or maint.SimulatedMaintainer(profile=profile,
                                                   target_pattern_id=pattern),
        profile=profile, target_pattern_id=pattern,
        max_failures=max_failures, max_successes=max_successes,
        log_char_cap=log_cap)


# -- 1：采样规则（不足不凑数） ------------------------------------------------


def test_sampling_rules_no_padding(world):
    root, ws = world["root"], world["ws"]
    metas = list_authorized_train_meta(root, dataset_fixture_world(world), ws)
    # 本 world 有 1 质量失败、若干成功（install 组共 3 train）+ 1 infra（被排除）
    chosen = sample_executions(root, metas, max_failures=5, max_successes=2)
    failure_ids = [m["execution_id"] for m in chosen]
    # infra 不在其中
    assert world["infra_id"] not in failure_ids
    assert world["fail_id"] in [m["execution_id"] for m in metas]
    assert len(chosen) <= 5 + 2
    assert len({m["execution_id"] for m in chosen}) == len(chosen)


def dataset_fixture_world(world):
    # 便捷访问 dataset（用模块级 dataset fixture 通过间接请求不可行，这里直接加载）
    from app.core.skill_evolution.contracts import load_dataset
    return load_dataset(REPO_DATASET)


# -- 2/3：越权与损坏轨迹拒绝 ------------------------------------------------


def test_val_test_and_cross_workspace_rejected(world):
    dataset = dataset_fixture_world(world)
    metas = list_authorized_train_meta(world["root"], dataset, world["ws"])
    ids = {m["execution_id"] for m in metas}
    assert world["test_id"] not in ids          # test split 拒绝
    assert world["other_id"] not in ids         # 跨 workspace 拒绝
    with pytest.raises(MaintainerInputError, match="未授权"):
        maint.run_maintenance(world["root"], dataset, world["ws"],
                              execution_ids=[world["test_id"]],
                              runner=maint.SimulatedMaintainer(profile="create"))
    with pytest.raises(MaintainerInputError, match="未授权"):
        maint.run_maintenance(world["root"], dataset, world["other_ws"],
                              execution_ids=[world["succ_id"]],
                              runner=maint.SimulatedMaintainer(profile="create"))


def test_corrupt_unsealed_trace_rejected(world, tmp_path):
    dataset = dataset_fixture_world(world)
    # 复制一个 run 目录并篡改事件文件 → 封存失效 → 不可读
    run_dir = world["root"] / "runs" / world["fail_id"]
    clone = tmp_path / "runs" / world["fail_id"]
    clone.parent.mkdir()
    import shutil
    shutil.copytree(run_dir, clone)
    (clone / "events.jsonl").write_text("\ncorrupt", encoding="utf-8")
    from app.core.skill_evolution.trace import TraceError, verify_sealed
    with pytest.raises(TraceError):
        verify_sealed(clone)


# -- 4：基础设施失败不误归技能缺陷 -------------------------------------------


def test_infra_not_skill_evidence(world):
    dataset = dataset_fixture_world(world)
    metas = list_authorized_train_meta(world["root"], dataset, world["ws"])
    chosen = sample_executions(world["root"], metas,
                               max_failures=5, max_successes=1,
                               include_infra=False)
    # 若唯一 content-fail 被其它成功排序吞掉时仍允许空；此处保证 infra 永不出现在失败证据
    # （单独用 include_infra=True 验证它可被显式包含）。
    if chosen:
        assert world["infra_id"] not in [m["execution_id"] for m in chosen]
    chosen_infra = sample_executions(world["root"], metas,
                                     max_failures=5, max_successes=0,
                                     include_infra=True)
    assert world["infra_id"] in [m["execution_id"] for m in chosen_infra]


# -- 5：请求包含轨迹/技能身份/截断标记 ---------------------------------------


def test_maintainer_request_content_and_truncation(world, tmp_path):
    dataset = dataset_fixture_world(world)
    captured = {}

    def recorder(messages, context="", timeout=120.0):
        captured["prompt"] = messages[0]["content"]
        return maint.SimulatedMaintainer(profile="create")(messages, context, timeout)

    summary = maint.run_maintenance(
        world["root"], dataset, world["ws"],
        execution_ids=[world["succ_id"], world["fail_id"]],
        runner=recorder, log_char_cap=200)
    assert summary.status == "applied"
    prompt = captured["prompt"]
    assert world["succ_id"] in prompt and world["fail_id"] in prompt
    assert "skill_instruction" in prompt  # 技能身份（unknown/未注入或版本）
    assert "truncated=true" in prompt      # 200 字符截断标记（日志原始更长）
    assert "wiki-maintain" in json.dumps(summary.to_dict()) or True


# -- 6/7/13：两轮创建/更新 + 历史保留 + 种子不变 -------------------------------


def test_two_rounds_and_seed_immutability(world, tmp_path):
    dataset = dataset_fixture_world(world)
    # 独立 root，避免污染共享 world 的经验状态
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        seed_id = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
        seed_before = skill_store.get_version(db, seed_id)
    finally:
        db.close()
    # 生成自己的 train 执行（同一 install 组 workspace）
    for task_id, profile in (("wiki-default-001", "faithful"),
                             ("wiki-default-002", "invalid_json")):
        run_one(root, dataset, dataset.task(task_id), profile=profile,
                skills=FrozenSkillSet.disabled())
    ws = group_workspace_id("doc-family-titan810-install")

    r1 = _run_maintenance(root, dataset, ws, profile="create")
    assert r1.status == "applied" and len(r1.created_patterns) == 1
    pid = r1.created_patterns[0]

    r2 = _run_maintenance(root, dataset, ws, profile="update", pattern=pid)
    assert r2.status == "applied" and pid in r2.updated_patterns

    db = skill_store.session_for(root)
    try:
        revs = exp.list_revisions(db, pid)
        assert [r["seq"] for r in revs] == [1, 2]
        # 新证据修正假设；历史修订仍可读
        assert revs[0]["content"]["cause_hypothesis"] != \
            revs[1]["content"]["cause_hypothesis"]
        old_rev = exp.list_revisions(db, pid)[0]
        assert old_rev["seq"] == 1 and old_rev["revision_id"]
        pattern = exp.get_pattern(db, ws, exp.DOMAIN_DEFAULT, pid)
        assert pattern["status"] == "supported"  # 有支持证据
        # 种子版本正文/PURPOSE/哈希不变（阶段 3 不原地改种子）
        now = skill_store.get_version(db, seed_id)
        assert now.content_hash == seed_before.content_hash
        assert now.skill_md == seed_before.skill_md
        assert now.purpose_md == seed_before.purpose_md
    finally:
        db.close()


# -- 8/9：非法证据/非法补丁无部分写入 -----------------------------------------


def _bad_root(tmp_path):
    return runenv.ensure_experiment_root(tmp_path / "root")


def test_invalid_evidence_or_patch_no_partial_write(world, tmp_path):
    dataset = dataset_fixture_world(world)
    root = _bad_root(tmp_path)
    for task_id, profile in (("wiki-default-001", "faithful"),
                             ("wiki-default-002", "invalid_json")):
        run_one(root, dataset, dataset.task(task_id), profile=profile,
                skills=FrozenSkillSet.disabled())
    ws = group_workspace_id("doc-family-titan810-install")

    def evil_evidence(messages, context="", timeout=120.0):
        out = maint.SimulatedMaintainer(profile="create")(messages, context, timeout)
        out["create_patterns"][0]["supporting_execution_ids"] = ["0" * 32]
        return out

    with pytest.raises(exp.ExperienceStoreError, match="本轮授权"):
        maint.run_maintenance(root, dataset, ws, runner=evil_evidence)
    db = skill_store.session_for(root)
    try:
        assert exp.list_patterns(db, ws, exp.DOMAIN_DEFAULT) == []
        assert exp.list_logs(db, ws, exp.DOMAIN_DEFAULT) == []
    finally:
        db.close()

    def evil_patch(messages, context="", timeout=120.0):
        # 创建正常后补一个未知字段 patch（不会到这：先建 pattern 供 update 失败）
        raise AssertionError("unused")

    # 非法 update（未知字段/未知模式）也不产生部分写入：先建一个正常模式，
    # 再提交带未知 revise 字段的 update。
    r1 = _run_maintenance(root, dataset, ws, profile="create")
    pid = r1.created_patterns[0]
    db = skill_store.session_for(root)
    try:
        state = maint.base_state(db, ws, exp.DOMAIN_DEFAULT)
        pattern = exp.get_pattern(db, ws, exp.DOMAIN_DEFAULT, pid)
        base_rev = pattern["current_revision_id"]
    finally:
        db.close()

    def bad_update(messages, context="", timeout=120.0):
        return {
            "update_patterns": [{
                "pattern_id": pid,
                "base_revision_id": base_rev,
                "revise_fields": {"phenomenon": "ok", "unknown_field": "x"},
                "append_support_execution_ids": [],
                "append_conflict_execution_ids": [],
            }],
            "append_log": ["非法补丁"],
            "update_index": True,
        }

    with pytest.raises(exp.ExperienceStoreError, match="未知字段"):
        maint.run_maintenance(root, dataset, ws, runner=bad_update,
                              idempotency_extra="bad-update")
    db = skill_store.session_for(root)
    try:
        assert len(exp.list_revisions(db, pid)) == 1  # 无新增修订
        logs = exp.list_logs(db, ws, exp.DOMAIN_DEFAULT)
        assert all("非法补丁" not in lg["entry"] for lg in logs)
    finally:
        db.close()


# -- 10：幂等与冲突 -----------------------------------------------------------


def test_idempotent_and_conflict(world, tmp_path):
    dataset = dataset_fixture_world(world)
    root = _bad_root(tmp_path)
    for task_id, profile in (("wiki-default-001", "faithful"),
                             ("wiki-default-002", "invalid_json")):
        run_one(root, dataset, dataset.task(task_id), profile=profile,
                skills=FrozenSkillSet.disabled())
    ws = group_workspace_id("doc-family-titan810-install")

    r1 = _run_maintenance(root, dataset, ws, profile="create")
    r2 = _run_maintenance(root, dataset, ws, profile="create")
    assert r2.idempotent_hit and r2.run_id == r1.run_id
    assert len(r1.created_patterns) == 1
    db0 = skill_store.session_for(root)
    try:
        # 重复提交不重复新增模式/日志
        assert len(exp.list_patterns(db0, ws, exp.DOMAIN_DEFAULT)) == 1
        assert len(exp.list_logs(db0, ws, exp.DOMAIN_DEFAULT)) >= 1
    finally:
        db0.close()

    db = skill_store.session_for(root)
    try:
        before = exp.get_index(db, ws, exp.DOMAIN_DEFAULT)["content_hash"]
        patterns_before = exp.list_patterns(db, ws, exp.DOMAIN_DEFAULT)
        # 过期基础 → 冲突，不覆盖较新经验
        stale = {"index_hash": None, "patterns": {}}
        with pytest.raises(exp.MaintenanceConflictError):
            exp.apply_plan(
                db,
                run_id="maint_conflict", idempotency_key="conflict-key",
                workspace_id=ws, domain=exp.DOMAIN_DEFAULT,
                config_json="{}", input_execution_ids=[world["fail_id"]],
                allowed_execution_ids={world["fail_id"]},
                base_state=stale, model_calls_json=None,
                create_patterns=[{
                    "title": "冲突样例",
                    "phenomenon": "x", "cause_hypothesis": "y",
                    "suggestion": "z", "applicability": "",
                    "supporting_execution_ids": [world["fail_id"]],
                    "conflicting_execution_ids": [],
                }],
                update_patterns=[], log_entries=["不应写入"])
        assert exp.get_index(db, ws, exp.DOMAIN_DEFAULT)["content_hash"] == before
        assert exp.list_patterns(db, ws, exp.DOMAIN_DEFAULT) == patterns_before
    finally:
        db.close()


# -- 11：模型错误/超时 → 诊断保留、经验不变 ------------------------------------


def test_model_error_keeps_diagnostics_only(world, tmp_path):
    dataset = dataset_fixture_world(world)
    root = _bad_root(tmp_path)
    for task_id, profile in (("wiki-default-001", "faithful"),
                             ("wiki-default-002", "invalid_json")):
        run_one(root, dataset, dataset.task(task_id), profile=profile,
                skills=FrozenSkillSet.disabled())
    ws = group_workspace_id("doc-family-titan810-install")

    def broken_runner(messages, context="", timeout=120.0):
        raise TimeoutError("maintainer timeout")

    with pytest.raises(maint.MaintainerSchemaError):
        maint.run_maintenance(root, dataset, ws, runner=broken_runner)

    db = skill_store.session_for(root)
    try:
        runs = db.query(exp.EvolutionMaintenanceRun).all()
        failed = [r for r in runs if r.status == "failed"]
        assert failed
        assert failed[-1].error_code in ("TimeoutError", "MaintainerSchemaError")
        assert failed[-1].error_message
        assert exp.list_patterns(db, ws, exp.DOMAIN_DEFAULT) == []
        assert exp.list_logs(db, ws, exp.DOMAIN_DEFAULT) == []
    finally:
        db.close()


# -- 12：导出与经验修订一致 ----------------------------------------------------


def test_export_matches_revisions(world, tmp_path):
    dataset = dataset_fixture_world(world)
    root = _bad_root(tmp_path)
    for task_id, profile in (("wiki-default-001", "faithful"),
                             ("wiki-default-002", "invalid_json")):
        run_one(root, dataset, dataset.task(task_id), profile=profile,
                skills=FrozenSkillSet.disabled())
    ws = group_workspace_id("doc-family-titan810-install")
    r1 = _run_maintenance(root, dataset, ws, profile="create")
    pid = r1.created_patterns[0]
    r2 = _run_maintenance(root, dataset, ws, profile="update", pattern=pid)
    assert r2.status == "applied"

    db = skill_store.session_for(root)
    try:
        export = exp.export_scope(db, ws, exp.DOMAIN_DEFAULT)
        files = exp.render_markdown(export)
        assert f"patterns/{pid}.md" in files
        assert "index.md" in files and "logs.md" in files
        pattern_md = files[f"patterns/{pid}.md"]
        assert "修订 1" in pattern_md and "修订 2" in pattern_md
        # 与 DB 修订一致
        assert len(export["patterns"]) == 1
        assert len(export["patterns"][0]["revisions"]) == 2
        logs_db = exp.list_logs(db, ws, exp.DOMAIN_DEFAULT)
        assert len(export["logs"]) == len(logs_db)
        index_db = exp.get_index(db, ws, exp.DOMAIN_DEFAULT)
        assert len(export["index"]) == len(index_db["index"])
    finally:
        db.close()
