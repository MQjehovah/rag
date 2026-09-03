"""Wiki 增量刷新调度器（V4 Phase C 调用链接线修复）。

并发模型（线程安全）：
- threading.Lock 保护进程级 pending 集合与恢复 backlog。
- 有上限的 ThreadPoolExecutor（settings.wiki_refresh_workers，默认 1，最大 2）。
- 真实队列上限：BoundedSemaphore 包住 running+queued 数量（settings.wiki_refresh_queue_size）。
- 队列满时任务进入 recovery backlog，worker 完成后自动续泵，不依赖 sleep。
- 开关判断调用 feature_enabled(db, "wiki_topic_enabled")，RuntimeFeatureFlag 数据库值优先。

Phase 5.1 单轨化（kill switch 语义）：
- 生产唯一自动链路 = wiki.default pipeline（KnowledgeCompileRun queued 落 DB，
  pipeline worker 消费）。旧线程池 `_run_page_refresh`/`_run_wiki_rebuild` 仅保留
  定义供测试/legacy 引用，生产入口一律不再 submit（双重发布消除）。
- flag `wiki_pipeline_default_enabled` 是 kill switch：settings 默认 True；DB
  RuntimeFeatureFlag 同名行可关闭。关闭 = 暂停编译：schedule_* 不建 run、保持
  dirty、返回未调度，绝不回退旧 Builder。
- flag 读取 DB-first（`_pipeline_kill_switch_enabled`）：DB 行优先，无行回退
  settings；进程内 5s TTL 缓存；DB 读失败回退 settings。

严格 LLM：_run_page_refresh / _run_wiki_rebuild 使用 call_wiki_llm_json（区分
service_unavailable 与 invalid_response），不再显式导入旧 call_llm_json。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.core.knowledge_compiler_v3.wiki_page_builder import (
    call_wiki_llm_json,
    rebuild_wiki_from_sources,
    refresh_wiki_for_page,
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

# Phase 5.1：kill switch 进程内 TTL 缓存（DB-first 读取，5 秒窗口避免每次开短 session）。
# 元组 (monotonic 时间戳, 生效值)。DB 读失败回退 settings 值并短暂缓存，防抖动。
_flag_cache_lock = threading.Lock()
_flag_cache: tuple[float, bool] | None = None
_FLAG_CACHE_TTL_SECONDS = 5.0


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


# ---------------------------------------------------------------------------
# Phase 5.1：调度入口单轨 wiki.default pipeline（kill switch）。
#
# flag wiki_pipeline_default_enabled = kill switch：
# - settings 默认 True；DB RuntimeFeatureFlag 同名行可关闭（跨进程/重启生效）。
# - ON：schedule_* / recover 建 wiki.default queued run（DB 持久队列），
#   由 pipeline worker 消费；绝不 submit 旧线程池 worker（避免双重发布）。
# - OFF（kill）：schedule_* / recover 不建 run、保持 Page/Wiki dirty、返回未调度；
#   dirty 由后续开关恢复后的 recover_dirty_pages 兜底。
# - _enqueue_* 失败：rollback + 保持 dirty + 返回 False（不回退旧 worker）。
# 实现约束：
# - flag 读取必须 DB-first：RuntimeFeatureFlag 行优先，无行回退 settings；
#   进程内 5s TTL 缓存减 DB 开销；DB 读失败回退 settings（防抖动）。
# - executor / wiki_default 延迟 import，避免模块级循环依赖。
# ---------------------------------------------------------------------------


def _read_kill_switch_row(db) -> bool | None:
    """读 RuntimeFeatureFlag 行；None = 无行（回退 settings）。"""
    from app.models.database import RuntimeFeatureFlag

    row = (
        db.query(RuntimeFeatureFlag)
        .filter(RuntimeFeatureFlag.name == "wiki_pipeline_default_enabled")
        .first()
    )
    return bool(row.enabled) if row is not None else None


def _pipeline_kill_switch_enabled() -> bool:
    """DB-first kill switch 读取：wiki_pipeline_default_enabled。

    返回 True = wiki.default 编译启用（可建 run）；False = kill（暂停编译）。

    规则：
    - settings 提供默认（Phase 5.1 默认 True）；
    - RuntimeFeatureFlag 同名行存在 → 以 DB 行为准（进程重启后 DB override 生效）；
    - 进程内模块级 TTL 缓存（_FLAG_CACHE_TTL_SECONDS 秒）避免每次入口开短 session；
    - DB 读失败（engine/init_db/session 异常）→ 回退 settings 值。
    """
    now = time.monotonic()
    global _flag_cache
    with _flag_cache_lock:
        if _flag_cache is not None and now - _flag_cache[0] < _FLAG_CACHE_TTL_SECONDS:
            return _flag_cache[1]

    default = bool(getattr(settings, "wiki_pipeline_default_enabled", True))
    value = default
    try:
        engine = get_engine(settings.database_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kill switch engine create failed, fallback settings=%s: %s", default, exc)
        with _flag_cache_lock:
            _flag_cache = (time.monotonic(), value)
        return value
    try:
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning("kill switch init_db failed, fallback settings=%s: %s", default, exc)
            value = default
        else:
            db = get_session(engine)
            try:
                override = _read_kill_switch_row(db)
                if override is not None:
                    value = override
            except Exception as exc:  # noqa: BLE001
                logger.warning("kill switch DB read failed, fallback settings=%s: %s", default, exc)
                value = default
            finally:
                db.close()
    finally:
        engine.dispose()

    with _flag_cache_lock:
        _flag_cache = (time.monotonic(), value)
    return value


def clear_kill_switch_cache() -> None:
    """清空 kill switch TTL 缓存（供测试翻转 DB flag 后强制重新读取）。"""
    global _flag_cache
    with _flag_cache_lock:
        _flag_cache = None


def _pipeline_flag_confirm(db) -> bool:
    """DB 层最终确认：RuntimeFeatureFlag override 优先（与 feature_enabled 一致）。"""
    from app.core.feature_flags import feature_enabled

    return bool(feature_enabled(db, "wiki_pipeline_default_enabled"))


def _resolve_page_workspace_id(db, page_id: str) -> str | None:
    """调度时解析 Page 归属 workspace（auto-create binding，镜像旧编译首建语义）。

    只 flush 不 commit：成功建 run 后由统一 commit 提交，失败则随 session 回滚。
    """
    from app.models.database import Notebook, Page
    from app.core.wiki_workspace.routing import ensure_notebook_workspace

    page = db.get(Page, page_id)
    if page is None or not page.notebook_id:
        return None
    notebook = db.get(Notebook, page.notebook_id)
    if notebook is None:
        return None
    workspace = ensure_notebook_workspace(db, notebook)
    return workspace.id if workspace is not None else None


def _page_refresh_input_hash(db, page_id: str) -> str:
    """Page 当前输入哈希（与旧 _page_input_hash 口径一致），作幂等 key 内容指纹。

    同内容重复调度 → 同 key 幂等返回原 run；内容变化 → 新 key 建新 run（supersede 旧）。
    """
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        _page_input_hash,
        _page_scope,
        _page_text,
        _scope_to_acl_json,
    )
    from app.models.database import Page

    page = db.get(Page, page_id)
    if page is None:
        return ""
    scope = _page_scope(db, page)
    acl = _scope_to_acl_json(scope) if scope else None
    content = _page_text(db, page)
    return _page_input_hash(page.title or "", content, page.notebook_id or "", acl or "")


def _wiki_rebuild_input_hash(db, wiki) -> str:
    """Wiki 重建输入哈希（标题 + 归属 + 来源 Page 内容），作 manual_rebuild 幂等指纹。"""
    import hashlib
    import json

    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        _page_input_hash,
        _page_scope,
        _page_text,
        _parse_source_pages,
        _scope_to_acl_json,
    )
    from app.models.database import Page

    parts = [wiki.title or "", wiki.acl_scope or "", wiki.workspace_id or ""]
    for pid in sorted(_parse_source_pages(wiki.source_page_ids)):
        parts.append(pid)
        page = db.get(Page, pid)
        if page is None:
            parts.append("")
            continue
        scope = _page_scope(db, page)
        acl = _scope_to_acl_json(scope) if scope else None
        parts.append(_page_input_hash(
            page.title or "", _page_text(db, page), page.notebook_id or "", acl or ""
        ))
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _create_default_run(
    db,
    *,
    trigger_type: str,
    trigger_object_id: str,
    workspace_id: str,
    wiki_page_id: str | None,
    idempotency_key: str,
    input_hash: str | None = None,
) -> bool:
    """建 wiki.default queued run 并 commit；任何失败回滚并返回 False。

    input_hash 缺省时由 executor 按 trigger 维度确定性计算（仅身份、不含内容）；
    删除事件等需内容/事件指纹的入队须显式传 input_hash（完整 64hex）。
    """
    from app.core.wiki_pipeline.executor import create_run
    from app.core.wiki_pipeline.pipelines.wiki_default import PIPELINE_KEY

    try:
        run = create_run(
            db,
            pipeline_key=PIPELINE_KEY,
            trigger_type=trigger_type,
            trigger_object_id=trigger_object_id,
            wiki_page_id=wiki_page_id,
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            supersede_same_trigger=True,
            input_hash=input_hash,
        )
        db.commit()
        logger.info(
            "wiki.default pipeline enqueued run=%s trigger=%s/%s workspace=%s",
            run.id, trigger_type, trigger_object_id, workspace_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "wiki.default pipeline enqueue failed trigger=%s/%s: %s",
            trigger_type, trigger_object_id, exc,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _enqueue_page_changed(page_id: str) -> bool:
    """kill on：page_changed → 建 wiki.default queued run（单 engine/session 生命周期）。

    返回 False = flag 实际关闭 或 无法解析/建 run；调用方保持 Page dirty（不回退旧 worker）。
    """
    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            logger.error("wiki.default enqueue init_db failed: %s", exc)
            return False
        db = get_session(engine)
        try:
            if not _pipeline_flag_confirm(db):
                return False
            from app.models.database import Page

            page = db.get(Page, page_id)
            if page is None:
                # 页面已不存在：无刷新目标（对齐旧 worker 早退语义）。
                return False
            workspace_id = _resolve_page_workspace_id(db, page_id)
            if not workspace_id:
                logger.warning("wiki.default enqueue skip page=%s (no workspace)", page_id)
                return False
            input_hash = _page_refresh_input_hash(db, page_id)
            # idempotency_key 列 String(128)：完整 64 hex + uuid 组合会超长，
            # hash 截断 16 hex（2^64 冲突概率可忽略），保持可读前缀。
            idempotency_key = f"wiki.default:{workspace_id}:{page_id}:{(input_hash or '_')[:16]}"
            return _create_default_run(
                db,
                trigger_type="page_changed",
                trigger_object_id=page_id,
                workspace_id=workspace_id,
                wiki_page_id=None,
                idempotency_key=idempotency_key,
            )
        finally:
            db.close()
    finally:
        engine.dispose()


def _enqueue_manual_rebuild(wiki_id: str) -> bool:
    """kill on：manual_rebuild（dirty wiki 重建）→ 建 wiki.default queued run。

    返回 False = flag 实际关闭 或 无法解析/建 run；调用方保持 Wiki dirty（不回退旧 worker）。
    """
    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            logger.error("wiki.default enqueue init_db failed: %s", exc)
            return False
        db = get_session(engine)
        try:
            if not _pipeline_flag_confirm(db):
                return False
            from app.models.database import WikiPage

            wiki = db.get(WikiPage, wiki_id)
            if wiki is None:
                return False
            workspace_id = wiki.workspace_id
            if not workspace_id:
                logger.warning("wiki.default enqueue skip wiki=%s (no workspace)", wiki_id)
                return False
            input_hash = _wiki_rebuild_input_hash(db, wiki)
            # idempotency_key 列 String(128)：hash 截断 16 hex 防超长。
            idempotency_key = f"wiki.default:{workspace_id}:{wiki_id}:{(input_hash or '_')[:16]}"
            return _create_default_run(
                db,
                trigger_type="manual_rebuild",
                trigger_object_id=wiki_id,
                workspace_id=workspace_id,
                wiki_page_id=wiki_id,
                idempotency_key=idempotency_key,
            )
        finally:
            db.close()
    finally:
        engine.dispose()


def schedule_page_refresh(page_id: str, *, changed: bool = True) -> bool:
    """Page 内容变化调度（Phase 5.1 单轨）。

    - changed=False / 空 id → True（无刷新目标，旧语义）。
    - kill switch OFF → 保持 Page dirty、不建 run、返回 False（未调度），
      绝不 submit 旧 worker。
    - kill switch ON → _enqueue_page_changed 建 page_changed run：
      成功 True；失败（无法解析/建 run）→ 保持 dirty、False。
    """
    if not changed or not page_id:
        return True

    if not _pipeline_kill_switch_enabled():
        logger.info("wiki.default kill switch off: page=%s kept dirty (not scheduled)", page_id)
        return False

    return _enqueue_page_changed(page_id)


def schedule_wiki_rebuild(wiki_id: str) -> bool:
    """Wiki 重建调度（Phase 5.1 单轨）。

    - 空 id → True（旧语义）。
    - kill switch OFF → 保持 Wiki dirty、不建 run、返回 False（未调度）。
    - kill switch ON → _enqueue_manual_rebuild 建 manual_rebuild run：成功 True；
      失败 → 保持 dirty、False。
    """
    if not wiki_id:
        return True

    if not _pipeline_kill_switch_enabled():
        logger.info("wiki.default kill switch off: wiki=%s kept dirty (not scheduled)", wiki_id)
        return False

    return _enqueue_manual_rebuild(wiki_id)


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


def _recover_via_default_pipeline(limit: int | None) -> dict:
    """kill on：dirty Page/Wiki 逐个转 wiki.default run（替代旧 backlog + 旧 worker）。

    查询与入队分离：先在一个短 session 读 dirty ids，再逐项 enqueue（每项自开
    session）。enqueue 失败项保持 dirty，由下次启动 recover_dirty_pages 重试
    （DB queued run 才是持久队列，不进进程级 backlog）。
    返回结构保持与旧 _load_recovery_backlog 一致（已装载的 dirty ids 统计）。
    """
    engine = get_engine(settings.database_url)
    dirty_ids: list[str] = []
    dirty_wiki_ids: list[str] = []
    try:
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            logger.error("recover init_db failed: %s", exc)
            return {"pages": dirty_ids, "wikis": dirty_wiki_ids}
        db = get_session(engine)
        try:
            if not _pipeline_flag_confirm(db):
                return {"pages": dirty_ids, "wikis": dirty_wiki_ids}
            dirty_ids = _query_dirty_page_ids(db, limit)
            from app.models.database import WikiPage
            dirty_wiki_ids = [r[0] for r in db.query(WikiPage.id).filter(WikiPage.dirty.is_(True)).all()]
        finally:
            db.close()
    finally:
        engine.dispose()

    for pid in dirty_ids:
        _enqueue_page_changed(pid)
    for wid in dirty_wiki_ids:
        _enqueue_manual_rebuild(wid)
    return {"pages": dirty_ids, "wikis": dirty_wiki_ids}


def _load_recovery_backlog(limit: int | None = None) -> dict:
    """Phase 5.1：kill switch 门控的 dirty 恢复装载。

    - kill ON：dirty Page/Wiki 逐个转 wiki.default run（_recover_via_default_pipeline），
      不再走进程级 backlog/pump。
    - kill OFF：不调度（保持 dirty），返回空统计。
    返回结构保持与旧 _load_recovery_backlog 一致（已装载的 dirty ids 统计）。
    """
    if not _pipeline_kill_switch_enabled():
        logger.info("wiki.default kill switch off: dirty recovery skipped (kept dirty)")
        return {"pages": [], "wikis": []}
    return _recover_via_default_pipeline(limit)


def recover_dirty_pages(limit: int | None = None) -> dict:
    """Phase 5.1：kill on 时 dirty Page/Wiki 转 wiki.default queued run（DB 持久队列）。

    kill off 时返回零统计、不调度（dirty 保留，由开关恢复后再次 recover 兜底）。
    """
    result = _load_recovery_backlog(limit)
    return {
        "submitted": len(result["pages"]),
        "rejected": 0,
        "wiki_loaded": len(result["wikis"]),
    }


def schedule_dirty_wikis() -> int:
    """kill on：dirty Wiki 逐个 enqueue manual_rebuild run；kill off：0（保持 dirty）。"""
    if not _pipeline_kill_switch_enabled():
        logger.info("wiki.default kill switch off: dirty wiki scheduling skipped")
        return 0
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


def _page_deleted_input_hash(page_id: str, workspace_id: str | None, notebook_id: str | None) -> str:
    """删除事件确定性输入指纹（契约四.2）。

    sha256("page_deleted:"+page_id+":"+(workspace_id or "")+":"+(notebook_id or ""))
    不含正文内容：删除事件本身即输入。恢复后再删 → workspace/notebook 变化产生新 hash。
    """
    import hashlib

    raw = "page_deleted:" + (page_id or "") + ":" + (workspace_id or "") + ":" + (notebook_id or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _page_deleted_idempotency_key(workspace_id: str, page_id: str, input_hash: str) -> str:
    """契约三：`wiki.default:v1:<sha256(workspace_id|page_deleted|page_id||input_hash)>`。

    列 String(128) 恰好容纳前缀 15 + 64 hex。内容冲突判定由 executor 基于
    run.input_hash（全量）比对，idempotency_key 只做唯一身份。
    """
    import hashlib

    raw = f"{workspace_id}|page_deleted|{page_id}||{input_hash}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"wiki.default:v1:{digest}"


def _enqueue_page_deleted(page_id: str) -> bool:
    """kill on：page_deleted → 建 wiki.default queued run（单 engine/session 生命周期）。

    语义：
    - Page 退役审计行保留时（sources 删除链）可解析 workspace → 建 run；
    - Page 行已不存在 / 无 binding / create_run 异常 → 记录 warning、返回 False
      （保持 dirty，不调旧删除 Builder；删除语义移交 pipeline publish_default 的
      page_deleted 分支处理）。
    """
    engine = get_engine(settings.database_url)
    try:
        try:
            init_db(engine)
        except Exception as exc:  # noqa: BLE001
            logger.error("wiki.default enqueue page_deleted init_db failed: %s", exc)
            return False
        db = get_session(engine)
        try:
            if not _pipeline_flag_confirm(db):
                return False
            from app.models.database import Page
            from app.core.wiki_workspace.routing import page_workspace_id

            page = db.get(Page, page_id)
            if page is None:
                logger.warning(
                    "wiki.default enqueue page_deleted skip page=%s (row gone)", page_id
                )
                return False
            workspace_id = page_workspace_id(db, page) if page.notebook_id else None
            if not workspace_id:
                logger.warning(
                    "wiki.default enqueue page_deleted skip page=%s (no active workspace binding)", page_id
                )
                return False
            input_hash = _page_deleted_input_hash(page_id, workspace_id, page.notebook_id)
            idempotency_key = _page_deleted_idempotency_key(workspace_id, page_id, input_hash)
            return _create_default_run(
                db,
                trigger_type="page_deleted",
                trigger_object_id=page_id,
                workspace_id=workspace_id,
                wiki_page_id=None,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
            )
        finally:
            db.close()
    finally:
        engine.dispose()


def schedule_page_deleted(page_id: str) -> None:
    """Page 删除调度（Phase 5.1 单轨）。

    - kill ON：enqueue page_deleted run（_enqueue_page_deleted），不再同步调
      remove_source_page_from_wikis —— 删除语义移交 pipeline run 内
      publish_default 的 page_deleted 分支（_safe_remove_source_page）。
    - kill OFF：保持（Page 已退役，其 wiki 来源保留 dirty，由后续 recover 重建）。
    - enqueue 失败：保持 dirty、返回（不调旧删除 Builder）。
    """
    if not page_id:
        return
    if not _pipeline_kill_switch_enabled():
        logger.info("wiki.default kill switch off: page_deleted=%s deferred (kept dirty)", page_id)
        return
    _enqueue_page_deleted(page_id)


def shutdown() -> None:
    """关闭线程池并清理状态（与 pump 协调，避免关闭后重建 executor）。"""
    global _executor, _semaphore, _shutdown_requested
    _shutdown_requested = True
    clear_kill_switch_cache()
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
