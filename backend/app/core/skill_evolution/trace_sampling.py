"""维护者输入：授权训练轨迹选择、采样与上下文日志（只读，不修改 Raw）。

规则：
- 只允许显式授权 train 执行（split='train' 且 dataset_version/domain 一致且所属
  workspace 匹配）；val/test 及跨 workspace 轨迹即使 ID 已知也不可读；
- 轨迹必须已封存且哈希有效；损坏/未封存 → 拒绝；
- 采样上限默认失败 5 条、成功 3 条；不足用实际数量，不复制凑数；排序确定性；
- 基础设施失败默认单独分类，不作为技能缺陷证据（include_infra=False 时不进入失败池）；
- 每条日志默认上限 15,000 字符：标记截断与原始长度；保留动作/结果/评分上下文；
- 阶段 1 历史缺指令版本 → 明确标记 unknown/no-injection，不推断技能版本。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.skill_evolution import runenv
from app.core.skill_evolution.contracts import DatasetSpec
from app.core.skill_evolution.errors import SkillStoreError
from app.core.skill_evolution.experience_store import ExperienceStoreError
from app.core.skill_evolution.trace import TraceError, load_meta, verify_sealed

DEFAULT_MAX_FAILURES = 5
DEFAULT_MAX_SUCCESSES = 3
DEFAULT_LOG_CHAR_CAP = 15_000
DEFAULT_PROMPT_BUDGET = 90_000

# 基础设施/模型失败标记（不作为技能缺陷证据）。
_INFRA_FAILURES = ("infra_or_model",)


def group_workspace_id(group_id: str) -> str:
    """由任务 group 派生实验 workspace id（与 adapter.materialize 口径一致）。"""
    suffix = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:12]
    return f"ws_{suffix}"


class MaintainerInputError(ExperienceStoreError):
    """授权/采样/上下文构建错误。"""


def _safe_meta(root: Path, execution_id: str) -> dict | None:
    run_dir = root / "runs" / execution_id
    try:
        return load_meta(run_dir)
    except (TraceError, OSError):
        return None


def list_authorized_train_meta(root: Path, dataset: DatasetSpec,
                               workspace_id: str) -> list[dict]:
    """枚举可被维护者使用的 train 执行（已封存、作用域一致、split=train）。"""
    root = Path(root).resolve()
    runs_dir = root / "runs"
    if not runs_dir.is_dir():
        return []
    out: list[dict] = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        meta = _safe_meta(root, run_dir.name)
        if meta is None:
            continue
        try:
            verify_sealed(run_dir)
        except TraceError:
            continue  # 损坏/未封存不可用于维护
        if meta.get("split") != "train":
            continue
        if meta.get("dataset_version") != dataset.dataset_version:
            continue
        if meta.get("domain") != dataset.domain:
            continue
        group = meta.get("group_id")
        if not group or group_workspace_id(str(group)) != workspace_id:
            continue
        out.append(meta)
    out.sort(key=lambda m: (m.get("created_at") or "", m.get("execution_id") or ""))
    return out


def _candidate_kind(root: Path, meta: dict) -> str:
    """分类：success / quality_failure / infra / unknown。

    判定来自冻结 grader 的 grade.json，不再把「编译成功且无 grade」当 success。
    """
    from app.core.skill_evolution.train_grading import interpret_grade_kind
    return interpret_grade_kind(root, meta)


def sample_executions(root: Path, metas: list[dict], *,
                      max_failures: int = DEFAULT_MAX_FAILURES,
                      max_successes: int = DEFAULT_MAX_SUCCESSES,
                      include_infra: bool = False) -> list[dict]:
    """确定性采样（失败优先，其次成功）；不足按实际数量，不复制凑数。"""
    if max_failures < 0 or max_successes < 0:
        raise MaintainerInputError("采样上限不能为负")
    failures = [m for m in metas if _candidate_kind(root, m) == "quality_failure"]
    successes = [m for m in metas if _candidate_kind(root, m) == "success"]
    if include_infra:
        failures += [m for m in metas if _candidate_kind(root, m) == "infra"]
    failures.sort(key=lambda m: (m.get("created_at") or "", m.get("execution_id") or ""))
    successes.sort(key=lambda m: (m.get("created_at") or "", m.get("execution_id") or ""))
    chosen = failures[:max_failures] + successes[:max_successes]
    chosen.sort(key=lambda m: (m.get("created_at") or "", m.get("execution_id") or ""))
    return chosen


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    truncated = len(text) > cap
    return (text[:cap] + "\n…(截断)" if truncated else text), truncated


def build_context_log(root: Path, meta: dict, *,
                      log_char_cap: int = DEFAULT_LOG_CHAR_CAP) -> dict:
    """单条执行日志（meta 摘要 + 阶段/评分 + 输出开头；带截断标记）。"""
    root = Path(root).resolve()
    run_dir = root / "runs" / meta["execution_id"]
    lines: list[str] = []
    lines.append(f"execution_id={meta['execution_id']}")
    lines.append(f"task={meta.get('task_id')} split={meta.get('split')} "
                 f"group={meta.get('group_id')}")
    skills = meta.get("skills") or {}
    if not skills or skills.get("mode") in (None, "none"):
        lines.append("skill_instruction=unknown/not_injected（阶段 1 历史或未启用）")
    elif skills.get("mode") == "empty":
        lines.append("skill_instruction=explicit_empty_set(not_injected)")
    else:
        lines.append("skill_instruction=injected versions="
                     + ",".join(skills.get("version_ids") or []))
    lines.append(f"run_status={meta.get('run_status')} "
                 f"outcome_kind={meta.get('outcome', {}).get('failure_kind')} "
                 f"published={(meta.get('candidate') or {}).get('revision_id') is not None}")
    stage_notes = [
        f"{st.get('stage_key')}:{st.get('status')}"
        + (f"({st.get('safe_error_code') or st.get('error_code')})"
           if st.get('status') == 'failed' else "")
        for st in (meta.get("stages") or [])
        if st.get("status") in ("failed", "succeeded")
    ]
    if stage_notes:
        lines.append("stages=" + ",".join(stage_notes))
    grade_summary = None
    try:
        from app.core.skill_evolution.train_grading import safe_grade_summary
        grade_summary = safe_grade_summary(root, meta)
    except Exception:  # noqa: BLE001
        grade_summary = None
    if grade_summary:
        fails = [str(x) for x in (grade_summary.get("checks_failed") or []) if x]
        lines.append("grade_verdict=" + str(grade_summary.get("verdict"))
                     + (" failing=" + ",".join(fails) if fails else ""))
    out_md = run_dir / "output.md"
    if out_md.is_file():
        try:
            head = out_md.read_text(encoding="utf-8")
        except OSError:
            head = ""
        if head:
            lines.append("output_head=" + head[:2000].replace("\n", "⏎"))
    text = "\n".join(lines)
    clipped, truncated = _truncate(text, log_char_cap)
    return {
        "execution_id": meta["execution_id"],
        "original_chars": len(text),
        "truncated": truncated,
        "text": clipped,
    }


def build_maintainer_context(root: Path, metas: list[dict], *,
                             log_char_cap: int = DEFAULT_LOG_CHAR_CAP,
                             prompt_budget: int = DEFAULT_PROMPT_BUDGET) -> list[dict]:
    logs = [build_context_log(root, m, log_char_cap=log_char_cap) for m in metas]
    total = sum(l["original_chars"] for l in logs) + 2000
    if total > prompt_budget:
        raise MaintainerInputError(
            f"维护上下文超预算：logs_chars={total - 2000} > 预算 {prompt_budget}；"
            "请减少采样量/提高预算（不静默截断历史）")
    return logs
