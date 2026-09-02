"""Phase 4.1：KnowledgeCompile 核心框架测试（服务层，无 HTTP）。

覆盖契约 Phase 4.1 测试矩阵：
- 状态机：合法/非法转换（终态不可迁移）、failed→cancelled 保持非法
- claim CAS：双 worker 并发仅一个成功；FIFO；attempt 由 claim 原子 +1
- registry 封闭：duplicate stage key / duplicate pipeline version /
  replace_for_test / cacheable 需完整 descriptor / execute 契约违规
- create_run 身份校验：workspace 未知(404)/archived(409)/跨 workspace wiki
  (409)/wiki_page 未知(404)/source_sync_run 未知(404)/trigger 目标非法(400)
- 幂等 fingerprint：同 key 同指纹幂等 / 同 key 异指纹 409 / 唯一索引并发回查归一
- supersede 隔离：workspace 隔离、null wiki_page 匹配、幂等、stage 闭合 skipped
- stage_input_hash 缓存隔离：workspace / pipeline_version / stage key+version /
  schema_version / 上游产物哈希链 任一不同不命中；失败 lineage 不复用
- attempt 语义：max_attempts 精确上限、retry_exhausted、stale requeue 不加 attempt
- stale recovery：worker_lost 闭合 running stage、queued→skipped、幂等
- 错误分层：_sanitize_for_api 剥离路径/URL/Bearer/Authorization/Token
- 保留回归：cancel、SourceSyncRun 独立、fake 不劫持、MANAGED fail closed、
  P42 alembic 往返（upgrade head / downgrade P41 / upgrade head）
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.wiki_pipeline import executor, fake, registry, state_machine, worker
from app.core.wiki_pipeline.executor import CompileRunError
from app.core.wiki_pipeline.registry import (
    FailureTransition,
    PipelineDef,
    PipelineError,
    StageDef,
    StageResult,
    stage_error_message,
    validate_stage_result,
)
from app.core.wiki_pipeline.state_machine import (
    apply_run_status,
    validate_run_transition,
    validate_stage_transition,
)
from app.models.database import (
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    SourceConnection,
    SourceSyncRun,
    WikiPage,
    WikiWorkspace,
    check_managed_migrations,
    init_db,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.abspath(os.path.join(BACKEND_ROOT, ".venv", "Scripts", "python.exe"))


@pytest.fixture(autouse=True)
def _registry_cleanup():
    """每个测试前后清理注册表 + worker，防止全局状态跨测试泄漏。"""
    registry.REGISTRY.clear()
    yield
    registry.REGISTRY.clear()
    worker.stop_worker()


@pytest.fixture()
def db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'pipeline.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def pipelines():
    fake.register_fake_pipelines()
    yield fake
    fake.unregister_fake_pipelines()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ws(db, ws_id: str, status: str = "active", key: str | None = None):
    if db.get(WikiWorkspace, ws_id) is not None:
        return
    db.add(WikiWorkspace(
        id=ws_id, key=key or f"ws_key_{ws_id}", name=ws_id,
        acl_scope='{"groups": ["g1"]}', scope_id="group:g1", status=status,
    ))
    db.flush()


# Phase 4.2：wiki_page.workspace_id 必须非空（create_run 严格校验 NULL 不可跳过）。
# 无显式 workspace 的测试页面自动归入共享默认 workspace（每个测试库独立，无跨测试污染）。
_DEFAULT_WS_ID = "ws-default"


def _page(db, page_id: str, workspace_id: str | None = None):
    if db.get(WikiPage, page_id) is not None:
        return
    if workspace_id is None:
        _ws(db, _DEFAULT_WS_ID)
        workspace_id = _DEFAULT_WS_ID
    else:
        _ws(db, workspace_id)
    db.add(WikiPage(id=page_id, title=page_id, workspace_id=workspace_id))
    db.flush()


def _mk_run(db, *, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
            trigger_object_id=None, wiki_page_id=None, workspace_id=None,
            source_sync_run_id=None, **kwargs) -> CompileRun:
    """建真实依赖行（workspace/page）+ queued run 并 commit。"""
    if workspace_id:
        _ws(db, workspace_id)
    if wiki_page_id:
        _page(db, wiki_page_id, workspace_id)
    run = executor.create_run(
        db,
        pipeline_key=pipeline_key,
        trigger_type=trigger_type,
        trigger_object_id=trigger_object_id,
        wiki_page_id=wiki_page_id,
        workspace_id=workspace_id,
        source_sync_run_id=source_sync_run_id,
        **kwargs,
    )
    db.commit()
    return run


def _mk_ws_page(db, ws_id: str, page_id: str) -> None:
    _ws(db, ws_id)
    _page(db, page_id, ws_id)
    db.commit()


def _stages(db, run_id):
    return db.query(StageRun).filter(StageRun.run_id == run_id).order_by(
        StageRun.stage_order, StageRun.attempt
    ).all()


def _register(key: str, version: str, stages, *, allow_null_workspace: bool = True) -> None:
    # allow_null_workspace 默认 True：这些测试用的自定义流水线常以 NULL workspace 跑
    # 流程语义（不测 workspace_required）；反例用显式 False。
    registry.replace_for_test(PipelineDef(
        key=key, version=version, stages=stages,
        allow_null_workspace=allow_null_workspace,
    ))


def _single_cache_pipeline(key, version, *, stage_key="cache_stage", stage_version="1",
                           schema_version="sv/1", content_src="same"):
    def _fn(db, run, stage_run, ctx):
        return {
            "ok": True,
            "artifact_type": "test_cache",
            "schema_version": schema_version,
            "object_type": "wiki_page",
            "object_id": run.id,
            # Phase 4.2：content_hash 由系统计算，stage 不自报
            "payload": {"x": content_src},
        }
    stage = StageDef(
        key=stage_key, version=stage_version, retryable=True, cachable=True,
        cache_type="test_cache", cache_schema_version=schema_version,
        execute=_fn, failure_transition=FailureTransition.FAIL,
    )
    _register(key, version, [stage])
    return key


# ---------------------------------------------------------------------------
# 状态机（纯函数）
# ---------------------------------------------------------------------------

def test_run_legal_transitions():
    for cur, new in [
        ("queued", "running"), ("queued", "cancelled"), ("queued", "superseded"),
        ("running", "succeeded"), ("running", "failed"), ("running", "queued"),
        ("running", "cancelled"), ("running", "superseded"),
        ("failed", "queued"), ("failed", "superseded"),
    ]:
        assert validate_run_transition(cur, new) == ""


def test_run_illegal_transitions_terminal_and_failed_cancel():
    for terminal in ("succeeded", "cancelled", "superseded"):
        for new in ("queued", "running", "failed", "succeeded", "cancelled", "superseded"):
            assert validate_run_transition(terminal, new) != ""
    assert validate_run_transition("failed", "cancelled") != ""  # 失败不可取消覆盖
    assert validate_run_transition("succeeded", "superseded") != ""
    assert validate_run_transition("queued", "succeeded") != ""


def test_stage_legal_and_illegal_transitions():
    for cur, new in [
        ("queued", "running"), ("queued", "skipped"), ("queued", "cancelled"),
        ("running", "succeeded"), ("running", "failed"), ("running", "queued"),
        ("running", "skipped"), ("running", "cancelled"),
        ("failed", "queued"),
    ]:
        assert validate_stage_transition(cur, new) == ""
    for terminal in ("succeeded", "skipped", "cancelled"):
        for new in ("queued", "running", "failed", "succeeded"):
            assert validate_stage_transition(terminal, new) != ""
    # failed stage 不可被 cancelled/skipped 覆盖（cancel/supersede 语义要求保持）
    assert validate_stage_transition("failed", "cancelled") != ""
    assert validate_stage_transition("failed", "skipped") != ""


def test_apply_run_status_does_not_write_illegal(db, pipelines):
    run = _mk_run(db)
    reason = apply_run_status(run, "succeeded")  # queued → succeeded 非法
    assert reason
    db.refresh(run)
    assert run.status == "queued"


# ---------------------------------------------------------------------------
# claim CAS / FIFO / attempt 语义
# ---------------------------------------------------------------------------

def test_two_workers_claim_once(db, pipelines):
    """双线程 + 独立连接并发 claim 同一 queued run：仅一个成功（CAS 原子）。"""
    run = _mk_run(db, wiki_page_id="race")
    engine = db.get_bind()
    Session = sessionmaker(bind=engine)
    results: list[tuple[str, str | None]] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def _claim(name: str):
        s = Session()
        try:
            barrier.wait(timeout=10)
            claimed = worker.claim_next_run(s)
            results.append((name, claimed.id if claimed is not None else None))
        except Exception as exc:  # noqa: BLE001
            try:
                s.rollback()
            except Exception:
                pass
            errors.append(f"{name}: {exc!r}")
        finally:
            s.close()

    threads = [threading.Thread(target=_claim, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    winners = [rid for _, rid in results if rid is not None]
    assert len(winners) == 1
    assert winners[0] == run.id
    db.refresh(run)
    assert run.status == "running"
    assert run.attempt == 1  # claim 原子 +1
    assert run.lease_token and run.worker_id


def test_claim_single_and_duplicate_claim_idempotent(db, pipelines):
    run = _mk_run(db)
    claimed = worker.claim_next_run(db)
    assert claimed is not None and claimed.id == run.id
    db.refresh(run)
    assert run.status == "running" and run.attempt == 1
    assert worker.claim_next_run(db) is None
    assert run.status == "running"


def test_claim_respects_fifo(db, pipelines):
    r1 = _mk_run(db, wiki_page_id="a")
    r2 = _mk_run(db, wiki_page_id="b")
    assert worker.claim_next_run(db).id == r1.id
    assert worker.claim_next_run(db).id == r2.id
    assert worker.claim_next_run(db) is None


def test_execute_run_direct_claim_on_queued(db, pipelines):
    """execute_run 直调 queued：内部 CAS claim（attempt 0→1），stage.attempt=1。"""
    run = _mk_run(db)
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "succeeded" and run.attempt == 1
    stages = _stages(db, run.id)
    assert {s.attempt for s in stages} == {1}


def test_execute_run_idempotent_no_duplicate_rows(db, pipelines):
    run = _mk_run(db)
    executor.execute_run(db, run.id)
    assert run.status == "succeeded"
    before = db.query(StageRun).filter(StageRun.run_id == run.id).count()
    executor.execute_run(db, run.id)  # 终态幂等
    assert db.query(StageRun).filter(StageRun.run_id == run.id).count() == before
    run2 = _mk_run(db, wiki_page_id="idem")
    executor.execute_run(db, run2.id)
    assert run2.status == "succeeded"


def test_execute_run_success_writes_rows_and_revision(db, pipelines):
    run = _mk_run(db)
    worker.claim_next_run(db)
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "succeeded"
    assert run.output_revision_id and run.output_revision_id.startswith("rev-fake-")
    stages = _stages(db, run.id)
    assert [(s.stage_key, s.status) for s in stages] == [
        ("canonicalize", "succeeded"),
        ("validate", "succeeded"),
        ("publish", "succeeded"),
    ]
    artifacts = db.query(Artifact).filter(Artifact.run_id == run.id).all()
    assert sorted(a.artifact_type for a in artifacts) == ["canonical_note", "validation_report"]


def test_claim_normalizes_exhausted_queued_candidate(db, pipelines):
    """attempt>=max 的 queued 候选被 claim 置 failed(retry_exhausted)，不可再 claim。"""
    run = _mk_run(db, trigger_object_id="fail")
    db.query(CompileRun).filter(CompileRun.id == run.id).update({
        "status": "queued",
        "attempt": 3,
        "max_attempts": 3,
    })
    db.commit()
    assert worker.claim_next_run(db) is None
    db.refresh(run)
    assert run.status == "failed"
    assert run.safe_error_code == "retry_exhausted"


# ---------------------------------------------------------------------------
# 阶段失败 / retry / cancel
# ---------------------------------------------------------------------------

def test_stage_failure_cascades_skip(db, pipelines):
    run = _mk_run(db, trigger_object_id="fail")
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed" and run.attempt == 1
    # Phase 4.2：error_code 严格大写；safe message 服务端固定映射（不含原始异常文本）。
    assert run.safe_error_code == "FAKE_VALIDATION_FAILURE"
    assert run.safe_error_message == stage_error_message("FAKE_VALIDATION_FAILURE")
    statuses = {s.stage_key: s.status for s in _stages(db, run.id)}
    assert statuses["canonicalize"] == "succeeded"
    assert statuses["validate"] == "failed"
    assert statuses["publish"] == "skipped"
    validate = next(s for s in _stages(db, run.id) if s.stage_key == "validate")
    assert validate.safe_error_code == "FAKE_VALIDATION_FAILURE"
    assert validate.safe_error_message == stage_error_message("FAKE_VALIDATION_FAILURE")
    # legacy error_code/error_message 只保存 safe 内容
    assert validate.error_code == "FAKE_VALIDATION_FAILURE"
    assert validate.error_message == validate.safe_error_message


def test_stage_exception_wrapped(db, pipelines):
    run = _mk_run(db, trigger_object_id="explode")
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed"
    validate = db.query(StageRun).filter(
        StageRun.run_id == run.id, StageRun.stage_key == "validate"
    ).first()
    assert validate.status == "failed"
    # Phase 4.2：原始异常只进日志；error_code 大写；safe 为服务端固定文案。
    assert validate.error_code == "STAGE_EXCEPTION"
    assert validate.safe_error_code == "STAGE_EXCEPTION"
    assert validate.safe_error_message == stage_error_message("STAGE_EXCEPTION")
    assert validate.error_message == validate.safe_error_message
    assert "fake explode" not in (validate.safe_error_message or "")
    assert run.safe_error_code == "STAGE_EXCEPTION"


def test_retry_attempts_only_by_next_claim(db, pipelines):
    """retry 不 +attempt；下一次 claim 才 +1；stage.attempt = claim 后执行序号。"""
    run = _mk_run(db, trigger_object_id="fail")
    executor.execute_run(db, run.id)  # claim → attempt 1
    db.refresh(run)
    assert run.status == "failed" and run.attempt == 1

    requeued = executor.requeue_failed_run(db, run)
    db.commit()
    db.refresh(run)
    assert requeued.id == run.id
    assert run.status == "queued"
    assert run.attempt == 1  # retry 不加 attempt
    assert run.error_summary is None

    claimed = worker.claim_next_run(db)
    assert claimed is not None and claimed.attempt == 2  # claim 原子 +1
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed" and run.attempt == 2

    validate_attempts = [s for s in _stages(db, run.id) if s.stage_key == "validate"]
    assert [s.attempt for s in validate_attempts] == [1, 2]
    first, second = validate_attempts[0], validate_attempts[1]
    assert first.error_code == "FAKE_VALIDATION_FAILURE"
    assert second.parent_stage_run_id == first.id  # 历史错误保留在新行 parent 链


def test_retry_exhausted_raises_409(db, pipelines):
    run = _mk_run(db, trigger_object_id="fail")
    db.query(CompileRun).filter(CompileRun.id == run.id).update({
        "status": "failed", "attempt": 3, "max_attempts": 3,
    })
    db.commit()
    with pytest.raises(CompileRunError) as exc:
        executor.retry_run(db, run.id)
    assert str(exc.value).startswith("retry_attempts_exhausted")
    db.refresh(run)
    assert run.status == "failed"


def test_retry_superseded_raises_use_create(db, pipelines):
    """superseded retry → 409 run_superseded_use_create（取消自动新建 run 旧逻辑）。"""
    r1 = _mk_run(db, trigger_object_id="slot-1")
    r2 = _mk_run(db, trigger_object_id="slot-1", supersede_same_trigger=True)
    db.refresh(r1)
    assert r1.status == "superseded"
    with pytest.raises(CompileRunError) as exc:
        executor.retry_run(db, r1.id)
    assert str(exc.value).startswith("run_superseded_use_create")
    # 不得自动新建 run 绕开上限
    assert db.query(CompileRun).count() == 2


def test_retry_illegal_for_terminal(db, pipelines):
    run = _mk_run(db)
    executor.execute_run(db, run.id)
    assert run.status == "succeeded"
    with pytest.raises(CompileRunError):
        executor.retry_run(db, run.id)


def test_failure_transition_cancel_cancels_run(db, pipelines):
    run = _mk_run(db, pipeline_key="fake.wiki.compile.cancel-fail", trigger_object_id="fail")
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "cancelled"
    statuses = {s.stage_key: s.status for s in _stages(db, run.id)}
    assert statuses["validate"] == "failed"  # failed stage 保持 failed
    assert statuses["publish"] == "cancelled"
    db.refresh(run)
    assert run.finished_at is not None


def test_cancel_queued_direct(db, pipelines):
    run = _mk_run(db)
    executor.cancel_run(db, run.id)
    db.commit()
    db.refresh(run)
    assert run.status == "cancelled" and run.finished_at is not None


def test_cancel_running_requests_and_executor_cascades(db, pipelines):
    run = _mk_run(db)
    worker.claim_next_run(db)
    assert run.status == "running"
    executor.cancel_run(db, run.id)
    db.commit()
    assert run.cancel_requested is True
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "cancelled" and run.finished_at is not None
    statuses = {s.stage_key: s.status for s in _stages(db, run.id)}
    assert set(statuses.values()) == {"cancelled"}


def test_cancel_running_terminal_rejected(db, pipelines):
    run = _mk_run(db)
    executor.execute_run(db, run.id)
    assert run.status == "succeeded"
    with pytest.raises(CompileRunError):
        executor.cancel_run(db, run.id)


# ---------------------------------------------------------------------------
# create_run 身份校验
# ---------------------------------------------------------------------------

def test_create_run_unknown_workspace_404(db, pipelines):
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_rebuild", workspace_id="ws-missing")
    assert str(exc.value).startswith("workspace_not_found")


def test_create_run_rejects_archived_workspace(db, pipelines):
    _mk_ws_page(db, "ws-arch", "pa")
    ws = db.get(WikiWorkspace, "ws-arch")
    ws.status = "archived"
    db.commit()
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_rebuild", workspace_id="ws-arch", wiki_page_id="pa")
    assert str(exc.value).startswith("workspace_not_active")


def test_create_run_unknown_wiki_page_404(db, pipelines):
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_rebuild", wiki_page_id="page-missing")
    assert str(exc.value).startswith("wiki_page_not_found")


def test_create_run_rejects_cross_workspace_wiki(db, pipelines):
    """wiki_page 归属 workspace 与显式 workspace_id 不符 → 409。"""
    _mk_ws_page(db, "ws-a", "pa")
    _mk_ws_page(db, "ws-b", "pb")
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_rebuild",
                            workspace_id="ws-b", wiki_page_id="pa")
    assert str(exc.value).startswith("wiki_page_workspace_mismatch")


def test_create_run_derives_workspace_from_wiki_page(db, pipelines):
    """workspace_id 为空 → 从 wiki_page.workspace_id 推导。"""
    _mk_ws_page(db, "ws-a", "pa")
    run = executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                              trigger_type="manual_rebuild", wiki_page_id="pa")
    db.commit()
    assert run.workspace_id == "ws-a"


def test_create_run_unknown_source_sync_run_404(db, pipelines):
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_rebuild", source_sync_run_id="ssr-missing")
    assert str(exc.value).startswith("source_sync_run_not_found")


def test_create_run_rejects_invalid_trigger_targets(db, pipelines):
    # page_changed 缺 trigger_object_id
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="page_changed", wiki_page_id="p")
    assert str(exc.value).startswith("invalid_trigger_targets")
    # manual_edit 缺 wiki_page_id
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_edit", trigger_object_id="x")
    assert str(exc.value).startswith("invalid_trigger_targets")


def test_source_sync_run_positive_wiring(db, pipelines):
    """合法 source_sync_run 可创建 run 且 CompileRun 失败不改 SourceSyncRun。"""
    conn = SourceConnection(id="sc-1", connector_key="gitlab", name="gitlab", enabled=True)
    db.add(conn)
    ssr = SourceSyncRun(id="ssr-1", connection_id="sc-1", mode="incremental", status="running")
    db.add(ssr)
    db.commit()
    run = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        source_sync_run_id="ssr-1", trigger_object_id="fail",
    )
    db.commit()
    assert run.source_sync_run_id == "ssr-1"
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed"
    db.refresh(ssr)
    assert ssr.status == "running"  # 绝不反向标记 SourceSyncRun
    assert ssr.error_summary is None


# ---------------------------------------------------------------------------
# 幂等 fingerprint / 唯一索引竞态
# ---------------------------------------------------------------------------

def _mk_idem_run(db, key, **kw):
    return executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                               trigger_type="manual_rebuild", idempotency_key=key, **kw)


def test_idempotency_same_key_same_fingerprint(db, pipelines):
    _page(db, "p-idem")
    r1 = _mk_idem_run(db, "idem-same", wiki_page_id="p-idem")
    db.commit()
    r2 = _mk_idem_run(db, "idem-same", wiki_page_id="p-idem")
    db.commit()
    assert r2.id == r1.id
    assert db.query(CompileRun).filter(CompileRun.idempotency_key == "idem-same").count() == 1
    executor.execute_run(db, r1.id)
    assert r1.status == "succeeded"


def test_idempotency_same_key_different_fingerprint_conflict(db, pipelines):
    _page(db, "p-idem")
    _mk_idem_run(db, "idem-conf", wiki_page_id="p-idem", input_hash="H-1")
    db.commit()
    with pytest.raises(CompileRunError) as exc:
        _mk_idem_run(db, "idem-conf", wiki_page_id="p-idem", input_hash="H-2")
    assert str(exc.value).startswith("idempotency_conflict")
    db.rollback()
    assert db.query(CompileRun).filter(CompileRun.idempotency_key == "idem-conf").count() == 1


def test_idempotency_integrity_race_normalizes(monkeypatch, db, pipelines):
    """模拟并发插撞唯一索引：幂等预查询错过 → IntegrityError → 回查归一（同指纹幂等）。"""
    _page(db, "p-race")
    existing = _mk_idem_run(db, "idem-race", wiki_page_id="p-race", input_hash="H-1")
    db.commit()

    real_find = executor._find_run_by_idempotency_key
    calls = [0]

    def _miss_once(db2, key):
        if calls[0] == 0:
            calls[0] += 1
            return None  # 竞态窗口：读快照错过已提交行
        return real_find(db2, key)

    monkeypatch.setattr(executor, "_find_run_by_idempotency_key", _miss_once)
    dup = _mk_idem_run(db, "idem-race", wiki_page_id="p-race", input_hash="H-1")
    db.commit()
    assert dup.id == existing.id  # 归一返回已有 run


def test_idempotency_integrity_race_conflict(monkeypatch, db, pipelines):
    """竞态窗口命中已存在行但指纹不同 → 409 idempotency_conflict。"""
    _page(db, "p-race")
    _mk_idem_run(db, "idem-race2", wiki_page_id="p-race", input_hash="H-1")
    db.commit()

    real_find = executor._find_run_by_idempotency_key
    calls = [0]

    def _miss_once(db2, key):
        if calls[0] == 0:
            calls[0] += 1
            return None
        return real_find(db2, key)

    monkeypatch.setattr(executor, "_find_run_by_idempotency_key", _miss_once)
    with pytest.raises(CompileRunError) as exc:
        _mk_idem_run(db, "idem-race2", wiki_page_id="p-race", input_hash="H-2")
    assert str(exc.value).startswith("idempotency_conflict")
    db.rollback()
    assert db.query(CompileRun).filter(CompileRun.idempotency_key == "idem-race2").count() == 1


def test_db_unique_idempotency_key_backstop(db):
    """绕过应用层去重直接插入重复 idempotency_key → IntegrityError。"""
    db.add(CompileRun(
        id="dup-a", pipeline_key="fake.wiki.compile.v1", pipeline_version="1",
        trigger_type="manual_rebuild", status="queued", input_hash="h1",
        idempotency_key="same-key",
    ))
    db.add(CompileRun(
        id="dup-b", pipeline_key="fake.wiki.compile.v1", pipeline_version="1",
        trigger_type="manual_rebuild", status="queued", input_hash="h2",
        idempotency_key="same-key",
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.query(CompileRun).filter(
        CompileRun.idempotency_key == "same-key"
    ).count() == 0


# ---------------------------------------------------------------------------
# supersede 隔离
# ---------------------------------------------------------------------------

def test_supersede_is_workspace_isolated(db, pipelines):
    """相同 trigger_object_id 不同 workspace 不互抢。"""
    _mk_ws_page(db, "ws-a", "pa")
    _mk_ws_page(db, "ws-b", "pb")
    r_a = _mk_run(db, wiki_page_id="pa", workspace_id="ws-a", trigger_object_id="obj-x")
    r_b = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="obj-x", wiki_page_id="pb", workspace_id="ws-b",
        supersede_same_trigger=True,
    )
    db.commit()
    db.refresh(r_a)
    assert r_a.status == "queued"  # 不跨 workspace 抢占
    assert r_b.status == "queued"


def test_supersede_same_workspace_same_wiki(db, pipelines):
    _mk_ws_page(db, "ws-a", "pa")
    old = _mk_run(db, wiki_page_id="pa", workspace_id="ws-a", trigger_object_id="obj-x")
    fresh = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="obj-x", wiki_page_id="pa", workspace_id="ws-a",
        supersede_same_trigger=True,
    )
    db.commit()
    db.refresh(old)
    assert old.status == "superseded"
    # superseded run 的非终态 stage → skipped + finished_at
    executor.execute_run(db, fresh.id)  # 先让 fresh succeeded，使其 stage 全 succeeded
    old2 = _mk_run(db, wiki_page_id="pa", workspace_id="ws-a", trigger_object_id="obj-x")
    # 手动给 old2 制造 queued/running stage，再抢占
    db.add_all([
        StageRun(run_id=old2.id, stage_key="canonicalize", stage_order=0, status="queued",
                 attempt=1, input_hash=old2.input_hash, component_key="canonicalize", component_version="1"),
        StageRun(run_id=old2.id, stage_key="validate", stage_order=1, status="running",
                 attempt=1, input_hash=old2.input_hash, component_key="validate", component_version="1"),
    ])
    db.commit()
    executor.supersede_matching_runs(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="obj-x", wiki_page_id="pa", workspace_id="ws-a",
    )
    db.commit()
    db.refresh(old2)
    assert old2.status == "superseded"
    srows = db.query(StageRun).filter(StageRun.run_id == old2.id).all()
    assert {s.status for s in srows} == {"skipped"}
    assert all(s.finished_at is not None for s in srows)


def test_supersede_null_wiki_page_matches(db, pipelines):
    """wiki_page_id 都为 NULL 时 null-safe 匹配（不抢占非 NULL 行）。"""
    old = _mk_run(db, trigger_object_id="obj-null")
    executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="obj-null", supersede_same_trigger=True,
    )
    db.commit()
    db.refresh(old)
    assert old.status == "superseded"


def test_supersede_idempotent(db, pipelines):
    old = _mk_run(db, trigger_object_id="obj-idem")
    fresh = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="obj-idem", supersede_same_trigger=True,
    )
    db.commit()
    db.refresh(old)
    assert old.status == "superseded"
    # 第三次相同抢占不再改变已 superseded 行
    fresh2 = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="obj-idem", supersede_same_trigger=True,
    )
    db.commit()
    db.refresh(fresh)
    assert fresh.status == "superseded"
    db.refresh(old)
    assert old.status == "superseded"
    assert db.query(CompileRun).filter(CompileRun.status == "superseded").count() == 2


def test_supersede_skips_terminal_runs(db, pipelines):
    old = _mk_run(db, trigger_object_id="obj-term")
    executor.execute_run(db, old.id)  # succeeded 终态不可被抢占
    fresh = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="obj-term", supersede_same_trigger=True,
    )
    db.commit()
    db.refresh(old)
    assert old.status == "succeeded"
    assert fresh.status == "queued"


# ---------------------------------------------------------------------------
# stage_input_hash 缓存
# ---------------------------------------------------------------------------

def test_cache_hit_writes_current_artifact_with_reused_from(db, pipelines):
    """Phase 4.2 缓存链：命中仍为当前 run 写 Artifact 行（reused_from=源.id、
    content_hash/类型/schema 与源一致、payload 复制），下游用当前链，不重复 execute。"""
    run1 = _mk_run(db, wiki_page_id="c1")
    executor.execute_run(db, run1.id)
    assert run1.status == "succeeded"
    source = db.query(Artifact).filter(
        Artifact.run_id == run1.id, Artifact.artifact_type == "canonical_note"
    ).one()
    assert source.content_hash and source.payload_json
    assert source.reused_from_artifact_id is None

    run2 = _mk_run(db, wiki_page_id="c1")  # 同 target → 同 input_hash → 命中
    assert run1.input_hash == run2.input_hash
    executor.execute_run(db, run2.id)
    db.refresh(run2)
    assert run2.status == "succeeded"
    canon2 = db.query(StageRun).filter(
        StageRun.run_id == run2.id, StageRun.stage_key == "canonicalize"
    ).first()
    metrics = json.loads(canon2.metrics_json)
    assert metrics.get("cached") is True
    current = db.query(Artifact).filter(
        Artifact.run_id == run2.id, Artifact.artifact_type == "canonical_note"
    ).one()
    # 当前 run 的 Artifact 行存在且与源一致（复用链落库，非仅 metrics）。
    assert current.stage_run_id == canon2.id
    assert current.content_hash == source.content_hash
    assert current.artifact_type == source.artifact_type
    assert current.schema_version == source.schema_version
    assert current.reused_from_artifact_id == source.id
    assert current.payload_json == source.payload_json
    # 下游 stage_input_hash 用当前链（= 源 content_hash）；校验 stage 行 output_hash。
    validate2 = db.query(StageRun).filter(
        StageRun.run_id == run2.id, StageRun.stage_key == "validate"
    ).first()
    assert validate2.stage_input_hash is not None
    assert canon2.output_hash == source.content_hash


def test_input_hash_change_blocks_reuse_of_old_artifact(db, pipelines):
    run1 = _mk_run(db, wiki_page_id="old")
    executor.execute_run(db, run1.id)
    canon1 = db.query(Artifact).filter(
        Artifact.run_id == run1.id, Artifact.artifact_type == "canonical_note"
    ).one()

    run2 = _mk_run(db, wiki_page_id="new")
    assert run2.input_hash != run1.input_hash
    executor.execute_run(db, run2.id)
    canon2 = db.query(StageRun).filter(
        StageRun.run_id == run2.id, StageRun.stage_key == "canonicalize"
    ).first()
    assert "cached" not in json.loads(canon2.metrics_json) or json.loads(canon2.metrics_json).get("cached") is False
    new_canon = db.query(Artifact).filter(
        Artifact.run_id == run2.id, Artifact.artifact_type == "canonical_note"
    ).one()
    assert new_canon.id != canon1.id

    run3 = _mk_run(db, wiki_page_id="new")
    executor.execute_run(db, run3.id)
    canon3 = db.query(StageRun).filter(
        StageRun.run_id == run3.id, StageRun.stage_key == "canonicalize"
    ).first()
    assert json.loads(canon3.metrics_json).get("cached") is True
    assert json.loads(canon3.metrics_json).get("reused_from_artifact_id") == new_canon.id


def test_cache_isolated_by_workspace(db, pipelines):
    _mk_ws_page(db, "ws-a", "pa")
    _mk_ws_page(db, "ws-b", "pb")
    run_a = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        wiki_page_id="pa", workspace_id="ws-a", input_hash="H-shared",
    )
    db.commit()
    executor.execute_run(db, run_a.id)
    assert run_a.status == "succeeded"

    run_b = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        wiki_page_id="pb", workspace_id="ws-b", input_hash="H-shared",
    )
    db.commit()
    executor.execute_run(db, run_b.id)
    canon_b = db.query(StageRun).filter(
        StageRun.run_id == run_b.id, StageRun.stage_key == "canonicalize"
    ).first()
    assert "cached" not in json.loads(canon_b.metrics_json) or json.loads(canon_b.metrics_json).get("cached") is not True
    assert db.query(Artifact).filter(
        Artifact.run_id.in_([run_a.id, run_b.id]),
        Artifact.artifact_type == "canonical_note",
    ).count() == 2

    # 同 workspace 内缓存仍生效
    run_b2 = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        wiki_page_id="pb", workspace_id="ws-b", input_hash="H-shared",
    )
    db.commit()
    executor.execute_run(db, run_b2.id)
    canon_b2 = db.query(StageRun).filter(
        StageRun.run_id == run_b2.id, StageRun.stage_key == "canonicalize"
    ).first()
    assert json.loads(canon_b2.metrics_json).get("cached") is True
    art_b = db.query(Artifact).filter(
        Artifact.run_id == run_b.id, Artifact.artifact_type == "canonical_note"
    ).one()
    assert json.loads(canon_b2.metrics_json).get("reused_from_artifact_id") == art_b.id


def test_cache_isolated_by_pipeline_version(db, pipelines):
    _page(db, "p-ver")
    key = "fake.wiki.compile.v1"
    r1 = executor.create_run(
        db, pipeline_key=key, trigger_type="manual_rebuild", wiki_page_id="p-ver",
        pipeline_version="1", input_hash="H", workspace_id=None,
    )
    db.commit()
    executor.execute_run(db, r1.id)
    assert r1.status == "succeeded"
    assert r1.pipeline_version == "1"  # 显式 version 固化

    # exact-version：注册 v2（同 key 不同 version 共存），不影响已固化 v1 的 r1。
    v1 = registry.get_pipeline(key, "1")
    assert v1 is not None
    _register(key, "2", list(v1.stages))

    r2 = executor.create_run(
        db, pipeline_key=key, trigger_type="manual_rebuild", wiki_page_id="p-ver",
        pipeline_version="2", input_hash="H", workspace_id=None,
    )
    db.commit()
    assert r2.pipeline_version == "2"
    executor.execute_run(db, r2.id)
    assert r2.status == "succeeded"
    c2 = db.query(StageRun).filter(StageRun.run_id == r2.id, StageRun.stage_key == "canonicalize").first()
    assert json.loads(c2.metrics_json).get("cached") is not True  # 不同 pipeline_version 不命中

    r3 = executor.create_run(
        db, pipeline_key=key, trigger_type="manual_rebuild", wiki_page_id="p-ver",
        pipeline_version="1", input_hash="H", workspace_id=None,
    )
    db.commit()
    executor.execute_run(db, r3.id)
    c3 = db.query(StageRun).filter(StageRun.run_id == r3.id, StageRun.stage_key == "canonicalize").first()
    assert json.loads(c3.metrics_json).get("cached") is True  # 同版本命中


def test_cache_isolated_by_stage_key_and_version(db):
    """stage key/version 任一不同 → stage_input_hash 不同 → 不命中。"""
    import tempfile
    from pathlib import Path as _P

    url = f"sqlite:///{(_P(tempfile.mkdtemp()) / 'c.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    s = sessionmaker(bind=engine)()
    try:
        fake.register_fake_pipelines()
        _page(s, "pv")
        _single_cache_pipeline("cache.v1", "1")
        r1 = executor.create_run(s, pipeline_key="cache.v1", trigger_type="manual_rebuild",
                                 wiki_page_id="pv", input_hash="H")
        s.commit()
        executor.execute_run(s, r1.id)
        assert r1.status == "succeeded"
        art1 = s.query(Artifact).filter(Artifact.run_id == r1.id).one()

        # 同 key/version 再跑命中
        r2 = executor.create_run(s, pipeline_key="cache.v1", trigger_type="manual_rebuild",
                                 wiki_page_id="pv", input_hash="H")
        s.commit()
        executor.execute_run(s, r2.id)
        row2 = s.query(StageRun).filter(StageRun.run_id == r2.id).one()
        assert json.loads(row2.metrics_json).get("cached") is True

        # stage version 不同 → 不命中
        _single_cache_pipeline("cache.v1", "1", stage_version="2")
        r3 = executor.create_run(s, pipeline_key="cache.v1", trigger_type="manual_rebuild",
                                 wiki_page_id="pv", input_hash="H")
        s.commit()
        executor.execute_run(s, r3.id)
        row3 = s.query(StageRun).filter(StageRun.run_id == r3.id).one()
        assert json.loads(row3.metrics_json).get("cached") is not True
    finally:
        s.close()
        engine.dispose()


def test_cache_isolated_by_schema_version(db):
    """cache_schema_version 不同 → 不命中。"""
    _page(db, "pv-s")
    _single_cache_pipeline("cache.sv", "1", schema_version="sv/1")
    r1 = executor.create_run(db, pipeline_key="cache.sv", trigger_type="manual_rebuild",
                             wiki_page_id="pv-s", input_hash="H")
    db.commit()
    executor.execute_run(db, r1.id)
    assert r1.status == "succeeded"

    _single_cache_pipeline("cache.sv", "1", schema_version="sv/2")
    r2 = executor.create_run(db, pipeline_key="cache.sv", trigger_type="manual_rebuild",
                             wiki_page_id="pv-s", input_hash="H")
    db.commit()
    executor.execute_run(db, r2.id)
    row2 = db.query(StageRun).filter(StageRun.run_id == r2.id).one()
    assert json.loads(row2.metrics_json).get("cached") is not True  # schema 变不命中


def test_cache_isolated_by_upstream_hash(db):
    """上游产物 content_hash 变化 → 下游（默认依赖全部前置）不命中。"""
    page = _page(db, "p-up")
    _register("up.pipeline", "1", [
        StageDef(
            key="gen", version="1", retryable=True, cachable=False,
            execute=_upstream_gen_fn, failure_transition=FailureTransition.FAIL,
        ),
        StageDef(
            key="derive", version="1", retryable=True, cachable=True,
            cache_type="derive_out", cache_schema_version="sv/1",
            execute=_downstream_fn, failure_transition=FailureTransition.FAIL,
        ),
    ])

    def run_once(obj_id):
        r = executor.create_run(db, pipeline_key="up.pipeline", trigger_type="manual_rebuild",
                                wiki_page_id="p-up", trigger_object_id=obj_id, input_hash="H-same")
        db.commit()
        executor.execute_run(db, r.id)
        return r

    r1 = run_once("o1")
    assert r1.status == "succeeded"
    art1 = db.query(Artifact).filter(
        Artifact.run_id == r1.id, Artifact.artifact_type == "derive_out"
    ).one()

    # 同上游内容（obj 相同）→ derive 缓存命中
    r2 = run_once("o1")
    derive2 = db.query(StageRun).filter(StageRun.run_id == r2.id, StageRun.stage_key == "derive").one()
    assert json.loads(derive2.metrics_json).get("cached") is True

    # 上游内容变化（gen 产物 content_hash 依赖 trigger_object_id）→ derive 不命中
    r3 = run_once("o2")
    derive3 = db.query(StageRun).filter(StageRun.run_id == r3.id, StageRun.stage_key == "derive").one()
    assert json.loads(derive3.metrics_json).get("cached") is not True


def _upstream_gen_fn(db, run, stage_run, ctx):
    return {
        "ok": True,
        "artifact_type": "gen_out", "schema_version": "v1",
        "object_type": "x", "object_id": run.id,
        # Phase 4.2：content_hash 由系统对 payload 计算（payload 随 trigger_object_id 变化）
        "payload": {"g": run.trigger_object_id},
    }


def _downstream_fn(db, run, stage_run, ctx):
    return {
        "ok": True,
        "artifact_type": "derive_out", "schema_version": "sv/1",
        "object_type": "x", "object_id": run.id,
        "payload": {"d": 1},
    }


def test_cache_skips_after_failure_lineage_not_reused(db, pipelines):
    run = _mk_run(db, trigger_object_id="fail")
    executor.execute_run(db, run.id)
    assert run.status == "failed"
    assert db.query(Artifact).filter(
        Artifact.run_id == run.id, Artifact.artifact_type == "canonical_note"
    ).count() == 1  # 失败前 canonicalize 成功产物

    executor.requeue_failed_run(db, run)
    db.commit()
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed"
    # 失败 run 的 canonical_note 不可复用 → 重新执行，产生 attempt2 artifact
    assert db.query(Artifact).filter(
        Artifact.run_id == run.id, Artifact.artifact_type == "canonical_note"
    ).count() == 2


# ---------------------------------------------------------------------------
# 恢复 / lease / max_attempts
# ---------------------------------------------------------------------------

def test_requeue_stale_runs_heartbeat_timeout(db, pipelines):
    run = _mk_run(db)
    worker.claim_next_run(db)
    assert run.status == "running" and run.attempt == 1
    run.heartbeat_at = datetime.now() - timedelta(seconds=400)
    db.commit()
    n = worker.requeue_stale_runs(db, timeout_seconds=300)
    assert n == 1
    db.refresh(run)
    assert run.status == "queued"
    assert run.attempt == 1  # stale requeue 不加 attempt（下次 claim +）
    assert run.lease_token is None  # lease 已清除
    worker.claim_next_run(db)
    executor.execute_run(db, run.id)
    assert run.status == "succeeded"


def test_requeue_stale_skips_fresh_heartbeat(db, pipelines):
    run = _mk_run(db)
    worker.claim_next_run(db)  # heartbeat=now（未超时）
    assert worker.requeue_stale_runs(db, timeout_seconds=300) == 0
    db.refresh(run)
    assert run.status == "running"


def test_long_stage_lease_heartbeat_not_requeued(db, pipelines):
    """正常续租（heartbeat + lease_expires_at 前进）的 running run 不被 recovery 重排。"""
    run = _mk_run(db)
    claimed = worker.claim_next_run(db)
    renewer = worker.LeaseRenewer(db.get_bind(), run.id, claimed.lease_token, claimed.worker_id, lease_interval=0.05)
    renewer.start()
    try:
        assert worker.requeue_stale_runs(db, timeout_seconds=300) == 0
        db.expire_all()
        fresh = db.get(CompileRun, run.id)
        assert fresh.status == "running"
        assert fresh.heartbeat_at is not None
    finally:
        renewer.stop()


def test_expired_lease_recovery_closes_running_stage(db, pipelines):
    """lease 过期：running StageRun → failed(worker_lost)；queued → skipped。"""
    run = _mk_run(db)
    claimed = worker.claim_next_run(db)  # attempt 1 running + lease
    db.add_all([
        StageRun(run_id=run.id, stage_key="canonicalize", stage_order=0, status="running",
                 attempt=1, input_hash=run.input_hash, component_key="canonicalize", component_version="1",
                 started_at=datetime.now()),
        StageRun(run_id=run.id, stage_key="validate", stage_order=1, status="queued",
                 attempt=1, input_hash=run.input_hash, component_key="validate", component_version="1"),
    ])
    db.commit()
    # lease 过期：heartbeat 与 lease_expires_at 全部过期
    run.heartbeat_at = datetime.now() - timedelta(seconds=400)
    run.lease_expires_at = datetime.now() - timedelta(seconds=10)
    db.commit()

    n = worker.requeue_stale_runs(db, timeout_seconds=300)
    assert n == 1
    db.refresh(run)
    assert run.status == "queued" and run.attempt == 1
    srows = db.query(StageRun).filter(StageRun.run_id == run.id).all()
    statuses = {s.stage_key: (s.status, s.safe_error_code, s.finished_at is not None) for s in srows}
    assert statuses["canonicalize"] == ("failed", "worker_lost", True)
    assert statuses["validate"] == ("skipped", None, True)


def test_expired_lease_max_attempts_marks_failed(db, pipelines):
    """lease 过期且 attempt>=max → failed(retry_exhausted)，不再 requeue。"""
    run = _mk_run(db)
    worker.claim_next_run(db)
    db.add(StageRun(run_id=run.id, stage_key="canonicalize", stage_order=0, status="running",
                    attempt=3, input_hash=run.input_hash, component_key="canonicalize", component_version="1"))
    db.query(CompileRun).filter(CompileRun.id == run.id).update({
        "attempt": 3, "max_attempts": 3,
        "heartbeat_at": datetime.now() - timedelta(seconds=400),
        "lease_expires_at": datetime.now() - timedelta(seconds=10),
    })
    db.commit()
    n = worker.requeue_stale_runs(db, timeout_seconds=300)
    assert n == 0
    db.refresh(run)
    assert run.status == "failed"
    assert run.safe_error_code == "retry_exhausted"
    row = db.query(StageRun).filter(StageRun.run_id == run.id).one()
    assert row.status == "failed" and row.safe_error_code == "worker_lost"
    assert row.finished_at is not None


def test_recovery_is_idempotent(db, pipelines):
    """连续两次 recovery 结果一致（第一次已闭合 stage/已 requeue 不重复副作用）。"""
    run = _mk_run(db)
    worker.claim_next_run(db)
    run.heartbeat_at = datetime.now() - timedelta(seconds=400)
    db.commit()
    n1 = worker.requeue_stale_runs(db, timeout_seconds=300)
    state1 = db.get(CompileRun, run.id).status
    assert n1 == 1 and state1 == "queued"
    n2 = worker.requeue_stale_runs(db, timeout_seconds=300)
    assert n2 == 0
    assert db.get(CompileRun, run.id).status == "queued"
    assert db.query(StageRun).filter(StageRun.run_id == run.id).count() == 0


def test_requeue_stale_cancels_cancel_requested_stale(db, pipelines):
    run = _mk_run(db)
    worker.claim_next_run(db)
    run.cancel_requested = True
    run.heartbeat_at = datetime.now() - timedelta(seconds=400)
    db.commit()
    n = worker.requeue_stale_runs(db, timeout_seconds=300)
    assert n == 0  # 取消请求的僵死 run 不重新入队
    db.refresh(run)
    assert run.status == "cancelled" and run.finished_at is not None


def test_max_attempts_is_exact(db, pipelines):
    """max_attempts=3：最多真实执行 3 次，第 4 次 retry 抛 409，不再执行。"""
    run = _mk_run(db, trigger_object_id="fail")
    executions = 0
    for i in range(5):
        claimed = worker.claim_next_run(db)
        if claimed is None:
            break
        executions += 1
        executor.execute_run(db, claimed.id)
        db.refresh(run)
        assert run.status == "failed"
        if run.attempt < run.max_attempts:
            executor.requeue_failed_run(db, run)
            db.commit()
        else:
            with pytest.raises(CompileRunError) as exc:
                executor.requeue_failed_run(db, run)
            assert str(exc.value).startswith("retry_attempts_exhausted")
            db.rollback()
    assert executions == 3
    assert run.attempt == 3
    # 终态失败后没有多余 stage 行（不会出现第 4 次执行）
    attempts = {s.attempt for s in _stages(db, run.id)}
    assert max(attempts) == 3


def test_retry_api_attempt_semantics(db, pipelines):
    """retry API：failed→queued（不加 attempt）；attempt 到上限 → 409。"""
    run = _mk_run(db, trigger_object_id="fail")
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed" and run.attempt == 1
    r = executor.retry_run(db, run.id)
    db.commit()
    db.refresh(run)
    assert r.id == run.id and run.status == "queued" and run.attempt == 1
    # 再次执行（claim +1）→ 2
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed" and run.attempt == 2


def test_recover_startup_requeues_stale_and_keeps_queued(db, pipelines):
    stale = _mk_run(db, wiki_page_id="s1")
    worker.claim_next_run(db)
    stale.heartbeat_at = datetime.now() - timedelta(seconds=400)
    db.commit()
    queued = _mk_run(db, wiki_page_id="s2")

    result = worker.recover_startup(db)
    assert result["stale_requeued"] == 1
    assert result["queued"] >= 2
    db.refresh(stale)
    db.refresh(queued)
    assert stale.status == "queued" and stale.attempt == 1
    assert queued.status == "queued"

    claimed = worker.claim_next_run(db)
    assert claimed is not None
    executor.execute_run(db, claimed.id)
    db.refresh(claimed)
    assert claimed.status == "succeeded"


def test_db_queue_is_durable_when_concurrency_full(db, pipelines):
    runs = [_mk_run(db, wiki_page_id=f"q{i}") for i in range(5)]
    assert db.query(CompileRun).filter(CompileRun.status == "queued").count() == 5
    claimed = worker.claim_next_run(db)
    assert claimed is not None
    assert db.query(CompileRun).filter(CompileRun.status == "queued").count() == 4
    worker.stop_worker()
    remaining = db.query(CompileRun).filter(
        CompileRun.status.in_(("queued", "running"))
    ).count()
    assert remaining == 5
    executor.execute_run(db, claimed.id)
    for _ in range(4):
        nxt = worker.claim_next_run(db)
        assert nxt is not None
        executor.execute_run(db, nxt.id)
    assert db.query(CompileRun).filter(CompileRun.status == "succeeded").count() == 5


# ---------------------------------------------------------------------------
# registry 封闭
# ---------------------------------------------------------------------------

def test_registry_rejects_duplicate_stage_keys():
    with pytest.raises(PipelineError):
        PipelineDef(key="dup", version="1", stages=[StageDef(key="a"), StageDef(key="a")])


def test_registry_rejects_empty_or_blank_keys():
    with pytest.raises(PipelineError):
        StageDef(key="  ", version="1")
    with pytest.raises(PipelineError):
        PipelineDef(key="x", version=" ", stages=[StageDef(key="a")])


def test_registry_rejects_duplicate_pipeline_version():
    p1 = PipelineDef(key="dup-p", version="1", stages=[StageDef(key="a")])
    p2 = PipelineDef(key="dup-p", version="1", stages=[StageDef(key="a")])
    registry.register_pipeline(p1)
    with pytest.raises(PipelineError) as exc:
        registry.register_pipeline(p2)
    assert "pipeline_already_registered" in str(exc.value)
    # replace_for_test 可显式覆盖
    p3 = PipelineDef(key="dup-p", version="1", stages=[StageDef(key="a"), StageDef(key="b")])
    registry.replace_for_test(p3)
    assert registry.get_pipeline("dup-p").stage_keys() == ["a", "b"]


def test_registry_cacheable_requires_descriptor():
    with pytest.raises(PipelineError):
        StageDef(key="s", cachable=True, cache_type=None, cache_schema_version=None)
    with pytest.raises(PipelineError):
        StageDef(key="s", cachable=True, cache_type="t", cache_schema_version=None)
    # 完整 descriptor 合法
    StageDef(key="s", cachable=True, cache_type="t", cache_schema_version="v/1")


def test_registry_failure_transition_validation():
    with pytest.raises(PipelineError):
        StageDef(key="s", failure_transition="explode")
    ok = StageDef(key="s", failure_transition="cancel")
    assert ok.failure_transition == FailureTransition.CANCEL


def test_registry_empty_without_registration():
    assert registry.registered_pipelines() == []
    assert registry.get_pipeline("wiki.compile") is None
    assert registry.get_pipeline("fake.wiki.compile.v1") is None


def test_real_wiki_compile_not_hijacked(db, pipelines):
    keys = registry.registered_pipelines()
    assert "fake.wiki.compile.v1" in keys
    assert "wiki.compile" not in keys


def test_registry_validate_stage_result_unit():
    assert validate_stage_result({"ok": True}) == ""
    assert validate_stage_result({"ok": True, "artifact_type": None}) == ""
    assert validate_stage_result({"ok": True, "payload": {"a": 1}}) == ""
    assert validate_stage_result({"ok": False, "error_code": "STAGE_BAD"}) == ""
    assert validate_stage_result("nope") != ""
    assert validate_stage_result({"ok": "yes"}) != ""
    assert validate_stage_result({"ok": True, "error_code": 5}) != ""
    # Phase 4.2 拒绝矩阵：error_code 大写 snake；失败禁带 artifact/output_revision_id
    assert validate_stage_result({"ok": False, "error_code": "lower_bad"}) != ""
    assert validate_stage_result({"ok": False, "error_code": ""}) != ""
    assert validate_stage_result({"ok": False, "error_code": "E", "artifact_type": "x"}) != ""
    assert validate_stage_result({"ok": False, "error_code": "E", "output_revision_id": "r"}) != ""
    assert validate_stage_result({"ok": False, "error_code": "E", "retryable": "no"}) != ""
    # 成功字段类型：artifact_type=123 / metrics 非 dict / payload 含 datetime → violation
    assert validate_stage_result({"ok": True, "artifact_type": 123}) != ""
    assert validate_stage_result({"ok": True, "schema_version": object()}) != ""
    assert validate_stage_result({"ok": True, "content_hash": 5}) != ""
    assert validate_stage_result({"ok": True, "metrics": "not-a-dict"}) != ""
    from datetime import datetime
    assert validate_stage_result({"ok": True, "payload": datetime.now()}) != ""


def test_execute_contract_violation_fails_stage(db, pipelines):
    """stage 返回非 dict → stage_contract_violation，run 失败。"""
    def _bad(db, run, stage_run, ctx):
        return "not-a-dict"

    _register("bad.contract.v1", "1", [
        StageDef(key="s1", execute=_bad, failure_transition=FailureTransition.FAIL),
    ])
    run = executor.create_run(db, pipeline_key="bad.contract.v1", trigger_type="manual_rebuild")
    db.commit()
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed"
    row = db.query(StageRun).filter(StageRun.run_id == run.id).one()
    assert row.status == "failed"
    assert row.error_code == "STAGE_CONTRACT_VIOLATION"


def test_register_stage_cachable_no_descriptor_rejected_in_pipeline():
    with pytest.raises(PipelineError):
        PipelineDef(key="k", version="1", stages=[
            StageDef(key="s", cachable=True, cache_type=None, cache_schema_version=None),
        ])


# ---------------------------------------------------------------------------
# 错误分层
# ---------------------------------------------------------------------------

def test_sanitize_for_api_strips_internal_details():
    raw = (
        "failed at C:\\Users\\20474\\Documents\\secret.py:12 "
        "fetch https://api.internal.example/query?token=abc123 "
        "auth=Bearer eyJhbGciOiJIUzI1NiJ9 x Authorization: tok-leak "
        "Token abc.def"
    )
    safe = executor._sanitize_for_api(raw)
    assert safe is not None
    assert "[path]" in safe
    assert "[url]" in safe
    assert "Bearer [redacted]" in safe
    assert "Authorization [redacted]" in safe
    assert "eyJhbGciOiJIUzI1NiJ9" not in safe
    assert "secret.py" not in safe
    assert "api.internal.example" not in safe
    assert len(safe) <= executor.SAFE_ERROR_TRUNCATE


def test_stage_error_safe_columns_sanitized(db, pipelines):
    """stage 异常内含 Windows 路径/URL/token → safe_* 净化、error 原文只进日志。"""
    def _leaky(db, run, stage_run, ctx):
        raise RuntimeError(
            "boom C:\\Users\\20474\\AppData\\secret.db https://intra.x/leak?k=abc "
            "Bearer tok-999"
        )

    _register("leak.pipeline.v1", "1", [
        StageDef(key="s1", execute=_leaky, failure_transition=FailureTransition.FAIL),
    ])
    run = executor.create_run(db, pipeline_key="leak.pipeline.v1", trigger_type="manual_rebuild")
    db.commit()
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed"
    row = db.query(StageRun).filter(StageRun.run_id == run.id).one()
    # Phase 4.2：原始异常不进任何落库文本（safe 为服务端固定文案）。
    assert "secret.db" not in (row.safe_error_message or "")
    assert "fake explode" not in (row.error_message or "")
    assert "tok-999" not in (run.error_summary or "")
    assert row.error_code == "STAGE_EXCEPTION"
    assert row.error_message == row.safe_error_message
    assert row.safe_error_message == stage_error_message("STAGE_EXCEPTION")
    assert run.safe_error_code == "STAGE_EXCEPTION"


# ---------------------------------------------------------------------------
# FakePipeline 不劫持 / MANAGED fail closed / P42 alembic 往返
# ---------------------------------------------------------------------------

def test_fake_pipeline_writes_only_compile_tables(db, pipelines):
    _page(db, "p-fake")
    run = _mk_run(db, wiki_page_id="p-fake")
    executor.execute_run(db, run.id)
    assert run.status == "succeeded"
    assert db.query(WikiPage).count() == 1  # 只用了我们预建的 page，未新建
    assert db.query(Notebook).count() == 0


def test_compile_tables_managed_fail_closed(tmp_path):
    """已有核心表的库缺少 compile 表 → check_managed_migrations 报缺失（fail closed）。"""
    from app.models.database import Base, get_engine

    url = f"sqlite:///{(tmp_path / 'p41_sim.db').as_posix()}"
    engine = get_engine(url)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE knowledge_compile_artifacts")
            conn.exec_driver_sql("DROP TABLE knowledge_compile_stage_runs")
            conn.exec_driver_sql("DROP TABLE knowledge_compile_runs")
        missing = check_managed_migrations(engine)
        for t in ("knowledge_compile_runs", "knowledge_compile_stage_runs", "knowledge_compile_artifacts"):
            assert t in missing
    finally:
        engine.dispose()


def _alembic_run(db_file: Path, *args):
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
    r = subprocess.run(
        [PY, "-m", "alembic", *args],
        cwd=BACKEND_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return r.returncode == 0, r.stdout or "", r.stderr or ""


def _sqlite_tables(db_file: Path) -> set[str]:
    con = sqlite3.connect(str(db_file))
    try:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_alembic_p42_compile_migration_roundtrip(tmp_path):
    """空库 upgrade head 建出 compile 表 + request_fingerprint 列/索引 +
    reused_from_artifact_id 列/索引；downgrade P41 移除；upgrade head 恢复；
    guard 全程通过（fail closed 无残留）。"""
    db_file = tmp_path / "p42_mig.db"

    def compile_cols(table="knowledge_compile_runs"):
        con = sqlite3.connect(str(db_file))
        try:
            return {c[1] for c in con.execute(f"PRAGMA table_info({table})")}
        finally:
            con.close()

    def compile_indexes(table="knowledge_compile_runs"):
        con = sqlite3.connect(str(db_file))
        try:
            return {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
            )}
        finally:
            con.close()

    ok, _, err = _alembic_run(db_file, "upgrade", "head")
    assert ok, f"upgrade head failed: {err}"
    tables = _sqlite_tables(db_file)
    for t in ("knowledge_compile_runs", "knowledge_compile_stage_runs", "knowledge_compile_artifacts"):
        assert t in tables
    assert "request_fingerprint" in compile_cols()
    assert "ix_knowledge_compile_runs_request_fingerprint" in compile_indexes()
    # Phase 4.2：Artifact.reused_from_artifact_id 自引用 FK 列 + 索引随 upgrade 存在。
    assert "reused_from_artifact_id" in compile_cols("knowledge_compile_artifacts")
    assert "ix_knowledge_compile_artifacts_reused_from" in compile_indexes("knowledge_compile_artifacts")

    ok, _, err = _alembic_run(db_file, "downgrade", "b1c2d3e4f5a6")  # P41
    assert ok, f"downgrade P41 failed: {err}"
    for t in ("knowledge_compile_runs", "knowledge_compile_stage_runs", "knowledge_compile_artifacts"):
        assert t not in _sqlite_tables(db_file)

    ok, _, err = _alembic_run(db_file, "upgrade", "head")
    assert ok, f"upgrade head (roundtrip) failed: {err}"
    assert "knowledge_compile_runs" in _sqlite_tables(db_file)
    assert "request_fingerprint" in compile_cols()
    assert "ix_knowledge_compile_runs_request_fingerprint" in compile_indexes()
    # roundtrip 后 reused_from 列/索引恢复（随 downgrade 消失、随 upgrade 回归）。
    assert "reused_from_artifact_id" in compile_cols("knowledge_compile_artifacts")
    assert "ix_knowledge_compile_artifacts_reused_from" in compile_indexes("knowledge_compile_artifacts")

    # guard：upgrade head 后无缺失（compile 表 + 列 + 索引精确匹配）
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        assert check_managed_migrations(engine) == []
    finally:
        engine.dispose()



# ---------------------------------------------------------------------------
# Phase 4.2 反例：lease fencing / recovery reclaim / heartbeat（并发双 Session）
# ---------------------------------------------------------------------------

def _conc_engine(tmp_path, name="conc.db"):
    """文件型 SQLite + foreign_keys/busy_timeout（并发写友好），独立于 db fixture。"""
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=8000")

    init_db(engine)
    return engine


def _conc_session(engine):
    return sessionmaker(bind=engine)()


def _block_stage(entered, release):
    def _fn(db, run, stage_run, ctx):
        entered.set()
        release.wait(timeout=30)
        return {"ok": True, "payload": {"x": 1}}
    return _fn


def _run_worker_thread(worker_session, run_id, errors):
    def _target():
        try:
            executor.execute_run(worker_session, run_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            try:
                worker_session.rollback()
            except Exception:  # noqa: BLE001
                pass
    t = threading.Thread(target=_target)
    t.start()
    return t


def _query_run_artifacts(session, run_id):
    return session.query(Artifact).filter(Artifact.run_id == run_id)


def test_lost_lease_old_worker_cannot_write_artifact(tmp_path, pipelines):
    """run 执行中另一 session supersede → 原 worker 返回后 fence 失败 →
    0 artifact、run 不 succeeded（保持 superseded，stage 不落 succeeded）。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()
    _register("block.pipeline", "1", [
        StageDef(key="blk", version="1", cachable=False, execute=_block_stage(entered, release),
                 failure_transition=FailureTransition.FAIL),
    ])
    try:
        run = executor.create_run(main, pipeline_key="block.pipeline",
                                  trigger_type="manual_rebuild", trigger_object_id="obj-lost")
        main.commit()
        worker_session = _conc_session(engine)
        errors: list[str] = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        claimed = main.get(CompileRun, run.id)
        assert claimed.status == "running" and claimed.attempt == 1
        executor.supersede_matching_runs(
            main, pipeline_key="block.pipeline", trigger_type="manual_rebuild",
            trigger_object_id="obj-lost",
        )
        main.commit()
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        fresh = main.get(CompileRun, run.id)
        assert fresh.status == "superseded"  # 旧 worker 未能写 succeeded
        assert _query_run_artifacts(main, run.id).count() == 0
        rows = main.query(StageRun).filter(StageRun.run_id == run.id).all()
        assert rows and all(r.status != "succeeded" for r in rows)
    finally:
        main.close()
        engine.dispose()


