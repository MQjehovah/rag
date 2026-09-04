"""Phase 4.1：KnowledgeCompileRun Worker（CAS claim/lease/心跳/恢复/进程内泵线程）。

与现有 wiki_refresh_scheduler 对齐的思路：
- DB queued 行 = 持久队列（进程重启不丢；无需消息队列组件）。
- claim_next_run：单条条件 UPDATE（原子 CAS）queued → running，attempt 原子 +1，
  写 lease_token/worker_id/lease_expires_at/started_at/heartbeat_at；
  rowcount==0 → 无有效候选 → 顺手归一 attempt 已满的 queued → failed(retry_exhausted)。
- heartbeat(db, run_id, lease_token, worker_id)：UPDATE 校验 running + lease_token
  匹配才续租；rowcount==0 → lease 已失效（返回 False）。
- LeaseRenewer：独立 session 周期性续租（stage 长执行期间不被 recovery 重排）。
- requeue_stale_runs：lease 过期 running → 闭合 stage（running→failed worker_lost、
  queued→skipped）后：attempt<max → queued（不加 attempt）；attempt>=max →
  failed(retry_exhausted)；cancel_requested → 级联 cancelled。
- start_worker/stop_worker：进程内 ThreadPoolExecutor 泵线程（main.py
  startup/shutdown 调用；不引入 Celery/Kafka/Temporal）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.wiki_pipeline.executor import (
    _close_unfinished_stages,
    _lease_seconds,  # Phase 9B：claim/heartbeat 到期长度同源（读同一 settings 字段）
    claim_by_id,
    execute_run,
)
from app.core.wiki_pipeline.registry import stage_error_message
from app.core.wiki_pipeline.state_machine import apply_run_status, apply_stage_status
from app.models.database import (
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
)

logger = logging.getLogger(__name__)

_MAX_CONCURRENT = 1  # 进程内并发拉取上限（信号量）；DB queued 行才是持久队列


def _renew_interval_default() -> float:
    """LeaseRenewer 未显式传 interval 时的默认续租间隔（读 settings，默认 30.0）。"""
    from app.config import settings

    return float(settings.wiki_pipeline_lease_renew_interval_seconds)


def _poll_interval() -> float:
    """worker 泵空队列轮询间隔（读 settings，默认 2.0s）。"""
    from app.config import settings

    return float(settings.wiki_pipeline_poll_interval_seconds)


def _heartbeat_timeout_default() -> int:
    """requeue_stale_runs 未显式传 timeout 时的 stale 判定窗口。

    说明：stale 判定是 OR（heartbeat 超时 或 lease_expires_at 已过期）。短 lease
    场景由 lease_expires_at 分支即可触发，heartbeat 窗口读同一 heartbeat 字段
    （默认 300）保持可注入且不弱化原有语义。
    """
    from app.config import settings

    return int(settings.wiki_pipeline_heartbeat_timeout_seconds)

_worker_executor: ThreadPoolExecutor | None = None
_worker_lock = threading.Lock()
_stop_event = threading.Event()
_semaphore: threading.BoundedSemaphore | None = None


def _now() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------------------
# CAS claim / heartbeat
# ---------------------------------------------------------------------------


def claim_next_run(db: Session, worker_id: str | None = None) -> CompileRun | None:
    """原子抢占下一个 queued run（复用 executor.claim_by_id 唯一 CAS 实现）。

    流程：FIFO 候选（queued & !cancel & attempt<max ORDER BY created_at,id LIMIT 1）
    → executor.claim_by_id(db, candidate_id, worker_id)。候选被并发抢占/exhausted
    时继续取下一候选（FIFO 不因抢失败而丢队）；无候选 → 归一 attempt 已满的
    queued 残留为 failed(retry_exhausted)（安全网）→ None。

    attempt 语义与 executor.execute_run 完全一致：claim 原子 +1。
    """
    wid = worker_id or f"w:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    while True:
        candidate_id = db.execute(
            select(CompileRun.id)
            .where(
                CompileRun.status == "queued",
                CompileRun.cancel_requested.is_not(True),
                CompileRun.attempt < CompileRun.max_attempts,
            )
            .order_by(CompileRun.created_at, CompileRun.id)
            .limit(1)
        ).scalar_one_or_none()
        if candidate_id is None:
            # 无有效候选：归一 attempt 已满的 queued 残留（安全网，幂等）。
            now = _now()
            db.execute(
                update(CompileRun)
                .where(
                    CompileRun.status == "queued",
                    CompileRun.cancel_requested.is_not(True),
                    CompileRun.attempt >= CompileRun.max_attempts,
                )
                .values(
                    status="failed",
                    safe_error_code="retry_exhausted",
                    safe_error_message=stage_error_message("RETRY_EXHAUSTED"),
                    error_summary=stage_error_message("RETRY_EXHAUSTED"),
                    finished_at=now,
                    heartbeat_at=now,
                ),
                execution_options={"synchronize_session": False},
            )
            db.commit()
            return None
        _, claimed = claim_by_id(db, candidate_id, wid)
        if claimed is not None:
            return claimed
        # 候选被抢占 / 已 exhausted 归一：继续取下一个 FIFO 候选。


def heartbeat(
    db: Session,
    run_id: str,
    lease_token: str | None = None,
    worker_id: str | None = None,
) -> bool:
    """续租：精确匹配 run_id + running + lease_token + worker_id，且 lease 未过期。

    WHERE 附加条件（禁止复活过期/空 lease）：
    - lease_token IS NOT NULL 且 == :tok；
    - worker_id IS NOT NULL 且 == :wid；
    - lease_expires_at IS NOT NULL 且 > now。
    已过期 / 空 lease → rowcount 0 → 返回 False（不得更新 heartbeat_at/lease_expires_at）。

    返回 False = lease 已失效（run 被恢复/cancel/易主/租约已过期），调用方应停止执行。
    供独立续租线程使用独立 session 调用。
    """
    now = _now()
    expires = now + timedelta(seconds=_lease_seconds())
    res = db.execute(
        update(CompileRun)
        .where(
            CompileRun.id == run_id,
            CompileRun.status == "running",
            CompileRun.lease_token.is_not(None),
            CompileRun.lease_token == lease_token,
            CompileRun.worker_id.is_not(None),
            CompileRun.worker_id == worker_id,
            CompileRun.lease_expires_at.is_not(None),
            CompileRun.lease_expires_at > now,
        )
        .values(heartbeat_at=now, lease_expires_at=expires),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return res.rowcount == 1


class LeaseRenewer:
    """独立 session 周期性续租 daemon：长 stage 执行期间不被 recovery 重排。

    start(run) / stop()。heartbeat 返回 False（lease 易主）→ 停止续租并标记
    lost（让 lease 自然过期由 recovery 收敛）；网络抖动/锁竞争仅警告不判 lost。
    """

    def __init__(
        self,
        engine,
        run_id: str,
        lease_token: str | None,
        worker_id: str | None,
        lease_interval: float | None = None,
    ):
        self._engine = engine
        self._run_id = run_id
        self._lease_token = lease_token
        self._worker_id = worker_id
        # Phase 9B：未显式传 interval → 读 settings renew 字段（默认 30s）。
        self._interval = lease_interval or _renew_interval_default()
        self._session_factory = sessionmaker(bind=engine)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="wiki-compile-lease-renew",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def _run(self) -> None:
        consecutive = 0
        while not self._stop.wait(self._interval):
            try:
                ok = self._heartbeat_once()
            except Exception as exc:  # noqa: BLE001
                consecutive += 1
                logger.warning("lease renew heartbeat error run=%s: %s", self._run_id, exc)
                # 连续异常累计达本地阈值 → lost，停止无限吞异常（交由 recovery）。
                if consecutive >= 3:
                    self.lost = True
                    logger.warning("lease renew lost (consecutive errors) run=%s", self._run_id)
                    break
                continue
            if not ok:
                self.lost = True
                logger.warning("lease lost run=%s", self._run_id)
                break
            consecutive = 0

    def _heartbeat_once(self) -> bool:
        db = self._session_factory()
        try:
            return heartbeat(
                db,
                self._run_id,
                lease_token=self._lease_token,
                worker_id=self._worker_id,
            )
        finally:
            db.close()


def _run_with_lease_renewer(db: Session, engine, run: CompileRun) -> None:
    """execute_run + 独立续租线程（泵循环使用）；finally 停止续租线程。"""
    renewer = LeaseRenewer(engine, run.id, run.lease_token, run.worker_id)
    renewer.start()
    try:
        execute_run(db, run.id)
    finally:
        renewer.stop()


# ---------------------------------------------------------------------------
# stale recovery（闭合 stage + max 判定；幂等）
# ---------------------------------------------------------------------------


def _close_recovered_stages(db: Session, run: CompileRun) -> None:
    """recovery：该 run 的 running StageRun → failed(worker_lost) + finished_at；
    queued StageRun → skipped + finished_at。其余状态不动。"""
    now = _now()
    rows = (
        db.query(StageRun)
        .filter(StageRun.run_id == run.id)
        .all()
    )
    for row in rows:
        if row.status == "running":
            if not apply_stage_status(row, "failed"):
                row.safe_error_code = "worker_lost"
                row.safe_error_message = "worker lease expired"
                row.finished_at = now
        elif row.status == "queued":
            if not apply_stage_status(row, "skipped"):
                row.finished_at = now


def requeue_stale_runs(
    db: Session,
    timeout_seconds: int | None = None,
) -> int:
    """lease/心跳过期的 running run 恢复（幂等）。

    对每个过期 running run：
    1. running StageRun → failed(worker_lost)；queued StageRun → skipped；
    2. cancel_requested → _close_unfinished_stages(cancelled) + run → cancelled；
    3. 否则 run：attempt<max → queued（不加 attempt，清 lease，下次 claim +1）；
       attempt>=max → failed(retry_exhausted)，不再 requeue。
    返回重新入队（→ queued）数量。

    timeout_seconds：默认 None → 运行时读 settings
    wiki_pipeline_heartbeat_timeout_seconds（默认 300）；显式传值仍优先
    （既有测试全部显式传值，行为不变）。
    """
    if timeout_seconds is None:
        timeout_seconds = _heartbeat_timeout_default()
    now = _now()
    cutoff = now - timedelta(seconds=timeout_seconds)
    stale = (
        db.query(CompileRun)
        .filter(
            CompileRun.status == "running",
            or_(
                CompileRun.heartbeat_at.is_(None),
                CompileRun.heartbeat_at < cutoff,
                CompileRun.lease_expires_at.is_(None),
                CompileRun.lease_expires_at < now,
            ),
        )
        .all()
    )
    requeued = 0
    for run in stale:
        if run.cancel_requested:
            _close_unfinished_stages(db, run, "cancelled")
            reason = apply_run_status(run, "cancelled")
            if reason:
                logger.warning("stale cancel rejected run=%s reason=%s", run.id, reason)
                continue
            run.finished_at = now
            run.heartbeat_at = now
            run.error_summary = "cancelled_by_user"
            logger.info("stale running run cancelled run=%s", run.id)
            continue
        _close_recovered_stages(db, run)
        if run.attempt >= run.max_attempts:
            reason = apply_run_status(run, "failed")
            if reason:
                logger.warning("stale exhaust rejected run=%s reason=%s", run.id, reason)
                continue
            run.safe_error_code = "retry_exhausted"
            run.safe_error_message = "maximum attempts reached"
            run.error_summary = "maximum attempts reached"
            run.finished_at = now
            run.heartbeat_at = now
            logger.info("stale running run exhausted run=%s attempt=%s", run.id, run.attempt)
            continue
        reason = apply_run_status(run, "queued")
        if reason:
            logger.warning("requeue rejected run=%s reason=%s", run.id, reason)
            continue
        run.heartbeat_at = None
        run.started_at = None
        run.lease_token = None
        run.worker_id = None
        run.lease_expires_at = None
        run.error_summary = None
        run.safe_error_code = None
        run.safe_error_message = None
        requeued += 1
        logger.info("stale running run requeued run=%s attempt=%s", run.id, run.attempt)
    if stale:
        db.commit()
    return requeued


def recover_startup(db: Session) -> dict:
    """重启恢复：requeue stale running + 统计现存活 queued（它们已在 DB，无需迁移）。"""
    stale = requeue_stale_runs(db)
    queued = (
        db.query(CompileRun)
        .filter(CompileRun.status == "queued")
        .count()
    )
    return {"stale_requeued": stale, "queued": queued}


def run_startup_recovery(engine=None) -> dict:
    """独立 engine/session 执行 recover_startup（main.py startup 调用）。

    可选 engine 供测试注入独立临时库；默认连接 settings.database_url。
    注入的 engine 由调用方负责 dispose（本函数不 dispose 外部传入的引擎）。
    """
    from app.models.database import get_engine, get_session, init_db

    should_dispose = engine is None
    if engine is None:
        from app.config import settings
        engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wiki compile recover init_db failed: %s", exc)
            return {"stale_requeued": 0, "queued": 0, "error": "init_db_failed"}
        db = get_session(engine)
        try:
            return recover_startup(db)
        finally:
            db.close()
    finally:
        if should_dispose:
            engine.dispose()


# ---------------------------------------------------------------------------
# 进程内 Worker 泵线程
# ---------------------------------------------------------------------------


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    with _worker_lock:
        if _semaphore is None:
            _semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT)
        return _semaphore


def _pump_loop(
    pool: ThreadPoolExecutor,
    engine_factory=None,
    database_url: str | None = None,
) -> None:
    """单泵线程：每轮 requeue stale + claim + 带 lease 续租执行；空队列每轮睡一拍。"""
    from app.models.database import get_engine, get_session, init_db

    if engine_factory is not None:
        engine = engine_factory()
    else:
        from app.config import settings
        engine = get_engine(database_url or settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            logger.error("wiki compile pump init_db failed: %s", exc)
            return
        while not _stop_event.is_set():
            # 运行期挂死恢复：每轮独立事务 requeue stale running。
            try:
                db = get_session(engine)
                try:
                    requeue_stale_runs(db)
                finally:
                    db.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("wiki compile requeue stale failed: %s", exc)

            acquired = _get_semaphore().acquire(blocking=False)
            if acquired:
                db = get_session(engine)
                try:
                    run = claim_next_run(db)
                    if run is not None:
                        _run_with_lease_renewer(db, engine, run)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("wiki compile worker failed: %s", exc)
                    try:
                        db.rollback()
                    except Exception:
                        pass
                finally:
                    db.close()
                    _get_semaphore().release()
            # 无任务也要睡一轮：稳态空队列下每 poll 秒轮询一次（settings 可注入）。
            _stop_event.wait(_poll_interval())
    finally:
        engine.dispose()
        logger.info("wiki compile pump thread stopped")


def start_worker(engine_factory=None, database_url: str | None = None) -> bool:
    """启动进程内泵线程（幂等；main.py startup 调用）。返回是否本次新启动。"""
    global _worker_executor
    with _worker_lock:
        if _worker_executor is not None and not _worker_executor._shutdown:  # noqa: SLF001
            return False
        _stop_event.clear()
        _worker_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="wiki-compile",
        )
        _worker_executor.submit(_pump_loop, _worker_executor, engine_factory, database_url)
        logger.info("wiki compile worker started")
        return True


def worker_running() -> bool:
    with _worker_lock:
        return bool(_worker_executor is not None and not _worker_executor._shutdown)  # noqa: SLF001


def stop_worker() -> None:
    """停止泵线程并清理状态（幂等；main.py shutdown 调用）。"""
    global _worker_executor, _semaphore
    with _worker_lock:
        executor = _worker_executor
        _worker_executor = None
        _semaphore = None
    if executor is not None and not executor._shutdown:  # noqa: SLF001
        _stop_event.set()
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wiki compile worker shutdown failed: %s", exc)
        logger.info("wiki compile worker stopped")
    _stop_event.clear()
