"""阶段 5：实验状态、隔离评估与严格门控。

边界：
- 门控只允许 split=val；test 不参与候选选择/参数/best_score（阶段 7 独立评测）；
- 候选执行走阶段 1 隔离环境（不发布业务、不碰生产库/外网）；
- 主分数 = 通过全部必需检查的任务数 / 验证任务总数（整数比较，避免浮点持平误判）；
  逐项保留诊断，不做未校准加权；聚合协议版本见 SCORE_PROTOCOL_VERSION；
- 基础设施/存储问题 → evaluation invalid（不晋升也不伪装效果拒绝）；
- 数据集/评分器配置错误 → 抛错（无有效分数）；空/重复/非 val 任务清单拒绝；
- 基线/候选共享 runner 配置/profile、数据集版本、grader、pipeline、runtime；
  配置变化拒绝在旧实验续跑（要求新实验或显式重建基线）；
- 门控：candidate > best（整数）接受；== 拒绝（tie）；< 拒绝（regression）；
  best 为实验历史上已接受集合的最佳验证成绩（基线初始化）；
- 接受单事务：rev 校验 → gate 事件 → 更新当前/最佳集合与 best_score → rev+1；
  并发 rev 变化 → 冲突失败；重复 gate 请求幂等（唯一 experiment+candidate_eval）；
- 业务绑定不变；拒绝/invalid 后实验集合不变；候选与经验历史保留。

模拟验证约束：门控纯逻辑单测可用分数 fixture；端到端演示必须真实执行任务并由
grader 出分（同一模拟模型与配置；禁止按 baseline/candidate 标签返回预设分）。
"""
from __future__ import annotations

import hashlib
import json
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.skill_evolution import skill_store
from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import DatasetSpec, canonical_json
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.grader import grade as grade_task
from app.core.skill_evolution.injector import FrozenSkillSet, set_hash_for_members
from app.models.evolution import (
    EXPERIMENT_STATUS_ACTIVE,
    GATE_ACCEPTED,
    GATE_INVALID,
    GATE_REJECTED,
    EvolutionEvaluation,
    EvolutionExperiment,
    EvolutionGateEvent,
    EvolutionProposal,
)

VALIDATION_SPLITS = ("val",)
SCORE_PROTOCOL_VERSION = "passed_over_total_v1"


class GateError(SkillEvolutionError):
    """门控/实验错误。"""


class ExperimentConfigMismatch(GateError):
    pass


class ExperimentStale(GateError):
    pass


class CandidateMismatch(GateError):
    pass


# ---------------------------------------------------------------------------
# 集合 JSON
# ---------------------------------------------------------------------------


def build_set_from_members(db: Session, members: list[dict]) -> FrozenSkillSet:
    rows = [skill_store.get_version(db, m["version_id"]) for m in members]
    skill_set = FrozenSkillSet.from_versions(rows)
    if members:
        recomputed = set_hash_for_members(
            [{k: m[k] for k in ("skill_id", "version_id", "content_hash")}
             for m in members])
        if recomputed != skill_set.set_hash:
            raise GateError("技能集合成员与哈希不一致")
    return skill_set


def apply_proposal_to_members(base_members: list[dict], proposal) -> list[dict]:
    """候选集合：create 新增一个技能 / patch 精确替换父版本；其余成员与顺序固定。"""
    members = [dict(m) for m in base_members]
    if proposal.action == "create":
        if any(m["skill_id"] == proposal.skill_id for m in members):
            raise CandidateMismatch(
                f"create: 基础集合已含 skill {proposal.skill_id}")
        members.append({"skill_id": proposal.skill_id,
                        "version_id": proposal.candidate_version_id,
                        "content_hash": proposal.candidate_content_hash})
    elif proposal.action == "patch":
        idx = next((i for i, m in enumerate(members)
                    if m["skill_id"] == proposal.skill_id
                    and m["version_id"] == proposal.parent_version_id), None)
        if idx is None:
            raise CandidateMismatch(
                f"patch: 父版本 {proposal.parent_version_id} 不在实验基础集合")
        members[idx] = {"skill_id": proposal.skill_id,
                        "version_id": proposal.candidate_version_id,
                        "content_hash": proposal.candidate_content_hash}
    else:
        raise CandidateMismatch(f"不支持 action: {proposal.action}")
    return sorted(members, key=lambda m: (m["skill_id"], str(m.get("seq", 0))))


def score_summary(passed: int, total: int) -> dict:
    return {"passed": int(passed), "total": int(total),
            "protocol": SCORE_PROTOCOL_VERSION}


