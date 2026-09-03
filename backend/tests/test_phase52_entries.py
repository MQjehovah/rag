"""Phase 5.2（B）：生产入口全部进入 CompileRun + DB-first kill switch + legacy 改名
+ 删除 API 接线测试（入口层）。

覆盖（对应 MUST-2/3/4/5/6/7）：
1. delete API kill ON：先持久化 page_deleted CompileRun + deletion artifact 后才允许
   物理删除；run/artifact 持久化失败（pipeline 未注册等）→ 500 + Page 不删 + 无 run。
2. delete API DB-first kill switch：settings True 但 DB RuntimeFeatureFlag 行 False
   （kill off）→ 删除成功、不建 run、引用 Wiki 保持 dirty（source_page_ids/status 不变）、
   任何 _legacy_*/legacy 发布都不触发（monkeypatch 抛错也不被调用）。
2b. delete API kill on 但无 workspace 绑定 → HTTP 409 拒绝，Page/PageChunk 不删、
   零 CompileRun / 零 deletion artifact、Wiki 来源关系不变。
3. 公开 builder wrapper（build_wiki_from_pages → batch_rebuild / refresh_wiki_for_page →
   page_changed / rebuild_wiki_from_sources & refresh_dirty_wikis → manual_rebuild）
   真实调用产生对应 CompileRun + Artifact，不产生任何 legacy 调用。
4. /rebuild 与 /refresh-page-dirty 核心逻辑：按 workspace 建 batch_rebuild /
   manual_rebuild queued run；kill off 零创建 + skipped 说明。
5. no_production_legacy_bypass（源码级 AST，含 wiki_page_builder.py）：公开名称既无
   调用/import、也无「公开名称 = _legacy_*」别名；公开名称为真 wrapper。
6. kill off 全局行为：schedule_page_refresh / recover_dirty_pages / rebuild 入队 /
   dirty 刷新均不建 run、保持 dirty、不调 legacy。
7. kill on 恢复：recover_dirty_pages 为 dirty Page/Wiki 建 page_changed /
   manual_rebuild run。

fixture/helper 风格复用 test_phase51_scheduler.py（独立临时 SQLite 文件库 +
monkeypatch settings.database_url，不触碰真实库）；batch 端到端复用
test_phase52_batch.py 风格（内存 SQLite + StaticPool + fake runner）。
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import wiki as wiki_api
from app.config import settings
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.wiki_pipeline import executor, registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    ARTIFACT_TYPE_WIKI_BATCH_INPUT,
    ARTIFACT_TYPE_WIKI_PAGE_DELETED_INPUT,
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    PIPELINE_KEY,
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.models.database import (
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    PageChunk,
    RuntimeFeatureFlag,
    WikiPage,
    WikiWorkspace,
    init_db,
)

_DB_FILE_NAME = "phase52-entries.db"
_BODY = "水箱维护需要定期检查冷却水温度与压力表读数并记录运行日志。"
_SYN_BODY = "聚合正文：包含所有来源知识点"


@pytest.fixture(autouse=True)
def _phase52_entries_isolation(monkeypatch):
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


# ---------------------------------------------------------------------------
# 文件库 helpers（scheduler / delete API / wiki API 共用）
# ---------------------------------------------------------------------------


def _e52_setup_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / _DB_FILE_NAME).as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    init_db(engine)
    db = sessionmaker(bind=engine)()
    return url, engine, db


def _e52_mk_notebook(db, notebook_id="nb-1", group_id="engineering"):
    if db.get(Notebook, notebook_id) is None:
        db.add(Notebook(id=notebook_id, name="研发库", group_id=group_id))
        db.flush()
    return db.get(Notebook, notebook_id)


def _e52_mk_workspace(db, workspace_id="ws1", status="active"):
    if db.get(WikiWorkspace, workspace_id) is None:
        db.add(WikiWorkspace(
            id=workspace_id, key=f"key-{workspace_id}", name=workspace_id,
            acl_scope='{"groups": ["engineering"]}', scope_id="group:engineering",
            status=status,
        ))
        db.flush()
    return db.get(WikiWorkspace, workspace_id)


def _e52_bind_workspace(db, notebook_id="nb-1", workspace_id="ws1"):
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


def _e52_mk_page(db, page_id="p1", notebook_id="nb-1", *, dirty=True,
                 content=_BODY):
    _e52_mk_notebook(db, notebook_id)
    _e52_mk_workspace(db)
    _e52_bind_workspace(db, notebook_id)
    if db.get(Page, page_id) is None:
        db.add(Page(
            id=page_id, notebook_id=notebook_id,
            title="水箱维护", content=content,
            wiki_dirty=dirty,
        ))
    db.commit()


def _e52_mk_wiki(db, wiki_id="w1", workspace_id="ws1", *, dirty=True,
                 source_page_ids=None, status=None):
    _e52_mk_workspace(db, workspace_id)
    if db.get(WikiPage, wiki_id) is None:
        if status is None:
            status = "draft" if dirty else "published"
        db.add(WikiPage(
            id=wiki_id, title="水箱维护", workspace_id=workspace_id,
            acl_scope='{"groups": ["engineering"]}',
            dirty=dirty, status=status,
            source_page_ids=json.dumps(source_page_ids or []),
        ))
    db.commit()
    return db.get(WikiPage, wiki_id)


def _e52_set_db_flag(db, enabled: bool) -> None:
    row = db.query(RuntimeFeatureFlag).filter(
        RuntimeFeatureFlag.name == "wiki_pipeline_default_enabled"
    ).first()
    if row is None:
        db.add(RuntimeFeatureFlag(name="wiki_pipeline_default_enabled", enabled=enabled))
    else:
        row.enabled = enabled
    db.commit()


def _e52_runs(db, trigger_type=None, trigger_object_id=None):
    q = db.query(CompileRun).filter(CompileRun.pipeline_key == PIPELINE_KEY)
    if trigger_type is not None:
        q = q.filter(CompileRun.trigger_type == trigger_type)
    if trigger_object_id is not None:
        q = q.filter(CompileRun.trigger_object_id == trigger_object_id)
    return q.all()


def _e52_any_runs(db):
    return db.query(CompileRun).count()


def _e52_artifact(db, run_id, artifact_type):
    return (
        db.query(Artifact)
        .filter(
            Artifact.run_id == run_id,
            Artifact.artifact_type == artifact_type,
        )
        .order_by(Artifact.created_at.desc())
        .first()
    )


def _e52_kill_off(db):
    _e52_set_db_flag(db, False)
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    sch.clear_kill_switch_cache()


def _e52_kill_on():
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    sch.clear_kill_switch_cache()


def _e52_arm_legacy_booms(monkeypatch):
    """monkeypatch 所有 legacy 发布函数抛错：任何生产入口调用都会立即失败。"""
    targets = {
        "remove_source_page_from_wikis",
        "_legacy_process_page_wiki",
        "_legacy_refresh_dirty_wikis",
        "_legacy_build_wiki_from_pages",
        "_legacy_refresh_wiki_for_page",
        "_legacy_rebuild_wiki_from_sources",
    }
    for name in targets:
        def _boom(*args, **kwargs):
            raise AssertionError(f"生产路径不得调用 legacy 发布函数 {name}")
        monkeypatch.setattr(builder, name, _boom)


# ---------------------------------------------------------------------------
# 1. delete API kill ON：run/artifact 持久化失败 → 500 + Page 不删 + 无 run
# ---------------------------------------------------------------------------


def test_delete_api_does_not_delete_page_when_run_or_artifact_persist_fails(
    tmp_path, monkeypatch,
):
    import app.api.pages as pages_mod
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    from fastapi import HTTPException

    _url, engine, db = _e52_setup_db(tmp_path, monkeypatch)
    _e52_mk_page(db, "p1")
    monkeypatch.setattr(pages_mod, "_check_page_manage", lambda user: None)
    monkeypatch.setattr(pages_mod, "_check_page_access", lambda page, user, d: None)
    sch.clear_kill_switch_cache()
    # 故意不注册 wiki.default pipeline：kill on（settings 默认 True，无 DB 行）时
    # create_page_deleted_run 在 executor 层抛 pipeline_not_registered。
    assert sch._pipeline_kill_switch_enabled() is True

    try:
        with pytest.raises(HTTPException) as excinfo:
            pages_mod.delete_page("p1", db=db, current_user={})
        assert excinfo.value.status_code == 500
        assert "未删除页面" in (excinfo.value.detail or "")
        db.expire_all()
        assert db.get(Page, "p1") is not None, "run/artifact 持久化失败不得删除 Page"
        assert _e52_any_runs(db) == 0, "失败路径 rollback，不得残留 CompileRun"
        art_count = (
            db.query(Artifact)
            .filter(Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PAGE_DELETED_INPUT)
            .count()
        )
        assert art_count == 0, "失败路径 rollback，不得残留 deletion artifact"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. delete API DB-first kill switch（settings True + DB row False → kill off）
# ---------------------------------------------------------------------------


def test_delete_api_uses_db_first_kill_switch(tmp_path, monkeypatch):
    import app.api.pages as pages_mod
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _e52_setup_db(tmp_path, monkeypatch)
    _e52_mk_page(db, "p1")
    _e52_mk_wiki(db, "w1", "ws1", dirty=False, status="published",
                 source_page_ids=["p1"])
    monkeypatch.setattr(pages_mod, "_check_page_manage", lambda user: None)
    monkeypatch.setattr(pages_mod, "_check_page_access", lambda page, user, d: None)

    # settings 默认 True，但 DB RuntimeFeatureFlag 行 enabled=False → DB-first kill。
    assert settings.wiki_pipeline_default_enabled is True
    _e52_kill_off(db)
    assert sch._pipeline_kill_switch_enabled() is False, "DB-first：DB 行必须覆盖 settings"

    _e52_arm_legacy_booms(monkeypatch)

    try:
        resp = pages_mod.delete_page("p1", db=db, current_user={})
        assert resp["message"] == "删除成功"
        db.expire_all()
        assert db.get(Page, "p1") is None, "kill off：删除应成功（不建 run）"
        assert _e52_any_runs(db) == 0, "kill off 不得创建任何 CompileRun"
        wiki = db.get(WikiPage, "w1")
        assert wiki is not None
        assert wiki.dirty is True, "kill off：引用 Wiki 应保持 dirty（可恢复）"
        assert wiki.status == "published", "kill off 不改 status"
        assert json.loads(wiki.source_page_ids or "[]") == ["p1"], \
            "kill off 不改 source_page_ids"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 3. delete API kill ON 成功：先持久化 page_deleted run + artifact，再物理删除
# ---------------------------------------------------------------------------


def test_delete_api_kill_on_persists_run_before_physical_delete(
    tmp_path, monkeypatch,
):
    import app.api.pages as pages_mod
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    register_default_pipeline()
    try:
        _url, engine, db = _e52_setup_db(tmp_path, monkeypatch)
        _e52_mk_page(db, "p1")
        _e52_mk_wiki(db, "w1", "ws1", dirty=False, status="published",
                     source_page_ids=["p1"])
        monkeypatch.setattr(pages_mod, "_check_page_manage", lambda user: None)
        monkeypatch.setattr(pages_mod, "_check_page_access", lambda page, user, d: None)
        _e52_kill_on()
        assert sch._pipeline_kill_switch_enabled() is True
        _e52_arm_legacy_booms(monkeypatch)

        try:
            resp = pages_mod.delete_page("p1", db=db, current_user={})
            assert resp["message"] == "删除成功"
            db.expire_all()
            assert db.get(Page, "p1") is None, "kill on：run 持久化成功后才允许物理删除"
            runs = _e52_runs(db, trigger_type="page_deleted", trigger_object_id="p1")
            assert len(runs) == 1 and runs[0].status == "queued", \
                "kill on 必须持久化 page_deleted run"
            assert runs[0].workspace_id == "ws1"
            art = _e52_artifact(db, runs[0].id, ARTIFACT_TYPE_WIKI_PAGE_DELETED_INPUT)
            assert art is not None, "kill on 必须持久化 deletion input artifact"
            payload = json.loads(art.payload_json)
            assert payload["page_id"] == "p1"
            assert payload["workspace_id"] == "ws1"
            wiki = db.get(WikiPage, "w1")
            assert wiki is not None
            assert wiki.dirty is False, "kill on 删除语义移交 pipeline，不即时改 wiki dirty"
        finally:
            db.close()
            engine.dispose()
    finally:
        unregister_default_pipeline()


# ---------------------------------------------------------------------------
# 4. delete API kill on 但无 workspace 绑定：拒绝（409），Page/PageChunk/Wiki 全不动
# ---------------------------------------------------------------------------


def test_delete_api_kill_on_unbound_page_is_rejected(tmp_path, monkeypatch):
    import app.api.pages as pages_mod
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    from fastapi import HTTPException

    _url, engine, db = _e52_setup_db(tmp_path, monkeypatch)
    # notebook 存在但无 active binding → page_workspace_id=None。
    _e52_mk_notebook(db, "nb-unbound")
    db.add(Page(id="p-unbound", notebook_id="nb-unbound",
                title="未绑定", content="未绑定正文足够长用于删除接线测试。"))
    db.add(PageChunk(id="chunk-unbound-1", page_id="p-unbound", chunk_index=0,
                     content="chunk"))
    db.commit()
    _e52_mk_wiki(db, "w-u", "ws1", dirty=False, status="published",
                 source_page_ids=["p-unbound"])
    monkeypatch.setattr(pages_mod, "_check_page_manage", lambda user: None)
    monkeypatch.setattr(pages_mod, "_check_page_access", lambda page, user, d: None)
    _e52_kill_on()
    assert sch._pipeline_kill_switch_enabled() is True
    _e52_arm_legacy_booms(monkeypatch)

    try:
        with pytest.raises(HTTPException) as excinfo:
            pages_mod.delete_page("p-unbound", db=db, current_user={})
        assert excinfo.value.status_code == 409
        assert "page_workspace_required" in (excinfo.value.detail or "")
        assert "未执行删除" in (excinfo.value.detail or "")
        db.expire_all()
        assert db.get(Page, "p-unbound") is not None, "kill on 无 workspace → Page 不得删除"
        assert db.get(PageChunk, "chunk-unbound-1") is not None, \
            "kill on 无 workspace → PageChunk 不得删除"
        assert _e52_any_runs(db) == 0, "kill on 无 workspace → 不得创建残缺 CompileRun"
        art_count = (
            db.query(Artifact)
            .filter(Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PAGE_DELETED_INPUT)
            .count()
        )
        assert art_count == 0, "kill on 无 workspace → 不得残留 deletion artifact"
        wiki = db.get(WikiPage, "w-u")
        assert wiki is not None
        assert wiki.status == "published", "Wiki 来源关系不得改变"
        assert json.loads(wiki.source_page_ids or "[]") == ["p-unbound"]
        assert wiki.dirty is False, "kill on 无 workspace 不得置 dirty"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 5. 公开 builder wrapper（build_wiki_from_pages）走 batch_rebuild CompileRun，
#    不调 legacy process_page_wiki
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _mk_mem_page(db, page_id, title, content, notebook_id="nb-1",
                 group_id="engineering"):
    if db.get(Notebook, notebook_id) is None:
        db.add(Notebook(id=notebook_id, name="研发库", group_id=group_id))
        db.flush()
    from app.core.wiki_workspace.routing import ensure_notebook_workspace
    ws = ensure_notebook_workspace(db, db.get(Notebook, notebook_id))
    if db.get(Page, page_id) is None:
        db.add(Page(id=page_id, notebook_id=notebook_id, title=title, content=content))
        db.commit()
    return ws.id, db.get(Page, page_id)


def _graph_noop(**kw):
    return True


def _mk_sync_llm(*, ops=None, body=_SYN_BODY):
    def _run(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": ops if ops is not None else []}
        if context == "wiki-synthesis":
            return {"summary": "聚合摘要", "content": body}
        if context == "wiki-batch-summary":
            return {"summary": "批次摘要"}
        if context == "wiki-mapreduce":
            return {"summary": "最终", "content": body}
        return {"worthy": True, "ops": ops if ops is not None else []}
    return _run


def test_public_build_wiki_from_pages_uses_batch_compile_run(db, monkeypatch):
    register_default_pipeline()
    try:
        ws_id, _ = _mk_mem_page(db, "p1", "来源一", "内容一足够长用于编译测试构建。")
        _, p2 = _mk_mem_page(db, "p2", "来源二", "内容二足够长用于编译测试构建。",
                             notebook_id="nb-1")
        llm = _mk_sync_llm(ops=[{"action": "update", "title": "驱动主题"}])
        executor.configure_external_runners(llm_runner=llm, graph_runner=_graph_noop)

        def _boom(*args, **kwargs):
            raise AssertionError("batch driver 不得调用 legacy process_page_wiki")

        monkeypatch.setattr(builder, "_legacy_process_page_wiki", _boom)

        import asyncio

        stats = asyncio.run(
            builder.build_wiki_from_pages(db, [db.get(Page, "p1"), db.get(Page, "p2")])
        )
        assert stats["failed"] == 0
        db.expire_all()
        runs = db.query(CompileRun).filter(
            CompileRun.trigger_type == "batch_rebuild",
            CompileRun.workspace_id == ws_id,
        ).all()
        assert len(runs) == 1 and runs[0].status == "succeeded", \
            "公开 build_wiki_from_pages 必须建 batch_rebuild run 并执行成功"
        art = _e52_artifact(db, runs[0].id, ARTIFACT_TYPE_WIKI_BATCH_INPUT)
        assert art is not None, "batch_rebuild run 必须带 wiki_batch_input artifact"
        payload = json.loads(art.payload_json)
        assert payload["page_ids"] == ["p1", "p2"]

        _, p3 = _mk_mem_page(db, "p3", "来源三", "内容三足够长用于编译测试构建。",
                             notebook_id="nb-1")
        stats2 = asyncio.run(builder.build_wiki_from_pages(db, [db.get(Page, "p3")]))
        assert stats2["failed"] == 0
        db.expire_all()
        runs2 = db.query(CompileRun).filter(
            CompileRun.trigger_type == "batch_rebuild",
            CompileRun.workspace_id == ws_id,
        ).all()
        assert len(runs2) == 2, "多次公开调用各建一个 batch_rebuild run"
    finally:
        unregister_default_pipeline()


# ---------------------------------------------------------------------------
# 5b. 其它公开 wrapper 真实调用 → 各自 CompileRun + Manifest Artifact
# ---------------------------------------------------------------------------


def _mem_mk_dirty_wiki(db, wiki_id, ws_id, page_id, *, workspace_id=None,
                       title="水箱维护流程"):
    """内存库构造引用 page 的 dirty wiki（可 workspace_id=None 模拟未归属）。"""
    scope_json = '{"groups": ["engineering"]}'
    wp = WikiPage(
        id=wiki_id, title=title, workspace_id=workspace_id if workspace_id is not None else ws_id,
        acl_scope=scope_json, dirty=True, status="draft",
        source_page_ids=json.dumps([page_id] if page_id else []),
    )
    db.add(wp)
    db.commit()
    return db.get(WikiPage, wiki_id)


def test_public_refresh_wiki_for_page_uses_page_changed_compile_run(db):
    import asyncio

    register_default_pipeline()
    try:
        ws_id, _ = _mk_mem_page(db, "pr", "水箱维护流程", "水箱维护流程的内容足够长用于编译构建测试。")
        llm = _mk_sync_llm(ops=[{"action": "create", "title": "水箱维护流程"}])
        executor.configure_external_runners(llm_runner=llm, graph_runner=_graph_noop)

        stats = asyncio.run(builder.refresh_wiki_for_page(db, db.get(Page, "pr")))
        assert stats["failed"] == 0, stats
        db.expire_all()
        runs = db.query(CompileRun).filter(
            CompileRun.trigger_type == "page_changed",
            CompileRun.trigger_object_id == "pr",
        ).all()
        assert len(runs) == 1 and runs[0].status == "succeeded", \
            "公开 refresh_wiki_for_page 必须建 page_changed run 并执行成功"
        assert _e52_artifact(db, runs[0].id, ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST) is not None, \
            "page_changed run 必须带 publish Manifest artifact"
    finally:
        unregister_default_pipeline()


def test_public_rebuild_wiki_from_sources_uses_manual_rebuild_compile_run(db):
    import asyncio

    register_default_pipeline()
    try:
        ws_id, _ = _mk_mem_page(db, "prb", "水箱维护流程", "水箱维护流程的内容足够长用于编译构建测试。")
        wp = _mem_mk_dirty_wiki(db, "w-rb", ws_id, "prb")
        llm = _mk_sync_llm(ops=[])
        executor.configure_external_runners(llm_runner=llm, graph_runner=_graph_noop)

        result = asyncio.run(builder.rebuild_wiki_from_sources(db, wp.id, None))
        assert result["wiki_id"] == wp.id
        db.expire_all()
        runs = db.query(CompileRun).filter(
            CompileRun.trigger_type == "manual_rebuild",
            CompileRun.trigger_object_id == wp.id,
        ).all()
        assert len(runs) == 1 and runs[0].status == "succeeded", \
            "公开 rebuild_wiki_from_sources 必须建 manual_rebuild run 并执行成功"
        assert _e52_artifact(db, runs[0].id, ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST) is not None, \
            "manual_rebuild run 必须带 publish Manifest artifact"
    finally:
        unregister_default_pipeline()


def test_public_refresh_dirty_wikis_uses_manual_rebuild_compile_runs(db):
    import asyncio

    register_default_pipeline()
    try:
        ws_id, _ = _mk_mem_page(db, "prd", "水箱维护流程", "水箱维护流程的内容足够长用于编译构建测试。")
        bound = _mem_mk_dirty_wiki(db, "w-b", ws_id, "prd")
        unbound = _mem_mk_dirty_wiki(db, "w-ub", ws_id, "", workspace_id=None)
        llm = _mk_sync_llm(ops=[])
        executor.configure_external_runners(llm_runner=llm, graph_runner=_graph_noop)

        result = asyncio.run(builder.refresh_dirty_wikis(db))
        assert result["archived"] == 1, "未归属 dirty wiki 直接 archived"
        db.expire_all()
        assert db.get(WikiPage, unbound.id).status == "archived"
        runs = db.query(CompileRun).filter(
            CompileRun.trigger_type == "manual_rebuild",
            CompileRun.trigger_object_id == bound.id,
        ).all()
        assert len(runs) == 1 and runs[0].status == "succeeded", \
            "公开 refresh_dirty_wikis 必须为 dirty wiki 建 manual_rebuild run"
        assert _e52_artifact(db, runs[0].id, ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST) is not None
        db.expire_all()
        assert db.get(WikiPage, bound.id).dirty is False, "发布成功应清 dirty"
    finally:
        unregister_default_pipeline()


# ---------------------------------------------------------------------------
# 5. /rebuild + /refresh-page-dirty 核心逻辑建 CompileRun；kill off 零创建
# ---------------------------------------------------------------------------


def test_wiki_admin_rebuild_endpoints_create_compile_runs(tmp_path, monkeypatch):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    register_default_pipeline()
    try:
        _url, engine, db = _e52_setup_db(tmp_path, monkeypatch)
        _e52_mk_page(db, "p1")
        _e52_mk_page(db, "p2")
        _e52_mk_wiki(db, "w1", "ws1", dirty=True)
        _e52_kill_on()

        # /rebuild 核心：按 workspace 分组 → 每 workspace 一个 batch_rebuild run。
        result = wiki_api._enqueue_rebuild_batches(db)
        assert result["skipped"] is False
        assert result["workspaces"] == 1 and result["pages"] == 2
        db.expire_all()
        batch_runs = _e52_runs(db, trigger_type="batch_rebuild")
        assert len(batch_runs) == 1 and batch_runs[0].status == "queued"
        assert batch_runs[0].workspace_id == "ws1"
        art = _e52_artifact(db, batch_runs[0].id, ARTIFACT_TYPE_WIKI_BATCH_INPUT)
        assert art is not None
        payload = json.loads(art.payload_json)
        assert set(payload["page_ids"]) == {"p1", "p2"}

        # /refresh-page-dirty 核心：dirty Wiki → manual_rebuild run。
        refresh = wiki_api._refresh_dirty_wikis_scheduled(db)
        assert refresh["skipped"] is False and refresh["submitted"] == 1
        db.expire_all()
        manual_runs = _e52_runs(db, trigger_type="manual_rebuild", trigger_object_id="w1")
        assert len(manual_runs) == 1 and manual_runs[0].status == "queued"

        # kill off：rebuild 与 dirty 刷新均零创建 + skipped 说明。
        _e52_kill_off(db)
        assert sch._pipeline_kill_switch_enabled() is False
        result_off = wiki_api._enqueue_rebuild_batches(db)
        assert result_off["skipped"] is True and result_off["runs"] == 0
        assert "kill switch off" in result_off["message"]
        refresh_off = wiki_api._refresh_dirty_wikis_scheduled(db)
        assert refresh_off["skipped"] is True and refresh_off["submitted"] == 0
        assert "kill switch off" in refresh_off["message"]
        db.expire_all()
        assert len(_e52_runs(db, trigger_type="batch_rebuild")) == 1, \
            "kill off 不得新增 batch run"
        assert len(_e52_runs(db, trigger_type="manual_rebuild",
                             trigger_object_id="w1")) == 1, \
            "kill off 不得新增 manual_rebuild run"
        assert db.get(WikiPage, "w1").dirty is True, "kill off 保持 dirty"
    finally:
        unregister_default_pipeline()
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 6. 生产源码级 legacy bypass 核查（AST，忽略注释/docstring）
# ---------------------------------------------------------------------------

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LEGACY_ENTRY_NAMES = (
    "build_wiki_from_pages",
    "refresh_dirty_wikis",
    "rebuild_wiki_from_sources",
    "refresh_wiki_for_page",
    "process_page_wiki",
)


def _ast_legacy_calls(tree, names):
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in names:
                hits.append((fn.id, node.lineno))
            elif isinstance(fn, ast.Attribute) and fn.attr in names:
                hits.append((fn.attr, node.lineno))
    return hits


def _ast_imports_module(tree, fragment: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if fragment in node.module:
                return True
    return False


def _ast_imports_legacy_entry(tree, names) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if not node.module.endswith("wiki_page_builder"):
                continue
            for alias in node.names:
                if alias.name in names and not alias.name.startswith("_"):
                    hits.append((alias.name, node.lineno))
    return hits


def _ast_public_alias_to_legacy(tree, names) -> list[tuple[str, int]]:
    """拒绝「公开名称 = _legacy_*」赋值（如 build_wiki_from_pages = _legacy_...）。"""
    hits: list[tuple[str, int]] = []
    legacy_ids = {f"_legacy_{n}" for n in names}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for t in targets:
                if isinstance(t, ast.Name) and t.id in names:
                    if isinstance(value, ast.Name) and value.id in legacy_ids:
                        hits.append((t.id, node.lineno))
    return hits


def test_no_production_legacy_bypass():
    builder_path = _BACKEND_ROOT / "app" / "core" / "knowledge_compiler_v3" \
        / "wiki_page_builder.py"
    files = {
        "app/api/pages.py": _BACKEND_ROOT / "app" / "api" / "pages.py",
        "app/api/wiki.py": _BACKEND_ROOT / "app" / "api" / "wiki.py",
        "scheduler": _BACKEND_ROOT / "app" / "core" / "knowledge_compiler_v3"
        / "wiki_refresh_scheduler.py",
        "multi_page": _BACKEND_ROOT / "app" / "core" / "wiki_pipeline" / "pipelines"
        / "multi_page.py",
        "main": _BACKEND_ROOT / "app" / "main.py",
        "wiki_page_builder": builder_path,
    }
    for label, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = _ast_legacy_calls(tree, _LEGACY_ENTRY_NAMES)
        assert hits == [], f"{label} 仍调用 legacy 入口: {hits}"
        legacy_imports = _ast_imports_legacy_entry(tree, _LEGACY_ENTRY_NAMES)
        assert legacy_imports == [], f"{label} 仍 import legacy 入口: {legacy_imports}"
        aliases = _ast_public_alias_to_legacy(tree, _LEGACY_ENTRY_NAMES)
        assert aliases == [], f"{label} 存在「公开名称 = _legacy_*」别名: {aliases}"

    # 公开 builder 名称必须是真 wrapper（async def），且 process_page_wiki 已私有化。
    builder_src = builder_path.read_text(encoding="utf-8")
    assert "async def build_wiki_from_pages(" in builder_src
    assert "async def refresh_wiki_for_page(" in builder_src
    assert "async def rebuild_wiki_from_sources(" in builder_src
    assert "async def refresh_dirty_wikis(" in builder_src
    assert "async def _legacy_process_page_wiki(" in builder_src
    for name in _LEGACY_ENTRY_NAMES:
        assert f"{name} = _legacy_" not in builder_src, \
            f"公开名称 {name} 不得指向 _legacy_*"

    # API / Page CRUD 不再直接 import executor 或 legacy builder 模块。
    api_pages_tree = ast.parse(
        (_BACKEND_ROOT / "app" / "api" / "pages.py").read_text(encoding="utf-8")
    )
    api_wiki_tree = ast.parse(
        (_BACKEND_ROOT / "app" / "api" / "wiki.py").read_text(encoding="utf-8")
    )
    for tree, label in ((api_pages_tree, "pages.py"), (api_wiki_tree, "wiki.py")):
        assert not _ast_imports_module(tree, "wiki_pipeline.executor"), \
            f"{label} 不得直接 import executor"
        assert not _ast_imports_module(tree, "wiki_page_builder"), \
            f"{label} 不得直接 import legacy builder"


# ---------------------------------------------------------------------------
# 7. kill off 全局：不建 run、保持 dirty、不调 legacy
# ---------------------------------------------------------------------------


def test_flag_off_keeps_dirty_without_legacy_publish(tmp_path, monkeypatch):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    _url, engine, db = _e52_setup_db(tmp_path, monkeypatch)
    _e52_mk_page(db, "p1", dirty=True)
    _e52_mk_wiki(db, "w1", "ws1", dirty=True, source_page_ids=["p1"])
    _e52_kill_off(db)
    assert sch._pipeline_kill_switch_enabled() is False
    _e52_arm_legacy_booms(monkeypatch)

    try:
        # schedule_page_refresh：kill off → False，Page 保持 dirty。
        assert sch.schedule_page_refresh("p1", changed=True) is False
        db.expire_all()
        assert db.get(Page, "p1").wiki_dirty is True

        # recover_dirty_pages：kill off → 零统计，不建 run。
        result = sch.recover_dirty_pages()
        assert result == {"submitted": 0, "rejected": 0, "wiki_loaded": 0}

        # /rebuild 核心逻辑：kill off → skipped、零 run。
        rebuild_off = wiki_api._enqueue_rebuild_batches(db)
        assert rebuild_off["skipped"] is True and rebuild_off["runs"] == 0

        # /refresh-page-dirty 核心逻辑：kill off → skipped、零提交，Wiki 保持 dirty。
        refresh_off = wiki_api._refresh_dirty_wikis_scheduled(db)
        assert refresh_off["skipped"] is True and refresh_off["submitted"] == 0

        db.expire_all()
        assert _e52_any_runs(db) == 0, "kill off 全局不得建任何 run"
        assert db.get(Page, "p1").wiki_dirty is True
        assert db.get(WikiPage, "w1").dirty is True, "kill off 保持 Wiki dirty"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 8. kill on 恢复：recover_dirty_pages 建 page_changed / manual_rebuild run
# ---------------------------------------------------------------------------


def test_flag_on_recovery_enqueues_runs(tmp_path, monkeypatch):
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch

    register_default_pipeline()
    try:
        _url, engine, db = _e52_setup_db(tmp_path, monkeypatch)
        _e52_mk_page(db, "dirty-1", dirty=True)
        _e52_mk_wiki(db, "w1", "ws1", dirty=True)
        _e52_kill_on()

        result = sch.recover_dirty_pages()
        assert result["submitted"] >= 1, "dirty Page 应被装载"
        assert result["wiki_loaded"] >= 1, "dirty Wiki 应被装载"
        db.expire_all()
        page_runs = _e52_runs(db, trigger_type="page_changed", trigger_object_id="dirty-1")
        assert len(page_runs) == 1 and page_runs[0].status == "queued"
        wiki_runs = _e52_runs(db, trigger_type="manual_rebuild", trigger_object_id="w1")
        assert len(wiki_runs) == 1 and wiki_runs[0].status == "queued"
    finally:
        unregister_default_pipeline()
        db.close()
        engine.dispose()
