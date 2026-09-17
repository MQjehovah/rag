"""任务集与任务契约（阶段 1，版本化、只读加载 + 校验）。

任务契约字段（方案 §4.1 子集 + 工程必需字段）：

task_id, dataset_version, domain, split, group_id, input_snapshot_id,
instruction, grader_version, reference_ref

外加执行必需字段：trigger（阶段 1 支持 manual_rebuild）、wiki 标题/分类、
来源文档（doc_id/title/file 及可选 product_version）。expected_points /
forbidden 属评分参考，只存在于 reference 文件（grader 私有），不进入本契约。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.skill_evolution.errors import DatasetError

# 阶段 1 支持的分割与触发类型。
VALID_SPLITS = ("train", "val", "test")
VALID_TRIGGERS = ("manual_rebuild",)

# 任务契约必填字段（RFC 2119 MUST）。
REQUIRED_TASK_FIELDS = (
    "task_id",
    "dataset_version",
    "domain",
    "split",
    "group_id",
    "input_snapshot_id",
    "instruction",
    "grader_version",
    "reference_ref",
    "trigger",
)

DATASET_FILENAME = "dataset.json"


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise DatasetError(message)


def canonical_json(data: Any) -> str:
    """确定性 JSON 序列化（排序键、ensure_ascii=False、无空格）。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SourceDoc:
    """任务来源资料（含元数据，供快照保存与物化）。"""

    doc_id: str
    title: str
    file: str = ""
    product_version: str | None = None  # 资料声明的产品版本（供模拟模型分组，非评分依据）

    def to_dict(self) -> dict:
        out = {"doc_id": self.doc_id, "title": self.title, "file": self.file}
        if self.product_version:
            out["product_version"] = self.product_version
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "SourceDoc":
        return cls(
            doc_id=str(data["doc_id"]),
            title=str(data["title"]),
            file=str(data.get("file") or ""),
            product_version=(data.get("product_version") or None),
        )


@dataclass(frozen=True)
class TaskSpec:
    """单个任务：契约字段 + 编译输入描述。"""

    task_id: str
    dataset_version: str
    domain: str
    split: str
    group_id: str
    input_snapshot_id: str
    instruction: str
    grader_version: str
    reference_ref: str
    trigger: str
    wiki_title: str
    wiki_category: str
    sources: tuple[SourceDoc, ...] = ()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "dataset_version": self.dataset_version,
            "domain": self.domain,
            "split": self.split,
            "group_id": self.group_id,
            "input_snapshot_id": self.input_snapshot_id,
            "instruction": self.instruction,
            "grader_version": self.grader_version,
            "reference_ref": self.reference_ref,
            "trigger": self.trigger,
            "wiki_title": self.wiki_title,
            "wiki_category": self.wiki_category,
            "sources": [s.to_dict() for s in self.sources],
        }

    @classmethod
    def from_dict(cls, data: dict, *, dataset_version: str, domain: str,
                  grader_version: str, dataset_dir: Path) -> "TaskSpec":
        _require(isinstance(data, dict), "task 必须是对象")
        missing = [f for f in REQUIRED_TASK_FIELDS if f not in data]
        _require(not missing, f"task 缺少必填字段: {', '.join(missing)}")
        task_id = str(data["task_id"])
        split = str(data["split"])
        trigger = str(data["trigger"])
        _require(split in VALID_SPLITS, f"task {task_id}: split 非法 {split!r}")
        _require(trigger in VALID_TRIGGERS, f"task {task_id}: trigger 非法 {trigger!r}")
        _require(str(data["dataset_version"]) == dataset_version,
                 f"task {task_id}: dataset_version 与数据集不一致")
        _require(str(data["domain"]) == domain, f"task {task_id}: domain 与数据集不一致")
        _require(str(data["grader_version"]) == grader_version,
                 f"task {task_id}: grader_version 与数据集不一致")
        _require(str(data["instruction"] or "").strip(),
                 f"task {task_id}: instruction 为空")
        _require(str(data.get("wiki_title") or "").strip(),
                 f"task {task_id}: wiki_title 为空")

        sources: list[SourceDoc] = []
        for raw in data.get("sources") or []:
            _require(isinstance(raw, dict) and raw.get("doc_id") and raw.get("title"),
                     f"task {task_id}: 来源声明非法")
            src = SourceDoc.from_dict(raw)
            if src.file:
                p = (dataset_dir / "sources" / src.file).resolve()
                _require(p.is_file(), f"task {task_id}: 来源文件缺失 {src.file!r}")
            sources.append(src)
        _require(sources, f"task {task_id}: 至少一个来源资料")

        return cls(
            task_id=task_id,
            dataset_version=dataset_version,
            domain=domain,
            split=split,
            group_id=str(data["group_id"]),
            input_snapshot_id=str(data["input_snapshot_id"]),
            instruction=str(data["instruction"]),
            grader_version=grader_version,
            reference_ref=str(data["reference_ref"]),
            trigger=trigger,
            wiki_title=str(data["wiki_title"]),
            wiki_category=str(data.get("wiki_category") or "资料"),
            sources=tuple(sources),
        )


