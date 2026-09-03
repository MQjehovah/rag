"""Phase 5：wiki.default Pipeline 关键反例（W3 交付）。

对照任务「必须新增测试」反例清单，聚焦 wiki.default 能验证且未被 W1/W2 覆盖的：
- 相同输入幂等不重复 Revision（executor 级 idempotency_key）。
- 新输入 supersede 旧 queued run（executor 级）。
- Topic 候选严格 workspace+ACL（两 workspace 同 title 不误 merge）。
- LLM 非法 JSON / 服务不可用 → 不覆盖已发布 Revision、Page 保持 dirty。
- publish 前输入变化 → STALE_INPUT 不发布（双连接模拟并发修改）。
- 删除语义 page_deleted：唯一来源 → archived（对照旧 remove_source_page_from_wikis）。
- LLM not_worthy（worthy=false）→ 解除来源。
- retry 不重复发布（**当前暴露 wiki.default 缺口**，xfail 固定预期，见缺陷报告）。
- SourceSyncRun 成功状态不受 Wiki Run 失败影响。

真实执行 create_run / execute_run / publish（fake LLM 经 configure_external_runners 注入；
graph_runner 默认空实现；不 mock 发布原语）。DB 一律独立临时文件 SQLite（WAL），便于
双连接模拟并发写入；registry / runner 每测试隔离。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline import registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    PIPELINE_KEY,
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    Page,
    SourceConnection,
    SourceSyncRun,
    WikiPage,
    WikiRevision,
    WikiVersionSource,
    init_db,
)

CREATE_OPS = [{"action": "create", "title": "水箱维护流程", "category": "操作指南"}]
UPDATE_OPS = [{"action": "update", "title": "水箱维护流程", "category": "操作指南"}]
SYN_BODY = "水箱维护需要每日检查水位与温度传感器，并记录运行日志。"


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
    url = f"sqlite:///{(tmp_path / 'p5-anti.db').as_posix()}"
    engine = _new_engine(url)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _phase5_anti_isolation():
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


def _mk_db(tmp_path, name):
    engine = _new_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    return engine, sessionmaker(bind=engine)()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed(db, *, notebook_id="nb-1", page_id="p1", group_id="engineering",
          title="水箱维护", content="水箱固定内容足够长用于构建", wiki_dirty=None):
    nb = db.get(Notebook, notebook_id)
    if nb is None:
        nb = Notebook(id=notebook_id, name=f"n-{notebook_id}", group_id=group_id)
        db.add(nb)
        db.flush()
    ws = ensure_notebook_workspace(db, nb)
    assert ws is not None and ws.status == "active"
    if db.get(Page, page_id) is None:
        kwargs = {}
        if wiki_dirty is not None:
            kwargs["wiki_dirty"] = wiki_dirty
        db.add(Page(id=page_id, notebook_id=notebook_id, title=title,
                    content=content, **kwargs))
    db.flush()
    db.commit()
    return ws.id, db.get(Page, page_id)


def _mk_llm(ops=None, ingest=None, exc=None, synthesis=None):
    """同步 fake llm：ingest 返回 ops；synthesis 返回固定正文。

    ingest: 显式 ingest 返回值；exc: ingest 抛异常（模拟服务不可用）。
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


def _run_page(db, ws_id, page_id, trigger="page_changed", *, supersede=False,
              idempotency_key=None):
    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        trigger_type=trigger,
        trigger_object_id=page_id,
        workspace_id=ws_id,
        idempotency_key=idempotency_key,
        supersede_same_trigger=supersede,
    )
    db.commit()
    return run


def _execute(db, run):
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    return executed


def _parse_source(raw) -> list[str]:
    try:
        return list(json.loads(raw or "[]"))
    except (TypeError, ValueError):
        return []


def _wiki(db, title="水箱维护流程"):
    return db.query(WikiPage).filter(WikiPage.title == title).first()


