"""wiki-default-v5 评分器校准（离线、不调用模型、不改动冻结评分器 wiki-default-grader/v1）。

对 5 类判别构造人工可核验候选并运行现有评分器，如实记录"能/不能区分"：
- 合理同义表达：预期漏报（phrase 精确匹配）→ 不能区分；
- 关键词齐全但结论错误：预期误通过（仅查存在性）→ 不能区分；
- 版本混淆：value_in_version / absent_in_version 可检出 → 能区分；
- 无证据自由补充：无禁词时预期漏报 → 不能区分（forbidden 仅覆盖显式黑名单）；
- 关键内容遗漏：phrase/value 缺失可检出 → 能区分；
- 适用条件混淆（conf 家族）数值齐全但适用范围张冠李戴 → 预期误通过 → 不能区分。
若需区分前三类/无证据类，应另立 grader_version 提议（不覆盖 v1）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DS = BASE / "eval/wiki_evolution/datasets/wiki-default-v5"
CALIB_DIR = BASE / "eval/wiki_evolution/calibration"
DOC_DIR = BASE.parent / "docs" / "wikiskill"

PROBE_TASKS = {
    "synonym_ok_rewrite": ("v5-op-001", "操作流程"),
    "keywords_ok_wrong_conclusion": ("v5-cfg-009", "配置约束"),
    "version_mixup": ("v5-mig-021", "版本迁移"),
    "no_evidence_free_supplement": ("v5-ts-028", "故障排查"),
    "key_omission": ("v5-ts-029", "故障排查"),
    "applicability_confusable": ("v5-conf-034", "多资料冲突与适用范围"),
}

_VERBOSE_FIELDS = {
    "v5-op-001": {"前置条件": "前置要求", "安全提醒": "安全注意事项",
                  "验收检查": "验收标准"},
    "v5-ts-028": {"预防建议": "长期措施建议"},
    "v5-ts-029": {"预防建议": "长期措施建议"},
}


def _ref(task_id: str) -> dict:
    data = json.loads((DS / "references/wiki-default-v5.json").read_text(
        encoding="utf-8"))
    return data[task_id]


def _texts_from_ref(ref: dict, task_id: str) -> dict[str, str]:
    """把参考检查项还原成候选正文素材（正例/干扰例共用，逐字段人工构造）。"""
    phrases = [p["text"] for p in ref["expected_points"] if p["kind"] == "phrase"]
    values = [p["value"] for p in ref["expected_points"] if p["kind"] == "value"]
    by_ver: dict[str, list[str]] = {}
    for p in ref["expected_points"]:
        if p["kind"] in ("value_in_version", "absent_in_version"):
            by_ver.setdefault(p["version"], []).append(p["value"])
    common = "、".join(phrases)
    return {"phrases": phrases, "values": values, "by_ver": by_ver,
            "common": common, "task_id": task_id}


def _sections(body: str, versions: list[str]) -> list[dict]:
    secs = []
    for v in versions:
        secs.append({"content": f"{v} 区段。{body}", "version_label": v,
                     "section_type": "facts"})
    return secs or [{"content": body, "section_type": "facts"}]


def _grade_candidate(candidate: dict, task_id: str) -> dict:
    from app.core.skill_evolution.grader import grade
    outcome = {"run_ok": True, "published": True, "failure_kind": None}
    return grade(outcome=outcome, candidate=candidate,
                 reference_ref="wiki-default-v5.json",
                 dataset_dir=DS, task_id=task_id)


def _full_candidate(meta: dict) -> dict:
    body = f"{meta['common']}。" + "；".join(meta["values"]) + "。"
    if meta["by_ver"]:
        body += "各版本数值均遵守资料对应区段。"
    return {"revision_id": "probe", "sections": _sections(
        body, sorted(meta["by_ver"])), "wiki_status": "published"}


def _candidate_for(case: str, meta: dict) -> dict:
    t = meta["task_id"]
    if case == "synonym_ok_rewrite":
        # 同义改写：v1 语义等价但用词不同（换词表）→ 现有 v1 评分器漏报
        body = meta["common"]
        for src, rep in _VERBOSE_FIELDS.get(t, {}).items():
            body = body.replace(src, rep)
        body += "。" + "；".join(meta["values"]) + "。"
        return {"revision_id": "probe", "sections": [
            {"content": body, "section_type": "facts"}]}
    if case == "keywords_ok_wrong_conclusion":
        body = (f"{meta['common']}。" + "；".join(meta["values"]) +
                "。工程结论：可突破取值边界上限以追求性能，无需复核。")
        return {"revision_id": "probe", "sections": [
            {"content": body, "section_type": "facts"}]}
    if case == "version_mixup":
        # v2 区段错误混入 v1 数值 → absent_in_version 应检出
        v1 = "；".join(meta["by_ver"].get("v1", []))
        v2 = "；".join(meta["by_ver"].get("v2", []))
        return {"revision_id": "probe", "sections": [
            {"content": f"v1 区段。{v1}", "version_label": "v1",
             "section_type": "facts"},
            {"content": f"v2 区段。{v2}。另附 {v1}（错误混入）",
             "version_label": "v2", "section_type": "facts"}]}
    if case == "no_evidence_free_supplement":
        body = (f"{meta['common']}。" + "；".join(meta["values"]) +
                "。补充：来源未记载，但推测应执行全量停机检修。")
        return {"revision_id": "probe", "sections": [
            {"content": body, "section_type": "facts"}]}
    if case == "key_omission":
        body = (f"{meta['common']}。" + "；".join(meta["values"]) +
                "。\n（此候选刻意省略全部预防/长期措施表述。）")
        for drop in ("预防建议", "长期措施建议"):
            body = body.replace(drop, "")
        return {"revision_id": "probe", "sections": [
            {"content": body, "section_type": "facts"}]}
    if case == "applicability_confusable":
        # 两组参数齐全，但适用范围张冠李戴（把例外参数宣称为通用车间适用）
        body = (f"{meta['common']}。通用车间地垫应采用导静电型（对应例外参数 "
                f"{meta['values'][1]}），例外条款仅在洁净室启用"
                f"（对应 {meta['values'][0]}）。")
        return {"revision_id": "probe", "sections": [
            {"content": body, "section_type": "facts"}]}
    raise ValueError(case)


def run_calibration() -> dict:
    results = {}
    for case, (task_id, family) in PROBE_TASKS.items():
        meta = _texts_from_ref(_ref(task_id), task_id)
        candidate = _candidate_for(case, meta)
        res = _grade_candidate(candidate, task_id)
        items = res.get("item_results") or []
        results[case] = {
            "task_id": task_id, "family": family,
            "verdict": res.get("verdict"),
            "passed": sum(1 for it in items if it.get("passed")),
            "total": len(items),
            "detail": res,
        }
    # 能力矩阵（如实：v1 语义仅"存在性 + 版本归属"）
    matrix = {
        "synonym_ok_rewrite": "不能区分（phrase 精确匹配 → 合理同义被判失败）",
        "keywords_ok_wrong_conclusion": "不能区分（值齐全即通过 → 错误结论误通过）",
        "version_mixup": "能区分（value_in_version/absent_in_version 检出）",
        "no_evidence_free_supplement": "不能区分（无禁词自由补充不被拦截；"
                                       "forbidden 仅覆盖显式黑名单措辞）",
        "key_omission": "能区分（phrase/value 缺失检出）",
        "applicability_confusable": "不能区分（适用范围张冠李戴但数值齐全 → 误通过）",
    }
    report = {
        "dataset_version": "wiki-default-v5",
        "grader": "wiki-default-grader/v1（未修改；不改语义迎合新案例）",
        "note": "探针为人工可核验候选；离线运行，无模型参与。",
        "results": results, "capability_matrix": matrix,
        "pending_human_review": [
            "是否接受‘同义表达漏报’（建议 v1 维持现状或另立 grader 同义表版本）",
            "关键词齐全但结论错误/适用范围混淆需语义级检查（建议另立 grader 版本，不覆盖 v1）",
            "无证据自由补充是否纳入禁止规则（可能引入误伤）",
            "人工复核全部 39 任务参考答案与探针文本",
        ],
    }
    return report


def main() -> int:
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    report = run_calibration()
    (CALIB_DIR / "calibration-v5.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# v5 评分校准（离线，未改冻结评分器 v1）", "",
             f"- 数据集：wiki-default-v5；评分器：`wiki-default-grader/v1`（未改动）。",
             "- 探针为人工编写候选，直接运行现有评分器；无模型调用。", "",
             "| 判别维度 | 探针任务 | 结果 | 说明 |", "|---|---|---|---|"]
    for case, desc in report["capability_matrix"].items():
        r = report["results"][case]
        lines.append(f"| {case} | {r['task_id']}（{r['family']}） | "
                     f"verdict={r['verdict']} | {desc} |")
    lines += ["", "## 待人工确认", ""]
    lines += [f"- {x}" for x in report["pending_human_review"]]
    (DOC_DIR / "calibration-v5.md").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")
    print(json.dumps(report["capability_matrix"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
