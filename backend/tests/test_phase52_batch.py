"""Phase 5.2：wiki.default batch_rebuild（多 Page 单 batch CompileRun）。

覆盖：
1. make_default_idempotency_key 确定性 + 不同 workspace/trigger/object 不同。
2. create_batch_run：排序去重 + wiki_batch_input Artifact（JSON 往返）+ idempotency_key
   + input_hash 完整 64。
3. 同 input 二次 → 返回原 run；新输入 → 新 run supersede 旧 queued。
4. batch 端到端（两 page 同 update 目标 title）：run succeeded、该 wiki 只 1 个最终
   Revision、source_page_ids={两 page}、Page dirty 清；batch 内不产中间 Revision。
5. batch 多来源（8 页超阈值）→ 真实 Map-Reduce（wiki-batch-summary≥2 + wiki-mapreduce==1）。
6. resolve_context batch：读输入 Artifact 恢复 page_ids。
7. batch 内不调用 legacy process_page_wiki（monkeypatch 抛错断言未调）。

fixture/helper 复用 test_phase51_multi_page.py 模式（内存 SQLite + StaticPool，
executor runner 注入同步 fake）。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.wiki_pipeline import executor, registry
from app.core.wiki_pipeline.pipelines.multi_page import batch_build, two_phase_build
from app.core.wiki_pipeline.pipelines.wiki_default import (
    ARTIFACT_SCHEMA_WIKI_BATCH,
    ARTIFACT_TYPE_WIKI_BATCH_INPUT,
    ARTIFACT_TYPE_WIKI_PUBLISH_MANIFEST,
    PIPELINE_KEY,
    _resolve_context_batch,
    create_batch_run,
    make_default_idempotency_key,
    register_default_pipeline,
    unregister_default_pipeline,
)
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

SYN_BODY = "聚合正文：包含所有来源知识点"


@pytest.fixture(autouse=True)
def _isolate():
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


def _mk_page(db, page_id, title, content, notebook_id="nb-1", group_id="engineering"):
    if db.get(Notebook, notebook_id) is None:
        db.add(Notebook(id=notebook_id, name="研发库", group_id=group_id))
        db.flush()
    ws = ensure_notebook_workspace(db, db.get(Notebook, notebook_id))
    if db.get(Page, page_id) is None:
        db.add(Page(id=page_id, notebook_id=notebook_id, title=title, content=content))
        db.commit()
    return ws.id, db.get(Page, page_id)


def _expected_page_hash(db, page_id: str) -> str:
    page = db.get(Page, page_id)
    scope = builder._page_scope(db, page)
    acl = builder._scope_to_acl_json(scope) if scope else None
    text = builder._page_text(db, page)
    return builder._page_input_hash(
        page.title or "", text, page.notebook_id or "", acl or ""
    )


def _graph_noop(**kw):
    return None


def _mk_sync_llm(*, ops=None, body=SYN_BODY, contexts=None,
                 long_batch=False, mapreduce_body=None):
    """同步 fake llm：ingest 返回 ops；synthesis 固定正文；可选 mapreduce 分流。"""
    def _run(messages, context="", timeout=120.0):
        if contexts is not None:
            contexts.append(context)
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": ops if ops is not None else []}
        if context == "wiki-synthesis":
            return {"summary": "聚合摘要", "content": body}
        if context == "wiki-batch-summary":
            return {"summary": ("批次摘要内容" * 200) if long_batch else "批次摘要"}
        if context == "wiki-mapreduce":
            return {"summary": "最终", "content": mapreduce_body or body}
        return {"worthy": True, "ops": ops if ops is not None else []}
    return _run


def _run_batch(db, ws_id, page_ids):
    run = create_batch_run(db, workspace_id=ws_id, page_ids=page_ids)
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    return executed


def _batch_manifest(db, run_id):
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


def _single_wiki(db):
    return db.query(WikiPage).first()


# ---------------------------------------------------------------------------
# 1. make_default_idempotency_key
# ---------------------------------------------------------------------------


def test_make_default_idempotency_key_deterministic_and_distinct():
    kwargs = dict(workspace_id="ws-1", trigger_type="batch_rebuild",
                  trigger_object_id="ws-1", wiki_page_id="", full_input_hash="h" * 64)
    k1 = make_default_idempotency_key(**kwargs)
    k2 = make_default_idempotency_key(**kwargs)
    assert k1 == k2
    assert k1.startswith(f"{PIPELINE_KEY}:v1:")
    assert len(k1) == len(f"{PIPELINE_KEY}:v1:") + 64

    assert k1 != make_default_idempotency_key(**{**kwargs, "workspace_id": "ws-2"})
    assert k1 != make_default_idempotency_key(**{**kwargs, "trigger_type": "page_changed"})
    assert k1 != make_default_idempotency_key(**{**kwargs, "trigger_object_id": "obj-9"})
    assert k1 != make_default_idempotency_key(**{**kwargs, "full_input_hash": "x" * 64})


# ---------------------------------------------------------------------------
# 2. create_batch_run：排序去重 + 输入 Artifact
# ---------------------------------------------------------------------------


def test_create_batch_run_sort_dedup_artifact(db):
    register_default_pipeline()
    ws_id, _ = _mk_page(db, "p2", "来源二", "内容二足够长用于编译测试构建。")
    _, p1 = _mk_page(db, "p1", "来源一", "内容一足够长用于编译测试构建。")

    run = create_batch_run(db, workspace_id=ws_id,
                           page_ids=["p2", "p1", "p2", "p1"])
    db.commit()

    assert run.trigger_type == "batch_rebuild"
    assert run.workspace_id == ws_id
    assert run.status == "queued"
    assert run.input_hash and len(run.input_hash) == 64
    assert run.idempotency_key.startswith(f"{PIPELINE_KEY}:v1:")

    art = (
        db.query(Artifact)
        .filter(
            Artifact.run_id == run.id,
            Artifact.artifact_type == ARTIFACT_TYPE_WIKI_BATCH_INPUT,
        )
        .first()
    )
    assert art is not None
    assert art.stage_run_id is None
    assert art.schema_version == ARTIFACT_SCHEMA_WIKI_BATCH
    assert art.content_hash and len(art.content_hash) == 64

    payload = json.loads(art.payload_json)
    # JSON 往返 + 排序去重
    assert payload == {
        "workspace_id": ws_id,
        "page_ids": ["p1", "p2"],
        "page_input_hashes": {
            "p1": _expected_page_hash(db, "p1"),
            "p2": _expected_page_hash(db, "p2"),
        },
    }
    assert payload["page_ids"] == sorted({"p2", "p1"})
    # idempotency_key = helper(ws, batch_rebuild, trigger_object_id, "", batch_input_hash)
    expected_key = make_default_idempotency_key(
        workspace_id=ws_id, trigger_type="batch_rebuild",
        trigger_object_id=ws_id, wiki_page_id="", full_input_hash=run.input_hash,
    )
    assert run.idempotency_key == expected_key

    assert db.get(Page, "p1").wiki_dirty is True  # 新 Page 默认 dirty；未执行不清理


# ---------------------------------------------------------------------------
# 3. 幂等返回原 run；新输入 supersede 旧 queued
# ---------------------------------------------------------------------------


def test_create_batch_run_idempotent_then_supersede(db):
    register_default_pipeline()
    ws_id, _ = _mk_page(db, "p1", "来源一", "原始内容足够长用于编译测试。")

    run1 = create_batch_run(db, workspace_id=ws_id, page_ids=["p1"])
    db.commit()

    # 同输入二次 → 返回原 run
    run1_again = create_batch_run(db, workspace_id=ws_id, page_ids=["p1", "p1"])
    db.commit()
    assert run1_again.id == run1.id
    assert run1_again.status == run1.status

    # 页面内容变 → batch_input_hash 变 → 新 run；旧 queued batch 被 supersede
    page = db.get(Page, "p1")
    page.content = "内容已被更新，与旧版本完全不同的正文长度足够。"
    db.commit()
    run2 = create_batch_run(db, workspace_id=ws_id, page_ids=["p1"])
    db.commit()
    assert run2.id != run1.id
    assert run2.trigger_type == "batch_rebuild"
    assert run2.status == "queued"
    db.expire_all()
    old = db.get(CompileRun, run1.id)
    assert old.status == "superseded"


# ---------------------------------------------------------------------------
# 4. batch 端到端：两 page 同 update 目标 → 单 wiki 单 Revision
# ---------------------------------------------------------------------------


def test_batch_end_to_end_single_wiki_revision(db):
    register_default_pipeline()
    ws_id, _ = _mk_page(db, "p1", "来源一", "水箱维护知识 A 足够长内容用于合成构建。")
    _, p2 = _mk_page(db, "p2", "来源二", "水箱维护知识 B 足够长内容用于合成构建。",
                     notebook_id="nb-1")
    contexts: list[str] = []
    llm = _mk_sync_llm(
        ops=[{"action": "update", "title": "水箱维护流程"}], contexts=contexts)
    executor.configure_external_runners(llm_runner=llm, graph_runner=_graph_noop)

    run = _run_batch(db, ws_id, ["p1", "p2"])

    assert run.status == "succeeded", run.safe_error_code
    wiki = _single_wiki(db)
    assert wiki is not None
    assert wiki.status == "published"
    assert wiki.dirty is False
    ids = json.loads(wiki.source_page_ids or "[]")
    assert set(ids) == {"p1", "p2"}
    assert db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count() == 1, \
        "batch 内不得产生中间 Revision"

    rev = db.get(WikiRevision, wiki.current_revision_id)
    facts = db.query(WikiSection).filter(
        WikiSection.revision_id == rev.id, WikiSection.section_type == "facts"
    ).first()
    assert facts is not None and SYN_BODY in (facts.content or "")
    for pid in ("p1", "p2"):
        page = db.get(Page, pid)
        assert page.wiki_dirty is False
        assert page.wiki_compiled_content_hash == _expected_page_hash(db, pid)
    # publish 写 wiki_publish_manifest（含本批 wiki_page_ids / revision_ids）
    manifest = _batch_manifest(db, run.id)
    assert manifest is not None
    assert manifest["outcome"] == "published"
    assert manifest["wiki_page_ids"] == [wiki.id]
    assert manifest["revision_ids"] == [run.output_revision_id]
    assert manifest["graph_targets"] == [{"kind": "wiki", "wiki_page_id": wiki.id}]
    assert manifest["page_id"] is None
    # 幂等 key 幂等执行：同 input 重跑返回原 succeeded run，不重复发布
    run_dup = create_batch_run(db, workspace_id=ws_id, page_ids=["p1", "p2"])
    db.commit()
    assert run_dup.id == run.id


# ---------------------------------------------------------------------------
# 5. batch 多来源（8 页超阈值）→ 真实 Map-Reduce
# ---------------------------------------------------------------------------


def test_batch_mapreduce_triggered_many_sources(db):
    register_default_pipeline()
    page_ids = []
    ws_id = None
    for i in range(8):
        pid = f"s{i}"
        wid, _ = _mk_page(db, pid, f"来源{i}", f"来源{i}的独特超长正文内容。" * 400,
                          notebook_id="nb-1")
        ws_id = ws_id or wid
        page_ids.append(pid)
    contexts: list[str] = []
    llm = _mk_sync_llm(
        ops=[{"action": "update", "title": "超长主题"}], contexts=contexts,
        long_batch=True, mapreduce_body="mapreduce 最终正文")
    executor.configure_external_runners(llm_runner=llm, graph_runner=_graph_noop)

    run = _run_batch(db, ws_id, page_ids)
    assert run.status == "succeeded", run.safe_error_code
    assert contexts.count("wiki-mapreduce") == 1, "应真实触发一次最终 Map-Reduce"
    assert contexts.count("wiki-batch-summary") >= 2, "多来源应分批 batch-summary"
    wiki = _single_wiki(db)
    assert wiki is not None and wiki.status == "published"
    rev = db.get(WikiRevision, wiki.current_revision_id)
    facts = db.query(WikiSection).filter(
        WikiSection.revision_id == rev.id, WikiSection.section_type == "facts"
    ).first()
    assert facts is not None and "mapreduce 最终正文" in (facts.content or "")
    assert json.loads(wiki.source_page_ids or "[]") == sorted(page_ids)


# ---------------------------------------------------------------------------
# 6. resolve_context batch：读 Artifact 恢复 page_ids
# ---------------------------------------------------------------------------


def test_resolve_context_batch_reads_artifact(db):
    register_default_pipeline()
    ws_id, _ = _mk_page(db, "p2", "来源二", "内容二足够长用于编译测试构建。")
    _, p1 = _mk_page(db, "p1", "来源一", "内容一足够长用于编译测试构建。")

    run = create_batch_run(db, workspace_id=ws_id, page_ids=["p2", "p1"])
    db.commit()

    context = _resolve_context_batch(db, run)
    assert context["applicable"] is True
    assert context["page_ids"] == ["p1", "p2"]
    assert context["page_input_hashes"] == {
        "p1": _expected_page_hash(db, "p1"),
        "p2": _expected_page_hash(db, "p2"),
    }
    assert context["workspace_id"] == ws_id
    assert context["trigger_type"] == "batch_rebuild"
    assert context["input_hash"] == run.input_hash


# ---------------------------------------------------------------------------
# 7. batch 内不调用 legacy process_page_wiki
# ---------------------------------------------------------------------------


def test_batch_never_calls_legacy_process_page_wiki(db, monkeypatch):
    register_default_pipeline()
    ws_id, _ = _mk_page(db, "p1", "来源一", "内容一足够长用于编译测试构建。")
    _, p2 = _mk_page(db, "p2", "来源二", "内容二足够长用于编译测试构建。",
                     notebook_id="nb-1")

    def _boom(*args, **kwargs):
        raise AssertionError("batch 不得调用 legacy process_page_wiki")

    monkeypatch.setattr(builder, "_legacy_process_page_wiki", _boom)
    llm = _mk_sync_llm(ops=[{"action": "create", "title": "单页主题"}])
    executor.configure_external_runners(llm_runner=llm, graph_runner=_graph_noop)

    run = _run_batch(db, ws_id, ["p1", "p2"])
    assert run.status == "succeeded", run.safe_error_code


# ---------------------------------------------------------------------------
# batch_build / two_phase_build driver 冒烟（旧 build_wiki_from_pages 返回键兼容）
# ---------------------------------------------------------------------------


def test_batch_build_driver_stats_compat(db):
    register_default_pipeline()
    ws_id, _ = _mk_page(db, "p1", "来源一", "内容一足够长用于编译测试构建。")
    _, p2 = _mk_page(db, "p2", "来源二", "内容二足够长用于编译测试构建。",
                     notebook_id="nb-1")
    contexts: list[str] = []
    llm = _mk_sync_llm(ops=[{"action": "update", "title": "驱动主题"}], contexts=contexts)
    executor.configure_external_runners(llm_runner=llm, graph_runner=_graph_noop)

    stats = batch_build(db, ws_id, ["p1", "p2"])
    assert stats["failed"] == 0
    assert stats["created"] == 1
    wiki = _single_wiki(db)
    assert wiki is not None and wiki.status == "published"
    assert db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count() == 1

    # two_phase_build（兼容别名）逐 page 推导 workspace → batch run（update 既有 wiki）
    _, p3 = _mk_page(db, "p3", "来源三", "内容三足够长用于编译测试构建。", notebook_id="nb-1")
    stats2 = two_phase_build(db, [db.get(Page, "p3")])
    assert stats2["failed"] == 0
    assert stats2["created"] == 0 and stats2["updated"] == 1
    db.expire_all()
    wiki = _single_wiki(db)
    assert wiki is not None and wiki.status == "published"
    assert set(json.loads(wiki.source_page_ids or "[]")) == {"p1", "p2", "p3"}
    assert db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count() == 2
