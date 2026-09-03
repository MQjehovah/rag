"""Phase 7C.3-C：wiki.default v3 混合 Skill batch_rebuild 测试。"""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.knowledge_compiler_v3 import wiki_page_builder as legacy_builder
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
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    EvidenceItem,
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
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
        db.add(Notebook(id=nb_id, name="研发库", group_id="eng"))
        db.flush()
    return ensure_notebook_workspace(db, db.get(Notebook, nb_id))


def _page(db, ws, pid, content, *, title="页面", dirty=True, bind=True):
    from app.models.database import NotebookWorkspaceBinding

    binding = (db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.workspace_id == ws.id).first()
        if bind else None)
    nb_id = binding.notebook_id if binding is not None else None
    db.add(Page(id=pid, notebook_id=nb_id, title=title, content=content,
                content_hash=_hash(content), wiki_dirty=dirty))
    db.flush()
    return db.get(Page, pid)


def _evidence(db, page, eid, *, status="active", content=None,
              locator=None, content_hash=None):
    db.add(EvidenceItem(
        id=eid, source_page_id=page.id, status=status,
        content=content or "evidence", locator_json=json.dumps(locator or {"section": "all"}),
        content_hash=content_hash or ("f" * 64),
        source_doc_hash=page.content_hash,
    ))
    db.flush()
    return db.get(EvidenceItem, eid)


def _wiki(db, ws, wid, title, *, skill, version="1", locked=False,
          page_ids=None, source=None, dirty=True):
    if source is None:
        source = page_ids
    w = WikiPage(
        id=wid, title=title, summary="", acl_scope=ws.acl_scope,
        category="资料", status="published",
        source_page_ids=json.dumps(list(source or [])), dirty=dirty,
        workspace_id=ws.id, content_skill=skill, skill_version=version,
        skill_locked=locked,
    )
    db.add(w)
    db.flush()
    return w


def _api_spec(marker=""):
    return json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "用户 API", "version": "1.0",
                 "description": marker + "GET /api/users 返回 200 成功。"},
        "paths": {"/api/users": {"get": {
            "summary": "列表", "description": "返回 200 成功。",
            "responses": {"200": {"description": "ok"}},
        }}},
    }, ensure_ascii=False)


def _mk_batch_llm(*, default_body="聚合正文内容足够长用于编译：" + "x" * 60,
                  default_empty=False, api_ops=None, default_ops=None):
    def _run(messages, context="", timeout=120.0):
        prompt = messages[0]["content"] if messages else ""
        if context == "wiki-ingest-page":
            if "MARK_API" in prompt or "MARK_MIG" in prompt:
                title = "主题B" if "MARK_API" in prompt else "主题C"
                return {"worthy": True, "ops": [{"action": "update",
                                                 "title": title, "category": "资料"}]}
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
    batch_hash = _batch_input_hash(db, ws_id, ids)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="3",
        trigger_type="batch_rebuild", trigger_object_id=ws_id,
        workspace_id=ws_id, input_hash=batch_hash,
        supersede_same_trigger=True,
    )
    payload = {
        "workspace_id": ws_id,
        "page_ids": ids,
        "page_input_hashes": {pid: _full_page_hash(db, pid) for pid in ids},
    }
    db.add(Artifact(
        run_id=run.id, artifact_type=ARTIFACT_TYPE_WIKI_BATCH_INPUT,
        schema_version=ARTIFACT_SCHEMA_WIKI_BATCH,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    ))
    db.commit()
    db.refresh(run)
    return run


def _full_page_hash(db, pid):
    from app.core.wiki_pipeline.pipelines.wiki_default import _page_full_hash

    return _page_full_hash(db, pid)


def _run_batch(db, ws_id, page_ids):
    run = _create_batch_run_v3(db, ws_id, page_ids)
    return executor.execute_run(db, run.id)


def _latest_manifest(db, run_id):
    art = db.query(Artifact).filter(
        Artifact.run_id == run_id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    ).order_by(Artifact.created_at.desc()).first()
    return json.loads(art.payload_json) if art else None


