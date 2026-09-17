"""A1–A5 人工标签入档与核验（Grader v2；离线，不访问网络）。

用途：
- 由“项目所有者在当前 Codex 会话中明确确认”的人工判定（reviewer 使用非秘密稳定
  角色标识 human-project-owner，2026-09-08）写入追加式审阅记录；
- 这是 human approved（人工标签批准），不是模型建议、不修改评分器实现、不代表真实
  校准、不解除晋升限制。
- 默认只处理固定 A1–A5；决策表内置于代码（不从命令行注入 reviewer/verdict）。

用法：
  python approve_grader_v2_labels.py --check     # 只读核验（不写任何文件）
  python approve_grader_v2_labels.py --apply     # 追加 15 条记录并更新验收元数据
重复 --apply 幂等（同绑定同 verdict 不新增行）；同 check 冲突 verdict fail-closed
（review_store.submit_review 抛 ValueError）。

记录（每条）：
  task_id=样例 id；check_id ∈ {c1,c2,overall}；verdict=pass|fail；reason；
  method=human；reviewer=human-project-owner；
  review_version=human@human-project-owner|2026-09-08（review_store 契约）；
  source/candidate/contract 绑定哈希（bind_hashes：source_excerpt/candidate_text/
  checks+disputes+category+ground_truth）；source_evidence_loc=source_excerpt；
  candidate_loc=candidate_text。
判定表（2026-09-08 人工确认）：
  A1 c1=pass c2=pass overall=pass
  A2 c1=pass c2=fail overall=fail
  A3 c1=fail c2=fail overall=fail
  A4 c1=fail c2=fail overall=fail
  A5 c1=fail c2=fail overall=fail
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
ACC = BACKEND / "eval/wiki_evolution/calibration/v2-acceptance-review.json"
STORE = BACKEND / "eval/wiki_evolution/calibration/v2-acceptance-reviews.jsonl"

REVIEWER = "human-project-owner"
REVIEW_VERSION_DATE = "2026-09-08"
TASK_IDS = ("A1", "A2", "A3", "A4", "A5")

# 人工确认判定（verdict, reason）。reason 文本与 2026-09-08 会话确认一致/由确认理由
# 分解到两个检查项，不新增任何事实。
DECISIONS = {
    "A1": {
        "c1": ("pass", "激光对中方法正确。"),
        "c2": ("pass", "径向和轴向偏差均明确为 0.05 mm，与来源等价。"),
        "overall": ("pass", "激光对中方法正确；径向和轴向偏差均明确为 0.05 mm，"
                           "与来源等价。"),
    },
    "A2": {
        "c1": ("pass", "步骤动作和顺序存在。"),
        "c2": ("fail", "“不统计 https 计数”否定了观察计数增长的验证要求。"),
        "overall": ("fail", "步骤动作和顺序存在，但“不统计 https 计数”否定了"
                            "观察计数增长的验证要求。"),
    },
    "A3": {
        "c1": ("fail", "mV 与 V 量级不同（单位归一化：mV≠V）。"),
        "c2": ("fail", "候选输入电压范围结论错误。"),
        "overall": ("fail", "mV 与 V 量级不同，候选输入电压范围结论错误。"),
    },
    "A4": {
        "c1": ("fail", "候选没有忠实给出两组完整范围。"),
        "c2": ("fail", "将通用车间与百级洁净室的地垫类型互换。"),
        "overall": ("fail", "候选没有忠实给出两组完整范围，并将通用车间与百级"
                            "洁净室的地垫类型互换。"),
    },
    "A5": {
        "c1": ("fail", "候选未提供可供对照的 3600 s→7200 s 版本差异说明。"),
        "c2": ("fail", "括号中“v1、3600 s”系声明这些对照没有提供，不构成版本"
                       "差异说明。"),
        "overall": ("fail", "括号中虽然出现“v1、3600 s”，但语义是在声明这些对照"
                            "没有提供，不能构成 3600 s→7200 s 的版本差异说明。"),
    },
}


def _sha256(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8")).hexdigest()


def load_acceptance() -> dict:
    if not ACC.is_file():
        raise SystemExit(f"验收样例集不存在: {ACC}")
    return json.loads(ACC.read_text(encoding="utf-8"))


def sample_contract(sample: dict) -> dict:
    return {"checks_text": sample["checks"], "disputes": sample.get("disputes"),
            "category": sample["category"],
            "ground_truth": sample.get("ground_truth")}


def records_plan() -> list[dict]:
    """(task_id, check_id, verdict, reason, source, candidate, contract) 计划行。"""
    acc = load_acceptance()
    by_id = {s["id"]: s for s in acc["samples"]}
    missing = [t for t in TASK_IDS if t not in by_id]
    if missing:
        raise SystemExit(f"验收样例缺失: {missing}")
    plan = []
    for task_id in TASK_IDS:
        s = by_id[task_id]
        checks = s.get("checks") or []
        if not isinstance(checks, list) or len(checks) < 2:
            raise SystemExit(f"样例 {task_id} 的 checks 字段异常（应含两个检查项）")
        for cid in ("c1", "c2", "overall"):
            verdict, reason = DECISIONS[task_id][cid]
            plan.append({"task_id": task_id, "check_id": cid,
                         "verdict": verdict, "reason": reason,
                         "source": s["source_excerpt"],
                         "candidate": s["candidate_text"],
                         "contract": sample_contract(s)})
    return plan


def _record_digest(rec: dict) -> dict:
    """去掉运行时字段（review_id/appended）后用于确定性摘要与比较。"""
    return {k: v for k, v in rec.items()
            if k not in ("review_id", "appended", "duplicate_of_existing")}


def approval_hash(reviews: list[dict]) -> str:
    rows = [_record_digest(r) for r in reviews
            if r.get("task_id") in TASK_IDS
            and r.get("reviewer") == REVIEWER
            and str(r.get("review_version", "")).startswith("human@")]
    order = {t: i for i, t in enumerate(TASK_IDS)}
    cid_order = {"c1": 0, "c2": 1, "overall": 2}
    rows.sort(key=lambda r: (order.get(r.get("task_id"), 99),
                             cid_order.get(r.get("check_id"), 99)))
    return _sha256(rows)


def expected_verdicts(plan) -> dict:
    return {(p["task_id"], p["check_id"]): p["verdict"] for p in plan}


def run_check(*, verbose: bool = True) -> int:
    from app.core.skill_evolution.review_store import (
        bind_hashes, load_reviews, usable_reviews)
    acc = load_acceptance()
    plan = records_plan()
    issues = []
    # 验收样例数/身份
    ids = [s["id"] for s in acc["samples"]]
    if ids != list(TASK_IDS):
        issues.append(f"样例集合异常: {ids}")
    if acc.get("status") != "human_labels_approved":
        issues.append(f"status={acc.get('status')!r} 应为 human_labels_approved")
    if acc.get("used_for_development") is not False:
        issues.append("used_for_development 必须为 false")
    if acc.get("reserved_for_future_independent_acceptance") is not False:
        issues.append("reserved_for_future_independent_acceptance 必须为 false")
    if acc.get("independent_acceptance_consumed") is not True:
        issues.append("independent_acceptance_consumed 必须为 true")
    if acc.get("reviewer") != REVIEWER:
        issues.append(f"reviewer={acc.get('reviewer')!r}")
    if acc.get("human_reviewed_at") != REVIEW_VERSION_DATE:
        issues.append(f"human_reviewed_at={acc.get('human_reviewed_at')!r}")
    reviews = load_reviews(STORE)
    per_task = {}
    for p in plan:
        binds = bind_hashes(task_id=p["task_id"], sources=[p["source"]],
                            candidate_output={"candidate_text": p["candidate"]},
                            contract=p["contract"])
        usable = [r for r in usable_reviews(
            reviews, task_id=p["task_id"], sources=[p["source"]],
            candidate_output={"candidate_text": p["candidate"]},
            contract=p["contract"])
            if r.get("check_id") == p["check_id"]]
        per_task.setdefault(p["task_id"], {})
        per_task[p["task_id"]][p["check_id"]] = usable
        if len(usable) != 1:
            issues.append(f"{p['task_id']}/{p['check_id']}: usable={len(usable)}")
            continue
        rec = usable[0]
        if rec.get("verdict") != p["verdict"]:
            issues.append(f"{p['task_id']}/{p['check_id']}: verdict 不一致")
        if rec.get("reviewer") != REVIEWER or rec.get("method") != "human":
            issues.append(f"{p['task_id']}/{p['check_id']}: 标识异常")
        if not str(rec.get("review_version", "")).startswith("human@"):
            issues.append(f"{p['task_id']}/{p['check_id']}: version 异常")
    want = expected_verdicts(plan)
    # overall 判定与两检查项一致性由 decision 表本身保证（由 want 校验）
    record_count = sum(len(v) for v in per_task.values())
    if record_count != 15:
        issues.append(f"记录数={record_count} 应为 15")
    if any(r.get("verdict") == "needs_review"
           for sub in per_task.values()
           for rec_list in sub.values() for r in rec_list):
        issues.append("存在 needs_review（不允许）")
    digest = approval_hash(reviews)
    if acc.get("approval_record_hash") != digest:
        issues.append("approval_record_hash 与当前记录不一致")
    if verbose:
        print("A1–A5 人工标签离线核验")
        for task_id in TASK_IDS:
            line = " ".join(
                f"{cid}={per_task[task_id][cid][0]['verdict']}"
                if per_task.get(task_id, {}).get(cid) else f"{cid}=缺失"
                for cid in ("c1", "c2", "overall"))
            print(f"  {task_id}: {line}")
        print(f"  记录数={record_count} approval_hash={digest[:16]}…")
        print("  汇总：A1=pass，A2–A5=fail（人工标签，非真实校准）")
        for issue in issues:
            print("  ISSUE:", issue)
    return 1 if issues else 0


def run_apply() -> int:
    from app.core.skill_evolution.review_store import (
        bind_hashes, load_reviews, submit_review)
    acc = load_acceptance()
    plan = records_plan()
    appended = duplicated = 0
    for p in plan:
        try:
            rec = submit_review(
                store_path=STORE, task_id=p["task_id"], check_id=p["check_id"],
                verdict=p["verdict"], reason=p["reason"],
                source_evidence_loc="source_excerpt",
                candidate_loc="candidate_text",
                method="human", reviewer=REVIEWER,
                review_version=REVIEW_VERSION_DATE,
                sources=[p["source"]],
                candidate_output={"candidate_text": p["candidate"]},
                contract=p["contract"])
        except ValueError as exc:  # 冲突 verdict fail-closed
            print(f"冲突拒绝：{p['task_id']}/{p['check_id']} -> {exc}")
            return 2
        if rec.get("duplicate_of_existing"):
            duplicated += 1
        else:
            appended += 1
    reviews = load_reviews(STORE)
    digest = approval_hash(reviews)
    acc["status"] = "human_labels_approved"
    acc["human_reviewed_at"] = REVIEW_VERSION_DATE
    acc["reviewer"] = REVIEWER
    acc["review_store"] = "eval/wiki_evolution/calibration/v2-acceptance-reviews.jsonl"
    acc["approval_record_hash"] = digest
    acc["reserved_for_future_independent_acceptance"] = False
    acc["independent_acceptance_consumed"] = True
    acc["used_for_development"] = False
    ACC.write_text(json.dumps(acc, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"apply 完成：新增={appended} 幂等跳过={duplicated} "
          f"approval_hash={digest[:16]}…")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只读核验")
    parser.add_argument("--apply", action="store_true",
                        help="追加人工记录并更新验收元数据")
    args = parser.parse_args()
    if args.apply:
        return run_apply()
    if args.check or (not args.check and not args.apply):
        return run_check()
    return 1


if __name__ == "__main__":
    sys.exit(main())
