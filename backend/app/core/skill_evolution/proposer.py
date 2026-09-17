"""阶段 4 Skill Proposer：受控多轮“模型→工具→模型→提交提案”。

- 工具集（受控 ID 参数；禁止路径/shell/DB/网络）：
  read_index / read_pattern / read_skill_history / read_skill_version / read_trace。
- 授权：读取限于同一 workspace+domain+dataset 作用域、train 且已封存哈希有效；
  val/test、跨 workspace、损坏轨迹拒绝。
- create/patch 提交前要求成功读取 ≥4 条**不同**轨迹（重复/失败不计数）；
  no_action 不强制 4 条。
- 提案证据 ⊆ 本轮实际读取轨迹；Pattern 引用 ⊆ 固定快照且实际读取。
- 启动时固定基础经验快照与技能父版本（不无声切换最新）。
- 候选版本与提案/运行行原子提交；不改变任何活动绑定；无 accepted/rejected。
- 状态：candidate_saved / no_action / output_invalid / prerequisites_insufficient /
  failed / budget_exhausted。
- 预算耗尽独立于 no_action 与效果拒绝。usage=null 不伪造成本。
- 本阶段脚本化 fake proposer 验证多轮工具流程（不声明真实模型有效提议）。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.core.skill_evolution import skill_store
from app.core.skill_evolution.contracts import DatasetSpec, canonical_json
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.experience_store import DOMAIN_DEFAULT, get_index, list_patterns
from app.core.skill_evolution.trace import TraceError, load_meta, verify_sealed
from app.core.skill_evolution.trace_sampling import (
    DEFAULT_LOG_CHAR_CAP,
    build_context_log,
    group_workspace_id,
    list_authorized_train_meta,
)
from app.models.evolution import (
    PROPOSAL_BUDGET_EXHAUSTED,
    PROPOSAL_CANDIDATE_SAVED,
    PROPOSAL_FAILED,
    PROPOSAL_NO_ACTION,
    PROPOSAL_OUTPUT_INVALID,
    PROPOSAL_PREREQ_INSUFFICIENT,
    PROPOSER_ACTION_CREATE,
    PROPOSER_ACTION_NO_ACTION,
    PROPOSER_ACTION_PATCH,
    EvolutionProposal,
    EvolutionProposalRun,
)

ROLE_CONTEXT = "wiki-propose"
MIN_DISTINCT_READS = 4
VALID_TOOLS = ("read_index", "read_pattern", "read_skill_history",
               "read_skill_version", "read_trace")
TOOL_CHAR_CAP = 20_000
DEFAULT_MAX_MODEL_TURNS = 6
DEFAULT_MAX_TOOL_CALLS = 12
DEFAULT_MAX_REPAIRS = 2
OP_TYPES = {"replace", "insert_after"}
PATCH_TARGETS = {"SKILL.md", "PURPOSE.md"}


class ProposerError(SkillEvolutionError):
    """提议框架错误。"""


class ProposerInputError(ProposerError):
    """结构/前置/授权错误。"""


# ---------------------------------------------------------------------------
# 快照 / 读取跟踪
# ---------------------------------------------------------------------------


def build_scope_snapshot(db: Session, workspace_id: str, domain: str,
                         parent_version_id: str | None) -> dict:
    index = get_index(db, workspace_id, domain)
    patterns = {}
    for p in list_patterns(db, workspace_id, domain):
        patterns[p["pattern_id"]] = {
            "title": p["title"],
            "status": p["status"],
            "current_revision_id": p["current_revision_id"],
            "content": p.get("revision", {}).get("content"),
        }
    skills = {}
    if parent_version_id:
        v = skill_store.get_version(db, parent_version_id)
        skills[v.skill_id] = {"version_id": v.version_id,
                              "content_hash": v.content_hash}
    return {
        "index_hash": (index or {}).get("content_hash"),
        "index_entries": (index or {}).get("index") or [],
        "patterns": patterns,
        "skills": skills,
    }


class ReadTracker:
    def __init__(self, root: Path, allowed: set[str]):
        self.root = Path(root)
        self.allowed = allowed
        self.read_distinct: set[str] = set()
        self.tool_events: list[dict] = []

    def read_trace(self, execution_id: str) -> str:
        if execution_id not in self.allowed:
            raise ProposerInputError(
                f"读取越权/未授权轨迹: {execution_id}（仅 train+同作用域）")
        run_dir = self.root / "runs" / execution_id
        try:
            verify_sealed(run_dir)
        except TraceError as exc:
            raise ProposerInputError(f"轨迹未封存/损坏: {execution_id}") from exc
        meta = load_meta(run_dir)
        entry = build_context_log(self.root, meta,
                                  log_char_cap=min(TOOL_CHAR_CAP, DEFAULT_LOG_CHAR_CAP))
        self.read_distinct.add(execution_id)
        self.tool_events.append({
            "tool": "read_trace",
            "execution_id": execution_id,
            "return_chars": entry["original_chars"],
            "truncated": entry["truncated"],
        })
        return entry["text"]


# ---------------------------------------------------------------------------
# 受控补丁
# ---------------------------------------------------------------------------


def apply_patch_ops(base: str, ops: list[dict], *, target: str) -> str:
    out = base
    for op in ops:
        if op.get("file") != target:
            continue
        kind = op.get("type")
        if kind not in OP_TYPES:
            raise ProposerInputError(f"{target}: 未知补丁类型 {kind!r}")
        anchor = str(op.get("anchor") or "")
        if not anchor:
            raise ProposerInputError(f"{target}: 缺少 anchor")
        count = out.count(anchor)
        if count != 1:
            raise ProposerInputError(
                f"{target}: anchor 失配/歧义（匹配 {count} 次，需唯一精确命中）")
        if kind == "replace":
            out = out.replace(anchor, str(op.get("replacement") or ""), 1)
        else:  # insert_after
            idx = out.index(anchor) + len(anchor)
            out = out[:idx] + "\n" + str(op.get("text") or "") + out[idx:]
    return out


def patch_package(skill_md: str, purpose_md: str, ops: list[dict]) -> tuple[str, str]:
    if not isinstance(ops, list):
        raise ProposerInputError("ops 必须是列表")
    unknown = [op.get("file") for op in ops if op.get("file") not in PATCH_TARGETS]
    if unknown:
        raise ProposerInputError(f"补丁目标文件非法: {unknown}")
    if not ops:
        raise ProposerInputError("补丁为空")
    return (apply_patch_ops(skill_md, ops, target="SKILL.md"),
            apply_patch_ops(purpose_md, ops, target="PURPOSE.md"))


# ---------------------------------------------------------------------------
# 脚本化 fake proposer（验证多轮协议）
# ---------------------------------------------------------------------------


@dataclass
class SimulatedProposer:
    profile: str = PROPOSER_ACTION_PATCH
    parent_version_id: str | None = "default:0001"
    skill_id: str | None = None

    def __post_init__(self) -> None:
        self._pending_reads: list[str] = []
        self._done = False
        self._parsed = False

    def __call__(self, messages, context: str = "", timeout: float = 120.0) -> dict:
        if context != ROLE_CONTEXT:
            raise ProposerError(f"未建模的提议上下文: {context!r}")
        if self._done:
            raise ProposerError("重复 finish")
        prompt = "\n".join(
            (m.get("content") or "") if isinstance(m, dict) else ""
            for m in (messages or []))
        if not self._parsed:
            ids = re.findall(r"- ([0-9a-f]{32}) .*kind=\w+", prompt)
            self._pending_reads = list(dict.fromkeys(ids))
            self._parsed = True
        pattern_id = None
        m = re.search(r"pat_[0-9a-f]{16}", prompt)
        if m:
            pattern_id = m.group(0)
        if self.profile == PROPOSER_ACTION_NO_ACTION:
            self._done = True
            return {"action": {
                "type": "no_action",
                "reason": "当前经验与轨迹未显示足够修改空间；本轮无技能修改。",
                "evidence_execution_ids": [],
                "pattern_ids": [],
                "pattern_revision_ids": [],
            }}
        if self._pending_reads:
            target = self._pending_reads.pop(0)
            return {"tool": "read_trace", "args": {"execution_id": target}}
        self._done = True
        # 证据取自本轮实际读取过的轨迹 id（在对话中被 read_trace 返回过）。
        evidence = re.findall(r"\b[0-9a-f]{32}\b", prompt)[:4]
        if self.profile == PROPOSER_ACTION_CREATE:
            sid = self.skill_id or "default-v2"
            return {"action": {
                "type": "create",
                "skill_id": sid,
                "skill_md": self._create_skill_md(sid),
                "purpose_md": self._create_purpose_md(),
                "reason": "依据训练轨迹缺失条件覆盖问题提出的候选指令（未评估）",
                "evidence_execution_ids": evidence,
                "pattern_ids": [pattern_id] if pattern_id else [],
                "pattern_revision_ids": [],
            }}
        anchor = "8. 参数与结论必须与来源一致（如扭矩、电压限值）；不确定时宁缺毋造。"
        return {"action": {
            "type": "patch",
            "skill_id": "default",
            "parent_version_id": self.parent_version_id or "default:0001",
            "parent_content_hash": None,
            "ops": [{
                "file": "SKILL.md", "type": "replace",
                "anchor": anchor,
                "replacement": anchor + "输出前逐条核对适用/前置条件的来源行。",
            }],
            "reason": "候选补丁：显式要求输出前逐条核对条件要点来源（未评估）",
            "evidence_execution_ids": evidence,
            "pattern_ids": [pattern_id] if pattern_id else [],
            "pattern_revision_ids": [],
        }}

    def _create_skill_md(self, skill_id: str) -> str:
        return (
            "```yaml\nskill_id: " + skill_id + "\n"
            "schema_version: \"1\"\n"
            "domain: wiki_compile.default\n"
            "runtime_ref: wiki.compile.default.runtime/v1\n```\n\n"
            "# 适用条件\n- default 领域通用主题编译（候选身份）。\n\n"
            "# 不适用条件\n- API Reference 结构化提取。\n\n"
            "# 操作步骤\n1. 只依据来源文档生成。\n"
            "2. 依次覆盖适用条件、前置条件、操作步骤、关键参数。\n"
            "3. 输出 JSON 并保留原提示词格式约束。\n"
            "4. 禁止引入资料外事实。\n"
        )

    def _create_purpose_md(self) -> str:
        return (
            "# 来源\n- 种子 default 人工整理与阶段 1–3 合成资料（无私有资料）。\n"
            "# 改进目的\n- 在 default 编译领域建立可独立评估的新技能身份。\n"
            "# 演化历史\n- 候选版本（未评估、未接受；来源 manual 整理模拟）。\n"
        )


# ---------------------------------------------------------------------------
# 工具执行器
# ---------------------------------------------------------------------------


class ToolExecutor:
    def __init__(self, db: Session, snapshot: dict, tracker: ReadTracker):
        self._db = db
        self._snapshot = snapshot
        self._tracker = tracker
        self._read_patterns: set[str] = set()
        self._tool_events = tracker.tool_events

    def call(self, tool: str, args: dict | None) -> str:
        args = args or {}
        if tool == "read_index":
            lines = [f"index_hash={self._snapshot.get('index_hash') or '-'}"]
            for e in self._snapshot.get("index_entries") or []:
                lines.append(f"- {e['pattern_id']} [{e['status']}] {e['title']}")
            return "\n".join(lines)
        if tool == "read_pattern":
            pid = str(args.get("pattern_id") or "")
            patterns = self._snapshot.get("patterns") or {}
            if pid not in patterns:
                raise ProposerInputError(f"Pattern 不在固定快照/作用域: {pid}")
            self._read_patterns.add(pid)
            self._tool_events.append({"tool": "read_pattern", "pattern_id": pid})
            return json.dumps(patterns[pid], ensure_ascii=False)[:TOOL_CHAR_CAP]
        if tool == "read_skill_history":
            sid = str(args.get("skill_id") or "")
            if not sid:
                raise ProposerInputError("缺少 skill_id")
            rows = skill_store.list_versions(self._db, skill_id=sid)
            lines = [
                f"- {r.version_id} hash={r.content_hash[:16]} src={r.source_type}"
                for r in rows] or ["（无版本）"]
            # 只暴露验证汇总反馈（真实门控历史；不含验证答案/逐任务参考/私有轨迹）。
            from app.core.skill_evolution import gating as _gate
            try:
                impact = _gate.skill_impact_summary(self._db, sid)
            except Exception:  # noqa: BLE001
                impact = []
            if impact:
                lines.append("--- gate impact (汇总) ---")
                for im in impact[:10]:
                    lines.append(
                        f"- {im['decision']} reason={im['reason']} "
                        f"score={im['candidate_score']} best={im['best_score']} "
                        f"exp={im['experiment_id']} ds={im['dataset_version']} "
                        f"grader={im['grader_version']} "
                        f"versions={im['candidate_version_ids']}")
            else:
                lines.append("--- gate impact: （尚无真实门控历史）---")
            return "\n".join(lines)
        if tool == "read_skill_version":
            vid = str(args.get("version_id") or "")
            if not vid:
                raise ProposerInputError("缺少 version_id")
            v = skill_store.get_version(self._db, vid)
            return (f"version={v.version_id} skill={v.skill_id} hash={v.content_hash}\n"
                    "--- SKILL.md ---\n" + v.skill_md[:TOOL_CHAR_CAP])
        if tool == "read_trace":
            return self._tracker.read_trace(str(args.get("execution_id") or ""))
        raise ProposerInputError(f"未知工具: {tool!r}")

    def read_pattern_ids(self) -> set[str]:
        return set(self._read_patterns)


# ---------------------------------------------------------------------------
# 原子保存
# ---------------------------------------------------------------------------


def _save(db, *, run_id, key, ws, domain, dataset_version, status, config,
          authorized, tracker: ReadTracker, snapshot, payload: dict) -> str:
    """单事务保存（运行行 + 提案 + 可选不可变候选版本）。

    幂等键只占“成功终态”（candidate_saved/no_action）：这样失败/无效/预算耗尽诊断
    行（用分键）不阻塞同配置修复后重试；重复成功提交仍由 _find_existing 拦截。
    """
    if status not in (PROPOSAL_CANDIDATE_SAVED, PROPOSAL_NO_ACTION):
        key = f"{key}:{run_id[-12:]}"
    run_row = EvolutionProposalRun(
        run_id=run_id, kind="proposer", idempotency_key=key,
        workspace_id=ws, domain=domain, dataset_version=dataset_version,
        status=status,
        scope_snapshot_json=json.dumps(snapshot, ensure_ascii=False, default=str),
        authorized_execution_ids_json=json.dumps(sorted(authorized), ensure_ascii=False),
        proposer_config_json=json.dumps(config, ensure_ascii=False, sort_keys=True),
        read_execution_ids_json=json.dumps(sorted(tracker.read_distinct),
                                           ensure_ascii=False),
        tool_events_json=json.dumps(tracker.tool_events, ensure_ascii=False, default=str),
        model_calls_json=json.dumps(payload.get("model_calls") or [],
                                    ensure_ascii=False, default=str),
        error_code=payload.get("error_code"),
        error_message=payload.get("error_message"),
        finished_at=datetime.now(),
    )
    db.add(run_row)
    proposal_id = None
    candidate_version_id = None
    if payload.get("record_proposal"):
        action = payload["action"]
        pid = "prop_" + _uuid.uuid4().hex[:20]
        package = payload.get("package")
        if package is not None:
            if action == PROPOSER_ACTION_CREATE:
                if skill_store.list_versions(db, skill_id=package.skill_id):
                    raise ProposerInputError(
                        f"create: skill 身份已存在 {package.skill_id}")
                cand = skill_store.add_version_uncommitted(
                    db, package, parent_version_id=None,
                    created_by=f"proposer:{run_id}")
            else:
                parent_vid = payload.get("parent_version_id")
                cand = skill_store.add_version_uncommitted(
                    db, package, parent_version_id=parent_vid,
                    created_by=f"proposer:{run_id}")
            candidate_version_id = cand.version_id
        proposal = EvolutionProposal(
            proposal_id=pid, run_id=run_id, workspace_id=ws, domain=domain,
            dataset_version=dataset_version, action=action,
            skill_id=payload.get("skill_id") or "",
            parent_version_id=payload.get("parent_version_id"),
            parent_content_hash=payload.get("parent_content_hash"),
            patch_json=(json.dumps(payload.get("ops"), ensure_ascii=False)
                        if payload.get("ops") else None),
            reason=payload.get("reason") or "",
            pattern_ids_json=json.dumps(payload.get("pattern_ids") or [],
                                        ensure_ascii=False),
            pattern_revision_ids_json=json.dumps(
                payload.get("pattern_revision_ids") or [], ensure_ascii=False),
            evidence_execution_ids_json=json.dumps(
                payload.get("evidence_execution_ids") or [], ensure_ascii=False),
            candidate_version_id=candidate_version_id,
            candidate_content_hash=(package.content_hash() if package else None),
            status=status,
        )
        db.add(proposal)
        proposal_id = pid
        run_row.proposal_id = pid
    db.commit()
    return proposal_id, candidate_version_id


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


@dataclass
class ProposerRunSummary:
    run_id: str
    status: str
    idempotent_hit: bool = False
    proposal_id: str | None = None
    candidate_version_id: str | None = None
    action: str | None = None
    distinct_reads: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "status": self.status,
            "idempotent_hit": self.idempotent_hit,
            "proposal_id": self.proposal_id,
            "candidate_version_id": self.candidate_version_id,
            "action": self.action, "distinct_reads": self.distinct_reads,
            "model_calls": self.model_calls, "tool_calls": self.tool_calls,
            "error_code": self.error_code, "error_message": self.error_message,
        }


def _proposal_history_text(db: Session, ws: str, domain: str) -> str:
    rows = (db.query(EvolutionProposalRun)
            .filter(EvolutionProposalRun.workspace_id == ws,
                    EvolutionProposalRun.domain == domain)
            .order_by(EvolutionProposalRun.created_at.desc()).limit(8).all())
    return "\n".join(f"- {r.run_id} {r.status}" for r in rows) or "（无）"


def run_proposer(
    root,
    dataset: DatasetSpec,
    workspace_id: str,
    *,
    domain: str = DOMAIN_DEFAULT,
    parent_version_id: str | None = "default:0001",
    runner: Callable | None = None,
    profile: str = PROPOSER_ACTION_PATCH,
    execution_ids: list[str] | None = None,
    max_model_turns: int = DEFAULT_MAX_MODEL_TURNS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
    idempotency_extra: str = "",
    budget_guard=None,
) -> ProposerRunSummary:
    """执行一次提议（多轮工具循环），返回运行摘要；不改变活动绑定。"""
    root_path = Path(root)
    if not str(workspace_id).startswith("ws_"):
        workspace_id = group_workspace_id(workspace_id)

    authorized = list_authorized_train_meta(root_path, dataset, workspace_id)
    by_id = {m["execution_id"]: m for m in authorized}
    if execution_ids:
        missing = [e for e in execution_ids if e not in by_id]
        if missing:
            raise ProposerInputError(f"未授权执行: {missing}")
        metas = [by_id[e] for e in execution_ids]
    else:
        metas = authorized
    allowed_ids = {m["execution_id"] for m in metas}
    if not allowed_ids:
        raise ProposerInputError("无可用授权 train 轨迹")

    if budget_guard is not None:
        remaining = int(budget_guard.tool_available())
        max_tool_calls = min(int(max_tool_calls), remaining)
    config = {"profile": profile, "parent_version_id": parent_version_id,
              "max_model_turns": max_model_turns, "max_tool_calls": max_tool_calls,
              "max_repairs": max_repairs, "idempotency_extra": idempotency_extra}
    key = hashlib.sha256(canonical_json({
        "scope": [workspace_id, domain], "dataset": dataset.dataset_version,
        "inputs": sorted(allowed_ids), "config": config,
    }).encode("utf-8")).hexdigest()
    run_id = "propose_" + _uuid.uuid4().hex[:20]
    tracker: ReadTracker | None = None

    db = skill_store.session_for(root_path)
    try:
        existing = _find_existing(db, key)
        if existing is not None:
            return existing
        snapshot = build_scope_snapshot(db, workspace_id, domain, parent_version_id)

        from app.core.skill_evolution.trace_sampling import _candidate_kind
        summaries = "\n".join(
            f"- {m['execution_id']} task={m.get('task_id')} "
            f"kind={_candidate_kind(root_path, m)} run={m.get('run_status')}"
            for m in metas)
        skills_text = "\n".join(
            f"- {sid}@{v['version_id']} hash={v['content_hash'][:16]}"
            for sid, v in (snapshot.get("skills") or {}).items()) or "（无）"
        patterns_text = "\n".join(
            f"- {pid} [{p['status']}] {p['title']}" for pid, p in
            (snapshot.get("patterns") or {}).items()) or "（无）"
        intro = (
            "你是 WikiSkill 技能提议者（role=wiki-propose）。\n"
            f"domain={domain} workspace={workspace_id} "
            f"dataset={dataset.dataset_version}\n"
            "可用工具：read_index / read_pattern / read_skill_history / "
            "read_skill_version / read_trace（受控 ID 参数）。\n"
            "最终以 action 消息提交：create(skill_id/skill_md/purpose_md/reason) 或 "
            "patch(skill_id/parent_version_id/ops/reason) 或 no_action(reason)。\n"
            "create/patch 提交前必须成功读取 ≥4 条不同训练轨迹。\n"
            "证据 execution_id 只能引用本轮实际读取的记录；Pattern 只能引用下方快照。\n"
            "--- 提案历史（同作用域，只读） ---\n"
            + _proposal_history_text(db, workspace_id, domain) + "\n"
            "--- 授权训练摘要 ---\n" + summaries + "\n"
            "--- 当前技能集合（固定父版本） ---\n" + skills_text + "\n"
            "--- 经验索引（固定快照） ---\n" + patterns_text + "\n"
        )
        messages = [{"role": "user", "content": intro}]
        tracker = ReadTracker(root_path, allowed_ids)
        executor = ToolExecutor(db, snapshot, tracker)
        model_calls: list[dict] = []
        tool_calls = 0
        repairs = 0
        action_seen = False
        effective_runner = runner or SimulatedProposer(
            profile=profile, parent_version_id=parent_version_id)

        def fail(status, code, message):
            payload = {"record_proposal": False, "error_code": code,
                       "error_message": message,
                       "model_calls": model_calls}
            try:
                _save(db, run_id=run_id, key=key, ws=workspace_id, domain=domain,
                      dataset_version=dataset.dataset_version, status=status,
                      config=config, authorized=allowed_ids, tracker=tracker,
                      snapshot=snapshot, payload=payload)
            except Exception:  # noqa: BLE001
                db.rollback()
                raise
            return ProposerRunSummary(
                run_id=run_id, status=status,
                distinct_reads=len(tracker.read_distinct),
                model_calls=len(model_calls), tool_calls=tool_calls,
                error_code=code, error_message=message)

        def finalize(action):
            nonlocal action_seen
            if action_seen:
                return fail(PROPOSAL_OUTPUT_INVALID, "DUPLICATE_FINISH",
                            "重复 finish")
            action_seen = True
            if not isinstance(action, dict):
                return fail(PROPOSAL_OUTPUT_INVALID, "ACTION_SCHEMA",
                            "action 必须是对象")
            kind = action.get("type")
            if kind not in (PROPOSER_ACTION_CREATE, PROPOSER_ACTION_PATCH,
                            PROPOSER_ACTION_NO_ACTION):
                return fail(PROPOSAL_OUTPUT_INVALID, "ACTION_TYPE",
                            f"非法 action type: {kind!r}")
            reason = str(action.get("reason") or "").strip()
            evidence = [str(x) for x in (action.get("evidence_execution_ids") or [])]
            pattern_ids = [str(x) for x in (action.get("pattern_ids") or [])]
            pattern_rev_ids = [str(x) for x in (action.get("pattern_revision_ids") or [])]
            payload_base = {
                "record_proposal": True, "action": kind, "reason": reason,
                "evidence_execution_ids": evidence,
                "pattern_ids": pattern_ids,
                "pattern_revision_ids": pattern_rev_ids,
                "model_calls": model_calls,
            }
            try:
                if len(reason) > 2000:
                    raise ProposerInputError("reason 超长")
                extra_evidence = set(evidence) - tracker.read_distinct
                if extra_evidence:
                    raise ProposerInputError(
                        f"证据引用未实际读取: {sorted(extra_evidence)}")
                snapshot_patterns = snapshot.get("patterns") or {}
                extra_pattern = set(pattern_ids) - set(snapshot_patterns)
                if extra_pattern:
                    raise ProposerInputError(
                        f"Pattern 不在固定快照: {sorted(extra_pattern)}")
                unread_pattern = set(pattern_ids) - executor.read_pattern_ids()
                if unread_pattern:
                    raise ProposerInputError(
                        f"Pattern 未实际读取: {sorted(unread_pattern)}")
                for rev in pattern_rev_ids:
                    owners = [pid for pid, p in snapshot_patterns.items()
                              if p.get("current_revision_id") == rev]
                    if not owners or owners[0] not in pattern_ids:
                        raise ProposerInputError(
                            f"Pattern 修订 {rev} 不属于已引用快照模式")
                if kind == PROPOSER_ACTION_NO_ACTION:
                    payload = {**payload_base, "skill_id": "",
                               "parent_version_id": None,
                               "parent_content_hash": None, "ops": None,
                               "package": None}
                    proposal_id, cand = _save(
                        db, run_id=run_id, key=key, ws=workspace_id, domain=domain,
                        dataset_version=dataset.dataset_version,
                        status=PROPOSAL_NO_ACTION, config=config,
                        authorized=allowed_ids, tracker=tracker,
                        snapshot=snapshot, payload=payload)
                    return ProposerRunSummary(
                        run_id=run_id, status=PROPOSAL_NO_ACTION,
                        proposal_id=proposal_id,
                        distinct_reads=len(tracker.read_distinct),
                        model_calls=len(model_calls), tool_calls=tool_calls,
                        action=kind)
                # create / patch
                if len(tracker.read_distinct) < MIN_DISTINCT_READS:
                    payload = {**payload_base, "skill_id": "",
                               "parent_version_id": parent_version_id,
                               "parent_content_hash": None, "ops": None,
                               "package": None,
                               "error_code": "PREREQUISITES_INSUFFICIENT",
                               "error_message":
                                   f"可用不同训练轨迹 {len(tracker.read_distinct)} "
                                   f"< {MIN_DISTINCT_READS}"}
                    proposal_id, _ = _save(
                        db, run_id=run_id, key=key, ws=workspace_id, domain=domain,
                        dataset_version=dataset.dataset_version,
                        status=PROPOSAL_PREREQ_INSUFFICIENT, config=config,
                        authorized=allowed_ids, tracker=tracker,
                        snapshot=snapshot, payload=payload)
                    return ProposerRunSummary(
                        run_id=run_id, status=PROPOSAL_PREREQ_INSUFFICIENT,
                        proposal_id=proposal_id,
                        distinct_reads=len(tracker.read_distinct),
                        model_calls=len(model_calls), tool_calls=tool_calls,
                        action=kind, error_code="PREREQUISITES_INSUFFICIENT",
                        error_message="训练轨迹不足 4 条")
                if kind == PROPOSER_ACTION_CREATE:
                    skill_md = str(action.get("skill_md") or "")
                    purpose_md = str(action.get("purpose_md") or "")
                    if not skill_md or not purpose_md:
                        raise ProposerInputError("create 缺少 skill_md/purpose_md")
                    package = skill_store.build_package_from_texts(
                        skill_md, purpose_md, source_type="manual_seed",
                        origin=f"proposer:{run_id}")
                    if skill_store.list_versions(db, skill_id=package.skill_id):
                        raise ProposerInputError(
                            f"create: skill 身份已存在 {package.skill_id}")
                    payload = {**payload_base, "skill_id": package.skill_id,
                               "parent_version_id": None,
                               "parent_content_hash": None, "ops": None,
                               "package": package}
                    proposal_id, cand = _save(
                        db, run_id=run_id, key=key, ws=workspace_id, domain=domain,
                        dataset_version=dataset.dataset_version,
                        status=PROPOSAL_CANDIDATE_SAVED, config=config,
                        authorized=allowed_ids, tracker=tracker,
                        snapshot=snapshot, payload=payload)
                    return ProposerRunSummary(
                        run_id=run_id, status=PROPOSAL_CANDIDATE_SAVED,
                        proposal_id=proposal_id, candidate_version_id=cand,
                        action=kind, distinct_reads=len(tracker.read_distinct),
                        model_calls=len(model_calls), tool_calls=tool_calls)
                # patch
                skill_id = str(action.get("skill_id") or "")
                parent_vid = str(action.get("parent_version_id") or "")
                parent = skill_store.get_version(db, parent_vid)
                if parent.skill_id != skill_id:
                    raise ProposerInputError("patch skill_id 与父版本不一致")
                provided_hash = action.get("parent_content_hash")
                if provided_hash and provided_hash != parent.content_hash:
                    raise ProposerInputError("patch 父内容哈希不匹配")
                ops = action.get("ops")
                new_skill, new_purpose = patch_package(parent.skill_md,
                                                       parent.purpose_md, ops)
                package = skill_store.build_package_from_texts(
                    new_skill, new_purpose, source_type="manual_seed",
                    origin=f"proposer:{run_id}")
                if package.content_hash() == parent.content_hash:
                    raise ProposerInputError("patch 无内容变化（拒绝空补丁）")
                payload = {**payload_base, "skill_id": package.skill_id,
                           "parent_version_id": parent_vid,
                           "parent_content_hash": parent.content_hash,
                           "ops": ops, "package": package}
                proposal_id, cand = _save(
                    db, run_id=run_id, key=key, ws=workspace_id, domain=domain,
                    dataset_version=dataset.dataset_version,
                    status=PROPOSAL_CANDIDATE_SAVED, config=config,
                    authorized=allowed_ids, tracker=tracker,
                    snapshot=snapshot, payload=payload)
                return ProposerRunSummary(
                    run_id=run_id, status=PROPOSAL_CANDIDATE_SAVED,
                    proposal_id=proposal_id, candidate_version_id=cand,
                    action=kind, distinct_reads=len(tracker.read_distinct),
                    model_calls=len(model_calls), tool_calls=tool_calls)
            except ProposerInputError as exc:
                return fail(PROPOSAL_OUTPUT_INVALID, "ACTION_INVALID", str(exc))

        for _ in range(max_model_turns):
            if action_seen:
                break
            model_calls.append({"turn": len(model_calls) + 1,
                                "messages": [m for m in messages]})
            try:
                raw = effective_runner(messages, context=ROLE_CONTEXT,
                                       timeout=120.0)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, ProposerError) and "重复 finish" in str(exc):
                    return fail(PROPOSAL_OUTPUT_INVALID, "DUPLICATE_FINISH",
                                "重复 finish")
                return fail(PROPOSAL_FAILED, type(exc).__name__, str(exc))
            model_calls[-1]["response"] = _json_safe(raw)
            if not isinstance(raw, dict):
                repairs += 1
                if repairs > max_repairs:
                    return fail(PROPOSAL_OUTPUT_INVALID, "OUTPUT_INVALID",
                                "输出非 JSON 对象")
                messages.append({"role": "user",
                                 "content": "[系统] 输出必须是 JSON 对象。"})
                continue
            if "tool" in raw:
                if tool_calls >= max_tool_calls:
                    return fail(PROPOSAL_BUDGET_EXHAUSTED, "BUDGET_EXHAUSTED",
                                "工具调用预算耗尽")
                mark = None
                if budget_guard is not None:
                    try:
                        mark = budget_guard.reserve_tool()
                    except Exception as exc:  # noqa: BLE001
                        if type(exc).__name__ == "BudgetExceeded":
                            return fail(PROPOSAL_BUDGET_EXHAUSTED,
                                        "BUDGET_EXHAUSTED",
                                        "全局工具调用预算耗尽（执行前阻止）")
                        raise
                tool_calls += 1
                tool = str(raw.get("tool") or "")
                try:
                    result = executor.call(tool, raw.get("args") or {})
                    messages.append({"role": "user",
                                     "content": f"[tool:{tool}]\n{result}"})
                except Exception as exc:  # noqa: BLE001
                    if isinstance(exc, ProposerError):
                        messages.append({"role": "user",
                                         "content": f"[tool_error] {exc}"})
                    else:
                        if mark is not None:
                            budget_guard.finish_tool(mark, attempted=True)
                        raise
                if mark is not None:
                    budget_guard.finish_tool(mark, attempted=True)
                continue
            if "action" in raw:
                return finalize(raw.get("action"))
            repairs += 1
            if repairs > max_repairs:
                return fail(PROPOSAL_OUTPUT_INVALID, "OUTPUT_INVALID",
                            "输出缺少 tool/action")
            messages.append({"role": "user",
                             "content": "[系统] 输出必须含 tool 或 action。"})
        return fail(PROPOSAL_BUDGET_EXHAUSTED, "BUDGET_EXHAUSTED",
                    "模型轮次预算耗尽")
    except ProposerError as exc:
        db.rollback()
        payload = {"record_proposal": False, "error_code": type(exc).__name__,
                   "error_message": str(exc), "model_calls": []}
        _save(db, run_id=run_id, key=key, ws=workspace_id, domain=domain,
              dataset_version=dataset.dataset_version, status=PROPOSAL_FAILED,
              config=config, authorized=allowed_ids,
              tracker=(tracker if tracker is not None
                       else ReadTracker(root_path, allowed_ids)),
              snapshot=snapshot, payload=payload)
        return ProposerRunSummary(run_id=run_id, status=PROPOSAL_FAILED,
                                  error_code=type(exc).__name__,
                                  error_message=str(exc))
    finally:
        db.close()


def _json_safe(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    return {"_repr": str(raw)[:2000]}


def _find_existing(db: Session, key: str) -> ProposerRunSummary | None:
    row = (db.query(EvolutionProposalRun)
           .filter(EvolutionProposalRun.idempotency_key == key).first())
    if row is None or row.status not in (PROPOSAL_CANDIDATE_SAVED, PROPOSAL_NO_ACTION):
        return None
    p = (db.query(EvolutionProposal)
         .filter(EvolutionProposal.run_id == row.run_id).first())
    return ProposerRunSummary(
        run_id=row.run_id, status=row.status, idempotent_hit=True,
        proposal_id=(p.proposal_id if p else None),
        candidate_version_id=(p.candidate_version_id if p else None),
        action=(p.action if p else None),
        distinct_reads=len(json.loads(row.read_execution_ids_json or "[]")),
        model_calls=0, tool_calls=0)