def test_superseded_worker_cannot_publish(tmp_path, pipelines):
    """supersede 后旧 worker 不得 publish Revision（fence 拦截 output_revision_id）。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()

    def _pub(db, run, stage_run, ctx):
        entered.set()
        release.wait(timeout=30)
        return {"ok": True, "output_revision_id": f"rev-{run.id}"}

    _register("pub.block", "1", [
        StageDef(key="publish", version="1", cachable=False, execute=_pub,
                 failure_transition=FailureTransition.FAIL, allows_publish=True),
    ])
    try:
        run = executor.create_run(main, pipeline_key="pub.block",
                                  trigger_type="manual_rebuild", trigger_object_id="obj-pub")
        main.commit()
        worker_session = _conc_session(engine)
        errors: list[str] = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        assert main.get(CompileRun, run.id).status == "running"
        executor.supersede_matching_runs(
            main, pipeline_key="pub.block", trigger_type="manual_rebuild",
            trigger_object_id="obj-pub",
        )
        main.commit()
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        fresh = main.get(CompileRun, run.id)
        assert fresh.status == "superseded"
        assert fresh.output_revision_id is None  # 未 publish
    finally:
        main.close()
        engine.dispose()


def test_recovery_reclaim_old_worker_result_discarded(tmp_path, pipelines):
    """recovery 重新 claim 后旧 worker 结果被丢弃：旧 attempt 不写 artifact，
    run 最终由新 claim 收敛 succeeded（attempt 2）。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()
    _register("block.pipeline", "1", [
        StageDef(key="blk", version="1", cachable=False, execute=_block_stage(entered, release),
                 failure_transition=FailureTransition.FAIL),
    ])
    try:
        run = executor.create_run(main, pipeline_key="block.pipeline",
                                  trigger_type="manual_rebuild", trigger_object_id="obj-rec")
        main.commit()
        worker_session = _conc_session(engine)
        errors: list[str] = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        # 过期 lease → recovery requeue（attempt 保持 1，旧 lease token 清空）
        main.execute(
            text("UPDATE knowledge_compile_runs SET heartbeat_at=:hb, lease_expires_at=:le"),
            {"hb": datetime.now() - timedelta(seconds=400),
             "le": datetime.now() - timedelta(seconds=10)},
        )
        main.commit()
        assert worker.requeue_stale_runs(main, timeout_seconds=300) == 1
        main.expire_all()
        stale = main.get(CompileRun, run.id)
        assert stale.status == "queued" and stale.attempt == 1
        # 旧 worker 返回：fence 失败 → 不写 artifact
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        assert _query_run_artifacts(main, run.id).count() == 0
        # 新 worker claim + execute → succeeded attempt=2（旧结果被丢弃）
        assert worker.claim_next_run(main) is not None
        executor.execute_run(main, run.id)
        main.expire_all()
        final = main.get(CompileRun, run.id)
        assert final.status == "succeeded" and final.attempt == 2
        attempts = {(r.attempt, r.status)
                    for r in main.query(StageRun).filter(StageRun.run_id == run.id).all()}
        assert (1, "failed") in attempts          # 旧 attempt：worker_lost
        assert (2, "succeeded") in attempts       # 新 attempt：真正成功
    finally:
        main.close()
        engine.dispose()


