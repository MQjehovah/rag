"""Phase 7C.3-B：单目标 Skill 迁移（default→api_reference）与 protected Section 保留。

覆盖：
1. migration_proposed 明确包含 proposed 字段；
2. 不再从 candidates 顺序猜目标；
3. 旧 migration Artifact 缺 proposed → v3 fail closed；
4. locked Wiki 不迁移；
5. proposed 版本未注册不迁移；
6. 不支持方向不迁移；
7. shadow compile 前零产品写 / 成功后原子切换；
8. shadow compile 失败保持旧 Revision/Skill/dirty；
9. 成功后 Revision/Skill/decision/Manifest 原子切换；
10. protected 与自动 key 不同 → 两者保留；
11. 相同 key → protected 胜出；
12. NULL key protected 全保留；
13. protected Binding 快照复制；
14. stale Evidence 的历史 protected Binding 仍保留；
15. 中途异常全部回滚；
16. lease 丢失不能切换；
17. order_index 连续；
18. 反转数据库/输入顺序结果一致；
19. 普通 API update 同样保留 protected；
20. default 分支原有保护行为无回归；
21. Manifest/structure_json 不泄露正文、excerpt、Prompt、Token、路径。
"""
from __future__ import annotations

import hashlib
import json
import threading

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 as v3mod
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline import registry as pregs
from app.core.wiki_pipeline.pipelines.wiki_default import register_default_pipeline
from app.core.wiki_pipeline.pipelines.wiki_skilled_default import register_default_pipeline_v2
from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import register_default_pipeline_v3
from app.core.wiki_skills import registry as sreg
from app.core.wiki_skills import service as skill_service
from app.core.wiki_skills.api_reference.runtime import ApiReferenceRuntime
from app.core.wiki_skills.builtin.default.runtime import DefaultSkillRuntime
from app.core.wiki_skills.schemas import SkillContext, SkillDecision, SkillDescriptor
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    EvidenceItem,
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    init_db,
)


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _openapi_spec(overview="GET /api/users 返回用户列表。返回 200 成功。"):
    return json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "用户 API", "version": "1.0", "description": overview},
        "paths": {"/api/users": {"get": {
            "summary": "用户列表", "description": "返回 200 成功。",
            "responses": {"200": {"description": "成功返回"}},
        }}},
    }, ensure_ascii=False)


def _graph_noop(**kw):
    return None


def _mk_llm(content="聚合正文：" + "x" * 60):
    def _run(messages, context="", timeout=120.0):
        if context in ("wiki-synthesis", "wiki-mapreduce"):
            return {"summary": "摘要", "content": content}
        return {"worthy": True, "ops": []}
    return _run


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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bootstrap_builtin():
    skill_service.register_builtin_skills()
    register_default_pipeline()
    register_default_pipeline_v2()
    register_default_pipeline_v3()


def _bootstrap_default():
    skill_service.register_default_skill()
    register_default_pipeline()
    register_default_pipeline_v2()
    register_default_pipeline_v3()


def _mk_ws(db, nb_id="nb-1"):
    if db.get(Notebook, nb_id) is None:
        db.add(Notebook(id=nb_id, name="研发库", group_id="eng"))
        db.flush()
    return ensure_notebook_workspace(db, db.get(Notebook, nb_id))


def _mk_page(db, ws, pid, spec=None, title="用户 API 文档"):
    spec = spec if spec is not None else _openapi_spec()
    from app.models.database import NotebookWorkspaceBinding

    binding = db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.workspace_id == ws.id).first()
    nb_id = binding.notebook_id if binding is not None else None
    db.add(Page(id=pid, notebook_id=nb_id, title=title, content=spec,
                content_hash=_hash(spec), wiki_dirty=True))
    db.flush()
    return db.get(Page, pid)


def _mk_evidence(db, page, eid, *, status="active", content_hash=None,
                 content="evidence-content", locator=None):
    db.add(EvidenceItem(
        id=eid, source_page_id=page.id, status=status, content=content,
        locator_json=json.dumps(locator or {"section": "all"}),
        content_hash=content_hash or ("f" * 64),
        source_doc_hash=page.content_hash,
    ))
    db.flush()
    return db.get(EvidenceItem, eid)


