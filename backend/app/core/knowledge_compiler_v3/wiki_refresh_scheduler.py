"""Wiki 增量刷新调度器（V4 Phase C 调用链接线修复）。

并发模型（线程安全）：
- threading.Lock 保护进程级 pending 集合与恢复 backlog。
- 有上限的 ThreadPoolExecutor（settings.wiki_refresh_workers，默认 1，最大 2）。
- 真实队列上限：BoundedSemaphore 包住 running+queued 数量（settings.wiki_refresh_queue_size）。
- 队列满时任务进入 recovery backlog，worker 完成后自动续泵，不依赖 sleep。
- 开关判断调用 feature_enabled(db, "wiki_topic_enabled")，RuntimeFeatureFlag 数据库值优先。

严格 LLM：_run_page_refresh / _run_wiki_rebuild 使用 call_wiki_llm_json（区分
service_unavailable 与 invalid_response），不再显式导入旧 call_llm_json。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.core.knowledge_compiler_v3.wiki_page_builder import (
    call_wiki_llm_json,
    rebuild_wiki_from_sources,
    refresh_wiki_for_page,
    remove_source_page_from_wikis,
)
from app.models.database import get_engine, get_session, init_db

logger = logging.getLogger(__name__)

_pending_pages: set[str] = set()
_pending_wikis: set[str] = set()
_lock = threading.Lock()

# 恢复 backlog：队列满时暂存，worker 完成后续泵（同一恢复轮次每个 ID 只尝试一次）。
_recovery_page_backlog: list[str] = []
_recovery_wiki_backlog: list[str] = []
_attempted_page_ids: set[str] = set()
_attempted_wiki_ids: set[str] = set()

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_semaphore: threading.BoundedSemaphore | None = None

# 独立 pump 锁：同一时刻只允许一个 _pump_recovery_backlog 执行，
# 不与保护 pending/backlog 的 _lock 复用，避免在持锁时调用 schedule_*。
_pump_lock = threading.Lock()
_shutdown_requested = False


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None or _executor._shutdown:  # noqa: SLF001
            workers = max(1, min(int(getattr(settings, "wiki_refresh_workers", 1)), 2))
            _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wiki-refresh")
            logger.info("wiki refresh executor started workers=%s", workers)
        return _executor


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore
    with _executor_lock:
        if _semaphore is None:
            size = max(1, int(getattr(settings, "wiki_refresh_queue_size", 500)))
            _semaphore = threading.BoundedSemaphore(size)
            logger.info("wiki refresh queue size=%s", size)
        return _semaphore


def _wiki_refresh_enabled(db) -> bool:
    try:
        from app.core.feature_flags import feature_enabled
        return bool(feature_enabled(db, "wiki_topic_enabled"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("wiki refresh feature flag check failed: %s", exc)
        return False


def _run_page_refresh(page_id: str) -> None:
    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:
            logger.error("wiki refresh init_db failed: %s", exc)
            return
        db = get_session(engine)
        try:
            if not _wiki_refresh_enabled(db):
                logger.info("wiki refresh disabled, skip page=%s", page_id)
                return
            from app.models.database import Page, WikiPage
            page = db.get(Page, page_id)
            if page is None:
                return
            import asyncio as _asyncio
            _asyncio.run(refresh_wiki_for_page(db, page, commit=True))

            # V4 Phase F：Page 更新/刷新后，同 scope 债务有界重验证（失败不影响主流程）。
            try:
                from app.core.retrieval.debt_service import notify_knowledge_changed_for_page
                notify_knowledge_changed_for_page(db, page_id)
            except Exception as _exc:  # noqa: BLE001
                logger.warning("debt revalidate after page refresh failed: %s", _exc)

            # V4 Phase J-3：Wiki 刷新后重建该 Page 的图谱 provenance（幂等）。
            try:
                from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_page_graph_rebuild
                schedule_page_graph_rebuild(page_id)
            except Exception as _exc:  # noqa: BLE001
                logger.warning("graph rebuild after page refresh failed page=%s: %s", page_id, _exc)

            # not_worthy / 主题迁移 / 来源撤销产生的 dirty Wiki 进入恢复 backlog，
            # 由 finally 的 _pump_recovery_backlog 续泵（不反向 import builder）。
            dirty_wiki_ids = [r[0] for r in db.query(WikiPage.id).filter(WikiPage.dirty.is_(True)).all()]
            with _lock:
                for wid in dirty_wiki_ids:
                    if wid not in _recovery_wiki_backlog and wid not in _pending_wikis:
                        _recovery_wiki_backlog.append(wid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wiki refresh failed for page %s: %s", page_id, exc)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
    finally:
        engine.dispose()
        with _lock:
            _pending_pages.discard(page_id)
        _get_semaphore().release()
        _pump_recovery_backlog()


def _run_wiki_rebuild(wiki_id: str) -> None:
    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:
            logger.error("wiki rebuild init_db failed: %s", exc)
            return
        db = get_session(engine)
        try:
            if not _wiki_refresh_enabled(db):
                logger.info("wiki rebuild disabled, skip wiki=%s", wiki_id)
                return
            import asyncio as _asyncio
            _asyncio.run(rebuild_wiki_from_sources(db, wiki_id, call_wiki_llm_json, commit=True))

            # V4 Phase F：Wiki 重建/新建后，同 scope 债务有界重验证（携带真实生效内容）。
            try:
                from app.core.retrieval.debt_service import notify_knowledge_changed_for_wiki
                from app.core.retrieval.wiki_retriever import _valid_current_revision, _wiki_content
                wp = db.get(WikiPage, wiki_id)
                if wp is not None:
                    rev = _valid_current_revision(db, wp)
                    content = _wiki_content(db, wp, rev) if rev is not None else ""
                    notify_knowledge_changed_for_wiki(db, wp.acl_scope, content)
            except Exception as _exc:  # noqa: BLE001
                logger.warning("debt revalidate after wiki rebuild failed: %s", _exc)

            # V4 Phase J-3：Wiki 自动刷新成功后重建该 Wiki 图谱 provenance（幂等）。
            try:
                from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_wiki_graph_rebuild
                schedule_wiki_graph_rebuild(wiki_id)
            except Exception as _exc:  # noqa: BLE001
                logger.warning("graph rebuild after wiki rebuild failed wiki=%s: %s", wiki_id, _exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wiki rebuild failed for %s: %s", wiki_id, exc)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
    finally:
        engine.dispose()
        with _lock:
            _pending_wikis.discard(wiki_id)
        _get_semaphore().release()
        _pump_recovery_backlog()


def schedule_page_refresh(page_id: str, *, changed: bool = True) -> bool:
    if not changed or not page_id:
        return True

    with _lock:
        if page_id in _pending_pages:
            logger.info("wiki refresh dedup: page=%s already pending", page_id)
            return True
        _pending_pages.add(page_id)

    acquired = _get_semaphore().acquire(blocking=False)
    if not acquired:
        logger.warning("wiki refresh queue full, page=%s kept dirty", page_id)
        with _lock:
            _pending_pages.discard(page_id)
        return False

    try:
        _get_executor().submit(_run_page_refresh, page_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("wiki refresh submit failed page=%s: %s", page_id, exc)
        _get_semaphore().release()
        with _lock:
            _pending_pages.discard(page_id)
        return False


def schedule_wiki_rebuild(wiki_id: str) -> bool:
    if not wiki_id:
        return True

    with _lock:
        if wiki_id in _pending_wikis:
            logger.info("wiki rebuild dedup: wiki=%s already pending", wiki_id)
            return True
        _pending_wikis.add(wiki_id)

    acquired = _get_semaphore().acquire(blocking=False)
    if not acquired:
        logger.warning("wiki rebuild queue full, wiki=%s kept dirty", wiki_id)
        with _lock:
            _pending_wikis.discard(wiki_id)
        return False

    try:
        _get_executor().submit(_run_wiki_rebuild, wiki_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("wiki rebuild submit failed wiki=%s: %s", wiki_id, exc)
        _get_semaphore().release()
        with _lock:
            _pending_wikis.discard(wiki_id)
        return False


def get_refresh_status() -> dict:
    with _lock:
        pending_pages = len(_pending_pages)
        pending_wikis = len(_pending_wikis)
        backlog = len(_recovery_page_backlog) + len(_recovery_wiki_backlog)
    return {
        "pending_pages": pending_pages,
        "pending_wikis": pending_wikis,
        "backlog": backlog,
        "queue_capacity": int(getattr(settings, "wiki_refresh_queue_size", 500)),
        "queue_occupied": pending_pages + pending_wikis,
    }


# ---------------------------------------------------------------------------
# 恢复 backlog 与续泵
# ---------------------------------------------------------------------------

def _pump_recovery_backlog() -> None:
    """按当前信号量容量提交 backlog 中的任务；队列满的项保留在 backlog 待下次续泵。

    并发时序：
    - 独立 _pump_lock 保证同一时刻只有一个 pump 执行；
    - worker finally 调用时阻塞等待当前 pump 完成，然后再次检查 backlog；
    - 不在持有 _lock 时调用 schedule_*（schedule 内部会自行加 _lock）；
    - 队列满时把失败项放回 backlog 并返回，等待 worker 完成后续泵。
    """
    with _pump_lock:
        if _shutdown_requested:
            return

        with _lock:
            pages = list(_recovery_page_backlog)
            wikis = list(_recovery_wiki_backlog)
            _recovery_page_backlog.clear()
            _recovery_wiki_backlog.clear()

        if not pages and not wikis:
            return

        remaining_pages: list[str] = []
        for pid in pages:
            if not schedule_page_refresh(pid, changed=True):
                remaining_pages.append(pid)

        remaining_wikis: list[str] = []
        for wid in wikis:
            if not schedule_wiki_rebuild(wid):
                remaining_wikis.append(wid)

        with _lock:
            _recovery_page_backlog.extend(remaining_pages)
            _recovery_wiki_backlog.extend(remaining_wikis)


def _load_recovery_backlog(limit: int | None = None) -> dict:
    """加载本轮 dirty Page/Wiki 到 backlog（只加入，不直接循环提交）。"""
    engine = get_engine(settings.database_url)
    dirty_ids: list[str] = []
    dirty_wiki_ids: list[str] = []
    try:
        try:
            init_db(engine)
        except Exception as exc:
            logger.error("recover init_db failed: %s", exc)
            return {"pages": dirty_ids, "wikis": dirty_wiki_ids}
        db = get_session(engine)
        try:
            if not _wiki_refresh_enabled(db):
                return {"pages": dirty_ids, "wikis": dirty_wiki_ids}
            dirty_ids = _query_dirty_page_ids(db, limit)
            from app.models.database import WikiPage
            dirty_wiki_ids = [r[0] for r in db.query(WikiPage.id).filter(WikiPage.dirty.is_(True)).all()]
        finally:
            db.close()
    finally:
        engine.dispose()

    with _lock:
        _attempted_page_ids.clear()
        _attempted_wiki_ids.clear()
        for pid in dirty_ids:
            if pid not in _recovery_page_backlog:
                _recovery_page_backlog.append(pid)
        for wid in dirty_wiki_ids:
            if wid not in _recovery_wiki_backlog:
                _recovery_wiki_backlog.append(wid)

    _pump_recovery_backlog()
    return {"pages": dirty_ids, "wikis": dirty_wiki_ids}


def recover_dirty_pages(limit: int | None = None) -> dict:
    """加载本轮 dirty Page/Wiki 到恢复 backlog 并触发续泵。

    dirty Wiki 也进入 backlog（/refresh-dirty 不再单独循环提交）。
    """
    result = _load_recovery_backlog(limit)
    return {
        "submitted": len(result["pages"]),
        "rejected": 0,
        "wiki_loaded": len(result["wikis"]),
    }


def schedule_dirty_wikis() -> int:
    """把当前 dirty Wiki 加入恢复 backlog（供 /refresh-dirty 调用）。"""
    result = _load_recovery_backlog()
    return len(result["wikis"])


def _query_dirty_page_ids(db, limit: int | None) -> list[str]:
    from app.models.database import Page, SourceItem

    query = (
        db.query(Page.id)
        .filter(Page.wiki_dirty.is_(True))
        .order_by(Page.updated_at, Page.id)
    )
    if limit is not None:
        query = query.limit(limit)
    rows = query.all()
    ids = [r[0] for r in rows]
    if not ids:
        return []

    source_pages = (
        db.query(Page.id)
        .filter(Page.id.in_(ids), Page.source_type.isnot(None))
        .all()
    )
    source_page_ids = {r[0] for r in source_pages}
    if not source_page_ids:
        return ids

    active_source_ids = {
        r[0]
        for r in db.query(SourceItem.page_id)
        .filter(SourceItem.page_id.in_(source_page_ids), SourceItem.state == "active")
        .all()
    }
    return [pid for pid in ids if pid not in source_page_ids or pid in active_source_ids]


def schedule_page_deleted(page_id: str) -> None:
    if not page_id:
        return
    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:
            logger.error("wiki schedule_page_deleted init_db failed: %s", exc)
            return
        db = get_session(engine)
        try:
            result = remove_source_page_from_wikis(db, page_id, commit=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wiki remove source failed for page %s: %s", page_id, exc)
            try:
                db.rollback()
            except Exception:
                pass
            result = {}
        finally:
            db.close()
    finally:
        engine.dispose()

    for wiki_id in (result.get("dirty_remaining_wiki_ids") or set()):
        schedule_wiki_rebuild(wiki_id)


def shutdown() -> None:
    """关闭线程池并清理状态（与 pump 协调，避免关闭后重建 executor）。"""
    global _executor, _semaphore, _shutdown_requested
    _shutdown_requested = True
    # 等待正在执行的 pump 完成，再关 executor，避免 pump 在关闭后重建 executor。
    with _pump_lock:
        pass
    with _executor_lock:
        if _executor is not None and not _executor._shutdown:  # noqa: SLF001
            _executor.shutdown(wait=True)
            _executor = None
            logger.info("wiki refresh executor shut down")
        _semaphore = None
    with _lock:
        _pending_pages.clear()
        _pending_wikis.clear()
        _recovery_page_backlog.clear()
        _recovery_wiki_backlog.clear()
        _attempted_page_ids.clear()
        _attempted_wiki_ids.clear()
    _shutdown_requested = False