def _wiki_sections(db, wiki_id):
    rows = db.query(WikiSection).filter(WikiSection.revision_id.in_(
        db.query(WikiRevision.id).filter(WikiRevision.wiki_page_id == wiki_id)
    )).order_by(WikiSection.revision_id, WikiSection.order_index).all()
    return rows


def _protected_old_revision(db, wiki):
    rev = WikiRevision(id="rev-batch-old", wiki_page_id=wiki.id, title=wiki.title,
                       summary="", source_hash=_hash("old"), status="published",
                       edit_type="auto")
    db.add(rev)
    db.flush()
    db.add(WikiSection(
        id="sec-batch-prot", revision_id=rev.id, section_type="facts",
        heading="人工", content="人工内容勿覆盖", order_index=0,
        locked=True, content_origin="manual", merge_policy="protected"))
    db.flush()
    wiki.current_revision_id = rev.id
    db.flush()


# ---------------------------------------------------------------------------
# 1/2. 同批 default + api_reference 各用自己决策
# ---------------------------------------------------------------------------


def test_mixed_default_and_api_targets(db):
    _bootstrap()
    ws = _mk_ws(db)
    # pA 触发 api_reference target（既有 locked api wiki 主题B）。
    spec = _api_spec("MARK_API")
    pA = _page(db, ws, "pA", spec, title="用户 API 文档")
    _evidence(db, pA, "evA")
    # pD 触发 default target（新建 主题A）。
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    wikiA = _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
                  page_ids=["pA"], source=["pA"])
    db.commit()

    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)

    # api target：wiki 主题B 内容为 api_reference Section。
    db.refresh(wikiA)
    assert wikiA.status == "published"
    api_secs = [s for s in _wiki_sections(db, wikiA.id)
                if s.skill_key == "api_reference"]
    assert api_secs and any(s.section_key == "overview" for s in api_secs)
    # default target：新建 wiki 主题A，default Section（skill_key NULL / facts）。
    new_wiki = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id,
        WikiPage.title == "主题A").first()
    assert new_wiki is not None
    def_secs = [s for s in _wiki_sections(db, new_wiki.id)
                if s.section_type in ("summary", "facts")]
    assert def_secs and new_wiki.content_skill == "default"
    # 每个成功 target 恰好一个 Revision。
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wikiA.id).count() == 1
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == new_wiki.id).count() == 1

    manifest = _latest_manifest(db, executed.id)
    trs = manifest["target_results"]
    branches = sorted({t["branch"] for t in trs})
    assert "api_reference" in branches and "default" in branches
    for tr in trs:
        assert tr["safe_error_code"] == ""
        assert json.dumps(tr, ensure_ascii=False)


def test_target_order_reversal_identical(db):
    """page_ids 顺序反转结果一致（target 按 key 排序）。"""
    _bootstrap()
    results = {}
    for tag in ("A", "B"):
        ws = _mk_ws(db, f"nb-{tag}")
        pageA = _page(db, ws, f"pA{tag}", _api_spec("MARK_API"), title="API 页")
        _evidence(db, pageA, f"evA{tag}")
        pageD = _page(db, ws, f"pD{tag}", "普通说明文字足够长用于编译构建测试。" * 5)
        _wiki(db, ws, f"wk-api{tag}", "主题B", skill="api_reference", locked=True,
              page_ids=[f"pA{tag}"], source=[f"pA{tag}"])
        db.commit()
        page_ids = [f"pA{tag}", f"pD{tag}"]
        order = page_ids if tag == "A" else list(reversed(page_ids))
        executor.configure_external_runners(llm_runner=_mk_batch_llm(),
                                            graph_runner=_graph_noop)
        executed = _run_batch(db, ws.id, order)
        db.refresh(executed)
        assert executed.status == "succeeded", (executed.safe_error_code,
                                                executed.safe_error_message)
        manifest = _latest_manifest(db, executed.id)
        results[tag] = sorted(
            [(t["target_key"], t["branch"], t["outcome"]) for t in manifest["target_results"]])
        assert manifest["wiki_page_ids"] == sorted(manifest["wiki_page_ids"])
        assert manifest["revision_ids"] == sorted(manifest["revision_ids"])
    assert results["A"] == results["B"]


