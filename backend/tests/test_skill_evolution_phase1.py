"""WikiSkill 阶段 1 —— 隔离执行集成测试（真实 wiki.default v3 编译 + fake runner）。

关键保证（任务书第六节）：
1. 复用真实 v3 编译核心（executor.create_run/execute_run，非自制模拟流水线）；
2. 实验执行不修改业务 Wiki 或生产数据库；
3. 原始资料变化后旧快照仍可重放；
4. 同一快照重跑生成独立记录（新 execution_id，不覆盖历史）；
5. 模型请求不含评分参考；
6. 跨 split 分组重复被检测（单元层已覆盖，此处保留集成断言）；
7. 模型失败/内容失败/成功执行分类正确；
8. 完整流水线没有未注入的外部网络调用；
9. 错误数据库路径 / 损坏快照 / 封存失败不静默（单元层覆盖 + 此处路径校验）；
10. 重放 = 恢复相同输入与配置再次执行。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.core.skill_evolution import runenv
from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.snapshot import SnapshotStore
from app.core.skill_evolution.trace import TraceError, iter_events, load_meta, verify_sealed

REPO_DATASET = (
    Path(__file__).resolve().parent.parent
    / "eval" / "wiki_evolution" / "datasets" / "wiki-default-v1"
)

V3_STAGE_KEYS = (
    "resolve_context", "topic_route", "skill_route", "synthesize_by_skill",
    "validate_by_skill", "publish_by_skill", "finalize_compile_outcome",
    "schedule_graph",
)


@pytest.fixture(autouse=True)
def _isolate_registries_and_runners():
    """每个测试前后清理进程内 pipeline 注册与外部 runner（防跨测试泄漏）。"""
    from app.core.wiki_pipeline import executor, registry

    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    executor.reset_external_runners()
    registry.REGISTRY.clear()


@pytest.fixture()
def dataset():
    return load_dataset(REPO_DATASET)


def _run(tmp_path, dataset, task_id, profile, snapshot=None):
    root = tmp_path / "exp-root"
    task = dataset.task(task_id)
    return run_one(root, dataset, task, profile=profile, snapshot=snapshot)


def _grade(dataset, res: dict, task_id: str) -> dict:
    from app.core.skill_evolution.grader import grade as _grade_fn
    task = dataset.task(task_id)
    return _grade_fn(outcome=res["meta"]["outcome"], candidate=res["candidate"],
                     reference_ref=task.reference_ref,
                     dataset_dir=dataset.dataset_dir, task_id=task_id)


# -- 1. 复用真实 v3 编译核心 -------------------------------------------------


def test_uses_real_v3_pipeline_and_full_stage_chain(tmp_path, dataset):
    res = _run(tmp_path, dataset, "wiki-default-001", "faithful")
    meta = res["meta"]
    assert meta["pipeline_key"] == "wiki.default"
    assert meta["pipeline_version"] == "3"
    assert meta["run_status"] == "succeeded"
    assert meta["outcome"]["published"] is True
    keys = [st["stage_key"] for st in meta["stages"]]
    assert keys == list(V3_STAGE_KEYS)
    assert meta["candidate"]["revision_id"]
    # 真实产物 artifact 链（wiki_publish_manifest 由真实 publish 阶段产出）。
    assert "wiki_publish_manifest" in meta["artifact_types"]


# -- 2. 业务库 / 业务 Wiki 不被修改 ------------------------------------------


def test_business_db_untouched(tmp_path, dataset, monkeypatch):
    # 造一个“业务库”：独立 sqlite + 哨兵行。
    from app.models.database import Notebook, init_db
    biz_db = tmp_path / "business" / "notes.db"
    biz_db.parent.mkdir()
    engine = runenv.make_experiment_engine(biz_db)
    init_db(engine)
    db = runenv.make_session(engine)
    db.add(Notebook(id="prod-nb", name="生产库", group_id="__public__"))
    db.commit()
    n_before = db.query(Notebook).count()
    db.close()
    engine.dispose()
    before_hash = _file_sha256(biz_db)

    orig_url = __import__("app.config", fromlist=["settings"]).settings.database_url
    monkeypatch.setattr("app.config.settings.database_url",
                        f"sqlite:///{biz_db.as_posix()}")
    _run(tmp_path, dataset, "wiki-default-001", "faithful")
    assert __import__("app.config", fromlist=["settings"]).settings.database_url \
        == f"sqlite:///{biz_db.as_posix()}"  # 实验不得改写配置

    engine2 = runenv.make_experiment_engine(biz_db)
    db2 = runenv.make_session(engine2)
    assert db2.query(Notebook).count() == n_before
    assert db2.query(Notebook).filter(Notebook.id == "prod-nb").one().name == "生产库"
    db2.close()
    engine2.dispose()
    assert _file_sha256(biz_db) == before_hash  # 业务库字节未变


def _file_sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- 3. 原始资料变化后，旧快照仍可重放 --------------------------------------


def test_snapshot_replay_independent_of_source_mutation(tmp_path, dataset):
    # 复制数据集到临时目录并首次运行（在复制品上只读运行）。
    copy_ds = tmp_path / "ds-copy"
    shutil.copytree(REPO_DATASET, copy_ds)
    ds = load_dataset(copy_ds)
    first = _run(tmp_path, ds, "wiki-default-001", "faithful")
    first_body = (Path(first["run_dir"]) / "output.md").read_text(encoding="utf-8")
    first_hash = first["meta"]["snapshot_content_hash"]

    # 篡改原始资料文件（同一 dataset 副本）。
    (copy_ds / "sources" / "a1.md").write_text(
        "篡改后的完全不同的内容：扭矩 999 N·m。", encoding="utf-8")
    # 从快照存储重放（不读被篡改的 dataset 文件）。
    snapshot = SnapshotStore(tmp_path / "exp-root").load("snapshot-001")
    second = _run(tmp_path, ds, "wiki-default-001", "faithful", snapshot=snapshot)
    second_body = (Path(second["run_dir"]) / "output.md").read_text(encoding="utf-8")
    assert second["meta"]["snapshot_content_hash"] == first_hash
    assert second_body == first_body  # 输入恢复一致（重放语义）


# -- 4. 同一快照重跑生成独立记录 --------------------------------------------


def test_rerun_creates_independent_execution(tmp_path, dataset):
    a = _run(tmp_path, dataset, "wiki-default-001", "faithful")
    b = _run(tmp_path, dataset, "wiki-default-001", "faithful")
    assert a["execution_id"] != b["execution_id"]
    run_a = tmp_path / "exp-root" / "runs" / a["execution_id"]
    run_b = tmp_path / "exp-root" / "runs" / b["execution_id"]
    assert run_a.is_dir() and run_b.is_dir()
    # 两条记录各自独立封存，互不覆盖。
    assert verify_sealed(run_a)
    assert verify_sealed(run_b)
    assert a["meta"]["run_status"] == b["meta"]["run_status"] == "succeeded"
    assert (run_a / "output.md").read_text(encoding="utf-8") == \
        (run_b / "output.md").read_text(encoding="utf-8")


# -- 5. 模型请求不含评分参考 -------------------------------------------------


def test_model_messages_do_not_contain_reference(tmp_path, dataset):
    res = _run(tmp_path, dataset, "wiki-default-003", "faithful")
    reference_text = (REPO_DATASET / "references" / "wiki-default-v1.json").read_text(
        encoding="utf-8")
    events = list(iter_events(Path(res["run_dir"])))
    calls = [e for e in events if e.get("kind") == "model_call"]
    assert calls, "应捕获至少一次模型调用"
    for call in calls:
        for msg in call.get("messages") or []:
            content = msg.get("content") or ""
            assert "expected_points" not in content
            assert reference_text not in content
            assert "forbidden" not in content


# -- 6/7. 分类：成功 / 模型失败 / 内容失败 ------------------------------------


def test_classification_matrix(tmp_path, dataset):
    ok = _run(tmp_path, dataset, "wiki-default-001", "faithful")
    assert ok["meta"]["outcome"]["failure_kind"] is None
    assert ok["meta"]["outcome"]["run_ok"] is True

    model_fail = _run(tmp_path, dataset, "wiki-default-001", "unavailable")
    assert model_fail["meta"]["run_status"] == "failed"
    assert model_fail["meta"]["outcome"]["failure_kind"] == "infra_or_model"
    assert model_fail["meta"]["outcome"]["published"] is False

    content_fail = _run(tmp_path, dataset, "wiki-default-001", "invalid_json")
    assert content_fail["meta"]["run_status"] == "failed"
    assert content_fail["meta"]["outcome"]["failure_kind"] == "content_compile"

    # 通过但要点缺失（遗漏）→ 成功 + checks 失败（编译产物质量失败，非基础设施）。
    omit = _run(tmp_path, dataset, "wiki-default-002", "omit_conditions")
    assert omit["meta"]["run_status"] == "succeeded"
    assert omit["meta"]["outcome"]["published"] is True
    assert omit["meta"]["outcome"]["failure_kind"] is None
    g = _grade(dataset, omit, "wiki-default-002")
    assert g["verdict"] == "fail"
    assert g["checks_ok"] is False


# -- 8. 无未注入的外部调用 ----------------------------------------------------


def test_no_undetected_external_calls(tmp_path, dataset, monkeypatch):
    from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
    import app.core.wiki_pipeline.pipelines.wiki_default as wd

    fired: list[str] = []

    def _boom_real_llm(*a, **k):
        fired.append("call_wiki_llm_json")
        raise AssertionError("不应触发真实 LLM 客户端")

    def _boom_default_runner(*a, **k):
        fired.append("_default_llm_runner")
        raise AssertionError("不应回退默认 runner")

    monkeypatch.setattr(builder, "call_wiki_llm_json", _boom_real_llm)
    monkeypatch.setattr(wd, "_default_llm_runner", _boom_default_runner)

    res = _run(tmp_path, dataset, "wiki-default-001", "faithful")
    assert fired == []
    assert res["meta"]["usage"]["model_calls"] == 1
    assert res["meta"]["usage"]["model_error_calls"] == 0
    assert res["meta"]["usage"]["usage_tokens"] is None  # 供应商用量=null，非 0
    assert res["meta"]["usage"]["usage_measured"] is False
    # 记录中的响应与请求一一对应，非只记最后一次。
    events = list(iter_events(Path(res["run_dir"])))
    calls = [e for e in events if e.get("kind") == "model_call"]
    assert len(calls) == 1 and calls[0]["response"] is not None
    assert res["meta"]["usage"]["graph_targets"] >= 0


# -- 10. 版本化 / 跨版本归属与差异（004 faithful 全过 / 005 flatten 失败） ------


def test_versioned_ownership_and_conflict_checks(tmp_path, dataset):
    good = _run(tmp_path, dataset, "wiki-default-004", "faithful")
    assert good["meta"]["run_status"] == "succeeded"
    g = _grade(dataset, good, "wiki-default-004")
    assert g["verdict"] == "pass", [i["detail"] for i in g["item_results"] if not i["passed"]]

    bad = _run(tmp_path, dataset, "wiki-default-005", "flatten_versions")
    assert bad["meta"]["run_status"] == "succeeded"
    g2 = _grade(dataset, bad, "wiki-default-005")
    assert g2["verdict"] == "fail"
    failed = [i for i in g2["item_results"] if not i["passed"]]
    kinds = {i["kind"] for i in failed}
    assert "absent_in_version" in kinds or "value_in_version" in kinds


# -- 数据集/评分器配置错误与 grader 版本冻结 -----------------------------------


def test_grader_config_errors_raise_not_silent(tmp_path, dataset):
    from app.core.skill_evolution.errors import GraderConfigError
    res = _run(tmp_path, dataset, "wiki-default-001", "faithful")
    # 未知 task → 配置错误。
    from app.core.skill_evolution.grader import grade
    with pytest.raises(GraderConfigError, match="缺少 task"):
        grade(outcome=res["meta"]["outcome"], candidate=res["candidate"],
              reference_ref="wiki-default-v1.json",
              dataset_dir=REPO_DATASET, task_id="does-not-exist")


def test_cross_split_group_duplication_detected_integration(tmp_path):
    from app.core.skill_evolution.contracts import load_dataset
    from app.core.skill_evolution.errors import DatasetError
    d = tmp_path / "ds"
    (d / "sources").mkdir(parents=True)
    (d / "sources" / "s1.md").write_text("适用条件：A。\n参数：扭矩 25 N·m。\n",
                                         encoding="utf-8")
    src = {"doc_id": "s1", "title": "资料A", "file": "s1.md"}
    tasks = []
    for tid, split in (("t1", "train"), ("t2", "test")):
        tasks.append({
            "task_id": tid, "dataset_version": "mini-v1",
            "domain": "wiki_compile.default", "split": split,
            "group_id": "g-same", "input_snapshot_id": "snap-x",
            "instruction": "根据资料生成 Wiki。",
            "grader_version": "wiki-default-grader/v1",
            "reference_ref": "ref.json", "trigger": "manual_rebuild",
            "wiki_title": "主题", "wiki_category": "资料",
            "sources": [src],
        })
    (d / "dataset.json").write_text(json.dumps({
        "dataset_version": "mini-v1", "domain": "wiki_compile.default",
        "grader_version": "wiki-default-grader/v1", "tasks": tasks,
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DatasetError, match="跨 split"):
        load_dataset(d)
