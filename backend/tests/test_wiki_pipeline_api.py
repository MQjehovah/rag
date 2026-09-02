"""Phase 4.1：wiki-compile 管理 API 测试（admin-only + 敏感字段白名单 + 错误映射）。

覆盖：
- 非 admin 访问全部端点 → 403；admin 正常。
- GET /runs 分页列表（摘要字段，不含内部列 input_hash/idempotency_key/request_fingerprint）。
- GET /runs/{id} 含 stage + artifact 摘要；payload_json/prompt/正文/token/ACL 不泄露。
- 序列化 safe_* 字段（净化后）；error_summary 截断。
- POST retry：failed & attempt<max → queued（attempt 由 claim +1）；attempt 用尽 →
  409 retry_attempts_exhausted；superseded → 409 run_superseded_use_create；
  succeeded → 409。
- POST cancel：queued → cancelled；running → cancel_requested；终态 → 409。
- api_does_not_expose_internal_exception：异常含 Windows 路径/Bearer Token/SQL/
  URL query → API 响应不含内部片段。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core import jwt_utils
from app.core.wiki_pipeline import executor, fake, registry, worker
from app.core.wiki_pipeline.executor import CompileRunError
from app.core.wiki_pipeline.registry import (
    FailureTransition,
    PipelineDef,
    StageDef,
    stage_error_message,
)
from app.main import app
from app.models.database import (
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    WikiPage,
    WikiWorkspace,
    init_db,
)


@pytest.fixture(autouse=True)
def _registry_cleanup():
    registry.REGISTRY.clear()
    yield
    registry.REGISTRY.clear()
    worker.stop_worker()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'compile_api.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)
    fake.register_fake_pipelines()

    db = sessionmaker(bind=engine)()

    def _admin():
        return {"id": "u-admin", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _eng():
        return {"id": "u-eng", "username": "eng", "groups": ["engineering"], "is_admin": False}

    c = TestClient(app)
    yield c, db, _admin, _eng
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()
    fake.unregister_fake_pipelines()


def _override(user):
    app.dependency_overrides[jwt_utils.get_current_user] = user


def _page(db, page_id: str, workspace_id: str | None = None):
    if db.get(WikiPage, page_id) is not None:
        return
    if workspace_id is None:
        # Phase 4.2：wiki_page.workspace_id 必须非空（create_run 严格校验 NULL）。
        ws = db.get(WikiWorkspace, "ws-default")
        if ws is None:
            db.add(WikiWorkspace(id="ws-default", key="ws_key_api_default", name="default",
                                 acl_scope='{"groups": ["engineering"]}', scope_id="group:engineering",
                                 status="active"))
            db.flush()
        workspace_id = "ws-default"
    db.add(WikiPage(id=page_id, title=page_id, workspace_id=workspace_id))
    db.flush()
    db.commit()


def _mk_succeeded_run(db) -> CompileRun:
    _page(db, "w-api")
    run = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild", wiki_page_id="w-api"
    )
    db.commit()
    executor.execute_run(db, run.id)
    db.refresh(run)
    # 塞入“敏感”payload / metrics，验证 API 永不回显
    canon = db.query(StageRun).filter(
        StageRun.run_id == run.id, StageRun.stage_key == "canonicalize"
    ).first()
    art = db.query(Artifact).filter(
        Artifact.stage_run_id == canon.id
    ).first()
    art.payload_json = json.dumps({"prompt": "SUPER_SECRET_PROMPT", "note": "正文内容", "acl": "internal", "token": "tok-x"})
    canon.metrics_json = json.dumps({"cached": True, "source_artifact_id": "a1", "internal_trace": "SECRET_TRACE"})
    db.commit()
    return run


def _mk_failed_run(db, page_id="w-fail"):
    _page(db, page_id)
    run = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="fail", wiki_page_id=page_id,
    )
    db.commit()
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed"
    return run


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------

def test_non_admin_403_all_endpoints(client):
    c, db, _admin, _eng = client
    _page(db, "p403")
    run = executor.create_run(db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild", wiki_page_id="p403")
    db.commit()
    _override(_eng)
    assert c.get("/api/wiki-compile/runs").status_code == 403
    assert c.get(f"/api/wiki-compile/runs/{run.id}").status_code == 403
    assert c.post(f"/api/wiki-compile/runs/{run.id}/retry").status_code == 403
    assert c.post(f"/api/wiki-compile/runs/{run.id}/cancel").status_code == 403


# ---------------------------------------------------------------------------
# 列表 / 详情 / 敏感过滤
# ---------------------------------------------------------------------------

def test_admin_list_runs_summary(client):
    c, db, _admin, _eng = client
    _mk_succeeded_run(db)
    _override(_admin)
    r = c.get("/api/wiki-compile/runs")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    run0 = body["runs"][0]
    assert run0["pipeline_key"] == "fake.wiki.compile.v1"
    assert run0["status"] == "succeeded"
    assert set(run0) >= {"id", "pipeline_version", "trigger_type", "wiki_page_id",
                         "workspace_id", "source_sync_run_id", "error_summary",
                         "safe_error_code", "safe_error_message", "created_at"}
    # 摘要不允许携带敏感/内部字段
    for secret in ("payload_json", "input_hash", "idempotency_key", "request_fingerprint",
                   "lease_token", "worker_id"):
        assert secret not in run0


def test_admin_get_run_detail_sensitive_filtered(client):
    c, db, _admin, _eng = client
    run = _mk_succeeded_run(db)
    _override(_admin)
    r = c.get(f"/api/wiki-compile/runs/{run.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == run.id
    assert body["status"] == "succeeded"
    assert "stages" in body and "artifacts" in body
    text = r.text
    assert "SUPER_SECRET_PROMPT" not in text
    assert "payload_json" not in body["artifacts"][0]
    assert "payload_json" not in text
    for word in ("prompt", "token", "acl"):
        assert f'"{word}"' not in text.lower()
    stage0 = body["stages"][0]
    assert stage0["metrics_summary"] == {"cached": True, "source_artifact_id": "a1"}
    assert "internal_trace" not in text
    assert "SECRET_TRACE" not in text


def test_admin_get_run_404(client):
    c, db, _admin, _eng = client
    _override(_admin)
    assert c.get("/api/wiki-compile/runs/not-exist").status_code == 404


def test_admin_failed_run_safe_fields_serialized(client):
    """失败 run/stage 的 error 字段只回 safe（服务端固定映射文案），error_summary 截断。"""
    c, db, _admin, _eng = client
    run = _mk_failed_run(db)
    _override(_admin)
    r = c.get(f"/api/wiki-compile/runs/{run.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["safe_error_code"] == "FAKE_VALIDATION_FAILURE"
    assert body["safe_error_message"] == stage_error_message("FAKE_VALIDATION_FAILURE")
    assert body["error_summary"] is not None
    validate = next(s for s in body["stages"] if s["stage_key"] == "validate")
    assert validate["safe_error_code"] == "FAKE_VALIDATION_FAILURE"
    assert validate["error_code"] == "FAKE_VALIDATION_FAILURE"
    assert validate["safe_error_message"] == stage_error_message("FAKE_VALIDATION_FAILURE")


# ---------------------------------------------------------------------------
# 内部异常不外泄
# ---------------------------------------------------------------------------

def _register_sneaky_leak_pipeline():
    def _leak(db, run, stage_run, ctx):
        raise RuntimeError(
            "sqlite3.OperationalError C:\\Users\\20474\\Documents\\secret.db "
            "https://intra.internal.example/leak?key=abc123 "
            "Bearer eyJhbGciOiJIUzI1NiJ9 abc "
            "Authorization: tok-999 SELECT * FROM users"
        )
    pipeline = PipelineDef(
        key="sneaky.leak.v1", version="1",
        stages=[StageDef(key="s1", execute=_leak, failure_transition=FailureTransition.FAIL)],
        allow_null_workspace=True,
    )
    registry.replace_for_test(pipeline)


def test_api_does_not_expose_internal_exception(client):
    """异常含 Windows 路径/Bearer/SQL/URL query → API 响应不含内部片段（safe 净化）。"""
    c, db, _admin, _eng = client
    _register_sneaky_leak_pipeline()
    run = executor.create_run(db, pipeline_key="sneaky.leak.v1", trigger_type="manual_rebuild")
    db.commit()
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "failed"
    _override(_admin)
    r = c.get(f"/api/wiki-compile/runs/{run.id}")
    assert r.status_code == 200
    text = r.text
    for leak in (
        "C:\\Users\\20474\\Documents\\secret.db", "secret.db",
        "intra.internal.example", "?key=abc123",
        "eyJhbGciOiJIUzI1NiJ9", "tok-999",
        "SELECT * FROM users",
    ):
        assert leak not in text
    body = r.json()
    # Phase 4.2：safe 为服务端固定映射文案，不含清洗后的原文片段。
    assert body["safe_error_code"] == "STAGE_EXCEPTION"
    assert body["safe_error_message"] == stage_error_message("STAGE_EXCEPTION")
    assert "secret.db" not in body["safe_error_message"]
    assert "[path]" not in body["safe_error_message"]
    assert "redacted" not in body["safe_error_message"]
    stage0 = body["stages"][0]
    assert stage0["error_code"] == "STAGE_EXCEPTION"
    assert stage0["safe_error_message"] == stage_error_message("STAGE_EXCEPTION")


# ---------------------------------------------------------------------------
# retry / cancel
# ---------------------------------------------------------------------------

def test_admin_retry_failed_requeues_attempt(client):
    c, db, _admin, _eng = client
    run = _mk_failed_run(db, "w-retry")
    assert run.attempt == 1
    _override(_admin)
    r = c.post(f"/api/wiki-compile/runs/{run.id}/retry")
    assert r.status_code == 200
    body = r.json()
    db.refresh(run)
    assert run.status == "queued"
    assert run.attempt == 1  # retry 不加 attempt（下次 claim +1）
    assert body["run_id"] == run.id
    assert body["attempt"] == 1
    # 重新入队后可被执行（claim 原子 +1）
    assert worker.claim_next_run(db) is not None


def test_admin_retry_exhausted_409(client):
    c, db, _admin, _eng = client
    run = _mk_failed_run(db, "w-exhaust")
    db.query(CompileRun).filter(CompileRun.id == run.id).update({
        "status": "failed", "attempt": 3, "max_attempts": 3,
    })
    db.commit()
    _override(_admin)
    r = c.post(f"/api/wiki-compile/runs/{run.id}/retry")
    assert r.status_code == 409
    db.refresh(run)
    assert run.status == "failed"


def test_admin_retry_superseded_409(client):
    c, db, _admin, _eng = client
    r1 = executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="slot-1",
    )
    db.commit()
    executor.create_run(
        db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild",
        trigger_object_id="slot-1", supersede_same_trigger=True,
    )
    db.commit()
    db.refresh(r1)
    assert r1.status == "superseded"
    _override(_admin)
    r = c.post(f"/api/wiki-compile/runs/{r1.id}/retry")
    assert r.status_code == 409
    assert "抢占" in r.json()["detail"]


def test_admin_retry_succeeded_409(client):
    c, db, _admin, _eng = client
    run = _mk_succeeded_run(db)
    _override(_admin)
    r = c.post(f"/api/wiki-compile/runs/{run.id}/retry")
    assert r.status_code == 409


def test_admin_cancel_queued(client):
    c, db, _admin, _eng = client
    _page(db, "cq")
    run = executor.create_run(db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild", wiki_page_id="cq")
    db.commit()
    _override(_admin)
    r = c.post(f"/api/wiki-compile/runs/{run.id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    db.refresh(run)
    assert run.status == "cancelled"
    assert run.finished_at is not None


def test_admin_cancel_running_sets_cancel_requested(client):
    c, db, _admin, _eng = client
    _page(db, "cr")
    run = executor.create_run(db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild", wiki_page_id="cr")
    db.commit()
    worker.claim_next_run(db)
    assert run.status == "running"
    _override(_admin)
    r = c.post(f"/api/wiki-compile/runs/{run.id}/cancel")
    assert r.status_code == 200
    assert r.json()["cancel_requested"] is True
    db.refresh(run)
    assert run.cancel_requested is True and run.status == "running"
    executor.execute_run(db, run.id)
    db.refresh(run)
    assert run.status == "cancelled"


def test_admin_cancel_terminal_409(client):
    c, db, _admin, _eng = client
    run = _mk_succeeded_run(db)
    _override(_admin)
    assert c.post(f"/api/wiki-compile/runs/{run.id}/cancel").status_code == 409


def test_admin_cancel_missing_404(client):
    c, db, _admin, _eng = client
    _override(_admin)
    assert c.post("/api/wiki-compile/runs/not-exist/cancel").status_code == 404


# ---------------------------------------------------------------------------
# 列表过滤 / 分页
# ---------------------------------------------------------------------------

def test_list_status_filter_and_pagination(client):
    c, db, _admin, _eng = client
    _mk_succeeded_run(db)
    _page(db, "q")
    queued = executor.create_run(db, pipeline_key="fake.wiki.compile.v1", trigger_type="manual_rebuild", wiki_page_id="q")
    db.commit()
    _override(_admin)
    r = c.get("/api/wiki-compile/runs", params={"status": "queued"})
    assert r.json()["total"] == 1
    assert r.json()["runs"][0]["id"] == queued.id
    r2 = c.get("/api/wiki-compile/runs", params={"limit": 1, "offset": 0})
    assert len(r2.json()["runs"]) == 1
    assert r2.json()["total"] == 2
