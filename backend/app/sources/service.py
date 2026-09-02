"""SourceSyncService：幂等 + 状态机 + 删除/恢复/ACL（P9-BE-04/07，V3 计划 4.7）。

幂等规则（V3 4.7）映射：
- 首次发现 → 创建 Page/Evidence/Card（调用方注入 ingest 钩子）
- 内容哈希未变 → 只更新检查时间（不重算解析/Embedding/LLM）
- 仅 ACL/标题变化 → 更新元数据 + ACL（不重算正文向量）
- 正文变化 → 更新 Page + 旧 Evidence stale + 新 Evidence + Card Proposal
- 源端删除 → SourceItem deleted + Page 归档 + Evidence stale
- 删除后恢复 → active + 按当前版本重跑

本模块只做 SourceItem 层状态机 + 哈希判定，Page/Evidence/Card 的实际写入
通过注入的 ingest 回调完成（P9-BE-06 接通 sync_page_evidence/compile）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import SourceItem, SourceSyncError, SourceSyncRun
from app.sources.schemas import NormalizedSourceItem

logger = logging.getLogger(__name__)

# SourceItem 状态机合法转换
_STATE_TRANSITIONS = {
    "active": {"deleted", "error", "skipped"},
    "deleted": {"active", "error"},
    "skipped": {"active", "deleted", "error"},
    "error": {"active", "deleted", "skipped"},
}


def compute_content_hash(item: NormalizedSourceItem) -> str:
    """标准化正文哈希（V3 4.4：根据实际选中的 text/bytes 载荷计算）。

    Phase 2.1：优先 content_bytes；否则 content 编码 UTF-8。必须与实际载荷一致。
    """
    return hashlib.sha256(item.effective_content).hexdigest()


def compute_metadata_hash(item: NormalizedSourceItem) -> str:
    """标题/路径/版本等元数据哈希（不包含正文）。"""
    raw = json.dumps({
        "title": item.title,
        "source_path": item.source_path,
        "external_version": item.external_version,
        "acl_scope": item.acl_scope,
        "content_type": item.content_type,
    }, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _valid_transition(current: str, target: str) -> bool:
    return target in _STATE_TRANSITIONS.get(current or "active", set())


@dataclass
class SyncDecision:
    """对单个 SourceItem 的同步决策。"""
    action: str  # create/unchanged/metadata_only/content_changed/delete/restore/skip
    reason: str = ""


def decide(item: NormalizedSourceItem, existing: SourceItem | None) -> SyncDecision:
    """根据文档 4.7 幂等表决定动作。纯函数，不碰 DB。

    J-1 最终返工：FOLDER_NOT_MAPPED 的 skipped 条目补上映射后必须能恢复导入
    （进入 create/restore），不得因 skipped 永久无法恢复；NEEDS_REASSIGN 的
    skipped 保持 skip（等管理员明确确认，不自动扩大权限）。
    """
    from app.core.folder_mapping import (
        NEEDS_REASSIGN_REASON,
        SAFE_REASON_UNMAPPED,
    )

    content_hash = compute_content_hash(item)
    metadata_hash = compute_metadata_hash(item)

    if item.deleted:
        if existing is None or existing.state == "deleted":
            return SyncDecision("skip", "无此条目或已删除")
        return SyncDecision("delete", "源端删除")

    if existing is None:
        return SyncDecision("create", "首次发现")

    if existing.state == "deleted":
        return SyncDecision("restore", "删除后恢复")

    if existing.state != "active":
        # 仅「未映射」的 skipped 可恢复；其余（含 NEEDS_REASSIGN）保持 skip。
        if existing.state == "skipped" and existing.last_error == SAFE_REASON_UNMAPPED:
            return SyncDecision("restore", "未映射条目补上映射后恢复")
        if existing.state == "skipped" and existing.last_error == NEEDS_REASSIGN_REASON:
            return SyncDecision("skip", "待管理员确认重新归属")
        return SyncDecision("skip", f"条目状态 {existing.state} 非 active")

    # 内容哈希未变
    if existing.content_hash == content_hash:
        if existing.metadata_hash == metadata_hash:
            return SyncDecision("unchanged", "内容与元数据均未变")
        return SyncDecision("metadata_only", "仅 ACL/标题/路径变化")

    return SyncDecision("content_changed", "正文变化")


def apply_item(
    db: Session,
    item: NormalizedSourceItem,
    ingest=None,
) -> SourceItem:
    """把单个 SourceItem 应用到 SourceItem 层（含状态机），返回 SourceItem。

    ingest: 可选回调 (db, SourceItem, NormalizedSourceItem, decision) 用于
    实际写入 Page/Evidence/Card。本函数只负责 SourceItem 层状态 + 哈希。
    """
    existing = (
        db.query(SourceItem)
        .filter(
            SourceItem.connection_id == item.connection_id,
            SourceItem.external_id == item.external_id,
        )
        .first()
    )
    decision = decide(item, existing)

    if existing is None:
        existing = SourceItem(
            connection_id=item.connection_id,
            external_id=item.external_id,
            state="active",
        )
        db.add(existing)

    # 状态机 + 字段更新
    target_state = {
        "create": "active",
        "restore": "active",
        "unchanged": existing.state,
        "metadata_only": existing.state,
        "content_changed": existing.state,
        "delete": "deleted",
        "skip": existing.state,
    }[decision.action]

    if decision.action == "delete":
        existing.state = "deleted"
    elif decision.action in ("create", "restore"):
        existing.state = "active"
        _fill_item(existing, item, decision)
    elif decision.action == "content_changed":
        _fill_item(existing, item, decision)
    elif decision.action == "metadata_only":
        # 只更新元数据与 ACL，不重算正文
        existing.metadata_hash = compute_metadata_hash(item)
        existing.external_version = item.external_version
        existing.source_url = item.source_url
        existing.source_path = item.source_path
        existing.acl_json = json.dumps(item.acl_scope, ensure_ascii=False)
        existing.source_updated_at = _parse_dt(item.source_updated_at)
    # unchanged/skip 不更新字段

    existing.last_synced_at = datetime.now()

    # 委托实际 Page/Evidence/Card 写入
    if ingest is not None and decision.action in ("create", "restore", "content_changed"):
        ingest(db, existing, item, decision)

    return existing


def _fill_item(existing: SourceItem, item: NormalizedSourceItem, decision: SyncDecision) -> None:
    existing.external_version = item.external_version
    existing.content_hash = compute_content_hash(item)
    existing.metadata_hash = compute_metadata_hash(item)
    existing.source_url = item.source_url
    existing.source_path = item.source_path
    existing.acl_json = json.dumps(item.acl_scope, ensure_ascii=False)
    existing.source_updated_at = _parse_dt(item.source_updated_at)
    existing.last_error = None
    existing.retry_count = 0


def _parse_dt(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def record_error(
    db: Session,
    run_id: str,
    external_id: str,
    stage: str,
    error_code: str,
    error_message: str,
    retryable: bool = True,
) -> None:
    """记录同步错误（P9-BE-08 结构化日志的数据载体）。"""
    db.add(SourceSyncError(
        run_id=run_id,
        external_id=external_id,
        stage=stage,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
        retry_count=0,
    ))
