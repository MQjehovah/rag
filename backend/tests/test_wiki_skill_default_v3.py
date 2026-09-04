"""Phase 7C.3-A：wiki.default v3 单目标发布闭环测试。

覆盖（每项对应需求清单）：
1. v3 精确注册但 active 仍为 v2；
2. api_reference 单 Page 成功发布；
3. Revision/Section/Binding 数量与字段正确；
4. Manifest 含全部 revision/wiki/graph targets 与 skill 信息；
5. default v3 与 v2 单目标等价；
6. Evidence stale 时零发布；
7. Evidence hash 改变时零发布；
8. Page hash 在编译后改变时零发布；
9. Workspace 不匹配时零发布；
10. publish 中途制造异常 → Revision/Section/Binding 全回滚；
11. lease 丢失后不发布；
12. protected/manual Section 存在时 fail closed；
13. Artifact/structure_json/Manifest 不含 excerpt/Prompt/Token/绝对路径；
14. 不支持的 batch_rebuild 不发布；
15. migration_proposed 本轮不切换；
16. 未知 Skill/版本不回退 default；
17. graph stage 从已持久化 Manifest 读取目标；
18. graph retry 不重复创建 Revision；
19. 输入顺序不影响 Section 与 Binding 的确定性结果；
20. 不调用 _legacy_*。

竞态测试优先使用 Event/Barrier（lease 丢失场景），不依赖长 sleep。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import threading

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 as v3mod
from app.core.knowledge_compiler_v3 import wiki_page_builder as legacy_builder
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline import registry as pregs
from app.core.wiki_pipeline.pipelines.wiki_default import (
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    register_default_pipeline,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
    ARTIFACT_TYPE_SKILL_DECISION,
    register_default_pipeline_v2,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
    register_default_pipeline_v3,
)
from app.core.wiki_skills import registry as sreg
from app.core.wiki_skills import service as skill_service
from app.core.wiki_skills.schemas import SkillDecision
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

V3_KEYS = ("resolve_context", "topic_route", "skill_route", "synthesize_by_skill",
           "validate_by_skill", "publish_by_skill", "finalize_compile_outcome",
           "schedule_graph")


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _openapi_spec(overview: str = "GET /api/users 返回用户列表。返回 200 成功。") -> str:
    return json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "用户 API", "version": "1.0", "description": overview},
        "paths": {"/api/users": {"get": {
            "summary": "用户列表",
            "description": "返回 200 成功。",
            "responses": {"200": {"description": "成功返回"}},
        }}},
    }, ensure_ascii=False)


def _mk_llm(*, ingest_ops=None, contexts=None, content="聚合正文：" + "x" * 60):
    def _run(messages, context="", timeout=120.0):
        if contexts is not None:
            contexts.append(context)
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": ingest_ops if ingest_ops is not None else []}
        if context in ("wiki-synthesis", "wiki-mapreduce"):
            return {"summary": "摘要", "content": content}
        return {"worthy": True, "ops": []}
    return _run


def _graph_noop(**kw):
    return None


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
# 注册 / 数据 helpers
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


def _mk_page(db, ws, pid, spec=None, title="用户 API 文档", wiki_dirty=True):
    spec = spec if spec is not None else _openapi_spec()
    from app.models.database import NotebookWorkspaceBinding

    binding = db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.workspace_id == ws.id).first()
    nb_id = binding.notebook_id if binding is not None else None
    db.add(Page(
        id=pid, notebook_id=nb_id,
        title=title, content=spec, content_hash=_hash(spec),
        wiki_dirty=wiki_dirty))
    db.flush()
    return db.get(Page, pid)


def _mk_evidence(db, page, eid, *, status="active", content_hash=None,
                 locator=None, content="evidence-content"):
    db.add(EvidenceItem(
        id=eid, source_page_id=page.id, status=status,
        content=content,
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


def _decision(target_key, wiki_page_id, skill="api_reference", version="1", **kw):
    fields = dict(
        target_key=target_key, wiki_page_id=wiki_page_id,
        selected_skill=skill, selected_version=version,
        selected_by=kw.pop("selected_by", "auto"),
        confidence=kw.pop("confidence", 1.0),
        status=kw.pop("status", "selected"),
        reason_code=kw.pop("reason_code", "TEST_DECISION"),
        matched_signals=("api_path",),
    )
    return SkillDecision(**fields)


def _write_decision_artifact(db, run_id, decisions):
    db.add(Artifact(
        run_id=run_id,
        artifact_type=ARTIFACT_TYPE_SKILL_DECISION,
        schema_version="skill-decision/v1",
        object_type="skill_decision",
        payload_json=json.dumps(
            {"schema_version": "skill-decision/v1",
             "decisions": [d.to_dict() for d in decisions]},
            ensure_ascii=False,
        ),
    ))
    db.flush()


def _new_queued_run(db, ws, wiki, trigger="manual_rebuild"):
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type=trigger, trigger_object_id=wiki.id,
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


def _api_stage_state(db, run, wiki, page_ids, decision):
    """直调 synthesize + publish 所需的 ctx（合成阶段已写入 v3 缓存）。"""
    ctx = _manual_ctx(db, run, wiki, page_ids)
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok"), res
    return ctx


def _counts(db):
    return {
        "revisions": db.query(WikiRevision).count(),
        "sections": db.query(WikiSection).count(),
        "bindings": db.query(WikiSectionEvidenceBinding).count(),
    }


def _counts_for_wiki(db, wiki):
    rev_ids = [r.id for r in db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).all()]
    secs = db.query(WikiSection).filter(
        WikiSection.revision_id.in_(rev_ids) if rev_ids else False).count()
    binds = db.query(WikiSectionEvidenceBinding).filter(
        WikiSectionEvidenceBinding.section_id.in_(
            [s.id for s in db.query(WikiSection).filter(
                WikiSection.revision_id.in_(rev_ids) if rev_ids else False).all()]
        )
    ).count()
    return {"revisions": len(rev_ids), "sections": secs, "bindings": binds}


# ---------------------------------------------------------------------------
# 1. v3 精确注册，active 仍 v2
# ---------------------------------------------------------------------------


def test_v3_registered_but_active_stays_v2():
    _bootstrap_default()
    assert pregs.get_pipeline("wiki.default", "3") is not None
    assert tuple(pregs.get_pipeline("wiki.default", "3").stage_keys()) == V3_KEYS
    assert pregs.get_pipeline("wiki.default", "2") is not None
    assert pregs.get_active_version("wiki.default") == "2"
    # 再注册 v1 不改变 active（显式 v2 优先）。
    assert pregs.get_pipeline("wiki.default").version == "2"
    # v3 重复注册幂等。
    register_default_pipeline_v3()


# ---------------------------------------------------------------------------
# 2/3. api_reference 单 Page 成功发布 + Revision/Section/Binding 数量与字段
# ---------------------------------------------------------------------------


def test_api_reference_single_page_publish_success(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
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
    assert executed.pipeline_version == "3"

    db.refresh(wiki)
    assert wiki.status == "published"
    assert wiki.dirty is False
    assert wiki.current_revision_id is not None
    assert wiki.content_skill == "api_reference"
    assert wiki.skill_version == "1"
    assert wiki.source_page_ids == json.dumps(["p1"], ensure_ascii=False)

    counts = _counts_for_wiki(db, wiki)
    assert counts["revisions"] == 1
    assert counts["sections"] == 3
    assert counts["bindings"] >= 1

    secs = db.query(WikiSection).filter(
        WikiSection.revision_id == wiki.current_revision_id
    ).order_by(WikiSection.order_index).all()
    keys = [s.section_key for s in secs]
    assert keys == ["overview", "api_endpoint|get|/api/users|unversioned", "sources"]
    for s in secs:
        assert s.skill_key == "api_reference"
        assert s.skill_version == "1"
        assert s.content_hash and len(s.content_hash) == 64
        assert all(c in "0123456789abcdef" for c in s.content_hash)
        assert s.validation_status == "pass"
        assert s.content_origin == "auto"
        assert s.merge_policy == "auto"
        assert json.loads(s.structure_json)["section_key"] == s.section_key
    binds = db.query(WikiSectionEvidenceBinding).filter(
        WikiSectionEvidenceBinding.section_id.in_([s.id for s in secs])
    ).all()
    assert binds
    for b in binds:
        assert b.usage_type in ("support", "conflict")
        assert b.field_path.strip()
        assert len(b.evidence_content_hash) == 64
        assert b.evidence_id == "ev1"
    # Revision 字段。
    rev = db.get(WikiRevision, wiki.current_revision_id)
    assert rev is not None and rev.status == "published"
    assert rev.edit_type == "auto"


# ---------------------------------------------------------------------------
# 4. Manifest 含 revision/wiki/graph targets + skill 信息
# ---------------------------------------------------------------------------


def test_api_manifest_fields(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
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
    art = db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    ).one()
    manifest = json.loads(art.payload_json)
    assert manifest["outcome"] == "published"
    assert manifest["wiki_page_ids"] == [wiki.id]
    assert manifest["revision_ids"] == [wiki.current_revision_id]
    assert {"kind": "wiki", "wiki_page_id": wiki.id} in manifest["graph_targets"]
    assert manifest["input_hash"]
    assert manifest["skill"]["skill_key"] == "api_reference"
    assert manifest["skill"]["skill_version"] == "1"


# ---------------------------------------------------------------------------
# 5. default v3 与 v2 单目标等价（manual_rebuild 同一 wiki 顺序执行）
# ---------------------------------------------------------------------------


def _section_snapshot(db, wiki):
    if not wiki.current_revision_id:
        return []
    rows = db.query(WikiSection).filter(
        WikiSection.revision_id == wiki.current_revision_id
    ).order_by(WikiSection.order_index).all()
    return [(s.section_type, s.heading, s.content) for s in rows]


def test_default_v3_equals_v2_single_target(db):
    _bootstrap_default()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1", title="功能说明",
                    spec="内容：" + "y" * 60, wiki_dirty=True)
    wiki = _mk_wiki(db, ws, "wk1", title="主题A", page_ids=["p1"], dirty=True)
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    # v2 先跑一遍（同一 wiki）。
    run2 = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="2",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed = executor.execute_run(db, run2.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wiki)
    snap_v2 = (_section_snapshot(db, wiki), wiki.status, wiki.dirty,
               wiki.content_skill, wiki.skill_version, wiki.current_revision_id is not None)
    # v3 同 wiki 再跑一遍。
    wiki.dirty = True
    db.commit()
    run3 = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed3 = executor.execute_run(db, run3.id)
    db.refresh(executed3)
    assert executed3.status == "succeeded", (executed3.safe_error_code,
                                             executed3.safe_error_message)
    db.refresh(wiki)
    snap_v3 = (_section_snapshot(db, wiki), wiki.status, wiki.dirty,
               wiki.content_skill, wiki.skill_version, wiki.current_revision_id is not None)
    assert snap_v3 == snap_v2
    # manual_rebuild 语义一致：不清空来源 Page 的 dirty（v2/v3 相同）。
    db.refresh(page)
    assert page.wiki_dirty is True
    assert wiki.dirty is False
    assert wiki.content_skill == "default"
    # Manifest 关键字段一致（revision_ids/wikis/graph targets 形状）。
    m2 = json.loads(db.query(Artifact).filter(
        Artifact.run_id == run2.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST).one().payload_json)
    m3 = json.loads(db.query(Artifact).filter(
        Artifact.run_id == run3.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST).one().payload_json)
    for key in ("outcome", "note", "wiki_page_ids", "graph_targets", "fail_code",
                "retryable"):
        assert m3[key] == m2[key]
    # revision_ids 为各 run 自身新 Revision（uuid 不同）；数量与语义一致。
    assert len(m3["revision_ids"]) == len(m2["revision_ids"]) == 1


# ---------------------------------------------------------------------------
# 6-9. 发布前 Evidence 重验矩阵：零发布
# ---------------------------------------------------------------------------


def _matrix_setup(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    ev = _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    decision = _decision(wiki.id, wiki.id)
    _write_decision_artifact(db, run.id, [decision])
    db.commit()
    return ws, page, ev, wiki, run


def _run_synth_and_publish(db, run, wiki, mutate):
    ctx = _api_stage_state(db, run, wiki, ["p1"], None)
    mutate()
    return v3mod._stage_publish_v3(db, run, None, ctx)


def test_evidence_status_stale_zero_publish(db):
    ws, page, ev, wiki, run = _matrix_setup(db)
    before = _counts_for_wiki(db, wiki)
    res = _run_synth_and_publish(db, run, wiki, lambda: setattr(ev, "status", "stale"))
    assert res.get("ok") is False
    assert res["error_code"] == "EVIDENCE_STALE"
    assert _counts_for_wiki(db, wiki) == before
    db.refresh(wiki)
    assert wiki.current_revision_id is None


def test_evidence_hash_changed_zero_publish(db):
    ws, page, ev, wiki, run = _matrix_setup(db)
    before = _counts_for_wiki(db, wiki)
    res = _run_synth_and_publish(db, run, wiki, lambda: setattr(ev, "content_hash", "b" * 64))
    assert res.get("ok") is False
    assert res["error_code"] == "EVIDENCE_STALE"
    assert _counts_for_wiki(db, wiki) == before


def test_page_hash_changed_zero_publish(db):
    ws, page, ev, wiki, run = _matrix_setup(db)
    before = _counts_for_wiki(db, wiki)
    res = _run_synth_and_publish(db, run, wiki,
                                 lambda: setattr(page, "content_hash", "a" * 64))
    assert res.get("ok") is False
    assert res["error_code"] == "PAGE_STALE"
    assert _counts_for_wiki(db, wiki) == before


def test_workspace_mismatch_zero_publish(db):
    ws, page, ev, wiki, run = _matrix_setup(db)
    before = _counts_for_wiki(db, wiki)
    res = _run_synth_and_publish(db, run, wiki, lambda: setattr(ws, "status", "archived"))
    assert res.get("ok") is False
    assert res["error_code"] == "WORKSPACE_MISMATCH"
    assert _counts_for_wiki(db, wiki) == before


# ---------------------------------------------------------------------------
# 10. publish 中途异常 → 原子回滚（Revision/Section/Binding 全回滚）
# ---------------------------------------------------------------------------


def test_publish_exception_rolls_back_atomically(db, monkeypatch):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)

    orig = v3mod._persist_api_revision

    def _boom_after_write(db, context, plan, compiled, wiki, skill_key, skill_version,
                          revision_id):
        orig(db, context, plan, compiled, wiki, skill_key, skill_version, revision_id)
        raise RuntimeError("boom-mid-publish")

    monkeypatch.setattr(v3mod, "_persist_api_revision", _boom_after_write)
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
    assert "boom" not in (executed.safe_error_message or "")
    db.refresh(wiki)
    assert wiki.current_revision_id is None
    assert wiki.dirty is True
    assert db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count() == 0
    assert db.query(WikiSection).count() == 0
    assert db.query(WikiSectionEvidenceBinding).count() == 0


# ---------------------------------------------------------------------------
# 11. lease 丢失后不发布（Event/Barrier，文件型 SQLite + 双 Session）
# ---------------------------------------------------------------------------


def _file_engine(tmp_path):
    url = f"sqlite:///{(tmp_path / 'lease_v3.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=8000")

    init_db(engine)
    return engine


def test_lease_lost_no_publish(tmp_path, monkeypatch):
    engine = _file_engine(tmp_path)
    main = sessionmaker(bind=engine)()
    _bootstrap_builtin()
    ws = _mk_ws(main, "nb-1")
    page = _mk_page(main, ws, "p1")
    _mk_evidence(main, page, "ev1")
    wiki = _mk_wiki(main, ws, "wk1", page_ids=["p1"], dirty=True)
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

    def _blocked_synth(db, run, stage_row, ctx):
        entered.set()
        assert release.wait(30)
        return orig_synth(db, run, stage_row, ctx)

    monkeypatch.setattr(v3mod, "_stage_synthesize_v3", _blocked_synth)
    # 重新注册 v3（execute 指向被 patch 的函数对象）。
    pregs.replace_for_test(pregs.PipelineDef(
        key="wiki.default", version="3",
        stages=v3mod._stage_defs_v3(), allow_null_workspace=False))

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
        # 让 lease 过期（模拟 worker 丢失/恢复）。
        main.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
                "UPDATE knowledge_compile_runs SET lease_expires_at=:le WHERE id=:id"
            ),
            {"le": datetime.datetime.utcnow() - datetime.timedelta(seconds=5), "id": run.id},
        )
        main.commit()
    finally:
        release.set()
    t.join(timeout=30)
    worker.close()

    main.expire_all()
    fresh = main.get(CompileRun, run.id)
    # 旧 worker 结果被 fence 丢弃：不成功、不发布。
    assert fresh.status != "succeeded"
    assert fresh.output_revision_id is None
    assert main.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 0
    assert main.query(WikiSection).count() == 0
    assert main.query(WikiSectionEvidenceBinding).count() == 0
    art = main.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    ).count()
    assert art == 0
    main.expire_all()
    wiki_fresh = main.get(WikiPage, wiki.id)
    assert wiki_fresh.current_revision_id is None
    assert wiki_fresh.dirty is True
    main.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 12. protected/manual Section → fail closed
# ---------------------------------------------------------------------------


def _add_manual_section_revision(db, wiki):
    rev = WikiRevision(
        id="rev-prot", wiki_page_id=wiki.id, title=wiki.title,
        summary="", status="published", edit_type="manual",
    )
    db.add(rev)
    db.flush()
    db.add(WikiSection(
        id="sec-prot", revision_id=rev.id, section_type="facts",
        heading="人工说明", content="人工内容不能覆盖",
        order_index=0, locked=True, content_origin="manual",
        merge_policy="protected",
    ))
    db.flush()
    wiki.current_revision_id = rev.id
    db.flush()


def _add_plain_revision(db, wiki):
    """给 wiki 附加一个普通 auto Revision（非 protected/manual），用于断言旧值保留。"""
    rev = WikiRevision(
        id="rev-old", wiki_page_id=wiki.id, title=wiki.title,
        summary="", status="published", edit_type="auto",
    )
    db.add(rev)
    db.flush()
    db.add(WikiSection(
        id="sec-old", revision_id=rev.id, section_type="facts",
        heading="正文", content="旧内容", order_index=0,
        content_origin="auto", merge_policy="auto",
    ))
    db.flush()
    wiki.current_revision_id = rev.id
    wiki.status = "published"
    db.flush()


def test_protected_manual_section_copied_into_new_revision(db):
    """旧 Revision 含 protected/manual Section → API 发布复制保留（不再 fail closed）。"""
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="api_reference", skill_version="1",
                    skill_locked=True)
    _add_manual_section_revision(db, wiki)
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    decision = _decision(wiki.id, wiki.id)
    _write_decision_artifact(db, run.id, [decision])
    db.commit()
    ctx = _api_stage_state(db, run, wiki, ["p1"], None)
    res = v3mod._stage_publish_v3(db, run, None, ctx)
    assert res.get("ok") is True, res
    # 新 current revision 已切换（同一 session 内对象已更新）；旧 Revision 仍保留。
    assert wiki.current_revision_id is not None
    assert wiki.current_revision_id != "rev-prot"
    new_rev = wiki.current_revision_id
    new_secs = db.query(WikiSection).filter(
        WikiSection.revision_id == new_rev).all()
    # 人工 Section 被复制到新 Revision（原样保留标志与内容）。
    copied = [s for s in new_secs if s.content == "人工内容不能覆盖"]
    assert len(copied) == 1
    cop = copied[0]
    assert cop.merge_policy == "protected"
    assert cop.content_origin == "manual"
    assert cop.locked is True
    # 旧人工 Section 未被删除。
    manual = db.get(WikiSection, "sec-prot")
    assert manual is not None and manual.content == "人工内容不能覆盖"
    # order_index 连续。
    indexes = sorted(s.order_index for s in new_secs)
    assert indexes == list(range(len(new_secs)))


# ---------------------------------------------------------------------------
# 13. 无 excerpt/Prompt/Token/绝对路径泄漏
# ---------------------------------------------------------------------------


def test_no_excerpt_prompt_token_path_leak(db):
    marker_excerpt = "MARKER_SECRET_EXCERPT_9x"
    marker_path = r"C:\Users\topsecret\api_spec_v3.json"
    overview = f"GET /api/users 返回 200 成功。{marker_path}"
    spec = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "用户 API", "version": "1.0", "description": overview},
        "paths": {"/api/users": {"get": {
            # marker 放在 parser 不建模的扩展字段：即使 8B display 携带 IR 事实
            # （endpoint.description 等），任意未建模来源文本也绝不能进入 structure_json。
            "description": "",
            "x-internal-probe": f"返回 200 成功。{marker_excerpt}",
            "responses": {"200": {"description": "ok"}},
        }}},
    }, ensure_ascii=False)
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1", spec=spec)
    _mk_evidence(db, page, "ev1", content=marker_excerpt)
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
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

    secs = db.query(WikiSection).filter(
        WikiSection.revision_id == wiki.current_revision_id).all()
    for s in secs:
        assert marker_excerpt not in (s.structure_json or "")
        assert marker_path not in (s.structure_json or "")
        assert "prompt" not in (s.structure_json or "").lower()
    # Manifest / 决策 / 其它 Artifact 不泄漏。
    for art in db.query(Artifact).filter(Artifact.run_id == run.id).all():
        blob = art.payload_json or ""
        assert marker_excerpt not in blob
        assert marker_path not in blob
        assert "prompt" not in blob.lower()
    rev = db.get(WikiRevision, wiki.current_revision_id)
    assert marker_excerpt not in (rev.summary or "")
    assert marker_path not in (rev.summary or "")
    assert wiki.skill_decision_json
    assert marker_excerpt not in wiki.skill_decision_json
    assert marker_path not in wiki.skill_decision_json


# ---------------------------------------------------------------------------
# 14. 不支持的 batch_rebuild 不发布
# ---------------------------------------------------------------------------


def test_batch_without_input_artifact_global_fatal_zero_publish(db):
    """batch run 无 wiki_batch_input Artifact → 全局 fatal：整批零发布、Run failed。"""
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="batch_rebuild", trigger_object_id=None,
        workspace_id=ws.id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "VALIDATION_FAILED"
    assert db.query(WikiRevision).count() == 0
    assert db.query(WikiSection).count() == 0
    db.refresh(page)
    assert page.wiki_dirty is True


# ---------------------------------------------------------------------------
# 15. migration_proposed 本轮不切换（default 保持）
# ---------------------------------------------------------------------------


def test_locked_wiki_migration_proposal_does_not_switch(db):
    """locked Wiki：来源内容再像 API 也不迁移（locked 精确沿用当前 default）。"""
    _bootstrap_builtin()
    ws = _mk_ws(db)
    # 目标 wiki 当前 skill=default 且锁定；来源内容 API 信号很强。
    page = _mk_page(db, ws, "p1", title="用户 API")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="default", skill_version="1",
                    skill_locked=True)
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
    db.refresh(wiki)
    # locked → router 恒 locked（无 proposed），不产生迁移。
    assert wiki.content_skill == "default"
    assert wiki.skill_locked is True
    api_secs = db.query(WikiSection).filter(
        WikiSection.revision_id == wiki.current_revision_id,
        WikiSection.skill_key == "api_reference").count()
    assert api_secs == 0
    assert db.query(WikiSectionEvidenceBinding).count() == 0


# ---------------------------------------------------------------------------
# 16. 未知 Skill / 版本不回退 default
# ---------------------------------------------------------------------------


def test_unknown_skill_fails_closed(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    decision = _decision(wiki.id, wiki.id, skill="mystery", version="1")
    _write_decision_artifact(db, run.id, [decision])
    db.commit()
    ctx = _manual_ctx(db, run, wiki, ["p1"])
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "SKILL_NOT_SUPPORTED"
    assert "v3" not in ctx["state"] or ctx["state"]["v3"].get("api") is None


def test_unknown_skill_version_fails_closed(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
    db.commit()
    run = _new_queued_run(db, ws, wiki)
    decision = _decision(wiki.id, wiki.id, skill="api_reference", version="99")
    _write_decision_artifact(db, run.id, [decision])
    db.commit()
    ctx = _manual_ctx(db, run, wiki, ["p1"])
    res = v3mod._stage_synthesize_v3(db, run, None, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "SKILL_NOT_SUPPORTED"
    assert db.query(WikiRevision).count() == 0


def test_compile_not_publishable_fails_closed(db):
    """compile publishable=False（编译校验失败/无事实内容）→ validate stage 真实失败、
    publish stage 被 skipped、零发布。"""
    _bootstrap_builtin()
    ws = _mk_ws(db)
    # 有效 active Evidence 但正文非接口内容（无事实 → 编译不可发布）。
    page = _mk_page(db, ws, "p1", spec="# 普通叙述\n只有自然语言，没有接口。")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="api_reference", skill_version="1",
                    skill_locked=True)
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
    assert executed.status == "failed"
    assert executed.safe_error_code == "VALIDATION_FAILED"
    db.refresh(wiki)
    assert wiki.current_revision_id is None
    assert wiki.dirty is True
    assert db.query(WikiRevision).count() == 0
    # stage 语义：validate_by_skill 真实失败、publish_by_skill 被 skipped。
    stage_by_key = {s.stage_key: s.status for s in db.query(StageRun).filter(
        StageRun.run_id == run.id).all()}
    assert stage_by_key["synthesize_by_skill"] == "succeeded"
    assert stage_by_key["validate_by_skill"] == "failed"
    assert stage_by_key["publish_by_skill"] == "skipped"


def test_source_page_without_active_evidence_blocks_publish(db):
    """两来源页其中一页无 active Evidence → 来源集合不完整 → validate 失败、零发布、
    旧 current_revision_id 保持不变。"""
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page1 = _mk_page(db, ws, "p1", title="用户 API 文档")
    _mk_evidence(db, page1, "ev1")
    page2 = _mk_page(db, ws, "p2", title="补充接口")
    # p2 不添加任何 Evidence。
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1", "p2"], dirty=True,
                    content_skill="api_reference", skill_version="1",
                    skill_locked=True)
    # 旧 Revision 与人工无关的普通 auto Section（非 protected/manual）。
    _add_plain_revision(db, wiki)
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
    assert executed.status == "failed"
    assert executed.safe_error_code == "VALIDATION_FAILED"
    stage_by_key = {s.stage_key: s.status for s in db.query(StageRun).filter(
        StageRun.run_id == run.id).all()}
    assert stage_by_key["validate_by_skill"] == "failed"
    assert stage_by_key["publish_by_skill"] == "skipped"
    db.refresh(wiki)
    assert wiki.current_revision_id == "rev-old"  # 旧 current_revision_id 不变
    assert wiki.dirty is True
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 1
    assert db.query(WikiSectionEvidenceBinding).count() == 0


# ---------------------------------------------------------------------------
# 17. graph stage 从已持久化 Manifest 读取目标
# ---------------------------------------------------------------------------


def test_graph_targets_from_persisted_manifest(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
    db.commit()
    calls: list[dict] = []

    def _graph(**kw):
        calls.append(dict(kw))
        return None

    executor.configure_external_runners(graph_runner=_graph)
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
    # schedule_graph 从 wiki_publish_manifest Artifact 读取 target 并调用真实 runner。
    assert calls == [{"wiki_page_id": wiki.id}]
    assert db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    ).count() == 1


# ---------------------------------------------------------------------------
# 18. graph retry 不重复创建 Revision
# ---------------------------------------------------------------------------


def test_graph_retry_does_not_duplicate_revision(db):
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1")
    _mk_evidence(db, page, "ev1")
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True)
    db.commit()
    calls: list[dict] = []

    def _graph_flaky(**kw):
        calls.append(dict(kw))
        if len(calls) == 1:
            raise RuntimeError("graph-down-once")
        return None

    executor.configure_external_runners(graph_runner=_graph_flaky)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki.id,
        workspace_id=ws.id, wiki_page_id=wiki.id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "GRAPH_BUILD_FAILED"
    # 第一次已发布 Revision（graph 失败不回滚已发布内容）。
    db.refresh(wiki)
    assert wiki.current_revision_id is not None
    first_rev = wiki.current_revision_id

    requeued = executor.retry_run(db, run.id)
    assert requeued.status == "queued"
    executed2 = executor.execute_run(db, run.id)
    db.refresh(executed2)
    assert executed2.status == "succeeded", (executed2.safe_error_code,
                                             executed2.safe_error_message)
    db.refresh(wiki)
    assert wiki.current_revision_id == first_rev
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 1
    assert len(calls) == 2  # attempt1 graph 失败 + retry 成功
    # retry 不产第二条 manifest。
    assert db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    ).count() == 1


# ---------------------------------------------------------------------------
# 19. 输入顺序不影响 Section 与 Binding 的确定性结果
# ---------------------------------------------------------------------------


def test_input_order_does_not_change_sections_and_bindings(db):
    _bootstrap_builtin()
    from app.core.wiki_skills.api_reference.compiler import (
        ApiSourceDocument,
        compile_api_reference,
    )

    spec_a = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "用户 API", "version": "1.0",
                 "description": "GET /api/users 返回 200 成功。"},
        "paths": {"/api/users": {"get": {
            "summary": "用户列表", "description": "返回 200 成功。",
            "responses": {"200": {"description": "ok"}},
        }}},
    }, ensure_ascii=False)
    spec_b = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "订单 API", "version": "2.0",
                 "description": "POST /api/orders 返回 201 成功。"},
        "paths": {"/api/orders": {"post": {
            "summary": "创建订单", "description": "返回 201 成功。",
            "responses": {"201": {"description": "ok"}},
        }}},
    }, ensure_ascii=False)

    def _doc(page, spec, eid, scope):
        return ApiSourceDocument(
            source_page_id=page, format="openapi_json", content=spec,
            version_scope=scope,
            evidence=({"evidence_id": eid, "source_page_id": page,
                       "status": "active", "locator": {"section": "all"}},))

    d1, d2 = _doc("p1", spec_a, "e1", "v1"), _doc("p2", spec_b, "e2", "v2")
    ra = compile_api_reference([d1, d2])
    rb = compile_api_reference([d2, d1])
    sa = v3mod._derive_persist_sections(ra)
    sb = v3mod._derive_persist_sections(rb)
    assert sa == sb
    # 同一输入不同顺序下，每个 Section 的 key/content_hash/结构/bindings 完全一致。
    a_map = {s["section_key"]: s for s in sa}
    b_map = {s["section_key"]: s for s in sb}
    assert set(a_map) == set(b_map)
    for key in a_map:
        assert a_map[key]["content_hash"] == b_map[key]["content_hash"]
        assert a_map[key]["structure"] == b_map[key]["structure"]
        assert a_map[key]["bindings"] == b_map[key]["bindings"]


# ---------------------------------------------------------------------------
# 20. 不调用 _legacy_*
# ---------------------------------------------------------------------------


def test_no_legacy_calls_for_default_and_api(db, monkeypatch):
    legacy_targets = [
        "app.core.knowledge_compiler_v3.wiki_page_builder",
        "app.core.knowledge_compiler_v3.wiki_refresh_scheduler",
        "app.core.wiki_pipeline.pipelines.multi_page",
    ]
    patched = []

    def _boom(*a, **k):
        raise AssertionError("legacy code must not be called in v3")

    for mod_name in legacy_targets:
        import importlib

        mod = importlib.import_module(mod_name)
        for name in list(vars(mod)):
            if name.startswith("_legacy_"):
                monkeypatch.setattr(mod, name, _boom)
                patched.append(name)
    assert patched, "expected some _legacy_* to patch"

    # default 单目标（manual_rebuild）走 v3 → v1 复用，不经 legacy。
    _bootstrap_default()
    ws = _mk_ws(db, "nb-1")
    page = _mk_page(db, ws, "p1", title="功能说明", spec="内容：" + "x" * 60)
    wiki = _mk_wiki(db, ws, "wk1", title="主题A", page_ids=["p1"], dirty=True)
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

    # api_reference 单目标同样不触碰 legacy。
    ws2 = _mk_ws(db, "nb-2")
    page2 = _mk_page(db, ws2, "p2")
    _mk_evidence(db, page2, "ev1")
    wiki2 = _mk_wiki(db, ws2, "wk2", page_ids=["p2"], dirty=True)
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    run2 = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=wiki2.id,
        workspace_id=ws2.id, wiki_page_id=wiki2.id,
    )
    db.commit()
    executed2 = executor.execute_run(db, run2.id)
    db.refresh(executed2)
    assert executed2.status == "succeeded", (executed2.safe_error_code,
                                             executed2.safe_error_message)


def test_safe_messages_from_registry_only(db):
    """安全文案唯一来源 = registry；v3 无本地重复映射；结果不含异常/路径/Token。"""
    from app.core.wiki_pipeline import registry as pregs_mod

    assert not hasattr(v3mod, "_SAFE_ERROR_MESSAGES")
    fixed = v3mod._safe_error("VALIDATION_FAILED")["error_message"]
    assert fixed == pregs_mod.stage_error_message("VALIDATION_FAILED")
    assert fixed and "Traceback" not in fixed
    unknown = v3mod._safe_error("SOME_UNKNOWN_CODE_XYZ")["error_message"]
    assert unknown == pregs_mod.stage_error_message("SOME_UNKNOWN_CODE_XYZ")

    # 行为级：正文/证据中的 token 与绝对路径只留在来源侧，安全字段不泄漏。
    marker = "TOKEN_V3_LEAK_12345"
    path = r"C:\Users\secret\v3\leak.md"
    _bootstrap_builtin()
    ws = _mk_ws(db)
    page = _mk_page(db, ws, "p1", spec="# 普通叙述\n没有接口。\n" + marker + "\n" + path)
    _mk_evidence(db, page, "ev1", content=marker + path)
    wiki = _mk_wiki(db, ws, "wk1", page_ids=["p1"], dirty=True,
                    content_skill="api_reference", skill_version="1",
                    skill_locked=True)
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
    assert executed.status == "failed"
    assert executed.safe_error_code == "VALIDATION_FAILED"
    assert executed.safe_error_message == pregs_mod.stage_error_message("VALIDATION_FAILED")
    assert marker not in (executed.safe_error_message or "")
    assert path not in (executed.safe_error_message or "")
    assert "Traceback" not in (executed.safe_error_message or "")
    for art in db.query(Artifact).filter(Artifact.run_id == run.id).all():
        blob = art.payload_json or ""
        assert marker not in blob and path not in blob
    for row in db.query(StageRun).filter(StageRun.run_id == run.id).all():
        for blob in (row.safe_error_message or "", row.error_message or ""):
            assert marker not in blob and path not in blob
            assert "Traceback" not in blob