def _mk_wiki(db, ws, wid, title="用户 API", page_ids=None, *, dirty=True,
             content_skill=None, skill_version=None, skill_locked=False):
    wiki = WikiPage(
        id=wid, title=title, summary="", acl_scope=ws.acl_scope,
        status="draft", source_page_ids=json.dumps(list(page_ids or [])),
        dirty=dirty, locked=False, workspace_id=ws.id,
        content_skill=content_skill, skill_version=skill_version,
        skill_locked=skill_locked,
    )
    db.add(wiki)
    db.flush()
    return wiki


def _decision(target_key, wiki_page_id, *, skill="api_reference", version="1",
              status="selected", proposed_skill=None, proposed_version=None,
              candidates=None, locked=False, selected_by="auto",
              reason_code="TEST", previous_skill=None, previous_version=None,
              confidence=1.0):
    return SkillDecision(
        target_key=target_key, wiki_page_id=wiki_page_id,
        selected_skill=skill, selected_version=version,
        selected_by=selected_by, confidence=confidence, status=status,
        reason_code=reason_code,
        matched_signals=("api_path",),
        candidates=tuple(candidates or ()),
        previous_skill=previous_skill, previous_version=previous_version,
        locked=locked,
        proposed_skill=proposed_skill, proposed_version=proposed_version,
    )


def _write_decision_artifact(db, run_id, decisions, *, drop_proposed=False):
    raw_decisions = [d.to_dict() for d in decisions]
    if drop_proposed:
        for d in raw_decisions:
            d.pop("proposed_skill", None)
            d.pop("proposed_version", None)
    db.add(Artifact(
        run_id=run_id, artifact_type="skill_decision",
        schema_version="skill-decision/v1", object_type="skill_decision",
        payload_json=json.dumps(
            {"schema_version": "skill-decision/v1", "decisions": raw_decisions},
            ensure_ascii=False),
    ))
    db.flush()


def _new_queued_run(db, ws, wiki):
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    db.refresh(run)
    return run


def _manual_ctx(db, run, wiki, page_ids):
    return {"state": {
        "context": {
            "applicable": True, "reason": "", "trigger_type": "manual_rebuild",
            "wiki_page_id": wiki.id, "workspace_id": wiki.workspace_id,
            "scope_acl_json": wiki.acl_scope, "title": wiki.title or "",
            "input_hash": run.input_hash or _hash("input"),
            "source_page_ids": tuple(page_ids),
        },
        "topic": {"status": "rebuild_wiki", "ops": [], "note": ""},
        "publish": {},
    }}


def _synth(db, run, wiki, page_ids):
    ctx = _manual_ctx(db, run, wiki, page_ids)
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok"), res
    return ctx


def _revision_of(db, wiki):
    rev = db.get(WikiRevision, wiki.current_revision_id)
    return sorted(
        db.query(WikiSection).filter(WikiSection.revision_id == rev.id).all(),
        key=lambda s: s.order_index or 0)


def _new_rev_secs(db, wiki):
    return _revision_of(db, wiki)


def _bindings_for(db, section_id):
    return db.query(WikiSectionEvidenceBinding).filter(
        WikiSectionEvidenceBinding.section_id == section_id).all()


def _default_migration_candidates():
    return [
        {"skill_key": "default", "skill_version": "1",
         "deterministic_score": 0.2, "llm_score": 0.0, "combined_score": 0.2,
         "matched_signals": ["generic_text"]},
        {"skill_key": "api_reference", "skill_version": "1",
         "deterministic_score": 1.0, "llm_score": 0.0, "combined_score": 1.0,
         "matched_signals": ["api_path", "http_method"]},
    ]


# ---------------------------------------------------------------------------
# 1/2. Router 显式 proposed（不再猜 candidates 顺序）
# ---------------------------------------------------------------------------


def test_router_migration_proposal_explicit_fields(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    # 当前 default wiki + 强 API 内容 → migration_proposed 且显式 proposed。
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wiki)
    assert wiki.content_skill == "api_reference"
    assert wiki.skill_selected_by == "migration"
    dec = json.loads(wiki.skill_decision_json)
    assert dec["selected_skill"] == "api_reference"
    assert dec["selected_version"] == "1"
    assert dec["selected_by"] == "migration"
    assert dec["status"] == "selected"
    assert dec["reason_code"] == "MIGRATION_APPLIED"
    assert dec["previous_skill"] == "default"
    # 原 proposal 保留在 Artifact（status=migration_proposed + 显式 proposed）。
    proposal_art = None
    for art in db.query(Artifact).filter(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "skill_decision").all():
        payload = json.loads(art.payload_json)
        for d in payload.get("decisions") or []:
            if d.get("status") == "migration_proposed":
                proposal_art = d
    assert proposal_art is not None
    assert proposal_art["proposed_skill"] == "api_reference"
    assert proposal_art["proposed_version"] == "1"
    assert proposal_art["selected_skill"] == "default"


