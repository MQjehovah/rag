"""Phase 4.1：KnowledgeCompileRun 串行执行引擎（executor）。

职责：
- create_run：建 queued run。含 Phase 4.1/4.2 增强：
  - 创建目标身份校验（workspace/wiki_page/source_sync_run/trigger 组合），
    校验失败抛 CompileRunError（错误码前缀，_http_error 映射 400/404/409）；
  - 幂等 fingerprint：同 idempotency_key 相同请求指纹 → 返回原 run（幂等）；
    不同指纹 → 409 idempotency_conflict；并发插撞唯一索引 → 回查归一。
- supersede_matching_runs：workspace/trigger/wiki_page 全维 null-safe 隔离抢占；
  被抢占 run 的未终态 stage → skipped + finished_at（消除 orphan stage）。
- execute_run：串行执行 pipeline stage，写 stage 行 + artifact 行 + run 状态；
  attempt 语义由 claim 原子 +1；stage.attempt = run.attempt（claim 后执行序号）。
  对 queued 直调自动走 CAS claim（lease_token 自动生成），语义统一。
- stage_input_hash 缓存：系统确定性计算
  sha256(workspace|pipeline|version|stage|stage_version|schema|input|上游产物哈希链)；
  命中（同 stage_input_hash 且所属 run 已 succeeded）→ 复用标记 cached。
- retry_run / cancel_run：供 API 复用（全部经 state_machine 集中状态机）。

错误分层：内部异常 detail 只写日志；DB/API 可见字段一律净化（Windows 盘符路径、
URL/query、Bearer/Authorization/Token 形态剥离 + 截断），safe_* 列承载。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.database import (
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    NotebookWorkspaceBinding,
    Page,
    SourceSyncRun,
    WikiPage,
    WikiWorkspace,
)
from app.core.wiki_pipeline.registry import (
    FailureTransition,
    StageResult,
    get_pipeline,
    stage_error_message,
    validate_stage_result,
)
from app.core.wiki_pipeline.state_machine import (
    apply_run_status,
    apply_stage_status,
    is_run_terminal,
    is_stage_terminal,
    validate_trigger_type,
)

logger = logging.getLogger(__name__)

# Phase 5：可注入的外部 runner（LLM / 图谱）。stage 不直接 await LLM / 不自行开独立事务，
# 通过 ctx["llm_runner"] / ctx["graph_runner"] 调用；默认走真实实现，测试可替换。
_LLM_RUNNER = None
_GRAPH_RUNNER = None


def configure_external_runners(*, llm_runner=None, graph_runner=None) -> None:
    global _LLM_RUNNER, _GRAPH_RUNNER
    if llm_runner is not None:
        _LLM_RUNNER = llm_runner
    if graph_runner is not None:
        _GRAPH_RUNNER = graph_runner


def reset_external_runners() -> None:
    global _LLM_RUNNER, _GRAPH_RUNNER
    _LLM_RUNNER = None
    _GRAPH_RUNNER = None


ERROR_SUMMARY_TRUNCATE = 300
SAFE_ERROR_TRUNCATE = 200
STAGE_ERROR_CODE_TRUNCATE = 64

# claim 时写入的租约长度：运行时读 settings.wiki_pipeline_lease_seconds（默认 300），
# 与 worker.heartbeat 同源，保证短 lease 不被心跳顶回 300s。
# 配置合法性由 pydantic 在 Settings 构造时校验；此处不再吞异常回退默认值。


def _lease_seconds() -> int:
    """当前租约秒数：直接读 settings（构造时已校验为正整数；非法即无法构造）。"""
    from app.config import settings

    return int(settings.wiki_pipeline_lease_seconds)

# 系统生成的 stage 失败错误码（严格大写 snake，满足 StageResult 契约）。
_STAGE_EXCEPTION_CODE = "STAGE_EXCEPTION"
_STAGE_CONTRACT_VIOLATION_CODE = "STAGE_CONTRACT_VIOLATION"
_STAGE_NO_EXECUTE_CODE = "STAGE_NO_EXECUTE"
_STAGE_STATE_ERROR_CODE = "STAGE_STATE_ERROR"
_STAGE_COMMIT_FORBIDDEN_CODE = "STAGE_COMMIT_FORBIDDEN"


class StageCommitForbidden(RuntimeError):
    """Stage 尝试自行 commit/rollback/close/begin 事务 → 立即阻止（Pipeline 独占事务）。"""


# Stage 禁止访问的事务控制方法/属性（事务边界由 Pipeline Manager 独占）。
_STAGE_FORBIDDEN_NAMES = frozenset({
    "commit", "rollback", "close", "begin", "begin_nested", "invalidate",
    "autobegin", "execute_dml", "transaction", "in_transaction",
    "session_factory", "bind", "twophase", "info", "get_nested_transaction",
})
# Stage 允许访问的 Session 方法（读/写实体 + flush，不触达事务边界）。
# 注意：不暴露 execute/scalars/scalar/connection/get_bind/merge——它们返回
# Result/ScalarResult/Connection/Query 等含底层 connection/session 引用的对象，
# 构成事务逃逸面。查询统一走 StageQuery 门面（不暴露 .session/.connection）。
_STAGE_ALLOWED_NAMES = frozenset({
    "add", "add_all", "flush", "delete", "query", "get",
    "refresh", "expire", "expire_all", "expunge",
})


class StageQuery:
    """受限 Query 门面：Stage 只能 filter/读行，无法触达 Session/Connection。

    - 暴露：filter/filter_by/order_by/limit/offset/first/one/all/count。
    - .session、.statement、.connection、底层 Query 的其它方法一律拒绝
      （raise StageCommitForbidden），堵住经 Query 逃逸到原始 Session 的路径。
    """

    __slots__ = ("_inner",)

    def __init__(self, query):
        object.__setattr__(self, "_inner", query)

    def filter(self, *args, **kwargs):
        return StageQuery(object.__getattribute__(self, "_inner").filter(*args, **kwargs))

    def filter_by(self, *args, **kwargs):
        return StageQuery(object.__getattribute__(self, "_inner").filter_by(*args, **kwargs))

    def order_by(self, *args, **kwargs):
        return StageQuery(object.__getattribute__(self, "_inner").order_by(*args, **kwargs))

    def limit(self, *args, **kwargs):
        return StageQuery(object.__getattribute__(self, "_inner").limit(*args, **kwargs))

    def offset(self, *args, **kwargs):
        return StageQuery(object.__getattribute__(self, "_inner").offset(*args, **kwargs))

    def first(self):
        return object.__getattribute__(self, "_inner").first()

    def one(self):
        return object.__getattribute__(self, "_inner").one()

    def all(self):
        return object.__getattribute__(self, "_inner").all()

    def count(self):
        return object.__getattribute__(self, "_inner").count()

    def __getattr__(self, name: str):
        raise StageCommitForbidden(f"stage must not access query.{name}")

    def __getattribute__(self, name: str):
        if name in ("_inner", "_STAGE_FORBIDDEN_NAMES", "_STAGE_ALLOWED_NAMES"):
            raise StageCommitForbidden(f"stage must not access query.{name}")
        return object.__getattribute__(self, name)


class StageSession:
    """受限 Session 门面：Stage.execute 只能读/写实体与 flush，不得 commit/rollback。

    - 允许 add/add_all/flush/query(StageQuery)/get/refresh/delete/expire/expunge；
    - commit/rollback/close/begin/begin_nested/invalidate/__enter__/__exit__ 抛
      StageCommitForbidden（事务边界由 Pipeline Manager 独占）。
    - 不暴露 execute/scalars/scalar/connection/get_bind/merge 等可返回底层
      Connection/Result/Query 的方法（事务逃逸面已关闭）。
    - 白名单外任何属性/方法默认拒绝（防通过 __getattr__/__getattribute__ 绕过，
      也无法访问到底层 Session 引用）。
    - Stage 产生的未提交写入由 Pipeline Manager 统一提交或回滚。
    - 注意：Stage 是受信本地组件，不是安全沙箱；Phase 6 Skill 不获得 StageSession，
      Skill 只能获得不可变输入与 Evidence DTO。
    """

    __slots__ = ("_inner",)

    def __init__(self, session: Session):
        object.__setattr__(self, "_inner", session)

    # ---- 明确允许（读/写实体、flush，不触达事务边界） ----
    def add(self, instance):
        return object.__getattribute__(self, "_inner").add(instance)

    def add_all(self, instances):
        return object.__getattribute__(self, "_inner").add_all(instances)

    def flush(self, *args, **kwargs):
        return object.__getattribute__(self, "_inner").flush(*args, **kwargs)

    def delete(self, instance):
        return object.__getattribute__(self, "_inner").delete(instance)

    def query(self, *entities, **kwargs):
        # 返回受限 StageQuery，不暴露底层 Query（.session/.statement 不可达）。
        raw = object.__getattribute__(self, "_inner").query(*entities, **kwargs)
        return StageQuery(raw)

    def get(self, *args, **kwargs):
        return object.__getattribute__(self, "_inner").get(*args, **kwargs)

    def refresh(self, instance, *args, **kwargs):
        return object.__getattribute__(self, "_inner").refresh(instance, *args, **kwargs)

    def expire_all(self):
        return object.__getattribute__(self, "_inner").expire_all()

    def expire(self, instance, *args, **kwargs):
        return object.__getattribute__(self, "_inner").expire(instance, *args, **kwargs)

    def expunge(self, instance):
        return object.__getattribute__(self, "_inner").expunge(instance)

    # ---- 事务控制 / 生命周期：一律禁止 ----
    def commit(self):  # noqa: D102
        raise StageCommitForbidden("stage must not commit; transaction owned by pipeline")

    def rollback(self):  # noqa: D102
        raise StageCommitForbidden("stage must not rollback; transaction owned by pipeline")

    def close(self):  # noqa: D102
        raise StageCommitForbidden("stage must not close session")

    def begin(self, *args, **kwargs):  # noqa: ARG002
        raise StageCommitForbidden("stage must not begin transaction")

    def begin_nested(self, *args, **kwargs):  # noqa: ARG002
        raise StageCommitForbidden("stage must not begin nested transaction")

    def invalidate(self, *args, **kwargs):  # noqa: ARG002
        raise StageCommitForbidden("stage must not invalidate session")

    def __enter__(self):
        raise StageCommitForbidden("stage must not use session context manager")

    def __exit__(self, *args, **kwargs):  # noqa: ARG002
        raise StageCommitForbidden("stage must not use session context manager")

    def __getattr__(self, name: str):
        # 白名单外任何访问（含 _inner、底层 Session 方法/属性）一律默认拒绝。
        if name in _STAGE_ALLOWED_NAMES:
            return getattr(self._inner, name)
        raise StageCommitForbidden(f"stage must not access {name}")

    def __getattribute__(self, name: str):
        # 仅放行本门面显式定义的方法与受控白名单；其余（含 _inner）一律拒绝，
        # 确保 Stage 无法通过属性访问触及底层 Session 或任何事务句柄。
        if name in ("_inner", "_STAGE_FORBIDDEN_NAMES", "_STAGE_ALLOWED_NAMES"):
            raise StageCommitForbidden(f"stage must not access {name}")
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_inner":
            object.__setattr__(self, "_inner", value)
            return
        raise StageCommitForbidden(f"stage must not set attribute {name}")


class CompileRunError(RuntimeError):
    """编译 run 业务错误（如 unknown pipeline / 非法状态操作）。"""


# 身份校验错误码 → HTTP 状态（api._http_error 同表集中映射）。
TARGET_ERROR_HTTP = {
    "workspace_not_found": 404,
    "wiki_page_not_found": 404,
    "source_sync_run_not_found": 404,
    "workspace_not_active": 409,
    "wiki_page_workspace_mismatch": 409,
    "page_workspace_mismatch": 409,
    "invalid_trigger_targets": 400,
    "workspace_required": 400,
    "idempotency_conflict": 409,
    "retry_attempts_exhausted": 409,
    "run_superseded_use_create": 409,
    "pipeline_version_not_registered": 400,
}


# ---------------------------------------------------------------------------
# 确定性哈希 / 净化
# ---------------------------------------------------------------------------


def compute_input_hash(
    *,
    pipeline_key: str,
    trigger_type: str,
    trigger_object_id: str | None = None,
    wiki_page_id: str | None = None,
    workspace_id: str | None = None,
    source_sync_run_id: str | None = None,
) -> str:
    """确定性 run 输入哈希：同 (pipeline, trigger, target) 幂等。"""
    parts = [
        pipeline_key,
        trigger_type,
        trigger_object_id or "",
        wiki_page_id or "",
        workspace_id or "",
        source_sync_run_id or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def compute_request_fingerprint(
    *,
    workspace_id: str | None = None,
    pipeline_key: str = "",
    pipeline_version: str = "",
    trigger_type: str = "",
    trigger_object_id: str | None = None,
    wiki_page_id: str | None = None,
    source_sync_run_id: str | None = None,
    input_hash: str = "",
) -> str:
    """请求幂等指纹：对规范化请求字段排序后 sha256。

    用于 idempotency_key 语义增强：同 key 同指纹 = 同一请求（幂等返回）；
    同 key 异指纹 = 不同请求误用同一 key（409 冲突）。null 一律空串。
    """
    fields = sorted(
        (
            f"workspace_id={workspace_id or ''}",
            f"pipeline_key={pipeline_key or ''}",
            f"pipeline_version={pipeline_version or ''}",
            f"trigger_type={trigger_type or ''}",
            f"trigger_object_id={trigger_object_id or ''}",
            f"wiki_page_id={wiki_page_id or ''}",
            f"source_sync_run_id={source_sync_run_id or ''}",
            f"input_hash={input_hash or ''}",
        )
    )
    return hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()


def _sanitize_for_api(text: str | None) -> str | None:
    """剥离内部细节（Windows 盘符路径 / URL / Bearer / Authorization / Token / SQL），截 200。

    供 safe_* 列与 API 可见错误使用；内部异常 detail 一律走 logger 不进 API 字段。
    """
    if not text:
        return text
    value = text
    patterns = [
        (r"[A-Za-z]:\\[^\s]*", "[path]"),
        (r"https?://[^\s]+", "[url]"),
        (r"(?i)bearer\s+\S+", "Bearer [redacted]"),
        (r"(?i)authorization\s*[:=]\s*\S+", "Authorization [redacted]"),
        (r"(?i)token\s*\S+", "Token [redacted]"),
        (
            r"(?i)\b(select\s|insert\s+into\s|update\s|delete\s+from\s|drop\s+table\s|"
            r"alter\s+table\s|create\s+table\s|truncate\s+table\s)[^\n]*$",
            "[sql]",
        ),
    ]
    for pattern, repl in patterns:
        value = re.sub(pattern, repl, value)
    return value[:SAFE_ERROR_TRUNCATE] if len(value) > SAFE_ERROR_TRUNCATE else value


def _now() -> datetime:
    return datetime.now()


def _truncate(value: str | None, limit: int) -> str | None:
    if not value:
        return value
    return value if len(value) <= limit else value[:limit] + "…"


def _truncate_code(value: str | None) -> str | None:
    if not value:
        return value
    return value if len(value) <= STAGE_ERROR_CODE_TRUNCATE else value[:STAGE_ERROR_CODE_TRUNCATE]


# ---------------------------------------------------------------------------
# 身份校验
# ---------------------------------------------------------------------------


def _resolve_active_workspace_for_page(db: Session, page_id: str) -> str | None:
    """Page（pages 表）→ 其 Notebook 的 active binding → active workspace id。

    无 Page / 无 notebook_id / 无 active binding / binding workspace 非 active →
    None（归属不可验证）。soft-deleted Page（物理删除即无行）同样不可验证 → None。
    """
    page = db.query(Page).filter(Page.id == page_id).first()
    if page is None or not page.notebook_id:
        return None
    binding = (
        db.query(NotebookWorkspaceBinding)
        .filter(
            NotebookWorkspaceBinding.notebook_id == page.notebook_id,
            NotebookWorkspaceBinding.status == "active",
        )
        .first()
    )
    if binding is None:
        return None
    ws = db.get(WikiWorkspace, binding.workspace_id)
    if ws is None or ws.status != "active":
        return None
    return ws.id


def _validate_create_targets(
    db: Session,
    *,
    workspace_id: str | None,
    wiki_page_id: str | None,
    source_sync_run_id: str | None,
    trigger_type: str,
    trigger_object_id: str | None,
) -> tuple[str | None, str | None]:
    """校验 create_run 目标身份，返回 (错误码 or None, 生效 workspace_id)。

    - workspace_id 非空 → WikiWorkspace 必须存在（404）且 status=active（409）。
    - wiki_page_id 非空 → WikiPage 必须存在（404）；其 workspace_id 必须非空且
      严格等于 effective workspace —— NULL 不能跳过比较（409
      wiki_page_workspace_mismatch）。effective 为空时从 wiki_page.workspace_id 推导。
    - page_changed/page_deleted：trigger_object_id 对应 Page 必须通过 Notebook
      active binding 归属 effective workspace（409 page_workspace_mismatch）。
      Page 物理删除/无绑定/绑定 workspace 非 active → 归属不可验证 → 拒绝
      （无 Page 快照列，本项目不支持无归属删除触发）。
    - source_sync_run_id 非空 → SourceSyncRun 必须存在（404）。
    - trigger 组合：page_changed/page_deleted 需 trigger_object_id；
      manual_edit 需 wiki_page_id（400）。
    """
    if trigger_type in ("page_changed", "page_deleted") and not trigger_object_id:
        return "invalid_trigger_targets", workspace_id
    if trigger_type == "manual_edit" and not wiki_page_id:
        return "invalid_trigger_targets", workspace_id

    effective_ws = workspace_id
    if workspace_id:
        ws = db.query(WikiWorkspace).filter(WikiWorkspace.id == workspace_id).first()
        if ws is None:
            return "workspace_not_found", workspace_id
        if ws.status != "active":
            return "workspace_not_active", workspace_id
    if wiki_page_id:
        page = db.query(WikiPage).filter(WikiPage.id == wiki_page_id).first()
        if page is None:
            return "wiki_page_not_found", effective_ws
        # wiki_page.workspace_id 必须非空且严格等于 effective（NULL 不能跳过比较）。
        if not page.workspace_id:
            return "wiki_page_workspace_mismatch", effective_ws
        if effective_ws is None:
            effective_ws = page.workspace_id
        elif page.workspace_id != effective_ws:
            return "wiki_page_workspace_mismatch", effective_ws
    if trigger_type in ("page_changed", "page_deleted") and trigger_object_id:
        page_ws = _resolve_active_workspace_for_page(db, trigger_object_id)
        if page_ws is None or (effective_ws is not None and page_ws != effective_ws):
            return "page_workspace_mismatch", effective_ws
        if effective_ws is None:
            effective_ws = page_ws
    if source_sync_run_id:
        ssr = db.query(SourceSyncRun).filter(SourceSyncRun.id == source_sync_run_id).first()
        if ssr is None:
            return "source_sync_run_not_found", effective_ws
    return None, effective_ws


# ---------------------------------------------------------------------------
# create_run / supersede / retry / cancel
# ---------------------------------------------------------------------------


def _run_fingerprint_from_row(existing: CompileRun) -> str:
    """对既有 run 行重算请求指纹（其存储字段即创建时的生效字段）。"""
    return compute_request_fingerprint(
        workspace_id=existing.workspace_id,
        pipeline_key=existing.pipeline_key,
        pipeline_version=existing.pipeline_version,
        trigger_type=existing.trigger_type,
        trigger_object_id=existing.trigger_object_id,
        wiki_page_id=existing.wiki_page_id,
        source_sync_run_id=existing.source_sync_run_id,
        input_hash=existing.input_hash or "",
    )


def _find_run_by_idempotency_key(db: Session, key: str) -> CompileRun | None:
    return db.query(CompileRun).filter(CompileRun.idempotency_key == key).first()


def _match_idempotent_run(db: Session, existing: CompileRun, fingerprint: str, key: str) -> CompileRun:
    """同 idempotency_key 命中：指纹一致 → 幂等返回原 run；不一致 → 409。"""
    if _run_fingerprint_from_row(existing) == fingerprint:
        return existing
    raise CompileRunError(f"idempotency_conflict: {key}")


def create_run(
    db: Session,
    *,
    pipeline_key: str,
    trigger_type: str,
    trigger_object_id: str | None = None,
    source_sync_run_id: str | None = None,
    workspace_id: str | None = None,
    wiki_page_id: str | None = None,
    pipeline_version: str | None = None,
    input_hash: str | None = None,
    idempotency_key: str | None = None,
    supersede_same_trigger: bool = False,
    created_by: str | None = None,
) -> CompileRun:
    """创建 queued run（含身份校验 + 幂等指纹 + 可选抢占）。

    - 身份校验失败 → CompileRunError(f"{code}: {detail}")。Phase 4.2 补全：
      wiki_page.workspace_id 必须非空且严格等于 effective（NULL 不跳过）；
      page_changed/page_deleted 的 trigger_object_id（Page）须经 Notebook active
      binding 归属 effective workspace；allow_null_workspace=False 的产品流水线
      最终 workspace 不得为 None（workspace_required，400）。
    - idempotency_key 命中：指纹一致返回原 run（幂等）；不一致 → 409。
      并发插撞唯一索引：rollback → 回查归一（同指纹幂等 / 异指纹 409）。
    - supersede_same_trigger=True：同 pipeline_key + trigger_type +
      trigger_object_id + null-safe(workspace_id, wiki_page_id) 的非终态旧 run
      置 superseded（workspace 隔离）。
    不提交（由调用方 commit），保证测试/API 事务边界一致。
    """
    reason = validate_trigger_type(trigger_type)
    if reason:
        raise CompileRunError(reason)
    # 未指定版本 → 解析 active version；指定版本 → 必须精确注册（否则拒绝，不建 run）。
    if pipeline_version is not None:
        exact = get_pipeline(pipeline_key, pipeline_version)
        if exact is None:
            raise CompileRunError(f"pipeline_version_not_registered={pipeline_key}:{pipeline_version}")
    pipeline = get_pipeline(pipeline_key, pipeline_version)
    if pipeline is None:
        raise CompileRunError(f"pipeline_not_registered={pipeline_key}:{pipeline_version or ''}")

    target_error, effective_ws = _validate_create_targets(
        db,
        workspace_id=workspace_id,
        wiki_page_id=wiki_page_id,
        source_sync_run_id=source_sync_run_id,
        trigger_type=trigger_type,
        trigger_object_id=trigger_object_id,
    )
    if target_error:
        raise CompileRunError(f"{target_error}: {pipeline_key}/{trigger_type}")
    # 产品流水线（allow_null_workspace=False）最终 workspace 必须非空。
    if effective_ws is None and not pipeline.allow_null_workspace:
        raise CompileRunError(f"workspace_required: {pipeline_key}/{trigger_type}")

    effective_version = pipeline_version or pipeline.version
    effective_hash = input_hash or compute_input_hash(
        pipeline_key=pipeline_key,
        trigger_type=trigger_type,
        trigger_object_id=trigger_object_id,
        wiki_page_id=wiki_page_id,
        workspace_id=effective_ws,
        source_sync_run_id=source_sync_run_id,
    )
    fingerprint = compute_request_fingerprint(
        workspace_id=effective_ws,
        pipeline_key=pipeline_key,
        pipeline_version=effective_version,
        trigger_type=trigger_type,
        trigger_object_id=trigger_object_id,
        wiki_page_id=wiki_page_id,
        source_sync_run_id=source_sync_run_id,
        input_hash=effective_hash,
    )

    if idempotency_key:
        existing = _find_run_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return _match_idempotent_run(db, existing, fingerprint, idempotency_key)

    if trigger_object_id and supersede_same_trigger:
        supersede_matching_runs(
            db,
            pipeline_key=pipeline_key,
            trigger_type=trigger_type,
            trigger_object_id=trigger_object_id,
            workspace_id=effective_ws,
            wiki_page_id=wiki_page_id,
        )

    run = CompileRun(
        pipeline_key=pipeline_key,
        pipeline_version=effective_version,
        trigger_type=trigger_type,
        trigger_object_id=trigger_object_id,
        source_sync_run_id=source_sync_run_id,
        workspace_id=effective_ws,
        wiki_page_id=wiki_page_id,
        status="queued",
        idempotency_key=idempotency_key,
        input_hash=effective_hash,
        attempt=0,
        max_attempts=3,
        cancel_requested=False,
        created_by=created_by,
        created_at=_now(),
    )
    db.add(run)
    try:
        db.flush()
    except IntegrityError:
        # 并发撞 idempotency_key 唯一索引：回滚 → 回查归一。
        db.rollback()
        if idempotency_key:
            existing = _find_run_by_idempotency_key(db, idempotency_key)
            if existing is not None:
                return _match_idempotent_run(db, existing, fingerprint, idempotency_key)
        raise
    return run


def _null_safe_filter(column, value: str | None):
    if value is None:
        return column.is_(None)
    return column == value


def supersede_matching_runs(
    db: Session,
    *,
    pipeline_key: str,
    trigger_type: str,
    trigger_object_id: str,
    wiki_page_id: str | None = None,
    workspace_id: str | None = None,
) -> list[str]:
    """把同维度（null-safe workspace/wiki_page + pipeline + trigger）非终态旧 run 置
    superseded，并闭合其未终态 stage（→ skipped + finished_at）。

    不跨 workspace 抢占。终态（succeeded/cancelled/superseded）不被抢占。
    返回被置 superseded 的 run id 列表。
    """
    superseded: list[str] = []
    if not trigger_object_id:
        return superseded
    old_runs = (
        db.query(CompileRun)
        .filter(
            CompileRun.pipeline_key == pipeline_key,
            CompileRun.trigger_type == trigger_type,
            CompileRun.trigger_object_id == trigger_object_id,
            _null_safe_filter(CompileRun.workspace_id, workspace_id),
            _null_safe_filter(CompileRun.wiki_page_id, wiki_page_id),
        )
        .all()
    )
    for old in old_runs:
        if old.status not in ("queued", "running", "failed"):
            continue
        _close_unfinished_stages(db, old, "skipped")
        reason = apply_run_status(old, "superseded")
        if reason:
            logger.warning("supersede rejected run=%s reason=%s", old.id, reason)
            continue
        old.finished_at = _now()
        superseded.append(old.id)
    if superseded:
        db.flush()
    return superseded


def requeue_failed_run(db: Session, run: CompileRun, created_by: str | None = None) -> CompileRun:
    """retry：failed 且 attempt < max_attempts → failed→queued（不加 attempt）。

    attempt 计数由下一次 claim 原子 +1。attempt 已满 → 抛 retry_attempts_exhausted。
    调用方负责 commit。
    """
    if run.status != "failed":
        raise CompileRunError(f"retry_requires_failed_status={run.status}")
    if run.attempt >= run.max_attempts:
        raise CompileRunError("retry_attempts_exhausted")
    reason = apply_run_status(run, "queued")
    if reason:
        raise CompileRunError(reason)
    run.cancel_requested = False
    run.error_summary = None
    run.safe_error_code = None
    run.safe_error_message = None
    run.finished_at = None
    run.started_at = None
    run.heartbeat_at = None
    run.lease_token = None
    run.worker_id = None
    run.lease_expires_at = None
    if created_by:
        run.created_by = created_by
    return run


def retry_run(db: Session, run_id: str, created_by: str | None = None) -> CompileRun:
    """API retry 语义：failed 且 attempt<max → 复用 run（不加 attempt，下次 claim +1）。

    attempt 已满 → 409 retry_attempts_exhausted；superseded → 409
    run_superseded_use_create（取消旧逻辑的自动新建 run，避免绕开 attempt 上限）。
    返回（同）queued run；调用方负责 commit。
    """
    run = db.query(CompileRun).filter(CompileRun.id == run_id).first()
    if run is None:
        raise CompileRunError("run_not_found")
    if run.status == "failed":
        return requeue_failed_run(db, run, created_by=created_by)
    if run.status == "superseded":
        raise CompileRunError("run_superseded_use_create")
    raise CompileRunError(f"run_not_retryable_status={run.status}")


def cancel_run(db: Session, run_id: str) -> CompileRun:
    """API cancel 语义：queued 直接 cancelled；running 设 cancel_requested。

    running 的实际取消由 executor/worker 在安全检查点级联执行。
    返回 run；调用方负责 commit。
    """
    run = db.query(CompileRun).filter(CompileRun.id == run_id).first()
    if run is None:
        raise CompileRunError("run_not_found")
    if run.status == "queued":
        _close_unfinished_stages(db, run, "cancelled")
        reason = apply_run_status(run, "cancelled")
        if reason:
            raise CompileRunError(reason)
        run.finished_at = _now()
    elif run.status == "running":
        run.cancel_requested = True
    else:
        raise CompileRunError(f"run_not_cancellable_status={run.status}")
    return run


def claim_by_id(
    db: Session,
    run_id: str,
    worker_id: str | None = None,
) -> tuple[str | None, CompileRun | None]:
    """唯一 CAS 领取入口：queued → running（attempt 原子 +1，写 lease/worker）。

    CAS UPDATE 外层显式含 status=='queued' AND cancel_requested IS NOT TRUE AND
    attempt < max_attempts AND id == run_id。rowcount==1 → commit + 回查
    (run_id + lease_token + worker_id + status=running) 返回 claimed run；
    rowcount==0 → 该 run 若 queued 且 attempt>=max → 单独 UPDATE 归一
    failed(retry_exhausted)。

    返回 (reason or None, claimed run)：
    - (None, run)              领取成功
    - ("retry_exhausted", None) attempt 已满（已归一 failed）
    - ("claim_lost", None)      被抢占 / 已非 queued（非 exhausted）
    worker_id 缺省（execute_run 直调）→ "direct:{pid}:{uuid}" 标记来源。
    """
    token = uuid.uuid4().hex
    wid = worker_id or f"direct:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    now = _now()
    expires = now + timedelta(seconds=_lease_seconds())
    res = db.execute(
        update(CompileRun)
        .where(
            CompileRun.id == run_id,
            CompileRun.status == "queued",
            CompileRun.cancel_requested.is_not(True),
            CompileRun.attempt < CompileRun.max_attempts,
        )
        .values(
            status="running",
            lease_token=token,
            worker_id=wid,
            lease_expires_at=expires,
            started_at=now,
            heartbeat_at=now,
            attempt=CompileRun.attempt + 1,
        ),
        execution_options={"synchronize_session": False},
    )
    if res.rowcount == 1:
        db.commit()
        db.expire_all()
        claimed = (
            db.query(CompileRun)
            .filter(
                CompileRun.id == run_id,
                CompileRun.status == "running",
                CompileRun.lease_token == token,
                CompileRun.worker_id == wid,
            )
            .first()
        )
        if claimed is not None:
            return (None, claimed)
        return ("claim_lost", None)
    # CAS 未命中：回查决定归一还是让位（attempt 精确上限，绝不产生 attempt=max+1）。
    db.rollback()
    db.expire_all()
    row = db.query(CompileRun).filter(CompileRun.id == run_id).first()
    if row is not None and row.status == "queued":
        if row.attempt >= row.max_attempts:
            now2 = _now()
            db.execute(
                update(CompileRun)
                .where(CompileRun.id == run_id, CompileRun.status == "queued")
                .values(
                    status="failed",
                    safe_error_code="retry_exhausted",
                    safe_error_message=stage_error_message("RETRY_EXHAUSTED"),
                    error_summary=stage_error_message("RETRY_EXHAUSTED"),
                    finished_at=now2,
                    heartbeat_at=now2,
                ),
                execution_options={"synchronize_session": False},
            )
            db.commit()
            return ("retry_exhausted", None)
    return ("claim_lost", None)


def _check_lease(db: Session, run: CompileRun) -> bool:
    """只读 fencing（stage 开始前预检用）：run 仍 running 且 lease_token/worker_id
    匹配且租约未过期。结果持久化前的强 fencing 见 _fence_run（原子条件 UPDATE）。

    False → run 已被 recovery / cancel / supersede 易主（token/status 改变），
    本 worker 必须停止写入，保持 running 交由恢复收敛。
    """
    if run is None:
        return False
    now = _now()
    row = (
        db.query(CompileRun)
        .filter(
            CompileRun.id == run.id,
            CompileRun.status == "running",
            CompileRun.lease_token == run.lease_token,
            CompileRun.worker_id == run.worker_id,
            CompileRun.lease_expires_at > now,
        )
        .first()
    )
    return row is not None


def _fence_run(
    db: Session,
    run: CompileRun,
    *,
    lease_token: str | None = None,
    worker_id: str | None = None,
) -> bool:
    """原子写 fencing：stage 结果持久化前/终态写入前执行的条件 UPDATE。

    UPDATE Run SET heartbeat_at=now
    WHERE id/status='running'/lease_token/worker_id/lease_expires_at>now/
      cancel_requested IS NOT TRUE
    rowcount==1 → 本事务获得有效 fence，随后在同一事务写 StageRun/Artifact/Revision
    并一次性 commit；rowcount==0 → lease 已失效/被易主/已取消，调用方必须
    db.rollback() 丢弃本 stage 全部变更，不得写 artifact/succeeded/Revision。

    lease_token/worker_id：默认取 run 当前值；但 stage 失败路径 reload 后 run 可能
    已是新 worker 的新 lease，因此必须传入**本 worker claim 时捕获的** lease 身份，
    防止旧 worker 借新 lease 写入（见 execute_run）。
    """
    tok = lease_token if lease_token is not None else run.lease_token
    wid = worker_id if worker_id is not None else run.worker_id
    now = _now()
    res = db.execute(
        update(CompileRun)
        .where(
            CompileRun.id == run.id,
            CompileRun.status == "running",
            CompileRun.lease_token == tok,
            CompileRun.worker_id == wid,
            CompileRun.lease_expires_at > now,
            CompileRun.cancel_requested.is_not(True),
        )
        .values(heartbeat_at=now),
        execution_options={"synchronize_session": False},
    )
    return res.rowcount == 1


def _finalize_run_succeeded(
    db: Session,
    run: CompileRun,
    *,
    lease_token: str | None = None,
    worker_id: str | None = None,
) -> bool:
    """Run 最终 succeeded：原子条件 UPDATE（running + 当前 token/worker + lease 未过期
    + 未 cancel → succeeded）。rowcount==1 → 成功；0 → 已被 supersede/cancel/recovery
    接管，调用方 rollback 后交由 recovery。禁止 SELECT 通过后再普通 commit。
    """
    tok = lease_token if lease_token is not None else run.lease_token
    wid = worker_id if worker_id is not None else run.worker_id
    now = _now()
    res = db.execute(
        update(CompileRun)
        .where(
            CompileRun.id == run.id,
            CompileRun.status == "running",
            CompileRun.lease_token == tok,
            CompileRun.worker_id == wid,
            CompileRun.lease_expires_at > now,
            CompileRun.cancel_requested.is_not(True),
        )
        .values(
            status="succeeded",
            error_summary=None,
            safe_error_code=None,
            safe_error_message=None,
            finished_at=now,
            heartbeat_at=now,
            current_stage=None,
        ),
        execution_options={"synchronize_session": False},
    )
    return res.rowcount == 1


def _close_unfinished_stages(db: Session, run: CompileRun, new_status: str, code: str | None = None) -> int:
    """闭合 run 的未终态 stage（公共级联，供 cancel/supersede/worker 复用）。

    - 终态（succeeded/skipped/cancelled）保持不动；
    - failed stage 不可被 cancel/skipped 覆盖（仅可 → queued），保持不动；
    - queued/running stage → new_status + finished_at；转换非法仅警告不改写。
    返回实际闭合数量。
    """
    rows = db.query(StageRun).filter(StageRun.run_id == run.id).all()
    closed = 0
    for row in rows:
        if is_stage_terminal(row.status):
            continue
        if row.status == "failed":
            logger.warning(
                "_close_unfinished_stages keep failed stage=%s run=%s", row.id, run.id
            )
            continue
        reason = apply_stage_status(row, new_status)
        if reason:
            logger.warning("_close_unfinished_stages reject run=%s stage=%s reason=%s", run.id, row.id, reason)
            continue
        row.finished_at = _now()
        if code:
            row.safe_error_code = code
        closed += 1
    return closed


def _fail_run(db: Session, run: CompileRun, stage_key: str, code: str, message: str | None) -> None:
    """run 级失败写入（safe 分层：raw 只进日志，API 可见一律净化）。"""
    reason = apply_run_status(run, "failed")
    if reason:
        logger.warning("run fail rejected run=%s reason=%s", run.id, reason)
        return
    run.current_stage = stage_key or run.current_stage
    safe_message = _sanitize_for_api(message) or _sanitize_for_api(code)
    run.safe_error_code = _truncate_code(code)
    run.safe_error_message = safe_message
    run.error_summary = _truncate(safe_message, ERROR_SUMMARY_TRUNCATE)
    run.finished_at = _now()
    run.heartbeat_at = _now()


def _finalize_cancel(db: Session, run: CompileRun, stage_rows: list[StageRun] | None = None) -> None:
    """run 级 cancel 级联：未终态 stage → cancelled（failed 保持 failed），run → cancelled。"""
    if stage_rows is not None:
        for row in stage_rows:
            if is_stage_terminal(row.status):
                continue
            if row.status == "failed":
                logger.warning("_finalize_cancel keep failed stage=%s run=%s", row.id, run.id)
                continue
            reason = apply_stage_status(row, "cancelled")
            if reason:
                logger.warning("_finalize_cancel reject run=%s stage=%s reason=%s", run.id, row.id, reason)
                continue
            row.finished_at = _now()
    else:
        _close_unfinished_stages(db, run, "cancelled")
    reason = apply_run_status(run, "cancelled")
    if reason:
        logger.warning("run cancel rejected run=%s reason=%s", run.id, reason)
    run.error_summary = "cancelled_by_user"
    run.finished_at = _now()
    run.heartbeat_at = _now()


def compute_stage_input_hash(
    db: Session,
    run: CompileRun,
    sdef,
    *,
    upstream_hashes: list[str],
) -> str:
    """系统确定性计算 stage 输入哈希（缓存身份核心）。

    sha256(workspace_id|pipeline_key|pipeline_version|stage_key|stage_version|
    cache_schema_version|run.input_hash|上游产物 content_hash 有序链)。
    workspace NULL → 空串：仅 NULL 与 NULL 命中（隔离正确）。
    """
    parts = [
        run.workspace_id or "",
        run.pipeline_key,
        run.pipeline_version,
        sdef.key,
        sdef.version,
        sdef.cache_schema_version or "",
        run.input_hash or "",
        *upstream_hashes,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _find_reusable_artifact(
    db: Session, run: CompileRun, sdef, stage_input_hash: str
) -> Artifact | None:
    """stage_input_hash 缓存查找：同 (stage_input_hash, artifact_type) 且
    stage 已 succeeded 且所属 run 已 succeeded → 复用（不重复执行）。

    所有身份维度（workspace/pipeline version/stage key/version/schema/
    run.input_hash/上游链）任一不同 → stage_input_hash 不同 → 不命中。
    """
    return (
        db.query(Artifact)
        .join(StageRun, StageRun.id == Artifact.stage_run_id)
        .join(CompileRun, CompileRun.id == StageRun.run_id)
        .filter(
            CompileRun.id != run.id,
            CompileRun.status == "succeeded",
            StageRun.status == "succeeded",
            StageRun.stage_input_hash == stage_input_hash,
            Artifact.artifact_type == sdef.cache_type,
        )
        .order_by(Artifact.created_at.desc())
        .first()
    )


def _write_artifact(
    db: Session,
    run: CompileRun,
    stage_row: StageRun,
    result: dict,
) -> None:
    artifact_type = result.get("artifact_type")
    if not artifact_type:
        return
    payload = result.get("payload")
    if isinstance(payload, (dict, list)):
        payload_text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    else:
        payload_text = str(payload) if payload is not None else None
    db.add(Artifact(
        run_id=run.id,
        stage_run_id=stage_row.id,
        artifact_type=str(artifact_type)[:32],
        schema_version=result.get("schema_version") or None,
        object_type=result.get("object_type") or None,
        object_id=result.get("object_id") or None,
        content_hash=result.get("content_hash") or None,
        payload_json=payload_text,
        created_at=_now(),
    ))


def compute_artifact_content_hash(
    payload: Any,
    artifact_type: str | None,
    schema_version: str | None,
) -> str | None:
    """系统确定性计算产物内容哈希（不信任 stage 自报）。

    sha256(json.dumps({artifact_type, schema_version, payload}, canonical))。
    artifact_type 缺失（本 stage 不产 artifact）→ 返回 None。
    """
    if not artifact_type:
        return None
    canonical = json.dumps(
        {
            "artifact_type": artifact_type,
            "schema_version": schema_version or None,
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stage_failure(
    code: str,
    *,
    retryable: bool = True,
) -> dict:
    """构造结构化失败结果（error_code 大写 + 服务端固定 safe 文案）。"""
    return {
        "ok": False,
        "error_code": code,
        "error_message": stage_error_message(code),
        "retryable": retryable,
    }


def _normalize_stage_result(raw: Any) -> tuple[dict | None, str]:
    """把 execute 返回值（dict 或 StageResult）规整为 dict 并做契约校验。

    返回 (norm dict, "") 合法；否则 (None, violation)。
    """
    if isinstance(raw, StageResult):
        norm = {
            "ok": raw.ok,
            "error_code": raw.error_code,
            "retryable": raw.retryable,
            "artifact_type": raw.artifact_type,
            "schema_version": raw.schema_version,
            "object_type": raw.object_type,
            "object_id": raw.object_id,
            "metrics": raw.metrics,
            "payload": raw.payload,
            "content_hash": raw.content_hash,
            "output_revision_id": raw.output_revision_id,
            "error_message": raw.error_message,
        }
    elif isinstance(raw, dict):
        norm = dict(raw)
    else:
        return None, "stage_contract_violation: result must be dict or StageResult"
    violation = validate_stage_result(norm)
    if violation:
        return None, violation
    return norm, ""


def _safe_execute(db: Session, run: CompileRun, stage_row: StageRun, sdef, ctx: dict) -> dict:
    """调用 stage.execute，包装异常/契约违规为结构化失败（safe 文案系统映射）。

    - stage.execute 只拿到受限 StageSession（禁 commit/rollback/close/begin）；
      自行事务控制 → StageCommitForbidden → STAGE_COMMIT_FORBIDDEN。
    - 抛异常 → logger.exception；结果 STAGE_EXCEPTION + 固定文案，不落原始异常文本。
    - 返回值非法（validate 拒绝矩阵）→ STAGE_CONTRACT_VIOLATION。
    - 成功 stage：content_hash 系统计算并校验自报一致（不符 → contract_violation）；
      未自报时以系统值回填。output_revision_id 仅 allows_publish stage 可返回。
    """
    if sdef.execute is None:
        return _stage_failure(_STAGE_NO_EXECUTE_CODE)
    try:
        raw = sdef.execute(StageSession(db), run, stage_row, ctx)
    except StageCommitForbidden:
        logger.warning("stage commit forbidden run=%s stage=%s", run.id, sdef.key)
        return _stage_failure(_STAGE_COMMIT_FORBIDDEN_CODE, retryable=bool(sdef.retryable))
    except Exception:  # noqa: BLE001
        logger.exception("stage execute failed run=%s stage=%s", run.id, sdef.key)
        return _stage_failure(_STAGE_EXCEPTION_CODE, retryable=bool(sdef.retryable))
    norm, violation = _normalize_stage_result(raw)
    if violation:
        logger.warning("stage contract violation run=%s stage=%s: %s", run.id, sdef.key, violation)
        return _stage_failure(_STAGE_CONTRACT_VIOLATION_CODE, retryable=bool(sdef.retryable))
    if norm["ok"]:
        if norm.get("output_revision_id") is not None and not sdef.allows_publish:
            logger.warning(
                "stage publish forbidden run=%s stage=%s (allows_publish=False)",
                run.id, sdef.key,
            )
            return _stage_failure(_STAGE_CONTRACT_VIOLATION_CODE, retryable=bool(sdef.retryable))
        # cacheable Stage 必须与 StageDef cache descriptor 严格一致（Phase 4.2.1）。
        if sdef.cachable:
            if norm.get("artifact_type") != sdef.cache_type:
                logger.warning("stage cache artifact_type mismatch run=%s stage=%s", run.id, sdef.key)
                return _stage_failure(_STAGE_CONTRACT_VIOLATION_CODE, retryable=bool(sdef.retryable))
            if norm.get("schema_version") != sdef.cache_schema_version:
                logger.warning("stage cache schema mismatch run=%s stage=%s", run.id, sdef.key)
                return _stage_failure(_STAGE_CONTRACT_VIOLATION_CODE, retryable=bool(sdef.retryable))
            if norm.get("payload") is None:
                logger.warning("stage cache missing payload run=%s stage=%s", run.id, sdef.key)
                return _stage_failure(_STAGE_CONTRACT_VIOLATION_CODE, retryable=bool(sdef.retryable))
        computed = compute_artifact_content_hash(
            norm.get("payload"),
            norm.get("artifact_type"),
            norm.get("schema_version"),
        )
        claimed = norm.get("content_hash")
        if claimed is not None and computed is not None and claimed != computed:
            logger.warning(
                "stage content_hash mismatch run=%s stage=%s claimed=%s computed=%s",
                run.id, sdef.key, claimed, computed,
            )
            return _stage_failure(_STAGE_CONTRACT_VIOLATION_CODE, retryable=bool(sdef.retryable))
        if claimed is None and computed is not None:
            norm = dict(norm)
            norm["content_hash"] = computed  # 系统计算，不信任 stage 自报
    return norm


def _stage_metrics(stage_row: StageRun) -> dict:
    try:
        parsed = json.loads(stage_row.metrics_json or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _record_metrics(db: Session, stage_row: StageRun, metrics: dict) -> None:
    existing = _stage_metrics(stage_row)
    existing.update(metrics or {})
    stage_row.metrics_json = json.dumps(existing, ensure_ascii=False)


def execute_run(db: Session, run_id: str) -> CompileRun:
    """串行执行一个 run（统一 CAS claim + lease fencing + StageResult 应用）。

    - queued run → 统一 claim_by_id（不再自行另一套 UPDATE）；claim 失败且已被
      抢占 → raise run_already_running；attempt 已满 → run 已归一 failed。
    - 幂等：终态 / 同 attempt 已执行 → 直接返回。
    - 事务边界：预建本 attempt stage 行后 commit（释放 run 行锁）；随后逐 stage
      粒度 commit；stage 长执行前后与 run 终态写入前执行 _check_lease（fencing）；
      lease 失效 → 停止写入保持 running 交由 recovery，不写 artifact/succeeded。
    - Stage.execute 抛异常 → logger.exception + STAGE_EXCEPTION（固定文案）；
      契约违规 → STAGE_CONTRACT_VIOLATION；均不落原始异常文本。
    """
    run = db.query(CompileRun).filter(CompileRun.id == run_id).first()
    if run is None:
        raise CompileRunError(f"run_not_found={run_id}")
    if is_run_terminal(run.status):
        return run

    if run.status == "queued":
        _, claimed = claim_by_id(db, run_id)
        if claimed is None:
            db.expire_all()
            fresh = db.query(CompileRun).filter(CompileRun.id == run_id).first()
            if fresh is None:
                raise CompileRunError(f"run_not_found={run_id}")
            if fresh.status == "running":
                raise CompileRunError("run_already_running")
            return fresh  # 已归一 failed(retry_exhausted) / 已被抢占并完成等
        run = claimed
    if run.status != "running":
        raise CompileRunError(f"run_not_executable={run.status}")

    # 捕获本 worker claim 时的 lease 身份（stage 失败路径 reload 后 run 可能已是
    # 新 worker 的 lease —— fence 必须用本 worker 的身份，防止旧 worker 借新 lease 写入）。
    run_lease_token = run.lease_token
    run_worker_id = run.worker_id

    cur_attempt = run.attempt
    already_progress = db.query(StageRun).filter(
        StageRun.run_id == run.id,
        StageRun.attempt == cur_attempt,
        StageRun.status.in_(("running", "succeeded", "failed", "skipped", "cancelled")),
    ).first()
    if already_progress is not None:
        logger.info("execute_run duplicate attempt skipped run=%s attempt=%s", run.id, cur_attempt)
        return run

    try:
        # exact-version：Run 创建时固化 pipeline_version，注册新版本不影响旧 Run。
        pipeline = get_pipeline(run.pipeline_key, run.pipeline_version)
        if pipeline is None:
            _fail_run(db, run, run.current_stage or "", "PIPELINE_NOT_REGISTERED",
                      stage_error_message("PIPELINE_NOT_REGISTERED"))
            db.commit()
            return run
        if not pipeline.stages:
            _fail_run(db, run, "", "PIPELINE_NO_STAGES",
                      stage_error_message("PIPELINE_NO_STAGES"))
            db.commit()
            return run

        # 预建本 attempt 全部 stage 行（queued），便于失败/取消时级联可见。
        previous = _previous_stage_rows(db, run.id)
        stage_rows: list[StageRun] = []
        for order, sdef in enumerate(pipeline.stages):
            prev = previous.get(sdef.key)
            row = StageRun(
                run_id=run.id,
                stage_key=sdef.key,
                stage_order=order,
                status="queued",
                attempt=cur_attempt,
                input_hash=run.input_hash,
                retryable=sdef.retryable,
                component_key=sdef.key,
                component_version=sdef.version,
                parent_stage_run_id=prev.id if prev is not None else None,
                created_at=_now(),
            )
            db.add(row)
            stage_rows.append(row)
        db.flush()
        db.commit()  # 预建完成：释放 run 行锁，后续 stage 粒度事务
        db.refresh(run)

        if run.cancel_requested:
            _finalize_cancel(db, run, stage_rows)
            db.commit()
            return run

        ctx: dict = {
            "state": {},
            "run_id": run.id,
            "pipeline_key": run.pipeline_key,
            "pipeline_version": run.pipeline_version,
        }
        ctx["llm_runner"] = _LLM_RUNNER
        ctx["graph_runner"] = _GRAPH_RUNNER
        # 阶段 8D：编译 run 首次执行时把作用域业务绑定“冻结”为该 run 的固定技能
        # 版本（artifact 落盘）；中断恢复/重试的后续 attempt 一律沿用冻结值——
        # 中途晋升/回退不能改变该 run 的固定版本。
        # 功能关闭 → 不注入（旧行为完全不变）；功能开启但 schema 缺失/绑定损坏 →
        # 把错误转成 fail-loud runner（首次模型调用抛错，绝不静默按无绑定成功）。
        try:
            from app.core.skill_evolution import business_ops as _bops
            _domain = _bops.domain_for_pipeline(run.pipeline_key)
            if _domain and getattr(run, "workspace_id", None):
                _bops.freeze_binding_for_run(ctx, db, str(run.id),
                                             run.workspace_id, _domain)
        except Exception as exc:  # noqa: BLE001
            logger.exception("evolution business binding freeze failed run=%s",
                             run.id)
            ctx["evolution_binding_error"] = getattr(
                exc, "code", "freeze_failed")
            from app.core.skill_evolution import business_ops as _bops2
            ctx["llm_runner"] = _bops2.binding_error_runner(
                ctx.get("llm_runner"), exc)
        # 本 attempt 已成功 stage 的 (stage_key, 产物 content_hash) 链（按顺序）。
        succeeded_chain: list[tuple[str, str | None]] = []
        for sdef, row in zip(pipeline.stages, stage_rows):
            # —— stage 开始：先原子 fence，再写 running ——
            # 用条件 UPDATE（_fence_run）取代 SELECT-only _check_lease：rowcount==1
            # 后在同一事务写 StageRun=running + current_stage，一次 commit。
            db.refresh(run)
            if run.cancel_requested:
                _finalize_cancel(db, run, stage_rows)
                db.commit()
                return run
            if not _fence_run(db, run, lease_token=run_lease_token, worker_id=run_worker_id):
                logger.warning("run lease lost before stage run=%s stage=%s", run.id, sdef.key)
                db.rollback()
                db.expire_all()
                return run  # 保持 running 交由 recovery 收敛

            upstream = [
                h for key, h in succeeded_chain if h
                and (not sdef.cache_key_stage_keys or key in sdef.cache_key_stage_keys)
            ]
            stage_input_hash = compute_stage_input_hash(
                db, run, sdef, upstream_hashes=upstream
            )
            row.stage_input_hash = stage_input_hash

            if sdef.cachable:
                hit = _find_reusable_artifact(db, run, sdef, stage_input_hash)
                if hit is not None:
                    # —— 缓存命中写当前 Artifact 前：原子写 fencing ——
                    if not _fence_run(db, run, lease_token=run_lease_token, worker_id=run_worker_id):
                        logger.warning("cache fence lost run=%s stage=%s", run.id, sdef.key)
                        db.rollback()
                        db.expire_all()
                        return run  # 不写缓存 Artifact；交由 recovery
                    # stage_input_hash 缓存命中：复用既有成功产物，不触发 execute。
                    # 命中仍为「当前 run」写 Artifact 行（复用链落库）——content_hash/
                    # schema/artifact_type 与源一致，payload 复制缓存内容供下游 stage
                    # 使用，reused_from_artifact_id 指向源产物。
                    reason = apply_stage_status(row, "running")
                    if reason:
                        logger.warning("stage cache running reject run=%s reason=%s", run.id, reason)
                    row.started_at = _now()
                    success_reason = apply_stage_status(row, "succeeded")
                    if success_reason:
                        logger.warning("stage cache success reject run=%s reason=%s", run.id, success_reason)
                    row.output_hash = hit.content_hash
                    row.finished_at = _now()
                    db.add(Artifact(
                        run_id=run.id,
                        stage_run_id=row.id,
                        artifact_type=hit.artifact_type,
                        schema_version=hit.schema_version,
                        object_type=hit.object_type,
                        object_id=hit.object_id,
                        content_hash=hit.content_hash,
                        payload_json=hit.payload_json,
                        reused_from_artifact_id=hit.id,
                        created_at=_now(),
                    ))
                    _record_metrics(db, row, {
                        "cached": True,
                        "source_artifact_id": hit.id,
                        "reused_from_artifact_id": hit.id,
                    })
                    db.commit()
                    succeeded_chain.append((sdef.key, hit.content_hash))
                    logger.info(
                        "stage_input_hash cache hit run=%s stage=%s source_artifact=%s",
                        run.id, sdef.key, hit.id,
                    )
                    continue

            reason = apply_stage_status(row, "running")
            if reason:
                logger.warning("stage running reject run=%s reason=%s", run.id, reason)
                _fail_run(db, run, sdef.key, _STAGE_STATE_ERROR_CODE,
                          stage_error_message(_STAGE_STATE_ERROR_CODE))
                db.commit()
                return run
            row.started_at = _now()
            run.current_stage = sdef.key
            run.heartbeat_at = _now()
            db.flush()
            db.commit()  # stage=running 短事务提交（释放 run 行锁供长执行）
            db.refresh(run)

            # —— stage 执行隔离：SAVEPOINT 包住 stage.execute 的全部写入 ——
            # StageSession 允许 add/flush 但禁 commit；stage 失败/异常/commit 被阻时
            # 回滚 savepoint，丢弃其孤儿写入，绝不落入 run 事务。成功后 release
            # savepoint（写入并入外层事务），随后原子 fence + succeeded 同事务提交。
            savepoint = db.begin_nested()
            result = _safe_execute(db, run, row, sdef, ctx)

            if not result.get("ok"):
                # 丢弃 stage 产生的全部未提交写入（含受限 Session 的孤儿 add）。
                savepoint.rollback()
                db.expire_all()
                reloaded_run = db.query(CompileRun).filter(CompileRun.id == run.id).first()
                reloaded_row = db.query(StageRun).filter(StageRun.id == row.id).first()
                if reloaded_run is None or reloaded_row is None:
                    db.rollback()
                    return run
                run = reloaded_run
                row = reloaded_row
                # —— 失败状态写入前：原子 fence ——
                # rowcount==1 后在同一事务写 StageRun failed / queued skipped/cancelled /
                # Run failed/cancelled，一次 commit；rowcount==0 → rollback 不改任何状态。
                if not _fence_run(db, run, lease_token=run_lease_token, worker_id=run_worker_id):
                    logger.warning("failure fence lost run=%s stage=%s", run.id, sdef.key)
                    db.rollback()
                    db.expire_all()
                    return run  # 不改任何状态，交由 recovery 收敛
                code = result.get("error_code") or _STAGE_EXCEPTION_CODE
                safe_msg = stage_error_message(code)
                apply_stage_status(row, "failed")
                # legacy error_code/error_message 只保存 safe 内容（无原始异常文本）。
                row.error_code = _truncate_code(code)
                row.error_message = safe_msg
                row.safe_error_code = _truncate_code(code)
                row.safe_error_message = safe_msg
                row.finished_at = _now()
                run.heartbeat_at = _now()
                if sdef.failure_transition == FailureTransition.CANCEL:
                    # 失败视为取消：未完成 stage 级联 cancelled，run → cancelled。
                    _finalize_cancel(db, run, stage_rows)
                else:
                    # 上游失败级联：未完成 stage → skipped；run → failed。
                    for other in stage_rows:
                        if other.status == "queued":
                            apply_stage_status(other, "skipped")
                            other.finished_at = _now()
                    _fail_run(db, run, sdef.key, code, safe_msg)
                db.commit()
                return run

            # stage 成功：release savepoint（保留其合法写入），随后原子 fence。
            savepoint.commit()
            db.refresh(run)

            # —— stage 结果持久化前：原子写 fencing ——
            # 必须用条件 UPDATE fence（同一事务内随后写 StageRun/Artifact/Revision，
            # 一次 commit）；禁止"SELECT 通过后再 commit"作为结束 fence。
            if not _fence_run(db, run, lease_token=run_lease_token, worker_id=run_worker_id):
                logger.warning("fence lost after stage run=%s stage=%s", run.id, sdef.key)
                db.rollback()  # 丢弃本 stage 全部未提交写入（含受限 Session 的 add）
                db.expire_all()
                return run  # 不写 artifact/succeeded；保持 running 交由 recovery

            # 成功：succeeded + artifact 原子提交（与 fence 同一事务）
            apply_stage_status(row, "succeeded")
            row.output_hash = result.get("content_hash") or None
            row.component_key = result.get("component_key") or row.component_key
            row.component_version = result.get("component_version") or row.component_version
            row.finished_at = _now()
            run.heartbeat_at = _now()
            _record_metrics(db, row, {"cached": False})
            if result.get("metrics"):
                _record_metrics(db, row, result.get("metrics"))
            _write_artifact(db, run, row, result)
            succeeded_chain.append((sdef.key, row.output_hash))
            rev = result.get("output_revision_id")
            if rev:
                run.output_revision_id = rev
                ctx["state"]["output_revision_id"] = rev
            db.commit()
            db.refresh(run)

        # 全部成功：Run 最终 succeeded 用原子条件 UPDATE（fenced CAS）。
        db.refresh(run)
        if run.cancel_requested:
            _finalize_cancel(db, run, stage_rows)
            db.commit()
            return run
        if not _finalize_run_succeeded(db, run, lease_token=run_lease_token, worker_id=run_worker_id):
            logger.warning("run success fence lost run=%s", run.id)
            db.rollback()
            db.expire_all()
            return run  # 已被 supersede/cancel/recovery 接管；交由 recovery
        db.commit()
        return run
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("execute_run failed run=%s", run.id)
        raise


def _previous_stage_rows(db: Session, run_id: str) -> dict[str, StageRun]:
    """返回该 run 每个 stage_key 最近一次（attempt 最大）的历史 stage 行。"""
    previous: dict[str, StageRun] = {}
    rows = (
        db.query(StageRun)
        .filter(StageRun.run_id == run_id)
        .order_by(StageRun.stage_key, StageRun.attempt.desc())
        .all()
    )
    for row in rows:
        previous.setdefault(row.stage_key, row)
    return previous
