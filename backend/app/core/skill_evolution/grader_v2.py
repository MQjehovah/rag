"""Grader v2 —— 保守确定性原型（v2-prototype-2；纠偏版）。

定位（务必先读 docs/wikiskill/08-grader-v2-design.md）：
- 不是默认评分器、不接入正式门控；v1（wiki-default-grader/v1）与 v1–v5 数据全部不动。
- 汇总契约：
    * 检查项状态 = pass | fail | needs_review；评审/数据故障另行 invalid。
    * 任一必需检查明确 fail → 任务 fail；
    * 全部必需检查 pass（且无 needs_review / invalid）→ 任务 pass；
    * 无 fail 但存在未判定必需检查 → needs_review。
  needs_review 不当作 pass；存在未解决的必需判定时**不得生成可晋升主分数**、
  不得跳过这些任务缩小分母。promotable 仅当 verdict == pass。
- 能力边界（收紧表述）：
    * 数值/单位归一化只证明“格式或数值等价/存在”，不证明主体、条件、来源归属正确；
    * 同一参数在不同条件下的不同取值不自动构成矛盾（仅当声明 same_condition 时才判）；
    * 覆盖短语只能证明“被提及”，不能单独证明内容覆盖正确；
    * 语义能力不足 → 如实 needs_review；禁止用关键词存在代替语义通过。
- 实现全部基于通用检查类型（claim_value / coverage / 声明式矛盾），
  不针对任何样例 ID 特判。

未来若引入“结构化结论声明”（见设计文档 §4），契约必须对所有实验组一致，且
不得只信模型自报声明、忽略正文矛盾或遗漏——本原型仍以正文检查为基准。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

GRADER_V2_VERSION = "wiki-default-grader/v2-prototype-2"

# 判别级别
LVL_A = "A_format_equivalence"       # 程序可靠：仅格式/数值等价、区段归属、声明式矛盾
LVL_B = "B_partial"                  # 程序部分可查：仅提示，不单独通过
LVL_C = "C_human_or_model"           # 需人工/模型：一律 needs_review

ST_PASS = "pass"
ST_FAIL = "fail"
ST_REVIEW = "needs_review"
ST_INVALID = "invalid"

UNIT_ALIAS = {"mm": ["mm", "毫米"], "min": ["min", "分钟"], "s": ["s", "秒"],
              "°C": ["°C", "℃", "摄氏度"], "Ω": ["Ω", "欧姆"],
              "V": ["V", "伏"], "µS/cm": ["µS/cm", "uS/cm", "μS/cm"]}


def _numbers_with_unit(text: str) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for unit, aliases in UNIT_ALIAS.items():
        pat = r"(-?\d+(?:\.\d+)?)\s*(" + "|".join(
            re.escape(a) for a in aliases) + r")"
        for m in re.finditer(pat, text):
            try:
                out.append((float(m.group(1)), unit))
            except ValueError:
                continue
    return out


@dataclass(frozen=True)
class ClaimSpec:
    cid: str
    subject: str
    predicate: str
    object_value: float | str
    unit: str = ""
    version: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class CoverageItem:
    topic: str


@dataclass
class DeclaredConflict:
    """声明式矛盾：仅当调用方确认两个取值属于同一条件同一参数时才自动判矛盾。"""
    cid: str
    same_condition: bool = False


@dataclass
class V2Spec:
    task_id: str
    claims: list[ClaimSpec] = field(default_factory=list)
    coverage: list[CoverageItem] = field(default_factory=list)
    conflicts: list[DeclaredConflict] = field(default_factory=list)


def _section_texts(candidate: dict) -> dict[str, str]:
    texts: dict[str, str] = {"unversioned": ""}
    for sec in candidate.get("sections") or []:
        label = sec.get("version_label") or "unversioned"
        texts[label] = texts.get(label, "") + "\n" + (sec.get("content") or "")
    return texts


def _claim_value_check(claim: ClaimSpec, text: str, all_text: str) -> dict:
    """A 级：区段内数值/目标文本存在性（只证明格式/数值等价，不证明归属语义）。"""
    boundary = ("未校验：主体/条件/适用范围归属与结论关系是否真的正确"
                "（需 C 级人工或模型）")
    if claim.unit:
        targets = [v for v, u in _numbers_with_unit(text) if u == claim.unit]
        ok = any(abs(v - float(claim.object_value)) < 1e-6 for v in targets)
        found = sorted({v for v in targets})
        return {"status": ST_PASS if ok else ST_FAIL,
                "check": f"数值等价（{claim.unit} 归一化）",
                "detail": (f"区段内发现 {found}；期望 {claim.object_value}"
                           if not ok else f"发现 {claim.object_value}{claim.unit}"),
                "boundary": boundary}
    target = str(claim.object_value)
    ok = target in text
    if ok:
        return {"status": ST_PASS, "check": "目标文本存在性",
                "detail": f"找到 {target!r}", "boundary": boundary}
    # 缺失也可能是同义表达 → 语义待审，不自动判 fail（不用关键词存在代答语义）
    return {"status": ST_REVIEW, "check": "目标文本存在性（缺失待核）",
            "detail": f"未找到 {target!r}；可能是同义改写或真遗漏，需人工/模型核验",
            "boundary": boundary}


def _auto_review_items(spec: V2Spec) -> list[dict]:
    """为每条必需检查附 C 级语义维度（能力不足 → 如实待审，不代答）。"""
    items = []
    for c in spec.claims:
        items.append({"id": f"semantic:{c.cid}",
                      "status": ST_REVIEW,
                      "level": LVL_C,
                      "check": "主体/关系/条件/适用范围归属是否与来源一致",
                      "detail": f"需人工或独立模型对照来源核验 {c.subject}"
                                f" {c.predicate} {c.object_value}"
                                f"{c.unit}（版本={c.version or '未标注'}）",
                      "boundary": "程序仅核数值/文本等价，不能代判归属语义"})
    for t in spec.coverage:
        items.append({"id": f"semantic:coverage:{t.topic}",
                      "status": ST_REVIEW,
                      "level": LVL_C,
                      "check": "覆盖主题的实质内容（非仅提及）",
                      "detail": f"主题 {t.topic} 是否被忠实覆盖需人工/模型核验",
                      "boundary": "提及短语 ≠ 内容覆盖正确"})
    if spec.claims:
        items.append({"id": "semantic:unsupported_new_fact",
                      "status": ST_REVIEW,
                      "level": LVL_C,
                      "check": "候选是否含来源未记载的新增事实性断言",
                      "detail": "无依据新增事实的识别需人工/模型（不用词表代替）",
                      "boundary": "原型不识别任意新增事实"})
    return items


def evaluate(candidate: dict, spec: V2Spec,
             resolutions: dict[str, str] | None = None) -> dict:
    """任务级评估；返回 verdict / promotable / 各检查状态（含 invalid 记录）。

    resolutions：{item_id: pass|fail}，由人工或（未来）独立模型对 C 级语义项给出
    判定后传入；原型本身不默认通过任何语义项。实现与样例 ID 无关。
    """
    resolutions = resolutions or {}
    if not isinstance(candidate, dict) or not candidate.get("sections"):
        return _summary(spec, [], invalid="候选缺少 sections（数据故障）")
    sections = _section_texts(candidate)
    all_text = "\n".join(sections.values())
    items: list[dict] = []

    # A/B 级确定性项（通用；无样例特判）
    for claim in spec.claims:
        text = sections.get(claim.version or "unversioned", "")
        if not text.strip():
            text = all_text if claim.version is None else ""
        if claim.version and not text.strip():
            items.append({"id": f"claim:{claim.cid}", "level": LVL_A,
                          "status": ST_FAIL,
                          "check": "版本区段归属",
                          "detail": f"缺少版本区段 {claim.version}（无法归属核验）",
                          "boundary": "区段缺失即不可核，按明确缺失处理"})
            continue
        r = _claim_value_check(claim, text, all_text)
        items.append({"id": f"claim:{claim.cid}", "level": LVL_A,
                      "status": r["status"], "check": r["check"],
                      "detail": r["detail"], "boundary": r["boundary"]})
    for t in spec.coverage:
        present = t.topic in all_text
        if present:
            items.append({"id": f"coverage:{t.topic}", "level": LVL_A,
                          "status": ST_PASS,
                          "check": "覆盖主题提及级检查",
                          "detail": f"主题 {t.topic} 已提及",
                          "boundary": "仅‘被提及’，实质覆盖正确性见语义项"})
        else:
            items.append({"id": f"coverage:{t.topic}", "level": LVL_A,
                          "status": ST_REVIEW,
                          "check": "覆盖主题提及级检查（缺失待核）",
                          "detail": f"未提及主题 {t.topic}；可能是同义改写或真遗漏，"
                                    "需人工/模型确认",
                          "boundary": "不能以缺失即判 fail（同义可能），也不当 pass"})
    for cf in spec.conflicts:
        claim = next((c for c in spec.claims if c.cid == cf.cid), None)
        if claim is None or not claim.unit:
            continue
        vals = sorted({v for v, _ in _numbers_with_unit(all_text)})
        if cf.same_condition and len(vals) > 1:
            items.append({"id": f"conflict:{cf.cid}", "level": LVL_A,
                          "status": ST_FAIL,
                          "check": "声明式同条件多值矛盾",
                          "detail": f"同一条件出现多值: {vals}",
                          "boundary": "仅当调用方声明 same_condition=true 才判矛盾"})
        elif len(vals) > 1:
            items.append({"id": f"conflict:{cf.cid}", "level": LVL_A,
                          "status": ST_REVIEW,
                          "check": "多值是否矛盾（条件未声明相同）",
                          "detail": f"出现多值 {vals}；不同条件下同参数可合法不同值",
                          "boundary": "未声明同条件 → 不自动判矛盾"})
    # C 级语义维度：如实待审；若有外部 resolution 则应用（仍由通用 id 映射）
    items.extend(_auto_review_items(spec))
    for it in items:
        resolved = resolutions.get(it["id"])
        if resolved in (ST_PASS, ST_FAIL):
            it["status"] = resolved
            it["resolved_by"] = "human_or_model"
    return _summary(spec, items)


def _summary(spec: V2Spec, items: list[dict], invalid: str | None = None) -> dict:
    if invalid:
        return {
            "grader_version": GRADER_V2_VERSION,
            "task_id": spec.task_id,
            "verdict": ST_INVALID,
            "evaluation_invalid": invalid,
            "promotable": False,
            "required_total": len(spec.claims) + len(spec.coverage),
            "decided": 0, "unresolved": 0, "items": [],
        }
    required_ids = [it for it in items
                    if it["id"].startswith(("claim:", "coverage:"))]
    # 任一必需检查（含经人工/模型解析的 C 级项）明确 fail → 任务 fail
    any_fail = any(it["status"] == ST_FAIL for it in items)
    unresolved = [it for it in items if it["status"] == ST_REVIEW]
    if any_fail:
        verdict = ST_FAIL
    elif unresolved:
        verdict = ST_REVIEW
    else:
        verdict = ST_PASS
    decided = [it for it in items if it["status"] in (ST_PASS, ST_FAIL)]
    return {
        "grader_version": GRADER_V2_VERSION,
        "task_id": spec.task_id,
        "verdict": verdict,
        "evaluation_invalid": None,
        "promotable": verdict == ST_PASS,
        "required_total": len(required_ids),
        "decided_pass": sum(1 for it in decided if it["status"] == ST_PASS),
        "decided_fail": sum(1 for it in decided if it["status"] == ST_FAIL),
        "unresolved": len(unresolved),
        "unresolved_ids": [it["id"] for it in unresolved],
        "items": items,
        "metric_note": ("主指标=全部必需检查通过/总任务；needs_review 或 invalid 任务"
                        "不产生可晋升主分数，也不得从分母剔除。"),
    }


def spec_from_json(doc: dict) -> V2Spec:
    claims = [ClaimSpec(cid=c["cid"], subject=c["subject"],
                        predicate=c["predicate"],
                        object_value=(c["object"] if isinstance(c["object"], str)
                                      else c["object"]),
                        unit=c.get("unit") or "", version=c.get("version"),
                        scope=c.get("scope"))
              for c in doc.get("claims", [])]
    coverage = [CoverageItem(topic=t["topic"]) for t in doc.get("coverage", [])]
    conflicts = [DeclaredConflict(cid=x["cid"],
                                  same_condition=bool(x.get("same_condition", False)))
                 for x in doc.get("conflicts", [])]
    return V2Spec(task_id=doc["task_id"], claims=claims, coverage=coverage,
                  conflicts=conflicts)


def load_dev_set(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["samples"]
