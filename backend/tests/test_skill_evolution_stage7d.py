"""v5 正式实验数据集与评分校准离线验收（无模型、不运行演化/评测）。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from app.core.skill_evolution.contracts import load_dataset

BACKEND = Path(__file__).resolve().parent.parent
DS = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v5"
CALIB = BACKEND / "eval/wiki_evolution/calibration/calibration-v5.json"
DOC = BACKEND.parent / "docs" / "wikiskill" / "calibration-v5.md"


@pytest.fixture(scope="module")
def ds5():
    return load_dataset(DS)


def test_v5_structure_and_exposure(ds5):
    ds = ds5
    assert ds.dataset_version == "wiki-default-v5"
    assert ds.grader_version == "wiki-default-grader/v1"
    manifest = json.loads((DS / "manifest.json").read_text(encoding="utf-8"))
    raw = json.loads((DS / "dataset.json").read_text(encoding="utf-8"))
    fam_of = {t["task_id"]: t.get("source_family", "?") for t in raw["tasks"]}
    counts = {"train": 0, "val": 0, "test": 0}
    group_split: dict[str, set[str]] = {}
    train_fams: set[str] = set()
    train_sizes: dict[str, int] = {}
    for t in ds.tasks:
        counts[t.split] += 1
        group_split.setdefault(t.group_id, set()).add(t.split)
        if t.split == "train":
            train_fams.add(fam_of[t.task_id])
            train_sizes[t.group_id] = train_sizes.get(t.group_id, 0) + 1
    assert counts == {"train": 10, "val": 14, "test": 15}
    assert all(len(v) == 1 for v in group_split.values())   # 组→split 单射
    assert len(train_fams) >= 2 and len(train_sizes) >= 3
    assert max(train_sizes.values()) >= 4                   # ≥4 互异训练轨迹来源组
    assert manifest["exposure"]["author_seen"] is True
    scope = manifest["exposure"].get("author_scope", "")
    assert "未进入任何执行/维护/提议/验证角色上下文" in scope
    # 参考答案仅供评分器读取（reference_ref 与参考文件齐全）
    assert (DS / "references/wiki-default-v5.json").is_file()
    assert all(t.reference_ref == "wiki-default-v5.json" for t in ds.tasks)
    # 每任务至少一个来源文件；conf 家族任务 = 双资料
    for t in ds.tasks:
        assert len(t.sources) >= 1
    conf_tasks = [t for t in ds.tasks if fam_of[t.task_id] == "多资料冲突与适用范围"]
    assert conf_tasks and all(len(t.sources) == 2 for t in conf_tasks)
    # 内容哈希一致（manifest 抽样全量复核）
    hashes = manifest["file_hashes"]
    assert hashes
    for rel, h in list(hashes.items()):
        assert hashlib.sha256((DS / rel).read_bytes()).hexdigest() == h


def test_v5_grader_calibration_boundaries():
    """锁定 v1 评分器当前语义（不改动评分器）；六项探针结果如实记录。"""
    sys.path.insert(0, str(BACKEND / "tools"))
    from grade_calibration_v5 import run_calibration
    report = run_calibration()
    results = report["results"]
    # 版本混淆 / 关键遗漏 → 能检出（fail）
    assert results["version_mixup"]["verdict"] == "fail"
    assert results["key_omission"]["verdict"] == "fail"
    # 关键词齐全但结论错误 / 无证据自由补充 / 适用条件混淆 → 当前误通过（pass）
    assert results["keywords_ok_wrong_conclusion"]["verdict"] == "pass"
    assert results["no_evidence_free_supplement"]["verdict"] == "pass"
    assert results["applicability_confusable"]["verdict"] == "pass"
    # 合理同义表达 → 当前被判失败（漏报）
    assert results["synonym_ok_rewrite"]["verdict"] == "fail"
    assert CALIB.is_file() and DOC.is_file()
    calib = json.loads(CALIB.read_text(encoding="utf-8"))
    assert calib["grader"] == "wiki-default-grader/v1（未修改；不改语义迎合新案例）"
    assert len(calib["pending_human_review"]) >= 3
