"""Phase 5.1：调度接线 + kill switch（wiki_pipeline_default_enabled）测试（W2-A）。

覆盖：
- kill switch OFF（settings False，DB 无 override 行）→ schedule_page_refresh /
  schedule_wiki_rebuild 不建 run、不 submit 旧 worker、返回未调度，Page/Wiki 保持 dirty。
- kill switch ON → schedule_page_refresh 建 wiki.default queued run（page_changed），
  同 input 幂等（不产生两个 active 同 trigger run）；内容变化 → supersede 旧 run。
- kill switch ON → schedule_wiki_rebuild 建 manual_rebuild run。
- kill switch ON → recover_dirty_pages 把 dirty Page 转 page_changed run（不走旧 backlog）。
- kill switch ON 但无法解析 workspace → enqueue 失败：不建 run、不 submit 旧 worker、
  返回 False（不再回退旧线程池）。

全部使用独立临时 SQLite 文件库（monkeypatch settings.database_url），不触碰真实库；
调度器模块级状态（含 kill switch 缓存）在每个测试前后 shutdown 清理。
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
    Page,
    WikiPage,
    WikiWorkspace,
    init_db,
)

_DB_FILE_NAME = "phase5-wiring.db"


@pytest.fixture(autouse=True)
def _phase5_wiring_isolation():
    """清理 scheduler 模块级状态 + registry + 外部 runner（防跨测试泄漏/串库）。"""
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    sch.shutdown()
    sch._semaphore = None
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    sch.shutdown()
    sch._semaphore = None
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


def _mk_page(db, page_id="p1", notebook_id="nb-1"):
    _mk_notebook(db, notebook_id)
    if db.get(Page, page_id) is None:
        db.add(Page(
            id=page_id, notebook_id=notebook_id,
            title="水箱维护", content="水箱维护需要定期检查冷却水温度与压力表读数并记录运行日志。",
        ))
    db.commit()


def _mk_wiki(db, wiki_id="w1", workspace_id="ws1", dirty=True):
    if db.get(WikiWorkspace, workspace_id) is None:
        db.add(WikiWorkspace(
            id=workspace_id, key=f"key-{workspace_id}", name=workspace_id,
            acl_scope='{"groups": ["engineering"]}', scope_id="group:engineering",
            status="active",
        ))
    if db.get(WikiPage, wiki_id) is None:
        db.add(WikiPage(
            id=wiki_id, title="水箱维护", workspace_id=workspace_id,
            dirty=dirty, status="draft" if dirty else "published",
        ))
    db.commit()


def _page_changed_runs(db, page_id="p1", statuses=None):
    q = db.query(CompileRun).filter(
        CompileRun.pipeline_key == PIPELINE_KEY,
        CompileRun.trigger_type == "page_changed",
        CompileRun.trigger_object_id == page_id,
    )
    if statuses is not None:
        q = q.filter(CompileRun.status.in_(statuses))
    return q.all()


# ---------------------------------------------------------------------------
# kill switch OFF（settings False，DB 无 override）：不建 run、不 submit 旧 worker
# ---------------------------------------------------------------------------


def test_kill_off_schedule_page_refresh_no_run_no_worker(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", False)
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    called = threading.Event()

    def _fake_worker(page_id):
        called.set()

    monkeypatch.setattr(sch, "_run_page_refresh", _fake_worker)
    sch.shutdown()
    sch._semaphore = None

    try:
        ok = sch.schedule_page_refresh("p1", changed=True)
        assert ok is False, "kill switch off → 未调度"
        assert not called.is_set(), "kill switch off 绝不 submit 旧 worker"
        db.expire_all()
        assert _page_changed_runs(db) == [], "kill switch off 时不得建 wiki.default run"
        assert db.get(Page, "p1").wiki_dirty is True, "Page 保持 dirty"
    finally:
        db.close()
        engine.dispose()


def test_kill_off_schedule_wiki_rebuild_no_run_no_worker(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", False)
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_wiki(db, "w1", dirty=True)

    called = threading.Event()

    def _fake_worker(wiki_id):
        called.set()

    monkeypatch.setattr(sch, "_run_wiki_rebuild", _fake_worker)
    sch.shutdown()
    sch._semaphore = None

    try:
        ok = sch.schedule_wiki_rebuild("w1")
        assert ok is False, "kill switch off → 未调度"
        assert not called.is_set(), "kill switch off 绝不 submit 旧 worker"
        db.expire_all()
        runs = db.query(CompileRun).filter(
            CompileRun.pipeline_key == PIPELINE_KEY,
            CompileRun.trigger_type == "manual_rebuild",
        ).all()
        assert runs == [], "kill switch off 时不得建 wiki.default run"
        assert db.get(WikiPage, "w1").dirty is True, "Wiki 保持 dirty"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# flag True：schedule_page_refresh → wiki.default queued run
# ---------------------------------------------------------------------------


def test_flag_on_schedule_page_refresh_creates_run(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", True)
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        ok = sch.schedule_page_refresh("p1", changed=True)
        assert ok is True
        db.expire_all()
        runs = _page_changed_runs(db)
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "queued"
        assert run.workspace_id, "run 必须归属 active workspace"
        assert run.pipeline_version == "1"
        assert run.idempotency_key.startswith("wiki.default:v1:"), \
            "Phase 5.2：page_changed 幂等键改为统一 helper（wiki.default:v1:<digest>）"
    finally:
        db.close()
        engine.dispose()


def test_flag_on_same_input_is_idempotent_single_active_run(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", True)
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        assert sch.schedule_page_refresh("p1", changed=True) is True
        assert sch.schedule_page_refresh("p1", changed=True) is True
        db.expire_all()
        active = _page_changed_runs(db, statuses=("queued", "running"))
        assert len(active) == 1, "同 input 二次调度不得产生两个 active run"
    finally:
        db.close()
        engine.dispose()


def test_flag_on_content_change_supersedes_old_run(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", True)
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        assert sch.schedule_page_refresh("p1", changed=True) is True
        # 内容变化 → 新 input_hash → 新 idempotency_key → 新 run，并 supersede 旧 queued run。
        page = db.get(Page, "p1")
        page.content = "水箱维护策略变更：新增每日三次巡检与压力释放阀年检记录要求。"
        db.commit()
        assert sch.schedule_page_refresh("p1", changed=True) is True
        db.expire_all()
        queued = _page_changed_runs(db, statuses=("queued",))
        superseded = _page_changed_runs(db, statuses=("superseded",))
        assert len(queued) == 1, "内容变化后应有且仅有一个 queued run"
        assert len(superseded) == 1, "旧 queued run 应被 supersede"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# flag True：schedule_wiki_rebuild → manual_rebuild run
# ---------------------------------------------------------------------------


def test_flag_on_schedule_wiki_rebuild_creates_run(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", True)
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_wiki(db, "w1", dirty=True)

    try:
        ok = sch.schedule_wiki_rebuild("w1")
        assert ok is True
        db.expire_all()
        runs = db.query(CompileRun).filter(
            CompileRun.pipeline_key == PIPELINE_KEY,
            CompileRun.trigger_type == "manual_rebuild",
            CompileRun.trigger_object_id == "w1",
        ).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "queued"
        assert run.wiki_page_id == "w1"
        assert run.workspace_id == "ws1"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# flag True：recover_dirty_pages → dirty Page 转 page_changed run
# ---------------------------------------------------------------------------


def test_flag_on_recover_dirty_page_creates_run(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", True)
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_notebook(db, "nb-1")
    db.add(Page(
        id="dirty-1", notebook_id="nb-1", title="脏页",
        content="脏页内容足够长用于 Wiki 编译恢复，包含有效正文信息。",
        wiki_dirty=True,
    ))
    db.commit()

    try:
        result = sch.recover_dirty_pages()
        assert result["submitted"] >= 1
        db.expire_all()
        runs = _page_changed_runs(db, page_id="dirty-1")
        assert len(runs) == 1
        assert runs[0].status == "queued"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# kill switch ON：无法解析 workspace → enqueue 失败：不建 run、不 submit 旧 worker
# ---------------------------------------------------------------------------


def test_flag_on_page_without_workspace_not_enqueued(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", True)
    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    # Page 无 notebook 归属 → workspace 不可解析 → enqueue 失败（不建 run）。
    if db.get(Page, "p-noscope") is None:
        db.add(Page(id="p-noscope", notebook_id=None, title="x", content="内容足够长以触发调度"))
        db.commit()

    # 不再回退旧 worker：monkeypatch 旧 worker 断言不会被调用。
    called = threading.Event()

    def _fake_worker(page_id):
        called.set()

    monkeypatch.setattr(sch, "_run_page_refresh", _fake_worker)
    sch.shutdown()
    sch._semaphore = None

    try:
        ok = sch.schedule_page_refresh("p-noscope", changed=True)
        assert ok is False, "无法解析 workspace → 未调度（不再回退旧 worker）"
        assert not called.is_set(), "enqueue 失败绝不回退 submit 旧 worker"
        db.expire_all()
        assert _page_changed_runs(db, page_id="p-noscope") == []
        assert db.get(Page, "p-noscope").wiki_dirty is True, "Page 保持 dirty"
    finally:
        db.close()
        engine.dispose()
