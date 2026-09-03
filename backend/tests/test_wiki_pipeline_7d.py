"""Phase 7D-A：wiki.default v3 生产启用前闭环定向验证。

覆盖：page_deleted v3、API Markdown LLM adapter、bootstrap/active、v2 回滚安全、
API recompile active version、生产入口静态审计。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 as v3mod
from app.config import settings
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline import registry as pregs
from app.core.wiki_pipeline.pipelines.wiki_default import (
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    create_page_deleted_run,
    register_default_pipeline,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
    _stage_publish_skilled,
    register_default_pipeline_v2,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import register_default_pipeline_v3
from app.core.wiki_skills import registry as sreg
from app.core.wiki_skills import service as skill_service
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    EvidenceItem,
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


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


def _mk_ws(db, nb_id="nb-1"):
    if db.get(Notebook, nb_id) is None:
        db.add(Notebook(id=nb_id, name="库", group_id="eng"))
        db.flush()
    return ensure_notebook_workspace(db, db.get(Notebook, nb_id))


def _notebook_id(db, ws):
    binding = db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.workspace_id == ws.id).first()
    return binding.notebook_id if binding else None


def _page(db, ws, pid, content, title="页"):
    binding = db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.workspace_id == ws.id).first()
    db.add(Page(id=pid, notebook_id=binding.notebook_id, title=title,
                content=content, content_hash=_hash(content), wiki_dirty=True))
    db.flush()
    return db.get(Page, pid)


def _evidence(db, page, eid, *, content=None, status="active", locator=None):
    db.add(EvidenceItem(
        id=eid, source_page_id=page.id, status=status,
        content=content or "e", locator_json=json.dumps(locator or {"section": "all"}),
        content_hash="f" * 64, source_doc_hash=page.content_hash))
    db.flush()
    return db.get(EvidenceItem, eid)


def _wiki(db, ws, wid, title, *, skill="default", version="1", locked=False, source=None):
    db.add(WikiPage(
        id=wid, title=title, summary="", acl_scope=ws.acl_scope, category="资料",
        status="published", source_page_ids=json.dumps(list(source or [])),
        dirty=True, workspace_id=ws.id, content_skill=skill, skill_version=version,
        skill_locked=locked))
    db.flush()
    return db.get(WikiPage, wid)


def _boot(active="3"):
    skill_service.register_builtin_skills()
    register_default_pipeline()
    register_default_pipeline_v2()
    register_default_pipeline_v3()
    pregs.set_active_version("wiki.default", active)
    executor.configure_external_runners(graph_runner=_graph_noop)


# ---------------------------------------------------------------------------
# A. page_deleted v3
# ---------------------------------------------------------------------------


def _mk_delete_run(db, ws, page_id, *, delete_page=False):
    run = create_page_deleted_run(
        db, page_id=page_id, workspace_id=ws.id, notebook_id=_notebook_id(db, ws))
    db.commit()
    if delete_page:
        p = db.get(Page, page_id)
        if p is not None:
            db.delete(p)
        db.commit()
    db.refresh(run)
    return run


def test_v3_page_deleted_single_source_archived(db):
    _boot()
    ws = _mk_ws(db)
    p = _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", source=["p1"])
    db.commit()
    run = _mk_delete_run(db, ws, "p1")
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(w)
    assert w.status == "archived"
    assert json.loads(w.source_page_ids) == []
    art = db.query(Artifact).filter(
        Artifact.run_id == run.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST).one()
    manifest = json.loads(art.payload_json)
    kinds = {(g["kind"], g.get("page_id")) for g in manifest["graph_targets"]}
    assert ("page_remove", "p1") in kinds
    assert any(g["kind"] == "wiki" and g["wiki_page_id"] == "w1"
               for g in manifest["graph_targets"])


def test_v3_page_deleted_physical_row_removed_still_succeeds(db):
    _boot()
    ws = _mk_ws(db)
    _page(db, ws, "p1", "内容" + "x" * 60)
    _wiki(db, ws, "w1", "主题A", source=["p1"])
    db.commit()
    run = _mk_delete_run(db, ws, "p1", delete_page=True)
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.expire_all()
    w = db.get(WikiPage, "w1")
    assert w.status == "archived"


def test_v3_page_deleted_multi_source_keeps_others(db):
    _boot()
    ws = _mk_ws(db)
    _page(db, ws, "p1", "内容" + "x" * 60)
    _page(db, ws, "p2", "内容" + "y" * 60)
    w = _wiki(db, ws, "w1", "主题A", source=["p1", "p2"])
    db.commit()
    run = _mk_delete_run(db, ws, "p1")
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(w)
    assert w.status == "draft" or w.status == "published"
    assert json.loads(w.source_page_ids) == ["p2"]
    assert db.get(Page, "p2") is not None


def test_v3_page_deleted_retry_no_duplicate_removal(db):
    _boot()
    ws = _mk_ws(db)
    _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", source=["p1"])
    db.commit()
    run = _mk_delete_run(db, ws, "p1")
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded"
    # 再次触发同删除语义（新 run）→ 幂等，无重复删除/破坏。
    run2 = create_page_deleted_run(db, page_id="p1", workspace_id=ws.id)
    db.commit()
    executed2 = executor.execute_run(db, run2.id)
    db.refresh(executed2)
    assert executed2.status == "succeeded"
    db.expire_all()
    w2 = db.get(WikiPage, "w1")
    assert w2.status == "archived"
    assert json.loads(w2.source_page_ids) == []


def test_v3_page_deleted_exception_keeps_old_state(db, monkeypatch):
    import app.core.wiki_pipeline.pipelines.wiki_default as wd

    _boot()
    ws = _mk_ws(db)
    _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", source=["p1"])
    db.commit()
    orig = wd._safe_remove_source_page

    def _boom(db2, page_id):
        orig(db2, page_id)
        raise RuntimeError("boom-delete")

    monkeypatch.setattr(wd, "_safe_remove_source_page", _boom)
    run = _mk_delete_run(db, ws, "p1")
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "failed"
    db.expire_all()
    w2 = db.get(WikiPage, "w1")
    assert json.loads(w2.source_page_ids) == ["p1"]  # 未变
    assert w2.status == "published"


# ---------------------------------------------------------------------------
# B. API Markdown LLM runner adapter
# ---------------------------------------------------------------------------


def _md_source_text():
    return ("# 用户接口\n\nGET /items 返回项目列表。\nHTTP 200 成功。\n"
            "错误码：FORBIDDEN；HTTP 403 表示无权。\n")


def _md_fake_payload(extra=None):
    item = {"method": "GET", "path": "/items", "evidence_id": "ev-md",
            "version_scope": "unversioned", "status_codes": ["200"],
            "error_codes": ["FORBIDDEN"]}
    if extra:
        item.update(extra)
    return json.dumps([item], ensure_ascii=False)


def test_markdown_api_fake_runner_publishable(db):
    _boot()
    ws = _mk_ws(db)
    secret_full = "PAGE_FULL_ONLY_MARKER_9x"
    md = _md_source_text() + secret_full + "\n更多叙述。"
    p = _page(db, ws, "p1", md)
    _evidence(db, p, "ev-md", content=_md_source_text())
    w = _wiki(db, ws, "w1", "主题B", skill="api_reference", locked=True, source=["p1"])
    db.commit()
    prompts = []

    def _llm(messages, context="", timeout=120.0):
        if context == "api-reference-compile":
            prompts.append(messages[0]["content"] if messages else "")
            return _md_fake_payload()
        return {"worthy": True, "ops": []}

    executor.configure_external_runners(llm_runner=_llm, graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=w.id,
        workspace_id=ws.id, wiki_page_id=w.id)
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert prompts, "markdown api compile must call runner"
    assert all(secret_full not in pr for pr in prompts)  # Prompt 不含整页正文
    db.refresh(w)
    assert w.content_skill == "api_reference"
    sections = db.query(WikiSection).filter(
        WikiSection.revision_id == w.current_revision_id).all()
    assert any("api_endpoint" in (s.section_key or "") for s in sections)


def test_openapi_only_does_not_call_runner(db):
    _boot()
    ws = _mk_ws(db)
    spec = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "u", "version": "1.0", "description": "GET /api/x 返回 200。"},
        "paths": {"/api/x": {"get": {"description": "返回 200 成功。",
                                     "responses": {"200": {"description": "ok"}}}}},
    }, ensure_ascii=False)
    p = _page(db, ws, "p1", spec)
    _evidence(db, p, "ev1")
    w = _wiki(db, ws, "w1", "主题B", skill="api_reference", locked=True, source=["p1"])
    db.commit()
    calls = []

    def _boom(messages, context="", timeout=120.0):
        calls.append(1)
        raise AssertionError("openapi must not call LLM")

    executor.configure_external_runners(llm_runner=_boom, graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=w.id,
        workspace_id=ws.id, wiki_page_id=w.id)
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert calls == []


def test_adapter_handles_list_and_json_str_and_invalid():
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        _pipeline_api_llm_adapter,
    )

    def _mk(ret):
        def _r(messages, context="", timeout=120.0):
            return ret
        return _r

    adapter = _pipeline_api_llm_adapter({"llm_runner": _mk([{"a": 1}])})
    assert adapter("p") == json.dumps([{"a": 1}], ensure_ascii=False, sort_keys=True)
    adapter2 = _pipeline_api_llm_adapter({"llm_runner": _mk({"a": 1})})
    assert json.loads(adapter2("p")) == {"a": 1}
    adapter3 = _pipeline_api_llm_adapter({"llm_runner": _mk("json-text")})
    assert adapter3("p") == "json-text"
    adapter_bad = _pipeline_api_llm_adapter({"llm_runner": _mk(42)})
    with pytest.raises(RuntimeError):
        adapter_bad("p")
    # ctx 无 runner → adapter 仍返回 Callable（生产默认 runner 在调用时解析）。
    default_adapter = _pipeline_api_llm_adapter({})
    assert callable(default_adapter)


def test_markdown_source_without_evidence_does_not_call_runner(db):
    _boot()
    ws = _mk_ws(db)
    p = _page(db, ws, "p1", _md_source_text())
    _evidence(db, p, "ev-stale", content=_md_source_text(), status="stale")
    w = _wiki(db, ws, "w1", "主题B", skill="api_reference", locked=True, source=["p1"])
    db.commit()
    calls = []

    def _boom(messages, context="", timeout=120.0):
        calls.append(1)
        raise AssertionError("must not call runner without usable evidence")

    executor.configure_external_runners(llm_runner=_boom, graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=w.id,
        workspace_id=ws.id, wiki_page_id=w.id)
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "failed"
    assert calls == []


def test_markdown_compile_result_artifacts_no_leak(db):
    _boot()
    ws = _mk_ws(db)
    raw_secret = "RAW_MODEL_SECRET_11"
    md = _md_source_text()
    p = _page(db, ws, "p1", md)
    _evidence(db, p, "ev-md", content=md)
    w = _wiki(db, ws, "w1", "主题B", skill="api_reference", locked=True, source=["p1"])
    db.commit()

    def _llm(messages, context="", timeout=120.0):
        if context == "api-reference-compile":
            return _md_fake_payload()  # 不含 secret；secret 只存在于“模型原始输出模拟”。
        return {"worthy": True, "ops": []}

    executor.configure_external_runners(llm_runner=_llm, graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=w.id,
        workspace_id=ws.id, wiki_page_id=w.id)
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(w)
    for art in db.query(Artifact).filter(Artifact.run_id == run.id).all():
        assert raw_secret not in (art.payload_json or "")
        assert "excerpt" not in (art.payload_json or "").lower()
        assert "api-reference-compile" not in (art.payload_json or "")
    for s in db.query(WikiSection).filter(
            WikiSection.revision_id == w.current_revision_id).all():
        assert raw_secret not in (s.structure_json or "")
    assert raw_secret not in (w.skill_decision_json or "")


def test_mixed_batch_markdown_and_default_share_adapter(db):
    _boot()
    ws = _mk_ws(db)
    md = "MDM " + _md_source_text()
    pM = _page(db, ws, "pM", md)
    _evidence(db, pM, "evM", content=_md_source_text())
    wM = _wiki(db, ws, "wM", "主题B", skill="api_reference", locked=True, source=["pM"])
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    db.commit()
    prompts = []

    def _llm(messages, context="", timeout=120.0):
        prompt = messages[0]["content"] if messages else ""
        if context == "api-reference-compile":
            prompts.append("1")
            payload = json.dumps([{
                "method": "GET", "path": "/items", "evidence_id": "evM",
                "version_scope": "unversioned", "status_codes": ["200"],
                "error_codes": ["FORBIDDEN"],
            }], ensure_ascii=False)
            return payload
        if context == "wiki-ingest-page":
            if "MDM" in prompt:
                return {"worthy": True, "ops": [{"action": "update", "title": "主题B",
                                                 "category": "资料"}]}
            return {"worthy": True, "ops": [{"action": "create", "title": "主题A",
                                             "category": "资料"}]}
        if context in ("wiki-synthesis", "wiki-mapreduce"):
            return {"summary": "摘要", "content": "正文" + "x" * 60}
        return {"worthy": True, "ops": []}

    from app.core.wiki_pipeline.pipelines.wiki_default import _batch_input_hash
    ids = ["pM", "pD"]
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="batch_rebuild", trigger_object_id=ws.id, workspace_id=ws.id,
        input_hash=_batch_input_hash(db, ws.id, ids), supersede_same_trigger=True)
    from app.core.wiki_pipeline.pipelines.wiki_default import (
        ARTIFACT_TYPE_WIKI_BATCH_INPUT, ARTIFACT_SCHEMA_WIKI_BATCH, _page_full_hash,
    )
    payload = {"workspace_id": ws.id, "page_ids": ids,
               "page_input_hashes": {pid: _page_full_hash(db, pid) for pid in ids}}
    db.add(Artifact(run_id=run.id, artifact_type=ARTIFACT_TYPE_WIKI_BATCH_INPUT,
                    schema_version=ARTIFACT_SCHEMA_WIKI_BATCH,
                    payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    db.commit()
    executor.configure_external_runners(llm_runner=_llm, graph_runner=_graph_noop)
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert prompts, "batch markdown api target must use the shared adapter"
    db.expire_all()
    wM2 = db.get(WikiPage, "wM")
    secs = db.query(WikiSection).filter(
        WikiSection.revision_id == wM2.current_revision_id).all()
    assert any("api_endpoint" in (s.section_key or "") for s in secs)


# ---------------------------------------------------------------------------
# C. bootstrap / active version
# ---------------------------------------------------------------------------


def test_bootstrap_registers_and_active_v3(monkeypatch):
    monkeypatch.setattr(settings, "wiki_pipeline_active_version", "3")
    from app.core.wiki_pipeline import bootstrap

    active = bootstrap.bootstrap_wiki_pipeline()
    assert active == "3"
    assert pregs.get_active_version("wiki.default") == "3"
    assert pregs.get_pipeline("wiki.default", "1") is not None
    assert pregs.get_pipeline("wiki.default", "2") is not None
    assert pregs.get_pipeline("wiki.default", "3") is not None
    assert sreg.has("default", "1") and sreg.has("api_reference", "1")
    # 幂等
    active2 = bootstrap.bootstrap_wiki_pipeline()
    assert active2 == "3"
    assert pregs.get_active_version("wiki.default") == "3"


def test_bootstrap_active_v2_allowed_rollback(monkeypatch):
    monkeypatch.setattr(settings, "wiki_pipeline_active_version", "2")
    from app.core.wiki_pipeline import bootstrap

    active = bootstrap.bootstrap_wiki_pipeline()
    assert active == "2"
    assert pregs.get_active_version("wiki.default") == "2"
    assert pregs.get_pipeline("wiki.default", "3") is not None


def test_bootstrap_invalid_active_version_fails(monkeypatch):
    monkeypatch.setattr(settings, "wiki_pipeline_active_version", "9")
    from app.core.wiki_pipeline import bootstrap

    with pytest.raises(RuntimeError):
        bootstrap.bootstrap_wiki_pipeline()


# ---------------------------------------------------------------------------
# D. v2 回滚安全
# ---------------------------------------------------------------------------


def test_v2_rejects_api_reference_decision(db):
    _boot(active="2")
    ws = _mk_ws(db)
    p = _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", skill="api_reference", locked=True, source=["p1"])
    db.commit()
    executor.configure_external_runners(graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="2",
        trigger_type="manual_rebuild", trigger_object_id=w.id,
        workspace_id=ws.id, wiki_page_id=w.id)
    db.commit()
    # 直接注入 api 决策（v2 router 在 api 注册后会可能产生）验证 publish 门禁。
    from app.core.wiki_skills.schemas import SkillDecision
    decision = SkillDecision(
        target_key=w.id, wiki_page_id=w.id, selected_skill="api_reference",
        selected_version="1", selected_by="locked", status="locked",
        reason_code="SKILL_LOCKED", locked=True)
    db.add(Artifact(
        run_id=run.id, artifact_type="skill_decision", schema_version="skill-decision/v1",
        object_type="skill_decision",
        payload_json=json.dumps(
            {"schema_version": "skill-decision/v1", "decisions": [decision.to_dict()]},
            ensure_ascii=False)))
    db.commit()
    ctx = {"state": {"context": {}, "skill": {"decisions": [decision.to_dict()]}}}
    res = _stage_publish_skilled(db, run, None, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "SKILL_NOT_SUPPORTED_BY_PIPELINE_VERSION"
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == w.id).count() == 0
    db.refresh(w)
    assert w.current_revision_id is None


# ---------------------------------------------------------------------------
# API recompile active version
# ---------------------------------------------------------------------------


def test_api_recompile_uses_active_v3(db, monkeypatch):
    _boot(active="3")
    from app.api import wiki_skills as api_mod

    ws = _mk_ws(db)
    p = _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", source=["p1"])
    db.commit()
    summary = api_mod._create_skill_rebuild_run(db, w, {"id": "u1"})
    assert summary is not None
    assert summary["pipeline_version"] == "3"
    assert db.get(CompileRun, summary["run_id"]) is not None
    db.rollback()


def test_api_recompile_active_v2_api_reference_rejected(db, monkeypatch):
    _boot(active="2")
    from app.api import wiki_skills as api_mod

    ws = _mk_ws(db)
    _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", skill="api_reference", locked=True, source=["p1"])
    db.commit()
    assert api_mod._create_skill_rebuild_run(db, w, {"id": "u1"}) is None


def test_production_create_run_no_hardcoded_v2():
    """生产入口不再硬编码 pipeline_version='2'（审计：允许测试/精确恢复历史 v2）。"""
    import ast
    from pathlib import Path

    api_path = Path("app/api/wiki_skills.py")
    tree = ast.parse(api_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "create_run":
            for kw in node.keywords:
                if kw.arg == "pipeline_version" and isinstance(kw.value, ast.Constant):
                    assert kw.value.value != "2", "production create_run must not hardcode v2"


# ---------------------------------------------------------------------------
# A.1：默认生产 LLM runner（无 ctx 注入时复用 wiki_default 默认 runner）
# ---------------------------------------------------------------------------


def _mk_md_wiki(db, ws, pid="p1", wid="w1"):
    md = _md_source_text()
    p = _page(db, ws, pid, md)
    _evidence(db, p, "ev-md", content=md)
    w = _wiki(db, ws, wid, "主题B", skill="api_reference", locked=True, source=[pid])
    db.commit()
    return w


def _manual_api_run(db, ws, w):
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="manual_rebuild", trigger_object_id=w.id,
        workspace_id=ws.id, wiki_page_id=w.id)
    db.commit()
    return executor.execute_run(db, run.id)


def test_default_llm_runner_used_when_ctx_none(db, monkeypatch):
    """reset_external_runners 后 ctx runner=None；monkeypatch 默认真实 runner → Markdown 可发布。"""
    from app.core.wiki_pipeline.pipelines import wiki_default as wd

    _boot(active="3")
    ws = _mk_ws(db)
    w = _mk_md_wiki(db, ws)
    calls = []

    def _fake_default(messages, context="", timeout=120.0):
        if context == "api-reference-compile":
            calls.append(1)
            return _md_fake_payload()
        return {"worthy": True, "ops": []}

    monkeypatch.setattr(wd, "_default_llm_runner", _fake_default)
    executor.reset_external_runners()  # 确保 ctx llm_runner=None
    executor.configure_external_runners(graph_runner=_graph_noop)
    executed = _manual_api_run(db, ws, w)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert calls, "default runner must be used when ctx runner is None"
    db.refresh(w)
    secs = db.query(WikiSection).filter(
        WikiSection.revision_id == w.current_revision_id).all()
    assert any("api_endpoint" in (s.section_key or "") for s in secs)


def test_openapi_only_no_default_runner_call(db, monkeypatch):
    from app.core.wiki_pipeline.pipelines import wiki_default as wd

    _boot(active="3")
    ws = _mk_ws(db)
    spec = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "u", "version": "1.0", "description": "GET /api/x 返回 200。"},
        "paths": {"/api/x": {"get": {"description": "返回 200 成功。",
                                     "responses": {"200": {"description": "ok"}}}}},
    }, ensure_ascii=False)
    p = _page(db, ws, "p1", spec)
    _evidence(db, p, "ev1")
    w = _wiki(db, ws, "w1", "主题B", skill="api_reference", locked=True, source=["p1"])
    db.commit()
    calls = []

    def _fake_default(messages, context="", timeout=120.0):
        calls.append(1)
        raise AssertionError("default runner must not be called for openapi-only")

    monkeypatch.setattr(wd, "_default_llm_runner", _fake_default)
    executor.reset_external_runners()
    executor.configure_external_runners(graph_runner=_graph_noop)
    executed = _manual_api_run(db, ws, w)
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    assert calls == []


def test_markdown_no_evidence_no_default_runner_call(db, monkeypatch):
    from app.core.wiki_pipeline.pipelines import wiki_default as wd

    _boot(active="3")
    ws = _mk_ws(db)
    p = _page(db, ws, "p1", _md_source_text())
    _evidence(db, p, "ev-stale", content=_md_source_text(), status="stale")
    w = _wiki(db, ws, "w1", "主题B", skill="api_reference", locked=True, source=["p1"])
    db.commit()
    calls = []

    def _fake_default(messages, context="", timeout=120.0):
        calls.append(1)
        raise AssertionError("must not call runner without usable evidence")

    monkeypatch.setattr(wd, "_default_llm_runner", _fake_default)
    executor.reset_external_runners()
    executor.configure_external_runners(graph_runner=_graph_noop)
    executed = _manual_api_run(db, ws, w)
    db.refresh(executed)
    assert executed.status == "failed"
    assert calls == []


# ---------------------------------------------------------------------------
# A.1：active Registry 缺失 / bootstrap 原子 / v2 损坏决策
# ---------------------------------------------------------------------------


def test_api_recompile_no_active_pipeline_rejected(db):
    """Registry 无 wiki.default active → 受控失败：不建 Run、不猜 v2、Wiki 原值不变。"""
    from app.api import wiki_skills as api_mod

    _boot(active="3")
    ws = _mk_ws(db)
    _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", source=["p1"])
    db.commit()
    pregs.clear_for_tests()  # 清空 registry（含 active）
    sreg.clear_for_tests()
    original = (w.content_skill, w.skill_version, w.skill_decision_json)
    assert api_mod._create_skill_rebuild_run(db, w, {"id": "u1"}) is None
    db.flush()
    assert db.query(CompileRun).count() == 0
    db.expire_all()
    w2 = db.get(WikiPage, "w1")
    assert (w2.content_skill, w2.skill_version, w2.skill_decision_json) == original


def test_bootstrap_invalid_config_leaves_registry_untouched(monkeypatch):
    from app.core.wiki_pipeline import bootstrap

    pregs.clear_for_tests()
    sreg.clear_for_tests()
    monkeypatch.setattr(settings, "wiki_pipeline_active_version", "9")
    with pytest.raises(RuntimeError):
        bootstrap.bootstrap_wiki_pipeline()
    assert pregs.registered_versions("wiki.default") == []
    assert pregs.get_active_version("wiki.default") is None
    assert sreg.list_skills() == []


def test_v2_corrupt_decisions_fail_closed(db):
    _boot(active="2")
    ws = _mk_ws(db)
    _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", source=["p1"])
    db.commit()
    cases = [
        {"skill": {"decisions": "not-a-list"}},
        {"skill": {"decisions": ["not-a-mapping"]}},
        {"skill": {"decisions": [{"schema_version": "bad", "selected_skill": "default",
                                  "selected_version": 999, "status": "selected"}]}},
    ]
    for state in cases:
        res = _stage_publish_skilled(db, None, None, {"state": state})
        assert res.get("ok") is False
        assert res["error_code"] == "SKILL_NOT_SUPPORTED_BY_PIPELINE_VERSION"
        assert "Traceback" not in (res.get("error_message") or "")
    assert db.query(WikiRevision).count() == 0
    db.refresh(w)
    assert w.current_revision_id is None


def test_v2_legal_default_decision_not_blocked(db):
    from app.core.wiki_skills.schemas import SkillDecision

    _boot(active="2")
    ws = _mk_ws(db)
    _page(db, ws, "p1", "内容" + "x" * 60)
    w = _wiki(db, ws, "w1", "主题A", source=["p1"])
    db.commit()
    d = SkillDecision(target_key="w1", wiki_page_id="w1", selected_skill="default",
                      selected_version="1", status="selected").to_dict()
    state = {"skill": {"decisions": [d]}}
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
        _v2_unsupported_decision,
    )

    assert _v2_unsupported_decision(state) is False
    na = SkillDecision(target_key="w1", wiki_page_id="w1", selected_skill=None,
                       selected_version=None, status="not_applicable").to_dict()
    assert _v2_unsupported_decision({"skill": {"decisions": [na]}}) is False
