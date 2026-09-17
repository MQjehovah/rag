"""阶段 3 经验 Wiki 存储：Pattern/修订/维护运行/日志/索引（权威 = 实验控制库 DB）。

- 权威存储：root/skill_store.db（独立 evolution metadata，与技能版本同库）；
  Markdown 仅由导出生成一次性一致快照，不是另一套可写权威源。
- 经验按 workspace_id + domain 隔离；不进入业务 RAG/检索。
- 修订不可变：新证据 = 新修订（seq+1）；pattern.current_revision_id 指针切换。
- 状态机：observed（默认，单次观察）/ supported / contradicted，不自动提升为规律。
- 证据引用合法性：引用必须属于本次维护提供的 execution_ids 子集（越权/不存在拒绝），
  且作用域一致 —— 由 maintainer.apply_plan 在事务内校验后写入。
- 原子性：所有写入在同一事务内；任一步失败回滚（不产生部分模式/修订/日志/索引）。
- 幂等：idempotency_key 覆盖 scope/输入执行集合/基础经验修订/配置；重复 → 返回既有运行。
- 冲突：基础经验修订（index/patterns 头）在事务内复核，不一致 → MaintenanceConflictError。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.core.skill_evolution.contracts import canonical_json
from app.core.skill_evolution.errors import SkillStoreError
from app.models.evolution import (
    EvolutionIndex,
    EvolutionLog,
    EvolutionMaintenanceRun,
    EvolutionPattern,
    EvolutionPatternRevision,
    PATTERN_STATUS_CONTRADICTED,
    PATTERN_STATUS_OBSERVED,
    PATTERN_STATUS_SUPPORTED,
    RUN_STATUS_APPLIED,
    RUN_STATUS_CREATED,
    RUN_STATUS_FAILED,
    VALID_PATTERN_STATUSES,
)

DOMAIN_DEFAULT = "wiki_compile.default"

# 内容字段白名单（模式正文；证据单独列表）。
CONTENT_FIELDS = ("phenomenon", "cause_hypothesis", "suggestion", "applicability")
FIELD_LIMITS = {
    "phenomenon": 4000,
    "cause_hypothesis": 4000,
    "suggestion": 4000,
    "applicability": 2000,
}
TITLE_MAX = 160
MAX_LOG_TEXT = 4000
MAX_CREATE_PATTERNS = 8
MAX_UPDATE_PATTERNS = 8
MAX_LOG_ENTRIES = 20
MAX_EVIDENCE_IDS = 20

_ID_RE = re.compile(r"^[0-9a-z][a-z0-9_:-]{0,127}$")


class ExperienceStoreError(SkillStoreError):
    """经验存储/校验错误。"""


class MaintenanceConflictError(ExperienceStoreError):
    """基础经验修订已变化（并发/过期基础）→ 冲突，拒绝覆盖较新经验。"""


def scope_key(workspace_id: str, domain: str) -> str:
    return f"{workspace_id}|{domain}"


def split_scope(scope_key: str) -> tuple[str, str]:
    ws, _, dom = scope_key.partition("|")
    return ws, dom or ""


# ---------------------------------------------------------------------------
# 内容载荷
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternContent:
    """一条模式修订的结构化内容（与 revision.payload_json 对应）。"""

    phenomenon: str
    cause_hypothesis: str
    suggestion: str
    applicability: str = ""
    supporting_execution_ids: tuple[str, ...] = ()
    conflicting_execution_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "phenomenon": self.phenomenon,
            "cause_hypothesis": self.cause_hypothesis,
            "suggestion": self.suggestion,
            "applicability": self.applicability,
            "supporting_execution_ids": list(self.supporting_execution_ids),
            "conflicting_execution_ids": list(self.conflicting_execution_ids),
        }

    def payload_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


def validate_content_fields(data: dict, *, context: str) -> dict:
    """校验/规整模式正文字段（未知字段拒绝）。"""
    extra = set(data) - set(CONTENT_FIELDS)
    if extra:
        raise ExperienceStoreError(f"{context}: 未知字段 {sorted(extra)}")
    out = {}
    for f in CONTENT_FIELDS:
        v = data.get(f, "")
        if not isinstance(v, str):
            raise ExperienceStoreError(f"{context}: 字段 {f} 必须是字符串")
        v = v.strip()
        limit = FIELD_LIMITS[f]
        if len(v) > limit:
            raise ExperienceStoreError(f"{context}: 字段 {f} 超长（>{limit}）")
        out[f] = v
    if not out["phenomenon"]:
        raise ExperienceStoreError(f"{context}: 缺少 phenomenon（现象）")
    if not out["cause_hypothesis"]:
        raise ExperienceStoreError(f"{context}: 缺少 cause_hypothesis（原因假设）")
    if not out["suggestion"]:
        raise ExperienceStoreError(f"{context}: 缺少 suggestion（建议）")
    return out


def _clean_ids(values: Any, *, context: str, allowed: set[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ExperienceStoreError(f"{context}: 证据 id 必须是列表")
    ids: list[str] = []
    for v in values:
        if not isinstance(v, str) or not _ID_RE.match(v):
            raise ExperienceStoreError(f"{context}: 非法证据 id {v!r}")
        if v not in allowed:
            raise ExperienceStoreError(f"{context}: 证据 id 不在本轮授权集合: {v}")
        if v not in ids:
            ids.append(v)
    if len(ids) > MAX_EVIDENCE_IDS:
        raise ExperienceStoreError(f"{context}: 证据 id 数量超限（>{MAX_EVIDENCE_IDS}）")
    return tuple(ids)


# ---------------------------------------------------------------------------
# 读取（只读）
# ---------------------------------------------------------------------------


def _content_from_payload(payload_json: str) -> PatternContent:
    try:
        data = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ExperienceStoreError(f"修订 payload_json 损坏: {exc}") from exc
    return PatternContent(
        phenomenon=str(data.get("phenomenon") or ""),
        cause_hypothesis=str(data.get("cause_hypothesis") or ""),
        suggestion=str(data.get("suggestion") or ""),
        applicability=str(data.get("applicability") or ""),
        supporting_execution_ids=tuple(data.get("supporting_execution_ids") or ()),
        conflicting_execution_ids=tuple(data.get("conflicting_execution_ids") or ()),
    )


def list_patterns(db: Session, workspace_id: str, domain: str) -> list[dict]:
    rows = (
        db.query(EvolutionPattern)
        .filter(EvolutionPattern.workspace_id == workspace_id,
                EvolutionPattern.domain == domain)
        .order_by(EvolutionPattern.title)
        .all()
    )
    out = []
    for row in rows:
        revision = None
        if row.current_revision_id:
            rev = db.get(EvolutionPatternRevision, row.current_revision_id)
            if rev is not None:
                revision = {
                    "revision_id": rev.revision_id,
                    "seq": rev.seq,
                    "content": _content_from_payload(rev.payload_json).to_dict(),
                }
        out.append({
            "pattern_id": row.pattern_id,
            "title": row.title,
            "status": row.status,
            "current_revision_id": row.current_revision_id,
            "revision": revision,
        })
    return out


def get_pattern(db: Session, workspace_id: str, domain: str,
                pattern_id: str) -> dict:
    row = db.query(EvolutionPattern).filter(
        EvolutionPattern.pattern_id == pattern_id).first()
    if row is None or row.workspace_id != workspace_id or row.domain != domain:
        raise ExperienceStoreError(f"模式不存在或越权: {pattern_id}")
    rev = (db.get(EvolutionPatternRevision, row.current_revision_id)
           if row.current_revision_id else None)
    return {
        "pattern_id": row.pattern_id,
        "title": row.title,
        "status": row.status,
        "workspace_id": row.workspace_id,
        "domain": row.domain,
        "current_revision_id": row.current_revision_id,
        "current_content": (_content_from_payload(rev.payload_json).to_dict()
                            if rev else None),
    }


def list_revisions(db: Session, pattern_id: str) -> list[dict]:
    rows = (
        db.query(EvolutionPatternRevision)
        .filter(EvolutionPatternRevision.pattern_id == pattern_id)
        .order_by(EvolutionPatternRevision.seq.asc())
        .all()
    )
    return [
        {
            "revision_id": r.revision_id,
            "pattern_id": r.pattern_id,
            "seq": r.seq,
            "parent_revision_id": r.parent_revision_id,
            "run_id": r.run_id,
            "content": _content_from_payload(r.payload_json).to_dict(),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def list_logs(db: Session, workspace_id: str, domain: str) -> list[dict]:
    rows = (
        db.query(EvolutionLog)
        .filter(EvolutionLog.workspace_id == workspace_id,
                EvolutionLog.domain == domain)
        .order_by(EvolutionLog.seq.asc())
        .all()
    )
    return [
        {"log_id": r.log_id, "run_id": r.run_id, "seq": r.seq, "entry": r.entry}
        for r in rows
    ]


def get_index(db: Session, workspace_id: str, domain: str) -> dict | None:
    row = db.get(EvolutionIndex, scope_key(workspace_id, domain))
    if row is None:
        return None
    return {
        "content_hash": row.content_hash,
        "run_id": row.run_id,
        "index": json.loads(row.index_json),
    }


def get_run(db: Session, run_id: str) -> dict | None:
    row = db.get(EvolutionMaintenanceRun, run_id)
    if row is None:
        return None
    return {
        "run_id": row.run_id,
        "status": row.status,
        "workspace_id": row.workspace_id,
        "domain": row.domain,
        "idempotency_key": row.idempotency_key,
        "input_execution_ids": json.loads(row.input_execution_ids_json or "[]"),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
    }


def find_run_by_key(db: Session, idempotency_key: str) -> dict | None:
    row = (db.query(EvolutionMaintenanceRun)
           .filter(EvolutionMaintenanceRun.idempotency_key == idempotency_key)
           .first())
    return get_run(db, row.run_id) if row is not None else None


# ---------------------------------------------------------------------------
# 索引构造（与已应用修订一致；读当前 patterns 生成）
# ---------------------------------------------------------------------------


def build_index(db: Session, workspace_id: str, domain: str) -> tuple[list[dict], str]:
    entries = []
    for p in list_patterns(db, workspace_id, domain):
        rev = p.get("revision")
        content = (rev or {}).get("content") or {}
        entries.append({
            "pattern_id": p["pattern_id"],
            "title": p["title"],
            "status": p["status"],
            "problem": (content.get("phenomenon") or "")[:200],
            "cause": (content.get("cause_hypothesis") or "")[:200],
            "suggestion": (content.get("suggestion") or "")[:200],
        })
    entries.sort(key=lambda e: (e["title"], e["pattern_id"]))
    index_json = json.dumps(entries, ensure_ascii=False, indent=2)
    content_hash = hashlib.sha256(index_json.encode("utf-8")).hexdigest()
    return entries, content_hash


# ---------------------------------------------------------------------------
# 原子应用（同一事务；全成或全不成）
# ---------------------------------------------------------------------------


def _next_revision_seq(db: Session, pattern_id: str) -> int:
    last = (
        db.query(EvolutionPatternRevision.seq)
        .filter(EvolutionPatternRevision.pattern_id == pattern_id)
        .order_by(EvolutionPatternRevision.seq.desc())
        .first()
    )
    return (last[0] + 1) if last else 1


def _merge_evidence(old: PatternContent, add_support: tuple[str, ...],
                    add_conflict: tuple[str, ...]) -> PatternContent:
    support = list(old.supporting_execution_ids)
    for eid in add_support:
        if eid not in support:
            support.append(eid)
    conflict = list(old.conflicting_execution_ids)
    for eid in add_conflict:
        if eid not in conflict:
            conflict.append(eid)
    return PatternContent(
        phenomenon=old.phenomenon,
        cause_hypothesis=old.cause_hypothesis,
        suggestion=old.suggestion,
        applicability=old.applicability,
        supporting_execution_ids=tuple(support),
        conflicting_execution_ids=tuple(conflict),
    )


def apply_plan(
    db: Session,
    *,
    run_id: str,
    idempotency_key: str,
    workspace_id: str,
    domain: str,
    config_json: str,
    input_execution_ids: list[str],
    allowed_execution_ids: set[str],
    base_state: dict | None,
    model_calls_json: str | None,
    create_patterns: list[dict],
    update_patterns: list[dict],
    log_entries: list[str],
    created_by: str | None = None,
) -> dict:
    """在一个事务内应用维护结果；任何非法输入抛错（调用方回滚，零部分写入）。

    base_state：{index_hash?, patterns:{pid: rev_id}} 期望基础；现库不一致 → 冲突。
    """
    import uuid as _uuid
    if len(create_patterns) > MAX_CREATE_PATTERNS:
        raise ExperienceStoreError("create_patterns 数量超限")
    if len(update_patterns) > MAX_UPDATE_PATTERNS:
        raise ExperienceStoreError("update_patterns 数量超限")
    if len(log_entries) > MAX_LOG_ENTRIES:
        raise ExperienceStoreError("append_log 数量超限")

    existing = find_run_by_key(db, idempotency_key)
    if existing is not None:
        return {"idempotent_hit": True, **existing}

    # ---- 基础状态一致性（并发/过期基础）----
    if base_state:
        index_now = get_index(db, workspace_id, domain)
        if (base_state.get("index_hash") or None) != (
                index_now or {}).get("content_hash"):
            raise MaintenanceConflictError(
                "经验索引基础版本已变化（index_hash 不一致）")
        heads = {
            p["pattern_id"]: p["current_revision_id"]
            for p in list_patterns(db, workspace_id, domain)
        }
        expected_heads = base_state.get("patterns") or {}
        for pid, rev_id in expected_heads.items():
            if heads.get(pid) != rev_id:
                raise MaintenanceConflictError(
                    f"模式 {pid} 基础修订已变化（{heads.get(pid)} != {rev_id}）")

    # ---- 模型运行行（先落：修订引用 run_id；最终状态在提交前更新）----
    run_row = EvolutionMaintenanceRun(
        run_id=run_id, idempotency_key=idempotency_key,
        workspace_id=workspace_id, domain=domain,
        status=RUN_STATUS_CREATED,
        config_json=config_json,
        input_execution_ids_json=json.dumps(input_execution_ids, ensure_ascii=False),
        model_calls_json=model_calls_json,
    )
    db.add(run_row)

    applied_patterns: list[str] = []
    applied_updates: list[str] = []
    for item in create_patterns:
        title = str(item.get("title") or "").strip()
        if not title or len(title) > TITLE_MAX:
            raise ExperienceStoreError(f"create: title 非法/超长: {title[:50]!r}")
        fields = validate_content_fields(
            {k: item.get(k, "") for k in CONTENT_FIELDS}, context="create")
        support = _clean_ids(item.get("supporting_execution_ids") or [],
                             context=f"create {title}",
                             allowed=allowed_execution_ids)
        conflict = _clean_ids(item.get("conflicting_execution_ids") or [],
                              context=f"create {title}",
                              allowed=allowed_execution_ids)
        if support and conflict and set(support) & set(conflict):
            raise ExperienceStoreError(f"create {title}: 同一执行不能既支持又冲突")
        pattern_id = str(item.get("pattern_id") or "") or None
        if pattern_id:
            if not _ID_RE.match(pattern_id) or "/" in pattern_id or "\\" in pattern_id:
                raise ExperienceStoreError(f"create: 非法 pattern_id {pattern_id!r}")
            if db.get(EvolutionPattern, pattern_id) is not None:
                raise ExperienceStoreError(f"create: pattern_id 已存在 {pattern_id}")
        else:
            pattern_id = "pat_" + hashlib.sha256(
                f"{workspace_id}|{title}".encode("utf-8")).hexdigest()[:16]
        content = PatternContent(
            phenomenon=fields["phenomenon"],
            cause_hypothesis=fields["cause_hypothesis"],
            suggestion=fields["suggestion"],
            applicability=fields.get("applicability") or "",
            supporting_execution_ids=support,
            conflicting_execution_ids=conflict,
        )
        revision_id = str(_uuid.uuid4())
        status = _status_from_evidence(support, conflict)
        db.add(EvolutionPattern(
            pattern_id=pattern_id, workspace_id=workspace_id, domain=domain,
            title=title, status=status, current_revision_id=None,
        ))
        rev = EvolutionPatternRevision(
            revision_id=revision_id, pattern_id=pattern_id, seq=1,
            parent_revision_id=None, run_id=run_id,
            payload_json=json.dumps(content.to_dict(), ensure_ascii=False),
            payload_hash=content.payload_hash(),
        )
        db.add(rev)
        row = db.get(EvolutionPattern, pattern_id)
        row.current_revision_id = revision_id
        applied_patterns.append(pattern_id)

    for item in update_patterns:
        pattern_id = str(item.get("pattern_id") or "").strip()
        if not pattern_id:
            raise ExperienceStoreError("update: 缺少 pattern_id")
        pattern = db.query(EvolutionPattern).filter(
            EvolutionPattern.pattern_id == pattern_id).first()
        if pattern is None or pattern.workspace_id != workspace_id \
                or pattern.domain != domain:
            raise ExperienceStoreError(f"update: 模式不存在或越权 {pattern_id}")
        base_rev = str(item.get("base_revision_id") or "").strip()
        if not base_rev or pattern.current_revision_id != base_rev:
            raise MaintenanceConflictError(
                f"update {pattern_id}: 基础修订失配 "
                f"(期望 {pattern.current_revision_id}，补丁基于 {base_rev or 'none'})")
        parent = db.get(EvolutionPatternRevision, base_rev)
        old = _content_from_payload(parent.payload_json)
        support = _clean_ids(item.get("append_support_execution_ids") or [],
                             context=f"update {pattern_id}",
                             allowed=allowed_execution_ids)
        conflict = _clean_ids(item.get("append_conflict_execution_ids") or [],
                              context=f"update {pattern_id}",
                              allowed=allowed_execution_ids)
        if support and conflict and set(support) & set(conflict):
            raise ExperienceStoreError(
                f"update {pattern_id}: 同一执行不能既追加支持又追加冲突")
        revise = item.get("revise_fields") or {}
        if not isinstance(revise, dict):
            raise ExperienceStoreError(f"update {pattern_id}: revise_fields 必须是对象")
        extra = set(revise) - set(CONTENT_FIELDS)
        if extra:
            raise ExperienceStoreError(
                f"update {pattern_id}: revise_fields 未知字段 {sorted(extra)}")
        merged = _merge_evidence(old, support, conflict)
        base_dict = merged.to_dict()
        for key, val in revise.items():
            if not isinstance(val, str):
                raise ExperienceStoreError(f"update {pattern_id}: {key} 非字符串")
            val = val.strip()
            if len(val) > FIELD_LIMITS[key]:
                raise ExperienceStoreError(
                    f"update {pattern_id}: {key} 超长")
            base_dict[key] = val
        if not base_dict["suggestion"]:
            raise ExperienceStoreError(f"update {pattern_id}: suggestion 为空")
        content = PatternContent(
            phenomenon=base_dict["phenomenon"],
            cause_hypothesis=base_dict["cause_hypothesis"],
            suggestion=base_dict["suggestion"],
            applicability=base_dict.get("applicability") or "",
            supporting_execution_ids=tuple(base_dict["supporting_execution_ids"]),
            conflicting_execution_ids=tuple(base_dict["conflicting_execution_ids"]),
        )
        rev_seq = _next_revision_seq(db, pattern_id)
        revision_id = str(_uuid.uuid4())
        db.add(EvolutionPatternRevision(
            revision_id=revision_id, pattern_id=pattern_id, seq=rev_seq,
            parent_revision_id=base_rev, run_id=run_id,
            payload_json=json.dumps(content.to_dict(), ensure_ascii=False),
            payload_hash=content.payload_hash(),
        ))
        pattern.current_revision_id = revision_id
        pattern.status = _status_from_evidence(
            content.supporting_execution_ids, content.conflicting_execution_ids)
        applied_updates.append(pattern_id)

    # ---- 日志（追加；不重复：幂等键已拦重复运行）----
    existing_log_seq = (
        db.query(EvolutionLog.seq)
        .filter(EvolutionLog.run_id == run_id)
        .order_by(EvolutionLog.seq.desc()).first()
    )
    next_seq = (existing_log_seq[0] + 1) if existing_log_seq else 1
    for entry in log_entries:
        if not isinstance(entry, str) or not entry.strip():
            raise ExperienceStoreError("append_log 含空条目")
        if len(entry) > MAX_LOG_TEXT:
            raise ExperienceStoreError(f"append_log 条目超长（>{MAX_LOG_TEXT}）")
        db.add(EvolutionLog(
            log_id=str(_uuid.uuid4()), run_id=run_id,
            workspace_id=workspace_id, domain=domain, seq=next_seq, entry=entry))
        next_seq += 1

    # ---- 索引（与已应用修订一致重建）----
    entries, index_hash = build_index(db, workspace_id, domain)
    row = db.get(EvolutionIndex, scope_key(workspace_id, domain))
    if row is None:
        db.add(EvolutionIndex(
            scope_key=scope_key(workspace_id, domain),
            index_json=json.dumps(entries, ensure_ascii=False, indent=2),
            content_hash=index_hash, run_id=run_id,
        ))
    else:
        row.index_json = json.dumps(entries, ensure_ascii=False, indent=2)
        row.content_hash = index_hash
        row.run_id = run_id
        row.updated_at = datetime.now()

    # ---- 收尾：run 状态 applied（同事务提交）----
    run_row.status = RUN_STATUS_APPLIED
    run_row.applied_at = datetime.now()
    db.commit()
    return {
        "idempotent_hit": False,
        "run_id": run_id,
        "created_patterns": applied_patterns,
        "updated_patterns": applied_updates,
        "index_hash": index_hash,
    }


def _status_from_evidence(support: tuple[str, ...],
                          conflict: tuple[str, ...]) -> str:
    if conflict:
        return PATTERN_STATUS_CONTRADICTED
    if support:
        return PATTERN_STATUS_SUPPORTED
    return PATTERN_STATUS_OBSERVED


def record_failed_run(db: Session, *, run_id: str, idempotency_key: str,
                      workspace_id: str, domain: str, config_json: str,
                      input_execution_ids: list[str], error_code: str,
                      error_message: str) -> None:
    """非法输出/超时等失败诊断：只写 failed 运行行，不污染经验 Wiki。"""
    if find_run_by_key(db, idempotency_key) is not None:
        return  # 既有运行（applied/failed）不重复写
    db.add(EvolutionMaintenanceRun(
        run_id=run_id, idempotency_key=idempotency_key,
        workspace_id=workspace_id, domain=domain, status=RUN_STATUS_FAILED,
        config_json=config_json,
        input_execution_ids_json=json.dumps(input_execution_ids, ensure_ascii=False),
        error_code=error_code, error_message=(error_message or "")[:4000],
    ))
    db.commit()


# ---------------------------------------------------------------------------
# 导出（单次一致快照；Markdown 非权威源）
# ---------------------------------------------------------------------------


def export_scope(db: Session, workspace_id: str, domain: str) -> dict:
    """一次性一致读取导出快照（index/logs/patterns+修订）。"""
    from datetime import datetime as _dt
    index = get_index(db, workspace_id, domain)
    logs = list_logs(db, workspace_id, domain)
    patterns = []
    for p in list_patterns(db, workspace_id, domain):
        patterns.append({
            "pattern_id": p["pattern_id"],
            "title": p["title"],
            "status": p["status"],
            "revisions": list_revisions(db, p["pattern_id"]),
        })
    patterns.sort(key=lambda x: x["pattern_id"])
    return {
        "scope_key": scope_key(workspace_id, domain),
        "workspace_id": workspace_id,
        "domain": domain,
        "exported_at": _dt.now().isoformat(timespec="seconds"),
        "index": (index.get("index") if index else []),
        "logs": logs,
        "patterns": patterns,
    }


def render_markdown(export: dict) -> dict[str, str]:
    """导出为 index.md / logs.md / patterns/<id>.md 的文本映射。"""
    lines_index = [
        "# 经验索引",
        "",
        f"- scope: `{export['workspace_id']}` / `{export['domain']}`",
        f"- exported_at: {export['exported_at']}",
        f"- patterns: {len(export['patterns'])}",
        "",
        "| pattern | status | 问题 | 原因假设 | 建议 |",
        "|---|---|---|---|---|",
    ]
    for p in export["patterns"]:
        cur = p["revisions"][-1]["content"] if p["revisions"] else {}
        lines_index.append(
            f"| {p['pattern_id']} | {p['status']} | "
            f"{str(cur.get('phenomenon') or '')[:60]} | "
            f"{str(cur.get('cause_hypothesis') or '')[:60]} | "
            f"{str(cur.get('suggestion') or '')[:60]} |")
    lines_index.append("")
    lines_index.append("> 索引与已应用模式修订一致（同一事务维护）。")

    lines_logs = ["# 经验日志", ""]
    for lg in export["logs"]:
        lines_logs.append(f"## {lg['seq']}. {lg['run_id']}")
        lines_logs.append(lg["entry"])
        lines_logs.append("")

    pattern_files = {}
    for p in export["patterns"]:
        lines = [
            f"# {p['title']}",
            "",
            f"- pattern_id: `{p['pattern_id']}`",
            f"- status: {p['status']}（observed/supported/contradicted；单次观察不自动提升）",
            f"- revisions: {len(p['revisions'])}",
            "",
        ]
        for r in p["revisions"]:
            c = r["content"]
            lines += [
                f"## 修订 {r['seq']}（{r['revision_id']}）",
                f"- 来源维护运行: {r['run_id'] or '-'}",
                f"- 现象: {c.get('phenomenon') or ''}",
                f"- 原因假设: {c.get('cause_hypothesis') or ''}",
                f"- 建议: {c.get('suggestion') or ''}",
                f"- 适用范围: {c.get('applicability') or ''}",
                f"- 支持证据: {', '.join(c.get('supporting_execution_ids') or []) or '-'}",
                f"- 冲突证据: {', '.join(c.get('conflicting_execution_ids') or []) or '-'}",
                "",
            ]
        pattern_files[f"patterns/{p['pattern_id']}.md"] = "\n".join(lines)
    return {
        "index.md": "\n".join(lines_index),
        "logs.md": "\n".join(lines_logs),
        **pattern_files,
    }