def test_router_proposal_stable_across_registration_order():
    def _descriptor(key, signals):
        return SkillDescriptor(
            key=key, version="1", label=key, description=key,
            applicability_signals=signals,
            extraction_schema_id="x", blueprint_schema_id="y",
            instruction_resource="i", runtime_key=key)

    def _route(reversed_order=False):
        sreg.clear_for_tests()
        sreg.register_runtime_allowlist("default", DefaultSkillRuntime)
        sreg.register_runtime_allowlist("api_reference", ApiReferenceRuntime)
        descs = [
            _descriptor("default", ["generic_text"]),
            _descriptor("api_reference", ["http_method", "api_path", "status_code",
                                          "error_code", "request_json", "response_json",
                                          "parameter_table"]),
        ]
        if reversed_order:
            descs.reverse()
        for d in descs:
            sreg.register_descriptor(d)
        sreg.set_active_version("default", "1")
        sreg.set_active_version("api_reference", "1")
        ctx = SkillContext(
            target_key="t1", wiki_page_id="wk", title="用户 API 文档",
            source_summaries=({"source_page_id": "p",
                               "summary": "GET /api/users 返回 200 状态码"},),
            current_skill="default", current_version="1",
            content_kind="generic_text",
        )
        return skill_service.decide(ctx)

    d1 = _route(False)
    d2 = _route(True)
    assert d1.status == d2.status == "migration_proposed"
    assert d1.proposed_skill == d2.proposed_skill == "api_reference"
    assert d1.proposed_version == d2.proposed_version == "1"
    for d in (d1, d2):
        keys = {(c["skill_key"], c["skill_version"]) for c in d.candidates}
        assert (d.proposed_skill, d.proposed_version) in keys


# ---------------------------------------------------------------------------
# DTO 契约与旧 Artifact 兼容
# ---------------------------------------------------------------------------


def test_proposed_fields_dto_invariants():
    base = dict(target_key="t", wiki_page_id="wk", selected_skill="default",
                selected_version="1", candidates=())
    # 成对约束。
    with pytest.raises(ValueError):
        SkillDecision(**base, status="migration_proposed", proposed_skill="api_reference")
    with pytest.raises(ValueError):
        SkillDecision(**base, status="migration_proposed", proposed_version="1")
    # 非 migration 状态不允许非空 proposed。
    with pytest.raises(ValueError):
        SkillDecision(**base, status="selected", proposed_skill="api_reference",
                      proposed_version="1")
    # 合法 migration + to_dict/from_dict 对称。
    d = SkillDecision(**base, status="migration_produced"
                      if False else "migration_proposed",
                      proposed_skill="api_reference", proposed_version="1")
    restored = SkillDecision.from_dict(d.to_dict())
    assert restored.to_dict() == d.to_dict()


def test_old_artifact_without_proposed_still_decodable():
    base = {
        "schema_version": "skill-decision/v1", "target_key": "t",
        "wiki_page_id": "wk", "selected_skill": "default",
        "selected_version": "1", "selected_by": "sticky", "confidence": 0.0,
        "status": "migration_proposed", "reason_code": "MIGRATION_PROPOSED",
        "matched_signals": [], "candidates": [], "previous_skill": "default",
        "previous_version": "1", "locked": False,
    }
    # 旧 Artifact（无 proposed 键）仍可读回（proposed=None），v3 需 fail closed。
    d = SkillDecision.from_dict(base)
    assert d.proposed_skill is None and d.proposed_version is None
    assert d.to_dict()["proposed_skill"] is None


# ---------------------------------------------------------------------------
# 3/5/6/8. v3 fail closed（shadow 前 / 失败 / 方向 / 版本）
# ---------------------------------------------------------------------------


