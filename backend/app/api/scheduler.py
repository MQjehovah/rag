"""每日扫描调度（W11）。

每天凌晨运行：过期扫描 + 冲突扫描。仿 organize.py 的 APScheduler 模式。
"""
from __future__ import annotations

import logging

from app.config import settings
from app.models.database import get_engine, get_session, init_db

logger = logging.getLogger(__name__)
_scheduler = None


def _run_daily_scan_sync():
    """Phase H：旧冲突扫描/债务扫描/Community 重建调度已退出，仅保留空实现。

    每日扫描任务仍在 APScheduler 注册（避免改动启动逻辑），但不再执行旧 Card/KO 扫描。
    """
    logger.info("daily_scan: disabled (Phase H removed legacy conflict/debt/community scan)")


def _run_persistent_jobs_sync():
    """高频处理可恢复后台任务：仅保留视觉分析任务，移除旧 Community 重建。"""
    try:
        engine = get_engine(settings.database_url)
        db = get_session(engine)
        try:
            from app.core.vision_analysis import process_vision_analysis_jobs

            vision = process_vision_analysis_jobs(db)
            if vision.get("jobs"):
                logger.info("persistent_jobs: vision=%s", vision)
        finally:
            db.close()
            engine.dispose()
    except Exception as exc:
        logger.exception("persistent jobs failed: %s", exc)


def start_daily_scheduler():
    """启动每日扫描调度器。config 开关控制。"""
    if not settings.auto_daily_scan_enabled:
        return
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            _run_daily_scan_sync,
            "cron",
            hour=settings.daily_scan_hour,
            id="daily_scan",
            replace_existing=True,
        )
        scheduler.add_job(
            _run_persistent_jobs_sync,
            "interval",
            minutes=1,
            id="persistent_jobs",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        _scheduler = scheduler
        logger.info(f"Daily scan scheduler started (hour={settings.daily_scan_hour})")
    except Exception as exc:
        logger.error(f"Failed to start daily scan scheduler: {exc}")