def _revision_count(db, wiki):
    return db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count()


def _stage_statuses(db, run_id):
    rows = db.query(StageRun).filter(StageRun.run_id == run_id).order_by(
        StageRun.stage_order, StageRun.attempt).all()
    return {r.stage_key: r.status for r in rows}


# ---------------------------------------------------------------------------
# 1. 相同输入幂等：不产生重复 Revision
# ---------------------------------------------------------------------------


def test_idempotent_same_input_single_publish(db, wiki_pipeline):
    ws_id, page = _seed(db)
    _configure(_mk_llm())
    key = f"{PIPELINE_KEY}:{ws_id}:{page.id}:same-input"

    r1 = _run_page(db, ws_id, page.id, idempotency_key=key)
    assert r1.status == "queued"
    r2 = _run_page(db, ws_id, page.id, idempotency_key=key)
    assert r2.id == r1.id, "同 idempotency_key 同指纹应返回原 run（不新建）"

    executed = _execute(db, r1)
    assert executed.status == "succeeded"
    # 终态幂等：再次执行直接返回，不重复发布
    again = _execute(db, r1)
    assert again.status == "succeeded"

    db.expire_all()
    wiki = _wiki(db)
    assert wiki is not None and wiki.status == "published"
    assert _revision_count(db, wiki) == 1


# ---------------------------------------------------------------------------
# 2. 新输入 supersede 旧 queued run（executor 级）→ 只发布一次
# ---------------------------------------------------------------------------


def test_new_input_supersedes_queued_run_single_publish(db, wiki_pipeline):
    ws_id, page = _seed(db)
    _configure(_mk_llm())

    r1 = _run_page(db, ws_id, page.id, supersede=True)
    assert r1.status == "queued"

    # 内容变化 → 新 input_hash → 新 run 抢占旧 queued run
    fresh = db.get(Page, page.id)
    fresh.content = "水箱维护策略变更：新增每日三次巡检与压力释放阀年检记录要求。"
    db.commit()
    r2 = _run_page(db, ws_id, page.id, supersede=True)
    db.expire_all()
    r1 = db.get(CompileRun, r1.id)
    r2 = db.get(CompileRun, r2.id)
    assert r1.status == "superseded"
    assert r2.status == "queued"
    # 被抢占 run 的未终态 stage 闭合为 skipped
    assert all(
        s == "skipped" for s in _stage_statuses(db, r1.id).values()
    )

    executed = _execute(db, r2)
    assert executed.status == "succeeded"

    db.expire_all()
    wiki = _wiki(db)
    assert wiki is not None and wiki.status == "published"
    assert _revision_count(db, wiki) == 1, "supersede 后只发布一次，无重复 Revision"


# ---------------------------------------------------------------------------
# 3. Topic 候选严格 workspace + ACL：两 workspace 同 title 不误 merge
# ---------------------------------------------------------------------------


def test_topic_candidates_workspace_acl_isolated(db, wiki_pipeline):
    ws_a, page_a = _seed(db, notebook_id="nb-g1", page_id="pa", group_id="g1")
    ws_b, page_b = _seed(db, notebook_id="nb-g2", page_id="pb", group_id="g2")
    assert ws_a != ws_b

    _configure(_mk_llm())
    ra = _execute(db, _run_page(db, ws_a, page_a.id))
    assert ra.status == "succeeded"
    rb = _execute(db, _run_page(db, ws_b, page_b.id))
    assert rb.status == "succeeded"

    db.expire_all()
    wikis = db.query(WikiPage).filter(WikiPage.title == "水箱维护流程").all()
    assert len(wikis) == 2, "两 workspace 同名主题各自建页，不得合并"
    by_ws = {w.workspace_id: w for w in wikis}
    assert set(by_ws) == {ws_a, ws_b}
    assert by_ws[ws_a].acl_scope != by_ws[ws_b].acl_scope
    assert _parse_source(by_ws[ws_a].source_page_ids) == ["pa"]
    assert _parse_source(by_ws[ws_b].source_page_ids) == ["pb"]
    # ws_b 的 run 不得改写 ws_a 的 wiki
    assert by_ws[ws_a].source_page_ids and "pb" not in _parse_source(by_ws[ws_a].source_page_ids)


