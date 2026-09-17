"""Grader v2 评估入口（review_eval）——接线 grader_v2 确定性引擎 + 语义评审器。

原则：
- 任务级：确定性项（A/B 级）由 grader_v2.evaluate 程序判定；C 级/未决语义项
  一律 needs_review，交给语义评审器（独立模型或 stub）给 resolution；
- 评审器只接收 资料片段 + 待评输出 + 检查项（不可信数据），不接收参考答案全文、
  候选身份、实验组与分数 → 参考答案与私有评审反馈绝不进入执行者/维护者/提议者
  上下文（评审输入/输出仅在本模块与评审记录中流动）；
- 评审失败/非法结构/证据引用错误 → 任务 invalid（不回退 v1，不生成 pass）；
- 任务 verdict=pass/fail/needs_review/invalid；needs_review 与 invalid 不通过、
  不缩分母（调用方仍保留 full total），未决评估不可晋升；
- v2 任务的 V2Spec 必须存在（任务 reference 文档内 "v2_spec" 块）；缺失 → 明确
  ConfigError（不静默退回 v1 expected_points 逻辑）。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.skill_evolution import grader_v2
from app.core.skill_evolution.contracts import load_reference
from app.core.skill_evolution.grader_registry import GRADER_V2
from app.core.skill_evolution.model_review import SemanticReviewer
from app.core.skill_evolution.errors import SkillEvolutionError, GraderConfigError


class V2EvalError(SkillEvolutionError):
    pass


def dataset_fingerprint(ds_dir: Path) -> str:
    """数据集冻结指纹：dataset.json + references/* + sources/* 逐字节内容哈希。"""
    import hashlib as _hl
    h = _hl.sha256()
    h.update(b"v2dev-fingerprint-v1\0")
    for name in sorted(["dataset.json"]):
        p_ = (ds_dir / name)
        h.update(name.encode("utf-8") + b"\0")
        h.update(p_.read_bytes())
    for sub in ("references", "sources"):
        for rel in sorted(str(x.relative_to(ds_dir)).replace("\\", "/")
                          for x in (ds_dir / sub).glob("*")):
            fp = ds_dir / rel
            if not fp.is_file():
                continue
            h.update(rel.encode("utf-8") + b"\0")
            h.update(fp.read_bytes())
    return h.hexdigest()


def load_v2_spec(dataset_dir: Path, task) -> grader_v2.V2Spec:
    doc = load_reference(dataset_dir, task.reference_ref) or {}
    # 沿用 v1 参考文件约定：reference 按 task_id 索引
    task_ref = doc.get(task.task_id)
    if isinstance(task_ref, dict):
        raw_spec = task_ref.get("v2_spec")
    else:
        raw_spec = None
    if not isinstance(raw_spec, dict):
        raise V2EvalError(
            f"任务 {task.task_id} 缺少 v2_spec（reference[{task.task_id}] 无 "
            f"'v2_spec' 块）：v2 评审不自动退回 v1 评分")
    spec = grader_v2.spec_from_json({"task_id": task.task_id, **raw_spec})
    return spec


def _source_texts(dataset_dir: Path, task) -> list[str]:
    """读取完整来源文本（不静默截断；不可读 → 明确 V2EvalError=invalid）。"""
    out: list[str] = []
    root = (dataset_dir / "sources").resolve()
    for src in task.sources or []:
        if not src.file:
            continue
        p = (root / src.file).resolve()
        if not p.is_file() or not p.is_relative_to(root):
            raise V2EvalError(f"来源文件缺失或越界: {src.file}")
        try:
            out.append(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise V2EvalError(f"来源不可读（utf-8 或 IO）: {src.file}") from exc
    return out


def _candidate_document(candidate: dict) -> dict:
    return {
        "sections": candidate.get("sections") or [],
        "summary": candidate.get("summary") or "",
    }


def _apply_review(verdict_map: dict, reviewer: SemanticReviewer | None,
                  *, task_id: str, sources: list[str],
                  candidate: dict) -> tuple[dict, int]:
    """未决语义项交给评审器；返回（任务判定 dict, 评审请求次数）。"""
    if reviewer is None:
        return verdict_map, 0
    unresolved = [it for it in verdict_map.get("items", [])
                  if it["status"] == grader_v2.ST_REVIEW]
    if not unresolved:
        return verdict_map, 0
    checks = [{"id": it["id"], "check": it["check"], "detail": it["detail"],
               "boundary": it.get("boundary", "")} for it in unresolved]
    try:
        review_out = reviewer.review(
            task_id=task_id, sources=sources,
            candidate_output=_candidate_document(candidate), checks=checks)
    except Exception as exc:  # noqa: BLE001  (ReviewerError 等)
        raise V2EvalError(f"任务 {task_id} 评审失败/非法输出（不回退 v1）: {exc}")
    # 复核契约：必需结果全覆盖、无未知/重复 id、位置与引文可追溯
    from app.core.skill_evolution.model_review import check_review_result as _check
    try:
        validated = _check(
            review_out, expected_check_ids=[c["id"] for c in checks],
            sources=sources,
            candidate_sections=_candidate_document(candidate).get("sections"))
    except Exception as exc:  # noqa: BLE001  (ReviewerError 等)
        raise V2EvalError(
            f"任务 {task_id} 评审结果复核失败（未知/重复/缺失/位置非法）: {exc}")
    resolutions: dict[str, str] = {}
    for it in validated.get("items") or []:
        if it["verdict"] in (grader_v2.ST_PASS, grader_v2.ST_FAIL):
            resolutions[it["check_id"]] = it["verdict"]
        # needs_review 不写入 resolution → 保持未决（不通过、不缩分母）
    final = grader_v2.evaluate(candidate, verdict_map["__spec__"],
                               resolutions=resolutions)
    return final, 1


def _review_records(reviewer) -> list[dict]:
    """保存评审理由/证据/身份/配置指纹/尝试次数（只入评估记录，不进优化上下文）。"""
    if reviewer is None:
        return []
    return [{
        "reviewer_identity": getattr(reviewer, "identity", None),
        "config_fingerprint": getattr(reviewer, "config_fingerprint", None),
        "attempts": int(getattr(reviewer, "last_attempts", 1) or 1),
        "last_error": getattr(reviewer, "last_error", None),
    }]


def grade_task_v2(*, dataset_dir: Path, task, candidate: dict,
                  reviewer: SemanticReviewer | None) -> dict:
    """v2 单任务评估入口：返回与 gating 兼容的 per-task dict。"""
    spec = load_v2_spec(dataset_dir, task)
    if not isinstance(candidate, dict) or not candidate.get("sections"):
        return {
            "task_id": task.task_id,
            "verdict": grader_v2.ST_INVALID,
            "evaluation_invalid": "候选缺少 sections（数据故障）",
            "checks_passed": 0, "checks_total": 0,
            "review_requests": 0,
            "grader_version": GRADER_V2,
        }
    base = grader_v2.evaluate(candidate, spec)
    base["__spec__"] = spec  # 传引用供评审后重算
    sources = _source_texts(dataset_dir, task)
    try:
        final, reqs = _apply_review(base, reviewer, task_id=task.task_id,
                                    sources=sources, candidate=candidate)
    except V2EvalError as exc:
        return {
            "task_id": task.task_id,
            "verdict": "invalid",
            "v2_verdict": grader_v2.ST_INVALID,
            "evaluation_invalid": str(exc),
            "checks_passed": 0,
            "checks_total": len(base.get("items") or []),
            "unresolved_ids": [it["id"] for it in base.get("items") or []
                               if it["status"] == grader_v2.ST_REVIEW],
            "promotable": False,
            "review_requests": 0,
            "grader_version": GRADER_V2,
            "reviewer_identity": getattr(reviewer, "identity", None),
        }
    final.pop("__spec__", None)
    verdict = final.get("verdict")
    if verdict in (grader_v2.ST_REVIEW, grader_v2.ST_INVALID):
        verdict_display = "needs_review" if verdict == grader_v2.ST_REVIEW \
            else "invalid"
    else:
        verdict_display = verdict  # pass / fail
    decided = [it for it in final.get("items", [])
               if it.get("status") in (grader_v2.ST_PASS, grader_v2.ST_FAIL)]
    records = _review_records(reviewer)
    return {
        "task_id": task.task_id,
        "verdict": verdict_display,
        "v2_verdict": verdict,
        "evaluation_invalid": final.get("evaluation_invalid"),
        "unresolved_ids": final.get("unresolved_ids") or [],
        "promotable": bool(final.get("promotable")),
        "checks_passed": sum(1 for it in decided
                             if it["status"] == grader_v2.ST_PASS),
        "checks_total": len(decided),
        "items": final.get("items") or [],
        "review_requests": reqs,
        "grader_version": final.get("grader_version"),
        "reviewer_identity": getattr(reviewer, "identity", None),
        "review_records": records,
    }
