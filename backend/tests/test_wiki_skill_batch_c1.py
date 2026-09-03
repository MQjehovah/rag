"""Phase 7C.3-C.1：mixed batch 身份/输入/retry 一致性封板反例。"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.wiki_pipeline.pipelines.wiki_skilled_batch_v3 as bmod
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline import registry as pregs
from app.core.wiki_pipeline.pipelines.wiki_default import (
    ARTIFACT_TYPE_WIKI_BATCH_INPUT,
    ARTIFACT_SCHEMA_WIKI_BATCH,
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    register_default_pipeline,
)
from app.core.wiki_pipeline.pipelines.wiki_skilled_default import register_default_pipeline_v2
from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import register_default_pipeline_v3
from app.core.wiki_skills import registry as sreg
from app.core.wiki_skills import service as skill_service
from app.core.wiki_skills.schemas import SkillDecision
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    EvidenceItem,
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    WikiWorkspace,
    init_db,
)


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _graph_noop(**kw):
    return None


def _normalize(title):
    from app.core.knowledge_compiler_v3.wiki_page_builder import normalize_wiki_title

    return normalize_wiki_title(title or "")


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


def _bootstrap():
    skill_service.register_builtin_skills()
    register_default_pipeline()
    register_default_pipeline_v2()
    register_default_pipeline_v3()


def _mk_ws(db, nb_id="nb-1"):
    if db.get(Notebook, nb_id) is None:
        db.add(Notebook(id=nb_id, name="库", group_id="eng"))
        db.flush()
    return ensure_notebook_workspace(db, db.get(Notebook, nb_id))


def _page(db, ws, pid, content, title="页", bind=True):
    binding = (db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.workspace_id == ws.id).first() if bind else None)
    nb = binding.notebook_id if binding is not None else None
    db.add(Page(id=pid, notebook_id=nb, title=title, content=content,
                content_hash=_hash(content), wiki_dirty=True))
    db.flush()
    return db.get(Page, pid)


def _evidence(db, page, eid, status="active", content=None):
    db.add(EvidenceItem(
        id=eid, source_page_id=page.id, status=status, content=content or "e",
        locator_json='{"section":"all"}', content_hash="f" * 64,
        source_doc_hash=page.content_hash))
    db.flush()
    return db.get(EvidenceItem, eid)


def _wiki(db, ws, wid, title, *, skill, version="1", locked=False, source=None):
    db.add(WikiPage(
        id=wid, title=title, summary="", acl_scope=ws.acl_scope, category="资料",
        status="published", source_page_ids=json.dumps(list(source or [])),
        dirty=True, workspace_id=ws.id, content_skill=skill, skill_version=version,
        skill_locked=locked))
    db.flush()
    return db.get(WikiPage, wid)


def _api_spec(marker="MARK_API"):
    return json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "u", "version": "1.0",
                 "description": marker + " GET /api/users 返回 200 成功。"},
        "paths": {"/api/users": {"get": {
            "description": "返回 200 成功。",
            "responses": {"200": {"description": "ok"}},
        }}},
    }, ensure_ascii=False)


def _mk_llm(*, default_body="正文" + "x" * 60, default_empty=False):
    def _run(messages, context="", timeout=120.0):
        prompt = messages[0]["content"] if messages else ""
        if context == "wiki-ingest-page":
            if "MARK_API" in prompt:
                return {"worthy": True, "ops": [{"action": "update",
                                                 "title": "主题B", "category": "资料"}]}
            return {"worthy": True, "ops": [{"action": "create", "title": "主题A",
                                             "category": "资料"}]}
        if context in ("wiki-synthesis", "wiki-mapreduce"):
            if default_empty:
                return {"summary": "摘要"}
            return {"summary": "摘要", "content": default_body}
        return {"worthy": True, "ops": []}
    return _run


def _create_batch_run_v3(db, ws_id, page_ids):
    from app.core.wiki_pipeline.pipelines.wiki_default import _batch_input_hash

    ids = sorted({p for p in page_ids})
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="batch_rebuild", trigger_object_id=ws_id, workspace_id=ws_id,
        input_hash=_batch_input_hash(db, ws_id, ids), supersede_same_trigger=True)
    from app.core.wiki_pipeline.pipelines.wiki_default import _page_full_hash

    payload = {"workspace_id": ws_id, "page_ids": ids,
               "page_input_hashes": {pid: _page_full_hash(db, pid) for pid in ids}}
    db.add(Artifact(run_id=run.id, artifact_type=ARTIFACT_TYPE_WIKI_BATCH_INPUT,
                    schema_version=ARTIFACT_SCHEMA_WIKI_BATCH,
                    payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    db.commit()
    db.refresh(run)
    return run


def _run_batch(db, ws, page_ids):
    run = _create_batch_run_v3(db, ws.id, page_ids)
    return executor.execute_run(db, run.id)


def _latest_manifest(db, run_id):
    art = db.query(Artifact).filter(
        Artifact.run_id == run_id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    ).order_by(Artifact.created_at.desc()).first()
    return json.loads(art.payload_json) if art else None


# ---------------------------------------------------------------------------
# 1-3. composite identity
# ---------------------------------------------------------------------------


def test_planner_composite_identity_distinct_scope_and_wiki(db):
    """同 normalized title、不同 ACL → 两个独立 target，可分别选择 default/API。"""
    _bootstrap()
    ws = WikiWorkspace(id="wsx", key="wsx", name="w", acl_scope="company",
                       scope_id="company")
    db.add(ws)
    db.flush()
    scope_a = json.dumps({"groups": ["a"]}, sort_keys=True)
    scope_b = json.dumps({"groups": ["b"]}, sort_keys=True)
    pa = _page(db, ws, "pA", "普通内容A足够长用于测试构建。", bind=False)
    pb = _page(db, ws, "pB", _api_spec("MARK_API") + "b", bind=False)
    # 同标题，不同 scope：一个既有 default wiki（scope a），另一个不存在。
    _wiki(db, ws, "wkA", "主题X", skill="default", version="1", locked=True,
          source=["pA"])
    ws.acl_scope = "company"
    # 让既有 wiki 的 acl 与 scope_a 一致。
    db.query(WikiPage).filter(WikiPage.id == "wkA").update(
        {"acl_scope": scope_a})
    db.commit()

    context = {"workspace_id": ws.id, "page_input_hashes": {}}
    batch_pages = [
        {"page_id": "pA", "status": "create_update",
         "ops": [{"action": "update", "title": "主题X", "category": "资料"}],
         "scope_acl_json": scope_a, "workspace_id": ws.id},
        {"page_id": "pB", "status": "create_update",
         "ops": [{"action": "create", "title": "主题X", "category": "资料"}],
         "scope_acl_json": scope_b, "workspace_id": ws.id},
    ]
    key_a = bmod._batch_key(scope_a, _normalize("主题X"))
    key_b = bmod._batch_key(scope_b, _normalize("主题X"))
    d_a = SkillDecision(target_key=key_a, wiki_page_id="wkA",
                        selected_skill="default", selected_version="1",
                        selected_by="locked", status="locked",
                        reason_code="SKILL_LOCKED", locked=True)
    d_b = SkillDecision(target_key=key_b, wiki_page_id=None,
                        selected_skill="api_reference", selected_version="1",
                        selected_by="auto", status="selected")
    targets, fatal = bmod._plan_batch_targets_v3(db, context, batch_pages, [d_a, d_b])
    assert fatal is None
    assert len(targets) == 2
    by_key = {t["key"]: t for t in targets}
    assert by_key[key_a]["branch"] == "default"
    assert by_key[key_a]["existing_wiki_id"] == "wkA"
    assert by_key[key_b]["branch"] == "api_reference"
    assert by_key[key_b]["existing_wiki_id"] is None
    # 同标题不同 scope 不会匹配到同一个既有 wiki。
    assert by_key[key_a]["existing_wiki_id"] != by_key[key_b]["existing_wiki_id"]


def test_skill_route_batch_decision_target_key_is_composite(db):
    """batch 持久化 SkillDecision.target_key = scope\x1fnorm composite key。"""
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API")
    _evidence(db, pA, "evA")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True, source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    for art in db.query(Artifact).filter(
            Artifact.run_id == executed.id,
            Artifact.artifact_type == "skill_decision").all():
        payload = json.loads(art.payload_json)
        for d in payload.get("decisions") or []:
            assert "\x1f" in d["target_key"]  # composite key，而非纯 norm
    manifest = _latest_manifest(db, executed.id)
    assert manifest and manifest.get("plan_hash")


# ---------------------------------------------------------------------------
# 4-6. default update content guard
# ---------------------------------------------------------------------------


def test_default_update_entry_content_sig_present(db):
    _bootstrap()
    ws = _mk_ws(db)
    p = _page(db, ws, "p1", "内容足够长用于合成测试构建。" * 5)
    wiki = _wiki(db, ws, "wk", "主题A", skill="default", version="1",
                 locked=True, source=["p1"])
    db.commit()
    target = {
        "key": bmod._batch_key(ws.acl_scope, _normalize("主题A")),
        "norm_title": _normalize("主题A"), "scope_acl_json": ws.acl_scope,
        "workspace_id": ws.id, "action": "update", "title": "主题A",
        "category": "资料", "existing_wiki_id": wiki.id,
        "source_page_ids": ["p1"], "page_ids": ["p1"],
        "branch": "default", "migration": False,
        "decision": SkillDecision(
            target_key="主题A", wiki_page_id=wiki.id, selected_skill="default",
            selected_version="1", selected_by="locked", status="locked",
            reason_code="SKILL_LOCKED").to_dict(),
    }
    ctx = {"state": {"context": {"workspace_id": ws.id, "input_hash": _hash("in")}},
           "llm_runner": _mk_llm()}
    out = bmod._synthesize_default_target(db, ctx["state"]["context"], ctx, target)
    assert out.get("ready"), out
    assert out["entry"].get("content_sig"), "update entry must carry content_sig"


def test_default_update_page_change_after_synth_no_publish(db):
    _bootstrap()
    ws = _mk_ws(db)
    p = _page(db, ws, "p1", "内容足够长用于合成测试构建。" * 5)
    wiki = _wiki(db, ws, "wk", "主题A", skill="default", version="1",
                 locked=True, source=["p1"])
    db.commit()
    old_rev = wiki.current_revision_id
    target = {
        "key": "k", "norm_title": _normalize("主题A"), "scope_acl_json": ws.acl_scope,
        "workspace_id": ws.id, "action": "update", "title": "主题A", "category": "资料",
        "existing_wiki_id": wiki.id, "source_page_ids": ["p1"], "page_ids": ["p1"],
    }
    ctx = {"state": {"context": {"workspace_id": ws.id, "input_hash": _hash("in")}},
           "llm_runner": _mk_llm()}
    out = bmod._synthesize_default_target(db, ctx["state"]["context"], ctx, target)
    assert out.get("ready")
    # synthesize→publish 窗口内 Page 内容变化 → 不得发布。
    p.content = p.content + "\nchanged"
    p.content_hash = _hash(p.content)
    db.flush()
    rev = bmod._publish_default_target(db, ctx, target, out)
    assert rev is None
    db.refresh(wiki)
    assert wiki.current_revision_id == old_rev
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 0


def test_default_update_wiki_revision_change_after_synth_no_publish(db):
    _bootstrap()
    ws = _mk_ws(db)
    p = _page(db, ws, "p1", "内容足够长用于合成测试构建。" * 5)
    wiki = _wiki(db, ws, "wk", "主题A", skill="default", version="1",
                 locked=True, source=["p1"])
    db.commit()
    target = {
        "key": "k", "norm_title": _normalize("主题A"), "scope_acl_json": ws.acl_scope,
        "workspace_id": ws.id, "action": "update", "title": "主题A", "category": "资料",
        "existing_wiki_id": wiki.id, "source_page_ids": ["p1"], "page_ids": ["p1"],
    }
    ctx = {"state": {"context": {"workspace_id": ws.id, "input_hash": _hash("in")}},
           "llm_runner": _mk_llm()}
    out = bmod._synthesize_default_target(db, ctx["state"]["context"], ctx, target)
    assert out.get("ready")
    # wiki 当前 revision 变化 → content guard 不匹配，不发布。
    rev2 = WikiRevision(id="rev-new", wiki_page_id=wiki.id, title=wiki.title,
                        status="draft", edit_type="auto")
    db.add(rev2)
    db.flush()
    wiki.current_revision_id = rev2.id
    db.flush()
    rev = bmod._publish_default_target(db, ctx, target, out)
    assert rev is None
    db.refresh(wiki)
    assert wiki.current_revision_id == rev2.id
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki.id).count() == 1


# ---------------------------------------------------------------------------
# 7-8. input_hash 覆盖全部 source / API 发布前全量重验
# ---------------------------------------------------------------------------


def test_input_hash_covers_historical_source_pages(db):
    _bootstrap()
    ws = _mk_ws(db)
    p1 = _page(db, ws, "p1", "历史来源内容足够长用于编译测试。")
    p2 = _page(db, ws, "p2", "本批来源内容足够长用于编译测试。")
    db.commit()
    target = {
        "key": "k", "branch": "default", "migration": False,
        "existing_wiki_id": None, "source_page_ids": ["p1", "p2"],
        "page_ids": ["p2"], "decision": None,
    }
    h1 = bmod._target_input_hash(db, target)
    p1.content = p1.content + "\n历史变化"
    p1.content_hash = _hash(p1.content)
    db.commit()
    h2 = bmod._target_input_hash(db, target)
    assert h1 != h2  # 历史来源变化同样影响 input_hash


def test_api_publish_reverify_all_source_pages(db):
    _bootstrap()
    ws = _mk_ws(db)
    p1 = _page(db, ws, "p1", _api_spec("MARK_API"), title="API1")
    p2 = _page(db, ws, "p2", _api_spec("MARK_API"), title="API2")
    _evidence(db, p1, "ev1")
    _evidence(db, p2, "ev2")
    wiki = _wiki(db, ws, "wk", "主题B", skill="api_reference", locked=True,
                 source=["p1", "p2"])
    db.commit()
    target = {
        "key": "k", "branch": "api_reference", "migration": False,
        "workspace_id": ws.id, "scope_acl_json": ws.acl_scope,
        "existing_wiki_id": wiki.id, "title": "主题B", "category": "资料",
        "source_page_ids": ["p1", "p2"], "page_ids": ["p1", "p2"],
        "decision": SkillDecision(
            target_key="主题B", wiki_page_id=wiki.id, selected_skill="api_reference",
            selected_version="1", selected_by="locked", status="locked",
            reason_code="SKILL_LOCKED", locked=True).to_dict(),
    }
    compiled = bmod._synthesize_api_target(db, target, False)
    assert compiled.get("ready"), compiled
    # 发布前把非本批"来源页"之一（p2，历史成员）evidence 置 stale → 不得发布。
    db.refresh(p2)
    ev2 = db.query(EvidenceItem).filter(EvidenceItem.source_page_id == "p2").first()
    ev2.status = "stale"
    db.flush()
    run = _create_batch_run_v3(db, ws.id, ["p1"])  # 仅用于 run/lease 上下文
    ctx = {"state": {"context": {"workspace_id": ws.id, "input_hash": _hash("in")}}}
    rev, extra = bmod._publish_api_target(db, ctx, run, target, compiled, False)
    assert rev is None
    assert extra.get("code") in ("EVIDENCE_STALE", "VALIDATION_FAILED")


# ---------------------------------------------------------------------------
# 9-12. plan_hash / retry 矩阵
# ---------------------------------------------------------------------------


def test_retry_target_removed_plan_changed(db):
    """历史 Manifest 计划（含该 target）与本次计划不同（target 消失）→ BATCH_PLAN_CHANGED。"""
    _bootstrap()
    ws = _mk_ws(db)
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    db.commit()
    run = _create_batch_run_v3(db, ws.id, ["pD"])
    # 历史 Manifest：计划含 A + B；本次计划只含 A → plan changed。
    db.add(Artifact(
        run_id=run.id, artifact_type=ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
        payload_json=json.dumps({
            "plan_hash": "historical-plan-with-two-targets",
            "target_results": [
                {"target_key": "kA", "action": "create", "outcome": "published",
                 "wiki_page_id": "wA", "revision_id": "rA", "input_hash": "hA"},
                {"target_key": "kB", "action": "create", "outcome": "failed",
                 "wiki_page_id": None, "revision_id": None, "input_hash": "hB"},
            ],
        }, ensure_ascii=False)))
    db.commit()
    ctx = {"state": {"context": {"workspace_id": ws.id, "input_hash": _hash("in")},
                     "v3": {"batch": {
                         "fatal": None,
                         "targets": [{"key": "kA", "branch": "default"}],
                         "results": {}, "payloads": {}, "plan_hash": "current-plan-only-A",
                     }}}}
    res = bmod.publish_batch_v3(db, run, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "BATCH_PLAN_CHANGED"
    # 不复用/不发布/不 reconcile：无产品写。
    assert db.query(WikiRevision).count() == 0
    assert db.query(WikiPage).filter(WikiPage.workspace_id == ws.id).count() == 0


def test_retry_target_added_plan_changed(db):
    """本次计划比历史新增 target → BATCH_PLAN_CHANGED。"""
    _bootstrap()
    ws = _mk_ws(db)
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    db.commit()
    run = _create_batch_run_v3(db, ws.id, ["pD"])
    db.add(Artifact(
        run_id=run.id, artifact_type=ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
        payload_json=json.dumps({
            "plan_hash": "historical-plan-A",
            "target_results": [
                {"target_key": "kA", "action": "create", "outcome": "published",
                 "wiki_page_id": "wA", "revision_id": "rA", "input_hash": "hA"},
            ],
        }, ensure_ascii=False)))
    db.commit()
    ctx = {"state": {"context": {"workspace_id": ws.id, "input_hash": _hash("in")},
                     "v3": {"batch": {
                         "fatal": None,
                         "targets": [{"key": "kA", "branch": "default"},
                                     {"key": "kB", "branch": "api_reference"}],
                         "results": {}, "payloads": {},
                         "plan_hash": "current-plan-A-plus-B",
                     }}}}
    res = bmod.publish_batch_v3(db, run, ctx)
    assert res.get("ok") is False
    assert res["error_code"] == "BATCH_PLAN_CHANGED"
    assert db.query(WikiRevision).count() == 0


def test_historical_manifest_missing_plan_hash_fails_closed(db):
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API")
    _evidence(db, pA, "evA-stale", status="stale")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "BATCH_PARTIAL"
    wiki = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").first()
    first_rev = wiki.current_revision_id
    # 删除 plan_hash 模拟"历史 Manifest 缺 plan_hash"→ fail closed。
    art = db.query(Artifact).filter(
        Artifact.run_id == executed.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST).one()
    payload = json.loads(art.payload_json)
    payload.pop("plan_hash", None)
    art.payload_json = json.dumps(payload, ensure_ascii=False)
    db.commit()
    retried = executor.retry_run(db, executed.id)
    executed2 = executor.execute_run(db, retried.id)
    db.refresh(executed2)
    assert executed2.status == "failed"
    assert executed2.safe_error_code == "BATCH_PLAN_CHANGED"
    db.expire_all()
    w2 = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").first()
    assert w2 is not None and w2.current_revision_id == first_rev
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == w2.id).count() == 1


# ---------------------------------------------------------------------------
# 13-15. reused + 累计 graph targets
# ---------------------------------------------------------------------------


def test_plan_same_input_same_reuse_no_duplicate(db):
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API")
    _evidence(db, pA, "evA-stale", status="stale")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True, source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "failed"
    new_wiki = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").first()
    first_rev = new_wiki.current_revision_id
    _evidence(db, pA, "evA-ok", status="active")
    db.commit()
    retried = executor.retry_run(db, executed.id)
    executed2 = executor.execute_run(db, retried.id)
    db.refresh(executed2)
    assert executed2.status == "succeeded", (executed2.safe_error_code,
                                             executed2.safe_error_message)
    db.expire_all()
    nw = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").first()
    assert nw.current_revision_id == first_rev
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == nw.id).count() == 1
    manifest = _latest_manifest(db, executed2.id)
    assert manifest["plan_hash"]
    trs = {t["target_key"]: t for t in manifest["target_results"]}
    assert any(t["outcome"] == "reused" for t in trs.values())
    assert any(t["outcome"] == "published" for t in trs.values())


def test_manifest_cumulative_wiki_and_page_graph_targets(db):
    """成功/reused target 的 graph_targets 含 wiki + 全部来源 page；排序去重。"""
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API")
    _evidence(db, pA, "evA")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True, source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    manifest = _latest_manifest(db, executed.id)
    graph = manifest["graph_targets"]
    kinds = {g["kind"] for g in graph}
    assert "wiki" in kinds and "page" in kinds
    page_ids = sorted({g["page_id"] for g in graph if g["kind"] == "page"})
    assert "pA" in page_ids and "pD" in page_ids
    assert len(page_ids) == len(set(page_ids))  # 去重
    assert len(graph) == len({(g["kind"], g.get("wiki_page_id") or g.get("page_id"))
                              for g in graph})


def test_failed_target_no_page_graph_target(db):
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API")
    _evidence(db, pA, "evA", status="stale")
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(default_empty=True),
                                        graph_runner=_graph_noop)
    # 仅一个失败 api 目标 → 无 graph target。
    executed = _run_batch(db, ws, ["pA"])
    db.refresh(executed)
    assert executed.status == "failed"
    manifest = _latest_manifest(db, executed.id)
    assert manifest["graph_targets"] == []
    assert all(t["outcome"] == "failed" for t in manifest["target_results"])


# ---------------------------------------------------------------------------
# 16. 孤儿写
# ---------------------------------------------------------------------------


def test_default_create_publish_none_no_orphan_wiki(db, monkeypatch):
    _bootstrap()
    ws = _mk_ws(db)
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    db.commit()
    target = {
        "key": "k", "branch": "default", "workspace_id": ws.id,
        "scope_acl_json": ws.acl_scope, "action": "create", "title": "主题A",
        "category": "资料", "existing_wiki_id": None,
        "source_page_ids": ["pD"], "page_ids": ["pD"],
        "decision": SkillDecision(target_key="k", wiki_page_id=None,
                                  selected_skill="default", selected_version="1",
                                  status="selected").to_dict(),
    }
    payload = {"entry": {"title": "主题A", "category": "资料", "action": "create",
                         "norm_title": "主题A", "source_page_ids": ["pD"],
                         "content": "正文", "summary": "摘要", "versioned": False,
                         "vc": None, "content_sig": None, "published_ready": True}}
    from app.core.wiki_pipeline.pipelines.wiki_default import (
        _publish_wiki_from_entry as _orig,
    )

    def _none(*a, **k):
        return None

    monkeypatch.setattr(
        "app.core.wiki_pipeline.pipelines.wiki_skilled_batch_v3._publish_wiki_from_entry",
        _none)
    ctx = {"state": {"context": {"workspace_id": ws.id, "input_hash": _hash("in")}}}
    rev = bmod._publish_default_target(db, ctx, target, payload)
    assert rev is None
    # 无孤儿 draft Wiki / 无 Revision / 无 membership。
    assert db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id).count() == 0
    assert db.query(WikiRevision).count() == 0


# ---------------------------------------------------------------------------
# 17-18. plan_hash 顺序无关 / manifest 无敏感信息
# ---------------------------------------------------------------------------


def test_plan_hash_order_independent():
    def _t(i, branch):
        return {
            "key": f"k{i}", "branch": branch, "action": "create",
            "existing_wiki_id": None, "source_page_ids": [f"p{i}"],
            "migration": False, "decision": None,
        }
    a = [_t(1, "default"), _t(2, "api_reference")]
    assert bmod._compute_plan_hash(a) == bmod._compute_plan_hash(list(reversed(a)))


def test_batch_manifest_no_sensitive_and_sorted(db):
    marker = "TOKEN_C1_LEAK"
    path = r"C:\Users\secret\c1\leak.md"
    _bootstrap()
    ws = _mk_ws(db)
    spec = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "u", "version": "1.0",
                 "description": "MARK_API " + marker + path + " GET /api/users 返回 200 成功。"},
        "paths": {"/api/users": {"get": {"description": "返回 200 成功。",
                                         "responses": {"200": {"description": "ok"}}}}},
    }, ensure_ascii=False)
    pA = _page(db, ws, "pA", spec, title="API")
    _evidence(db, pA, "evA")
    _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True, source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws, ["pA"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    manifest = _latest_manifest(db, executed.id)
    blob = json.dumps(manifest, ensure_ascii=False)
    assert marker not in blob and path not in blob and "prompt" not in blob.lower()
    assert manifest["wiki_page_ids"] == sorted(manifest["wiki_page_ids"])
    assert manifest["revision_ids"] == sorted(manifest["revision_ids"])
