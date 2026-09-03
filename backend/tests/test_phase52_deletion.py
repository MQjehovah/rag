"""Phase 5.2 删除闭环：page_deleted 从 deletion Artifact 恢复上下文 → 真实来源移除。

覆盖（对应 MUST-4 / 契约十二 / 产品语义决策）：
1. 物理删除后 resolve_context 从 wiki_page_deleted_input Artifact 恢复（单来源 archived、
   来源移除、WikiVersionSource 清空、无 no-op note）。
2. deletion Artifact 缺失 → run failed（safe_error_code 非空），wiki 不被错误发布。
3. deletion Artifact 损坏（非 dict / 缺字段 / deletion_hash 非 64hex）→ run failed。
4. deletion Artifact 身份不一致（page_id / workspace_id 不匹配 run）→ run failed。
5. 单来源 wiki 删除后：status=archived、source_page_ids=[]、WikiVersionSource 清空。
6. 多来源 wiki 删除其一：dirty=True、status=draft、剩余来源保留。
7. publish manifest 含 page_remove + 受影响 wiki target。
8. graph 失败 → run failed(GRAPH_BUILD_FAILED)；retry 依据持久化 manifest 恢复执行，
   retry 期间不新增任何 WikiRevision。
9. create_page_deleted_run 幂等：同输入二次返回原 run、不重复写 artifact。
10. register_default_pipeline：同完整定义重复注册 no-op；stage_keys 相同但定义不同 → raise。

fixture/helper 风格复用 test_phase52_batch.py / test_phase5_equivalence.py（内存 SQLite +
StaticPool + PRAGMA foreign_keys=ON，executor runner 注入同步 fake，无真实网络/LLM）。
helper 统一 _del_ 前缀避免与其它模块冲突。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.wiki_pipeline import executor, registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    ARTIFACT_TYPE_WIKI_PAGE_DELETED_INPUT,
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    PIPELINE_KEY,
    PIPELINE_VERSION,
    create_page_deleted_run,
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
    WikiPage,
    WikiRevision,
    WikiVersionSource,
    WikiWorkspace,
    init_db,
)

GROUP = "engineering"
ACL = '{"groups": ["engineering"]}'
SCOPE_ID = "group:engineering"
BODY = "水箱维护需要定期检查冷却水温度与压力表读数并记录运行日志。"


@pytest.fixture(autouse=True)
def _phase52_del_isolation():
    """每个测试前后清理 registry + 外部 runner（防跨测试泄漏）。"""
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    yield
    registry.REGISTRY.clear()
    executor.reset_external_runners()
    unregister_default_pipeline()


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
# helpers（_del_ 前缀）
# ---------------------------------------------------------------------------


def _del_mk_notebook_ws(db, notebook_id="nb-1", ws_id="ws1"):
    """notebook + 固定 id 的 active binding workspace（wiki_workspaces + binding）。返回 ws_id。"""
    nb = db.get(Notebook, notebook_id)
    if nb is None:
        nb = Notebook(id=notebook_id, name=f"n-{notebook_id}", group_id=GROUP)
        db.add(nb)
        db.flush()
    ws = db.get(WikiWorkspace, ws_id)
    if ws is None:
        ws = WikiWorkspace(id=ws_id, key=f"key-{ws_id}", name=f"w-{ws_id}",
                           acl_scope=ACL, scope_id=SCOPE_ID, status="active")
        db.add(ws)
        db.flush()
    binding = (
        db.query(NotebookWorkspaceBinding)
        .filter(NotebookWorkspaceBinding.notebook_id == notebook_id,
                NotebookWorkspaceBinding.status == "active")
        .first()
    )
    if binding is None:
        db.add(NotebookWorkspaceBinding(
            notebook_id=notebook_id, workspace_id=ws_id, status="active"))
        db.flush()
    db.commit()
    return ws_id


def _del_mk_ws(db, ws_id="ws-shared"):
    """显式共享 workspace（供非 notebook 绑定的手工 wiki 归属使用）。"""
    ws = db.get(WikiWorkspace, ws_id)
    if ws is None:
        ws = WikiWorkspace(id=ws_id, key=f"key-{ws_id}", name=f"w-{ws_id}",
                           acl_scope=ACL, scope_id=SCOPE_ID, status="active")
        db.add(ws)
        db.flush()
    db.commit()
    return ws.id


def _del_mk_page(db, page_id, notebook_id="nb-1", ws_id=None):
    """建 Page（notebook 绑定 workspace）。返回 ws_id。"""
    ws = _del_mk_notebook_ws(db, notebook_id=notebook_id, ws_id=ws_id or "ws1")
    if db.get(Page, page_id) is None:
        db.add(Page(id=page_id, notebook_id=notebook_id, title=f"t-{page_id}",
                    content=BODY, wiki_dirty=True))
        db.flush()
    db.commit()
    return ws


def _del_mk_wiki(db, wiki_id, ws_id, source_pages, *, status="published",
                 with_revision=False, rev_id=None):
    """建 WikiPage（source_page_ids + 每页 WikiVersionSource 行；可带 published Revision）。"""
    if db.get(WikiPage, wiki_id) is not None:
        return db.get(WikiPage, wiki_id)
    dirty = status == "draft"
    wp = WikiPage(
        id=wiki_id, title=f"标题-{wiki_id}", workspace_id=ws_id, acl_scope=ACL,
        status=status, dirty=dirty, category="操作指南",
        source_page_ids=json.dumps(sorted(source_pages)),
        latest_version="common", summary="",
    )
    db.add(wp)
    db.flush()
    if with_revision:
        rid = rev_id or f"rev-{wiki_id}"
        db.add(WikiRevision(id=rid, wiki_page_id=wiki_id, title=wp.title or "",
                            summary="", status="published", edit_type="auto"))
        wp.current_revision_id = rid
    for pid in source_pages:
        db.add(WikiVersionSource(wiki_page_id=wiki_id, version_label="common", page_id=pid))
    db.commit()
    return db.get(WikiPage, wiki_id)


def _del_scenario(db, page_id, wiki_id, source_pages=None, *, ws_id="ws1",
                  notebook_id="nb-1", status="published", with_revision=False):
    """标准删除场景：notebook/ws/page + 引用该 page 的 wiki。返回 (ws_id, page_id, wiki_id)。"""
    _del_mk_page(db, page_id, notebook_id=notebook_id, ws_id=ws_id)
    _del_mk_wiki(db, wiki_id, ws_id, source_pages or [page_id],
                 status=status, with_revision=with_revision)
    return ws_id, page_id, wiki_id
def _del_graph_recorder(**kw):
    calls: list[dict] = []

    def _recorder(**kwargs):
        calls.append(kwargs)
        return True

    _recorder.calls = calls  # type: ignore[attr-defined]
    return _recorder


def _del_noop_llm(messages, context="", timeout=120.0):
    return {"worthy": False, "ops": []}


def _del_enable_pipeline_and_runners(db, recorder=None):
    register_default_pipeline()
    executor.configure_external_runners(
        llm_runner=_del_noop_llm,
        graph_runner=recorder or _del_graph_recorder(),
    )


def _del_create_page_deleted_run(db, page_id, ws_id, notebook_id, source_wiki_ids):
    run = create_page_deleted_run(
        db, page_id=page_id, workspace_id=ws_id, notebook_id=notebook_id,
        source_wiki_ids=list(source_wiki_ids), page_input_hash="",
    )
    db.commit()
    db.refresh(run)
    return run


def _del_plain_page_deleted_run(db, page_id, ws_id):
    """无 deletion Artifact 的手工 page_deleted run（executor.create_run 直建）。"""
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_deleted",
        trigger_object_id=page_id, workspace_id=ws_id,
    )
    db.commit()
    db.refresh(run)
    return run


def _del_physical_delete_page(db, page_id):
    """物理删除 Page + 其 PageChunk（foreign_keys=ON 时 chunk 级联删，显式删更稳）。"""
    for chunk in db.query(PageChunk).filter(PageChunk.page_id == page_id).all():
        db.delete(chunk)
    page = db.get(Page, page_id)
    if page is not None:
        db.delete(page)
    db.commit()


def _del_run_manifest(db, run_id):
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


def _del_deletion_artifact(db, run_id):
    art = (
        db.query(Artifact)
        .filter(
            Artifact.run_id == run_id,
            Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PAGE_DELETED_INPUT,
        )
        .order_by(Artifact.created_at.desc())
        .first()
    )
    return art


def _del_set_artifact_payload(db, art, payload):
    art.payload_json = payload if isinstance(payload, str) else json.dumps(payload)
    db.commit()


def _del_execute(db, run):
    executed = executor.execute_run(db, run.id)
    db.expire_all()
    return db.get(CompileRun, executed.id)


def _del_wiki(db, wiki_id):
    return db.get(WikiPage, wiki_id)


# ---------------------------------------------------------------------------
# 1. 物理删除后从 Artifact 恢复上下文 → 单来源 archived（MUST-4）
# ---------------------------------------------------------------------------


def test_page_deleted_after_physical_delete_resolves_from_artifact(db):
    rec = _del_graph_recorder()
    _del_enable_pipeline_and_runners(db, recorder=rec)
    ws_id, page_id, wiki_id = _del_scenario(
        db, "p1", "w1", status="published", with_revision=True)
    db.expire_all()

    # 先持久化 deletion run + artifact，再物理删除 Page 行（提交）。
    run = _del_create_page_deleted_run(db, page_id, ws_id, "nb-1", [wiki_id])
    assert run.trigger_type == "page_deleted"
    _del_physical_delete_page(db, page_id)
    assert db.get(Page, page_id) is None, "Page 行应已物理删除"

    # worker 泵消费：Page 行已不在 → resolve 从 artifact 恢复 → 真实移除来源。
    executed = _del_execute(db, run)

    assert executed.status == "succeeded", executed.safe_error_code
    manifest = _del_run_manifest(db, run.id)
    assert manifest is not None
    assert manifest["note"] == "page_deleted_remove_source", \
        "不得出现 no_write/no-op note"
    assert manifest["outcome"] == "archived"

    wiki = _del_wiki(db, wiki_id)
    assert wiki.status == "archived"
    assert wiki.dirty is False
    assert json.loads(wiki.source_page_ids or "[]") == [], "来源应被移除"
    assert db.query(WikiVersionSource).filter(
        WikiVersionSource.wiki_page_id == wiki_id).count() == 0

    # schedule_graph 收到 page_remove + wiki(archived → remove_wiki) 调用。
    kw = [sorted(c.keys()) for c in rec.calls]
    assert any(set(c) == {"page_id", "remove_page"} for c in rec.calls)
    assert any(set(c) == {"wiki_page_id", "remove_wiki"} for c in rec.calls)


# ---------------------------------------------------------------------------
# 2. Artifact 缺失 → fail closed（run failed，非 no-op succeeded）
# ---------------------------------------------------------------------------


def test_page_deleted_missing_artifact_fails_closed(db):
    _del_enable_pipeline_and_runners(db)
    ws_id, page_id, wiki_id = _del_scenario(
        db, "p2", "w2", status="published", with_revision=True)
    rev_before = db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki_id).count()

    run = _del_plain_page_deleted_run(db, page_id, ws_id)
    assert _del_deletion_artifact(db, run.id) is None, "本用例应无 deletion artifact"
    _del_physical_delete_page(db, page_id)

    executed = _del_execute(db, run)

    assert executed.status == "failed", "artifact 缺失必须 fail closed"
    assert executed.safe_error_code == "VALIDATION_FAILED"
    manifest = _del_run_manifest(db, run.id)
    assert manifest is not None and manifest["note"] == "deletion_artifact_missing"
    assert manifest["outcome"] == "keep_dirty"

    wiki = _del_wiki(db, wiki_id)
    assert wiki.status == "published", "wiki 不得被错误发布/归档"
    assert set(json.loads(wiki.source_page_ids or "[]")) == {page_id}
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki_id).count() == rev_before


# ---------------------------------------------------------------------------
# 3. Artifact 损坏（非 dict / 缺字段 / deletion_hash 非 64hex）→ fail closed
# ---------------------------------------------------------------------------


def test_page_deleted_corrupt_artifact_fails_closed(db):
    _del_enable_pipeline_and_runners(db)
    cases = ["non_dict", "missing_field", "bad_hash"]
    for idx, kind in enumerate(cases):
        page_id = f"p3-{idx}"
        wiki_id = f"w3-{idx}"
        ws_id, _, _ = _del_scenario(db, page_id, wiki_id, status="published")
        run = _del_create_page_deleted_run(db, page_id, ws_id, "nb-1", [wiki_id])
        art = _del_deletion_artifact(db, run.id)
        assert art is not None
        payload = json.loads(art.payload_json)
        if kind == "non_dict":
            _del_set_artifact_payload(db, art, "[1,2,3]")
        elif kind == "missing_field":
            payload.pop("deletion_hash")
            _del_set_artifact_payload(db, art, payload)
        else:
            payload["deletion_hash"] = "deadbeef"
            _del_set_artifact_payload(db, art, payload)
        _del_physical_delete_page(db, page_id)

        executed = _del_execute(db, run)

        assert executed.status == "failed", f"kind={kind} 必须 fail closed"
        assert executed.safe_error_code == "VALIDATION_FAILED", kind
        wiki = _del_wiki(db, wiki_id)
        assert wiki.status == "published", f"kind={kind} wiki 不得被错误改动"
        assert set(json.loads(wiki.source_page_ids or "[]")) == {page_id}, kind


# ---------------------------------------------------------------------------
# 4. Artifact 身份不一致（page_id / workspace_id）→ fail closed
# ---------------------------------------------------------------------------


def test_page_deleted_identity_mismatch_fails_closed(db):
    _del_enable_pipeline_and_runners(db)
    for idx, kind in enumerate(["page_id", "workspace_id"]):
        page_id = f"p4-{idx}"
        wiki_id = f"w4-{idx}"
        ws_id, _, _ = _del_scenario(db, page_id, wiki_id, status="published")
        run = _del_create_page_deleted_run(db, page_id, ws_id, "nb-1", [wiki_id])
        art = _del_deletion_artifact(db, run.id)
        payload = json.loads(art.payload_json)
        if kind == "page_id":
            payload["page_id"] = "some-other-page"
        else:
            payload["workspace_id"] = "ws-not-the-run-workspace"
        _del_set_artifact_payload(db, art, payload)
        _del_physical_delete_page(db, page_id)

        executed = _del_execute(db, run)

        assert executed.status == "failed", f"kind={kind} 必须 fail closed"
        assert executed.safe_error_code == "VALIDATION_FAILED", kind
        manifest = _del_run_manifest(db, run.id)
        assert manifest is not None and manifest["note"] == "deletion_artifact_invalid"
        wiki = _del_wiki(db, wiki_id)
        assert wiki.status == "published", kind


# ---------------------------------------------------------------------------
# 5. 单来源 archived：status/source_page_ids/WikiVersionSource 清空
# ---------------------------------------------------------------------------


def test_page_deleted_removes_source_and_archives_single_source_wiki(db):
    _del_enable_pipeline_and_runners(db)
    ws_id, page_id, wiki_id = _del_scenario(
        db, "p5", "w5", status="published", with_revision=True)
    assert db.query(WikiVersionSource).filter(
        WikiVersionSource.wiki_page_id == wiki_id).count() == 1

    run = _del_create_page_deleted_run(db, page_id, ws_id, "nb-1", [wiki_id])
    _del_physical_delete_page(db, page_id)
    executed = _del_execute(db, run)

    assert executed.status == "succeeded", executed.safe_error_code
    db.expire_all()
    wiki = _del_wiki(db, wiki_id)
    assert wiki.status == "archived"
    assert wiki.dirty is False
    assert json.loads(wiki.source_page_ids or "[]") == []
    assert db.query(WikiVersionSource).filter(
        WikiVersionSource.wiki_page_id == wiki_id).count() == 0, \
        "WikiVersionSource 行应全部清空"
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki_id).count() == 1, "Revision 历史保留（审计）"


# ---------------------------------------------------------------------------
# 6. 多来源置 dirty + draft，剩余来源保留
# ---------------------------------------------------------------------------


def test_page_deleted_marks_or_rebuilds_multi_source_wiki(db):
    rec = _del_graph_recorder()
    _del_enable_pipeline_and_runners(db, recorder=rec)
    _del_mk_page(db, "p6a", notebook_id="nb-1", ws_id="ws1")
    _del_mk_page(db, "p6b", notebook_id="nb-1", ws_id="ws1")
    _del_mk_wiki(db, "w6", "ws1", ["p6a", "p6b"],
                 status="published", with_revision=True)
    db.expire_all()

    run = _del_create_page_deleted_run(db, "p6a", "ws1", "nb-1", ["w6"])
    _del_physical_delete_page(db, "p6a")
    executed = _del_execute(db, run)

    assert executed.status == "succeeded", executed.safe_error_code
    manifest = _del_run_manifest(db, run.id)
    assert manifest is not None and manifest["note"] == "page_deleted_remove_source"

    db.expire_all()
    wiki = _del_wiki(db, "w6")
    assert wiki.status == "draft", "多来源剩余 → draft"
    assert wiki.dirty is True
    assert set(json.loads(wiki.source_page_ids or "[]")) == {"p6b"}, "剩余来源保留"
    rows = db.query(WikiVersionSource).filter(
        WikiVersionSource.wiki_page_id == "w6").all()
    assert sorted(r.page_id for r in rows) == ["p6b"], "被删来源的 WikiVersionSource 行移除"

    # dirty/draft 有剩余来源 → schedule_graph 不重建（stale_deferred），
    # 只做 page_remove 图谱清理，绝不按旧 published revision 内容复活被删实体。
    assert not any(set(c) == {"wiki_page_id", "remove_wiki"} for c in rec.calls)
    assert any(set(c) == {"page_id", "remove_page"} for c in rec.calls)


# ---------------------------------------------------------------------------
# 7. publish manifest 含 page_remove + 受影响 wiki target
# ---------------------------------------------------------------------------


def test_page_deleted_manifest_contains_page_remove_and_wiki_targets(db):
    _del_enable_pipeline_and_runners(db)
    ws_id, page_id, wiki_id = _del_scenario(db, "p7", "w7", status="published")

    run = _del_create_page_deleted_run(db, page_id, ws_id, "nb-1", [wiki_id])
    _del_physical_delete_page(db, page_id)
    executed = _del_execute(db, run)

    assert executed.status == "succeeded", executed.safe_error_code
    manifest = _del_run_manifest(db, run.id)
    assert manifest is not None
    targets = manifest["graph_targets"]
    assert {"kind": "page_remove", "page_id": page_id} in targets
    assert {"kind": "wiki", "wiki_page_id": wiki_id} in targets
    assert targets[0]["kind"] == "page_remove", "page_remove 应为首个图谱目标"


# ---------------------------------------------------------------------------
# 8. graph 失败 retry：不重复发布 Revision，恢复后按持久化 manifest 重放 remove
# ---------------------------------------------------------------------------


def test_page_deleted_graph_retry_does_not_republish_revision(db):
    flaky = _del_graph_recorder()
    flaky.fail_next = 1  # type: ignore[attr-defined]

    def _flaky_runner(**kwargs):
        if getattr(flaky, "fail_next", 0) > 0:  # type: ignore[attr-defined]
            flaky.fail_next -= 1  # type: ignore[attr-defined]
            raise RuntimeError("graph boom")
        flaky.calls.append(kwargs)  # type: ignore[attr-defined]
        return True

    _del_enable_pipeline_and_runners(db, recorder=_flaky_runner)
    ws_id, page_id, wiki_id = _del_scenario(
        db, "p8", "w8", status="published", with_revision=True)
    rev_before = db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki_id).count()
    assert rev_before == 1

    run = _del_create_page_deleted_run(db, page_id, ws_id, "nb-1", [wiki_id])
    _del_physical_delete_page(db, page_id)
    executed = _del_execute(db, run)

    assert executed.status == "failed", executed.safe_error_code
    assert executed.safe_error_code == "GRAPH_BUILD_FAILED"
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki_id).count() == rev_before, \
        "graph 失败不得产生任何新 Revision"

    # 恢复 runner 后 retry：publish 幂等重复清理，schedule_graph 依据持久化 manifest
    # 重新执行 page_remove + remove_wiki（archived wiki）。
    retried = executor.retry_run(db, run.id)
    db.commit()
    db.refresh(retried)
    assert retried.status == "queued"
    executed2 = _del_execute(db, retried)

    assert executed2.status == "succeeded", executed2.safe_error_code
    assert db.query(WikiRevision).filter(
        WikiRevision.wiki_page_id == wiki_id).count() == rev_before, \
        "retry 期间不得新增任何 WikiRevision（page_deleted 无 revision）"
    assert any(
        set(c) == {"page_id", "remove_page"} for c in flaky.calls
    ), "第二次 schedule_graph 应依据持久化 manifest 重放 page_remove"
    assert any(
        set(c) == {"wiki_page_id", "remove_wiki"} for c in flaky.calls
    ), "archived wiki 图谱应被 remove_wiki 清理"
    wiki = _del_wiki(db, wiki_id)
    assert wiki.status == "archived"


# ---------------------------------------------------------------------------
# 9. create_page_deleted_run 幂等：同输入不重复 artifact
# ---------------------------------------------------------------------------


def test_create_page_deleted_run_idempotent_single_artifact(db):
    register_default_pipeline()
    ws_id, page_id, wiki_id = _del_scenario(db, "p9", "w9", status="published")

    run1 = _del_create_page_deleted_run(db, page_id, ws_id, "nb-1", [wiki_id])
    run2 = _del_create_page_deleted_run(db, page_id, ws_id, "nb-1", [wiki_id])

    assert run2.id == run1.id, "同删除输入应幂等返回原 run"
    arts = db.query(Artifact).filter(
        Artifact.run_id == run1.id,
        Artifact.artifact_type == ARTIFACT_TYPE_WIKI_PAGE_DELETED_INPUT,
    ).all()
    assert len(arts) == 1, "幂等命中不得重复 add deletion Artifact"


# ---------------------------------------------------------------------------
# 10. register_default_pipeline 幂等完整定义比较（契约十二强化）
# ---------------------------------------------------------------------------


def test_duplicate_registration_same_full_definition_is_noop():
    register_default_pipeline()
    register_default_pipeline()  # 同 key+version+完整定义 → no-op 不抛
    pipe = registry.get_pipeline(PIPELINE_KEY, PIPELINE_VERSION)
    assert pipe is not None
    assert tuple(pipe.stage_keys()) == (
        "resolve_context", "topic_route", "synthesize_default", "validate_default",
        "publish_default", "finalize_compile_outcome", "schedule_graph",
    )


def test_duplicate_registration_different_definition_aborts():
    from app.core.wiki_pipeline.pipelines import wiki_default as wd

    register_default_pipeline()

    def _clone_stages(override):
        stages = []
        for s in wd._stage_defs():
            params = {
                "key": s.key, "version": s.version, "retryable": s.retryable,
                "cachable": s.cachable, "execute": s.execute,
                "failure_transition": s.failure_transition,
                "description": s.description, "allows_publish": s.allows_publish,
            }
            params.update(override.get(s.key, {}))
            stages.append(registry.StageDef(**params))
        return stages

    # 同 stage_keys 序列，但 resolve_context stage version/flag 不同 → 必须 raise。
    different_version = registry.PipelineDef(
        key=PIPELINE_KEY,
        version=PIPELINE_VERSION,
        stages=_clone_stages({"resolve_context": {"version": "2"}}),
        allow_null_workspace=False,
    )
    registry.replace_for_test(different_version)
    with pytest.raises(registry.PipelineError):
        register_default_pipeline()

    # allow_null_workspace 不同（stage_keys 相同）→ 同样必须 raise。
    different_allow = registry.PipelineDef(
        key=PIPELINE_KEY,
        version=PIPELINE_VERSION,
        stages=wd._stage_defs(),
        allow_null_workspace=True,
    )
    registry.replace_for_test(different_allow)
    with pytest.raises(registry.PipelineError):
        register_default_pipeline()

    # 定义冲突拒绝静默覆盖后，原定义仍可恢复注册。
    unregister_default_pipeline()
    register_default_pipeline()
    pipe = registry.get_pipeline(PIPELINE_KEY, PIPELINE_VERSION)
    assert tuple(pipe.stage_keys()) == tuple(wd.STAGE_KEYS)
