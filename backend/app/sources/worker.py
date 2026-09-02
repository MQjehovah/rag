"""数据库 Worker（P9-BE-05，V3 计划 4.6）。

第一阶段用「数据库任务表 + 单独 Worker」，不引入消息队列：
- 原子抢占 queued → running
- 心跳 heartbeat_at
- 重启后超时 running → 重新入队
- 取消只设 cancel_requested，Worker 在安全检查点停止
- 每个 SourceItem 独立短事务

纯函数 + DB，不依赖 asyncio 任务系统。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.feature_flags import feature_enabled
from app.models.database import SourceConnection, SourceSyncRun

logger = logging.getLogger(__name__)

# 心跳超时阈值：超过则视为僵死，重新入队
HEARTBEAT_TIMEOUT_SECONDS = 300


def claim_next_run(db: Session) -> SourceSyncRun | None:
    """原子抢占下一个 queued 任务（SQLite 单写者下用短事务 + 状态检查）。"""
    # P14 回滚的硬约束：总开关关闭后 Worker 不再抢任何新任务。
    if not feature_enabled(db, "source_hub_enabled"):
        return None

    candidates = (
        db.query(SourceSyncRun, SourceConnection)
        .join(SourceConnection, SourceConnection.id == SourceSyncRun.connection_id)
        .filter(SourceSyncRun.status == "queued", SourceConnection.enabled.is_(True))
        .order_by(SourceSyncRun.started_at, SourceSyncRun.id)
        .all()
    )
    connector_flags = {
        "dingtalk": "dingtalk_connector_enabled",
        "gitlab": "gitlab_connector_enabled",
    }
    run = None
    for candidate, connection in candidates:
        flag = connector_flags.get(connection.connector_key)
        if flag is None or feature_enabled(db, flag):
            run = candidate
            break
    if run is None:
        return None
    run.status = "running"
    run.started_at = datetime.now()
    run.heartbeat_at = datetime.now()
    db.commit()
    return run


def heartbeat(db: Session, run_id: str) -> None:
    """更新心跳时间。"""
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    if run is None:
        return
    run.heartbeat_at = datetime.now()
    db.commit()


def requeue_stale_runs(db: Session, timeout_seconds: int = HEARTBEAT_TIMEOUT_SECONDS) -> int:
    """重启后：将超时未心跳的 running 任务重新入队。返回重新入队数量。"""
    cutoff = datetime.now() - timedelta(seconds=timeout_seconds)
    stale = (
        db.query(SourceSyncRun)
        .filter(
            SourceSyncRun.status == "running",
            SourceSyncRun.heartbeat_at < cutoff,
        )
        .all()
    )
    for run in stale:
        run.status = "queued"
    if stale:
        db.commit()
    return len(stale)


def request_cancel(db: Session, run_id: str) -> bool:
    """请求取消（只设 cancel_requested，Worker 在安全检查点停止）。"""
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    if run is None:
        return False
    run.cancel_requested = True
    if run.status == "queued":
        run.status = "cancelled"
        run.finished_at = datetime.now()
    db.commit()
    return True


def finish_run(db: Session, run_id: str, status: str, error_summary: str = "") -> None:
    """结束任务。status ∈ succeeded/failed/cancelled。"""
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    if run is None:
        return
    run.status = status
    run.finished_at = datetime.now()
    if error_summary:
        run.error_summary = error_summary
    db.commit()


def should_stop(db: Session, run_id: str) -> bool:
    """安全检查点：显式取消或 P14 总开关关闭时停止。"""
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    return bool(
        run is not None
        and (run.cancel_requested or not feature_enabled(db, "source_hub_enabled"))
    )
