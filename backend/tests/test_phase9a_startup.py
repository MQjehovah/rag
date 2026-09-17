# -*- coding: utf-8 -*-
"""Phase 9A 最小闭合：生产启动闭环验证（基线 39be5e0）。

目标：去掉测试环境对「生产 worker 启动缺陷」的绕过（backend/phase9a/server.py
不再提前 start_worker），并证明修复后 app.main 的真实 startup 能闭环消费。

覆盖：
1. logging 回归 + 启动顺序：直接调用 main._start_scheduler 覆盖「正常初始化成功路径」
   ——顺序必须是 bootstrap 就绪 → run_startup_recovery → start_worker（修复前此处
   UnboundLocalError 被自身 except 吞掉，start_worker 永不执行）；
2. 正常启动闭环：真实 lifespan（with TestClient）启动后，通过 HTTP 创建 queued Run，
   不手动 execute_run、不手动启动 worker，最终由真实 pump 消费到 succeeded；
   断言实际 Stage/Artifact/Revision，而不是仅检查线程存在；
3. schema 未就绪：startup 保持原有失败语义（SchemaNotReadyError 传播），worker 不启动；
4. 关闭：退出 lifespan / _shutdown_workers 后本任务 worker 线程正常退出。

隔离：每个测试全新临时 SQLite + 真实 alembic（head 或 P43）；conftest autouse 已隔离
外部模型；LLM/graph 由本文件注入确定性替身。不写真实 notes.db、不改正式 .env。

门禁：cd backend; python -m pytest tests/test_phase9a_startup.py \
      tests/test_phase9a_integration.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.api import deps  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.wiki_pipeline import executor  # noqa: E402
from app.core.wiki_pipeline import registry as pregs  # noqa: E402
from app.core.wiki_pipeline import worker as wiki_worker  # noqa: E402
from app.core.wiki_skills import registry as sreg  # noqa: E402
from app.core.wiki_workspace.routing import ensure_notebook_workspace  # noqa: E402
from app.main import _shutdown_workers, _start_scheduler, app  # noqa: E402
from app.models.database import (  # noqa: E402
    KnowledgeCompileArtifact as CompileArtifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    Page,
    User,
    UserGroup,
    WikiPage,
    WikiRevision,
    WikiSection,
    SchemaNotReadyError,
    init_db,
)
from phase9a import bootstrap_db as bdb  # noqa: E402
from phase9b_migration.migrate import ALEMBIC_HEAD_EXPECTED

_HEAD_REVISION = ALEMBIC_HEAD_EXPECTED
_P43_REVISION = "d3e4f5a6b7c8"  # 历史库停在 P43 → 缺托管表 wiki_section_evidence_bindings
_ADMIN_USER = "u9a-admin"
_ADMIN_PASS = "Phase9a!2026"
_WS_GROUP = "eng"
_TIMEOUT = 60.0

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


def _upgrade_to(db_path: Path, revision: str) -> None:
    url = _sqlite_url(db_path)
    envmap = dict(os.environ)
    envmap["DATABASE_URL"] = url
    envmap["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url={url}", "upgrade", revision],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(_BACKEND), env=envmap, timeout=300,
    )
    assert proc.returncode == 0, f"alembic upgrade {revision} failed: {proc.stderr}"


def _llm_ok(messages, context: str = "", timeout: float = 120.0):
    """确定性 default 编译 stub：manual_rebuild 只触发 wiki-synthesis。"""
    if context == "wiki-synthesis":
        return {"summary": "Phase9A 启动闭环摘要", "content": "Phase9A 启动闭环正文。" + "x" * 60}
    return {"worthy": True, "ops": []}


def _graph_noop(**kw):
    return None


@pytest.fixture(autouse=True)
def _isolate_phase9a():
    """清 registry / runner / worker 线程；失败时也保证不残留后台线程。"""
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    executor.reset_external_runners()
    if wiki_worker.worker_running():
        wiki_worker.stop_worker()
    app.dependency_overrides.clear()
    yield
    executor.reset_external_runners()
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    try:
        _shutdown_workers()
    finally:
        if wiki_worker.worker_running():
            wiki_worker.stop_worker()
    app.dependency_overrides.clear()


class StartupEnv:
    """单个启动测试隔离环境：真实 alembic DB + 进程配置。"""

    def __init__(self, tmp_path: Path, monkeypatch, revision: str = _HEAD_REVISION) -> None:
        self.tmp = Path(tmp_path)
        self.db_path = self.tmp / "phase9a.db"
        self.db_url = _sqlite_url(self.db_path)
        self._monkeypatch = monkeypatch

        # 真实 alembic 到指定 revision（head 或历史 P43）。
        heads = [h for h in bdb.alembic_single_head(bdb._BACKEND, {
            **os.environ, "DATABASE_URL": self.db_url, "PYTHONIOENCODING": "utf-8"
        }).split("|") if h]
        assert heads == [_HEAD_REVISION], heads
        _upgrade_to(self.db_path, revision)

        self.engine = create_engine(self.db_url, connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.execute("PRAGMA busy_timeout=8000")

        init_db(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # 进程配置：全部读 settings；关掉会启动额外后台线程的调度器。
        monkeypatch.setattr(settings, "database_url", self.db_url)
        monkeypatch.setattr(settings, "wiki_pipeline_active_version", "3")
        monkeypatch.setattr(settings, "wiki_topic_enabled", True)
        monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
        monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
        monkeypatch.setattr(settings, "auto_daily_scan_enabled", False)
        monkeypatch.setattr(settings, "auto_organize_enabled", False)
        monkeypatch.setattr(deps, "_engine", self.engine)

    def session(self):
        return self.Session()

    def close(self) -> None:
        self.engine.dispose()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    e = StartupEnv(tmp_path, monkeypatch)
    yield e
    e.close()


def _seed_local_admin(db) -> str:
    uid = _ADMIN_USER
    db.add(User(
        id=uid,
        username=_ADMIN_USER,
        is_local=True,
        password_hash=_pwd.hash(_ADMIN_PASS),
        is_active=True,
    ))
    db.add(UserGroup(id=f"{uid}-g", user_id=uid, group_name="__local_admin__"))
    db.flush()
    return uid


def _seed_closed_loop_wiki(db) -> dict:
    """一个 default 锁定的 wiki（dirty=False）：启动期不触发 recovery，测试内再置 dirty。"""
    _seed_local_admin(db)
    nb = Notebook(id="nb-9a", name="启动闭环库", group_id=_WS_GROUP)
    db.add(nb)
    db.flush()
    ws = ensure_notebook_workspace(db, nb)
    page = Page(
        id="p-9a",
        notebook_id=nb.id,
        title="启动闭环来源页",
        content="Phase9A 启动闭环说明。系统负责编译与发布。" + "（无版本号）" * 3,
        content_hash=hashlib.sha256(b"startup").hexdigest(),
        wiki_dirty=False,
    )
    db.add(page)
    db.flush()
    wiki = WikiPage(
        id="w-9a",
        title="Phase9A 启动闭环主题",
        summary="",
        acl_scope=ws.acl_scope,
        category="资料",
        status="draft",
        source_page_ids=json.dumps(["p-9a"], ensure_ascii=False),
        dirty=False,
        workspace_id=ws.id,
        content_skill="default",
        skill_version="1",
        skill_locked=True,
    )
    db.add(wiki)
    db.flush()
    return {"workspace_id": ws.id, "notebook_id": nb.id, "page_id": page.id, "wiki_id": wiki.id}


def _wait_run_terminal(db, wiki_id: str, want: str, timeout: float = _TIMEOUT) -> CompileRun:
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        db.expire_all()
        run = (db.query(CompileRun)
               .filter(CompileRun.wiki_page_id == wiki_id)
               .order_by(CompileRun.created_at.desc(), CompileRun.id.desc())
               .first())
        if run is not None and run.status == want:
            return run
        last = run.status if run is not None else None
        time.sleep(0.3)
    raise AssertionError(f"run for {wiki_id} 未到 {want} (last={last})")


# ---------------------------------------------------------------------------
# 1/4. logging 回归 + 启动顺序（直接覆盖正常初始化成功路径）
# ---------------------------------------------------------------------------


def test_startup_normal_path_starts_worker_in_order(env: StartupEnv, monkeypatch):
    """修复前：_start_scheduler 正常路径在 start_worker 前 UnboundLocalError 被吞，
    start_worker 永不执行。修复后：顺序 run_startup_recovery → start_worker。"""
    order: list[str] = []
    real_recovery = wiki_worker.run_startup_recovery
    real_start = wiki_worker.start_worker

    def _recovery(engine=None):
        # 此时 bootstrap 已完成（skill + pipeline active 就绪）且 schema 已 init。
        assert pregs.get_active_version("wiki.default") == "3"
        assert sreg.has("default", "1")
        order.append("recovery")
        return real_recovery(engine)

    def _start(*a, **kw):
        assert pregs.get_active_version("wiki.default") == "3", "bootstrap 必须先于 worker"
        assert order == ["recovery"], "recovery 必须先于 start_worker"
        order.append("start_worker")
        return real_start(*a, **kw)

    monkeypatch.setattr(wiki_worker, "run_startup_recovery", _recovery)
    monkeypatch.setattr(wiki_worker, "start_worker", _start)

    # 正常初始化成功路径：不抛异常，worker 真实启动。
    _start_scheduler()
    assert order == ["recovery", "start_worker"]
    assert wiki_worker.worker_running() is True

    # 关闭：本任务 worker 正常退出。
    _shutdown_workers()
    assert wiki_worker.worker_running() is False


# ---------------------------------------------------------------------------
# 3/4. schema 未就绪：startup 保持失败语义，worker 不启动
# ---------------------------------------------------------------------------


def test_startup_schema_not_ready_aborts(env: StartupEnv, monkeypatch):
    """停在 P43 的库缺 P44 托管表 → init_db 抛 SchemaNotReadyError，startup 中止，
    不静默继续、worker 不启动。"""
    from sqlalchemy import text as sa_text

    # 重建 P43 库（Env 已建 head 库，这里另建一个落后库）。
    old_path = env.tmp / "p43.db"
    _upgrade_to(old_path, _P43_REVISION)
    monkeypatch.setattr(settings, "database_url", _sqlite_url(old_path))

    # 校验前置：该库确实缺 P44 表。
    old_engine = create_engine(_sqlite_url(old_path), connect_args={"check_same_thread": False})
    insp_conn = old_engine.connect()
    has = insp_conn.execute(sa_text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_section_evidence_bindings'"
    )).fetchone()
    insp_conn.close()
    old_engine.dispose()
    assert has is None, "前置：P43 库不应有 P44 表"

    with pytest.raises(SchemaNotReadyError):
        _start_scheduler()
    assert wiki_worker.worker_running() is False, "schema 未就绪时不得启动 worker"


# ---------------------------------------------------------------------------
# 2/4 + 4/4. 真实 startup 闭环：HTTP 建 queued run → 真实 pump 自动消费；
#          退出 lifespan 后 worker 正常退出
# ---------------------------------------------------------------------------


def test_startup_closed_loop_http_run_consumed_by_real_worker(env: StartupEnv):
    """真实 lifespan（with TestClient）启动后，worker 泵自动消费 HTTP 创建的 queued run，
    不手动 execute_run、不手动 start_worker。断言实际 Stage/Artifact/Revision。"""
    db = env.session()
    ids = _seed_closed_loop_wiki(db)
    db.commit()

    executor.configure_external_runners(llm_runner=_llm_ok, graph_runner=_graph_noop)

    with TestClient(app) as client:
        assert wiki_worker.worker_running() is True, "真实 startup 必须已启动 worker"

        # admin 登录（本地用户走生产 bcrypt + Bearer）。
        r = client.post("/api/auth/login", json={
            "username": _ADMIN_USER, "password": _ADMIN_PASS})
        assert r.status_code == 200, r.text
        admin = {"Authorization": f"Bearer {r.json()['token']}"}

        # 启动完成后置 dirty，再经 HTTP 正式入口创建 queued run（startup recovery
        # 在 dirty=False 时不产生 run，确保该 run 确为 HTTP 触发）。
        w = db.get(WikiPage, ids["wiki_id"])
        w.dirty = True
        db.commit()

        resp = client.post("/api/wiki/refresh-page-dirty", headers=admin)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["submitted"] >= 1, body
        assert body["skipped"] is False, body

        # 不手动执行：仅轮询 DB，pump 线程自主消费。
        run = _wait_run_terminal(db, ids["wiki_id"], "succeeded")
        assert run.status == "succeeded", (run.safe_error_code, run.safe_error_message)
        assert run.attempt >= 1

        # 实际 Stage 落库：v3 全链 8 个阶段都在（含 schedule_graph）。
        stages = (db.query(StageRun)
                  .filter(StageRun.run_id == run.id)
                  .order_by(StageRun.stage_order, StageRun.id).all())
        keys = {s.stage_key for s in stages}
        assert keys == {
            "resolve_context", "topic_route", "skill_route", "synthesize_by_skill",
            "validate_by_skill", "publish_by_skill", "finalize_compile_outcome",
            "schedule_graph",
        }, sorted(keys)
        assert all(s.status in ("succeeded", "skipped") for s in stages), [
            (s.stage_key, s.status) for s in stages]

        # 实际 Manifest Artifact 落库。
        manifest = (db.query(CompileArtifact)
                    .filter(CompileArtifact.run_id == run.id,
                            CompileArtifact.artifact_type == "wiki_publish_manifest")
                    .first())
        assert manifest is not None and manifest.payload_json, "缺少 publish manifest"

        # 实际 Revision/Section 发布落库。
        db.expire_all()
        w2 = db.get(WikiPage, ids["wiki_id"])
        assert w2.dirty is False, "发布成功后 wiki 必须清除 dirty"
        assert w2.current_revision_id is not None, "必须回填 current_revision_id"
        assert w2.status == "published"
        rev = db.get(WikiRevision, w2.current_revision_id)
        assert rev is not None and rev.wiki_page_id == ids["wiki_id"]
        secs = (db.query(WikiSection)
                .filter(WikiSection.revision_id == w2.current_revision_id).all())
        assert len(secs) >= 2 and {s.section_type for s in secs} >= {"summary", "facts"}

    # 退出 lifespan：worker/后台进程正常退出。
    assert wiki_worker.worker_running() is False, "关闭后 worker 必须停止"
    alive = [t.name for t in threading.enumerate()
             if t.name and t.name.startswith("wiki-compile")]
    assert alive == [], f"残留 worker 线程: {alive}"
    db.close()