def test_blocking_stage_heartbeat_really_advances_during_execute(tmp_path, pipelines):
    """长 stage 执行期间 LeaseRenewer heartbeat 真实推进（recovery 不重排，run 正常 succeeded）。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()
    _register("block.pipeline", "1", [
        StageDef(key="blk", version="1", cachable=False, execute=_block_stage(entered, release),
                 failure_transition=FailureTransition.FAIL),
    ])
    try:
        run = executor.create_run(main, pipeline_key="block.pipeline",
                                  trigger_type="manual_rebuild", trigger_object_id="obj-hb")
        main.commit()
        worker_session = _conc_session(engine)
        errors: list[str] = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        claimed = main.get(CompileRun, run.id)
        assert claimed.status == "running" and claimed.lease_token
        renewer = worker.LeaseRenewer(
            engine, run.id, claimed.lease_token, claimed.worker_id, lease_interval=0.05,
        )
        renewer.start()
        try:
            time.sleep(0.35)
            main.expire_all()
            fresh = main.get(CompileRun, run.id)
            # heartbeat 真实推进：lease_expires_at 被续租到未来
            assert fresh.lease_expires_at is not None
            assert fresh.lease_expires_at > datetime.now()
            assert worker.requeue_stale_runs(main, timeout_seconds=300) == 0  # 不重排
            main.commit()
        finally:
            renewer.stop()
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        final = main.get(CompileRun, run.id)
        assert final.status == "succeeded"  # lease 存活 → 旧 worker 正常完成
    finally:
        main.close()
        engine.dispose()



# ---------------------------------------------------------------------------
# Phase 4.2 反例：attempt 上限直调、heartbeat 归属、create_run 目标补全、registry
# ---------------------------------------------------------------------------

def test_direct_execute_exhausted_attempt_no_execute(db, pipelines):
    """attempt=3/max=3 queued 直调 execute_run/claim：不执行不 +attempt →
    归一 failed(retry_exhausted)，无 stage 行。"""
    run = _mk_run(db, trigger_object_id="fail")
    db.query(CompileRun).filter(CompileRun.id == run.id).update({
        "status": "queued", "attempt": 3, "max_attempts": 3,
    })
    db.commit()
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed"
    assert run.attempt == 3  # 绝不产生 attempt=4
    assert run.safe_error_code == "retry_exhausted"
    assert db.query(StageRun).filter(StageRun.run_id == run.id).count() == 0
    assert worker.claim_next_run(db) is None


def test_heartbeat_rejects_wrong_worker_or_token(db, pipelines):
    """heartbeat 精确匹配 run_id+lease_token+worker_id；错误归属一律 False。"""
    run = _mk_run(db)
    claimed = worker.claim_next_run(db)
    assert claimed is not None
    token, wid = claimed.lease_token, claimed.worker_id
    assert worker.heartbeat(db, run.id, lease_token="wrong-token", worker_id=wid) is False
    assert worker.heartbeat(db, run.id, lease_token=token, worker_id="wrong-worker") is False
    # lease_token IS NULL 宽松匹配被禁止（NULL 绝不续租）。
    assert worker.heartbeat(db, run.id, lease_token=None, worker_id=wid) is False
    assert worker.heartbeat(db, run.id, lease_token=token, worker_id=wid) is True
    db.expire_all()
    fresh = db.get(CompileRun, run.id)
    assert fresh.status == "running" and fresh.lease_token == token


# ---------------------------------------------------------------------------
# create_run 目标校验补全（Phase 4.2）
# ---------------------------------------------------------------------------

def test_create_run_rejects_wiki_page_null_workspace(db, pipelines):
    """wiki_page.workspace_id 为 NULL → 拒绝（NULL 不能跳过归属比较），不写 run。"""
    db.add(WikiPage(id="page-no-ws", title="x", workspace_id=None))
    db.commit()
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_rebuild", wiki_page_id="page-no-ws")
    assert str(exc.value).startswith("wiki_page_workspace_mismatch")
    assert db.query(CompileRun).count() == 0
    # 显式 workspace 也不豁免：page 无 workspace 与任何 effective 都无法匹配。
    _ws(db, "ws-x")
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_rebuild",
                            workspace_id="ws-x", wiki_page_id="page-no-ws")
    assert str(exc.value).startswith("wiki_page_workspace_mismatch")


def _mk_page_owned_by(db, page_id, nb_id, ws_id):
    _ws(db, ws_id)
    db.add(Notebook(id=nb_id, name=nb_id, group_id="engineering"))
    db.add(NotebookWorkspaceBinding(id=f"b-{nb_id}", notebook_id=nb_id,
                                    workspace_id=ws_id, status="active"))
    db.add(Page(id=page_id, notebook_id=nb_id, title=page_id, content="content"))
    db.commit()


def test_create_run_page_membership_via_notebook_binding(db, pipelines):
    """page_changed/page_deleted 的 trigger_object_id（Page）须通过 Notebook active
    binding 归属 effective workspace；无显式 workspace 时从 binding 推导。"""
    _mk_page_owned_by(db, "pg-a", "nb-a", "ws-a")
    run = executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                              trigger_type="page_changed", trigger_object_id="pg-a")
    db.commit()
    assert run.workspace_id == "ws-a"  # 归属推导

    _ws(db, "ws-b")
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="page_changed", trigger_object_id="pg-a",
                            workspace_id="ws-b")
    assert str(exc.value).startswith("page_workspace_mismatch")


def test_create_run_page_changed_object_must_be_bound_page(db, pipelines):
    """page_changed/page_deleted object 非 Page / Page 无 binding → 拒绝。"""
    _ws(db, "ws-a")
    db.add(Notebook(id="nb-u", name="nb-u", group_id="engineering"))
    db.add(Page(id="pg-unbound", notebook_id="nb-u", title="t", content="c"))  # 无 binding
    db.commit()
    for trigger in ("page_changed", "page_deleted"):
        with pytest.raises(CompileRunError) as exc:
            executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                                trigger_type=trigger, trigger_object_id="pg-unbound",
                                workspace_id="ws-a")
        assert str(exc.value).startswith("page_workspace_mismatch"), trigger
        with pytest.raises(CompileRunError) as exc:
            executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                                trigger_type=trigger, trigger_object_id="not-a-page",
                                workspace_id="ws-a")
        assert str(exc.value).startswith("page_workspace_mismatch"), trigger


def test_create_run_product_pipeline_requires_workspace(db, pipelines):
    """产品流水线（allow_null_workspace=False）无有效 workspace → 400 workspace_required。"""
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.strict",
                            trigger_type="manual_rebuild")
    assert str(exc.value).startswith("workspace_required")
    assert db.query(CompileRun).count() == 0
    # 控制：fake test pipeline（allow_null_workspace=True）允许 NULL workspace。
    run = executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                              trigger_type="manual_rebuild")
    db.commit()
    assert run.workspace_id is None


# ---------------------------------------------------------------------------
# registry exact-version（Phase 4.2）
# ---------------------------------------------------------------------------

def test_registry_same_key_different_versions_coexist():
    v1 = PipelineDef(key="ver.pipe", version="1", stages=[StageDef(key="a")], allow_null_workspace=True)
    v2 = PipelineDef(key="ver.pipe", version="2", stages=[StageDef(key="b")], allow_null_workspace=True)
    registry.replace_for_test(v1)
    registry.replace_for_test(v2)
    assert registry.registered_versions("ver.pipe") == ["1", "2"]
    assert registry.get_pipeline("ver.pipe").version == "2"      # active = 最新注册
    assert registry.get_pipeline("ver.pipe", "1").version == "1"
    assert registry.get_pipeline("ver.pipe", "2").version == "2"


def test_create_run_rejects_unregistered_pipeline_version(db, pipelines):
    """指定未注册 version → 拒绝不建 run（pipeline_version_not_registered）。"""
    with pytest.raises(CompileRunError) as exc:
        executor.create_run(db, pipeline_key="fake.wiki.compile.v1",
                            trigger_type="manual_rebuild", pipeline_version="999")
    assert str(exc.value).startswith("pipeline_version_not_registered")
    assert db.query(CompileRun).count() == 0


def test_queued_v1_then_register_v2_executes_v1(db, pipelines):
    """queued v1 后注册 v2 仍执行 v1（version 已在 run 固化）。"""
    key = "late.version.pipe"
    _register(key, "1", [
        StageDef(key="s1", version="1", execute=lambda db, run, stage, ctx: {
            "ok": True, "artifact_type": "ver_out", "schema_version": "v1",
            "object_type": "x", "object_id": run.id, "payload": {"version": "1"},
        }),
    ])
    run = executor.create_run(db, pipeline_key=key, trigger_type="manual_rebuild")
    db.commit()
    assert run.pipeline_version == "1"
    _register(key, "2", [
        StageDef(key="s2", version="2", execute=lambda db, run, stage, ctx: {
            "ok": True, "artifact_type": "ver_out", "schema_version": "v2",
            "object_type": "x", "object_id": run.id, "payload": {"version": "2"},
        }),
    ])
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "succeeded" and run.pipeline_version == "1"
    art = db.query(Artifact).filter(Artifact.run_id == run.id).one()
    assert art.schema_version == "v1"  # 按固化 v1 定义执行，未受 v2 影响


# ---------------------------------------------------------------------------
# CHECK 约束 DB 层（SQLite 强制 CHECK；raw SQL 尝试非法 status 断言拒绝）
# ---------------------------------------------------------------------------

def test_db_check_compile_run_status_and_attempt(db, pipelines):
    ok_sql = (
        "INSERT INTO knowledge_compile_runs "
        "(id, pipeline_key, pipeline_version, trigger_type, status, input_hash, attempt, max_attempts) "
        "VALUES (:id, :k, '1', 'manual_rebuild', :status, 'h', :attempt, :max)"
    )
    db.execute(text(ok_sql), {"id": "r-ok", "k": "k", "status": "queued", "attempt": 0, "max": 3})
    db.commit()
    # 非法 status → CHECK 拒绝
    with pytest.raises(IntegrityError):
        db.execute(text(ok_sql), {"id": "r-bad1", "k": "k", "status": "bogus", "attempt": 0, "max": 3})
        db.commit()
    db.rollback()
    # attempt <= max_attempts → 拒绝
    with pytest.raises(IntegrityError):
        db.execute(text(ok_sql), {"id": "r-bad2", "k": "k", "status": "queued", "attempt": 4, "max": 3})
        db.commit()
    db.rollback()
    # max_attempts >= 1 → 拒绝
    with pytest.raises(IntegrityError):
        db.execute(text(ok_sql), {"id": "r-bad3", "k": "k", "status": "queued", "attempt": 0, "max": 0})
        db.commit()
    db.rollback()
    # attempt >= 0 → 拒绝
    with pytest.raises(IntegrityError):
        db.execute(text(ok_sql), {"id": "r-bad4", "k": "k", "status": "queued", "attempt": -1, "max": 3})
        db.commit()
    db.rollback()
    assert db.get(CompileRun, "r-ok") is not None


def test_db_check_compile_stage_status(db, pipelines):
    db.execute(text(
        "INSERT INTO knowledge_compile_runs "
        "(id, pipeline_key, pipeline_version, trigger_type, status, input_hash, attempt, max_attempts) "
        "VALUES ('r-stage', 'k', '1', 'manual_rebuild', 'queued', 'h', 0, 3)"
    ))
    db.commit()
    db.execute(text(
        "INSERT INTO knowledge_compile_stage_runs "
        "(id, run_id, stage_key, stage_order, status, attempt) "
        "VALUES ('s-ok', 'r-stage', 'a', 0, 'queued', 1)"
    ))
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO knowledge_compile_stage_runs "
            "(id, run_id, stage_key, stage_order, status, attempt) "
            "VALUES ('s-bad', 'r-stage', 'a', 0, 'bogus', 1)"
        ))
        db.commit()
    db.rollback()



# ---------------------------------------------------------------------------
# Phase 4.2.1 最小安全闭合反例
# ---------------------------------------------------------------------------


def test_stage_calls_commit_is_blocked(db, pipelines):
    def _commit_stage(ss, run, stage_run, ctx):
        try:
            ss.commit()
        except executor.StageCommitForbidden:
            return {'ok': True}
        return {'ok': False, 'error_code': 'UNEXPECTED_ALLOWED'}
    _register('p.stage.commit', '1', [StageDef(
        key='s', execute=_commit_stage, failure_transition=FailureTransition.FAIL)])
    run = _mk_run(db, pipeline_key='p.stage.commit', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    db.expire_all()
    run = db.get(CompileRun, run.id)
    assert run.status == 'succeeded'  # stage 拦截 commit 后正常成功


def test_stage_commit_attempt_writes_nothing(db, pipelines):
    """Stage 自行 commit 被代码层阻止；其「尝试提交」不产生独立持久化副作用。

    受限 StageSession 的 add 只入当前事务，由 Pipeline 统一提交/回滚；stage 无法
    自行 commit → 不会提前把写入固化。验证 commit 调用抛 StageCommitForbidden，
    且 run 正常走 pipeline 事务完成。
    """
    commit_attempts = []

    def _committing_stage(ss, run, stage_run, ctx):
        try:
            ss.add(WikiPage(id='stage-orphan', title='x', workspace_id=_DEFAULT_WS_ID))
        except Exception:
            pass
        try:
            ss.commit()
            commit_attempts.append('allowed')
        except executor.StageCommitForbidden:
            commit_attempts.append('blocked')
        # stage 不得自行 commit；它也无法自行 rollback（同样被拦）——返回前不清理，
        # 孤儿对象仍挂在 pipeline 事务，由 execute_run 的成功路径统一提交或失败回滚。
        # 这里返回失败，验证 executor 对该孤儿对象做整体 rollback（不落库）。
        return {'ok': False, 'error_code': 'STAGE_FAILED_ON_PURPOSE'}
    _register('p.stage.commit2', '1', [StageDef(
        key='s', execute=_committing_stage, retryable=False,
        failure_transition=FailureTransition.FAIL)])
    _ws(db, _DEFAULT_WS_ID)
    db.commit()
    run = _mk_run(db, pipeline_key='p.stage.commit2', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    assert commit_attempts == ['blocked']
    db.expire_all()
    # stage 失败 → execute_run 失败路径提交 failed（孤儿 add 随成功路径不提交，
    # 失败路径 stage_rows 内不包含孤儿 WikiPage）——孤儿不落库。
    assert db.get(WikiPage, 'stage-orphan') is None


def test_stage_calls_rollback_is_blocked(db, pipelines):
    def _rollback_stage(ss, run, stage_run, ctx):
        try:
            ss.rollback()
        except executor.StageCommitForbidden:
            return {'ok': True}
        return {'ok': False, 'error_code': 'UNEXPECTED_ALLOWED'}
    _register('p.stage.rollback', '1', [StageDef(
        key='s', execute=_rollback_stage, failure_transition=FailureTransition.FAIL)])
    run = _mk_run(db, pipeline_key='p.stage.rollback', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    db.expire_all()
    assert db.get(CompileRun, run.id).status == 'succeeded'


def test_stage_cannot_access_inner_session(db, pipelines):
    leaked = []

    def _probe(ss, run, stage_run, ctx):
        try:
            inner = ss._inner
            leaked.append(inner)
        except executor.StageCommitForbidden:
            pass
        return {'ok': True}
    _register('p.stage.inner', '1', [StageDef(
        key='s', execute=_probe, failure_transition=FailureTransition.FAIL)])
    run = _mk_run(db, pipeline_key='p.stage.inner', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    assert leaked == []


def _claim_run_running(db, run_id):
    return executor.claim_by_id(db, run_id)


def test_expired_lease_cannot_be_renewed(db, pipelines):
    run = _mk_run(db, pipeline_key='fake.wiki.compile.v1', trigger_type='manual_rebuild')
    _, claimed = _claim_run_running(db, run.id)
    db.expire_all()
    claimed = db.get(CompileRun, run.id)
    claimed.lease_expires_at = datetime.now() - timedelta(seconds=10)
    db.commit()
    ok = worker.heartbeat(db, run.id, lease_token=claimed.lease_token, worker_id=claimed.worker_id)
    assert ok is False  # 过期 lease 不得复活
    db.expire_all()
    after = db.get(CompileRun, run.id)
    assert after.lease_expires_at < datetime.now()  # 未续期


def test_null_lease_cannot_be_renewed(db, pipelines):
    run = _mk_run(db, pipeline_key='fake.wiki.compile.v1', trigger_type='manual_rebuild')
    _, claimed = _claim_run_running(db, run.id)
    db.expire_all()
    claimed = db.get(CompileRun, run.id)
    claimed.lease_token = None
    claimed.worker_id = None
    db.commit()
    ok = worker.heartbeat(db, run.id, lease_token=None, worker_id=None)
    assert ok is False


def test_valid_unexpired_lease_can_be_renewed(db, pipelines):
    run = _mk_run(db, pipeline_key='fake.wiki.compile.v1', trigger_type='manual_rebuild')
    _, claimed = _claim_run_running(db, run.id)
    db.expire_all()
    claimed = db.get(CompileRun, run.id)
    ok = worker.heartbeat(db, run.id, lease_token=claimed.lease_token, worker_id=claimed.worker_id)
    assert ok is True


def test_heartbeat_rejects_wrong_worker_or_token(db, pipelines):
    run = _mk_run(db, pipeline_key='fake.wiki.compile.v1', trigger_type='manual_rebuild')
    _, claimed = _claim_run_running(db, run.id)
    db.expire_all()
    claimed = db.get(CompileRun, run.id)
    assert worker.heartbeat(db, run.id, lease_token='wrong-token', worker_id=claimed.worker_id) is False
    assert worker.heartbeat(db, run.id, lease_token=claimed.lease_token, worker_id='wrong-worker') is False


def test_stage_result_deeply_immutable():
    payload = {'a': [1, 2], 'b': {'k': 'v'}}
    metrics = {'m': [1]}
    result = StageResult(ok=True, payload=payload, metrics=metrics)
    payload['a'].append(3)
    payload['c'] = 'changed'
    metrics['x'] = 'changed'
    assert result.payload['a'] == [1, 2]
    assert 'c' not in result.payload
    assert 'x' not in result.metrics


def test_cache_descriptor_must_match_stage_definition(db, pipelines):
    def _fn(ss, run, stage_run, ctx):
        return {'ok': True, 'artifact_type': 'WRONG_TYPE', 'schema_version': 'wrong/sv',
                'payload': {'x': 1}}
    stage = StageDef(key='s', cachable=True, cache_type='expected_type',
                     cache_schema_version='expected/sv', execute=_fn,
                     failure_transition=FailureTransition.FAIL)
    _register('p.cache.mismatch', '1', [stage])
    run = _mk_run(db, pipeline_key='p.cache.mismatch', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    db.expire_all()
    rows = db.query(StageRun).filter(StageRun.run_id == run.id).all()
    assert rows and rows[0].status == 'failed'
    assert rows[0].safe_error_code == 'STAGE_CONTRACT_VIOLATION'


def test_content_hash_without_artifact_rejected(db, pipelines):
    def _fn(ss, run, stage_run, ctx):
        return {'ok': True, 'content_hash': 'forged', 'payload': {'x': 1}}
    _register('p.noartifact', '1', [StageDef(key='s', execute=_fn,
                                             failure_transition=FailureTransition.FAIL)])
    run = _mk_run(db, pipeline_key='p.noartifact', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    db.expire_all()
    rows = db.query(StageRun).filter(StageRun.run_id == run.id).all()
    assert rows[0].safe_error_code == 'STAGE_CONTRACT_VIOLATION'


def test_failed_result_cannot_carry_payload_or_metrics(db, pipelines):
    for fields in [
        {'payload': {'x': 1}},
        {'metrics': {'m': 1}},
        {'schema_version': 'sv'},
        {'artifact_type': 'a'},
        {'content_hash': 'h'},
        {'output_revision_id': 'rev'},
    ]:
        bad = {'ok': False, 'error_code': 'SOME_ERROR', **fields}
        violation = validate_stage_result(bad)
        assert violation != '', fields


def test_string_cache_key_stage_keys_rejected():
    with pytest.raises(PipelineError):
        StageDef(key='s', cache_key_stage_keys='upstream', execute=lambda *a: None)


def test_non_bool_stage_flags_rejected():
    for kwargs in [{'retryable': 1}, {'cachable': 'yes'}, {'allows_publish': 1}]:
        with pytest.raises(PipelineError):
            StageDef(key='s', execute=lambda *a: None, **kwargs)
    with pytest.raises(PipelineError):
        PipelineDef(key='p', version='1', allow_null_workspace='yes', stages=[])



def test_cache_hit_requires_result_fence(tmp_path, pipelines, monkeypatch):
    """缓存命中后写当前 Artifact 前 fence 失败 -> 当前 run 无 Artifact、run 不 succeeded。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    try:
        _single_cache_pipeline('cachefence', '1')
        r1 = executor.create_run(main, pipeline_key='cachefence', trigger_type='manual_rebuild',
                                 trigger_object_id='x', input_hash='H')
        main.commit()
        executor.execute_run(main, r1.id)
        assert r1.status == 'succeeded'
        main.expire_all()

        r2 = executor.create_run(main, pipeline_key='cachefence', trigger_type='manual_rebuild',
                                 trigger_object_id='x', input_hash='H')
        main.commit()
        real_fence = executor._fence_run

        def _fence_lost(db, run, **kwargs):
            # 模拟结果持久化前 lease 已失效/run 被终结（side 已提交）：
            # fence 返回 False → 缓存命中路径不得写当前 Artifact。
            return False

        monkeypatch.setattr(executor, '_fence_run', _fence_lost)
        executor.execute_run(main, r2.id)
        monkeypatch.setattr(executor, '_fence_run', real_fence)
        main.expire_all()
        r2f = main.get(CompileRun, r2.id)
        assert r2f.status != 'succeeded'
        assert main.query(Artifact).filter(Artifact.run_id == r2.id).count() == 0
    finally:
        main.close()
        engine.dispose()


