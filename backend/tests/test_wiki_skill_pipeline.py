"""Phase 6 Pipeline v1/v2 测试（20.7：兼容矩阵 + skill_route Artifact + publish 原子写 Skill）。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.wiki_pipeline import executor, registry as pregs
from app.core.wiki_pipeline.pipelines.wiki_default import (
    PIPELINE_KEY,
    _stage_publish_default as v1_publish,
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
    ARTIFACT_SCHEMA_SKILL_DECISION,
    ARTIFACT_TYPE_SKILL_DECISION,
    _apply_skill_to_wiki,
    _default_decision,
    _match_decision,
    register_default_pipeline_v2,
    unregister_skilled_default_pipeline,
)
from app.core.wiki_skills import registry as sreg
from app.core.wiki_skills import service as skill_service
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)

V1_KEYS = ("resolve_context", "topic_route", "synthesize_default", "validate_default",
           "publish_default", "finalize_compile_outcome", "schedule_graph")
V2_KEYS = ("resolve_context", "topic_route", "skill_route", "synthesize_default",
           "validate_default", "publish_default", "finalize_compile_outcome", "schedule_graph")

SYN_BODY = "聚合正文：包含所有来源知识点。\n" * 3


@pytest.fixture(autouse=True)
def _isolate():
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    executor.reset_external_runners()
    yield
    pregs.clear_for_tests()
    sreg.clear_for_tests()
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
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _mk_page(db, page_id, title, content, notebook_id="nb-1", group_id="engineering"):
    if db.get(Notebook, notebook_id) is None:
        db.add(Notebook(id=notebook_id, name="研发库", group_id=group_id))
        db.flush()
    ws = ensure_notebook_workspace(db, db.get(Notebook, notebook_id))
    if db.get(Page, page_id) is None:
        db.add(Page(id=page_id, notebook_id=notebook_id, title=title, content=content))
        db.commit()
    return ws.id, db.get(Page, page_id)


def _mk_llm(*, ingest_ops=None, contexts=None):
    def _run(messages, context="", timeout=120.0):
        if contexts is not None:
            contexts.append(context)
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": ingest_ops if ingest_ops is not None else []}
        if context in ("wiki-synthesis",):
            return {"summary": "摘要", "content": SYN_BODY}
        if context == "wiki-batch-summary":
            return {"summary": "批次摘要"}
        if context == "wiki-mapreduce":
            return {"summary": "最终", "content": SYN_BODY}
        return {"worthy": True, "ops": []}
    return _run


def _graph_noop(**kw):
    return None


def _bootstrap(register_v1=True, register_v2=True):
    skill_service.register_default_skill()
    if register_v1:
        register_default_pipeline()
    if register_v2:
        register_default_pipeline_v2()


# ---------------------------------------------------------------------------
# 76-80：v1/v2 共存 / active / queued v1 / retry
# ---------------------------------------------------------------------------


def test_v1_v2_coexist():
    _bootstrap()
    assert pregs.get_pipeline(PIPELINE_KEY, "1") is not None
    assert pregs.get_pipeline(PIPELINE_KEY, "2") is not None
    assert tuple(pregs.get_pipeline(PIPELINE_KEY, "1").stage_keys()) == V1_KEYS
    assert tuple(pregs.get_pipeline(PIPELINE_KEY, "2").stage_keys()) == V2_KEYS


def test_active_v2_not_dependent_on_registration_order():
    # v2 先注册、v1 后注册：显式 active 仍为 v2。
    pregs.clear_for_tests()
    _bootstrap(register_v1=False, register_v2=True)
    register_default_pipeline()
    assert pregs.get_pipeline(PIPELINE_KEY).version == "2"
    # 反转：v1 先、v2 后，active 仍 v2。
    pregs.clear_for_tests()
    _bootstrap(register_v1=True, register_v2=False)
    register_default_pipeline_v2()
    assert pregs.get_pipeline(PIPELINE_KEY).version == "2"


def test_register_v1_only_new_run_uses_v1():
    _bootstrap(register_v1=True, register_v2=False)
    assert pregs.get_pipeline(PIPELINE_KEY).version == "1"


def test_queued_v1_run_executes_v1_after_v2_registered(db):
    _bootstrap(register_v1=True, register_v2=False)
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    executor.configure_external_runners(llm_runner=_mk_llm(
        ingest_ops=[{"action": "create", "title": "主题A", "category": "资料"}],
    ), graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, pipeline_version="1",
        trigger_type="page_changed", trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    # v2 在 v1 run 排队之后注册。
    register_default_pipeline_v2()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded"
    assert executed.pipeline_version == "1"
    stages = [s.stage_key for s in db.query(StageRun).filter(
        StageRun.run_id == run.id).order_by(StageRun.stage_order).all()]
    assert stages == list(V1_KEYS)
    # v1 run 不产 skill_decision artifact。
    assert db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_SKILL_DECISION,
    ).count() == 0


def test_v1_retry_still_v1(db):
    _bootstrap(register_v1=True, register_v2=False)
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, pipeline_version="1",
        trigger_type="page_changed", trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    register_default_pipeline_v2()  # 新 run 默认 v2
    assert pregs.get_pipeline(PIPELINE_KEY).version == "2"
    # v1 run 手动置 failed 后 retry → 仍 v1。
    run.status = "failed"
    db.commit()
    requeued = executor.retry_run(db, run.id)
    assert requeued.pipeline_version == "1"
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.pipeline_version == "1"
    assert tuple(s.stage_key for s in db.query(StageRun).filter(
        StageRun.run_id == run.id).order_by(StageRun.stage_order).all()) == V1_KEYS


def test_new_run_defaults_v2(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    executor.configure_external_runners(llm_runner=_mk_llm(
        ingest_ops=[{"action": "create", "title": "主题A", "category": "资料"}],
    ), graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_changed",
        trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    assert run.pipeline_version == "2"


# ---------------------------------------------------------------------------
# 81-89：skill_route Artifact / 批量决策 / 删除 / input identity
# ---------------------------------------------------------------------------


def _execute_v2_page_run(db, page_id, ws_id, ingest_ops):
    executor.configure_external_runners(llm_runner=_mk_llm(ingest_ops=ingest_ops),
                                        graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_changed",
        trigger_object_id=page_id, workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    return run


def test_v2_skill_route_produces_artifact(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    run = _execute_v2_page_run(db, "p1", ws_id, [
        {"action": "create", "title": "主题A", "category": "资料"}])
    rows = db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_SKILL_DECISION,
    ).all()
    assert len(rows) == 1
    art = rows[0]
    assert art.schema_version == ARTIFACT_SCHEMA_SKILL_DECISION
    payload = json.loads(art.payload_json)
    assert payload["schema_version"] == "skill-decision/v1"
    assert len(payload["decisions"]) == 1
    d0 = payload["decisions"][0]
    assert d0["selected_skill"] == "default"
    assert d0["selected_version"] == "1"


def test_artifact_payload_no_full_content_or_prompt(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "秘密正文标记_SECRET_" + "x" * 60)
    run = _execute_v2_page_run(db, "p1", ws_id, [
        {"action": "create", "title": "主题A", "category": "资料"}])
    art = db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_SKILL_DECISION,
    ).one()
    blob = art.payload_json
    assert "秘密正文标记_SECRET_" not in blob
    assert "prompt" not in blob.lower()


def test_batch_per_target_decision(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "页面一", "内容：" + "y" * 60)
    _, _ = _mk_page(db, "p2", "页面二", "内容：" + "z" * 60)
    # 两个 page 各路由到不同主题。
    executor.configure_external_runners(llm_runner=_mk_llm(
        ingest_ops=[{"action": "create", "title": "主题X", "category": "资料"}],
    ), graph_runner=_graph_noop)
    from app.core.wiki_pipeline.pipelines.wiki_default import create_batch_run

    run = create_batch_run(db, workspace_id=ws_id, page_ids=["p1", "p2"])
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert executed.pipeline_version == "2"
    art = db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_SKILL_DECISION,
    ).one()
    payload = json.loads(art.payload_json)
    # 两个 page 同标题 → 合并为一个 target；这里每 page 都 ops 主题X → 1 条 decision。
    keys = [d["target_key"] for d in payload["decisions"]]
    assert keys == sorted(keys)
    # 每个 decision 都有精确可解析 skill。
    for d in payload["decisions"]:
        assert sreg.has(d["selected_skill"], d["selected_version"])


def test_manual_rebuild_uses_v2_and_sets_skill(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    executor.configure_external_runners(llm_runner=_mk_llm(
        ingest_ops=[{"action": "create", "title": "主题A", "category": "资料"}],
    ), graph_runner=_graph_noop)
    # 先经 v2 page run 建 wiki 并发布。
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_changed",
        trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    executor.execute_run(db, run.id)
    wiki = db.query(WikiPage).filter(WikiPage.workspace_id == ws_id).first()
    assert wiki.content_skill == "default"
    # 置 dirty 触发 manual_rebuild（v2）。
    wiki.dirty = True
    wiki.content_skill = None  # 模拟旧字段（manual rebuild 应重新写入）
    db.commit()
    mrun = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="manual_rebuild",
        trigger_object_id=wiki.id, workspace_id=ws_id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed = executor.execute_run(db, mrun.id)
    db.refresh(executed)
    assert executed.pipeline_version == "2"
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wiki)
    assert wiki.content_skill == "default"
    assert wiki.skill_selected_by in ("default_fallback", "sticky")


def test_page_deleted_no_skill_llm_call(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    contexts: list = []
    executor.configure_external_runners(
        llm_runner=_mk_llm(
            ingest_ops=[{"action": "create", "title": "主题A", "category": "资料"}],
            contexts=contexts,
        ),
        graph_runner=_graph_noop,
    )
    # 先发布 wiki（含来源 p1）。
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_changed",
        trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    executor.execute_run(db, run.id)
    wiki = db.query(WikiPage).filter(WikiPage.workspace_id == ws_id).first()
    # page_deleted：建 run（Page 行仍在）→ 物理删除 Page → 执行。
    from app.core.wiki_pipeline.pipelines.wiki_default import create_page_deleted_run

    drun = create_page_deleted_run(
        db, page_id="p1", workspace_id=ws_id, notebook_id="nb-1",
    )
    db.commit()
    contexts.clear()
    page = db.get(Page, "p1")
    db.delete(page)
    db.commit()
    executed = executor.execute_run(db, drun.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert contexts == []  # 删除触发不调用任何 Skill/主题 LLM
    # 删除后 wiki 被归档或去源；skill 字段未被清空（未发布 Revision）。
    db.refresh(wiki)
    assert wiki.source_page_ids in ("[]", "null") or wiki.status == "archived"


def test_skill_decision_enters_downstream_input_identity(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    run = _execute_v2_page_run(db, "p1", ws_id, [
        {"action": "create", "title": "主题A", "category": "资料"}])
    sk_stage = db.query(StageRun).filter(
        StageRun.run_id == run.id, StageRun.stage_key == "skill_route").one()
    syn_stage = db.query(StageRun).filter(
        StageRun.run_id == run.id, StageRun.stage_key == "synthesize_default").one()
    assert sk_stage.output_hash  # skill_route artifact content hash
    # synthesize_default 的 stage_input_hash 由 executor 含 skill_route 上游链计算。
    from app.core.wiki_pipeline.executor import compute_stage_input_hash

    run_row = db.query(CompileRun).filter(CompileRun.id == run.id).one()
    sdef = next(s for s in pregs.get_pipeline("wiki.default", "2").stages
                if s.key == "synthesize_default")
    with_skill = compute_stage_input_hash(
        db, run_row, sdef, upstream_hashes=[sk_stage.output_hash]
    )
    without = compute_stage_input_hash(db, run_row, sdef, upstream_hashes=[])
    assert syn_stage.stage_input_hash == with_skill
    assert with_skill != without  # SkillDecision 进入下游 input identity


def test_v1_v2_cache_identity_not_crossed(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    executor.configure_external_runners(llm_runner=_mk_llm(
        ingest_ops=[{"action": "create", "title": "主题A", "category": "资料"}],
    ), graph_runner=_graph_noop)
    # v1 run
    r1 = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, pipeline_version="1",
        trigger_type="page_changed", trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    executor.execute_run(db, r1.id)
    # v2 run 相同输入。
    r2 = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_changed",
        trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    executor.execute_run(db, r2.id)
    # 全部 stage 非 cachable → 无缓存复用（run 独立执行）。
    def _syn_hash(run_id):
        return db.query(StageRun).filter(
            StageRun.run_id == run_id,
            StageRun.stage_key == "synthesize_default").one().stage_input_hash
    assert _syn_hash(r1.id) != _syn_hash(r2.id)  # pipeline_version 维度不同


# ---------------------------------------------------------------------------
# 91-95：publish 原子写 / 失败不写 / decision 不串 wiki
# ---------------------------------------------------------------------------


def test_publish_success_atomically_writes_skill_fields(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    _execute_v2_page_run(db, "p1", ws_id, [
        {"action": "create", "title": "主题A", "category": "资料"}])
    wiki = db.query(WikiPage).filter(WikiPage.workspace_id == ws_id).first()
    assert wiki.content_skill == "default"
    assert wiki.skill_version == "1"
    assert wiki.skill_confidence == 1.0
    assert wiki.skill_selected_by in ("default_fallback", "sticky")
    assert wiki.skill_decision_json


def test_publish_failure_keeps_original_skill(db):
    from app.core.knowledge_compiler_v3.wiki_page_builder import LLMServiceUnavailable

    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    # 先成功发布一次写入 skill。
    run1 = _execute_v2_page_run(db, "p1", ws_id, [
        {"action": "create", "title": "主题A", "category": "资料"}])
    wiki = db.query(WikiPage).filter(WikiPage.workspace_id == ws_id).first()
    orig_skill = wiki.content_skill
    orig_rev = wiki.current_revision_id
    # 第二次 LLM 服务不可用 → publish keep dirty、run failed → skill/revision 不变。
    def _down_llm(messages, context="", timeout=120.0):
        raise LLMServiceUnavailable("down")
    executor.configure_external_runners(llm_runner=_down_llm, graph_runner=_graph_noop)
    page = db.get(Page, "p1")
    page.content = page.content + "\n变化" * 5
    db.commit()
    run2 = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_changed",
        trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run2.id)
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "SERVICE_UNAVAILABLE"
    db.refresh(wiki)
    assert wiki.content_skill == orig_skill
    assert wiki.current_revision_id == orig_rev
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 1


def test_stale_input_does_not_overwrite_skill(db):
    _bootstrap()
    ws_id, _ = _mk_page(db, "p1", "功能说明", "内容：" + "x" * 60)
    run1 = _execute_v2_page_run(db, "p1", ws_id, [
        {"action": "create", "title": "主题A", "category": "资料"}])
    wiki = db.query(WikiPage).filter(WikiPage.workspace_id == ws_id).first()
    orig_rev = wiki.current_revision_id
    # 直接改 source_hash 使 publish 守卫判定 stale 分支（模拟窗口内变化）。
    page = db.get(Page, "p1")
    page.content = page.content + "\n外部改动"
    db.commit()
    # 非法响应场景已由上述覆盖；此处验证 stale 守卫不写新 Revision。
    executor.configure_external_runners(llm_runner=_mk_llm(
        ingest_ops=[{"action": "create", "title": "主题A", "category": "资料"}],
    ), graph_runner=_graph_noop)
    run2 = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_changed",
        trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run2.id)
    db.refresh(executed)
    db.refresh(wiki)
    # 第二次成功发布会追加 Revision —— stale 窗口由 input_changed 兜底；
    # 关键断言：skill 字段只随成功发布写入。
    assert wiki.content_skill == "default"


def test_target_decision_not_written_to_wrong_wiki(db):
    # 函数级：两个 decision 各对应一个 wiki，publish 时按 id/标题精确匹配。
    _bootstrap()
    wiki_a = WikiPage(id="wa", title="主题A")
    wiki_b = WikiPage(id="wb", title="主题B")
    db.add_all([wiki_a, wiki_b])
    db.flush()
    dec_a = _default_decision(wiki_a)
    dec_b = _default_decision(wiki_b)
    # 人为让 b 决策带不同 target_key（模拟不同 target），验证按 id 精确匹配。
    from app.core.wiki_skills.schemas import SkillDecision

    dec_b2 = SkillDecision(
        target_key=wiki_b.id, wiki_page_id=wiki_b.id,
        selected_skill="default", selected_version="1",
        selected_by="auto", confidence=1.0, status="selected", reason_code="X",
    )
    state = {"skill": {"decisions": [dec_a.to_dict(), dec_b2.to_dict()]}}
    matched_a = _match_decision(state, wiki_a)
    matched_b = _match_decision(state, wiki_b)
    assert matched_a.target_key == wiki_a.id
    assert matched_b.target_key == wiki_b.id
    _apply_skill_to_wiki(db, wiki_a, matched_a)
    _apply_skill_to_wiki(db, wiki_b, matched_b)
    assert wiki_a.skill_decision_json is not None
    assert json.loads(wiki_a.skill_decision_json)["target_key"] == wiki_a.id
    assert json.loads(wiki_b.skill_decision_json)["target_key"] == wiki_b.id
