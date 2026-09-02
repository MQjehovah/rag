"""统一数据源任务执行器：消费 SourceSyncRun 并进入 Page→Evidence→Card 主链路。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.core.evidence_ingest import sync_page_evidence
from app.core.source_conversion.schemas import CanonicalNote
from app.models.database import (
    EvidenceItem,
    Notebook,
    Page,
    PageChunk,
    SourceConnection,
    SourceItem,
    SourceSyncRun,
)
from app.sources.registry import get_connector
from app.sources.schemas import InputRepresentation, NormalizedSourceItem, SourcePayloadError
from app.sources.service import apply_item, decide, record_error
from app.sources.worker import finish_run, heartbeat, should_stop


logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _valid_sha256(value: str | None) -> str | None:
    """校验 original_source_hash 为空或严格匹配 64 位小写 SHA-256；否则返回 None。"""
    if not value:
        return None
    value = value.strip().lower()
    if not _SHA256_RE.match(value):
        return None
    return value


def register_builtin_connectors() -> None:
    """显式注册内置 Connector；导入模块本身不会发起网络或任务。"""
    from app.sources.dingtalk import register_dingtalk_connector
    from app.sources.gitlab import register_gitlab_connector

    register_dingtalk_connector()
    register_gitlab_connector()


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _require_target_notebook(db: Session, connection: SourceConnection) -> Notebook:
    notebook = db.get(Notebook, connection.target_notebook_id) if connection.target_notebook_id else None
    if notebook is None:
        raise ValueError("数据源连接必须绑定有效的 target_notebook_id")
    if not notebook.group_id:
        raise ValueError("目标知识库未绑定权限组，按照 fail-closed 原则拒绝同步")
    return notebook


def _convert_note(
    normalized: NormalizedSourceItem,
    connector_key: str,
) -> tuple[CanonicalNote | None, str, str, str, bool]:
    """在 executor 统一边界把 NormalizedSourceItem 转换为 CanonicalNote（Phase 2）。

    返回 (note, error_code, error_message, converter_summary, retryable)。
    - note：converted/partial 时返回；failed/blocked 时返回 None。
    - error_code：失败诊断 code；成功时为 ""。
    - converter_summary：converter_key/version 摘要（供 SourceSyncError）。
    - retryable：按错误类型判断。
    """
    from app.core.source_conversion.schemas import ConversionStatus
    from app.sources.conversion_adapter import convert_normalized
    from app.sources.schemas import SourcePayloadError

    try:
        note = convert_normalized(normalized, connector_key=connector_key)
    except SourcePayloadError as exc:
        # 结构化 payload 错误：safe_message 面向用户，internal_detail 只进日志
        logger.warning(
            "convert_note payload error external_id=%s code=%s detail=%s",
            normalized.external_id, exc.error_code, exc.internal_detail or exc.safe_message,
        )
        return None, exc.error_code, exc.safe_message, "", exc.retryable
    except Exception as exc:  # noqa: BLE001
        logger.warning("convert_note 失败 external_id=%s: %s", normalized.external_id, exc)
        return None, "convert_failed", "来源内容转换失败", "", True

    status = note.conversion_status
    if status in (ConversionStatus.CONVERTED, ConversionStatus.PARTIAL):
        return note, "", "", f"{note.converter_key}/{note.converter_version}", False

    # failed / blocked：提取首个 error diagnostic
    error_code = "conversion_failed"
    error_message = "转换未产生有效正文"
    for d in note.diagnostics:
        if d.is_error:
            error_code = d.code
            error_message = d.message or error_code
            break
    converter_summary = f"{note.converter_key}/{note.converter_version}" if note.converter_key else ""
    # blocked（加密/不支持/歧义/match失败）通常 retryable=False；failed 可重试
    retryable = status != ConversionStatus.BLOCKED
    return None, error_code, error_message, converter_summary, retryable


def _upsert_page(
    db: Session,
    connection: SourceConnection,
    notebook: Notebook,
    source_item: SourceItem,
    normalized: NormalizedSourceItem,
    note: CanonicalNote,
) -> Page:
    page = db.query(Page).filter(
        Page.source_type == normalized.source_type,
        Page.source_id == normalized.external_id,
    ).first()
    if page is None and source_item.page_id:
        page = db.get(Page, source_item.page_id)
    if page is None:
        page = Page(source_type=normalized.source_type, source_id=normalized.external_id)
        db.add(page)
        db.flush()

    page.notebook_id = notebook.id
    page.title = note.title or normalized.title or normalized.external_id
    # Page.content 保持 CanonicalNote.body（检索/证据/图谱/Wiki 依赖 page.content）
    page.content = note.body or ""
    page.source_path = normalized.source_path
    page.source_url = normalized.source_url
    # Phase 2.2：三种 hash 真正持久化语义
    #   source_content_hash = 原始来源文件/文本 SHA-256
    #       = normalized.original_source_hash（Connector 提供且可验证）否则 None
    #       （不退化冒充 Markdown 输入 hash；metadata 标记 original_hash_unavailable）
    #   source_markdown_hash = CanonicalNote.body_hash
    #   content_hash = 与 Page.content 一致的 CanonicalNote.body_hash
    page.source_content = note.body or ""
    original_source_hash = _valid_sha256(normalized.original_source_hash)
    # Phase 2.3：用 input_representation 决定 source_content_hash 权威来源（不靠 source_type 猜测）
    if normalized.input_representation == InputRepresentation.ORIGINAL:
        # ORIGINAL：payload 即原始资料 → 已验证 original_source_hash；未显式提供则回退 effective payload hash
        page.source_content_hash = original_source_hash or note.source_hash
    else:
        # PRECONVERTED_MARKDOWN：原始文件 hash；无法验证 → NULL（不冒充）
        page.source_content_hash = original_source_hash
    page.source_markdown_hash = note.body_hash
    page.content_hash = note.body_hash
    # original_hash_unavailable 标记：由 executor 主循环在 note 级别记录
    page.index_dirty = True
    page.wiki_dirty = True  # V4 Phase C：数据源新增/更新 Page 标记 Wiki 待处理
    page.last_synced_at = datetime.now()
    # Phase 2：CanonicalNote 字段写入（nullable，历史 Page 缺失视为 legacy-note/v0）
    page.note_schema_version = note.schema_version
    page.content_format = "markdown"
    page.content_kind = note.content_kind.value if note.content_kind else ""
    page.converter_key = note.converter_key
    page.converter_version = note.converter_version
    page.conversion_status = note.conversion_status.value
    page.conversion_warnings_json = _serialize_warnings(note)
    source_item.page_id = page.id
    return page


def _serialize_warnings(note: CanonicalNote) -> str | None:
    """把 note.warnings（派生 dict 列表）序列化为 JSON。"""
    import json
    warnings = note.warnings
    if not warnings:
        return None
    return json.dumps(list(warnings), ensure_ascii=False)


def _retire_deleted_page(db: Session, source_item: SourceItem) -> None:
    """保留原 Page 审计内容，但从检索和 Evidence 主链路移除。

    顺序（J-3 封板）：
    1. 先 remove_page_graph(commit=False)：在 PageChunk/Evidence 删除前收集受影响
       关系/实体/Community（避免 FK CASCADE 先删 provenance 导致无法收集）。
    2. 再删 PageChunk / 标记 Evidence stale / 退役 Page。
    3. 统一 commit（由调用方）；图谱失效失败抛异常，不产生半完成状态。
    """
    if not source_item.page_id:
        return
    # 1. 先移除图谱 provenance（收集受影响关系/实体/Community 后清理）。
    from app.core.knowledge_compiler_v3.v4_graph_builder import remove_page_graph
    remove_page_graph(db, source_item.page_id, commit=False)
    # 2. 再删 Chunk / 标记 Evidence stale / 退役 Page。
    db.query(PageChunk).filter(PageChunk.page_id == source_item.page_id).delete(
        synchronize_session=False
    )
    db.query(EvidenceItem).filter(EvidenceItem.source_page_id == source_item.page_id).update(
        {EvidenceItem.status: "stale"}, synchronize_session=False
    )
    page = db.get(Page, source_item.page_id)
    if page:
        page.keywords = ""
        page.indexed_content_hash = None
        page.index_dirty = True
        # V4 Phase C：删除/退役 Page 不再进入 Wiki 恢复范围
        page.wiki_dirty = False


def _set_stage(db: Session, run_id: str, stage: str, progress: str) -> None:
    """更新任务阶段/进度/心跳并提交。"""
    run = db.get(SourceSyncRun, run_id)
    if run is None:
        return
    run.stage = stage
    run.progress = progress
    run.heartbeat_at = datetime.now()
    db.commit()


def _schedule_wiki_refresh_for_page(db: Session, page_id: str, action: str, *, changed: bool = False) -> None:
    """V4 Phase C：数据源新增/更新 Page 后调度 Wiki 增量刷新（异步、幂等）。

    Phase 2.5：changed 显式传入，不靠 action 推导。派生恢复（retry_derived）必须传 changed=True，
    否则真实 schedule_page_refresh(changed=False) 会直接返回、不提交任务。
    """
    try:
        from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import schedule_page_refresh
        schedule_page_refresh(page_id, changed=changed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule wiki refresh failed for page=%s: %s", page_id, exc)


def _schedule_graph_rebuild(page_id: str) -> None:
    """V4 Phase J-3：数据源同步完成后调度图谱重建（异步、幂等，失败不影响同步）。"""
    try:
        from app.core.knowledge_compiler_v3.graph_refresh_scheduler import schedule_page_graph_rebuild
        schedule_page_graph_rebuild(page_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schedule graph rebuild failed for page=%s: %s", page_id, exc)


def _mark_llm_degraded(db: Session, run_id: str, reason: str) -> None:
    """标记 GLM/LLM 降级，不 fail 任务。"""
    run = db.get(SourceSyncRun, run_id)
    if run is None:
        return
    run.llm_degraded = True
    run.degraded_reason = (run.degraded_reason or "") + reason + "; "
    db.commit()


async def _run_dingtalk_remote_sync(
    db: Session,
    run: SourceSyncRun,
    connection: SourceConnection,
    config: dict,
    space_ids: list[str],
) -> dict:
    """在 iter_changes 之前执行钉钉远程下载/转换流水线，返回游标。"""
    from app.sources.dingtalk_pipeline import DingTalkRemoteSyncPipeline

    space_ids = [str(s) for s in space_ids if str(s).strip()]

    async def _on_progress(stage: str, processed: int, total: int, message: str):
        _set_stage(db, run.id, f"remote:{stage}", message)

    async def _check_cancelled() -> bool:
        return should_stop(db, run.id)

    pipeline = DingTalkRemoteSyncPipeline(config)
    _set_stage(db, run.id, "remote:scan", "正在刷新钉钉远程文档")
    result = await pipeline.run(
        mode=run.mode,
        space_ids=space_ids,
        on_progress=_on_progress,
        check_cancelled=_check_cancelled,
    )
    return result


async def execute_run(db: Session, run: SourceSyncRun) -> None:
    """执行一个已被 claim 为 running 的任务；单条失败不回滚整批。"""
    connection = db.get(SourceConnection, run.connection_id)
    if connection is None or not connection.enabled:
        finish_run(db, run.id, "failed", "连接不存在或已停用")
        return

    try:
        # 统一路径映射：所有 Connector 逐文件由路径映射/连接级目标决策解析。
        # 不再在读取第一条 SourceItem 之前因缺 target 让整个 run 失败。
        notebook = None  # 由统一目标 Notebook 决策逐文件解析
        config = _json(connection.config_json)
        config["connection_id"] = connection.id
        connector = get_connector(connection.connector_key, config)
        cursor = _json(connection.cursor_json)
        # run 级参数（cursor_before_json）优先于连接级 config
        run_params = _json(run.cursor_before_json)
        pilot_limit = max(0, int(run_params.get("pilot_limit") or config.get("pilot_limit") or 0))
        selected_space_ids = list(run_params.get("selected_space_ids") or config.get("selected_space_ids") or [])
        retry_scope = {
            str(s) for s in (run_params.get("retry_scope") or config.get("retry_scope") or [])
            if str(s).strip()
        }
    except Exception as exc:
        finish_run(db, run.id, "failed", str(exc))
        return

    db.commit()  # 网络调用前结束读取事务

    new_cursor: dict | None = None
    remote_summary: dict | None = None

    # 钉钉：先刷新远程清单 + 下载 + 转换
    if connection.connector_key == "dingtalk":
        _set_stage(db, run.id, "remote", "刷新钉钉远程数据")
        try:
            remote_summary = await _run_dingtalk_remote_sync(
                db, run, connection, config, selected_space_ids,
            )
            new_cursor = remote_summary.get("new_cursor")
            run = db.get(SourceSyncRun, run.id)
            run.discovered_count = int(remote_summary.get("scanned") or 0)
            db.commit()
        except Exception as exc:
            db.rollback()
            finish_run(db, run.id, "failed", f"钉钉远程同步失败：{exc}")
            logger.exception("dingtalk remote sync failed run=%s", run.id)
            return
        if should_stop(db, run.id):
            finish_run(db, run.id, "cancelled", "任务已在远程同步阶段停止")
            return

    processed = 0
    _set_stage(db, run.id, "discover", "读取变更并写入条目")
    # V4 Phase F：本次真正新增/变化的 page_id（成功后做债务重验证，不全连接扫描）
    changed_page_ids: set[str] = set()
    try:
        async for change in connector.iter_changes(cursor):
            if should_stop(db, run.id):
                break
            if pilot_limit > 0 and processed >= pilot_limit:
                break
            if retry_scope and change.external_id not in retry_scope:
                continue
            processed += 1
            run = db.get(SourceSyncRun, run.id)
            run.discovered_count = int(run.discovered_count or 0) + 1
            run.heartbeat_at = datetime.now()
            db.commit()

            existing = db.query(SourceItem).filter(
                SourceItem.connection_id == connection.id,
                SourceItem.external_id == change.external_id,
            ).first()
            # Phase 2.3：每轮循环重置条目级状态（防止跨条目污染）
            page = None
            normalized = None
            derived_started = False
            # Phase 2.5：成功计数在关键处理链成功后才增加（互斥于 failed_count）
            decision_succeeded = False
            try:
                if change.deleted:
                    normalized = NormalizedSourceItem(
                        connection_id=connection.id,
                        source_type=connection.connector_key,
                        external_id=change.external_id,
                        external_version=change.external_version,
                        deleted=True,
                    )
                else:
                    acl = await connector.fetch_acl(change.external_id)
                    if acl.resolve_failed or not acl.scope:
                        raise PermissionError("ACL 无法解析，已 fail closed")
                    try:
                        normalized = await connector.fetch_item(change.external_id)
                    except SourcePayloadError as exc:
                        db.rollback()
                        run = db.get(SourceSyncRun, run.id)
                        run.failed_count = int(run.failed_count or 0) + 1
                        record_error(
                            db, run.id, change.external_id, exc.stage,
                            exc.error_code, exc.safe_message, retryable=exc.retryable,
                        )
                        db.commit()
                        logger.warning(
                            "fetch payload error external_id=%s code=%s detail=%s",
                            change.external_id, exc.error_code, exc.internal_detail or exc.safe_message,
                        )
                        heartbeat(db, run.id)
                        continue
                    normalized.connection_id = connection.id
                    # Phase 2.1：明确 ACL 结构（scope/groups/resolve_failed/raw），不压平 raw。
                    # raw 中的 scope 不得覆盖标准 scope。
                    normalized.acl_scope = {
                        "scope": acl.scope,
                        "groups": list(getattr(acl, "groups", []) or []),
                        "resolve_failed": bool(acl.resolve_failed),
                        "raw": dict(acl.raw or {}),
                    }

                # 统一路径映射：有路径能力的数据源按路径映射归属目标 Notebook；
                # 未映射不创建 Page/Chunk/Evidence，只记录 skipped 与安全原因。
                # 统一路径映射：所有 Connector 逐文件由统一目标 Notebook 决策解析。
                if not normalized.deleted:
                    from app.core.path_mapping import (
                        NEEDS_REASSIGN_CODE,
                        NEEDS_REASSIGN_REASON,
                        SKIP_CODE_UNMAPPED,
                        SAFE_REASON_UNMAPPED,
                        resolve_target_notebook_decision,
                    )
                    # 命名域：钉钉 space_id / GitLab project_id（acl.raw 里）。
                    raw = (normalized.acl_scope or {}).get("raw") or (acl.raw or {})
                    mapping_space = str(
                        raw.get("space_id") or raw.get("project_id") or ""
                    ) or None
                    decision_res = resolve_target_notebook_decision(
                        db, connection, mapping_space, normalized.source_path
                    )
                    if decision_res.source == "unmapped":
                        source_item = apply_item(db, normalized)
                        source_item.state = "skipped"
                        source_item.last_error = SAFE_REASON_UNMAPPED
                        source_item.metadata_hash = None  # 不视为已同步内容
                        db.commit()
                        run = db.get(SourceSyncRun, run.id)
                        run.unchanged_count = int(run.unchanged_count or 0) + 1
                        run.heartbeat_at = datetime.now()
                        record_error(
                            db, run.id, change.external_id, "persist",
                            SKIP_CODE_UNMAPPED, SAFE_REASON_UNMAPPED,
                            retryable=False,
                        )
                        db.commit()
                        heartbeat(db, run.id)
                        continue
                    effective_notebook = db.get(Notebook, decision_res.notebook_id)
                    if effective_notebook is None:
                        raise ValueError("目标 Notebook 不存在，已 fail closed")
                    # 已有 SourceItem 且其 Page 归属与当前有效目标不同 → NEEDS_REASSIGN，
                    # 不静默迁移；同事务失效 Wiki/图谱 provenance。
                    if existing is not None and existing.page_id:
                        existing_page = db.get(Page, existing.page_id)
                        if (
                            existing_page is not None
                            and existing_page.notebook_id != effective_notebook.id
                        ):
                            try:
                                from app.core.knowledge_compiler_v3.wiki_page_builder import (
                                    remove_source_page_from_wikis,
                                )
                                from app.core.knowledge_compiler_v3.v4_graph_builder import (
                                    remove_page_graph,
                                )
                                source_item = apply_item(db, normalized)
                                source_item.state = "skipped"
                                source_item.last_error = NEEDS_REASSIGN_REASON
                                source_item.metadata_hash = None
                                db.flush()
                                remove_source_page_from_wikis(db, existing_page.id, commit=False)
                                remove_page_graph(db, existing_page.id, commit=False)
                                run = db.get(SourceSyncRun, run.id)
                                run.unchanged_count = int(run.unchanged_count or 0) + 1
                                run.heartbeat_at = datetime.now()
                                record_error(
                                    db, run.id, change.external_id, "persist",
                                    NEEDS_REASSIGN_CODE, NEEDS_REASSIGN_REASON,
                                    retryable=False,
                                )
                                db.commit()
                            except Exception as reassign_exc:  # noqa: BLE001
                                db.rollback()
                                raise reassign_exc
                            heartbeat(db, run.id)
                            continue
                    notebook = effective_notebook
                else:
                    # 已删除文件：原 Page 已删除/退役，不需要目标 Notebook。
                    notebook = None

                decision = decide(normalized, existing)
                page = None
                deleted_page_id = None
                metadata_title_changed = False
                retry_derived = False
                # Phase 2.1：转换先行。create/restore/content_changed 必须先得到 CanonicalNote，
                # 成功才推进 SourceItem 并原子写 Page；失败不推进 hash/version。
                if decision.action in ("create", "restore", "content_changed"):
                    note, conv_code, conv_msg, conv_summary, retryable = _convert_note(
                        normalized, connection.connector_key
                    )
                    if note is None:
                        # failed/blocked：不创建/覆盖 Page；不推进 SourceItem 的
                        # content_hash/metadata_hash/external_version；
                        # 只提交 SourceSyncError + run 统计。
                        record_error(
                            db, run.id, change.external_id, "convert",
                            conv_code or "conversion_failed", conv_msg,
                            retryable=retryable,
                        )
                        run = db.get(SourceSyncRun, run.id)
                        run.failed_count = int(run.failed_count or 0) + 1
                        run.heartbeat_at = datetime.now()
                        db.commit()
                        heartbeat(db, run.id)
                        continue
                    # 转换成功：原子推进 SourceItem + Page（同一输入版本）。
                    source_item = apply_item(db, normalized)
                    db.flush()
                    page = _upsert_page(db, connection, notebook, source_item, normalized, note)
                    if page is not None and page.id:
                        changed_page_ids.add(page.id)
                elif decision.action == "metadata_only":
                    source_item = apply_item(db, normalized)
                    db.flush()
                    if source_item.page_id:
                        page = db.get(Page, source_item.page_id)
                        if page:
                            metadata_title_changed = (normalized.title or "") != (page.title or "")
                            page.title = normalized.title or page.title
                            page.source_path = normalized.source_path
                            page.source_url = normalized.source_url
                            if metadata_title_changed:
                                page.wiki_dirty = True
                                changed_page_ids.add(page.id)
                    # Phase 2.5：metadata_only 必要操作成功
                    decision_succeeded = True
                elif decision.action == "delete":
                    source_item = apply_item(db, normalized)
                    db.flush()
                    _retire_deleted_page(db, source_item)
                    deleted_page_id = source_item.page_id  # 记录，commit 后再调度
                    # Phase 2.5：delete 必要操作成功
                    decision_succeeded = True
                else:
                    # unchanged / skip：只更新 SourceItem 检查时间，不写 Page。
                    source_item = apply_item(db, normalized)
                    db.flush()
                    # Phase 2.2：索引/Evidence 失败恢复——若关联 Page index_dirty=True
                    # （派生未完成），同版本下一轮重试 index→evidence→graph（不重转换）。
                    # Phase 2.3：必须同时把 page 指向 pending_page（后续派生用其 id）。
                    if decision.action == "unchanged" and source_item.page_id:
                        pending_page = db.get(Page, source_item.page_id)
                        if pending_page is not None and pending_page.index_dirty:
                            page = pending_page
                            retry_derived = True
                        else:
                            retry_derived = False
                    else:
                        retry_derived = False
                    # Phase 2.5：普通 unchanged（无派生恢复）必要操作成功 → 计入 unchanged_count。
                    # 有派生恢复（retry_derived）时由派生分支成功后再置 True；失败则 except 只 failed_count。
                    if not retry_derived:
                        decision_succeeded = True

                # Phase 2.5 统计语义：created/updated/unchanged/deleted = 关键处理链成功后的决策计数；
                # failed_count = 任一关键阶段失败；互斥（同一条目不同时计入成功计数与 failed_count）。
                # decision_succeeded 由各决策分支在关键链成功后置 True；except 分支只加 failed_count。
                run = db.get(SourceSyncRun, run.id)
                run.heartbeat_at = datetime.now()
                db.commit()

                # V4 Phase C：删除来源 Page 在主事务 commit 后再移除 Wiki 来源；
                # 若主事务回滚，Wiki 来源不变。
                if deleted_page_id:
                    try:
                        from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import schedule_page_deleted
                        schedule_page_deleted(deleted_page_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("schedule wiki deleted failed page=%s: %s", deleted_page_id, exc)

                if page is not None and (
                    decision.action in ("create", "restore", "content_changed") or retry_derived
                ):
                    # Phase 2.2：retry_derived=True（unchanged 但 index_dirty）时只重试派生，
                    # 不重复调用 CanonicalNote（不重转换）。
                    derived_started = True  # Phase 2.3：派生阶段实际开始
                    _set_stage(db, run.id, "index", f"索引与证据：{normalized.title or change.external_id}")
                    from app.api.pages import background_index_page
                    # 1. 先完成 PageChunk 写入（schedule_graph=False：不在索引内部调度，
                    #    由 executor 在 Evidence 同步完成后只调度一次）。
                    await background_index_page(page.id, page.title, page.content, schedule_graph=False)
                    # 2. 再同步 Evidence（失败则抛异常，由外层 except 处理，不调度图谱）。
                    sync_page_evidence(db, page.id)
                    # 3. 最后只调度一次图谱重建（Chunk + Evidence 均完成后）。
                    _schedule_graph_rebuild(page.id)
                    # V4 Phase C：数据源新增/更新后调度 Wiki 增量刷新（不阻塞同步）。
                    # Phase 2.5：changed 显式 True（create/restore/content_changed 与 retry_derived
                    # 都是新内容，需刷新；普通 unchanged 不在此分支）。
                    _schedule_wiki_refresh_for_page(db, page.id, decision.action, changed=True)
                    # Phase 2.4：retry_derived 成功后加入 changed_page_ids（触发等价 debt 通知）。
                    # 幂等：changed_page_ids 为 set，重复加入无副作用。
                    changed_page_ids.add(page.id)
                    # Phase 2.5：关键派生链（index/evidence/graph/Wiki）全部成功 → 计入成功计数。
                    decision_succeeded = True
                elif page is not None and metadata_title_changed:
                    # 仅标题/路径变化：可能影响实体/版本/version_family，调度图谱重建；
                    # 不重算索引/证据（标题不影响 Embedding）。
                    _schedule_graph_rebuild(page.id)
                    # V4 Phase G：不触发 Card 编译（旧编译 flag 不再可达）。
                    _schedule_wiki_refresh_for_page(db, page.id, "metadata_only", changed=True)
                    decision_succeeded = True
            except Exception as exc:
                db.rollback()
                run = db.get(SourceSyncRun, run.id)
                run.failed_count = int(run.failed_count or 0) + 1
                record_error(
                    db, run.id, change.external_id, "persist",
                    "ACL_FAILED" if isinstance(exc, PermissionError) else "ITEM_SYNC_FAILED",
                    str(exc), retryable=not isinstance(exc, (PermissionError, ValueError)),
                )
                # Phase 2.3：仅派生阶段实际开始（derived_started）后才恢复 dirty。
                # fetch/ACL/path mapping/转换异常不得执行派生恢复逻辑。
                if derived_started and page is not None:
                    pending = db.get(Page, page.id)
                    if pending is not None:
                        pending.index_dirty = True
                        logger.warning("derived stage failed, mark page=%s index_dirty=True", page.id)
                db.commit()
                logger.exception("source item failed run=%s external_id=%s", run.id, change.external_id)

            # Phase 2.5：仅关键处理链成功（decision_succeeded）才增加成功计数；失败只 failed_count。
            if decision_succeeded:
                counter = {
                    "create": "created_count",
                    "restore": "updated_count",
                    "content_changed": "updated_count",
                    "metadata_only": "updated_count",
                    "unchanged": "unchanged_count",
                    "skip": "unchanged_count",
                    "delete": "deleted_count",
                }[decision.action]
                run = db.get(SourceSyncRun, run.id)
                setattr(run, counter, int(getattr(run, counter) or 0) + 1)
                run.heartbeat_at = datetime.now()
                db.commit()
            heartbeat(db, run.id)

        run = db.get(SourceSyncRun, run.id)
        if should_stop(db, run.id):
            finish_run(db, run.id, "cancelled", "任务已在安全检查点停止")
        elif int(run.failed_count or 0) > 0:
            finish_run(db, run.id, "failed", f"{run.failed_count} 个条目失败")
        else:
            # 保存真实远端游标（钉钉为 last_inventory_at；其余 fallback 时间戳）
            cursor_after = new_cursor or {
                "last_run_at": datetime.now().isoformat(),
                "mode": run.mode,
            }
            connection = db.get(SourceConnection, run.connection_id)
            connection.cursor_json = json.dumps(cursor_after, ensure_ascii=False)
            connection.last_success_at = datetime.now()
            run = db.get(SourceSyncRun, run.id)
            run.cursor_after_json = json.dumps(cursor_after, ensure_ascii=False)
            run.stage = "done"
            db.commit()
            finish_run(db, run.id, "succeeded")
            # V4 Phase F：数据源同步成功后，仅通知本次真正新增/变化的 Page（失败不影响同步）。
            try:
                from app.core.retrieval.debt_service import notify_knowledge_changed_for_page
                for pid in changed_page_ids:
                    notify_knowledge_changed_for_page(db, pid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("debt revalidate after source sync failed: %s", exc)
    except Exception as exc:
        db.rollback()
        finish_run(db, run.id, "failed", str(exc))
        logger.exception("source run failed run=%s", run.id)