def test_api_target_only_uses_own_evidence(db):
    """API target 不读取 default target 的 Evidence。"""
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API 页")
    _evidence(db, pA, "ev-api")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    # default 页上有独立 Evidence（不应进入 api 目标绑定）。
    _evidence(db, pD, "ev-default-only", content="default-excerpt")
    wikiA = _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
                  page_ids=["pA"], source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wikiA)
    sec_ids = [s.id for s in _wiki_sections(db, wikiA.id)]
    binds = db.query(WikiSectionEvidenceBinding).filter(
        WikiSectionEvidenceBinding.section_id.in_(sec_ids)).all()
    assert binds
    assert all(b.evidence_id == "ev-api" for b in binds)
    assert "ev-default-only" not in {b.evidence_id for b in binds}


# ---------------------------------------------------------------------------
# 6/7/8/9. partial：一个 target 失败、另一成功
# ---------------------------------------------------------------------------


def test_api_target_fails_default_succeeds_partial(db):
    """api target 无 active evidence → 失败；default target 成功；Run failed(partial)。"""
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API 页")
    _evidence(db, pA, "ev-stale", status="stale")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    wikiA = _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
                  page_ids=["pA"], source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "BATCH_PARTIAL"
    # default 主题A 成功发布。
    new_wiki = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").first()
    assert new_wiki is not None and new_wiki.current_revision_id is not None
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == new_wiki.id).count() == 1
    # api target 零 Revision，旧 wiki 无改动。
    db.refresh(wikiA)
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wikiA.id).count() == 0
    assert wikiA.current_revision_id is None
    # 相关 Page dirty：api 页保持 dirty，default 页清 dirty。
    db.refresh(pA)
    assert pA.wiki_dirty is True
    db.refresh(pD)
    assert pD.wiki_dirty is False
    manifest = _latest_manifest(db, executed.id)
    assert manifest["fail_code"] == "BATCH_PARTIAL"
    trs = {t["target_key"]: t for t in manifest["target_results"]}
    assert any(t["branch"] == "api_reference" and t["outcome"] == "failed"
               for t in trs.values())
    assert any(t["branch"] == "default" and t["outcome"] == "published"
               for t in trs.values())


def test_default_target_fails_api_succeeds(db):
    """default target 合成失败（无正文）→ default 零 Revision；api target 成功。"""
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API 页")
    _evidence(db, pA, "evA")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    wikiA = _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
                  page_ids=["pA"], source=["pA"])
    db.commit()
    # default 合成 LLM 返回空正文 → invalid_response。
    executor.configure_external_runners(
        llm_runner=_mk_batch_llm(default_empty=True), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "BATCH_PARTIAL"
    db.refresh(wikiA)
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wikiA.id).count() == 1
    api_secs = [s for s in _wiki_sections(db, wikiA.id)
                if s.skill_key == "api_reference"]
    assert api_secs
    # default 目标没有 wiki 被创建。
    assert db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").count() == 0
    db.refresh(pD)
    assert pD.wiki_dirty is True


# ---------------------------------------------------------------------------
# 10. 同一 Page 参与成功+失败目标
# ---------------------------------------------------------------------------