# ---------------------------------------------------------------------------
# 4. LLM 非法 JSON → 不覆盖已发布 Revision、Page 保持 dirty
# ---------------------------------------------------------------------------


def test_invalid_llm_json_keeps_old_revision_and_dirty(db, wiki_pipeline):
    ws_id, page = _seed(db)
    _configure(_mk_llm())
    r1 = _execute(db, _run_page(db, ws_id, page.id))
    assert r1.status == "succeeded"
    db.expire_all()
    wiki = _wiki(db)
    assert wiki is not None and wiki.status == "published"
    old_rev = wiki.current_revision_id
    assert db.get(Page, page.id).wiki_dirty is False

    # 内容变化但 LLM 返回非法响应（worthy=true + ops 空 → invalid_response）
    fresh = db.get(Page, page.id)
    fresh.content = "水箱维护改造：新增水位传感器替换要求，涉及系统停机窗口。"
    db.commit()
    _configure(_mk_llm(ingest={"worthy": True, "ops": []}))
    r2 = _execute(db, _run_page(db, ws_id, page.id))
    assert r2.status == "failed", "非法 LLM 响应 → Run 真实 failed（INVALID_RESPONSE）"
    assert r2.output_revision_id is None

    db.expire_all()
    wiki = _wiki(db)
    assert wiki.current_revision_id == old_rev, "非法响应不得覆盖已发布 Revision"
    assert _revision_count(db, wiki) == 1
    assert wiki.dirty is False, "识别失败不得误置 wiki dirty（无来源变更）"
    assert wiki.status == "published"
    p = db.get(Page, page.id)
    assert p.wiki_dirty is True
    assert p.wiki_last_error == "invalid_response"


# ---------------------------------------------------------------------------
# 5. LLM 不可用（runner 抛异常）→ 不覆盖已发布 Revision、Page 保持 dirty
# ---------------------------------------------------------------------------


def test_llm_unavailable_keeps_old_revision_and_dirty(db, wiki_pipeline):
    ws_id, page = _seed(db)
    _configure(_mk_llm())
    r1 = _execute(db, _run_page(db, ws_id, page.id))
    assert r1.status == "succeeded"
    db.expire_all()
    wiki = _wiki(db)
    old_rev = wiki.current_revision_id

    fresh = db.get(Page, page.id)
    fresh.content = "水箱维护升级：迁移到 PLC 控制系统，新增联锁逻辑要求。"
    db.commit()
    _configure(_mk_llm(exc=RuntimeError("llm down")))
    r2 = _execute(db, _run_page(db, ws_id, page.id))
    assert r2.status == "failed", "LLM 服务不可用 → Run 真实 failed（SERVICE_UNAVAILABLE）"
    assert r2.output_revision_id is None

    db.expire_all()
    wiki = _wiki(db)
    assert wiki.current_revision_id == old_rev
    assert _revision_count(db, wiki) == 1
    p = db.get(Page, page.id)
    assert p.wiki_dirty is True
    assert p.wiki_last_error == "service_unavailable"


# ---------------------------------------------------------------------------
# 6. publish 前输入变化 → STALE_INPUT 不发布（双连接模拟并发修改）
# ---------------------------------------------------------------------------


