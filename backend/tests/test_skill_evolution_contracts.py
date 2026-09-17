"""WikiSkill 阶段 1 —— 契约/快照/边界单元测试（无真实编译执行）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.skill_evolution import runenv
from app.core.skill_evolution.contracts import DatasetError, load_dataset
from app.core.skill_evolution.errors import SnapshotError, TraceError
from app.core.skill_evolution.snapshot import SnapshotContent, SnapshotDoc, SnapshotStore
from app.core.skill_evolution.trace import TraceStore

REPO_DATASET = (
    Path(__file__).resolve().parent.parent
    / "eval" / "wiki_evolution" / "datasets" / "wiki-default-v1"
)


def _write_source(dirpath: Path, name: str, content: str) -> None:
    (dirpath / name).write_text(content, encoding="utf-8")


def make_mini_dataset(tmp_path: Path, *, tasks: list[dict], sources: dict[str, str],
                      dataset_version: str = "mini-v1",
                      grader_version: str = "wiki-default-grader/v1") -> Path:
    """构造临时任务集（两任务同 source 家族拆分冲突场景用）。"""
    d = tmp_path / "dataset"
    (d / "sources").mkdir(parents=True)
    for name, text in sources.items():
        _write_source(d / "sources", name, text)
    ref = {t["task_id"]: {"expected_points": [], "forbidden": []} for t in tasks}
    (d / "references").mkdir()
    (d / "references" / "ref.json").write_text(
        json.dumps(ref, ensure_ascii=False, indent=2), encoding="utf-8")
    full = []
    for t in tasks:
        full.append({
            "task_id": t["task_id"], "dataset_version": dataset_version,
            "domain": "wiki_compile.default", "split": t["split"],
            "group_id": t["group_id"], "input_snapshot_id": t["input_snapshot_id"],
            "instruction": "根据资料生成 Wiki。", "grader_version": grader_version,
            "reference_ref": "ref.json", "trigger": "manual_rebuild",
            "wiki_title": t.get("wiki_title", "合成主题"),
            "wiki_category": t.get("wiki_category", "资料"),
            "sources": t["sources"],
        })
    (d / "dataset.json").write_text(json.dumps({
        "dataset_version": dataset_version,
        "domain": "wiki_compile.default",
        "grader_version": grader_version,
        "tasks": full,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 契约 / 分组防泄漏
# ---------------------------------------------------------------------------


def test_repo_dataset_validates_and_no_cross_split_group_duplication():
    spec = load_dataset(REPO_DATASET)
    assert len(spec.tasks) >= 6
    groups = spec.group_split_map()  # 不抛 = 无跨 split 分组重复
    assert len(groups) == len({t.group_id for t in spec.tasks})
    for t in spec.tasks:
        assert groups[t.group_id] == t.split


def test_cross_split_group_duplication_detected(tmp_path):
    sources = {"s1.md": "适用条件：A。\n参数：扭矩 25 N·m。\n"}
    base = {"sources": [{"doc_id": "s1", "title": "资料A", "file": "s1.md"}],
            "input_snapshot_id": "snap-x"}
    d = make_mini_dataset(
        tmp_path, sources=sources,
        tasks=[
            {**base, "task_id": "t1", "split": "train",
             "group_id": "g-same"},
            {**base, "task_id": "t2", "split": "val",
             "group_id": "g-same"},
        ])
    with pytest.raises(DatasetError, match="跨 split"):
        load_dataset(d)


def test_dataset_requires_all_contract_fields(tmp_path):
    d = tmp_path / "ds"
    (d / "sources").mkdir(parents=True)
    (d / "sources" / "s1.md").write_text("x", encoding="utf-8")
    # 任务缺 instruction/grader_version/reference_ref 等契约字段。
    (d / "dataset.json").write_text(json.dumps({
        "dataset_version": "mini-v1", "domain": "wiki_compile.default",
        "grader_version": "wiki-default-grader/v1",
        "tasks": [{
            "task_id": "t1", "dataset_version": "mini-v1",
            "domain": "wiki_compile.default", "split": "train",
            "group_id": "g1", "input_snapshot_id": "s",
            "trigger": "manual_rebuild", "wiki_title": "主题",
            "wiki_category": "资料",
            "sources": [{"doc_id": "s1", "title": "t", "file": "s1.md"}],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DatasetError, match="缺少必填字段"):
        load_dataset(d)


def test_snapshot_id_cannot_map_different_content(tmp_path):
    root = tmp_path / "root"
    store = SnapshotStore(root)
    a = SnapshotContent(label="snap-1", wiki_title="T", wiki_category="C",
                        docs=(SnapshotDoc(doc_id="d1", title="t", content="25 N·m"),))
    store.store(a)
    b = SnapshotContent(label="snap-1", wiki_title="T", wiki_category="C",
                        docs=(SnapshotDoc(doc_id="d1", title="t", content="99 N·m"),))
    with pytest.raises(SnapshotError, match="不可覆盖"):
        store.store(b)


def test_snapshot_tamper_detected_on_load(tmp_path):
    root = tmp_path / "root"
    store = SnapshotStore(root)
    a = SnapshotContent(label="snap-1", wiki_title="T", wiki_category="C",
                        docs=(SnapshotDoc(doc_id="d1", title="t", content="25 N·m"),))
    store.store(a)
    target = root / "snapshots" / "snap-1"
    (target / "sources.json").write_text(
        json.dumps([{"doc_id": "d1", "title": "t", "content": "篡改内容"}]),
        encoding="utf-8")
    with pytest.raises(SnapshotError, match="封存校验失败"):
        store.load("snap-1")


# ---------------------------------------------------------------------------
# 路径与数据库边界
# ---------------------------------------------------------------------------


def test_experiment_root_requires_brand(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(runenv.ExperimentPathError, match="品牌"):
        runenv.ensure_experiment_root(plain)


def test_experiment_root_created_with_brand(tmp_path):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    assert (root / runenv.BRAND_FILENAME).is_file()
    # 再次调用幂等。
    assert runenv.ensure_experiment_root(root) == root


def test_reject_existing_database_and_business_url(tmp_path, monkeypatch):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    existing = root / "runs" / "x" / runenv.DB_FILENAME
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"not-empty")
    with pytest.raises(runenv.ExperimentPathError, match="已存在"):
        runenv.reject_unsafe_db_path(existing, root)
    # 与配置中业务库路径一致的新路径（即使不存在）也必须拒绝。
    biz = root / "runs" / "y" / "notes.db"
    biz.parent.mkdir(parents=True)
    monkeypatch.setattr("app.config.settings.database_url", f"sqlite:///{biz.as_posix()}")
    with pytest.raises(runenv.ExperimentPathError, match="业务数据库"):
        runenv.reject_unsafe_db_path(biz, root)


# ---------------------------------------------------------------------------
# Trace 封存
# ---------------------------------------------------------------------------


def test_trace_seal_failure_not_silent(tmp_path):
    from app.core.skill_evolution.trace import verify_sealed
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    tr = TraceStore(run_dir, {"execution_id": "e1"})
    tr.append({"kind": "start"})
    tr.seal()
    # 事件文件事后缺失 → 封存校验失败，不得静默通过。
    (run_dir / "events.jsonl").unlink()
    with pytest.raises(TraceError, match="缺少事件文件|封存校验失败"):
        verify_sealed(run_dir)


def test_trace_double_seal_rejected(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    tr = TraceStore(run_dir, {"execution_id": "e1"})
    tr.append({"kind": "start"})
    tr.seal()
    with pytest.raises(TraceError, match="重复封存"):
        tr.seal()
