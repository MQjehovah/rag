"""生成 v2 专用开发数据集 wiki-default-v2dev（Grader v2 接线用；不覆盖 v1–v5）。

内容全部为本脚本新写/参数化的开发资料（非 A1–A5、非既有 test 样例）。
任务 reference 内同时含：
- expected_points（v1 形状，仅保持结构一致；本数据集 grader_version 为 v2）；
- v2_spec（claims 含 主体/谓词/数值单位/版本/条件，coverage 覆盖主题，conflicts
  声明式矛盾）——v2_spec 属私有评分要求，只在评分端读取，不进入执行/维护/提议上下文。

覆盖目标（供 stage8g 测试）：
- 数值单位归一（mm/毫米、°C/℃、V/伏）；
- 版本区段归属（version 字段）与跨区段值；
- coverage 覆盖主题；
- 主体-谓词-宾语断言（subject/predicate/object）；
- 声明式矛盾（same_condition）；
- 一个确定性失败任务与一个数值单位别名任务。

用法（backend 目录）：
python tools/gen_dataset_v2dev.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.skill_evolution.grader_registry import GRADER_V2

OUT = Path(__file__).resolve().parent.parent / "eval" / "wiki_evolution" / "datasets" / "wiki-default-v2dev"

DOMAIN = "wiki_compile.default"
VERSION = "wiki-default-v2dev"

# ---------------------------------------------------------------------------
# 开发资料（全部为本脚本新写内容）
# ---------------------------------------------------------------------------


def _doc(doc_id: str, title: str, lines: list[str]) -> str:
    return "\n".join([f"# {title}"] + lines) + "\n"


DEV_DOCS = {
    "d1": _doc("d1", "V300 设备安装（v2dev 开发样例）", [
        "适用条件：室内环境，供电 220V。",
        "前置条件：断电，佩戴绝缘手套。",
        "操作步骤：打开箱体，安装导轨，锁紧螺栓（扭矩 8N·m）。",
        "版本 1.0：控制器支持 50 Hz。",
        "版本 1.1：控制器支持 60 Hz。",
        "线缆直径要求：≥6mm（毫米）。",
        "温度要求：25°C（摄氏度）。",
        "电流规格：5V 信号线。",
    ]),
    "d2": _doc("d2", "V300 巡检清单（v2dev 开发样例）", [
        "巡检项目：供电电压、线缆、温度。",
        "供电电压目标：220V（伏）。",
        "允许温度：25°C；超限需停检。",
        "巡检周期：每天一次。",
    ]),
    "d3": _doc("d3", "V300 故障排查（v2dev 开发样例）", [
        "现象：无法开机。",
        "排查步骤：检查供电 220V 是否正常；检查线缆 ≥6mm；检查温度记录。",
        "版本 2.0：不同地区的频率可不同（50 Hz / 60 Hz）。",
    ]),
}


def _task(task_id: str, split: str, group: str, title: str,
          sources: list[str], spec: dict, forbidden: list[str]) -> dict:
    return {
        "task_id": task_id, "dataset_version": VERSION, "domain": DOMAIN,
        "split": split, "group_id": group,
        "input_snapshot_id": f"{VERSION}-snap-{task_id}",
        "instruction": "根据资料生成 Wiki 正文：覆盖适用条件、操作步骤与关键参数，"
                       "数值单位须与来源一致；禁止引入资料外事实。",
        "grader_version": GRADER_V2,
        "reference_ref": f"{VERSION}.json",
        "trigger": "manual_rebuild",
        "wiki_title": title,
        "wiki_category": "操作指南",
        "sources": [{"doc_id": s, "title": s, "file": f"{s}.md"}
                    for s in sources],
        "v2_spec": spec,
        "v2_forbidden": forbidden,
    }


def _spec_for_val_pass() -> dict:
    return {
        "claims": [
            {"cid": "voltage", "subject": "供电电压", "predicate": "为",
             "object": "220", "unit": "V"},
            {"cid": "temp", "subject": "温度", "predicate": "为",
             "object": "25", "unit": "°C"},
        ],
        "coverage": [{"topic": "操作步骤"}, {"topic": "适用条件"}],
        "conflicts": [],
    }


def _spec_for_val_fail() -> dict:
    """确定性失败：温度上限 40°C（unit °C 受支持；基线输出无 40°C →
    数值单元检查明确 fail；候选技能注入后输出 40°C → 通过）。"""
    return {
        "claims": [
            {"cid": "temp_lim", "subject": "温度上限", "predicate": "≤",
             "object": 40, "unit": "°C"},
            {"cid": "voltage", "subject": "供电电压", "predicate": "为",
             "object": "220", "unit": "V"},
        ],
        "coverage": [{"topic": "故障排查"}],
        "conflicts": [],
    }


def _spec_for_val_versioned() -> dict:
    """版本区段归属：值必须出现在对应版本（v1.0=50Hz / v1.1=60Hz）。"""
    return {
        "claims": [
            {"cid": "v50", "subject": "频率 v1.0", "predicate": "支持",
             "object": "50", "unit": "", "version": "v1.0"},
            {"cid": "v60", "subject": "频率 v1.1", "predicate": "支持",
             "object": "60", "unit": "", "version": "v1.1"},
        ],
        "coverage": [{"topic": "版本 v1.1"}],
        "conflicts": [],
    }


def _spec_for_val_alias() -> dict:
    """数值单位别名：mm 与 毫米 等价（确定性 A 级）。"""
    return {
        "claims": [
            {"cid": "cable", "subject": "线缆直径", "predicate": "≥",
             "object": "6", "unit": "mm"},
        ],
        "coverage": [{"topic": "线缆"}],
        "conflicts": [],
    }


def _spec_train(i: int) -> dict:
    return {
        "claims": [{"cid": f"t{i}v", "subject": "供电电压", "predicate": "为",
                    "object": "220", "unit": "V"},
                   {"cid": f"t{i}t", "subject": "温度", "predicate": "为",
                    "object": "25", "unit": "°C"}],
        "coverage": [{"topic": "操作步骤"}, {"topic": "巡检"}],
        "conflicts": [],
    }


def _task_versioned_content() -> str:
    """版本化正文（供执行链路输出与 spec 匹配的区段内容）。"""
    return "版本 v1.0：控制器支持 50Hz。\n版本 v1.1：控制器支持 60Hz。"


def main() -> int:
    if OUT.exists():
        # 幂等：不允许覆盖已有开发数据集内容（防误覆盖）。
        raise SystemExit(f"目标目录已存在，拒绝覆盖: {OUT}")
    sources_dir = OUT / "sources"
    refs_dir = OUT / "references"
    sources_dir.mkdir(parents=True)
    refs_dir.mkdir(parents=True)

    for doc_id, content in DEV_DOCS.items():
        (sources_dir / f"{doc_id}.md").write_text(content, encoding="utf-8")

    group_train = "dev-v2-family-train"
    group_val = "dev-v2-family-val"
    tasks = []
    for i in range(1, 6):
        tasks.append(_task(
            f"vd2-train-0{i}", "train", group_train,
            f"V300 安装与巡检 {i}", ["d1", "d2"], _spec_train(i), []))
    tasks.append(_task("vd2-val-pass", "val", group_val, "V300 安装要点",
                       ["d1", "d2"], _spec_for_val_pass(), []))
    tasks.append(_task("vd2-val-fail", "val", group_val, "V300 频率要求",
                       ["d1", "d3"], _spec_for_val_fail(), []))
    tasks.append(_task("vd2-val-versioned", "val", group_val, "V300 版本频率",
                       ["d1"], _spec_for_val_versioned(), ["60Hz 泄漏进 v1.0"]))
    # 结构性不可修复失败：线缆直径 8mm（资料只有 6mm）→ 永远 fail（占位非满分）
    tasks.append(_task("vd2-val-fail2", "val", group_val, "V300 线缆要求",
                       ["d1", "d2"], {
                           "claims": [
                               {"cid": "cable8", "subject": "线缆直径",
                                "predicate": "≥", "object": 8, "unit": "mm"}],
                           "coverage": [], "conflicts": []}, []))

    dataset = {
        "dataset_version": VERSION, "domain": DOMAIN,
        "grader_version": GRADER_V2,
        "description": "Grader v2 接线专用开发样例（非效果/校准数据；v2_spec 私有"
                       "评分要求仅供评分端读取）。train 5（单家族，满足提议者 ≥4"
                       "不同轨迹前置）、val 4（pass / 温度上限 fail / 版本归属 / "
                       "结构性 fail2 占位非满分 / 单位别名）。",
        "tasks": tasks,
    }

    # reference：每一任务 1) v1 形状 expected_points（不启用）2) v2_spec
    refs = {"dataset_version": VERSION}
    for t in tasks:
        spec = t["v2_spec"]
        # v2_spec 内容已含全部评分要求；v1 形状仅占位不参与评分（grader=v2）。
        refs[t["task_id"]] = {
            "v2_spec": spec,
            "v2_forbidden": t.get("v2_forbidden") or [],
            "expected_points": [],
            "forbidden": t.get("v2_forbidden") or [],
        }
    (OUT / "dataset.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=1), encoding="utf-8")
    (refs_dir / f"{VERSION}.json").write_text(
        json.dumps(refs, ensure_ascii=False, indent=1), encoding="utf-8")
    # 校验可加载
    from app.core.skill_evolution.contracts import load_dataset
    ds = load_dataset(OUT)
    assert ds.dataset_version == VERSION and ds.grader_version == GRADER_V2
    assert len([t for t in ds.tasks if t.split == "train"]) == 5
    assert len([t for t in ds.tasks if t.split == "val"]) == 4
    print(f"generated {OUT} tasks={len(ds.tasks)} grader={ds.grader_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
