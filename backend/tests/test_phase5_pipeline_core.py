"""Phase 5：wiki.default 真实流水线核心测试（W1，无 HTTP / 无真实 LLM / 无真实 DB）。

覆盖：
- register_default_pipeline：pipeline 定义 shape（版本/stage 顺序/flag/allow_null_workspace）。
- 端到端 page_changed → create → publish：run succeeded、WikiPage/Revision 落库、
  Page 状态清理、图谱 runner 收到调度。
- 内容过短 → not_worthy：run succeeded 且无产品写（不新建 wiki）。
- 注入 runner（llm/graph）与 reset 隔离。

参考 fixture 风格：test_wiki_pipeline_core.py（db/pipelines/helper）+ test_wiki_v4_acceptance
（fake LLM 按 context 分流）。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3.wiki_page_builder import call_wiki_llm_json  # noqa: F401
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline import registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    PIPELINE_KEY,
    PIPELINE_VERSION,
    STAGE_KEYS,
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)

CREATE_OPS = [{"action": "create", "title": "水箱维护流程", "category": "操作指南"}]
SYNTHESIS_BODY = "水箱维护需要每日检查水位与温度传感器，并记录运行日志。"


def _mk_llm_runner(*, ops=None):
    """同步 fake llm_runner：ingest 返回 ops；synthesis 返回固定正文。"""
    ingest = ops if ops is not None else CREATE_OPS

    def _run(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            return {"summary": "水箱维护流程摘要", "content": SYNTHESIS_BODY}
        return {"worthy": True, "ops": ingest}

    return _run


@pytest.fixture(autouse=True)
def _phase5_isolation():
    """每个测试前后清理注册表 + 外部 runner（防跨测试泄漏）。"""
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    registry.REGISTRY.clear()
    executor.reset_external_runners()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def wiki_pipeline():
    register_default_pipeline()
    yield PIPELINE_KEY
    unregister_default_pipeline()


def _mk_page(db, *, page_id="p1", title="水箱维护", content="水箱固定内容足够长用于构建", notebook_id="nb-1", group_id="engineering"):
    if db.get(Notebook, notebook_id) is None:
        db.add(Notebook(id=notebook_id, name="研发库", group_id=group_id))
        db.flush()
    notebook = db.get(Notebook, notebook_id)
    ws = ensure_notebook_workspace(db, notebook)
    assert ws is not None and ws.status == "active"
    if db.get(Page, page_id) is None:
        db.add(Page(id=page_id, notebook_id=notebook_id, title=title, content=content))
    db.commit()
    return ws.id, db.get(Page, page_id)


def _expected_input_hash(db, page: Page) -> str:
    scope = builder._page_scope(db, page)
    acl = builder._scope_to_acl_json(scope) if scope else None
    text = builder._page_text(db, page)
    return builder._page_input_hash(page.title or "", text, page.notebook_id or "", acl or "")


def _stage_statuses(db, run_id):
    rows = db.query(StageRun).filter(StageRun.run_id == run_id).order_by(
        StageRun.stage_order, StageRun.attempt
    ).all()
    return {r.stage_key: r.status for r in rows}


# ---------------------------------------------------------------------------
# pipeline 定义 shape
# ---------------------------------------------------------------------------


def test_register_default_pipeline_definition(wiki_pipeline):
    pipe = registry.get_pipeline(PIPELINE_KEY)
    assert pipe is not None
    assert pipe.version == PIPELINE_VERSION
    assert pipe.allow_null_workspace is False  # 产品流水线：workspace 必填
    assert pipe.stage_keys() == list(STAGE_KEYS) == [
        "resolve_context",
        "topic_route",
        "synthesize_default",
        "validate_default",
        "publish_default",
        "finalize_compile_outcome",
        "schedule_graph",
    ]
    flags = {s.key: (s.retryable, s.allows_publish, s.cachable) for s in pipe.stages}
    assert flags["resolve_context"] == (True, False, False)
    assert flags["topic_route"] == (True, False, False)
    assert flags["synthesize_default"] == (True, False, False)
    assert flags["validate_default"] == (False, False, False)
    assert flags["publish_default"] == (False, True, False)  # 唯一产品写
    assert flags["finalize_compile_outcome"] == (False, False, False)
    assert flags["schedule_graph"] == (True, False, False)


def test_register_default_pipeline_duplicate_idempotent(wiki_pipeline):
    # 契约十二：同 key+version+同定义重复注册 → 幂等返回，不抛。
    register_default_pipeline()
    pipe = registry.get_pipeline(PIPELINE_KEY, PIPELINE_VERSION)
    assert pipe is not None and pipe.version == PIPELINE_VERSION
    assert pipe.stage_keys() == list(STAGE_KEYS)


def test_register_default_pipeline_conflict_definition_raises(wiki_pipeline):
    # 已注册但定义（stage 序列）不同 → PipelineError（拒绝静默覆盖，启动应中止）。
    registry.replace_for_test(registry.PipelineDef(
        key=PIPELINE_KEY,
        version=PIPELINE_VERSION,
        stages=[registry.StageDef(key="resolve_context", version="1")],
        allow_null_workspace=False,
    ))
    with pytest.raises(registry.PipelineError):
        register_default_pipeline()


def test_runners_injectable_and_resettable():
    def _f(messages, context="", timeout=120.0):
        return {}

    def _g(**kw):
        return True

    executor.configure_external_runners(llm_runner=_f, graph_runner=_g)
    assert executor._LLM_RUNNER is _f
    assert executor._GRAPH_RUNNER is _g
    executor.reset_external_runners()
    assert executor._LLM_RUNNER is None
    assert executor._GRAPH_RUNNER is None


# ---------------------------------------------------------------------------
# 端到端：page_changed → create → publish（fake LLM + 空 graph runner）
# ---------------------------------------------------------------------------


def _run_page_change(db, page, ws_id, llm_ops):
    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        trigger_type="page_changed",
        trigger_object_id=page.id,
        workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    return executed


def test_e2e_page_changed_create_publish(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    calls: list[dict] = []

    def _graph_runner(**kw):
        calls.append(kw)

    executor.configure_external_runners(llm_runner=_mk_llm_runner(), graph_runner=_graph_runner)

    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        trigger_type="page_changed",
        trigger_object_id=page.id,
        workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded"
    assert executed.workspace_id == ws_id
    assert executed.output_revision_id  # publish 返回 output_revision_id

    # 六个 stage 全部 succeeded（无 skipped / failed）
    assert _stage_statuses(db, executed.id) == {k: "succeeded" for k in STAGE_KEYS}

    # WikiPage 生成：published / workspace / source_page_ids / current_revision_id
    wiki = db.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
    assert wiki is not None
    assert wiki.status == "published"
    assert wiki.workspace_id == ws_id
    assert page.id in json.loads(wiki.source_page_ids or "[]")
    assert wiki.dirty is False
    assert wiki.current_revision_id == executed.output_revision_id

    # Revision 落库：published + 正文 Section
    rev = db.get(WikiRevision, wiki.current_revision_id)
    assert rev is not None
    assert rev.status == "published"
    assert rev.edit_type == "auto"
    facts = db.query(WikiSection).filter(
        WikiSection.revision_id == rev.id,
        WikiSection.section_type == "facts",
    ).first()
    assert facts is not None and SYNTHESIS_BODY in (facts.content or "")

    # Page 状态：dirty 清理 + 编译 hash 写入
    fresh = db.get(Page, page.id)
    assert fresh.wiki_dirty is False
    assert fresh.wiki_compiled_content_hash == _expected_input_hash(db, fresh)

    # schedule_graph：publish 成功后调度 wiki + page
    assert calls == [{"wiki_page_id": wiki.id}, {"page_id": page.id}]


# ---------------------------------------------------------------------------
# 端到端：第二次 page_changed → update 既有主题（不重复建 Wiki）
# ---------------------------------------------------------------------------


def test_e2e_page_changed_update_existing_wiki(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    executor.configure_external_runners(
        llm_runner=_mk_llm_runner(ops=CREATE_OPS), graph_runner=lambda **kw: None,
    )
    r1 = _run_page_change(db, page, ws_id, CREATE_OPS)
    assert r1.status == "succeeded"
    wiki1 = db.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
    assert wiki1 is not None and wiki1.status == "published"
    old_rev_id = wiki1.current_revision_id  # 提前固化（同一 Session 身份映射会被后续改写）

    # Page 内容更新 → LLM 判定 update 既有主题
    fresh = db.get(Page, page.id)
    fresh.content = "水箱维护需要每四小时检查一次水位并补充软化水，记录温度曲线。"
    db.commit()
    update_ops = [{"action": "update", "title": "水箱维护流程", "category": "操作指南"}]

    calls: list[dict] = []
    executor.configure_external_runners(
        llm_runner=_mk_llm_runner(ops=update_ops),
        graph_runner=lambda **kw: calls.append(kw),
    )
    r2 = _run_page_change(db, page, ws_id, update_ops)
    db.refresh(r2)
    assert r2.status == "succeeded"
    assert _stage_statuses(db, r2.id) == {k: "succeeded" for k in STAGE_KEYS}

    # 不重复建 Wiki：同一标题唯一，新增 Revision 并切换 current
    db.expire_all()
    wikis = db.query(WikiPage).filter(WikiPage.title == "水箱维护流程").all()
    assert len(wikis) == 1
    wiki = wikis[0]
    assert wiki.status == "published"
    assert wiki.current_revision_id != old_rev_id
    assert wiki.current_revision_id == r2.output_revision_id
    assert page.id in json.loads(wiki.source_page_ids or "[]")
    fresh = db.get(Page, page.id)
    assert fresh.wiki_dirty is False
    assert fresh.wiki_compiled_content_hash == _expected_input_hash(db, fresh)


# ---------------------------------------------------------------------------
# 端到端：manual_rebuild（dirty Wiki 重建）→ 复用既有 Wiki 追加 Revision
# ---------------------------------------------------------------------------


def test_e2e_manual_rebuild_dirty_wiki(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    executor.configure_external_runners(
        llm_runner=_mk_llm_runner(ops=CREATE_OPS), graph_runner=lambda **kw: None,
    )
    r1 = _run_page_change(db, page, ws_id, CREATE_OPS)
    assert r1.status == "succeeded"
    wiki = db.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
    old_rev = wiki.current_revision_id

    wiki.dirty = True
    db.commit()

    calls: list[dict] = []
    executor.configure_external_runners(
        llm_runner=_mk_llm_runner(ops=CREATE_OPS),
        graph_runner=lambda **kw: calls.append(kw),
    )
    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        trigger_type="manual_rebuild",
        wiki_page_id=wiki.id,
        workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded"
    assert _stage_statuses(db, executed.id) == {k: "succeeded" for k in STAGE_KEYS}

    db.expire_all()
    wiki = db.get(WikiPage, wiki.id)
    assert wiki.status == "published"
    assert wiki.dirty is False
    assert wiki.current_revision_id != old_rev  # 重建追加新 Revision
    assert wiki.current_revision_id == executed.output_revision_id
    assert calls == [{"wiki_page_id": wiki.id}]  # manual_rebuild 只调度 wiki 图谱



# ---------------------------------------------------------------------------
# 内容过短 → not_worthy：run 成功但无产品写
# ---------------------------------------------------------------------------


def test_e2e_short_content_not_worthy_no_write(db, wiki_pipeline):
    ws_id, page = _mk_page(db, title="短", content="短")

    def _graph_runner(**kw):
        raise AssertionError("not_worthy 不调度图谱")

    executor.configure_external_runners(llm_runner=_mk_llm_runner(), graph_runner=_graph_runner)

    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        trigger_type="page_changed",
        trigger_object_id=page.id,
        workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded"
    statuses = _stage_statuses(db, executed.id)
    assert statuses["resolve_context"] == "succeeded"
    assert statuses["topic_route"] == "succeeded"
    assert statuses["publish_default"] == "succeeded"
    assert executed.output_revision_id is None

    assert db.query(WikiPage).filter(WikiPage.title == "水箱维护流程").count() == 0
    assert db.query(WikiPage).count() == 0  # 无任何产品写
    fresh = db.get(Page, page.id)
    assert fresh.wiki_dirty is False  # not_worthy 清 dirty（镜像 _finalize_page）


# ---------------------------------------------------------------------------
# 版本化流水线注册后 exact-version create_run 固化
# ---------------------------------------------------------------------------


def test_create_run_freezes_pipeline_version(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        trigger_type="page_changed",
        trigger_object_id=page.id,
        workspace_id=ws_id,
    )
    db.commit()
    assert run.pipeline_version == PIPELINE_VERSION
    assert run.workspace_id == ws_id