def effective_score(row) -> dict | None:
    """有效主分数；invalid 评估返回 None（有效分数不可用），原始计数进诊断。"""
    if row is not None and getattr(row, "valid", False):
        return score_summary(row.main_passed, row.main_total)
    return None


def score_diagnostics(row) -> dict:
    """诊断计数（与有效分数分开）：已完成/失败/基础设施等。"""
    try:
        tasks = json.loads(row.per_task_results_json or "[]")
    except (TypeError, ValueError):
        tasks = []
    succeeded = sum(1 for t in tasks if t.get("run_status") == "succeeded")
    infra = sum(1 for t in tasks
                if t.get("outcome_failure_kind") == "infra_or_model")
    return {"completed": succeeded, "failed": len(tasks) - succeeded,
            "infra": infra}


def runner_fingerprint(dataset: DatasetSpec, grader_version: str,
                       runner_config: dict, pipeline_key: str,
                       pipeline_version: str, runtime_ref: str) -> str:
    return hashlib.sha256(canonical_json({
        "dataset": dataset.dataset_version, "grader": grader_version,
        "runner": runner_config, "pipeline": [pipeline_key, pipeline_version],
        "runtime_ref": runtime_ref,
    }).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 实验
# ---------------------------------------------------------------------------


@dataclass
class ExperimentInfo:
    experiment_id: str
    workspace_id: str
    domain: str
    dataset_version: str
    grader_version: str
    status: str
    runner_config: dict
    current_skill_set: dict
    initial_skill_set: dict
    best_score: dict | None
    best_skill_set: dict | None
    baseline_evaluation_id: str | None
    best_evaluation_id: str | None
    status_rev: int

    @classmethod
    def from_row(cls, row: EvolutionExperiment) -> "ExperimentInfo":
        return cls(
            experiment_id=row.experiment_id,
            workspace_id=row.workspace_id,
            domain=row.domain,
            dataset_version=row.dataset_version,
            grader_version=row.grader_version,
            status=row.status,
            runner_config=json.loads(row.runner_config_json),
            current_skill_set=json.loads(row.current_skill_set_json),
            initial_skill_set=json.loads(row.initial_skill_set_json),
            best_score=(score_summary(row.best_score_passed, row.best_score_total)
                        if row.best_score_total is not None else None),
            best_skill_set=(json.loads(row.best_skill_set_json)
                            if row.best_skill_set_json else None),
            baseline_evaluation_id=row.baseline_evaluation_id,
            best_evaluation_id=row.best_evaluation_id,
            status_rev=row.status_rev,
        )


def get_experiment(db: Session, experiment_id: str) -> ExperimentInfo:
    row = db.get(EvolutionExperiment, experiment_id)
    if row is None:
        raise GateError(f"实验不存在: {experiment_id}")
    return ExperimentInfo.from_row(row)


def create_experiment(
    db: Session, *, workspace_id: str, domain: str, dataset: DatasetSpec,
    grader_version: str, runner_config: dict, pipeline_key: str,
    pipeline_version: str, runtime_ref: str, val_task_ids: list[str],
    initial_members: list[dict],
) -> ExperimentInfo:
    """创建最小实验状态（不跑调度、不改业务绑定）。val 任务清单冻结在配置中。"""
    validate_task_list(dataset, val_task_ids)
    # 空成员 = 显式空技能集合（论文对齐“空技能开始”路径）。
    initial_set = build_set_from_members(db, initial_members)
    cfg = dict(runner_config)
    cfg["val_task_ids"] = list(val_task_ids)
    cfg["dataset_version"] = dataset.dataset_version
    cfg["grader_version"] = grader_version
    cfg["pipeline"] = [pipeline_key, pipeline_version]
    experiment_id = "exp_" + _uuid.uuid4().hex[:20]
    row = EvolutionExperiment(
        experiment_id=experiment_id,
        workspace_id=workspace_id,
        domain=domain,
        dataset_version=dataset.dataset_version,
        grader_version=grader_version,
        runner_config_json=json.dumps(cfg, ensure_ascii=False, sort_keys=True),
        pipeline_key=pipeline_key,
        pipeline_version=pipeline_version,
        runtime_ref=runtime_ref,
        initial_skill_set_json=json.dumps(initial_set.to_dict(), ensure_ascii=False),
        current_skill_set_json=json.dumps(initial_set.to_dict(), ensure_ascii=False),
        status_rev=1,
        status=EXPERIMENT_STATUS_ACTIVE,
    )
    db.add(row)
    db.commit()
    return get_experiment(db, experiment_id)


def validate_task_list(dataset: DatasetSpec, task_ids: list[str]) -> list:
    if not task_ids:
        raise GateError("验证任务清单为空（拒绝无效分数）")
    if len(set(task_ids)) != len(task_ids):
        raise GateError("验证任务清单重复")
    tasks = []
    for tid in task_ids:
        task = dataset.task(tid)
        if task.split not in VALIDATION_SPLITS:
            raise GateError(f"test/其它 split 禁止进入门控: {tid}（split={task.split}）")
        if task.dataset_version != dataset.dataset_version:
            raise GateError("任务与数据集版本不一致")
        tasks.append(task)
    return tasks


def _verify_config(db: Session, experiment: ExperimentInfo,
                   dataset: DatasetSpec, profile: str) -> None:
    cfg = experiment.runner_config
    ok = (
        cfg.get("dataset_version") == dataset.dataset_version
        and cfg.get("grader_version") == experiment.grader_version
        and cfg.get("profile") == profile
        and cfg.get("pipeline") == ["wiki.default", "3"]
    )
    if not ok:
        raise ExperimentConfigMismatch(
            "基线/候选配置不一致（数据/评分器/runner/pipeline 需一致）；"
            "请新建实验或显式重建基线")


# ---------------------------------------------------------------------------
# 隔离执行与评分
# ---------------------------------------------------------------------------


def _run_tasks(root: Path, dataset: DatasetSpec, task_ids: list[str],
               skill_set: FrozenSkillSet, profile: str,
               executor_runner=None, *, use_v2: bool = False,
               reviewer=None) -> list[dict]:
    results = []
    review_calls = 0
    for tid in task_ids:
        task = dataset.task(tid)
        res = run_one(root, dataset, task, profile=profile, skills=skill_set,
                      llm_runner_override=executor_runner)
        meta = res["meta"]
        if use_v2:
            from app.core.skill_evolution import review_eval
            grading = review_eval.grade_task_v2(
                dataset_dir=dataset.dataset_dir, task=task,
                candidate=res["candidate"], reviewer=reviewer)
            review_calls += int(grading.get("review_requests") or 0)
            verdict = grading["verdict"]
            item_results = [{"id": (it.get("id") or ""),
                             "passed": it.get("status") == "pass"}
                            for it in grading.get("items") or []]
            item_failures = [it.get("id") for it in grading.get("items") or []
                             if it.get("status") == "fail"]
            extra = {"v2_verdict": grading.get("v2_verdict"),
                     "evaluation_invalid": grading.get("evaluation_invalid"),
                     "unresolved_ids": grading.get("unresolved_ids") or [],
                     "promotable": bool(grading.get("promotable")),
                     "reviewer_identity": grading.get("reviewer_identity")}
        else:
            grading = grade_task(outcome=meta["outcome"],
                                 candidate=res["candidate"],
                                 reference_ref=task.reference_ref,
                                 dataset_dir=dataset.dataset_dir, task_id=tid)
            verdict = grading["verdict"]
            item_results = grading["item_results"]
            item_failures = [i.get("id") for i in item_results
                             if not i.get("passed")]
            extra = {}
        row = {
            "task_id": tid,
            "execution_id": res["execution_id"],
            "run_status": meta["run_status"],
            "published": bool((meta.get("candidate") or {}).get("revision_id")),
            "outcome_failure_kind": meta.get("outcome", {}).get("failure_kind"),
            "verdict": verdict,
            "checks_passed": sum(1 for r in item_results if r.get("passed")),
            "checks_total": len(item_results),
            "item_failures": item_failures,
        }
        row.update(extra)
        results.append(row)
    # 评审请求数随结果回传（评估入口据此计预算/审计）
    setattr(_run_tasks, "last_review_calls", review_calls)
    return results



def _review_mode(exp_info: ExperimentInfo) -> bool:
    return bool((exp_info.runner_config or {}).get("review") == "v2")


def _task_pending(tr: dict) -> bool:
    """needs_review / invalid 不通过、不缩分母；未决评估不可晋升。"""
    return tr.get("verdict") in ("needs_review", "invalid")


def _task_passed(tr: dict) -> bool:
    return bool(tr["run_status"] == "succeeded" and tr["published"]
                and tr["verdict"] == "pass")


def _infra_failure(results: list[dict]) -> str | None:
    for tr in results:
        if tr["run_status"] != "succeeded" and \
                tr.get("outcome_failure_kind") == "infra_or_model":
            return f"基础设施失败 task={tr['task_id']}"
    return None


def _existing_gate_for_proposal(db: Session, experiment_id: str,
                                proposal) -> dict | None:
    """提案候选版本已在门控历史（accepted/rejected/invalid）→ 幂等返回。"""
    events = gate_history(db, experiment_id, limit=200)
    for ev in events:
        if proposal.candidate_version_id in ev["candidate_version_ids"]:
            return {
                "evaluation_id": ev["candidate_evaluation_id"],
                "valid": True,
                "score": ev["candidate_score"],
                "best_score": ev["best_score"],
                "gate": {"decision": ev["decision"], "reason": ev["reason"],
                         "event_id": ev["event_id"], "idempotent_hit": True},
            }
    return None


def _eval_key(experiment_id: str, kind: str, set_hash: str,
              task_ids: list[str], proposal_id: str | None) -> str:
    return hashlib.sha256(canonical_json({
        "experiment": experiment_id, "kind": kind, "set_hash": set_hash,
        "tasks": task_ids, "proposal": proposal_id or "",
    }).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 基线 / 候选评估 / 门控
# ---------------------------------------------------------------------------


def run_baseline(root, dataset: DatasetSpec, experiment_id: str,
                 profile: str = "faithful", executor_runner=None,
                 reviewer=None) -> dict:
    """初始集合跑完整验证集；有效才初始化 best_score。

    v2（review=v2）时任务经 review_eval 评审；needs_review/invalid 任务 →
    基线 invalid（不产生可晋升 best_score），总分母不缩。
    """
    from app.core.skill_evolution import experience_store as _unused  # noqa: F401
    root = Path(root)
    db = skill_store.session_for(root)
    try:
        exp_info = get_experiment(db, experiment_id)
        if exp_info.best_score is not None:
            raise GateError("基线已初始化（重建基线需新实验）")
        _verify_config(db, exp_info, dataset, profile)
        use_v2 = _review_mode(exp_info)
        from app.core.skill_evolution.grader_registry import GRADER_V2
        if use_v2 and dataset.grader_version != GRADER_V2:
            raise ExperimentConfigMismatch(
                "v2 评审要求数据集评分器为 v2；不同 grader_version 的分数不可"
                "直接比较——请新建实验并重建基线")
        if dataset.grader_version == GRADER_V2 and not use_v2:
            raise ExperimentConfigMismatch(
                "v2 数据集必须经 v2 评审入口（review=v2）；禁止以 v1 引擎评分")
        initial_set = build_set_from_members(
            db, exp_info.initial_skill_set.get("members") or [])
        task_ids = list(exp_info.runner_config.get("val_task_ids") or [])
        validate_task_list(dataset, task_ids)

        idem = _eval_key(experiment_id, "baseline", initial_set.set_hash,
                         task_ids, None)
        existing = _find_evaluation(db, idem)
        if existing is not None:
            return {"evaluation_id": existing.evaluation_id,
                    "idempotent_hit": True, "valid": existing.valid,
                    "score": score_summary(existing.main_passed, existing.main_total)}

        results = _run_tasks(root, dataset, task_ids, initial_set, profile,
                            executor_runner=executor_runner, use_v2=use_v2,
                            reviewer=reviewer)
        review_calls = int(getattr(_run_tasks, "last_review_calls", 0))
        passed = sum(1 for r in results if _task_passed(r))
        total = len(results)
        infra = _infra_failure(results)
        pending = [r.get("task_id") for r in results if _task_pending(r)]
        reason = infra
        if reason is None and pending:
            reason = ("v2 未决/无效任务（不通过、不缩分母、不可晋升）: "
                      + ", ".join(pending))
        eval_id = "eval_" + _uuid.uuid4().hex[:20]
        row = EvolutionEvaluation(
            evaluation_id=eval_id, idempotency_key=idem,
            experiment_id=experiment_id, kind="baseline", proposal_id=None,
            base_set_json=json.dumps(initial_set.to_dict(), ensure_ascii=False),
            candidate_set_json=json.dumps(initial_set.to_dict(), ensure_ascii=False),
            task_ids_json=json.dumps(task_ids, ensure_ascii=False),
            per_task_results_json=json.dumps(results, ensure_ascii=False, default=str),
            config_json=json.dumps(exp_info.runner_config, ensure_ascii=False,
                                   sort_keys=True),
            # 计数保留完整分母（不缩）；valid=False → 有效分数=null/unavailable。
            main_passed=(passed if reason is None else 0),
            main_total=(total if reason is None else 0),
            valid=reason is None, invalid_reason=reason,
            usage_json=json.dumps({"executions": total,
                                   "review_requests": review_calls},
                                  ensure_ascii=False))
        db.add(row)
        if reason is None:
            exp_row = db.get(EvolutionExperiment, experiment_id)
            exp_row.baseline_evaluation_id = eval_id
            exp_row.best_evaluation_id = eval_id
            exp_row.best_skill_set_json = json.dumps(initial_set.to_dict(),
                                                     ensure_ascii=False)
            exp_row.best_score_passed = passed
            exp_row.best_score_total = total
            exp_row.status_rev += 1
        db.commit()
        return {"evaluation_id": eval_id, "idempotent_hit": False,
                "valid": reason is None, "invalid_reason": reason,
                "score": score_summary(passed, total),
                "review_model_calls": review_calls}
    finally:
        db.close()


def evaluate_and_gate(root, dataset: DatasetSpec, experiment_id: str,
                      proposal_id: str, profile: str = "faithful",
                      executor_runner=None, reviewer=None) -> dict:
    """候选评估（真实执行 + v1 或 v2 评审）+ 严格门控；返回评估与门控结果。

    v2 时：任务 pass ≠ 候选晋升——候选仍须在全部判定完成后严格超过历史最佳；
    needs_review/invalid 任务 → 评估 invalid（未决评估不能晋升），不缩分母。
    """
    root = Path(root)
    db = skill_store.session_for(root)
    try:
        exp_info = get_experiment(db, experiment_id)
        if exp_info.status != EXPERIMENT_STATUS_ACTIVE:
            raise GateError("实验已关闭")
        if exp_info.best_score is None:
            raise GateError("先运行有效基线（best_score 未初始化）")
        _verify_config(db, exp_info, dataset, profile)
        use_v2 = _review_mode(exp_info)
        from app.core.skill_evolution.grader_registry import GRADER_V2
        if use_v2 and dataset.grader_version != GRADER_V2:
            raise ExperimentConfigMismatch(
                "v2 评审要求数据集评分器为 v2（不同 grader_version 分数不可直接"
                "比较；切换评分器应新建实验并重建基线）")
        if dataset.grader_version == GRADER_V2 and not use_v2:
            raise ExperimentConfigMismatch(
                "v2 数据集必须经 v2 评审入口（review=v2）；禁止以 v1 引擎评分")
        proposal = db.get(EvolutionProposal, proposal_id)
        if proposal is None or proposal.status != "candidate_saved":
            raise GateError(f"提案不是候选状态: {proposal_id}")
        if proposal.domain != exp_info.domain or \
                proposal.dataset_version != dataset.dataset_version:
            raise GateError("提案与实验作用域/数据集不一致")

        # 同一提案/实验的既有门控结果（幂等；先于候选构造，避免父版本已迁移的误判）。
        existing_gate = _existing_gate_for_proposal(db, experiment_id, proposal)
        if existing_gate is not None:
            return existing_gate

        base_members = exp_info.current_skill_set.get("members") or []
        base_set = build_set_from_members(db, base_members)
        candidate_members = apply_proposal_to_members(base_members, proposal)
        candidate_set = build_set_from_members(db, candidate_members)
        task_ids = list(exp_info.runner_config.get("val_task_ids") or [])
        validate_task_list(dataset, task_ids)

        idem = _eval_key(experiment_id, "candidate", candidate_set.set_hash,
                         task_ids, proposal_id)
        existing = _find_evaluation(db, idem)
        if existing is not None:
            return _report_existing_gate(db, experiment_id, existing,
                                         candidate_set, proposal)
        results = _run_tasks(root, dataset, task_ids, candidate_set, profile,
                            executor_runner=executor_runner, use_v2=use_v2,
                            reviewer=reviewer)
        review_calls = int(getattr(_run_tasks, "last_review_calls", 0))
        passed = sum(1 for r in results if _task_passed(r))
        total = len(results)
        infra = _infra_failure(results)
        pending = [r.get("task_id") for r in results if _task_pending(r)]
        reason = infra
        if reason is None and pending:
            reason = ("v2 未决/无效任务（不通过、不缩分母、不可晋升）: "
                      + ", ".join(pending))
        eval_id = "eval_" + _uuid.uuid4().hex[:20]
        db.add(EvolutionEvaluation(
            evaluation_id=eval_id, idempotency_key=idem,
            experiment_id=experiment_id, kind="candidate",
            proposal_id=proposal_id,
            base_set_json=json.dumps(base_set.to_dict(), ensure_ascii=False),
            candidate_set_json=json.dumps(candidate_set.to_dict(), ensure_ascii=False),
            task_ids_json=json.dumps(task_ids, ensure_ascii=False),
            per_task_results_json=json.dumps(results, ensure_ascii=False, default=str),
            config_json=json.dumps(exp_info.runner_config, ensure_ascii=False,
                                   sort_keys=True),
            main_passed=(passed if reason is None else 0),
            main_total=(total if reason is None else 0),
            valid=reason is None, invalid_reason=reason,
            usage_json=json.dumps({"executions": total,
                                   "review_requests": review_calls},
                                  ensure_ascii=False))
        )
        db.commit()
        out = _report_existing_gate(db, experiment_id, db.get(
            EvolutionEvaluation, eval_id), candidate_set, proposal)
        out["review_model_calls"] = review_calls
        return out
    finally:
        db.close()


def _find_evaluation(db: Session, idem_key: str) -> EvolutionEvaluation | None:
    return (db.query(EvolutionEvaluation)
            .filter(EvolutionEvaluation.idempotency_key == idem_key).first())


def _report_existing_gate(db: Session, experiment_id: str, eval_row,
                          candidate_set, proposal) -> dict:
    exp_info = get_experiment(db, experiment_id)
    report = {
        "evaluation_id": eval_row.evaluation_id,
        "kind": eval_row.kind,
        "valid": bool(eval_row.valid),
        "invalid_reason": eval_row.invalid_reason,
        "score": effective_score(eval_row),
        "diagnostics": score_diagnostics(eval_row),
        "best_score": exp_info.best_score,
    }
    if eval_row.valid and eval_row.kind == "candidate":
        report["gate"] = _apply_gate(db, experiment_id, eval_row)
    else:
        report["gate"] = _ensure_invalid_event(
            db, experiment_id, eval_row,
            reason=eval_row.invalid_reason or "evaluation invalid")
    return report


def _ensure_invalid_event(db: Session, experiment_id: str, eval_row,
                          reason: str) -> dict:
    """无效评估也落 invalid 门控事件（不晋升、不改指针；幂等）。"""
    exp_row = db.get(EvolutionExperiment, experiment_id)
    existing = (db.query(EvolutionGateEvent)
                .filter(EvolutionGateEvent.experiment_id == experiment_id,
                        EvolutionGateEvent.candidate_evaluation_id == eval_row.evaluation_id)
                .first())
    if existing is not None:
        return {"decision": existing.decision, "reason": existing.reason,
                "event_id": existing.event_id, "idempotent_hit": True}
    cand_set = json.loads(eval_row.candidate_set_json)
    event_id = "gate_" + _uuid.uuid4().hex[:20]
    db.add(EvolutionGateEvent(
        event_id=event_id, experiment_id=experiment_id,
        candidate_evaluation_id=eval_row.evaluation_id,
        baseline_evaluation_id=exp_row.baseline_evaluation_id,
        best_evaluation_id=exp_row.best_evaluation_id,
        decision=GATE_INVALID, reason=reason,
        candidate_version_ids_json=json.dumps(
            [m["version_id"] for m in (cand_set.get("members") or [])],
            ensure_ascii=False),
        candidate_score=None,  # invalid 不产生有效分数（null/unavailable）
        best_score=(json.dumps(score_summary(exp_row.best_score_passed,
                                             exp_row.best_score_total),
                               ensure_ascii=False)
                    if exp_row.best_score_total is not None else None),
        previous_set_json=exp_row.current_skill_set_json,
        next_set_json=None,
        status_rev_before=exp_row.status_rev,
        status_rev_after=exp_row.status_rev,
    ))
    db.commit()
    return {"decision": GATE_INVALID, "reason": reason, "event_id": event_id,
            "idempotent_hit": False}


def _apply_gate(db: Session, experiment_id: str, eval_row) -> dict:
    exp_row = db.get(EvolutionExperiment, experiment_id)
    expected_rev = exp_row.status_rev
    existing = (db.query(EvolutionGateEvent)
                .filter(EvolutionGateEvent.experiment_id == experiment_id,
                        EvolutionGateEvent.candidate_evaluation_id == eval_row.evaluation_id)
                .first())
    if existing is not None:
        return {"decision": existing.decision, "reason": existing.reason,
                "event_id": existing.event_id, "idempotent_hit": True}
    cand = score_summary(eval_row.main_passed, eval_row.main_total)
    best = (score_summary(exp_row.best_score_passed, exp_row.best_score_total)
            if exp_row.best_score_total is not None else None)
    cand_set = json.loads(eval_row.candidate_set_json)
    if best is None:
        decision, reason, next_set = GATE_REJECTED, "无有效基线（拒绝）", None
    elif cand["passed"] > best["passed"]:
        decision, reason, next_set = GATE_ACCEPTED, "strict: candidate > best", cand_set
    elif cand["passed"] == best["passed"]:
        decision, reason, next_set = GATE_REJECTED, "tie: candidate == best（持平拒绝）", None
    else:
        decision, reason, next_set = GATE_REJECTED, "regression: candidate < best", None
    event_id = "gate_" + _uuid.uuid4().hex[:20]
    event = EvolutionGateEvent(
        event_id=event_id, experiment_id=experiment_id,
        candidate_evaluation_id=eval_row.evaluation_id,
        baseline_evaluation_id=exp_row.baseline_evaluation_id,
        best_evaluation_id=exp_row.best_evaluation_id,
        decision=decision, reason=reason,
        candidate_version_ids_json=json.dumps(
            [m["version_id"] for m in (cand_set.get("members") or [])],
            ensure_ascii=False),
        candidate_score=json.dumps(cand, ensure_ascii=False),
        best_score=json.dumps(best, ensure_ascii=False) if best else None,
        previous_set_json=exp_row.current_skill_set_json,
        next_set_json=(json.dumps(next_set, ensure_ascii=False)
                       if next_set is not None else None),
        status_rev_before=expected_rev,
    )
    db.add(event)
    if decision == GATE_ACCEPTED:
        if db.get(EvolutionExperiment, experiment_id).status_rev != expected_rev:
            db.rollback()
            raise ExperimentStale(
                f"实验状态已变化（expected rev {expected_rev}）；不强行晋升")
        exp_row.current_skill_set_json = json.dumps(cand_set, ensure_ascii=False)
        exp_row.best_skill_set_json = json.dumps(cand_set, ensure_ascii=False)
        exp_row.best_score_passed = cand["passed"]
        exp_row.best_score_total = cand["total"]
        exp_row.best_evaluation_id = eval_row.evaluation_id
        exp_row.status_rev = expected_rev + 1
        event.status_rev_after = expected_rev + 1
    else:
        event.status_rev_after = expected_rev
    db.commit()
    return {"decision": decision, "reason": reason, "event_id": event_id,
            "idempotent_hit": False,
            "score": cand, "best_score": best,
            "next_set": next_set}


# ---------------------------------------------------------------------------
# 查询 / 影响历史 / 导出
# ---------------------------------------------------------------------------


def list_experiments(db: Session, workspace_id: str | None = None) -> list[ExperimentInfo]:
    q = db.query(EvolutionExperiment)
    if workspace_id:
        q = q.filter(EvolutionExperiment.workspace_id == workspace_id)
    return [ExperimentInfo.from_row(r)
            for r in q.order_by(EvolutionExperiment.created_at.desc()).all()]


def get_evaluation(db: Session, evaluation_id: str) -> dict:
    row = db.get(EvolutionEvaluation, evaluation_id)
    if row is None:
        raise GateError(f"评估不存在: {evaluation_id}")
    return {
        "evaluation_id": row.evaluation_id, "experiment_id": row.experiment_id,
        "kind": row.kind, "proposal_id": row.proposal_id,
        "valid": bool(row.valid), "invalid_reason": row.invalid_reason,
        "score": effective_score(row),
        "diagnostics": score_diagnostics(row),
        "task_ids": json.loads(row.task_ids_json),
        "per_task": json.loads(row.per_task_results_json),
    }


def gate_history(db: Session, experiment_id: str,
                 limit: int = 100) -> list[dict]:
    rows = (db.query(EvolutionGateEvent)
            .filter(EvolutionGateEvent.experiment_id == experiment_id)
            .order_by(EvolutionGateEvent.created_at.desc()).limit(limit).all())
    return [
        {
            "event_id": r.event_id,
            "decision": r.decision,
            "reason": r.reason,
            "candidate_evaluation_id": r.candidate_evaluation_id,
            "baseline_evaluation_id": r.baseline_evaluation_id,
            "best_evaluation_id": r.best_evaluation_id,
            "candidate_version_ids": json.loads(r.candidate_version_ids_json),
            "candidate_score": json.loads(r.candidate_score) if r.candidate_score else None,
            "best_score": json.loads(r.best_score) if r.best_score else None,
            "previous_set": json.loads(r.previous_set_json),
            "next_set": json.loads(r.next_set_json) if r.next_set_json else None,
            "status_rev_before": r.status_rev_before,
            "status_rev_after": r.status_rev_after,
        }
        for r in rows
    ]


def skill_impact_summary(db: Session, skill_id: str) -> list[dict]:
    """真实门控历史（验证汇总；不含验证答案/逐任务参考/私有轨迹）。"""
    version_rows = skill_store.list_versions(db, skill_id=skill_id)
    version_ids = {v.version_id for v in version_rows}
    events = (db.query(EvolutionGateEvent)
              .order_by(EvolutionGateEvent.created_at.desc())
              .all())
    out = []
    for e in events:
        ids = set(json.loads(e.candidate_version_ids_json))
        if not ids & version_ids:
            continue
        exp = db.get(EvolutionExperiment, e.experiment_id)
        ev = db.get(EvolutionEvaluation, e.candidate_evaluation_id)
        out.append({
            "event_id": e.event_id,
            "decision": e.decision,
            "reason": e.reason,
            "experiment_id": e.experiment_id,
            "workspace_id": exp.workspace_id if exp else None,
            "dataset_version": exp.dataset_version if exp else None,
            "grader_version": exp.grader_version if exp else None,
            "candidate_score": json.loads(e.candidate_score) if e.candidate_score else None,
            "best_score": json.loads(e.best_score) if e.best_score else None,
            "candidate_version_ids": json.loads(e.candidate_version_ids_json),
            "proposal_id": ev.proposal_id if ev else None,
            "evaluation_valid": bool(ev.valid) if ev else None,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    return out


def export_report_markdown(db: Session, experiment_id: str) -> dict[str, str]:
    """导出评估报告 + skill-impact.md（由权威事件导出，非第二写入源）。"""
    exp = get_experiment(db, experiment_id)
    evals = (db.query(EvolutionEvaluation)
             .filter(EvolutionEvaluation.experiment_id == experiment_id)
             .order_by(EvolutionEvaluation.created_at.desc()).all())
    lines = [
        "# WikiSkill 评估与门控报告", "",
        f"- experiment: `{experiment_id}`",
        f"- workspace: `{exp.workspace_id}` domain: `{exp.domain}`",
        f"- dataset_version: {exp.dataset_version}",
        f"- grader_version: {exp.grader_version}",
        f"- score protocol: `{SCORE_PROTOCOL_VERSION}`",
        f"- best_score: {exp.best_score}",
        f"- baseline_evaluation: {exp.baseline_evaluation_id}",
        f"- status_rev: {exp.status_rev}",
        "", "## 评估记录", "",
        "| evaluation | kind | valid | score | proposal |",
        "|---|---|---|---|---|",
    ]
    for e in evals:
        score_text = (f"{e.main_passed}/{e.main_total}"
                      if e.valid else "（invalid，无有效分数）")
        lines.append(
            f"| {e.evaluation_id} | {e.kind} | {bool(e.valid)} | "
            f"{score_text} | {e.proposal_id or '-'} |")
    lines.append("")
    lines.append("## 门控历史")
    for g in gate_history(db, experiment_id):
        lines.append(f"- **{g['decision']}** `{g['event_id']}`：{g['reason']} "
                     f"(rev {g['status_rev_before']}→{g['status_rev_after']})")
    impact = _skill_impact_md(db, experiment_id)
    return {
        "report.md": "\n".join(lines) + "\n",
        "skill-impact.md": impact,
    }


def _skill_impact_md(db: Session, experiment_id: str) -> str:
    exp = get_experiment(db, experiment_id)
    lines = [
        "# skill-impact（由门控事件导出的只读快照）", "",
        f"- experiment: `{experiment_id}`  workspace: `{exp.workspace_id}`",
        "", "## 事件", "",
    ]
    for g in gate_history(db, experiment_id):
        lines.append(
            f"### {g['decision']} {g['event_id']}\n"
            f"- reason: {g['reason']}\n"
            f"- candidate_evaluation: {g['candidate_evaluation_id']}\n"
            f"- candidate_score: {g['candidate_score']}  "
            f"best_score: {g['best_score']}\n"
            f"- candidate versions: {g['candidate_version_ids']}\n"
            f"- status_rev: {g['status_rev_before']} → {g['status_rev_after']}\n")
    lines.append("> 不含验证答案/逐任务参考值/私有轨迹；格式非法、评估无效与效果被拒绝 "
                  "分别以 decision=output_invalid/invalid/rejected 记录。")
    return "\n".join(lines) + "\n"
