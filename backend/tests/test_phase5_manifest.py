"""Phase 5.1：wiki.default Publish Manifest / finalize_compile_outcome / 持久化 graph 调度。

W1 交付。聚焦 wiki_default.py 单文件改动，仅用 fake runner（无 HTTP / 无真实 LLM /
无真实 DB），DB 一律独立临时文件 SQLite（WAL，便于双连接模拟并发写入）：

- publish_default 成功/失败均产 wiki_publish_manifest Artifact（outcome/fail_code/
  retryable/wiki_page_ids/revision_ids/archived|dirty_wiki_ids/graph_targets/input_hash）。
- finalize_compile_outcome 使 Run 真实反映编译结果：LLM 不可用 / 非法响应 / stale /
  partial synthesis → run failed（对应 fail_code）；不再虚假 succeeded。
- schedule_graph 读持久化 manifest 同步真实重建：graph 失败 run failed、Revision 不回滚、
  retry 重放不重复发布 Revision 且 graph runner 再次被调用。
- resolve_context 无 binding → fail closed（WORKSPACE_MISMATCH），不创建 binding/workspace。

fixture/helper 复用 test_phase5_pipeline_core 的 db/wiki_pipeline/_mk_page/_mk_llm_runner
模式（本文件自带副本，避免跨文件耦合）。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3.wiki_page_builder import LLMServiceUnavailable
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline import registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    ARTIFACT_SCHEMA_WIKI_PUBLISH,
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    MANIFEST_OBJECT_TYPE,
    PIPELINE_KEY,
    PIPELINE_VERSION,
    STAGE_KEYS,
    _classify_publish_note,
    _manifest_retryable,
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.core.wiki_pipeline.pipelines.dto import (
    FAIL_INVALID_RESPONSE,
    FAIL_PARTIAL_SYNTHESIS,
    FAIL_SERVICE_UNAVAILABLE,
    FAIL_STALE_INPUT,
    FAIL_WORKSPACE_MISMATCH,
    OUTCOME_ARCHIVED,
    OUTCOME_KEEP_DIRTY,
    OUTCOME_NOT_APPLICABLE,
    OUTCOME_NOT_WORTHY,
    OUTCOME_NOOP,
    OUTCOME_PUBLISHED,
)
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiRevision,
    WikiWorkspace,
    init_db,
)

CREATE_OPS = [{"action": "create", "title": "水箱维护流程", "category": "操作指南"}]
SYN_BODY = "水箱维护需要每日检查水位与温度传感器，并记录运行日志。"


# ---------------------------------------------------------------------------
# fixtures（antitheses 同款：临时文件 SQLite + WAL，支持双连接并发）
# ---------------------------------------------------------------------------


def _new_engine(url: str):
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        try:
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
        except Exception:  # noqa: BLE001  (in-memory 不支持 WAL)
            pass
        dbapi_conn.execute("PRAGMA busy_timeout=10000")

    init_db(engine)
    return engine


@pytest.fixture()
def db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'p51-manifest.db').as_posix()}"
    engine = _new_engine(url)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _phase5_manifest_isolation():
    """每个测试前后清理注册表 + 外部 runner（防跨测试泄漏）。"""
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    registry.REGISTRY.clear()
    executor.reset_external_runners()


@pytest.fixture()
def wiki_pipeline():
    register_default_pipeline()
    yield PIPELINE_KEY
    unregister_default_pipeline()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mk_page(db, *, page_id="p1", title="水箱维护", content="水箱固定内容足够长用于构建",
             notebook_id="nb-1", group_id="engineering"):
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


def _mk_llm_runner(*, ops=None, exc=None, ingest=None, synthesis=None):
    """同步 fake llm：ingest 返回 ops；synthesis 返回固定正文。

    exc: ingest 抛异常（模拟服务不可用）；ingest/synthesis: 显式返回值。
    """
    def _run(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            if exc is not None:
                raise exc
            if ingest is not None:
                return ingest
            return {"worthy": True, "ops": ops if ops is not None else CREATE_OPS}
        if synthesis is not None:
            return synthesis
        return {"summary": "水箱维护流程摘要", "content": SYN_BODY}
    return _run


def _graph_noop(**kw):
    return None


def _configure(llm, graph=None):
    executor.configure_external_runners(llm_runner=llm, graph_runner=graph or _graph_noop)


def _run_page(db, ws_id, page_id, *, trigger="page_changed"):
    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        trigger_type=trigger,
        trigger_object_id=page_id,
        workspace_id=ws_id,
    )
    db.commit()
    return run


def _execute(db, run):
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    return executed


def _stage_statuses(db, run_id):
    rows = db.query(StageRun).filter(StageRun.run_id == run_id).order_by(
        StageRun.stage_order, StageRun.attempt).all()
    return {r.stage_key: r.status for r in rows}


def _latest_manifest(db, run_id):
    art = (
        db.query(Artifact)
        .filter(
            Artifact.run_id == run_id,
            Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
        )
        .order_by(Artifact.created_at.desc())
        .first()
    )
    if art is None or not art.payload_json:
        return None
    return json.loads(art.payload_json)


def _wiki(db, title="水箱维护流程"):
    return db.query(WikiPage).filter(WikiPage.title == title).first()


def _revision_count(db, wiki):
    return db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count()


# ---------------------------------------------------------------------------
# 0. pipeline 定义 shape：finalize 已插入 publish_default 与 schedule_graph 之间
# ---------------------------------------------------------------------------


def test_pipeline_inserts_finalize_compile_outcome_stage(wiki_pipeline):
    pipe = registry.get_pipeline(PIPELINE_KEY)
    assert pipe is not None
    assert pipe.version == PIPELINE_VERSION
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
    assert flags["finalize_compile_outcome"] == (False, False, False)
    assert flags["publish_default"] == (False, True, False)
    assert flags["schedule_graph"] == (True, False, False)


def test_classify_publish_note_table():
    """manifest 判定表（_classify_publish_note）直接单测，锁语义防回归。"""
    cases = {
        "published": (OUTCOME_PUBLISHED, None),
        "rebuild_published": (OUTCOME_PUBLISHED, None),
        "partial_synthesis": (OUTCOME_KEEP_DIRTY, FAIL_PARTIAL_SYNTHESIS),
        "decision_service_unavailable_keep_dirty": (OUTCOME_KEEP_DIRTY, FAIL_SERVICE_UNAVAILABLE),
        "decision_invalid_response_keep_dirty": (OUTCOME_KEEP_DIRTY, FAIL_INVALID_RESPONSE),
        "rebuild_keep_dirty:service_unavailable": (OUTCOME_KEEP_DIRTY, FAIL_SERVICE_UNAVAILABLE),
        "rebuild_keep_dirty:invalid_response": (OUTCOME_KEEP_DIRTY, FAIL_INVALID_RESPONSE),
        "input_changed_during_llm": (OUTCOME_KEEP_DIRTY, FAIL_STALE_INPUT),
        "input_changed_during_synthesis": (OUTCOME_KEEP_DIRTY, FAIL_STALE_INPUT),
        "rebuild_stale_keep_dirty": (OUTCOME_KEEP_DIRTY, FAIL_STALE_INPUT),
        "no_workspace_binding": (OUTCOME_KEEP_DIRTY, FAIL_WORKSPACE_MISMATCH),
        "workspace_mismatch": (OUTCOME_KEEP_DIRTY, FAIL_WORKSPACE_MISMATCH),
        "no_scope": (OUTCOME_KEEP_DIRTY, FAIL_WORKSPACE_MISMATCH),
        "scope_changed_during_llm": (OUTCOME_KEEP_DIRTY, FAIL_WORKSPACE_MISMATCH),
        "unknown_scope": (OUTCOME_KEEP_DIRTY, FAIL_WORKSPACE_MISMATCH),
        "wiki_no_workspace_archived": (OUTCOME_ARCHIVED, None),
        "wiki_no_pages_archived": (OUTCOME_ARCHIVED, None),
        "rebuild_scope_mismatch_archived": (OUTCOME_ARCHIVED, None),
        "not_worthy_cleanup": (OUTCOME_NOT_WORTHY, None),
        "already_published_skip_republish": (OUTCOME_NOOP, None),
        "page_not_found": (OUTCOME_NOT_APPLICABLE, None),
    }
    for note, (exp_outcome, exp_fail) in cases.items():
        outcome, fail_code = _classify_publish_note(note, {})
        assert (outcome, fail_code) == (exp_outcome, exp_fail), note
    # page_deleted 语义：唯一来源 archived → archived；多来源移除（kept_dirty>0）→ noop
    archived_outcome, _ = _classify_publish_note("page_deleted_remove_source", {"archived": 1})
    assert archived_outcome == OUTCOME_ARCHIVED
    noop_outcome, _ = _classify_publish_note("page_deleted_remove_source", {"archived": 0})
    assert noop_outcome == OUTCOME_NOOP

    assert _manifest_retryable(FAIL_SERVICE_UNAVAILABLE) is True
    assert _manifest_retryable(FAIL_INVALID_RESPONSE) is True
    assert _manifest_retryable(FAIL_STALE_INPUT) is True
    assert _manifest_retryable(FAIL_PARTIAL_SYNTHESIS) is True
    assert _manifest_retryable(FAIL_WORKSPACE_MISMATCH) is False


# ---------------------------------------------------------------------------
# 1. publish 成功写 wiki_publish_manifest Artifact
# ---------------------------------------------------------------------------


def test_publish_success_writes_manifest_artifact(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    _configure(_mk_llm_runner())
    run = _execute(db, _run_page(db, ws_id, page.id))
    assert run.status == "succeeded"
    assert run.output_revision_id
    assert _stage_statuses(db, run.id) == {k: "succeeded" for k in STAGE_KEYS}

    wiki = _wiki(db)
    assert wiki is not None and wiki.status == "published"

    art = (
        db.query(Artifact)
        .filter(Artifact.run_id == run.id)
        .order_by(Artifact.created_at.desc())
        .first()
    )
    assert art is not None
    assert art.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST
    assert art.schema_version == ARTIFACT_SCHEMA_WIKI_PUBLISH
    assert art.object_type == MANIFEST_OBJECT_TYPE
    assert art.object_id == page.id

    manifest = _latest_manifest(db, run.id)
    assert manifest is not None
    assert manifest["outcome"] == OUTCOME_PUBLISHED
    assert manifest["fail_code"] is None
    assert manifest["page_id"] == page.id
    assert manifest["wiki_page_ids"] == [wiki.id]
    assert manifest["revision_ids"] == [run.output_revision_id]
    assert manifest["archived_wiki_ids"] == []
    assert manifest["dirty_wiki_ids"] == []
    assert manifest["input_hash"] == _expected_input_hash(db, page)
    assert manifest["graph_targets"] == [
        {"kind": "wiki", "wiki_page_id": wiki.id},
        {"kind": "page", "page_id": page.id},
    ]


# ---------------------------------------------------------------------------
# 2. LLM 不可用 → finalize SERVICE_UNAVAILABLE → run failed + retryable +
#    Page dirty + 不覆盖已发布 Revision
# ---------------------------------------------------------------------------


def test_llm_unavailable_finalize_failed_retryable(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    _configure(_mk_llm_runner())
    r1 = _execute(db, _run_page(db, ws_id, page.id))
    assert r1.status == "succeeded"
    db.expire_all()
    wiki = _wiki(db)
    old_rev = wiki.current_revision_id
    assert db.get(Page, page.id).wiki_dirty is False

    # 内容变化 + LLM 服务不可用
    fresh = db.get(Page, page.id)
    fresh.content = "水箱维护升级：迁移到 PLC 控制系统，新增联锁逻辑与旁路要求。"
    db.commit()
    _configure(_mk_llm_runner(exc=LLMServiceUnavailable("llm down")))

    r2 = _execute(db, _run_page(db, ws_id, page.id))
    assert r2.status == "failed", "finalize 应使 LLM 失败真实反映为 run failed"
    assert r2.output_revision_id is None
    assert r2.safe_error_code == FAIL_SERVICE_UNAVAILABLE
    assert _stage_statuses(db, r2.id)["finalize_compile_outcome"] == "failed"
    assert _stage_statuses(db, r2.id)["schedule_graph"] == "skipped"

    manifest = _latest_manifest(db, r2.id)
    assert manifest["outcome"] == OUTCOME_KEEP_DIRTY
    assert manifest["fail_code"] == FAIL_SERVICE_UNAVAILABLE
    assert manifest["retryable"] is True
    assert manifest["graph_targets"] == []

    db.expire_all()
    wiki = _wiki(db)
    assert wiki.current_revision_id == old_rev, "LLM 不可用不得覆盖已发布 Revision"
    assert _revision_count(db, wiki) == 1
    page_row = db.get(Page, page.id)
    assert page_row.wiki_dirty is True
    assert page_row.wiki_last_error == "service_unavailable"

    # retryable 语义：恢复 LLM 后 retry 同一 run → succeeded
    _configure(_mk_llm_runner())
    executor.retry_run(db, r2.id)
    db.commit()
    r3 = _execute(db, r2)
    assert r3.status == "succeeded"


# ---------------------------------------------------------------------------
# 3. invalid JSON（worthy=true + ops=[] → invalid_response）→ run failed
# ---------------------------------------------------------------------------


def test_invalid_response_finalize_failed(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    _configure(_mk_llm_runner())
    r1 = _execute(db, _run_page(db, ws_id, page.id))
    assert r1.status == "succeeded"
    db.expire_all()
    wiki = _wiki(db)
    old_rev = wiki.current_revision_id

    fresh = db.get(Page, page.id)
    fresh.content = "水箱维护改造：新增水位传感器更换要求，涉及停机窗口与备件清单。"
    db.commit()
    # worthy=True + ops=[] → _llm_outcome invalid_response（决策 status=invalid_response）
    _configure(_mk_llm_runner(ingest={"worthy": True, "ops": []}))

    r2 = _execute(db, _run_page(db, ws_id, page.id))
    assert r2.status == "failed"
    assert r2.output_revision_id is None
    assert r2.safe_error_code == FAIL_INVALID_RESPONSE

    manifest = _latest_manifest(db, r2.id)
    assert manifest["fail_code"] == FAIL_INVALID_RESPONSE
    assert manifest["retryable"] is True

    db.expire_all()
    wiki = _wiki(db)
    assert wiki.current_revision_id == old_rev
    assert _revision_count(db, wiki) == 1
    p = db.get(Page, page.id)
    assert p.wiki_dirty is True
    assert p.wiki_last_error == "invalid_response"


# ---------------------------------------------------------------------------
# 4. stale input（LLM 期间并发改 Page）→ run failed + STALE_INPUT
# ---------------------------------------------------------------------------


def test_stale_input_finalize_failed(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    Session = sessionmaker(bind=db.get_bind())

    def _mutating_ingest(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            s = Session()
            try:
                p = s.get(Page, page.id)
                p.content = "水箱维护内容在识别期间被并发修改，长度足以绕过短内容判定分支。"
                s.commit()
            finally:
                s.close()
        return {"worthy": True, "ops": CREATE_OPS}

    _configure(_mutating_ingest)
    run = _execute(db, _run_page(db, ws_id, page.id))
    assert run.status == "failed"
    assert run.output_revision_id is None
    assert run.safe_error_code == FAIL_STALE_INPUT
    assert _stage_statuses(db, run.id)["finalize_compile_outcome"] == "failed"

    manifest = _latest_manifest(db, run.id)
    assert manifest["fail_code"] == FAIL_STALE_INPUT
    assert manifest["retryable"] is True

    db.expire_all()
    assert db.query(WikiPage).count() == 0, "stale 输入不得落库 wiki/revision"
    p = db.get(Page, page.id)
    assert p.wiki_dirty is True
    assert p.wiki_last_error == "input_changed_during_llm"


# ---------------------------------------------------------------------------
# 5. partial synthesis（两目标：一成功一失败）→ run failed + PARTIAL_SYNTHESIS
# ---------------------------------------------------------------------------


def test_partial_synthesis_finalize_failed(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    two_ops = [
        {"action": "create", "title": "水箱日常点检", "category": "操作指南"},
        {"action": "create", "title": "PLC 联锁校验", "category": "操作指南"},
    ]

    def _partial_llm(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": two_ops}
        # 第一个目标合成成功，第二个目标 LLM 不可用 → partial
        if context == "wiki-synthesis":
            if _partial_llm.syn_calls == 0:
                _partial_llm.syn_calls += 1
                return {"summary": "日常点检摘要", "content": "每班次执行水箱外观与液位点检并记录。"}
            raise LLMServiceUnavailable("synthesis down for second target")
        return {"summary": "", "content": SYN_BODY}

    _partial_llm.syn_calls = 0
    _configure(_partial_llm)

    run = _execute(db, _run_page(db, ws_id, page.id))
    assert run.status == "failed"
    assert run.safe_error_code == FAIL_PARTIAL_SYNTHESIS
    assert _stage_statuses(db, run.id)["finalize_compile_outcome"] == "failed"

    manifest = _latest_manifest(db, run.id)
    assert manifest["outcome"] == OUTCOME_KEEP_DIRTY
    assert manifest["fail_code"] == FAIL_PARTIAL_SYNTHESIS
    assert manifest["retryable"] is True
    assert manifest["dirty_wiki_ids"]  # 失败目标被记录为 dirty wiki

    db.expire_all()
    assert db.query(WikiPage).count() == 2  # 两个主题均被识别创建
    assert db.query(WikiRevision).count() == 1  # 仅第一个目标发布 Revision
    p = db.get(Page, page.id)
    assert p.wiki_dirty is True


# ---------------------------------------------------------------------------
# 6. graph runner 抛异常 → GRAPH_BUILD_FAILED，Revision 不回滚；retry 重放不重复发布
# ---------------------------------------------------------------------------


def test_graph_failure_failed_run_retry_rebuilds_without_dup_revision(db, wiki_pipeline):
    ws_id, page = _mk_page(db)

    def _raise_graph(**kw):
        raise RuntimeError("graph backend down")

    _configure(_mk_llm_runner(), graph=_raise_graph)
    r1 = _execute(db, _run_page(db, ws_id, page.id))
    assert r1.status == "failed"  # publish 已生效，仅 schedule_graph 失败
    assert r1.safe_error_code == "GRAPH_BUILD_FAILED"
    assert r1.output_revision_id  # Revision 已发布，不回滚

    db.expire_all()
    wiki = _wiki(db)
    assert wiki is not None and wiki.status == "published"
    first_rev = wiki.current_revision_id
    assert _revision_count(db, wiki) == 1

    # 失败前 publish 已持久化 manifest（含 graph_targets），供 retry 恢复
    manifest = _latest_manifest(db, r1.id)
    assert manifest is not None
    assert manifest["outcome"] == OUTCOME_PUBLISHED
    assert manifest["graph_targets"] == [
        {"kind": "wiki", "wiki_page_id": wiki.id},
        {"kind": "page", "page_id": page.id},
    ]

    # 图谱恢复 → retry 同一 run：publish 幂等守卫 no-op，schedule_graph 读 manifest
    # 真实重建全部目标，且不重复 append Revision
    calls: list[dict] = []
    _configure(_mk_llm_runner(), graph=lambda **kw: calls.append(kw))
    executor.retry_run(db, r1.id)
    db.commit()
    r2 = _execute(db, r1)
    assert r2.status == "succeeded"
    assert _stage_statuses(db, r2.id)["publish_default"] == "succeeded"
    assert _stage_statuses(db, r2.id)["finalize_compile_outcome"] == "succeeded"

    assert calls == [{"wiki_page_id": wiki.id}, {"page_id": page.id}], \
        "retry 必须从持久化 manifest 恢复全部 graph 目标"
    db.expire_all()
    wiki = _wiki(db)
    assert _revision_count(db, wiki) == 1, "retry 不得重复发布 Revision"
    assert wiki.current_revision_id == first_rev


# ---------------------------------------------------------------------------
# 7. resolve_context 无 binding → fail closed，不创建 binding/workspace
# ---------------------------------------------------------------------------


def test_resolve_context_no_binding_fail_closed_no_new_rows(db, wiki_pipeline):
    ws_id, page = _mk_page(db)
    nb_before = db.query(NotebookWorkspaceBinding).count()
    ws_before = db.query(WikiWorkspace).count()
    assert nb_before >= 1

    # 先在有 binding 时建 run（executor 创建期校验通过），再移除 binding → 执行期 fail closed
    run = _run_page(db, ws_id, page.id)
    db.query(NotebookWorkspaceBinding).delete()
    db.commit()

    executed = _execute(db, run)
    assert executed.status == "failed"
    assert executed.safe_error_code == FAIL_WORKSPACE_MISMATCH
    assert _stage_statuses(db, executed.id)["finalize_compile_outcome"] == "failed"

    # resolve_context 不自动创建 binding/workspace（无副作用）
    assert db.query(NotebookWorkspaceBinding).count() == 0
    assert db.query(WikiWorkspace).count() == ws_before

    manifest = _latest_manifest(db, run.id)
    assert manifest["fail_code"] == FAIL_WORKSPACE_MISMATCH
    assert manifest["retryable"] is False
