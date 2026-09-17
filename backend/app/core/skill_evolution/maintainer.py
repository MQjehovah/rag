"""阶段 3 Wiki Maintainer：授权轨迹采样 → 模型（可注入）→ 结构化建议 → 事务应用。

- 模型只输出结构化建议（role=wiki-maintain），由程序校验后落库；
- 维护者调用复用 runner 接口（messages + context + timeout），事件与 usage 记录在
  maintenance run 行（role 独立，绝不与执行 Agent 技能注入混用）；
- 应用原子、幂等、冲突拒绝（experience_store.apply_plan）；
- 非法输出/超时 → 仅保存失败诊断（failed run 行），不污染经验 Wiki；
- 输出关注：现象/证据支持的原因假设(待验证显式标记)/建议/适用性/证据/反例；
  不把单次观察自动标为规律（默认 observed）。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.core.skill_evolution import skill_store
from app.core.skill_evolution.contracts import DatasetSpec, canonical_json
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.experience_store import (
    DOMAIN_DEFAULT,
    MAX_LOG_TEXT,
    ExperienceStoreError,
    MaintenanceConflictError,
    apply_plan,
    build_index,
    find_run_by_key,
    get_index,
    list_patterns,
    record_failed_run,
)
from app.core.skill_evolution.trace_sampling import (
    DEFAULT_LOG_CHAR_CAP,
    DEFAULT_MAX_FAILURES,
    DEFAULT_MAX_SUCCESSES,
    DEFAULT_PROMPT_BUDGET,
    MaintainerInputError,
    build_maintainer_context,
    group_workspace_id,
    list_authorized_train_meta,
    sample_executions,
)

MAINTAINER_CONTEXT = "wiki-maintain"
MAX_ATTEMPTS = 2
MAX_PROMPT_CHARS = DEFAULT_PROMPT_BUDGET

_TOP_FIELDS = ("create_patterns", "update_patterns", "update_index", "append_log")


class MaintainerSchemaError(ExperienceStoreError):
    """模型输出结构非法（可保存诊断，不产生经验写入）。"""


@dataclass
class MaintenanceRunSummary:
    run_id: str
    status: str
    idempotent_hit: bool = False
    created_patterns: list = field(default_factory=list)
    updated_patterns: list = field(default_factory=list)
    index_hash: str | None = None
    model_calls: int = 0
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "idempotent_hit": self.idempotent_hit,
            "created_patterns": self.created_patterns,
            "updated_patterns": self.updated_patterns,
            "index_hash": self.index_hash,
            "model_calls": self.model_calls,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def base_state(db: Session, workspace_id: str, domain: str) -> dict:
    """当前基础经验修订（用于幂等键与冲突比较）。"""
    index = get_index(db, workspace_id, domain)
    heads = {
        p["pattern_id"]: p["current_revision_id"]
        for p in list_patterns(db, workspace_id, domain)
    }
    return {
        "index_hash": (index or {}).get("content_hash"),
        "patterns": heads,
    }


# ---------------------------------------------------------------------------
# 提示词与模拟维护者（流程验证用；真实供应商经同一 runner 接口）
# ---------------------------------------------------------------------------


def _exec_ids_from_metas(metas: list[dict]) -> list[str]:
    seen: list[str] = []
    for m in metas:
        eid = m.get("execution_id")
        if eid and eid not in seen:
            seen.append(eid)
    return seen


def build_prompt(*, workspace_id: str, domain: str, index: dict | None,
                 patterns: list[dict], logs: list[dict], feedback: list[str],
                 skills_summary: str, kinds: list[str] | None = None) -> str:
    index_text = "（无）" if not index else "\n".join(
        f"- {e['pattern_id']}: 问题={e['problem'][:120]} 原因={e['cause'][:120]} "
        f"建议={e['suggestion'][:120]}"
        for e in index.get("index", []))
    patterns_text = "（无）" if not patterns else "\n".join(
        f"- {p['pattern_id']} [{p['status']}] {p['title']}（修订 "
        f"{p['current_revision_id']}）"
        for p in patterns)
    logs_text = "（无采样）" if not logs else "\n\n".join(
        f"[{l['execution_id']}] chars={l['original_chars']} "
        f"truncated={str(l['truncated']).lower()}\n{l['text']}"
        for l in logs)
    feedback_text = "（无）" if not feedback else "\n".join(f"- {f}" for f in feedback)
    kinds_text = "\n".join(f"- {k}" for k in (kinds or [])) if kinds else "（无）"
    return (
        "你是 WikiSkill 经验维护者。基于以下内容提炼/更新经验模式。\n"
        "要求：描述实际发生；说明成功与失败执行的差别；区分证据支持的结论与待验证假设"
        "（待验证要写明）；说明当前技能是否被执行、是否可能有帮助；给出可推广性与反例。\n"
        "输出必须严格 JSON，只允许顶层字段 "
        "create_patterns/update_patterns/update_index/append_log。\n"
        "create_patterns 项字段：title/phenomenon/cause_hypothesis/suggestion/"
        "applicability/supporting_execution_ids/conflicting_execution_ids。\n"
        "update_patterns 项字段：pattern_id/base_revision_id/revise_fields/"
        "append_support_execution_ids/append_conflict_execution_ids。\n"
        "证据 id 只能引用下述授权执行集合。\n"
        f"workspace={workspace_id} domain={domain}\n"
        f"技能版本说明：{skills_summary}\n"
        "--- 当前经验索引 ---\n" + index_text + "\n"
        "--- 当前模式 ---\n" + patterns_text + "\n"
        "--- 授权训练执行采样（只读，禁止外推） ---\n" + logs_text + "\n"
        "--- 采样分类 ---\n" + kinds_text + "\n"
        "--- 授权训练反馈 ---\n" + feedback_text + "\n"
    )


@dataclass
class SimulatedMaintainer:
    """确定性模拟维护者（流程验证；不读任何参考/答案文件）。

    profile=create：从失败样本创建一条模式（支持证据=首批失败样本）。
    profile=update：对指定/索引首个模式追加本轮证据并修正假设文本。
    """

    profile: str = "create"
    target_pattern_id: str | None = None
    marker: str = "（第二轮补充证据）"

    def __call__(self, messages, context: str = "", timeout: float = 120.0) -> dict:
        if context != MAINTAINER_CONTEXT:
            raise MaintainerSchemaError(f"未建模的维护上下文: {context!r}")
        prompt = messages[0]["content"] if messages else ""
        # 只在本轮“授权训练执行采样”区段内取 id（防止旧轮 id 混入证据）。
        section = prompt.split("--- 授权训练执行采样（只读，禁止外推） ---", 1)
        section = section[1].split("--- 采样分类 ---", 1)[0] if len(section) > 1 else prompt
        ids = re.findall(r"\b([0-9a-f]{32})\b", section)
        log_ids = [i for i in ids]
        kinds = re.findall(r"- ([0-9a-f]{32}) kind=(\w+)", section)
        failure_ids = [eid for eid, k in kinds if k in ("quality_failure", "failure")]
        support = (failure_ids or log_ids)[:2]
        pattern_id = self.target_pattern_id
        if pattern_id is None:
            m = re.search(r"- (pat_[0-9a-f]+) \[[a-z]+\]", prompt)
            if m:
                pattern_id = m.group(1)
        if self.profile == "update":
            if not pattern_id:
                raise MaintainerSchemaError("update profile: 提示词中没有可更新模式")
            base = re.search(r"修订 ([0-9a-f-]{36})", prompt)
            return {
                "update_patterns": [{
                    "pattern_id": pattern_id,
                    "base_revision_id": (base.group(1) if base else ""),
                    "revise_fields": {
                        "cause_hypothesis": "新增证据后修正的假设：" + self.marker,
                    },
                    "append_support_execution_ids": support,
                    "append_conflict_execution_ids": [],
                }],
                "append_log": [f"更新 {pattern_id}：追加证据并修正原因假设 {self.marker}"],
                "update_index": True,
            }
        return {
            "create_patterns": [{
                "title": "失败集中在条件/前置要点覆盖不足",
                "phenomenon": "多个训练失败样本显示正文缺少来源明确存在的适用/前置条件要点"
                              f"（支持证据 {support}）",
                "cause_hypothesis": "（待验证）技能指令未显式要求逐条覆盖条件要点，"
                                    "或生成模型倾向省略条件段落",
                "suggestion": "在 default 指令的步骤中显式要求逐条覆盖适用条件、"
                              "前置条件、操作步骤并回指来源行",
                "applicability": "default 通用主题编译，来源资料含明确条件/步骤段落",
                "supporting_execution_ids": support,
                "conflicting_execution_ids": [],
            }],
            "append_log": [
                f"创建模式：失败集中在条件/前置要点覆盖不足（支持 {support}）",
                "观察仅一次，状态 observed；待更多证据确认。",
            ],
            "update_index": True,
        }


def call_maintainer(runner: Callable, prompt: str) -> dict:
    """带有限重试的结构化调用（attempt<=2）；非法输出 → MaintainerSchemaError。"""
    messages = [{"role": "user", "content": prompt}]
    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = runner(messages, context=MAINTAINER_CONTEXT, timeout=120.0)
        if not isinstance(raw, dict):
            last_error = f"attempt {attempt}: 输出非 JSON 对象（{type(raw).__name__}）"
            messages = [{"role": "user",
                         "content": prompt + "\n[系统] 输出结构非法，请严格按 Schema 输出 JSON 对象。"}]
            continue
        extra = set(raw) - set(_TOP_FIELDS)
        if extra:
            raise MaintainerSchemaError(f"未知顶层字段: {sorted(extra)}")
        return raw
    raise MaintainerSchemaError(last_error or "模型输出结构非法")


# ---------------------------------------------------------------------------
# 维护运行编排
# ---------------------------------------------------------------------------


def run_maintenance(
    root,
    dataset: DatasetSpec,
    workspace_id: str,
    *,
    domain: str = DOMAIN_DEFAULT,
    execution_ids: list[str] | None = None,
    runner: Callable | None = None,
    profile: str = "create",
    target_pattern_id: str | None = None,
    feedback: list[str] | None = None,
    max_failures: int = DEFAULT_MAX_FAILURES,
    max_successes: int = DEFAULT_MAX_SUCCESSES,
    include_infra: bool = False,
    log_char_cap: int = DEFAULT_LOG_CHAR_CAP,
    prompt_budget: int = DEFAULT_PROMPT_BUDGET,
    idempotency_extra: str = "",
) -> MaintenanceRunSummary:
    """执行一次维护；返回运行摘要。失败/冲突以异常抛出（诊断已落 failed run 行）。"""
    root_path = root if isinstance(root, str) else str(root)
    root_path = __import__("pathlib").Path(root_path)
    if not workspace_id.startswith("ws_"):
        # 允许直接传 group_id 形式
        workspace_id = group_workspace_id(workspace_id)

    authorized = list_authorized_train_meta(root_path, dataset, workspace_id)
    authorized_by_id = {m["execution_id"]: m for m in authorized}
    if execution_ids:
        missing = [e for e in execution_ids if e not in authorized_by_id]
        if missing:
            raise MaintainerInputError(
                f"未授权/不存在的执行（train+同数据集+同 workspace 才可读）: {missing}")
        metas = [authorized_by_id[e] for e in dict.fromkeys(execution_ids)]
        metas.sort(key=lambda m: (m.get("created_at") or "", m.get("execution_id") or ""))
    else:
        metas = sample_executions(
            root_path, authorized, max_failures=max_failures,
            max_successes=max_successes, include_infra=include_infra)
    if not metas:
        raise MaintainerInputError(
            "没有可用的授权 train 执行（无成功/质量失败样本）")

    logs = build_maintainer_context(root_path, metas, log_char_cap=log_char_cap,
                                    prompt_budget=prompt_budget)
    skills_versions = sorted({
        ",".join((m.get("skills") or {}).get("version_ids") or [])
        for m in metas if (m.get("skills") or {}).get("mode") == "versions"
    })
    skills_summary = (
        "versions=" + ";".join(skills_versions) if skills_versions
        else "unknown/no-injection（历史或未启用，不推断版本）")

    db = skill_store.session_for(root_path)
    try:
        state = base_state(db, workspace_id, domain)
    finally:
        db.close()

    config = {
        "profile": profile,
        "target_pattern_id": target_pattern_id,
        "max_failures": max_failures,
        "max_successes": max_successes,
        "include_infra": include_infra,
        "log_char_cap": log_char_cap,
        "prompt_budget": prompt_budget,
        "idempotency_extra": idempotency_extra,
    }
    input_ids = [m["execution_id"] for m in metas]
    idem_raw = {
        "scope": [workspace_id, domain],
        "dataset": dataset.dataset_version,
        "inputs": input_ids,
        "config": config,
    }
    idempotency_key = hashlib.sha256(
        canonical_json(idem_raw).encode("utf-8")).hexdigest()
    import uuid as _uuid
    run_id = "maint_" + _uuid.uuid4().hex[:24]

    from app.core.skill_evolution.trace_sampling import _candidate_kind as _kind
    kinds = [f"{m['execution_id']} kind={_kind(root_path, m)}" for m in metas]
    prompt = build_prompt(
        workspace_id=workspace_id, domain=domain,
        index=get_index(db, workspace_id, domain),
        patterns=list_patterns(db, workspace_id, domain),
        logs=logs, feedback=feedback or [],
        skills_summary=skills_summary, kinds=kinds)

    model_calls: list[dict] = []
    db = skill_store.session_for(root_path)
    try:
        existing = find_run_by_key(db, idempotency_key)
        if existing is not None:
            return MaintenanceRunSummary(
                run_id=existing["run_id"], status=existing["status"],
                idempotent_hit=True)
    finally:
        db.close()

    effective_runner = runner or SimulatedMaintainer(
        profile=profile, target_pattern_id=target_pattern_id)
    rec = _RecordingMaintainer(effective_runner)
    try:
        output = call_maintainer(rec, prompt)
    except Exception as exc:
        _record_failure(root_path, workspace_id, domain, run_id, idempotency_key,
                        config, input_ids,
                        type(exc).__name__, str(exc), rec.model_calls_json())
        if isinstance(exc, ExperienceStoreError):
            raise
        raise MaintainerSchemaError(f"{type(exc).__name__}: {exc}") from exc
    rec.attach_inputs(input_ids)

    db = skill_store.session_for(root_path)
    try:
        applied = apply_plan(
            db, run_id=run_id, idempotency_key=idempotency_key,
            workspace_id=workspace_id, domain=domain,
            config_json=json.dumps(config, ensure_ascii=False, sort_keys=True),
            input_execution_ids=input_ids,
            allowed_execution_ids=set(input_ids),
            base_state=state,
            model_calls_json=json.dumps(
                rec.model_calls_json(), ensure_ascii=False, default=str),
            create_patterns=output.get("create_patterns") or [],
            update_patterns=output.get("update_patterns") or [],
            log_entries=output.get("append_log") or [],
        )
        if applied.get("idempotent_hit"):
            return MaintenanceRunSummary(**{**applied, "model_calls": len(rec.calls)})
        return MaintenanceRunSummary(
            run_id=run_id, status="applied",
            created_patterns=applied["created_patterns"],
            updated_patterns=applied["updated_patterns"],
            index_hash=applied["index_hash"], model_calls=len(rec.calls))
    except (ExperienceStoreError, MaintenanceConflictError) as exc:
        db.rollback()
        _record_failure(root_path, workspace_id, domain, run_id, idempotency_key,
                        config, input_ids, type(exc).__name__, str(exc),
                        rec.model_calls_json())
        raise
    finally:
        db.close()


def _record_failure(root, workspace_id, domain, run_id, idempotency_key, config,
                    input_ids, code, message, model_calls_json) -> None:
    db = skill_store.session_for(root)
    try:
        # 失败诊断用独立键（主幂等键只被 applied 占用），修复后可重试同一次维护。
        record_failed_run(
            db, run_id=run_id, idempotency_key=idempotency_key + ":failed",
            workspace_id=workspace_id, domain=domain,
            config_json=json.dumps(config, ensure_ascii=False, sort_keys=True),
            input_execution_ids=input_ids,
            error_code=code, error_message=message)
    finally:
        db.close()


class _RecordingMaintainer:
    """记录维护者角色调用（消息 + 结果/异常 + 估算；usage=null，不伪造成本）。"""

    def __init__(self, inner: Callable):
        self._inner = inner
        self.calls: list[dict] = []
        self.inputs: list[str] = []

    def attach_inputs(self, input_ids: list[str]) -> None:
        self.inputs = list(input_ids)

    def __call__(self, messages, context: str = "", timeout: float = 120.0):
        entry: dict = {
            "context": context,
            "messages": list(messages),
            "inputs": list(self.inputs),
            "estimated_input_chars": sum(
                len((m.get("content") or "") if isinstance(m, dict) else "")
                for m in (messages or [])),
            "usage_tokens": None,
        }
        try:
            result = self._inner(messages, context=context, timeout=timeout)
            entry["response_ok"] = isinstance(result, dict)
            entry["response_summary"] = json.dumps(
                result, ensure_ascii=False, default=str)[:4000]
            self.calls.append(entry)
            return result
        except Exception as exc:  # noqa: BLE001
            entry["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self.calls.append(entry)
            raise

    def model_calls_json(self) -> list[dict]:
        return list(self.calls)
