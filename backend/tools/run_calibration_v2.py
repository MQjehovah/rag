"""v2 校准运行器（升级）：真实 v1 引擎对照 + 统一指标（正确/误通过/误拒绝/待审率/故障率）。

- v1 列：开发集可适配时调用真实 wiki-default-grader/v1 引擎；参考由 dev spec 生成
  于 v1-probe-references（代理参考），并显式标注限制，不冒充实测官方参考答案。
- v2 列：保守原型（needs_review 不计通过）。
- 指标展示同时给出 正确判定/误通过/误拒绝/待审率/故障率；不用“零错误”宣称质量达标。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DEV = BASE / "eval/wiki_evolution/calibration/v2-dev.json"
OUT = BASE / "eval/wiki_evolution/calibration/v2-dev-results.json"
PROBE_DIR = BASE / "eval/wiki_evolution/calibration/v1-probe"
PROBE_REF = "wiki-default-v1-probe.json"

_UNITS = {"mm": "mm", "min": "min", "s": "s", "°C": "°C", "Ω": "Ω",
          "V": "V", "µS/cm": "µS/cm"}


def _num_str(v) -> str:
    if isinstance(v, (int, float)) and float(v) == int(v):
        return str(int(v))
    return str(v)


def build_v1_probe_references() -> Path:
    """从 dev spec 适配 v1 参考（代理；非官方参考答案）。确定性生成、可复跑。"""
    samples = json.loads(DEV.read_text(encoding="utf-8"))["samples"]
    refs = {}
    for s in samples:
        spec = s["spec"]
        points = []
        vers = sorted({c.get("version") for c in spec.get("claims", [])
                       if c.get("version")})
        for c in spec.get("claims", []):
            val = (f"{_num_str(c['object'])} {_UNITS.get(c.get('unit') or '', '')}"
                   if c.get("unit") else str(c["object"]))
            ver = c.get("version")
            if ver:
                points.append({"id": f"v_in_{c['cid']}", "kind": "value_in_version",
                               "value": val, "version": ver})
                for other in vers:
                    if other != ver:
                        points.append({"id": f"a_in_{c['cid']}_{other}",
                                       "kind": "absent_in_version",
                                       "value": val, "version": other})
            else:
                points.append({"id": f"p_{c['cid']}", "kind": "value", "value": val})
        for t in spec.get("coverage", []):
            points.append({"id": f"cov_{t['topic']}", "kind": "phrase",
                           "text": t["topic"]})
        refs[s["id"]] = {"expected_points": points, "forbidden": []}
    ref_dir = PROBE_DIR / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / PROBE_REF).write_text(
        json.dumps(refs, ensure_ascii=False, indent=2), encoding="utf-8")
    return ref_dir


def v1_real(sample: dict) -> dict:
    """调用真实 v1 引擎（grader.grade）；参考为上述代理文件。"""
    from app.core.skill_evolution.grader import grade
    outcome = {"run_ok": True, "published": True, "failure_kind": None}
    res = grade(outcome=outcome, candidate=sample["candidate"],
                reference_ref=PROBE_REF, dataset_dir=PROBE_DIR,
                task_id=sample["id"])
    return {"verdict": res["verdict"],
            "engine": "real:wiki-default-grader/v1",
            "reference_proxy": True,
            "limitation": ("v1 参考由 dev spec 适配生成（canonical 数值/单位、版本归属"
                           "点、主题短语）——代理参考，不是官方 v1 参考答案")}


def v2_outcome(sample: dict) -> dict:
    from app.core.skill_evolution.grader_v2 import evaluate, spec_from_json
    return evaluate(sample["candidate"], spec_from_json(sample["spec"]))


def classify_quality(gt: str, verdict: str, engine: str) -> str:
    if engine == "v1":
        if gt == "good":
            return "v1正确通过" if verdict == "pass" else "v1误拒绝"
        return "v1误通过（缺陷复现）" if verdict == "pass" else "v1正确拒绝"
    if verdict == "needs_review":
        return "v2待审（保守，非误通过）"
    if gt == "good":
        return "v2正确通过" if verdict == "pass" else "v2误拒绝"
    return "v2误通过" if verdict == "pass" else "v2正确拒绝"


def run() -> dict:
    build_v1_probe_references()
    samples = json.loads(DEV.read_text(encoding="utf-8"))["samples"]
    rows: dict = {}
    tally: dict[str, int] = {}
    v1_fail = v2_review = v2_invalid = 0
    for s in samples:
        v1 = v1_real(s)
        v2 = v2_outcome(s)
        c1 = classify_quality(s["ground_truth"], v1["verdict"], "v1")
        c2 = classify_quality(s["ground_truth"], v2["verdict"], "v2")
        for c in (c1, c2):
            tally[c] = tally.get(c, 0) + 1
        if v2["verdict"] == "needs_review":
            v2_review += 1
        if v2["verdict"] == "invalid":
            v2_invalid += 1
        rows[s["id"]] = {
            "category": s["category"],
            "expected_quality_label": s["ground_truth"],
            "v1_engine": v1["engine"], "v1_reference_proxy": v1["reference_proxy"],
            "v1_limitation": v1["limitation"],
            "v1_verdict": v1["verdict"], "v1_class": c1,
            "v2_verdict": v2["verdict"], "v2_class": c2,
            "v2_promotable": v2["promotable"],
            "v2_unresolved": v2["unresolved"],
            "rationale": s["rationale"],
        }
    n = len(samples)
    correct = sum(tally.get(k, 0) for k in
                  ("v1正确通过", "v1正确拒绝", "v2正确通过", "v2正确拒绝"))
    report = {
        "grader_v2_version": "wiki-default-grader/v2-prototype-2",
        "samples_total": n,
        "tally": tally,
        "metrics": {
            "v1_engine": "real:wiki-default-grader/v1（参考=代理参考，见行内 limitation）",
            "v2_correct_decisions": tally.get("v2正确通过", 0)
                                    + tally.get("v2正确拒绝", 0),
            "v2_mispass": tally.get("v2误通过", 0),
            "v2_misreject": tally.get("v2误拒绝", 0),
            "needs_review_tasks": v2_review,
            "needs_review_rate": round(v2_review / n, 4),
            "invalid_tasks": v2_invalid,
            "failure_rate": round(v2_invalid / n, 4),
        },
        "no_quality_claim": ("待审不等于通过；待审率高不代表质量达标，也不代表零错误——"
                             "指标必须与人工/模型判定补齐后再解释"),
        "rows": rows,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return report


def main() -> int:
    report = run()
    print(json.dumps(report["tally"], ensure_ascii=False, indent=2))
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
