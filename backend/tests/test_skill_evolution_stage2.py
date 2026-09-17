"""WikiSkill 阶段 2 —— 技能版本与指令注入验收测试。

覆盖任务书第八节 1–11（12 为回归套件，另行统一运行）：
1. 两个不同指令版本出现在实际捕获的模型请求中；
2. 每次调用只注入一次，原任务约束仍保留；
3. RecordingRunner 捕获注入后的请求；
4. 切换绑定后新任务用新版本，在途任务仍用旧版本；
5. 重放使用原版本；阶段 1 历史按无指令方式重放；
6. 空技能集合可执行；
7. 缺失、损坏、不兼容版本明确失败；
8. 不可变版本禁止覆盖，重复导入行为明确；
9. Workspace / 实验 / 业务绑定互不串用；
10. 功能关闭时旧编译行为保持；
11. 临时库迁移、MANAGED 与旧库兼容检查通过。

说明：使用检查消息的 fake runner 证明“不同技能版本改变模型收到的指令”；
不把依指令改变结果的 fake 行为当作真实质量提升。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.errors import (
    DatasetError,
    SkillPackageError,
    SkillStoreError,
)
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution.snapshot import SnapshotStore
from app.core.skill_evolution.trace import iter_events

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


@pytest.fixture()
def dataset():
    return load_dataset(REPO_DATASET)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_package(root: Path, name: str, marker: str) -> Path:
    """构造临时技能包目录（版本特征以 marker 文本区分，便于消息断言）。"""
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "```yaml\nskill_id: default\nschema_version: \"1\"\n"
        "domain: wiki_compile.default\n"
        "runtime_ref: wiki.compile.default.runtime/v1\n```\n\n"
        f"# 适用条件\n- 通用主题 Wiki 编译（default）。\n\n"
        f"# 不适用条件\n- API Reference 结构化提取。\n\n"
        f"# 操作步骤\n1. 只依据来源文档生成。\n"
        f"2. 版本特征标记：{marker}\n"
        "3. 返回 JSON 并保留原提示词输出格式约束。\n",
        encoding="utf-8")
    (d / "PURPOSE.md").write_text(
        f"# 来源\n- 测试构造技能包 {name}（builtin/manual 整理模拟）。\n"
        f"# 改进目的\n- 测试 marker={marker}\n"
        "# 演化历史\n- v-tmp 未验证。\n",
        encoding="utf-8")
    return d


def prepare_root(tmp_path, *, second_marker: str | None = None) -> tuple[Path, str, str]:
    """建立 root：种子 v1 +（可选）manual v2；返回 (root, v1, v2|None)。"""
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        v1 = skill_store.import_seed(db, skill_store.load_package(REPO_SEED, source_type="builtin_seed"))
        v2 = None
        if second_marker:
            pkg2_dir = make_package(tmp_path / "pkgs", "v2-pkg", second_marker)
            v2 = skill_store.add_version(
                db, skill_store.load_package(pkg2_dir, source_type="manual_seed"),
                parent_version_id=v1)
        return root, v1, v2
    finally:
        db.close()


def run_task(root: Path, dataset, task_id: str, plan: FrozenSkillSet) -> dict:
    task = dataset.task(task_id)
    return run_one(root, dataset, task, profile="faithful", skills=plan)


def captured_calls(run_dir: Path) -> list[dict]:
    return [e for e in iter_events(run_dir) if e.get("kind") == "model_call"]


def injection_events(run_dir: Path) -> list[dict]:
    return [e for e in iter_events(run_dir) if e.get("kind") == "skill_injection"]


# -- 1/2/3：版本差异、单次注入、记录为注入后消息 ------------------------------


def test_distinct_versions_reach_model_and_single_injection(tmp_path, dataset):
    root, v1, v2 = prepare_root(tmp_path, second_marker="严格版步骤：先核对版本标签")
    assert v1 != v2
    plan1 = FrozenSkillSet.from_versions((skill_store.get_version(
        skill_store.session_for(root), v1),))
    plan2 = FrozenSkillSet.from_versions((skill_store.get_version(
        skill_store.session_for(root), v2),))

    r1 = run_task(root, dataset, "wiki-default-001", plan1)
    r2 = run_task(root, dataset, "wiki-default-001", plan2)

    calls1 = captured_calls(Path(r1["run_dir"]))
    calls2 = captured_calls(Path(r2["run_dir"]))
    assert len(calls1) >= 1 and len(calls2) >= 1

    # 注入后的第一条消息是 system 指令，两个版本文本确实不同。
    first1 = calls1[0]["messages"][0]
    first2 = calls2[0]["messages"][0]
    assert first1["role"] == "system"
    # v1=仓库种子（builtin 整理版）：含来源约束行；v2=测试包带专属 marker。
    assert "禁止引入经验库" in first1["content"]
    assert "严格版步骤" not in first1["content"]
    assert "严格版步骤" in first2["content"]
    assert "禁止引入经验库" not in first2["content"]

    # 每次调用只注入一次：每条 model_call 前恰好一条 skill_injection(injected=true)。
    for run_dir in (Path(r1["run_dir"]), Path(r2["run_dir"])):
        evs = list(iter_events(run_dir))
        injects = [e for e in evs if e.get("kind") == "skill_injection" and e.get("injected")]
        model_calls = [e for e in evs if e.get("kind") == "model_call"]
        assert len(injects) == len(model_calls) == 1

    # 原任务约束仍在：用户消息（注入后仍是原提示词）含输出格式 JSON 要求。
    original_user = calls1[0]["messages"][1]
    assert original_user["role"] == "user"
    assert '"summary"' in original_user["content"] or "只返回 JSON" in original_user["content"]


# -- 4：绑定切换 vs 在途任务 ------------------------------------------------


def test_binding_switch_affects_new_runs_not_inflight(tmp_path, dataset):
    root, v1, v2 = prepare_root(tmp_path, second_marker="绑定切换版本 B")
    db = skill_store.session_for(root)
    try:
        skill_store.bind(db, kind="experiment", workspace_id="ws-a",
                         domain=skill_store.DEFAULT_DOMAIN, version_id=v1)
        resolved1 = skill_store.resolve_binding(
            db, kind="experiment", workspace_id="ws-a",
            domain=skill_store.DEFAULT_DOMAIN)
        run1 = run_task(root, dataset, "wiki-default-001",
                        FrozenSkillSet.from_versions((resolved1["version"],)))
        # 切换绑定 → v2。
        skill_store.bind(db, kind="experiment", workspace_id="ws-a",
                         domain=skill_store.DEFAULT_DOMAIN, version_id=v2)
        # 在途任务 run1 的记录保持 v1（冻结）。
        meta1 = json.loads((Path(run1["run_dir"]) / "meta.json").read_text(encoding="utf-8"))
        assert meta1["skills"]["mode"] == "versions"
        assert meta1["skills"]["version_ids"] == [v1]
        # 新任务用 v2。
        resolved2 = skill_store.resolve_binding(
            db, kind="experiment", workspace_id="ws-a",
            domain=skill_store.DEFAULT_DOMAIN)
        run2 = run_task(root, dataset, "wiki-default-001",
                        FrozenSkillSet.from_versions((resolved2["version"],)))
        meta2 = json.loads((Path(run2["run_dir"]) / "meta.json").read_text(encoding="utf-8"))
        assert meta2["skills"]["version_ids"] == [v2]
        assert meta1["skill_instruction_version"] == v1
        assert meta2["skill_instruction_version"] == v2
    finally:
        db.close()


# -- 5：重放用原版本；legacy 记录无指令重放 -----------------------------------


def test_rerun_uses_original_version_not_latest(tmp_path, dataset):
    root, v1, v2 = prepare_root(tmp_path, second_marker="重放目标版本 B")
    db = skill_store.session_for(root)
    try:
        row1 = skill_store.get_version(db, v1)
        run1 = run_task(root, dataset, "wiki-default-001",
                        FrozenSkillSet.from_versions((row1,)))
        # 制造“更晚版本”后重放：仍应用 v1（按原记录冻结，非自动最新）。
        db2 = skill_store.session_for(root)
        try:
            v3_dir = make_package(tmp_path / "pkgs", "v3-pkg", "最新版 C")
            v3 = skill_store.add_version(
                db2, skill_store.load_package(v3_dir, source_type="manual_seed"),
                parent_version_id=v1)
        finally:
            db2.close()
        assert v3
        meta1 = json.loads((Path(run1["run_dir"]) / "meta.json").read_text(encoding="utf-8"))
        plan = skill_store.freeze_versions(
            db, tuple(meta1["skills"]["version_ids"]),
            expected_hashes=tuple(meta1["skills"]["content_hashes"]))
        run2 = run_task(root, dataset, "wiki-default-001", plan)
        meta2 = json.loads((Path(run2["run_dir"]) / "meta.json").read_text(encoding="utf-8"))
        assert meta2["skills"]["version_ids"] == [v1]
        assert v3 not in meta2["skills"]["version_ids"]
    finally:
        db.close()


def test_legacy_history_replays_without_instruction(tmp_path, dataset):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    # 阶段 1 历史：无技能（legacy）执行。
    legacy = run_task(root, dataset, "wiki-default-001", FrozenSkillSet.disabled())
    meta1 = json.loads((Path(legacy["run_dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta1["skills"]["mode"] == "none"
    # 即使之后种子存在，重放 legacy 也不自动补入种子。
    db = skill_store.session_for(root)
    try:
        v1 = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
    finally:
        db.close()
    plan = FrozenSkillSet.disabled()  # 由 meta mode=none 决定
    replay = run_task(root, dataset, "wiki-default-001", plan)
    meta2 = json.loads((Path(replay["run_dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta2["skills"]["mode"] == "none"
    calls = captured_calls(Path(replay["run_dir"]))
    assert all(c["messages"][0]["role"] != "system" for c in calls)
    assert v1  # 种子已存在但未自动使用


# -- 6：空技能集合 -----------------------------------------------------------


def test_empty_skill_set_runs(tmp_path, dataset):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    res = run_task(root, dataset, "wiki-default-001", FrozenSkillSet.empty())
    meta = json.loads((Path(res["run_dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["skills"]["mode"] == "empty"
    assert meta["skill_instruction_injected"] is False
    evs = injection_events(Path(res["run_dir"]))
    assert evs and all(not e["injected"] for e in evs)
    assert all(e["reason"] == "empty_set" for e in evs)
    assert res["meta"]["run_status"] == "succeeded"


# -- 7：缺失 / 损坏 / 不兼容版本明确失败 --------------------------------------


def test_missing_corrupt_incompatible_fail_loud(tmp_path, dataset):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        with pytest.raises(SkillStoreError, match="缺失"):
            skill_store.get_version(db, "default:9999")
        with pytest.raises(SkillStoreError, match="缺失"):
            skill_store.freeze_versions(db, ["default:9999"])
    finally:
        db.close()

    # 损坏：入库后直接篡改 skill_md → 读取时哈希失配（不可静默替换）。
    db = skill_store.session_for(root)
    try:
        v1 = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
        row = db.query(skill_store.EvolutionSkillVersion).filter(
            skill_store.EvolutionSkillVersion.version_id == v1).one()
        row.skill_md = row.skill_md + "\n# 篡改"
        db.commit()
        with pytest.raises(SkillStoreError, match="损坏"):
            skill_store.get_version(db, v1)
    finally:
        db.close()

    # 重放哈希失配（记录哈希与实际不一致）→ 拒绝。
    root2 = runenv.ensure_experiment_root(tmp_path / "root2")
    db = skill_store.session_for(root2)
    try:
        v1 = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
        with pytest.raises(SkillStoreError, match="不一致"):
            skill_store.freeze_versions(db, [v1], expected_hashes=["0" * 64])
    finally:
        db.close()


def test_incompatible_domain_runtime_rejected(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "```yaml\nskill_id: api_reference\nschema_version: \"1\"\n"
        "domain: wiki_compile.api_reference\n"
        "runtime_ref: api.reference.runtime/v1\n```\n\n"
        "# 适用条件\n- API。\n# 不适用条件\n- 通用。\n# 操作步骤\n- 提取。\n",
        encoding="utf-8")
    (d / "PURPOSE.md").write_text("# 来源\n- x\n# 改进目的\n- y\n# 演化历史\n- z\n",
                                  encoding="utf-8")
    with pytest.raises(SkillPackageError,
                       match="不支持 skill_id|不支持 domain|Runtime 不兼容"):
        skill_store.load_package(d, source_type="manual_seed")


# -- 8：不可变与重复导入 -----------------------------------------------------


def test_immutable_versions_no_overwrite(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        pkg = skill_store.load_package(REPO_SEED, source_type="builtin_seed")
        v1 = skill_store.import_seed(db, pkg)
        # 重复 seed（同内容同 source）幂等复用。
        assert skill_store.import_seed(db, pkg) == v1
        # add-version 同内容 → 始终新建（不覆盖），原行内容不变。
        v2 = skill_store.add_version(db, pkg, parent_version_id=v1)
        assert v2 != v1
        rows = skill_store.list_versions(db)
        assert [r.version_id for r in rows] == [v1, v2]
        assert rows[0].content_hash == pkg.content_hash()
        assert skill_store.get_version(db, v1).skill_md == pkg.skill_md
    finally:
        db.close()


# -- 9/10：workspace 隔离与业务绑定 / 功能关闭 --------------------------------


def test_binding_scopes_isolated_and_feature_off_default(tmp_path, dataset):
    root, v1, v2 = prepare_root(tmp_path, second_marker="ws-b 版本")
    db = skill_store.session_for(root)
    try:
        skill_store.bind(db, kind="experiment", workspace_id="ws-a",
                         domain=skill_store.DEFAULT_DOMAIN, version_id=v1)
        skill_store.bind(db, kind="experiment", workspace_id="ws-b",
                         domain=skill_store.DEFAULT_DOMAIN, version_id=v2)
        # 业务绑定写入不影响 experiment 解析。
        skill_store.bind(db, kind="business", workspace_id="ws-a",
                         domain=skill_store.DEFAULT_DOMAIN, version_id=v2)
        ra = skill_store.resolve_binding(db, kind="experiment", workspace_id="ws-a",
                                         domain=skill_store.DEFAULT_DOMAIN)
        rb = skill_store.resolve_binding(db, kind="experiment", workspace_id="ws-b",
                                         domain=skill_store.DEFAULT_DOMAIN)
        assert ra["version"].version_id == v1
        assert rb["version"].version_id == v2
        # ws-c 无任何绑定 → none（功能关闭，不回退 business）。
        assert skill_store.resolve_binding(db, kind="experiment", workspace_id="ws-c",
                                           domain=skill_store.DEFAULT_DOMAIN)["mode"] == "none"
    finally:
        db.close()

    # 功能关闭：无技能参数运行 = 旧编译行为（与阶段 1 一致：无 system 注入）。
    res = run_task(root, dataset, "wiki-default-001", FrozenSkillSet.disabled())
    meta = json.loads((Path(res["run_dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["skills"]["mode"] == "none"
    assert meta["run_status"] == "succeeded"
    calls = captured_calls(Path(res["run_dir"]))
    assert all(c["messages"][0]["role"] != "system" for c in calls)


# -- 11：迁移 / MANAGED / 旧库兼容 -------------------------------------------


def test_p44_legacy_db_feature_off_compile_runs(tmp_path):
    """WikiSkill 关闭（默认）时，P44 旧库 init_db 通过且旧编译链可执行。"""
    import json as _json
    from app.core.knowledge_compiler_v3.wiki_page_builder import call_wiki_llm_json  # noqa: F401
    db_path = tmp_path / "legacy.db"
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
    r0 = subprocess.run(
        [sys.executable, "-m", "alembic", "-x",
         f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "a9b8c7d6e5f4"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(BACKEND_ROOT), env=env)
    assert r0.returncode == 0, r0.stderr[-2000:]

    from app.models import evolution as ev
    from app.models.database import get_engine, init_db
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)  # 旧库不带演化表也允许启动（功能关闭）
    # 启用演化（require）必须明确报错，不能静默退化。
    with pytest.raises(RuntimeError, match="evolution_schema_not_ready"):
        ev.require_evolution_schema(engine, context="test:feature-on")
    engine.dispose()

    # 关闭状态下跑一条真实 default 编译（fake LLM 注入），验证旧编译链可执行。
    from app.core.wiki_pipeline import executor
    from app.core.skill_evolution import runenv as _renv
    from app.core.skill_evolution.runner import GraphRecorder, RecordingRunner, SimDoc, SimulatedModel
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    db = _renv.make_session(engine)
    from app.models.database import (
        Notebook, NotebookWorkspaceBinding, Page, WikiPage, WikiWorkspace)
    ws = WikiWorkspace(id="ws-leg", key="key_leg", name="legacy-ws",
                       acl_scope='{"groups": ["__public__"]}', scope_id="company",
                       status="active")
    nb = Notebook(id="nb-leg", name="legacy-nb", group_id="__public__")
    db.add_all([ws, nb])
    db.flush()
    db.add(NotebookWorkspaceBinding(notebook_id="nb-leg", workspace_id="ws-leg",
                                    status="active"))
    db.add(Page(id="p-leg", notebook_id="nb-leg", title="资料",
                content="适用条件：适用于 legacy。\n参数：扭矩 25 N·m。\n"))
    db.flush()
    db.add(WikiPage(id="w-leg", title="legacy 主题", summary="", category="资料",
                    acl_scope='{"groups": ["__public__"]}', status="draft",
                    source_page_ids=_json.dumps(["p-leg"]), dirty=True,
                    locked=False, workspace_id="ws-leg"))
    db.commit()
    _renv.register_compile_stack()
    calls = []
    sim = SimulatedModel(docs=(SimDoc(doc_id="p-leg", title="资料",
                                      content="适用条件：适用于 legacy。\n参数：扭矩 25 N·m。\n"),))
    def _emit(e):
        calls.append(e)
    rec = RecordingRunner(sim, _emit)
    graph = GraphRecorder(_emit)
    executor.reset_external_runners()
    executor.configure_external_runners(llm_runner=rec, graph_runner=graph)
    run = executor.create_run(db, pipeline_key="wiki.default", pipeline_version="3",
                              trigger_type="manual_rebuild", wiki_page_id="w-leg",
                              workspace_id="ws-leg")
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert executed.output_revision_id
    db.close()
    engine.dispose()


def test_migration_head_and_lagging_db_compat(tmp_path):
    """启用后：P44 旧库缺演化表 → require 失败；升级 head → init_db + require 通过。"""
    db_path = tmp_path / "mig.db"
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
    r0 = subprocess.run(
        [sys.executable, "-m", "alembic", "-x",
         f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "a9b8c7d6e5f4"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(BACKEND_ROOT), env=env)
    assert r0.returncode == 0, r0.stderr[-2000:]
    from app.models import evolution as ev
    from app.models.database import get_engine
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    with pytest.raises(RuntimeError, match="evolution_schema_not_ready"):
        ev.require_evolution_schema(engine, context="test:feature-on")
    engine.dispose()

    r1 = subprocess.run(
        [sys.executable, "-m", "alembic", "-x",
         f"database_url=sqlite:///{db_path.as_posix()}", "upgrade", "head"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(BACKEND_ROOT), env=env)
    assert r1.returncode == 0, r1.stderr[-2000:]
    engine = get_engine(f"sqlite:///{db_path.as_posix()}")
    ev.require_evolution_schema(engine, context="test:after-head")
    assert ev.missing_evolution_tables(engine) == []
    engine.dispose()