def test_same_page_success_and_failed_targets_membership(db):
    """一页同时参与 default（成功）与 api（失败既有）→ 两个 wiki 都保留且页 dirty。"""
    _bootstrap()
    ws = _mk_ws(db)
    p = _page(db, ws, "p1", _api_spec("MARK_API"), title="API 页")
    _evidence(db, p, "ev1", status="stale")  # api 目标失败；default 不依赖 evidence。
    _wiki(db, ws, "wk-def", "主题B", skill="default", locked=True,
          page_ids=["p1"], source=["p1"], dirty=False)
    _wiki(db, ws, "wk-api", "主题C", skill="api_reference", locked=True,
          page_ids=["p1"], source=["p1"], dirty=False)
    db.commit()

    def _dual(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": [
                {"action": "update", "title": "主题B", "category": "资料"},
                {"action": "update", "title": "主题C", "category": "资料"}]}
        if context in ("wiki-synthesis", "wiki-mapreduce"):
            return {"summary": "摘要", "content": "正文内容" + "x" * 60}
        return {"worthy": True, "ops": []}

    executor.configure_external_runners(llm_runner=_dual, graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["p1"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "BATCH_PARTIAL"
    db.expire_all()
    wk_def = db.get(WikiPage, "wk-def")
    wk_api = db.get(WikiPage, "wk-api")
    # 两 wiki 均仍保留 p1 membership（reconcile 不互相删除）。
    assert "p1" in json.loads(wk_def.source_page_ids)
    assert "p1" in json.loads(wk_api.source_page_ids)
    db.refresh(p)
    assert p.wiki_dirty is True
    assert wk_def.current_revision_id is not None  # default target 成功
    assert wk_api.current_revision_id is None       # api target 失败


# ---------------------------------------------------------------------------
# 13. batch API protected 保留 / 14-15. batch migration
# ---------------------------------------------------------------------------


def test_batch_api_update_preserves_protected_section(db):
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API 页")
    _evidence(db, pA, "evA")
    wikiA = _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
                  page_ids=["pA"], source=["pA"], dirty=False)
    _protected_old_revision(db, wikiA)
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wikiA)
    secs = db.query(WikiSection).filter(
        WikiSection.revision_id == wikiA.current_revision_id).all()
    prot = [s for s in secs if s.content == "人工内容勿覆盖"]
    assert prot and prot[0].merge_policy == "protected"


def test_batch_migration_applies_and_blocks_others_not_affected(db):
    """batch 中 migration 目标 shadow 成功 → 原子切换。"""
    _bootstrap()
    ws = _mk_ws(db)
    pM = _page(db, ws, "pM", _api_spec("MARK_MIG"), title="API 页")
    _evidence(db, pM, "evM")
    # 既有 default wiki 主题C（migration 候选）。
    wC = _wiki(db, ws, "wk-mig", "主题C", skill="default", version="1",
               page_ids=["pM"], source=["pM"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pM"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    db.refresh(wC)
    assert wC.content_skill == "api_reference"
    assert wC.skill_selected_by == "migration"
    dec = json.loads(wC.skill_decision_json)
    assert dec["reason_code"] == "MIGRATION_APPLIED"
    assert dec["previous_skill"] == "default"
    api_secs = [s for s in _wiki_sections(db, wC.id) if s.skill_key == "api_reference"]
    assert api_secs
    manifest = _latest_manifest(db, executed.id)
    mig = [t for t in manifest["target_results"] if t["migration_applied"]]
    assert mig and mig[0]["outcome"] == "published"


def test_batch_migration_shadow_failure_does_not_block_others(db):
    """migration 目标 shadow 失败 → 其他 default 目标仍发布，migration 目标零切换。"""
    _bootstrap()
    ws = _mk_ws(db)
    pM = _page(db, ws, "pM", _api_spec("MARK_MIG"), title="API 页")
    _evidence(db, pM, "ev-stale", status="stale")   # shadow compile 不可发布
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    wC = _wiki(db, ws, "wk-mig", "主题C", skill="default", version="1",
               page_ids=["pM"], source=["pM"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pM", "pD"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "BATCH_PARTIAL"
    db.refresh(wC)
    assert wC.content_skill == "default"
    assert wC.current_revision_id is None
    new_wiki = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").first()
    assert new_wiki is not None and new_wiki.current_revision_id is not None


# ---------------------------------------------------------------------------
# 16-20. partial retry 幂等 + BATCH_STALE
# ---------------------------------------------------------------------------


def test_partial_retry_reuses_success_no_duplicate(db):
    """第一次 partial（api 失败/default 成功），retry 后只补发 api、default reused。"""
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API 页")
    _evidence(db, pA, "evA-stale", status="stale")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    wikiA = _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
                  page_ids=["pA"], source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "BATCH_PARTIAL"
    new_wiki = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").first()
    def_first_rev = new_wiki.current_revision_id
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == new_wiki.id).count() == 1

    # 修复 api evidence 后 retry 同一 run。
    _evidence(db, pA, "evA-new", status="active")
    db.commit()
    retried = executor.retry_run(db, executed.id)
    executed2 = executor.execute_run(db, retried.id)
    db.refresh(executed2)
    assert executed2.status == "succeeded", (executed2.safe_error_code,
                                             executed2.safe_error_message)
    # default 不重复 Revision（reused）；api 恰好一个 Revision。
    db.refresh(new_wiki)
    assert new_wiki.current_revision_id == def_first_rev
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == new_wiki.id).count() == 1
    db.refresh(wikiA)
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wikiA.id).count() == 1
    # 累积 Manifest 含两次 attempt 全部成功 target。
    manifest = _latest_manifest(db, executed2.id)
    trs = {t["target_key"]: t["outcome"] for t in manifest["target_results"]}
    assert set(trs.values()) <= {"published", "reused"}
    assert len([w for w in manifest["wiki_page_ids"]]) == 2
    assert len(manifest["revision_ids"]) == 2
    assert all(t.get("safe_error_code") == "" for t in manifest["target_results"])
    assert manifest["fail_code"] is None


def test_retry_stale_input_fails_closed(db):
    """retry 时已成功 target 的输入变化 → BATCH_STALE，旧成功记录不复用/不覆盖。"""
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API 页")
    _evidence(db, pA, "ev-stale", status="stale")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    wikiA = _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
                  page_ids=["pA"], source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "BATCH_PARTIAL"
    new_wiki = db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id, WikiPage.title == "主题A").first()
    assert new_wiki is not None
    first_rev = new_wiki.current_revision_id
    # 已成功 default target 的来源页内容变化 → retry BATCH_STALE，不覆盖旧记录。
    p = db.get(Page, "pD")
    p.content = p.content + "\nchanged"
    p.content_hash = _hash(p.content)
    db.commit()
    retried = executor.retry_run(db, executed.id)
    executed2 = executor.execute_run(db, retried.id)
    db.refresh(executed2)
    assert executed2.status == "failed"
    assert executed2.safe_error_code == "BATCH_STALE"
    db.expire_all()
    nw = db.get(WikiPage, new_wiki.id)
    assert nw.current_revision_id == first_rev
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == new_wiki.id).count() == 1