def test_stale_input_page_changed_during_run_no_publish(db, wiki_pipeline):
    ws_id, page = _seed(db)
    Session = sessionmaker(bind=db.get_bind())

    def _mutating_ingest(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            # 识别（topic_route）期间另一连接并发改写 Page 输入
            s = Session()
            try:
                p = s.get(Page, page.id)
                p.content = "水箱维护内容在识别期间被并发修改，长度足以绕过短内容判定分支。"
                s.commit()
            finally:
                s.close()
        return {"worthy": True, "ops": CREATE_OPS}

    executor.configure_external_runners(llm_runner=_mutating_ingest,
                                        graph_runner=_graph_noop)
    run = _execute(db, _run_page(db, ws_id, page.id))
    assert run.status == "failed", "stale input → Run 真实 failed（STALE_INPUT）"
    assert run.output_revision_id is None

    db.expire_all()
    assert db.query(WikiPage).count() == 0, "stale 输入不得落库 wiki/revision"
    p = db.get(Page, page.id)
    assert p.wiki_dirty is True
    assert p.wiki_last_error == "input_changed_during_llm"


# ---------------------------------------------------------------------------
# 7. page_deleted：唯一来源 → archived（对照旧 remove_source_page_from_wikis 语义）
# ---------------------------------------------------------------------------

# 注（如实记录）：W2 契约本期 schedule_page_deleted 未迁移调度入口；本反例验证
# wiki.default 自身对 page_deleted run 的 remove_source 语义。create_run 要求 Page 行
# 仍存在（删除前 resolve 建 run 的契约场景）；物理删除后无行无法建 run。

_ARCHIVED_FIELDS = (
    "status", "dirty", "source_page_ids",
)


def _deleted_wiki_fields(db, title="水箱维护流程"):
    wiki = db.query(WikiPage).filter(WikiPage.title == title).first()
    if wiki is None:
        return None
    fields = [(f, getattr(wiki, f)) for f in _ARCHIVED_FIELDS]
    rev = db.get(WikiRevision, wiki.current_revision_id) if wiki.current_revision_id else None
    fields.append(("has_current_revision", rev is not None))
    fields.append(("current_revision_status", rev.status if rev else None))
    return tuple(fields)


def test_page_deleted_unique_source_archives_pipeline(tmp_path, wiki_pipeline):
    _old_eng, old_db = _mk_db(tmp_path, "old-del.db")
    _new_eng, new_db = _mk_db(tmp_path, "new-del.db")
    try:
        # 库A（新）：create publish → page_deleted run
        ws_b, page_b = _seed(new_db, notebook_id="nb-1", page_id="p1")
        _configure(_mk_llm())
        run = _execute(new_db, _run_page(new_db, ws_b, page_b.id))
        assert run.status == "succeeded"
        _execute(new_db, _run_page(new_db, ws_b, page_b.id, trigger="page_deleted"))
        new_db.expire_all()
        w = _wiki(new_db)
        assert w.status == "archived" and w.dirty is False
        assert _parse_source(w.source_page_ids) == []
        assert (
            new_db.query(WikiVersionSource)
            .filter(WikiVersionSource.wiki_page_id == w.id).count()
        ) == 0

        # 库B（旧）：create publish → remove_source_page_from_wikis
        ws_a, page_a = _seed(old_db, notebook_id="nb-1", page_id="p1")
        asyncio.run(builder._legacy_build_wiki_from_pages(
            old_db, [page_a], llm_json=_async(_mk_llm()), commit=True))
        builder.remove_source_page_from_wikis(old_db, page_a.id, commit=True)
        old_db.expire_all()

        assert _deleted_wiki_fields(new_db) == _deleted_wiki_fields(old_db), \
            "pipeline page_deleted 终态必须与旧 remove_source 一致"
    finally:
        old_db.close()
        _old_eng.dispose()
        new_db.close()
        _new_eng.dispose()


def _async(llm):
    async def _call(messages, context="", timeout=120.0):
        return llm(messages, context=context, timeout=timeout)
    return _call


# ---------------------------------------------------------------------------
# 8. LLM not_worthy（worthy=false）→ 解除来源、唯一来源 wiki archived
# ---------------------------------------------------------------------------


def test_llm_not_worthy_detaches_source_and_archives(db, wiki_pipeline):
    ws_id, page = _seed(db)
    _configure(_mk_llm())
    r1 = _execute(db, _run_page(db, ws_id, page.id))
    assert r1.status == "succeeded"
    db.expire_all()
    wiki = _wiki(db)
    assert wiki.status == "published"
    assert _parse_source(wiki.source_page_ids) == ["p1"]

    _configure(_mk_llm(ingest={"worthy": False, "ops": []}))
    r2 = _execute(db, _run_page(db, ws_id, page.id))
    assert r2.status == "succeeded"
    assert r2.output_revision_id is None

    db.expire_all()
    wiki = _wiki(db)
    assert wiki.status == "archived"
    assert wiki.dirty is False
    assert _parse_source(wiki.source_page_ids) == []
    # not_worthy 是产品判定，不是故障：Page dirty 被清、无 last_error
    p = db.get(Page, page.id)
    assert p.wiki_dirty is False
    assert p.wiki_last_error is None
    assert p.wiki_compiled_content_hash is not None


# ---------------------------------------------------------------------------
# 9. retry 不重复发布
# ---------------------------------------------------------------------------
# publish_default 已加「本 run 已成功发布（run.output_revision_id 非空）→ 幂等
# 不重复 append Revision」守卫：publish 成功后下游 schedule_graph 失败 → run failed，
# retry 重放 publish 时命中守卫 no-op，不重复发布。本测试为正式通过用例。


def test_retry_after_schedule_graph_failure_does_not_duplicate_publish(db, wiki_pipeline):
    ws_id, page = _seed(db)
    _configure(_mk_llm(), graph=_raise_graph)
    r1 = _execute(db, _run_page(db, ws_id, page.id))
    assert r1.status == "failed"  # publish 已生效，仅 schedule_graph 失败

    db.expire_all()
    wiki = _wiki(db)
    assert wiki is not None and wiki.status == "published"
    first_rev = wiki.current_revision_id
    assert _revision_count(db, wiki) == 1
    assert r1.output_revision_id == first_rev

    # 图谱恢复 → retry 同一 run → 不应再次发布 Revision
    _configure(_mk_llm(), graph=_graph_noop)
    executor.retry_run(db, r1.id)
    db.commit()
    r2 = _execute(db, r1)
    assert r2.status == "succeeded"

    db.expire_all()
    wiki = _wiki(db)
    assert _revision_count(db, wiki) == 1, "retry 不得重复发布 Revision"
    assert wiki.current_revision_id == first_rev


def _raise_graph(**kw):
    raise RuntimeError("graph backend down")


# ---------------------------------------------------------------------------
# 10. SourceSyncRun 成功状态不受 Wiki Run 失败影响
# ---------------------------------------------------------------------------


def test_source_sync_run_unaffected_by_failed_wiki_run(db, wiki_pipeline):
    ws_id, page = _seed(db)
    conn = SourceConnection(
        id="conn-1", connector_key="gitlab", name="conn", enabled=True,
        target_notebook_id=page.notebook_id,
    )
    db.add(conn)
    ssr = SourceSyncRun(
        id="ssr-1", connection_id="conn-1", mode="incremental", status="succeeded",
    )
    db.add(ssr)
    db.commit()

    _configure(_mk_llm(), graph=_raise_graph)
    run = executor.create_run(
        db,
        pipeline_key=PIPELINE_KEY,
        trigger_type="page_changed",
        trigger_object_id=page.id,
        workspace_id=ws_id,
        source_sync_run_id="ssr-1",
    )
    db.commit()
    executed = _execute(db, run)
    assert executed.status == "failed"  # publish 成功但 schedule_graph 失败

    db.expire_all()
    row = db.get(SourceSyncRun, "ssr-1")
    assert row.status == "succeeded", "Wiki run 失败不得改写 SourceSyncRun 状态"
    assert row.finished_at is None
    assert row.error_summary is None
