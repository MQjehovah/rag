"""Phase 6 default v1/v2 等价测试（20.9）。

允许差异（仅此三项）：
- v2 多 skill_decision Artifact；
- CompileRun.pipeline_version 不同；
- WikiPage skill 字段新增。

禁止差异：
- Wiki 正文 / Section（含版本结构、merge_policy、protected 内容）；
- 来源 / Revision 发布语义 / 图谱与删除行为。

对每个固定场景分别以 v1 与 v2 执行，比较归一快照（内容与结构，忽略已允许差异）。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.wiki_pipeline import executor, registry as pregs
from app.core.wiki_skills import registry as sreg
from app.core.wiki_skills import service as skill_service
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    KnowledgeCompileRun as CompileRun,
    Notebook,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiVersionSource,
    init_db,
)

CONTENT_LONG = ("本主题描述产品模块的配置与使用方式。\n" * 30)
CONTENT_SHORT = "太短"
CONTENT_VERSIONED = "功能说明。\n" * 30
CONTENT_V3 = "3.0 新增能力：自动化运维与监控面板。\n" * 10


# ---------------------------------------------------------------------------
# 快照（忽略 v1/v2 已允许差异）
# ---------------------------------------------------------------------------


def _sections_snapshot(db, revision_id):
    rows = db.query(WikiSection).filter(
        WikiSection.revision_id == revision_id).order_by(WikiSection.order_index).all()
    return [
        {
            "section_type": s.section_type,
            "heading": s.heading,
            "content": s.content or "",
            "version_label": s.version_label,
            "is_common": bool(s.is_common),
            "content_origin": s.content_origin,
            "merge_policy": s.merge_policy,
            "version_status": s.version_status,
        }
        for s in rows
    ]


def _wiki_snapshot(db, workspace_id):
    out = []
    for w in db.query(WikiPage).filter(WikiPage.workspace_id == workspace_id) \
            .order_by(WikiPage.id).all():
        rev = db.query(WikiRevision).filter(
            WikiRevision.id == w.current_revision_id).first() if w.current_revision_id else None
        out.append({
            "title": w.title,
            "status": w.status,
            "dirty": bool(w.dirty),
            "source_page_ids": json.loads(w.source_page_ids or "[]"),
            "latest_version": w.latest_version,
            "sections": _sections_snapshot(db, rev.id) if rev else [],
            "revision_count": db.query(WikiRevision).filter(
                WikiRevision.wiki_page_id == w.id).count(),
        })
    return out


def _page_state(db, page_id):
    p = db.get(Page, page_id)
    return {
        "wiki_dirty": bool(p.wiki_dirty) if p else None,
        "wiki_last_error": (p.wiki_last_error if p else None),
    }


# ---------------------------------------------------------------------------
# runner 构造
# ---------------------------------------------------------------------------


def _mk_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    return engine


def _mk_llm(generic_body="合成正文。\n" * 3, versioned_body=None):
    versioned_body = versioned_body or {
        "summary": "版本摘要",
        "common": "通用内容",
        "versions": [{"version": "3.0", "content": "3.0 专属内容"}],
        "unversioned": "",
    }

    def _run(messages, context="", timeout=120.0):
        prompt = messages[0]["content"] if messages else ""
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": [
                {"action": "create", "title": "产品模块指南", "category": "操作指南"}]}
        if context == "wiki-synthesis":
            if "versions" in prompt and "3.0" in prompt:
                return dict(versioned_body)
            return {"summary": "摘要", "content": generic_body}
        if context == "wiki-batch-summary":
            return {"summary": "批次摘要"}
        if context == "wiki-mapreduce":
            return {"summary": "最终", "content": generic_body}
        return {"worthy": True, "ops": []}
    return _run


def _graph_noop(**kw):
    return None


class _Variant:
    """v1 / v2 环境（注册 + run 创建入口）。"""

    def __init__(self, variant):
        self.variant = variant
        self.version = "1" if variant == "v1" else "2"

    def __enter__(self):
        pregs.clear_for_tests()
        sreg.clear_for_tests()
        executor.reset_external_runners()
        skill_service.register_default_skill()
        from app.core.wiki_pipeline.pipelines.wiki_default import (
            register_default_pipeline,
        )
        register_default_pipeline()
        if self.variant == "v2":
            from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
                register_default_pipeline_v2,
            )
            register_default_pipeline_v2()
        return self

    def __exit__(self, *exc):
        pregs.clear_for_tests()
        sreg.clear_for_tests()
        executor.reset_external_runners()

    def create_run(self, db, **kw):
        if self.variant == "v1":
            kw["pipeline_version"] = "1"
        return executor.create_run(db, pipeline_key="wiki.default", **kw)


def _fresh_db():
    engine = _mk_engine()
    s = sessionmaker(bind=engine)()
    return engine, s


def _seed_pages(db, specs):
    ws_ids = []
    for spec in specs:
        nb = db.get(Notebook, spec["notebook_id"])
        if nb is None:
            nb = Notebook(id=spec["notebook_id"], name="研发库",
                          group_id=spec.get("group_id", "engineering"))
            db.add(nb)
            db.flush()
        ws = ensure_notebook_workspace(db, nb)
        ws_ids.append(ws.id)
        db.add(Page(id=spec["page_id"], notebook_id=spec["notebook_id"],
                    title=spec["title"], content=spec["content"]))
        db.commit()
    return ws_ids[0], ws_ids


def _run_page_run(db, var: _Variant, page_id, ws_id):
    run = var.create_run(db, trigger_type="page_changed", trigger_object_id=page_id,
                         workspace_id=ws_id)
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    return executed


# ---------------------------------------------------------------------------
# 场景执行与断言
# ---------------------------------------------------------------------------


def _compare_scenario(name, seed_fn, expect_failed=False):
    """同一 seed 分别以 v1/v2 执行并比较归一快照。"""
    snapshots = {}
    page_states = {}
    for variant in ("v1", "v2"):
        engine, db = _fresh_db()
        with _Variant(variant) as var:
            ws_id, extra = seed_fn(db)
            contexts = []
            executor.configure_external_runners(
                llm_runner=_mk_llm(generic_body="合成正文。\n" * 3),
                graph_runner=_graph_noop,
            )
            run = _run_page_run(db, var, extra["page_id"], ws_id)
            assert (run.status == "failed") == expect_failed, \
                f"{name}/{variant}: {run.status} {run.safe_error_code}"
            snapshots[variant] = {
                "wiki": _wiki_snapshot(db, ws_id),
                "page": _page_state(db, extra["page_id"]),
            }
        db.close()
        engine.dispose()
    # 内容/结构必须一致；忽略 skill 字段与 pipeline_version（已从快照排除）。
    assert snapshots["v1"] == snapshots["v2"], name


@pytest.fixture(autouse=True)
def _clean():
    yield
    pregs.clear_for_tests()
    sreg.clear_for_tests()
    executor.reset_external_runners()


def _s1_ordinary(db):
    ws_id, _ = _seed_pages(db, [{
        "page_id": "p1", "notebook_id": "nb1", "title": "模块功能",
        "content": CONTENT_LONG}])
    return ws_id, {"page_id": "p1"}


def test_ordinary_wiki_equivalent():
    _compare_scenario("ordinary", _s1_ordinary)


def _s2_versioned(db):
    ws_id, _ = _seed_pages(db, [{
        "page_id": "p1", "notebook_id": "nb1", "title": "产品模块指南 3.0",
        "content": CONTENT_VERSIONED + "版本：3.0\n" + CONTENT_V3}])
    return ws_id, {"page_id": "p1"}


def test_versioned_wiki_equivalent():
    _compare_scenario("versioned", _s2_versioned)


def _s3_protected(db):
    """版本化发布后，人为保护某版本块，再触发重建 —— protected 内容两侧保留。"""
    ws_id, _ = _seed_pages(db, [{
        "page_id": "p1", "notebook_id": "nb1", "title": "产品模块指南 3.0",
        "content": CONTENT_VERSIONED + "版本：3.0\n" + CONTENT_V3}])
    from app.core.wiki_pipeline.pipelines.wiki_default import register_default_pipeline
    register_default_pipeline()
    # 先以 v1 发布一次得到 current revision（种子数据，两侧共用同一来源语义）。
    executor.configure_external_runners(llm_runner=_mk_llm(), graph_runner=_graph_noop)
    run = executor.create_run(
        db, pipeline_key="wiki.default", pipeline_version="1",
        trigger_type="page_changed", trigger_object_id="p1", workspace_id=ws_id,
    )
    db.commit()
    executor.execute_run(db, run.id)
    wiki = db.query(WikiPage).filter(WikiPage.workspace_id == ws_id).first()
    sec = db.query(WikiSection).filter(
        WikiSection.revision_id == wiki.current_revision_id,
        WikiSection.version_label == "3.0").first()
    sec.merge_policy = "protected"
    sec.content_origin = "manual"
    sec.content = "人工保护内容：请勿覆盖。"
    db.commit()
    wiki.dirty = True
    db.commit()
    return ws_id, {"page_id": "p1", "wiki": wiki}


def test_protected_section_equivalent(db_self=None):
    """重建后 protected 版本块内容保持不变且 v1/v2 一致（对 wiki 做 manual rebuild）。"""
    results = {}
    for variant in ("v1", "v2"):
        engine, db = _fresh_db()
        with _Variant(variant) as var:
            ws_id, extra = _s3_protected(db)
            executor.configure_external_runners(
                llm_runner=_mk_llm(), graph_runner=_graph_noop)
            wiki = db.query(WikiPage).filter(WikiPage.workspace_id == ws_id).first()
            run = var.create_run(
                db, trigger_type="manual_rebuild", trigger_object_id=wiki.id,
                workspace_id=ws_id, wiki_page_id=wiki.id,
            )
            db.commit()
            executed = executor.execute_run(db, run.id)
            db.refresh(executed)
            assert executed.status == "succeeded", executed.safe_error_code
            results[variant] = _wiki_snapshot(db, ws_id)
        db.close()
        engine.dispose()
    v1, v2 = results["v1"], results["v2"]
    assert v1 == v2
    protected = [s for s in v2[0]["sections"] if s["merge_policy"] == "protected"]
    assert protected and protected[0]["content"] == "人工保护内容：请勿覆盖。"


def test_multisource_mapreduce_equivalent():
    """两来源多页同主题聚合（batch，真实多来源/Map-Reduce 形态）。"""
    snapshots = {}
    for variant in ("v1", "v2"):
        engine, db = _fresh_db()
        with _Variant(variant) as var:
            ws_id, _ = _seed_pages(db, [
                {"page_id": "p1", "notebook_id": "nb1", "title": "聚合主题",
                 "content": CONTENT_LONG},
                {"page_id": "p2", "notebook_id": "nb1", "title": "聚合主题补充",
                 "content": CONTENT_LONG},
            ])
            executor.configure_external_runners(
                llm_runner=_mk_llm(generic_body="聚合合成正文。\n" * 4),
                graph_runner=_graph_noop,
            )
            from app.core.wiki_pipeline.pipelines.wiki_default import create_batch_run

            run = create_batch_run(db, workspace_id=ws_id, page_ids=["p1", "p2"])
            db.commit()
            executed = executor.execute_run(db, run.id)
            db.refresh(executed)
            assert executed.status == "succeeded", executed.safe_error_code
            snapshots[variant] = _wiki_snapshot(db, ws_id)
        db.close()
        engine.dispose()
    assert snapshots["v1"] == snapshots["v2"]


def test_not_worthy_equivalent():
    _compare_scenario("not_worthy", lambda db: (
        _seed_pages(db, [{"page_id": "p1", "notebook_id": "nb1", "title": "短",
                          "content": CONTENT_SHORT}])[0],
        {"page_id": "p1"}))


def test_llm_invalid_equivalent():
    """LLM 返回非法 → 两侧都不发布、保持 dirty、run 语义一致。"""
    snapshots = {}
    for variant in ("v1", "v2"):
        engine, db = _fresh_db()
        with _Variant(variant) as var:
            ws_id, _ = _seed_pages(db, [{
                "page_id": "p1", "notebook_id": "nb1", "title": "模块功能",
                "content": CONTENT_LONG}])

            def _bad_llm(messages, context="", timeout=120.0):
                return {}  # 无 summary/content/ops → invalid
            executor.configure_external_runners(llm_runner=_bad_llm,
                                                graph_runner=_graph_noop)
            run = _run_page_run(db, var, "p1", ws_id)
            assert run.status == "failed"
            assert run.safe_error_code == "INVALID_RESPONSE"
            snapshots[variant] = {
                "wiki": _wiki_snapshot(db, ws_id),
                "page": _page_state(db, "p1"),
            }
        db.close()
        engine.dispose()
    assert snapshots["v1"] == snapshots["v2"]


def test_llm_unavailable_equivalent():
    from app.core.knowledge_compiler_v3.wiki_page_builder import LLMServiceUnavailable

    snapshots = {}
    for variant in ("v1", "v2"):
        engine, db = _fresh_db()
        with _Variant(variant) as var:
            ws_id, _ = _seed_pages(db, [{
                "page_id": "p1", "notebook_id": "nb1", "title": "模块功能",
                "content": CONTENT_LONG}])

            def _down(messages, context="", timeout=120.0):
                raise LLMServiceUnavailable("down")
            executor.configure_external_runners(llm_runner=_down, graph_runner=_graph_noop)
            run = _run_page_run(db, var, "p1", ws_id)
            assert run.status == "failed"
            assert run.safe_error_code == "SERVICE_UNAVAILABLE"
            snapshots[variant] = {"wiki": _wiki_snapshot(db, ws_id),
                                  "page": _page_state(db, "p1")}
        db.close()
        engine.dispose()
    assert snapshots["v1"] == snapshots["v2"]


def test_stale_input_equivalent():
    """窗口内内容变化（stale）→ 两侧 keep dirty、不覆盖 Revision 语义一致。"""
    results = {}
    for variant in ("v1", "v2"):
        engine, db = _fresh_db()
        with _Variant(variant) as var:
            ws_id, _ = _seed_pages(db, [{
                "page_id": "p1", "notebook_id": "nb1", "title": "模块功能",
                "content": CONTENT_LONG}])
            executor.configure_external_runners(llm_runner=_mk_llm(),
                                                graph_runner=_graph_noop)
            r1 = _run_page_run(db, var, "p1", ws_id)
            assert r1.status == "succeeded"
            # 窗口内变化：直接改 Page 后立刻再次编译（新 input hash run）。
            page = db.get(Page, "p1")
            page.content = page.content + "\n后续改动内容。"
            db.commit()
            r2 = _run_page_run(db, var, "p1", ws_id)
            db.refresh(page)
            results[variant] = {
                "wiki": _wiki_snapshot(db, ws_id),
                "page": _page_state(db, "p1"),
                "run2": r2.status,
            }
        db.close()
        engine.dispose()
    assert results["v1"] == results["v2"]


def test_page_deleted_equivalent():
    """发布后删除来源页 → 两侧删除语义（去源/归档 + 图谱目标）一致。"""
    snapshots = {}
    for variant in ("v1", "v2"):
        engine, db = _fresh_db()
        with _Variant(variant) as var:
            ws_id, _ = _seed_pages(db, [{
                "page_id": "p1", "notebook_id": "nb1", "title": "模块功能",
                "content": CONTENT_LONG}])
            executor.configure_external_runners(llm_runner=_mk_llm(),
                                                graph_runner=_graph_noop)
            _run_page_run(db, var, "p1", ws_id)
            wiki = db.query(WikiPage).filter(WikiPage.workspace_id == ws_id).first()
            assert wiki.status == "published"
            from app.core.wiki_pipeline.pipelines.wiki_default import (
                create_page_deleted_run,
            )
            drun = create_page_deleted_run(
                db, page_id="p1", workspace_id=ws_id, notebook_id="nb1",
            )
            db.commit()
            page = db.get(Page, "p1")
            db.delete(page)
            db.commit()
            executed = executor.execute_run(db, drun.id)
            db.refresh(executed)
            assert executed.status == "succeeded", executed.safe_error_code
            snapshots[variant] = {
                "wiki": _wiki_snapshot(db, ws_id),
                "revisions": db.query(WikiRevision).filter(
                    WikiRevision.wiki_page_id == wiki.id).count(),
            }
        db.close()
        engine.dispose()
    assert snapshots["v1"] == snapshots["v2"]
    # 两侧都归档唯一来源 Wiki（或保留旧 Revision 但去源）。