def test_final_success_is_fenced_cas(tmp_path, pipelines, monkeypatch):
    """Run 最终 succeeded 必须经原子条件 UPDATE（fenced CAS），非 apply+commit。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    try:
        fake.register_fake_pipelines()
        run = executor.create_run(main, pipeline_key='fake.wiki.compile.v1',
                                  trigger_type='manual_rebuild', trigger_object_id='fs-final')
        main.commit()
        real_final = executor._finalize_run_succeeded

        def _fake_final(db, run, **kwargs):
            side = _conc_session(engine)
            try:
                executor.supersede_matching_runs(
                    side, pipeline_key='fake.wiki.compile.v1',
                    trigger_type='manual_rebuild', trigger_object_id='fs-final')
                side.commit()
            finally:
                side.close()
            # 复用真实 fenced CAS（带本 worker 捕获 lease 身份）→ 应返回 False
            return real_final(db, run, **kwargs)

        monkeypatch.setattr(executor, '_finalize_run_succeeded', _fake_final)
        executor.execute_run(main, run.id)
        monkeypatch.setattr(executor, '_finalize_run_succeeded', real_final)
        main.expire_all()
        fresh = main.get(CompileRun, run.id)
        assert fresh.status != 'succeeded'
        assert fresh.status in ('running', 'superseded')
    finally:
        main.close()
        engine.dispose()


def test_supersede_between_check_and_artifact_commit(tmp_path, pipelines):
    """stage 执行返回后、artifact 提交前被 supersede -> fence 拦截，0 artifact。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()

    def _block_ok(db, run, stage_run, ctx):
        entered.set()
        release.wait(timeout=30)
        return {'ok': True, 'artifact_type': 'canonical_note', 'schema_version': 'v1',
                'object_type': 'x', 'object_id': run.id, 'payload': {'b': 1}}
    _register('fence.between', '1', [StageDef(
        key='gen', version='1', execute=_block_ok, failure_transition=FailureTransition.FAIL,
        cachable=False)])
    try:
        run = executor.create_run(main, pipeline_key='fence.between',
                                  trigger_type='manual_rebuild', trigger_object_id='obj-fb')
        main.commit()
        worker_session = _conc_session(engine)
        errors = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        assert main.get(CompileRun, run.id).status == 'running'
        executor.supersede_matching_runs(
            main, pipeline_key='fence.between', trigger_type='manual_rebuild',
            trigger_object_id='obj-fb')
        main.commit()
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        fresh = main.get(CompileRun, run.id)
        assert fresh.status == 'superseded'
        assert main.query(Artifact).filter(Artifact.run_id == run.id).count() == 0
        rows = main.query(StageRun).filter(StageRun.run_id == run.id).all()
        assert rows and all(r.status != 'succeeded' for r in rows)
    finally:
        main.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Phase 4.2.2 最终事务闭合反例
