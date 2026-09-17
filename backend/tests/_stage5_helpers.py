"""阶段 5 门控 e2e 演示/验收：基线 → 接受 → 持平拒绝 → 退化拒绝 → invalid。

候选技能正文携带模拟行为标记（STRICT-V1 / WEAK-SKIP-CONDITIONS /
FORCE-UNAVAILABLE），由同一 SimulatedModel 依据注入内容产生不同结果——禁止按
baseline/candidate 标签预设分数。候选通过真实 proposer 多轮工具循环 + 补丁校验
生成；评估走阶段 1 隔离执行 + grader；门控为严格整数比较。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.skill_evolution import proposer as prop
from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution.trace_sampling import group_workspace_id

DS_V3 = Path(__file__).resolve().parent.parent / "eval/wiki_evolution/datasets/wiki-default-v3"
DS_V2 = Path(__file__).resolve().parent.parent / "eval/wiki_evolution/datasets/wiki-default-v2"
REPO_SEED = Path(__file__).resolve().parent.parent / "eval/wiki_evolution/skills/seed-default-v1"


def env_with_v3(root):
    ds = load_dataset(DS_V3)
    ws = group_workspace_id("doc-family-titan810v3-train")
    for tid in [f"v3-train-0{i}" for i in range(1, 5)]:
        run_one(root, ds, ds.task(tid), profile="faithful",
                skills=FrozenSkillSet.disabled())
    return ds, ws


def make_proposal(env, marker: str, mode: str = "patch",
                 parent: str = "default:0001", extra: str | None = None):
    """真实 proposer 循环生成候选（读 4 条不同 v3 train 轨迹后提交补丁）。

    以父版本 SKILL.md 中行首为 “8. ” 的唯一整行作为 anchor，整行替换为携带 marker
    的新指令；父为 seed(default:0001) 或上一候选均可精确命中（不强依赖锚点前缀）。
    """
    db = skill_store.session_for(env["root"])
    try:
        parent_row = skill_store.get_version(db, parent)
    finally:
        db.close()
    base_line = next((ln for ln in parent_row.skill_md.splitlines()
                      if ln.startswith("8. ")), None)
    if base_line is None:
        raise RuntimeError("父版本缺少 8. 指令行")
    anchor = base_line
    directive_line = {
        "STRICT-V1": "8. 参数与结论必须与来源一致；输出 STRICT-V1 核对清单。",
        "STRICT-V1-ALT": "8. 参数与来源一致；结尾附 STRICT-V1 核对清单（逐条覆盖条件要点）。",
        "WEAK-SKIP-CONDITIONS": "8. 允许省略适用/前置条件要点（WEAK-SKIP-CONDITIONS）。",
        "FORCE-UNAVAILABLE": "8. 触发 FORCE-UNAVAILABLE 模拟不可用。",
    }[marker]

    def model(messages, context="", timeout=120.0):
        return prop.SimulatedProposer(profile="patch",
                                      parent_version_id="default:0001")(
            messages, context, timeout)

    class MarkerProposer:
        def __init__(self):
            self._inner = prop.SimulatedProposer(
                profile="patch", parent_version_id=parent)

        def __call__(self, messages, context="", timeout=120.0):
            out = self._inner(messages, context, timeout)
            if out.get("action") and out["action"].get("type") == "patch":
                out["action"]["ops"] = [{
                    "file": "SKILL.md", "type": "replace",
                    "anchor": anchor,
                    "replacement": directive_line,
                }]
            return out

    summary = prop.run_proposer(env["root"], env["ds"], env["ws"],
                                runner=MarkerProposer(), profile="patch",
                                parent_version_id=parent,
                                idempotency_extra=extra or marker)
    return summary.proposal_id, summary


def base_root(tmp_path) -> dict:
    root = runenv.ensure_experiment_root(tmp_path / "root")
    db = skill_store.session_for(root)
    try:
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
    finally:
        db.close()
    return {"root": root, "seed": seed}