# ---------------------------------------------------------------------------
# 11/12. unsupported skill 只影响对应 target；全局 fatal 零发布
# ---------------------------------------------------------------------------


def test_unsupported_skill_only_fails_its_target(db):
    """batch planning：未知 Skill 的 target 失败不影响 default target（决策来自持久化）。"""
    from app.core.wiki_pipeline.pipelines.wiki_skilled_batch_v3 import (
        _plan_batch_targets_v3,
    )
    from app.core.wiki_skills.schemas import SkillDecision

    _bootstrap()
    ws = _mk_ws(db)
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    pX = _page(db, ws, "pX", "另一种普通文本也足够长用于测试。" * 5)
    db.commit()
    context = {
        "applicable": True, "workspace_id": ws.id,
        "page_input_hashes": {pid: _full_page_hash(db, pid) for pid in ("pD", "pX")},
    }
    batch_pages = [
        {"page_id": "pD", "status": "create_update",
         "ops": [{"action": "create", "title": "主题A", "category": "资料"}],
         "scope_acl_json": ws.acl_scope, "workspace_id": ws.id},
        {"page_id": "pX", "status": "create_update",
         "ops": [{"action": "create", "title": "主题X", "category": "资料"}],
         "scope_acl_json": ws.acl_scope, "workspace_id": ws.id},
    ]
    d_ok = SkillDecision(target_key="主题A", wiki_page_id=None, selected_skill="default",
                         selected_version="1", selected_by="auto", status="selected")
    d_bad = SkillDecision(target_key="主题X", wiki_page_id=None, selected_skill="mystery",
                          selected_version="1", selected_by="auto", status="selected")
    targets, fatal = _plan_batch_targets_v3(db, context, batch_pages, [d_ok, d_bad])
    assert fatal is None
    by_norm = {_normalize(t["norm_title"]): t for t in targets}
    assert by_norm[_normalize("主题A")]["branch"] == "default"
    assert by_norm[_normalize("主题X")]["branch"] is None
    assert by_norm[_normalize("主题X")].get("resolve_error")


