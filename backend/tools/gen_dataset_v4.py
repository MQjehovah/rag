"""wiki-default-v4 实验数据集生成器（阶段 7A，合成资料）。

生成 24 案例：
- train 6（组 doc-family-v4e-train，同一 workspace，满足提议者 ≥4 条不同轨迹）；
- val 8（2 组）；test 8（2 组）。
来源为参数化模板自建合成资料（无私有/生产资料）；外部有效性受限需在报告注明。
覆盖：完整性（含适用/前置/步骤/参数）、数值正确、版本差异、冲突、边界与证据值。
参考答案（references）仅供评分器读取；manifest 记录生成方式/分组/文件哈希/暴露状态
（v4 此前从未用于任何调试/回归）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "eval/wiki_evolution/datasets/wiki-default-v4"

TRAIN_PARAMS = [
    ("v4e-t-01", 25, 60, "standard", 2.0, 3.0),
    ("v4e-t-02", 25, 60, "conflict", 2.0, 3.0),
    ("v4e-t-03", 28, 60, "boundary", 3.0, None),
    ("v4e-t-04", 32, 48, "standard", 3.0, 3.1),
    ("v4e-t-05", 25, 60, "omit_prereq", 2.0, 3.0),
    ("v4e-t-06", 28, 60, "evidence", 3.0, None),
]
VAL_PARAMS = [
    ("v4e-v-01", 25, 60, "standard", 2.0, 3.0, "val-a"),
    ("v4e-v-02", 28, 60, "boundary", 3.0, None, "val-a"),
    ("v4e-v-03", 32, 48, "conflict", 3.0, 3.1, "val-a"),
    ("v4e-v-04", 25, 60, "standard", 2.0, 3.0, "val-a"),
    ("v4e-v-05", 28, 48, "evidence", 3.0, None, "val-b"),
    ("v4e-v-06", 25, 60, "boundary", 2.0, 3.0, "val-b"),
    ("v4e-v-07", 32, 60, "standard", 3.0, None, "val-b"),
    ("v4e-v-08", 28, 48, "conflict", 3.0, 3.1, "val-b"),
]
TEST_PARAMS = [
    ("v4e-x-01", 25, 60, "standard", 2.0, 3.0, "test-x"),
    ("v4e-x-02", 28, 60, "conflict", 3.0, 3.1, "test-x"),
    ("v4e-x-03", 25, 48, "boundary", 2.0, 3.0, "test-x"),
    ("v4e-x-04", 32, 60, "standard", 3.0, None, "test-x"),
    ("v4e-x-05", 28, 48, "evidence", 3.0, None, "test-y"),
    ("v4e-x-06", 25, 60, "conflict", 2.0, 3.0, "test-y"),
    ("v4e-x-07", 32, 48, "standard", 3.0, 3.1, "test-y"),
    ("v4e-x-08", 28, 60, "boundary", 3.0, None, "test-y"),
]
GROUP_MAP = {
    "train": "doc-family-v4e-train",
    "val": {"val-a": "doc-family-v4e-val-a", "val-b": "doc-family-v4e-val-b"},
    "test": {"test-x": "doc-family-v4e-test-x", "test-y": "doc-family-v4e-test-y"},
}


def source_md(task_id: str, torque: int, voltage: int, kind: str,
              v1: float | None, v2: float | None, versioned: bool) -> str:
    lines = []
    if versioned and v1 and v2 and v1 != v2:
        lines.append(f"产品版本：{v1}。")
    lines += [
        f"适用条件：本资料适用于 {task_id} 对应的 Titan 模组（扭矩 {torque} N·m，"
        f"电压限值 {voltage}V）。",
        f"前置条件：安装前断电并确认电压不超过 {voltage}V。",
        f"步骤1：固定模组；步骤2：测量电压 {voltage}V；步骤3：紧固扭矩 {torque} N·m。",
        f"参数：紧固扭矩 {torque} N·m；电压限值 {voltage}V。",
    ]
    if versioned and v2:
        lines.append(f"（后续版本 {v2} 依据随箱标签：电压限值 {voltage}V。）")
    return "\n".join(lines) + "\n"


def ref_points(torque: int, voltage: int, kind: str) -> dict:
    points = [
        {"id": "p1", "kind": "phrase", "text": "适用条件"},
        {"id": "p2", "kind": "value", "value": f"{torque} N·m"},
        {"id": "p3", "kind": "value", "value": f"{voltage}V"},
        {"id": "p4", "kind": "phrase", "text": "步骤1"},
    ]
    return {"expected_points": points, "forbidden": ["120V"]}


def main() -> int:
    (OUT / "sources").mkdir(parents=True, exist_ok=True)
    (OUT / "references").mkdir(exist_ok=True)
    tasks, refs = [], {}
    files: list[Path] = []

    def add(task_id: str, split: str, group: str, torque: int, voltage: int,
            kind: str, v1: float | None, v2: float | None) -> None:
        src = f"{task_id}.md"
        (OUT / "sources" / src).write_text(
            source_md(task_id, torque, voltage, kind, v1, v2, True),
            encoding="utf-8")
        files.append(OUT / "sources" / src)
        versioned = bool(v1 and v2 and v1 != v2)
        version_tag = f"{v1}" if v1 else None
        tasks.append({
            "task_id": task_id, "dataset_version": "wiki-default-v4",
            "domain": "wiki_compile.default", "split": split,
            "group_id": group, "input_snapshot_id": f"v4-{task_id}",
            "instruction": ("根据资料生成 Wiki：覆盖适用条件、前置条件、操作步骤与"
                            "关键参数，数值与来源一致。"),
            "grader_version": "wiki-default-grader/v1",
            "reference_ref": "wiki-default-v4.json", "trigger": "manual_rebuild",
            "wiki_title": f"Titan 模组参数 {task_id}",
            "wiki_category": "产品资料",
            "sources": [{"doc_id": "d1", "title": f"资料 {task_id}", "file": src,
                         "product_version": version_tag}],
        })
        refs[task_id] = ref_points(torque, voltage, kind)

    for (tid, tq, tv, kind, v1, v2) in TRAIN_PARAMS:
        add(tid, "train", GROUP_MAP["train"], tq, tv, kind, v1, v2)
    for (tid, tq, tv, kind, v1, v2, sub) in VAL_PARAMS:
        add(tid, "val", GROUP_MAP["val"][sub], tq, tv, kind, v1, v2)
    for (tid, tq, tv, kind, v1, v2, sub) in TEST_PARAMS:
        add(tid, "test", GROUP_MAP["test"][sub], tq, tv, kind, v1, v2)

    manifest_payload = {
        "dataset_version": "wiki-default-v4",
        "generation": "synthetic parameterized templates (stage 7A)",
        "groups": GROUP_MAP,
        "task_count": len(tasks),
        "split_counts": {"train": len(TRAIN_PARAMS), "val": len(VAL_PARAMS),
                         "test": len(TEST_PARAMS)},
        "external_validity_limits": "合成资料，非真实生产文档；首次用于阶段 7 实验，"
                                    "未参与阶段 0–6 任何调试/回归（未见测试集）。",
        "exposure": "never exposed before v4 generation",
    }
    hashes = {}
    for f in sorted(files):
        hashes[str(f.relative_to(OUT))] = hashlib.sha256(
            f.read_bytes()).hexdigest()
    for tid in refs:
        pass
    ref_file = OUT / "references/wiki-default-v4.json"
    ref_file.write_text(json.dumps(refs, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    manifest_payload["file_hashes"] = hashes
    (OUT / "dataset.json").write_text(json.dumps({
        "dataset_version": "wiki-default-v4",
        "domain": "wiki_compile.default",
        "grader_version": "wiki-default-grader/v1",
        "tasks": tasks,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "manifest.json").write_text(
        json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"v4 generated: {len(tasks)} tasks in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
