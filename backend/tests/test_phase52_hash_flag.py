"""Phase 5.2（A）：scheduler 完整 input hash + 统一幂等键 + kill switch cache 失效
+ 注册幂等/中止语义测试。

覆盖（对应契约六 / 十二 目标）：
1. page_changed Run.input_hash == 完整 64hex 内容 hash（= _page_refresh_input_hash），
   数据库断言（knowledge_compile_runs.input_hash）。
2. page_changed idempotency_key == B1 make_default_idempotency_key 计算值（非截断）。
3. manual_rebuild / page_deleted 同构断言（input_hash 完整 64 + key 用统一 helper）。
4. 同输入重调度 → 返回原 run（DB 单 run）；内容变 → 新 run supersede 旧 queued
   （同 trigger 至多一活跃）。
5. set_feature_flag(False) 后 scheduler 立即 kill（clear cache 生效，无需等 5s TTL）；
   再 toggle True 立即恢复建 run。
6. 重复 register_default_pipeline（同定义）幂等不抛；冲突定义（不同 stage 序列）→ 抛。
7. main 注册错误中止：register 抛真实异常 → startup 直接 raise（不再吞错继续）。

fixture/helper 复用 test_phase51_scheduler.py 模式（独立临时 SQLite 文件库 +
monkeypatch settings.database_url，不触碰真实库）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core.feature_flags import set_feature_flag
from app.core.wiki_pipeline import executor, registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    PIPELINE_KEY,
    make_default_idempotency_key,
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.models.database import (
    KnowledgeCompileRun as CompileRun,
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiWorkspace,
    init_db,
)

_DB_FILE_NAME = "phase52-hash-flag.db"
_HEX64 = set("0123456789abcdef")


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in _HEX64 for c in value)


@pytest.fixture(autouse=True)
def _phase52_isolation(monkeypatch):
    """清理 scheduler 模块级状态（含 kill switch 缓存）+ registry + runner；
    每个测试重置 kill switch settings 默认值（防跨测试泄漏）。"""
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    sch.shutdown()
    sch._semaphore = None
    sch.clear_kill_switch_cache()
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    monkeypatch.setattr(settings, "wiki_pipeline_default_enabled", True)
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


def _mk_page(db, page_id="p1", notebook_id="nb-1", *, dirty=True,
             content="水箱维护需要定期检查冷却水温度与压力表读数并记录运行日志。"):
    _mk_notebook(db, notebook_id)
    _mk_workspace(db)
    _bind_workspace(db, notebook_id)
    if db.get(Page, page_id) is None:
        db.add(Page(
            id=page_id, notebook_id=notebook_id,
            title="水箱维护", content=content,
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


def _runs(db, trigger_type=None, trigger_object_id=None):
    q = db.query(CompileRun).filter(CompileRun.pipeline_key == PIPELINE_KEY)
    if trigger_type is not None:
        q = q.filter(CompileRun.trigger_type == trigger_type)
    if trigger_object_id is not None:
        q = q.filter(CompileRun.trigger_object_id == trigger_object_id)
    return q.all()


def _runs_by_status(db, trigger_type, trigger_object_id, statuses):
    q = db.query(CompileRun).filter(
        CompileRun.pipeline_key == PIPELINE_KEY,
        CompileRun.trigger_type == trigger_type,
        CompileRun.trigger_object_id == trigger_object_id,
        CompileRun.status.in_(statuses),
    )
    return q.all()


# ---------------------------------------------------------------------------
# 1. page_changed Run.input_hash == 完整 64hex 内容 hash（数据库断言）
# ---------------------------------------------------------------------------


def test_page_changed_run_input_hash_is_full_content_hash(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        ok = sch.schedule_page_refresh("p1", changed=True)
        assert ok is True
        db.expire_all()
        runs = _runs(db, trigger_type="page_changed", trigger_object_id="p1")
        assert len(runs) == 1
        run = runs[0]
        expected = sch._page_refresh_input_hash(db, "p1")
        assert _is_hex64(expected), "内容 hash 必须是完整 64hex"
        assert _is_hex64(run.input_hash or ""), "run.input_hash 必须是完整 64hex"
        assert run.input_hash == expected, "run.input_hash 必须等于调度时的完整内容 hash"
        assert run.status == "queued"
        assert run.workspace_id == "ws1"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. page_changed idempotency_key == make_default_idempotency_key（非截断）
# ---------------------------------------------------------------------------


def test_page_changed_idempotency_key_uses_full_hash_helper(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        assert sch.schedule_page_refresh("p1", changed=True) is True
        db.expire_all()
        runs = _runs(db, trigger_type="page_changed", trigger_object_id="p1")
        run = runs[0]
        expected_key = make_default_idempotency_key(
            workspace_id=run.workspace_id,
            trigger_type="page_changed",
            trigger_object_id="p1",
            wiki_page_id="",
            full_input_hash=run.input_hash,
        )
        assert run.idempotency_key == expected_key
        assert run.idempotency_key.startswith("wiki.default:v1:")
        assert len(run.idempotency_key) == len("wiki.default:v1:") + 64
        # 旧截断格式（wiki.default:{ws}:{page}:{hash[:16]}）已废弃。
        assert not run.idempotency_key.startswith(f"wiki.default:{run.workspace_id}:p1:")
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. manual_rebuild / page_deleted 同构断言
# ---------------------------------------------------------------------------


def test_manual_rebuild_input_hash_and_key_shape(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_wiki(db, "w1", "ws1", dirty=True)

    try:
        ok = sch.schedule_wiki_rebuild("w1")
        assert ok is True
        db.expire_all()
        runs = _runs(db, trigger_type="manual_rebuild", trigger_object_id="w1")
        assert len(runs) == 1
        run = runs[0]
        wiki = db.get(WikiPage, "w1")
        expected_hash = sch._wiki_rebuild_input_hash(db, wiki)
        assert _is_hex64(expected_hash)
        assert run.input_hash == expected_hash, "manual_rebuild input_hash 必须完整 64hex"
        assert run.wiki_page_id == "w1"
        assert run.workspace_id == "ws1"
        expected_key = make_default_idempotency_key(
            workspace_id=run.workspace_id,
            trigger_type="manual_rebuild",
            trigger_object_id="",  # manual_rebuild 身份维是 wiki_page_id
            wiki_page_id="w1",
            full_input_hash=run.input_hash,
        )
        assert run.idempotency_key == expected_key
        assert run.idempotency_key.startswith("wiki.default:v1:")
    finally:
        db.close()
        engine.dispose()


def test_page_deleted_input_hash_and_key_shape(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        sch.schedule_page_deleted("p1")
        db.expire_all()
        runs = _runs(db, trigger_type="page_deleted", trigger_object_id="p1")
        assert len(runs) == 1
        run = runs[0]
        assert _is_hex64(run.input_hash or ""), "删除指纹 hash 必须完整 64hex"
        expected_key = make_default_idempotency_key(
            workspace_id=run.workspace_id,
            trigger_type="page_deleted",
            trigger_object_id="p1",
            wiki_page_id="",
            full_input_hash=run.input_hash,
        )
        assert run.idempotency_key == expected_key
        assert run.idempotency_key.startswith("wiki.default:v1:")
        assert len(run.idempotency_key) == len("wiki.default:v1:") + 64
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 4. 同输入幂等（DB 单 run）；内容变 → supersede 旧 queued
# ---------------------------------------------------------------------------


def test_same_input_idempotent_then_content_change_supersedes(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    try:
        assert sch.schedule_page_refresh("p1", changed=True) is True
        db.expire_all()
        first = _runs(db, trigger_type="page_changed", trigger_object_id="p1")
        assert len(first) == 1
        run1 = first[0]

        # 同输入二次调度 → 幂等返回原 run（DB 单 run、同 id）。
        assert sch.schedule_page_refresh("p1", changed=True) is True
        db.expire_all()
        active = _runs_by_status(db, "page_changed", "p1", ("queued", "running"))
        assert len(active) == 1 and active[0].id == run1.id, "同输入不得新建第二个 active run"
        assert len(_runs(db, trigger_type="page_changed", trigger_object_id="p1")) == 1

        # 内容变化 → 新 key 建新 run，旧 queued 被 supersede（同 trigger 至多一活跃）。
        page = db.get(Page, "p1")
        page.content = "水箱维护策略变更：新增每日三次巡检与压力释放阀年检记录要求。"
        db.commit()
        assert sch.schedule_page_refresh("p1", changed=True) is True
        db.expire_all()
        queued = _runs_by_status(db, "page_changed", "p1", ("queued",))
        superseded = _runs_by_status(db, "page_changed", "p1", ("superseded",))
        assert len(queued) == 1 and queued[0].id != run1.id, "内容变 → 新 run"
        assert len(superseded) == 1 and superseded[0].id == run1.id, "旧 queued 被 supersede"
        assert len(_runs(db, trigger_type="page_changed", trigger_object_id="p1")) == 2
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 5. set_feature_flag toggle → kill switch cache 立即失效（无需等 5s TTL）
# ---------------------------------------------------------------------------


def test_set_feature_flag_invalidates_kill_switch_cache_immediately(tmp_path, monkeypatch, wiki_default):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _setup_db(tmp_path, monkeypatch)
    _mk_page(db, "p1")

    # ON 状态建 run（预热 kill 缓存为 ON）。
    set_feature_flag(db, "wiki_pipeline_default_enabled", True, user_id="u1")
    assert sch.schedule_page_refresh("p1", changed=True) is True
    db.expire_all()
    assert len(_runs(db, trigger_type="page_changed", trigger_object_id="p1")) == 1
    assert sch._pipeline_kill_switch_enabled() is True, "kill 缓存应预热为 ON"

    # 内容变化（若误建会得到新 run，便于断言 kill 生效）。
    page = db.get(Page, "p1")
    page.content = "关闭开关后的内容变更，正文足够长以产生不同内容 hash。"
    db.commit()

    # 运行时 toggle OFF：set_feature_flag 写 DB + settings 并清 scheduler 缓存。
    # 关键：此处不手动 clear_kill_switch_cache —— 验证 toggle 即失效。
    set_feature_flag(db, "wiki_pipeline_default_enabled", False, user_id="u1")
    ok_kill = sch.schedule_page_refresh("p1", changed=True)
    assert ok_kill is False, "toggle OFF 后必须立即 kill（不等 5s TTL）"
    db.expire_all()
    assert len(_runs(db, trigger_type="page_changed", trigger_object_id="p1")) == 1, \
        "kill 后不得新建 run"
    assert sch._pipeline_kill_switch_enabled() is False

    # 再 toggle ON：缓存已失效 → 立即恢复建 run（新内容 → 新 run supersede 旧 queued）。
    set_feature_flag(db, "wiki_pipeline_default_enabled", True, user_id="u1")
    ok_on = sch.schedule_page_refresh("p1", changed=True)
    assert ok_on is True, "toggle ON 后必须立即恢复建 run（缓存已被 set_feature_flag 清空）"
    db.expire_all()
    runs = _runs(db, trigger_type="page_changed", trigger_object_id="p1")
    queued = _runs_by_status(db, "page_changed", "p1", ("queued",))
    superseded = _runs_by_status(db, "page_changed", "p1", ("superseded",))
    assert len(runs) == 2 and len(queued) == 1 and len(superseded) == 1
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 6. register_default_pipeline：同定义重复幂等；冲突定义抛 PipelineError
# ---------------------------------------------------------------------------


def test_register_default_pipeline_duplicate_same_definition_idempotent():
    from app.core.wiki_pipeline.pipelines.wiki_default import STAGE_KEYS

    register_default_pipeline()
    register_default_pipeline()  # 同 key+version+同定义 → 幂等不抛（契约十二）
    pipe = registry.get_pipeline(PIPELINE_KEY, "1")
    assert pipe is not None
    assert tuple(pipe.stage_keys()) == STAGE_KEYS


def test_register_default_pipeline_conflicting_definition_raises():
    from app.core.wiki_pipeline.pipelines.wiki_default import STAGE_KEYS

    register_default_pipeline()
    # 注册一个同 key+version 但 stage 序列不同的定义 → 冲突。
    conflicting = registry.PipelineDef(
        key=PIPELINE_KEY,
        version="1",
        stages=[registry.StageDef(key="resolve_context", version="1")],
        allow_null_workspace=False,
    )
    registry.replace_for_test(conflicting)
    with pytest.raises(registry.PipelineError):
        register_default_pipeline()
    # 原注册未被静默覆盖前的默认定义仍可通过重新注册恢复。
    unregister_default_pipeline()
    register_default_pipeline()
    pipe = registry.get_pipeline(PIPELINE_KEY, "1")
    assert tuple(pipe.stage_keys()) == STAGE_KEYS


# ---------------------------------------------------------------------------
# 7. main 注册错误 → 中止启动（raise，不再吞错继续）
# ---------------------------------------------------------------------------


def test_main_startup_aborts_on_registration_error(monkeypatch):
    import app.main as main_mod
    from app.core.dingtalk_storage import DingTalkLocalStorage
    from app.core.wiki_pipeline.pipelines import wiki_default

    # ensure_directories 打桩避免写盘副作用；注册抛真实异常 → startup 必须 raise。
    monkeypatch.setattr(DingTalkLocalStorage, "ensure_directories", lambda self: None)

    def _boom():
        raise RuntimeError("register_default_pipeline exploded")

    monkeypatch.setattr(wiki_default, "register_default_pipeline", _boom)
    with pytest.raises(RuntimeError, match="register_default_pipeline exploded"):
        main_mod._start_scheduler()
