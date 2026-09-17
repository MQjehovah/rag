"""WikiSkill 阶段 4 —— 技能提议者与候选版本验收测试（第九节 1–13，14 由全量回归覆盖）。

演示使用 SimulatedProposer（脚本化）走真实工具分派/授权/补丁校验/候选存储/报告，
不直接写库冒充模型流程；不声明真实模型提出有效技能。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.skill_evolution import proposer as prop
from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution.trace_sampling import group_workspace_id

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DS_V2 = BACKEND_ROOT / "eval" / "wiki_evolution" / "datasets" / "wiki-default-v2"
REPO_SEED = BACKEND_ROOT / "eval" / "wiki_evolution" / "skills" / "seed-default-v1"

V2_INSTALL_TASKS = ["v2-install-01", "v2-install-02", "v2-install-03", "v2-install-04"]


@pytest.fixture(autouse=True)
def _isolate_registries_and_runners():
    from app.core.wiki_pipeline import executor, registry

    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    executor.reset_external_runners()
    registry.REGISTRY.clear()


@pytest.fixture(scope="module")
def dataset_v2():
    return load_dataset(DS_V2)


def fresh_env(tmp_path):
    """独立 root：种子 default:0001 + 4 个 v2 install train 执行。"""
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
    finally:
        db.close()
    ds = load_dataset(DS_V2)
    ids = []
    for task_id in V2_INSTALL_TASKS:
        res = run_one(root, ds, ds.task(task_id), profile="faithful",
                      skills=FrozenSkillSet.disabled())
        ids.append(res["execution_id"])
    return {
        "root": root,
        "ds": ds,
        "ws": group_workspace_id("doc-family-titan810v2-install"),
        "ids": ids,
    }


def _propose(env, **kw):
    return prop.run_proposer(env["root"], env["ds"], env["ws"], **kw)


def _default_versions(root) -> list[str]:
    db = skill_store.session_for(root)
    try:
        return [v.version_id for v in skill_store.list_versions(db, skill_id="default")]
    finally:
        db.close()


# -- 1/2：轨迹不足与重复/失败读取不计数 -------------------------------------


def test_less_than_four_distinct_traces_blocked(tmp_path):
    env = fresh_env(tmp_path)
    summary = _propose(env, profile="patch", execution_ids=env["ids"][:3])
    assert summary.status == prop.PROPOSAL_PREREQ_INSUFFICIENT
    assert summary.distinct_reads < 4
    assert summary.candidate_version_id is None
    assert _default_versions(env["root"]) == ["default:0001"]


def test_duplicate_and_failed_reads_do_not_count(tmp_path):
    env = fresh_env(tmp_path)

    def dup_reader(messages, context="", timeout=120.0):
        # 永远读同一轨迹 → 只算 1 条
        return {"tool": "read_trace",
                "args": {"execution_id": env["ids"][0]}}

    s = _propose(env, runner=dup_reader, max_tool_calls=6)
    # 预算耗尽（重复读取不增加计数，无法凑足 4 条）
    assert s.status in (prop.PROPOSAL_BUDGET_EXHAUSTED,
                        prop.PROPOSAL_PREREQ_INSUFFICIENT)
    assert s.distinct_reads == 1

    # 读取失败（越权轨迹）不计数：自定义 runner 先尝试读 val → 失败，再正常读 4 条
    ds = env["ds"]
    val_id = run_one(env["root"], ds, ds.task("v2-val-01"), profile="faithful",
                     skills=FrozenSkillSet.disabled())["execution_id"]
    calls = {"n": 0, "reads": []}

    def read_then_fail(messages, context="", timeout=120.0):
        if calls["n"] == 0:
            calls["n"] += 1
            return {"tool": "read_trace", "args": {"execution_id": val_id}}
        if len(calls["reads"]) < 4:
            eid = env["ids"][len(calls["reads"])]
            calls["reads"].append(eid)
            return {"tool": "read_trace", "args": {"execution_id": eid}}
        return {"action": {"type": "patch", "skill_id": "default",
                           "parent_version_id": "default:0001",
                           "ops": [{"file": "SKILL.md", "type": "replace",
                                    "anchor": "8. 参数与结论必须与来源一致（如扭矩、电压限值）；不确定时宁缺毋造。",
                                    "replacement": "8. 参数与结论必须与来源一致；输出前核对。"
                                    }],
                           "reason": "r",
                           "evidence_execution_ids": list(calls["reads"]),
                           "pattern_ids": [], "pattern_revision_ids": []}}

    s2 = _propose(env, runner=read_then_fail)
    assert s2.distinct_reads == 4  # val 读取失败不计入
    assert s2.status == prop.PROPOSAL_CANDIDATE_SAVED


# -- 3：越权/损坏轨迹拒绝 ----------------------------------------------------


def test_unauthorized_and_corrupt_traces_rejected(tmp_path):
    env = fresh_env(tmp_path)
    ds = env["ds"]
    val_id = run_one(env["root"], ds, ds.task("v2-val-01"), profile="faithful",
                     skills=FrozenSkillSet.disabled())["execution_id"]
    with pytest.raises(prop.ProposerInputError, match="未授权"):
        _propose(env, execution_ids=[val_id])

    # 跨 workspace：用另一 dataset(v1 install 组)的 train id
    ds1 = load_dataset(BACKEND_ROOT / "eval/wiki_evolution/datasets/wiki-default-v1")
    other = run_one(env["root"], ds1, ds1.task("wiki-default-001"),
                    profile="faithful", skills=FrozenSkillSet.disabled())["execution_id"]
    with pytest.raises(prop.ProposerInputError, match="未授权"):
        _propose(env, execution_ids=[other])

    # 损坏（未封存）轨迹：复制某 run 并破坏封存后尝试
    import shutil
    src = env["root"] / "runs" / env["ids"][0]
    clone_id = env["ids"][0][:28] + "c0ffee00"  # 独立 32hex 且非授权记录
    clone = env["root"] / "runs" / clone_id
    shutil.copytree(src, clone)
    (clone / "events.jsonl").write_text("corrupt", encoding="utf-8")
    with pytest.raises(prop.ProposerInputError, match="未授权"):
        _propose(env, execution_ids=[clone_id])


# -- 4：未读取证据 / Pattern 引用拒绝 ----------------------------------------


def test_unread_evidence_or_pattern_rejected(tmp_path):
    env = fresh_env(tmp_path)
    read4 = {"n": 0, "reads": []}

    def runner(messages, context="", timeout=120.0):
        if len(read4["reads"]) < 4:
            eid = env["ids"][len(read4["reads"])]
            read4["reads"].append(eid)
            return {"tool": "read_trace", "args": {"execution_id": eid}}
        read4["n"] += 1
        return {"action": {"type": "patch", "skill_id": "default",
                           "parent_version_id": "default:0001",
                           "ops": [{"file": "SKILL.md", "type": "replace",
                                    "anchor": "9. 页面间引用用 `[[页面标题]]` 语法；保留关键代码块。",
                                    "replacement": "9. 页面间引用用 [[页面标题]]；保留代码。"}],
                           "reason": "r",
                           "evidence_execution_ids": ["0" * 32],  # 未读取
                           "pattern_ids": [], "pattern_revision_ids": []}}

    s = _propose(env, runner=runner)
    assert s.status == prop.PROPOSAL_OUTPUT_INVALID
    assert "未实际读取" in (s.error_message or "")
    assert _default_versions(env["root"]) == ["default:0001"]

    # Pattern 不在快照/未读取也拒绝（独立 reader：先读 4 条再给非法 pattern 引用）
    reads2 = []

    def runner2(messages, context="", timeout=120.0):
        if len(reads2) < 4:
            eid = env["ids"][len(reads2)]
            reads2.append(eid)
            return {"tool": "read_trace", "args": {"execution_id": eid}}
        return {"action": {"type": "patch", "skill_id": "default",
                           "parent_version_id": "default:0001",
                           "ops": [{"file": "SKILL.md", "type": "replace",
                                    "anchor": "9. 页面间引用用 `[[页面标题]]` 语法；保留关键代码块。",
                                    "replacement": "9. 页面引用保持 [[页面标题]]。"}],
                           "reason": "r",
                           "evidence_execution_ids": list(reads2),
                           "pattern_ids": ["pat_notexist"],
                           "pattern_revision_ids": []}}

    s2 = _propose(env, runner=runner2)
    assert s2.status == prop.PROPOSAL_OUTPUT_INVALID
    assert "固定快照" in (s2.error_message or "")


# -- 5：create 不改变活动绑定 ------------------------------------------------


def test_create_candidate_without_binding_change(tmp_path):
    env = fresh_env(tmp_path)
    db = skill_store.session_for(env["root"])
    try:
        before = skill_store.list_bindings(db)
    finally:
        db.close()
    s = _propose(env, profile="create")
    assert s.status == prop.PROPOSAL_CANDIDATE_SAVED
    assert s.candidate_version_id and s.candidate_version_id.startswith("default-v2:")
    db = skill_store.session_for(env["root"])
    try:
        assert skill_store.list_bindings(db) == before  # 活动绑定不变
        assert len(skill_store.list_versions(db, skill_id="default-v2")) == 1
    finally:
        db.close()
    # 重复提交（同配置）→ 幂等命中，不重复生成版本
    s2 = _propose(env, profile="create")
    assert s2.idempotent_hit and s2.run_id == s.run_id
    db = skill_store.session_for(env["root"])
    try:
        assert len(skill_store.list_versions(db, skill_id="default-v2")) == 1
    finally:
        db.close()


# -- 6/7：patch 精确父版本 / 失配歧义无部分写入 ------------------------------


def test_patch_exact_parent_and_parent_unchanged(tmp_path):
    env = fresh_env(tmp_path)
    db = skill_store.session_for(env["root"])
    try:
        parent_before = skill_store.get_version(db, "default:0001")
    finally:
        db.close()
    s = _propose(env, profile="patch")
    assert s.status == prop.PROPOSAL_CANDIDATE_SAVED
    assert s.candidate_version_id == "default:0002"
    db = skill_store.session_for(env["root"])
    try:
        parent = skill_store.get_version(db, "default:0001")
        cand = skill_store.get_version(db, "default:0002")
        assert parent.content_hash == parent_before.content_hash  # 父版本不变
        assert cand.parent_version_id == "default:0001"
        assert cand.content_hash != parent.content_hash
    finally:
        db.close()


def test_patch_mismatch_or_hash_error_no_partial_write(tmp_path):
    env = fresh_env(tmp_path)

    def with_reads(action_factory):
        reads = []

        def _r(messages, context="", timeout=120.0):
            if len(reads) < 4:
                eid = env["ids"][len(reads)]
                reads.append(eid)
                return {"tool": "read_trace", "args": {"execution_id": eid}}
            return action_factory(list(reads))
        return _r

    s = _propose(env, runner=with_reads(
        lambda reads: {"action": {"type": "patch", "skill_id": "default",
                                  "parent_version_id": "default:0001",
                                  "ops": [{"file": "SKILL.md", "type": "replace",
                                           "anchor": "不存在的锚点文本",
                                           "replacement": "x"}],
                                  "reason": "r",
                                  "evidence_execution_ids": reads,
                                  "pattern_ids": [], "pattern_revision_ids": []}}),
        max_repairs=0)
    assert s.status == prop.PROPOSAL_OUTPUT_INVALID
    assert "失配/歧义" in (s.error_message or "")
    assert _default_versions(env["root"]) == ["default:0001"]

    s2 = _propose(env, runner=with_reads(
        lambda reads: {"action": {"type": "patch", "skill_id": "default",
                                  "parent_version_id": "default:0001",
                                  "parent_content_hash": "0" * 64,
                                  "ops": [{"file": "SKILL.md", "type": "replace",
                                           "anchor": "10. 禁止引入经验库、历史训练答案、验证答案或其他任务资料；禁止输出无关解释文本。",
                                           "replacement": "10. 禁止引入任务外资料。"}],
                                  "reason": "r", "evidence_execution_ids": reads,
                                  "pattern_ids": [], "pattern_revision_ids": []}}))
    assert s2.status == prop.PROPOSAL_OUTPUT_INVALID
    assert "父内容哈希不匹配" in (s2.error_message or "")
    assert _default_versions(env["root"]) == ["default:0001"]


# -- 8/9/10：no_action 无版本；失败无孤儿；重复提交不重复 --------------------


def test_no_action_creates_no_version_and_no_orphan(tmp_path):
    env = fresh_env(tmp_path)
    s = _propose(env, profile="no_action")
    assert s.status == prop.PROPOSAL_NO_ACTION
    assert s.candidate_version_id is None
    assert _default_versions(env["root"]) == ["default:0001"]
    # 保存失败（create 撞已存在 skill 身份）→ output_invalid，无孤儿候选
    db = skill_store.session_for(env["root"])
    try:
        skill_store.add_version(db, skill_store.load_package(
            REPO_SEED, source_type="manual_seed"), parent_version_id="default:0001")
    finally:
        db.close()

    def dup_create(messages, context="", timeout=120.0):
        return {"action": {"type": "create", "skill_id": "default",
                           "skill_md": messages[-1]["content"],  # 不合法内容
                           "purpose_md": "x", "reason": "r",
                           "evidence_execution_ids": [], "pattern_ids": [],
                           "pattern_revision_ids": []}}

    # 读 4 条后 create 撞已存在 skill → finalize 内部拒绝；不产生候选版本/孤儿版本
    reads = {"n": 0}

    def dup_create_read(messages, context="", timeout=120.0):
        if reads["n"] < 4:
            eid = env["ids"][reads["n"]]
            reads["n"] += 1
            return {"tool": "read_trace", "args": {"execution_id": eid}}
        return {"action": {"type": "create", "skill_id": "default",
                           "skill_md": "```yaml\nskill_id: default\nschema_version: \"1\"\n"
                                       "domain: wiki_compile.default\nruntime_ref: wiki.compile.default.runtime/v1\n```\n"
                                       "# 适用条件\n- x\n# 不适用条件\n- y\n# 操作步骤\n- z\n",
                           "purpose_md": "# 来源\n- x\n# 改进目的\n- y\n# 演化历史\n- z\n",
                           "reason": "r", "evidence_execution_ids": list(env["ids"][:4]),
                           "pattern_ids": [], "pattern_revision_ids": []}}

    s2 = _propose(env, runner=dup_create_read)
    assert s2.status == prop.PROPOSAL_OUTPUT_INVALID
    db = skill_store.session_for(env["root"])
    try:
        rows = db.query(prop.EvolutionProposalRun).filter(
            prop.EvolutionProposalRun.run_id == s2.run_id).all()
        assert len(rows) == 1
        assert skill_store.list_versions(db, skill_id="default-v2") == []
    finally:
        db.close()


# -- 11：分类（未知工具/非法输出/超时/预算） ---------------------------------


def test_error_classification(tmp_path):
    env = fresh_env(tmp_path)

    def unknown_tool(messages, context="", timeout=120.0):
        return {"tool": "shell_exec", "args": {"cmd": "ls"}}

    s = _propose(env, runner=unknown_tool, max_tool_calls=1)
    assert s.status in (prop.PROPOSAL_BUDGET_EXHAUSTED, prop.PROPOSAL_FAILED)

    def timeout_runner(messages, context="", timeout=120.0):
        raise TimeoutError("proposer timeout")

    s2 = _propose(env, runner=timeout_runner)
    assert s2.status == prop.PROPOSAL_FAILED
    assert s2.error_code == "TimeoutError"

    def bad_output(messages, context="", timeout=120.0):
        return ["not-a-dict"]

    s3 = _propose(env, runner=bad_output, max_repairs=0)
    assert s3.status == prop.PROPOSAL_OUTPUT_INVALID

    # 未知工具诊断落到对话（tool_error），不当作 action
    db = skill_store.session_for(env["root"])
    try:
        rows = db.query(prop.EvolutionProposalRun).filter(
            prop.EvolutionProposalRun.run_id == s.run_id).all()
        assert rows and rows[0].status in (prop.PROPOSAL_BUDGET_EXHAUSTED,
                                           prop.PROPOSAL_FAILED)
    finally:
        db.close()


# -- 12：多轮调用链可审计 ----------------------------------------------------


def test_multiturn_tool_chain_auditable(tmp_path):
    env = fresh_env(tmp_path)
    calls: list[dict] = []
    proposer = prop.SimulatedProposer(profile="patch")

    def recorder(messages, context="", timeout=120.0):
        calls.append({"msgs": list(messages)})
        return proposer(messages, context, timeout)

    s = _propose(env, runner=recorder)
    assert s.status == prop.PROPOSAL_CANDIDATE_SAVED
    assert s.distinct_reads == 4 and s.tool_calls == 4 and s.model_calls == 5
    db = skill_store.session_for(env["root"])
    try:
        row = db.query(prop.EvolutionProposalRun).filter(
            prop.EvolutionProposalRun.run_id == s.run_id).one()
        mc = json.loads(row.model_calls_json or "[]")
        assert len(mc) == 5
        tools = [e for e in json.loads(row.tool_events_json or "[]")
                 if e["tool"] == "read_trace"]
        assert len(tools) == 4
        assert len({t["execution_id"] for t in tools}) == 4
    finally:
        db.close()


# -- 13：不触发业务发布与外部网络（无 CompileRun 新增、仅演化表写入） ---------


def test_no_compile_publish_and_scope_clean(tmp_path):
    env = fresh_env(tmp_path)
    runs_before = sorted(p.name for p in (env["root"] / "runs").iterdir()
                         if p.is_dir())
    s = _propose(env, profile="patch")
    assert s.status == prop.PROPOSAL_CANDIDATE_SAVED
    runs_after = sorted(p.name for p in (env["root"] / "runs").iterdir()
                        if p.is_dir())
    # 只新增 proposal 状态；不产生任何新执行 run（不触发编译/发布）
    assert runs_after == runs_before


# -- 迁移：P47 表在 head 迁移后存在 ------------------------------------------


def test_p47_migration_head(tmp_path):
    import subprocess, sys
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
    db_path = tmp_path / "p47.db"
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-x",
         f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "head"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(BACKEND_ROOT), env=env)
    assert r.returncode == 0, r.stderr[-1500:]
    from app.models import evolution as ev
    from app.models.database import get_engine
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    ev.require_evolution_schema(engine, context="p47-test")
    assert ev.missing_evolution_tables(engine) == []
    assert "evolution_proposal_runs" in ev.EVOLUTION_TABLES
    assert "evolution_proposals" in ev.EVOLUTION_TABLES
    engine.dispose()
