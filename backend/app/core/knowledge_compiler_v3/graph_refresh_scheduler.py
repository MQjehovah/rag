"""V4 图谱增量刷新调度器（J-3 封板）。

普通知识更新（Page/Wiki 内容变化）使用幂等后台调度；调度失败只记录安全结构化
原因，不回滚 Page/Wiki 主流程。删除/失效类（Page 删除、SourceItem skipped、
NEEDS_REASSIGN、Notebook 改组、Wiki 回滚）必须在调用方事务内同步执行
remove_page_graph / remove_wiki_graph（commit=False），本模块不异步处理失效。

幂等：进程级 pending 集合去重，同一 ID 只调度一次；有界线程池（默认 1 worker）。
构建是纯确定性 SQL 抽取（不调用 LLM/Embedding/Reranker），失败只记录原因。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.models.database import get_engine, get_session, init_db

logger = logging.getLogger(__name__)

_pending_pages: set[str] = set()
_pending_wikis: set[str] = set()
_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_shutdown_requested = False


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None or _executor._shutdown:  # noqa: SLF001
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph-build")
            logger.info("graph build executor started")
        return _executor


def _run_page_graph_build(page_id: str) -> None:
    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:
            logger.error("graph build init_db failed page=%s: %s", page_id, exc)
            return
        db = get_session(engine)
        try:
            from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
            rebuild_page_graph(db, page_id, commit=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph build failed page=%s: %s", page_id, exc)
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


def _run_wiki_graph_build(wiki_id: str) -> None:
    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:
            logger.error("graph build init_db failed wiki=%s: %s", wiki_id, exc)
            return
        db = get_session(engine)
        try:
            from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_wiki_graph
            rebuild_wiki_graph(db, wiki_id, commit=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph build failed wiki=%s: %s", wiki_id, exc)
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


def schedule_page_graph_rebuild(page_id: str) -> bool:
    """幂等调度 Page 图谱重建（后台）。失败仅记录，不影响调用方。"""
    if not page_id:
        return True
    with _lock:
        if page_id in _pending_pages:
            return True
        _pending_pages.add(page_id)
    try:
        _get_executor().submit(_run_page_graph_build, page_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph build submit failed page=%s: %s", page_id, exc)
        with _lock:
            _pending_pages.discard(page_id)
        return False


def schedule_wiki_graph_rebuild(wiki_id: str) -> bool:
    """幂等调度 Wiki 图谱重建（后台）。失败仅记录，不影响调用方。"""
    if not wiki_id:
        return True
    with _lock:
        if wiki_id in _pending_wikis:
            return True
        _pending_wikis.add(wiki_id)
    try:
        _get_executor().submit(_run_wiki_graph_build, wiki_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph build submit failed wiki=%s: %s", wiki_id, exc)
        with _lock:
            _pending_wikis.discard(wiki_id)
        return False


def shutdown() -> None:
    global _executor, _shutdown_requested
    _shutdown_requested = True
    with _executor_lock:
        if _executor is not None and not _executor._shutdown:  # noqa: SLF001
            _executor.shutdown(wait=True)
            _executor = None
    with _lock:
        _pending_pages.clear()
        _pending_wikis.clear()
    _shutdown_requested = False