def test_global_workspace_fatal_zero_publish(db):
    """workspace/输入 fatal → 整批零发布、Run failed、相关 Page 保持 dirty。"""
    _bootstrap()
    ws = _mk_ws(db)
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    # 未绑定 notebook/workspace 的页 → resolve 判定 workspace_mismatch（全局 fatal）。
    pNo = _page(db, ws, "pNo", "无绑定页内容也足够长用于编译构建测试。", bind=False)
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pD", "pNo"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code in ("WORKSPACE_MISMATCH", "VALIDATION_FAILED")
    assert db.query(WikiRevision).count() == 0
    assert db.query(WikiSection).count() == 0
    assert db.query(WikiSectionEvidenceBinding).count() == 0
    db.refresh(pD)
    assert pD.wiki_dirty is True


# ---------------------------------------------------------------------------
# 22. DB 异常整体回滚；25. 不调用 _legacy_*
# ---------------------------------------------------------------------------


def test_batch_publish_exception_rolls_back_all(db, monkeypatch):
    import app.core.wiki_pipeline.pipelines.wiki_skilled_batch_v3 as batch_mod

    _bootstrap()
    ws = _mk_ws(db)
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    db.commit()

    orig = batch_mod._publish_default_target

    def _boom(db, ctx, target, payload):
        orig(db, ctx, target, payload)
        raise RuntimeError("boom-batch")

    monkeypatch.setattr(batch_mod, "_publish_default_target", _boom)
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pD"])
    db.refresh(executed)
    assert executed.status == "failed"
    assert executed.safe_error_code == "BATCH_PUBLISH_FAILED"
    assert "boom" not in (executed.safe_error_message or "")
    # 整体回滚：无任何 Revision/Section/Binding 残留。
    assert db.query(WikiRevision).count() == 0
    assert db.query(WikiSection).count() == 0
    assert db.query(WikiSectionEvidenceBinding).count() == 0
    assert db.query(WikiPage).filter(
        WikiPage.workspace_id == ws.id).count() == 0


def test_batch_no_legacy_calls(db, monkeypatch):
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", _api_spec("MARK_API"), title="API 页")
    _evidence(db, pA, "evA")
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
          page_ids=["pA"], source=["pA"])
    db.commit()

    def _boom(*a, **k):
        raise AssertionError("legacy must not run in v3 batch")

    legacy_names = [n for n in dir(legacy_builder) if n.startswith("_legacy_")]
    for name in legacy_names:
        monkeypatch.setattr(legacy_builder, name, _boom)
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)


# ---------------------------------------------------------------------------
# 24. Manifest/TargetResult 无敏感内容
# ---------------------------------------------------------------------------


def test_batch_manifest_no_sensitive_content(db):
    marker = "TOKEN_BATCH_LEAK_31"
    path = r"C:\Users\secret\batch\leak.md"
    spec = json.dumps({
        "openapi": "3.0.1",
        "info": {"title": "用户 API", "version": "1.0",
                 "description": "MARK_API " + marker + " " + path +
                 " GET /api/users 返回 200 成功。"},
        "paths": {"/api/users": {"get": {
            "description": "返回 200 成功。",
            "responses": {"200": {"description": "ok"}},
        }}},
    }, ensure_ascii=False)
    _bootstrap()
    ws = _mk_ws(db)
    pA = _page(db, ws, "pA", spec, title="API 页")
    _evidence(db, pA, "evA", content=marker + path)
    pD = _page(db, ws, "pD", "普通说明文字足够长用于编译构建测试。" * 5)
    _wiki(db, ws, "wk-api", "主题B", skill="api_reference", locked=True,
          page_ids=["pA"], source=["pA"])
    db.commit()
    executor.configure_external_runners(llm_runner=_mk_batch_llm(), graph_runner=_graph_noop)
    executed = _run_batch(db, ws.id, ["pA", "pD"])
    db.refresh(executed)
    assert executed.status == "succeeded", (executed.safe_error_code,
                                            executed.safe_error_message)
    for art in db.query(Artifact).filter(Artifact.run_id == executed.id).all():
        blob = art.payload_json or ""
        assert marker not in blob
        assert path not in blob
        assert "prompt" not in blob.lower()
        assert "Traceback" not in blob