@dataclass(frozen=True)
class DatasetSpec:
    """已加载并校验的任务集。"""

    dataset_dir: Path
    dataset_version: str
    domain: str
    grader_version: str
    tasks: tuple[TaskSpec, ...]

    def task(self, task_id: str) -> TaskSpec:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        raise DatasetError(f"未知 task_id: {task_id}")

    def group_split_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for t in self.tasks:
            if t.group_id in out and out[t.group_id] != t.split:
                raise DatasetError(
                    f"group_id {t.group_id!r} 出现在多个 split "
                    f"({out[t.group_id]}/{t.split}) —— 跨 split 分组重复（防泄漏）"
                )
            out[t.group_id] = t.split
        return out

    def split_summary(self) -> dict[str, dict]:
        groups = self.group_split_map()
        per_split: dict[str, dict] = {s: {"task_count": 0, "group_ids": []} for s in VALID_SPLITS}
        for t in self.tasks:
            per_split[t.split]["task_count"] += 1
            if t.group_id not in per_split[t.split]["group_ids"]:
                per_split[t.split]["group_ids"].append(t.group_id)
        return {"groups": groups, "splits": per_split}


def load_dataset(dataset_dir: Path) -> DatasetSpec:
    """加载 dataset.json 并做契约校验（task 字段/split/group/文件存在性）。"""
    dataset_dir = Path(dataset_dir).resolve()
    f = dataset_dir / DATASET_FILENAME
    _require(f.is_file(), f"数据集缺失 {f}")
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"dataset.json 解析失败: {exc}") from exc
    _require(isinstance(raw, dict), "dataset.json 必须是对象")
    version = str(raw.get("dataset_version") or "").strip()
    domain = str(raw.get("domain") or "").strip()
    grader = str(raw.get("grader_version") or "").strip()
    _require(version and domain and grader, "dataset.json 缺少 dataset_version/domain/grader_version")
    tasks_raw = raw.get("tasks")
    _require(isinstance(tasks_raw, list) and tasks_raw, "dataset.json tasks 缺失或为空")
    tasks = [
        TaskSpec.from_dict(t, dataset_version=version, domain=domain,
                           grader_version=grader, dataset_dir=dataset_dir)
        for t in tasks_raw
    ]
    seen_ids: set[str] = set()
    for t in tasks:
        _require(t.task_id not in seen_ids, f"task_id 重复: {t.task_id}")
        seen_ids.add(t.task_id)
    spec = DatasetSpec(
        dataset_dir=dataset_dir,
        dataset_version=version,
        domain=domain,
        grader_version=grader,
        tasks=tuple(tasks),
    )
    spec.group_split_map()  # 触发跨 split 分组重复检测
    return spec


def load_reference(dataset_dir: Path, reference_ref: str) -> dict:
    """加载评分参考（grader 私有；reference_ref 必须指向 dataset 内 references/ 文件）。"""
    path = (dataset_dir / "references" / reference_ref).resolve()
    if not path.is_file() or not path.is_relative_to(dataset_dir.resolve()):
        raise DatasetError(f"reference 缺失或越界: {reference_ref!r}")
    try:
        ref = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"reference {reference_ref} 解析失败: {exc}") from exc
    _require(isinstance(ref, dict), f"reference {reference_ref} 必须是对象")
    return ref