def test_old_migration_artifact_missing_proposed_fails_closed(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    dec = _decision(wiki.id, wiki.id, skill="default", version="1",
                    status="migration_proposed", reason_code="MIGRATION_PROPOSED",
                    candidates=_default_migration_candidates())
    _write_decision_artifact(db, run.id, [dec], drop_proposed=True)
    db.commit()
    ctx = _manual_ctx(db, run, wiki, ["p1"])
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "MIGRATION_TARGET_MISSING"
    assert db.query(WikiRevision).count() == 0
    db.refresh(wiki)
    assert wiki.content_skill == "default"
    assert wiki.current_revision_id is None


def test_proposed_version_unregistered_fails_closed(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    dec = _decision(wiki.id, wiki.id, skill="default", version="1",
                    status="migration_proposed", reason_code="MIGRATION_PROPOSED",
                    proposed_skill="api_reference", proposed_version="99",
                    candidates=_default_migration_candidates())
    _write_decision_artifact(db, run.id, [dec])
    db.commit()
    ctx = _manual_ctx(db, run, wiki, ["p1"])
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "MIGRATION_TARGET_INVALID"
    assert db.query(WikiRevision).count() == 0


def test_direction_api_to_default_fails_closed(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="api_reference", skill_version="1")
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    dec = _decision(wiki.id, wiki.id, skill="api_reference", version="1",
                    status="migration_proposed", reason_code="MIGRATION_PROPOSED",
                    proposed_skill="default", proposed_version="1",
                    candidates=[{"skill_key": "api_reference", "skill_version": "1",
                                 "combined_score": 1.0},
                                {"skill_key": "default", "skill_version": "1",
                                 "combined_score": 0.2}])
    _write_decision_artifact(db, run.id, [dec])
    db.commit()
    ctx = _manual_ctx(db, run, wiki, ["p1"])
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "MIGRATION_DIRECTION_NOT_SUPPORTED"


def test_unknown_proposed_skill_fails_closed(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    dec = _decision(wiki.id, wiki.id, skill="default", version="1",
                    status="migration_proposed", reason_code="MIGRATION_PROPOSED",
                    proposed_skill="mystery", proposed_version="1",
                    candidates=_default_migration_candidates())
    _write_decision_artifact(db, run.id, [dec])
    db.commit()
    ctx = _manual_ctx(db, run, wiki, ["p1"])
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "MIGRATION_DIRECTION_NOT_SUPPORTED"


def test_migration_state_mismatch_fails_closed(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    # DB 当前 skill 与 proposal 的 selected 不一致 → 不迁移。
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="2")
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    dec = _decision(wiki.id, wiki.id, skill="default", version="1",
                    status="migration_proposed", reason_code="MIGRATION_PROPOSED",
                    proposed_skill="api_reference", proposed_version="1",
                    candidates=_default_migration_candidates())
    _write_decision_artifact(db, run.id, [dec])
    db.commit()
    ctx = _manual_ctx(db, run, wiki, ["p1"])
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "MIGRATION_TARGET_INVALID"


def test_locked_wiki_does_not_migrate(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1", skill_locked=True)
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    # locked 决策：selected default、无 proposed。
    dec = _decision(wiki.id, wiki.id, skill="default", version="1",
                    status="locked", reason_code="SKILL_LOCKED",
                    selected_by="locked", locked=True,
                    previous_skill="default", previous_version="1")
    _write_decision_artifact(db, run.id, [dec])
    db.commit()
    # 分派：status=locked（非 migration）→ default 分支保持锁定 Skill，不迁移。
    plan, error = v3mod._build_v3_plan(db, run, _manual_ctx(db, run, wiki, ["p1"]))
    assert error is None and plan is not None
    assert plan["branch"] == "default"
    assert plan.get("migration") in (None, False)
    db.refresh(wiki)
    assert wiki.content_skill == "default"


# ---------------------------------------------------------------------------
# 7/8/9. Shadow compile 与原子切换
# ---------------------------------------------------------------------------


def test_shadow_compile_has_no_product_writes_before_publish(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    dec = _decision(wiki.id, wiki.id, skill="default", version="1",
                    status="migration_proposed", reason_code="MIGRATION_PROPOSED",
                    proposed_skill="api_reference", proposed_version="1",
                    candidates=_default_migration_candidates())
    _write_decision_artifact(db, run.id, [dec])
    db.commit()
    ctx = _manual_ctx(db, run, wiki, ["p1"])
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok"), res
    # shadow 编译不产生任何产品写。
    assert db.query(WikiRevision).count() == 0
    assert db.query(WikiSection).count() == 0
    assert db.query(WikiSectionEvidenceBinding).count() == 0
    # validate 通过（publishable=True）。
    vres = v3mod._stage_validate_v3(db, run, None, ctx)
    assert vres.get("ok"), vres
    db.refresh(wiki)
    assert wiki.content_skill == "default"
    assert wiki.current_revision_id is None
    assert wiki.dirty is True


def test_migration_shadow_failure_keeps_old_revision_and_dirty(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    # 来源只有 stale Evidence → shadow compile 不可发布（来源集合/无 active evidence）。
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev-old", status="stale")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    _add_old_revision(db, wiki, content_skill="default", skill_version="1")
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    # 用直接决策 artifact 模拟（Router 会因无 active evidence 仍产 migration 建议：
    # 信号来自正文；但为确定性直接注入 proposal）。
    dec = _decision(wiki.id, wiki.id, skill="default", version="1",
                    status="migration_proposed", reason_code="MIGRATION_PROPOSED",
                    proposed_skill="api_reference", proposed_version="1",
                    candidates=_default_migration_candidates())
    db.add(Artifact(
        run_id=run.id, artifact_type="skill_decision",
        schema_version="skill-decision/v1", object_type="skill_decision",
        payload_json=json.dumps(
            {"schema_version": "skill-decision/v1",
             "decisions": [dec.to_dict()]}, ensure_ascii=False,
        )))
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "MIGRATION_VALIDATION_FAILED"
    db.refresh(wiki)
    assert wiki.content_skill == "default"
    assert wiki.skill_version == "1"
    assert wiki.current_revision_id == "rev-old"
    assert wiki.dirty is True
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 1


def test_default_to_api_migration_atomic_switch(db):
    """Router 真实产出 migration_proposed → shadow compile → 原子切换 + Manifest。"""
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wiki)
    assert wiki.current_revision_id is not None
    assert wiki.content_skill == "api_reference"
    assert wiki.skill_version == "1"
    assert wiki.skill_selected_by == "migration"
    assert wiki.dirty is False
    dec = json.loads(wiki.skill_decision_json)
    assert dec["status"] == "selected"
    assert dec["reason_code"] == "MIGRATION_APPLIED"
    assert dec["selected_by"] == "migration"
    assert dec["previous_skill"] == "default"
    # Manifest：previous/skill/migration_applied/reason。
    art = db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == "wiki_publish_manifest").one()
    manifest = json.loads(art.payload_json)
    skill = manifest["skill"]
    assert skill["migration_applied"] is True
    assert skill["previous_skill"] == "default"
    assert skill["skill_key"] == "api_reference"
    assert skill["reason"] == "MIGRATION_PROPOSED"
    # skill_decision Artifact 保留原 proposal（未被覆盖）。
    decisions = []
    for a in db.query(Artifact).filter(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "skill_decision").all():
        payload = json.loads(a.payload_json)
        decisions.extend(payload.get("decisions") or [])
    assert any(d.get("status") == "migration_proposed" and
               d.get("proposed_skill") == "api_reference" for d in decisions)


def test_migration_publish_exception_rolls_back_all(db, monkeypatch):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    _add_old_revision(db, wiki, content_skill="default", skill_version="1",
                      protected_content="人工内容：请勿覆盖。")
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    orig = v3mod._persist_api_revision

    def _boom(db, context, plan, compiled, wiki, skill_key, skill_version, revision_id):
        orig(db, context, plan, compiled, wiki, skill_key, skill_version, revision_id)
        raise RuntimeError("boom-migration")

    monkeypatch.setattr(v3mod, "_persist_api_revision", _boom)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "API_PERSIST_FAILED"
    db.refresh(wiki)
    assert wiki.content_skill == "default"
    assert wiki.skill_version == "1"
    assert wiki.current_revision_id == "rev-old"
    assert wiki.dirty is True
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 1
    # 旧 protected 内容未被破坏。
    old = db.query(WikiSection).filter(
        WikiSection.revision_id == "rev-old",
        WikiSection.content == "人工内容：请勿覆盖。").first()
    assert old is not None


def test_migration_lease_lost_does_not_switch(tmp_path, monkeypatch):
    engine = _file_engine(tmp_path)
    main = sessionmaker(bind=engine)()
    _bootstrap_builtin()
    ws = _mk_ws(main, "nb-1")
    page = _mk_page(main, ws, "p1")
    _mk_evidence(main, page, "ev1")
    wiki = _mk_wiki(main, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    main.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    run = executor.create_run(
        main, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    main.commit()
    entered = threading.Event()
    release = threading.Event()
    orig_synth = v3mod._stage_synthesize_v3

    def _blocked(db, run, stage_row, ctx):
        entered.set()
        assert release.wait(30)
        return orig_synth(db, run, stage_row, ctx)

    monkeypatch.setattr(v3mod, "_stage_synthesize_v3", _blocked)
    pregs.replace_for_test(pregs.PipelineDef(
        key="wiki.default", version="3", stages=v3mod._stage_defs_v3(),
        allow_null_workspace=False))
    worker = sessionmaker(bind=engine)()
    errors: list[str] = []

    def _target():
        try:
            executor.execute_run(worker, run.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            try:
                worker.rollback()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_target)
    t.start()
    try:
        assert entered.wait(30)
        import datetime
        main.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
                "UPDATE knowledge_compile_runs SET lease_expires_at=:le WHERE id=:id"),
            {"le": datetime.datetime.utcnow() - datetime.timedelta(seconds=5),
             "id": run.id})
        main.commit()
    finally:
        release.set()
    t.join(timeout=30)
    worker.close()
    main.expire_all()
    fresh = main.get(CompileRun, run.id)
    assert fresh.status != "succeeded"
    main.expire_all()
    wiki_fresh = main.get(WikiPage, wiki.id)
    assert wiki_fresh.content_skill == "default"
    assert wiki_fresh.current_revision_id is None
    assert wiki_fresh.dirty is True
    assert main.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 0
    main.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 10-14/17/19/21. protected 复制保留与冲突（普通 API 更新）
# ---------------------------------------------------------------------------


def _add_old_revision(db, wiki, *, content_skill="default", skill_version="1",
                      protected_content=None, sections=None):
    """给 wiki 追加一个旧 Revision（可带 protected Section），置为 current。"""
    rev = WikiRevision(id="rev-old", wiki_page_id=wiki.id, title=wiki.title,
                       summary="", source_hash=_hash("old"), status="published",
                       edit_type="auto")
    db.add(rev)
    db.flush()
    if sections:
        for spec in sections:
            db.add(_section_row(rev.id, spec))
    elif protected_content:
        db.add(WikiSection(
            id="sec-prot", revision_id=rev.id, section_type="facts",
            heading="人工说明", content=protected_content, order_index=0,
            locked=True, content_origin="manual", merge_policy="protected"))
    else:
        db.add(WikiSection(
            id="sec-plain", revision_id=rev.id, section_type="facts",
            heading="正文", content="旧正文", order_index=0,
            content_origin="auto", merge_policy="auto"))
    db.flush()
    wiki.current_revision_id = rev.id
    wiki.status = "published"
    wiki.content_skill = content_skill
    wiki.skill_version = skill_version
    db.flush()
    return rev


def _section_row(revision_id, spec):
    return WikiSection(
        id=spec["id"], revision_id=revision_id,
        section_type=spec.get("section_type", "facts"),
        heading=spec.get("heading"), content=spec.get("content", ""),
        order_index=spec.get("order_index", 0),
        locked=spec.get("locked", False),
        version_label=spec.get("version_label"),
        content_origin=spec.get("content_origin", "auto"),
        merge_policy=spec.get("merge_policy", "auto"),
        section_key=spec.get("section_key"),
        content_hash=spec.get("content_hash"),
        validation_status=spec.get("validation_status"),
        structure_json=spec.get("structure_json"),
        skill_key=spec.get("skill_key"),
        skill_version=spec.get("skill_version"),
    )


def _add_binding(db, section_id, field_path, evidence_id, ev_hash, usage="support"):
    db.add(WikiSectionEvidenceBinding(
        id=("bind-" + field_path.replace(".", "_") + "-" + (evidence_id or "")),
        section_id=section_id, evidence_id=evidence_id, field_path=field_path,
        usage_type=usage, evidence_content_hash=ev_hash))
    db.flush()


def _run_api_update(db, ws, wiki, page_ids, page_id="p1", decision=None):
    """通过 executor 跑一次 api_reference manual rebuild（锁定 api，普通更新）。"""
    if decision is None:
        decision = _decision(wiki.id, wiki.id, skill="api_reference", version="1",
                             status="locked", reason_code="SKILL_LOCKED",
                             selected_by="locked", locked=True,
                             previous_skill="api_reference", previous_version="1")
    run = _new_queued_run(db, ws, wiki)
    _write_decision_artifact(db, run.id, [decision])
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wiki)
    return wiki


def test_ordinary_api_update_preserves_protected_and_orders(db):
    """普通 api 更新：不同 key protected 保留 + 相同 key protected 胜出 + NULL 保留。"""
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    ev = _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="api_reference", skill_version="1",
                    skill_locked=True)
    # 旧 Revision：普通 auto overview（将被覆盖）、protected 'manual'、NULL key、冲突 'sources'。
    _add_old_revision(db, wiki, content_skill="api_reference", skill_version="1",
                      sections=[
                          {"id": "s-a", "order_index": 0, "section_key": "overview",
                           "content": "OLD-AUTO-OVERVIEW", "content_origin": "auto",
                           "merge_policy": "auto"},
                          {"id": "s-b", "order_index": 1, "section_key": "manual",
                           "content": "MANUAL-KEEP", "locked": True,
                           "content_origin": "manual", "merge_policy": "protected"},
                          {"id": "s-c", "order_index": 2, "section_key": None,
                           "content": "NULL-KEEP", "locked": True,
                           "content_origin": "manual", "merge_policy": "protected"},
                          {"id": "s-d", "order_index": 3, "section_key": "sources",
                           "content": "MANUAL-SOURCES", "locked": True,
                           "content_origin": "manual", "merge_policy": "protected"},
                      ])
    _add_binding(db, "s-b", "manual.path", "ev1", "f" * 64)
    db.commit()
    _run_api_update(db, ws, wiki, ["p1"], page_id="p1")
    secs = _new_rev_secs(db, wiki)
    # order_index 连续。
    assert [s.order_index for s in secs] == list(range(len(secs)))
    keys = [s.section_key for s in secs]
    # 自动 overview（s-a auto 被新编译覆盖）、endpoint 均在。
    assert keys[0] == "overview"
    assert "api_endpoint|get|/api/users|unversioned" in keys
    # protected 'sources' 冲突 → protected 胜出（占据 sources 位置）。
    sources = [s for s in secs if s.section_key == "sources"]
    assert len(sources) == 1
    assert sources[0].content == "MANUAL-SOURCES"
    assert sources[0].merge_policy == "protected"
    # 'manual' 与 NULL 追加在自动 Section 之后，相对顺序保留。
    tail = [s for s in secs if s.section_key in ("manual", None)]
    assert [s.content for s in tail] == ["MANUAL-KEEP", "NULL-KEEP"]
    # 新 auto overview 非人工。
    overview = [s for s in secs if s.section_key == "overview"][0]
    assert overview.content != "OLD-AUTO-OVERVIEW"
    assert overview.content_origin == "auto"
    # 旧 Revision 行不受影响。
    old_s = db.get(WikiSection, "s-b")
    assert old_s is not None and old_s.revision_id == "rev-old"
    # 相同 key protected 胜出 → 只有一条 sources。
    assert sum(1 for s in secs if s.section_key == "sources") == 1


def test_protected_binding_snapshot_copied_and_stale_evidence_kept(db):
    """protected Binding 快照复制；Evidence 变 stale 不删历史 binding。"""
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev-active")
    _mk_evidence(db, page, "ev-old", content_hash="a" * 64)
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="api_reference", skill_version="1",
                    skill_locked=True)
    _add_old_revision(db, wiki, content_skill="api_reference", skill_version="1",
                      sections=[
                          {"id": "s-prot", "order_index": 0,
                           "section_key": "custom_manual", "content": "MANUAL-KEEP",
                           "locked": True, "content_origin": "manual",
                           "merge_policy": "protected"},
                      ])
    _add_binding(db, "s-prot", "responses.200", "ev-old", "a" * 64)
    db.commit()
    # 让 ev-old 变 stale（历史 binding 保留，快照复制）。
    ev_old = db.get(EvidenceItem, "ev-old")
    ev_old.status = "stale"
    db.commit()
    _run_api_update(db, ws, wiki, ["p1"], page_id="p1")
    secs = _new_rev_secs(db, wiki)
    copied = [s for s in secs if s.section_key == "custom_manual"]
    assert len(copied) == 1
    new_sec = copied[0]
    binds = _bindings_for(db, new_sec.id)
    assert len(binds) == 1
    b = binds[0]
    assert b.evidence_id == "ev-old"
    assert b.evidence_content_hash == "a" * 64
    assert b.field_path == "responses.200"
    assert b.usage_type == "support"
    # 旧 binding 仍指向旧 Section。
    old_binds = _bindings_for(db, "s-prot")
    assert len(old_binds) == 1


def test_reversed_section_insert_order_same_result(db):
    """protected Section 插入顺序反转（同数据）→ 最终 Section 顺序与内容一致。"""
    _bootstrap_builtin()
    results = []
    for tag in ("A", "B"):
        ws = _mk_ws(db, f"nb-{tag}")
        page = _mk_page(db, ws, f"p-{tag}")
        _mk_evidence(db, page, f"ev-{tag}")
        wiki = _mk_wiki(db, ws, f"wk-{tag}", page_ids=[f"p-{tag}"], dirty=True,
                        content_skill="api_reference", skill_version="1",
                        skill_locked=True)
        rev = WikiRevision(id=f"rev-{tag}", wiki_page_id=wiki.id, title=wiki.title,
                           summary="", source_hash=_hash("old"), status="published",
                           edit_type="auto")
        db.add(rev)
        db.flush()
        rows = [
            {"id": f"x-{tag}-1", "order_index": 0, "section_key": None,
             "content": "M1", "locked": True, "content_origin": "manual",
             "merge_policy": "protected"},
            {"id": f"x-{tag}-2", "order_index": 1, "section_key": None,
             "content": "M2", "locked": True, "content_origin": "manual",
             "merge_policy": "protected"},
        ]
        order = rows if tag == "A" else list(reversed(rows))
        for spec in order:
            db.add(_section_row(rev.id, spec))
            db.flush()
        wiki.current_revision_id = rev.id
        wiki.status = "published"
        db.flush()
        db.commit()
        _run_api_update(db, ws, wiki, [f"p-{tag}"], page_id=f"p-{tag}")
        secs = _new_rev_secs(db, wiki)
        results.append([(s.section_key, s.content, s.order_index) for s in secs])
    assert results[0] == results[1]


# ---------------------------------------------------------------------------
# 20. default 分支保护无回归
# ---------------------------------------------------------------------------


def test_default_branch_protected_preserved_no_regression(db):
    """v3 default（仅 default Skill）manual rebuild 保持 v1 protected 复制语义。"""
    _bootstrap_default()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1", spec="内容：" + "y" * 60, title="功能说明")
    wiki = _mk_wiki(db, ws, "wk1", title="主题A", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    _add_old_revision(db, wiki, content_skill="default", skill_version="1",
                      sections=[
                          {"id": "s-lock", "order_index": 0, "section_key": None,
                           "content": "人工锁定正文：勿覆盖",
                           "locked": True, "content_origin": "manual",
                           "merge_policy": "protected"},
                      ])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wiki)
    assert wiki.content_skill == "default"
    secs = _new_rev_secs(db, wiki)
    locked = [s for s in secs if s.content == "人工锁定正文：勿覆盖"]
    assert locked and locked[0].merge_policy == "protected"


# ---------------------------------------------------------------------------
# 21. 迁移 Manifest / structure_json 不泄漏
# ---------------------------------------------------------------------------


def test_migration_manifest_and_structure_no_leak(db):
    marker = "TOKEN_MIGRATION_LEAK_77"
    path = r"C:\Users\secret\migration.md"
    spec = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "用户 API", "version": "1.0",
                 "description": "GET /api/users 返回 200 成功。" + marker + path},
        "paths": {"/api/users": {"get": {
            "description": "返回 200 成功。",
            "responses": {"200": {"description": "ok"}},
        }}},
    }, ensure_ascii=False)
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1", spec=spec)
    _mk_evidence(db, page, "ev1", content=marker + path)
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1")
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wiki)
    art = db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == "wiki_publish_manifest").one()
    blob = art.payload_json or ""
    assert marker not in blob and path not in blob
    assert "prompt" not in blob.lower()
    secs = db.query(WikiSection).filter(
        WikiSection.revision_id == wiki.current_revision_id).all()
    for s in secs:
        assert marker not in (s.structure_json or "")
        assert path not in (s.structure_json or "")
    assert marker not in wiki.skill_decision_json
    assert path not in wiki.skill_decision_json


def _file_engine(tmp_path):
    url = f"sqlite:///{(tmp_path / 'mig7c.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=8000")

    init_db(engine)
    return engine
