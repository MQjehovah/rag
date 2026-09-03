"""Phase 5.1：scheduler 单轨化 + kill switch（wiki_pipeline_default_enabled）测试（W2-A）。

覆盖（对应契约目标语义）：
1. settings 默认 True（DB 无 override 行）→ schedule_page_refresh 建 page_changed
   queued run。
2. DB RuntimeFeatureFlag 行 enabled=False（kill）→ schedule_page_refresh 不建 run、
   返回 False/未调度、Page 保持 dirty；不提交旧 worker（monkeypatch _run_page_refresh）。
3. enqueue 失败（monkeypatch executor.create_run 抛异常）→ 返回 False、Page dirty
   保持、无残留 run、不调旧 worker。
4. schedule_page_deleted（Page 行在、有 active binding）→ 建 page_deleted run。
5. recover_dirty_pages kill on → dirty Page/Wiki 逐个转 queued run。
6. DB-first：settings False + DB row True → 启用建 run；settings True + DB row False →
   kill 不建。验证 TTL 缓存经 clear_kill_switch_cache 失效。

全部使用独立临时 SQLite 文件库（monkeypatch settings.database_url），不触碰真实库。
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core.wiki_pipeline import executor, registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    PIPELINE_KEY,
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.models.database import (
    KnowledgeCompileRun as CompileRun,
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    RuntimeFeatureFlag,
    WikiPage,
    WikiWorkspace,
    init_db,
)

_DB_FILE_NAME = "phase51-scheduler.db"


@pytest.fixture(autouse=True)
def _phase51_isolation():
    """清理 scheduler 模块级状态（含 kill switch 缓存）+ registry + runner。"""
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    sch.shutdown()
    sch._semaphore = None
    sch.clear_kill_switch_cache()
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    sch.shutdown()
    sch._semaphore = None
    sch.clear_kill_switch_cache()
    registry.REGISTRY.clear()
    executor.reset_external_runners()


@pytest.fixture()
def wiki_default():
    register_default_pipeline()
    yield PIPELINE_KEY
    unregister_default_pipeline()


def _setup_db(tmp_path, monkeypatch):
    """monkeypatch settings.database_url → 独立临时文件库，返回 (url, engine, db)。"""
    url = f"sqlite:///{(tmp_path / _DB_FILE_NAME).as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    init_db(engine)
    db = sessionmaker(bind=engine)()
    return url, engine, db


def _mk_notebook(db, notebook_id="nb-1", group_id="engineering"):
    if db.get(Notebook, notebook_id) is None:
        db.add(Notebook(id=notebook_id, name="研发库", group_id=group_id))
        db.flush()
    return db.get(Notebook, notebook_id)


def _mk_workspace(db, workspace_id="ws1", status="active"):
    if db.get(WikiWorkspace, workspace_id) is None:
        db.add(WikiWorkspace(
            id=workspace_id, key=f"key-{workspace_id}", name=workspace_id,
            acl_scope='{"groups": ["engineering"]}', scope_id="group:engineering",
            status=status,
        ))
        db.flush()
    return db.get(WikiWorkspace, workspace_id)


def _bind_workspace(db, notebook_id="nb-1", workspace_id="ws1"):
    binding = (
        db.query(NotebookWorkspaceBinding)
        .filter(
            NotebookWorkspaceBinding.notebook_id == notebook_id,
            NotebookWorkspaceBinding.status == "active",
        )
        .first()
    )
    if binding is None:
        db.add(NotebookWorkspaceBinding(
            notebook_id=notebook_id, workspace_id=workspace_id, status="active",
        ))
        db.flush()


def _mk_page(db, page_id="p1", notebook_id="nb-1", *, dirty=True):
    _mk_notebook(db, notebook_id)
    _mk_workspace(db)
    _bind_workspace(db, notebook_id)
    if db.get(Page, page_id) is None:
        db.add(Page(
            id=page_id, notebook_id=notebook_id,
            title="水箱维护", content="水箱维护需要定期检查冷却水温度与压力表读数并记录运行日志。",
            wiki_dirty=dirty,
        ))
    db.commit()


def _mk_wiki(db, wiki_id="w1", workspace_id="ws1", *, dirty=True):
    _mk_workspace(db, workspace_id)
    if db.get(WikiPage, wiki_id) is None:
        db.add(WikiPage(
            id=wiki_id, title="水箱维护", workspace_id=workspace_id,
            dirty=dirty, status="draft" if dirty else "published",
        ))
    db.commit()


def _set_db_flag(db, enabled: bool) -> None:
    row = db.query(RuntimeFeatureFlag).filter(
        RuntimeFeatureFlag.name == "wiki_pipeline_default_enabled"
    ).first()
    if row is None:
        db.add(RuntimeFeatureFlag(name="wiki_pipeline_default_enabled", enabled=enabled))
    else:
        row.enabled = enabled
    db.commit()


def _runs(db, trigger_type=None, trigger_object_id=None):
    q = db.query(CompileRun).filter(CompileRun.pipeline_key == PIPELINE_KEY)
    if trigger_type is not None:
        q = q.filter(CompileRun.trigger_type == trigger_type)
    if trigger_object_id is not None:
        q = q.filter(CompileRun.trigger_object_id == trigger_object_id)
    return q.all()


# ---------------------------------------------------------------------------
# 1. settings 默认 True（无 DB override 行）→ page_changed run
# ---------------------------------------------------------------------------


def test_default_enabled_creates_page_changed_run(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    assert settings.wiki_pipeline_default_enabled is True, "settings 默认必须 True"
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        ok = sch.schedule_page_refresh("p1", changed=True)
        assert ok is True
        db.expire_all()
        runs = _runs(db, trigger_type="page_changed", trigger_object_id="p1")
        assert len(runs) == 1
        assert runs[0].status == "queued"
        assert runs[0].workspace_id == "ws1"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. DB flag off（kill）→ 不建 run、未调度、Page 保持 dirty、不 submit 旧 worker
# ---------------------------------------------------------------------------


def test_db_flag_off_does_not_enqueue(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")
    _set_db_flag(db, False)
    sch.clear_kill_switch_cache()

    called = threading.Event()

    def _fake_worker(page_id):
        called.set()

    monkeypatch.setattr(sch, "_run_page_refresh", _fake_worker)

    try:
        ok = sch.schedule_page_refresh("p1", changed=True)
        assert ok is False, "kill → 未调度"
        assert not called.is_set(), "kill 绝不 submit 旧 worker"
        db.expire_all()
        assert _runs(db, trigger_type="page_changed") == [], "kill 不建 run"
        assert db.get(Page, "p1").wiki_dirty is True, "Page 保持 dirty"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. enqueue 失败（create_run 抛异常）→ False、dirty 保持、无残留 run、不调旧 worker
# ---------------------------------------------------------------------------


def test_enqueue_failure_keeps_dirty_no_legacy_worker(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    real_create_run = executor.create_run

    def _boom(*args, **kwargs):
        raise RuntimeError("create_run exploded")

    monkeypatch.setattr(executor, "create_run", _boom)

    called = threading.Event()

    def _fake_worker(page_id):
        called.set()

    monkeypatch.setattr(sch, "_run_page_refresh", _fake_worker)

    try:
        ok = sch.schedule_page_refresh("p1", changed=True)
        assert ok is False, "enqueue 失败 → False"
        assert not called.is_set(), "enqueue 失败不回退旧 worker"
        db.expire_all()
        assert _runs(db, trigger_type="page_changed") == [], "rollback 无残留 run"
        assert db.get(Page, "p1").wiki_dirty is True, "Page 保持 dirty"
    finally:
        monkeypatch.setattr(executor, "create_run", real_create_run)
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 4. schedule_page_deleted：Page 行在、有 active binding → page_deleted run
# ---------------------------------------------------------------------------


def test_schedule_page_deleted_creates_run(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        sch.schedule_page_deleted("p1")
        db.expire_all()
        runs = _runs(db, trigger_type="page_deleted", trigger_object_id="p1")
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "queued"
        assert run.workspace_id == "ws1"
        assert run.input_hash, "删除事件输入指纹非空"
        assert run.idempotency_key.startswith("wiki.default:v1:")
    finally:
        db.close()
        engine.dispose()


def test_schedule_page_deleted_kill_off_defers(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")
    _set_db_flag(db, False)
    sch.clear_kill_switch_cache()

    try:
        sch.schedule_page_deleted("p1")
        db.expire_all()
        assert _runs(db, trigger_type="page_deleted") == [], "kill 不建删除 run"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 5. recover_dirty_pages kill on → dirty Page/Wiki 逐个转 queued run
# ---------------------------------------------------------------------------


def test_recover_dirty_pages_kill_on_creates_runs(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "dirty-1")
    _mk_wiki(db, "w1", "ws1", dirty=True)

    try:
        result = sch.recover_dirty_pages()
        assert result["submitted"] >= 1
        assert result["wiki_loaded"] >= 1
        db.expire_all()
        page_runs = _runs(db, trigger_type="page_changed", trigger_object_id="dirty-1")
        assert len(page_runs) == 1 and page_runs[0].status == "queued"
        wiki_runs = _runs(db, trigger_type="manual_rebuild", trigger_object_id="w1")
        assert len(wiki_runs) == 1 and wiki_runs[0].status == "queued"
    finally:
        db.close()
        engine.dispose()


def test_recover_dirty_pages_kill_off_not_scheduled(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "dirty-1", dirty=True)
    _set_db_flag(db, False)
    sch.clear_kill_switch_cache()

    try:
        result = sch.recover_dirty_pages()
        assert result == {"submitted": 0, "rejected": 0, "wiki_loaded": 0}
        db.expire_all()
        assert _runs(db) == [], "kill 不调度"
        assert db.get(Page, "dirty-1").wiki_dirty is True
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 6. DB-first：DB row 覆盖 settings；TTL 缓存失效
# ---------------------------------------------------------------------------


def test_db_first_flag_override(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    # settings False + DB row True → DB 覆盖启用 → 建 run。
    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", False)
    _set_db_flag(db, True)
    sch.clear_kill_switch_cache()
    ok = sch.schedule_page_refresh("p1", changed=True)
    db.expire_all()
    assert ok is True
    assert len(_runs(db, trigger_type="page_changed", trigger_object_id="p1")) == 1

    # settings True + DB row False → kill → 不建新 run。
    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", True)
    _set_db_flag(db, False)
    sch.clear_kill_switch_cache()
    ok2 = sch.schedule_page_refresh("p1", changed=True)
    db.expire_all()
    assert ok2 is False
    assert len(_runs(db, trigger_type="page_changed", trigger_object_id="p1")) == 1, \
        "kill 后不得再建 run"

    # TTL 缓存失效：kill 状态在缓存窗口内生效后，翻转 DB 并清缓存 → 恢复启用。
    _set_db_flag(db, True)
    sch.clear_kill_switch_cache()
    ok3 = sch.schedule_page_refresh("p1", changed=True)
    db.expire_all()
    assert ok3 is True
    assert db.get(Page, "p1").wiki_dirty is True, "幂等命中既有 run 时 Page 仍等编译消费"
    db.close()
    engine.dispose()