# ---------------------------------------------------------------------------


def test_stage_query_cannot_access_original_session(db, pipelines):
    leaked = []

    def _probe(ss, run, stage_run, ctx):
        q = ss.query(WikiPage)
        try:
            leaked.append(q.session)
        except executor.StageCommitForbidden:
            pass
        try:
            leaked.append(q._inner)
        except executor.StageCommitForbidden:
            pass
        return {'ok': True}

    _register('q.escape', '1', [StageDef(key='s', execute=_probe,
                                         failure_transition=FailureTransition.FAIL)])
    run = _mk_run(db, pipeline_key='q.escape', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    assert leaked == []
    db.expire_all()
    assert db.get(CompileRun, run.id).status == 'succeeded'


def test_stage_connection_is_forbidden(db, pipelines):
    leaked = []

    def _probe(ss, run, stage_run, ctx):
        for attr in ('connection',):
            try:
                leaked.append(getattr(ss, attr))
            except (executor.StageCommitForbidden, AttributeError):
                pass
        return {'ok': True}

    _register('conn.forbid', '1', [StageDef(key='s', execute=_probe,
                                            failure_transition=FailureTransition.FAIL)])
    run = _mk_run(db, pipeline_key='conn.forbid', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    assert leaked == []


def test_stage_get_bind_is_forbidden(db, pipelines):
    leaked = []

    def _probe(ss, run, stage_run, ctx):
        try:
            leaked.append(ss.get_bind)
        except (executor.StageCommitForbidden, AttributeError):
            pass
        return {'ok': True}

    _register('bind.forbid', '1', [StageDef(key='s', execute=_probe,
                                            failure_transition=FailureTransition.FAIL)])
    run = _mk_run(db, pipeline_key='bind.forbid', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    assert leaked == []


def test_stage_cannot_commit_via_query_session(db, pipelines):
    """即使能拿到底层 Query，也无法经其 .session.commit() 逃逸提交。"""
    from app.models.database import WikiSection
    wrote = []

    def _probe(ss, run, stage_run, ctx):
        try:
            q = ss.query(WikiPage)
            # 尝试各种访问路径触发逃逸
            target = None
            try:
                target = q.session
            except executor.StageCommitForbidden:
                pass
            try:
                target = q._inner
            except executor.StageCommitForbidden:
                pass
            try:
                target = ss.execute
            except (executor.StageCommitForbidden, AttributeError):
                pass
            if target is not None:
                try:
                    target.commit()
                    wrote.append('commit-called')
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            wrote.append(repr(exc))
        return {'ok': True}

    _register('vq.escape', '1', [StageDef(key='s', execute=_probe,
                                          failure_transition=FailureTransition.FAIL)])
    _page(db, 'pg-extra')
    run = _mk_run(db, pipeline_key='vq.escape', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    db.expire_all()
    assert wrote == []
    assert db.get(CompileRun, run.id).status == 'succeeded'


def test_stage_transaction_escape_writes_nothing(db, pipelines):
    """stage 尝试逃逸提交写入 → 全部被拦；stage 失败时孤儿被 pipeline rollback，
    数据库最终零写入（不只断言抛异常）。"""
    before = db.query(WikiPage).count()

    def _escape(ss, run, stage_run, ctx):
        # 尝试经 add 写入孤儿（若逃逸成功会落库）
        try:
            ss.add(WikiPage(id='escape-write', title='x', workspace_id=_DEFAULT_WS_ID))
        except Exception:
            pass
        # 逃逸 commit 尝试（均应被拦）
        try:
            ss.commit()
        except executor.StageCommitForbidden:
            pass
        try:
            ss.connection.commit()
        except (executor.StageCommitForbidden, AttributeError):
            pass
        # stage 失败 → pipeline 回滚 SAVEPOINT，孤儿不得残留
        return {'ok': False, 'error_code': 'STAGE_ESCAPE_PROBE'}

    _register('esc.write', '1', [StageDef(key='s', execute=_escape, retryable=False,
                                          failure_transition=FailureTransition.FAIL)])
    _ws(db, _DEFAULT_WS_ID)
    db.commit()
    run = _mk_run(db, pipeline_key='esc.write', trigger_type='manual_rebuild')
    executor.execute_run(db, run.id)
    db.expire_all()
    assert db.query(WikiPage).count() == before  # 逃逸写入零落库
    assert db.get(CompileRun, run.id).status == 'failed'
    assert db.get(WikiPage, 'escape-write') is None


# ---------------------------------------------------------------------------
# Phase 4.2.2 fence 确定性竞态（Event/Barrier，非 sleep）
# ---------------------------------------------------------------------------


def _conc_run(db, pipeline_key, trigger_object_id):
    return executor.create_run(db, pipeline_key=pipeline_key,
                               trigger_type='manual_rebuild',
                               trigger_object_id=trigger_object_id)


def test_supersede_between_precheck_and_stage_start(tmp_path, pipelines):
    """stage 已过开始 fence 进入执行后，期间被 supersede → 结果 fence 拦截，0 artifact。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()

    def _blk(db, run, stage_run, ctx):
        entered.set()
        release.wait(timeout=30)
        return {'ok': True, 'artifact_type': 'canonical_note', 'schema_version': 'v1',
                'object_type': 'x', 'object_id': run.id, 'payload': {'a': 1}}
    _register('race.start', '1', [StageDef(key='s', version='1', execute=_blk,
                                           failure_transition=FailureTransition.FAIL)])
    try:
        run = _conc_run(main, 'race.start', 'obj-start')
        main.commit()
        worker_session = _conc_session(engine)
        errors = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        assert main.get(CompileRun, run.id).status == 'running'
        executor.supersede_matching_runs(main, pipeline_key='race.start',
                                         trigger_type='manual_rebuild',
                                         trigger_object_id='obj-start')
        main.commit()
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        fresh = main.get(CompileRun, run.id)
        assert fresh.status == 'superseded'
        assert main.query(Artifact).filter(Artifact.run_id == run.id).count() == 0
    finally:
        main.close()
        engine.dispose()


def test_recovery_between_precheck_and_stage_start(tmp_path, pipelines):
    """stage 执行期间被 recovery requeue → 结果 fence 失败，旧 attempt 不写 artifact。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()

    def _blk(db, run, stage_run, ctx):
        entered.set()
        release.wait(timeout=30)
        return {'ok': True, 'artifact_type': 'canonical_note', 'schema_version': 'v1',
                'object_type': 'x', 'object_id': run.id, 'payload': {'a': 1}}
    _register('race.recover', '1', [StageDef(key='s', version='1', execute=_blk,
                                             failure_transition=FailureTransition.FAIL)])
    try:
        run = _conc_run(main, 'race.recover', 'obj-rec2')
        main.commit()
        worker_session = _conc_session(engine)
        errors = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        run_running = main.get(CompileRun, run.id)
        assert run_running.status == 'running'
        # 强制 lease 过期后 recovery requeue（换 token，旧 worker fence 必失败）
        main.execute(
            CompileRun.__table__.update().where(
                CompileRun.id == run.id).values(
                lease_expires_at=datetime.now() - timedelta(seconds=30),
                heartbeat_at=datetime.now() - timedelta(seconds=30)))
        main.commit()
        worker.requeue_stale_runs(main, timeout_seconds=10)
        main.commit()
        main.expire_all()
        assert main.get(CompileRun, run.id).status == 'queued'
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        # 旧 attempt 结果未写 artifact
        assert main.query(Artifact).filter(Artifact.run_id == run.id).count() == 0
    finally:
        main.close()
        engine.dispose()


def test_supersede_while_stage_returns_failure(tmp_path, pipelines):
    """stage 返回失败前被 supersede → 失败状态 fence 失败 → run 保持 superseded，不改写。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()

    def _fail(db, run, stage_run, ctx):
        entered.set()
        release.wait(timeout=30)
        return {'ok': False, 'error_code': 'STAGE_BLOCKED_FAIL'}
    _register('race.fail', '1', [StageDef(key='s', version='1', execute=_fail, retryable=False,
                                          failure_transition=FailureTransition.FAIL)])
    try:
        run = _conc_run(main, 'race.fail', 'obj-fail2')
        main.commit()
        worker_session = _conc_session(engine)
        errors = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        executor.supersede_matching_runs(main, pipeline_key='race.fail',
                                         trigger_type='manual_rebuild',
                                         trigger_object_id='obj-fail2')
        main.commit()
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        fresh = main.get(CompileRun, run.id)
        assert fresh.status == 'superseded'  # 未被旧 worker 改写 failed
    finally:
        main.close()
        engine.dispose()


def test_recovery_while_stage_returns_failure(tmp_path, pipelines):
    """stage 返回失败前被 recovery requeue → 失败 fence 失败，旧 worker 不改状态。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()

    def _fail(db, run, stage_run, ctx):
        entered.set()
        release.wait(timeout=30)
        return {'ok': False, 'error_code': 'STAGE_BLOCKED_FAIL2'}
    _register('race.failrec', '1', [StageDef(key='s', version='1', execute=_fail, retryable=False,
                                             failure_transition=FailureTransition.FAIL)])
    try:
        run = _conc_run(main, 'race.failrec', 'obj-fr')
        main.commit()
        worker_session = _conc_session(engine)
        errors = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        main.execute(
            CompileRun.__table__.update().where(
                CompileRun.id == run.id).values(
                lease_expires_at=datetime.now() - timedelta(seconds=30),
                heartbeat_at=datetime.now() - timedelta(seconds=30)))
        main.commit()
        worker.requeue_stale_runs(main, timeout_seconds=10)
        main.commit()
        main.expire_all()
        assert main.get(CompileRun, run.id).status == 'queued'
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        # run 保持 queued（旧 worker 未能把它改成 failed），新 claim 后可重跑
        assert main.get(CompileRun, run.id).status == 'queued'
    finally:
        main.close()
        engine.dispose()


def test_lost_worker_failure_cannot_overwrite_new_attempt(tmp_path, pipelines):
    """旧 worker 丢失后新 attempt 已被 claim，旧 worker 失败 fence 不得覆盖新 attempt。"""
    engine = _conc_engine(tmp_path)
    main = _conc_session(engine)
    entered = threading.Event()
    release = threading.Event()

    def _fail(db, run, stage_run, ctx):
        entered.set()
        release.wait(timeout=30)
        return {'ok': False, 'error_code': 'STAGE_LOST_OLD'}
    _register('race.lost', '1', [StageDef(key='s', version='1', execute=_fail, retryable=False,
                                          failure_transition=FailureTransition.FAIL)])
    try:
        run = _conc_run(main, 'race.lost', 'obj-lost')
        main.commit()
        worker_session = _conc_session(engine)
        errors = []
        t = _run_worker_thread(worker_session, run.id, errors)
        assert entered.wait(30)
        main.expire_all()
        # 旧 worker lease 过期 → requeue → 另一 worker（新 lease）claim 为新 attempt
        main.execute(
            CompileRun.__table__.update().where(
                CompileRun.id == run.id).values(
                lease_expires_at=datetime.now() - timedelta(seconds=30),
                heartbeat_at=datetime.now() - timedelta(seconds=30)))
        main.commit()
        worker.requeue_stale_runs(main, timeout_seconds=10)
        main.commit()
        main.expire_all()
        # 新 worker claim（attempt+1、新 lease_token）
        claimed = worker.claim_next_run(main, worker_id='new-worker')
        main.commit()
        assert claimed is not None and claimed.status == 'running'
        assert claimed.attempt >= 2
        # 旧 worker 放行返回失败 → 其 fence 因 lease_token 不匹配而失败
        release.set()
        t.join(timeout=30)
        worker_session.close()
        assert not errors, errors
        main.expire_all()
        fresh = main.get(CompileRun, run.id)
        # 新 attempt 的 running 未被旧 worker 覆盖为 failed（旧 fence 失败不改写）
        assert fresh.status == 'running'
        assert fresh.worker_id == 'new-worker'
    finally:
        main.close()
        engine.dispose()
