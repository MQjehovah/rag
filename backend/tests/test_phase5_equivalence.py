"""Phase 5：wiki.default Pipeline 端到端输出 与 旧 Builder 直接编译输出 的产品语义等价。

W3 交付。对**同一固定 seed**，在两张独立库上分别执行：
- 旧路径：builder.build_wiki_from_pages / builder.rebuild_wiki_from_sources（fake async LLM）；
- 新路径：注册 wiki.default → executor.create_run → execute_run（fake LLM 经
  configure_external_runners 注入，graph_runner 空实现，无真实 LLM / 无真实 DB / 无 HTTP）。

断言方式：比较 DB 语义字段快照（WikiPage / current WikiRevision / WikiSections /
WikiVersionSource / Page.wiki_dirty·hash·last_error），排除 id / 时间戳 / workspace uuid
口径差异（workspace 归一为 key）。覆盖等价测试策略清单 1~6 中 wiki.default 支持的
单 page / 单 wiki 驱动场景；多 Page 两阶段聚合（build_wiki_from_pages dedupe_synthesis）
本期 wiki.default 不承诺等价，Map-Reduce 触发不在单 page 触发下可达 —— 如实记录不入测试。

真实差异风险点（topic 识别 / not_worthy / 删除 / dirty 终态 / 版本块保护）是断言重点，
因此每条等价断言都要求两边语义快照**逐字段相等**。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3.wiki_lifecycle import set_section_protection_current
from app.core.wiki_pipeline import executor
from app.core.wiki_pipeline import registry
from app.core.wiki_pipeline.pipelines.wiki_default import (
    PIPELINE_KEY,
    register_default_pipeline,
    unregister_default_pipeline,
)
from app.core.wiki_workspace.routing import ensure_notebook_workspace
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiVersionSource,
    WikiWorkspace,
    init_db,
)

GROUP = "engineering"
ACL = '{"groups": ["engineering"]}'
SCOPE_ID = "group:engineering"

SYN_BODY = "水箱维护需要每日检查水位与温度传感器，并记录运行日志。"

CREATE_TITAN_OPS = [{"action": "create", "title": "Titan 安装说明", "category": "部署运维"}]
UPDATE_TITAN_OPS = [{"action": "update", "title": "Titan 安装说明", "category": "部署运维"}]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    return engine, sessionmaker(bind=engine)()


@pytest.fixture()
def db():
    """旧路径库（A）。"""
    engine, s = _make_session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def db_b():
    """新路径库（B）。"""
    engine, s = _make_session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _phase5_equiv_isolation():
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


# ---------------------------------------------------------------------------
# seed / fake llm
# ---------------------------------------------------------------------------


def _seed(db, *, notebook_id="nb-1", page_id="p1", title="水箱维护",
          content="水箱固定内容足够长用于构建", source_path=None):
    """同一固定 seed：notebook + 默认私用 workspace + page（两库可分别调用）。"""
    nb = db.get(Notebook, notebook_id)
    if nb is None:
        nb = Notebook(id=notebook_id, name=f"n-{notebook_id}", group_id=GROUP)
        db.add(nb)
        db.flush()
    ws = ensure_notebook_workspace(db, nb)
    assert ws is not None and ws.status == "active"
    if db.get(Page, page_id) is None:
        db.add(Page(id=page_id, notebook_id=notebook_id, title=title,
                    content=content, source_path=source_path))
    db.flush()
    db.commit()
    return ws.id, db.get(Page, page_id)


def _shared_ws(db, ws_id="ws-shared"):
    """显式共享 workspace（工程组）。"""
    ws = db.get(WikiWorkspace, ws_id)
    if ws is None:
        ws = WikiWorkspace(id=ws_id, key=f"key-{ws_id}", name="共享-工程",
                           acl_scope=ACL, scope_id=SCOPE_ID, status="active")
        db.add(ws)
        db.flush()
    return ws


def _seed_shared(db, *, notebook_id, page_id, title, content,
                 ws_id="ws-shared", source_path=None):
    """同 ACL 多 notebook 显式绑定同一共享 workspace 后落 Page（供版本化合并）。"""
    ws = _shared_ws(db, ws_id)
    nb = db.get(Notebook, notebook_id)
    if nb is None:
        nb = Notebook(id=notebook_id, name=f"n-{notebook_id}", group_id=GROUP)
        db.add(nb)
        db.flush()
    if (
        db.query(NotebookWorkspaceBinding)
        .filter(NotebookWorkspaceBinding.notebook_id == notebook_id,
                NotebookWorkspaceBinding.workspace_id == ws.id)
        .first()
        is None
    ):
        db.add(NotebookWorkspaceBinding(
            notebook_id=notebook_id, workspace_id=ws.id, status="active",
        ))
        db.flush()
    if db.get(Page, page_id) is None:
        db.add(Page(id=page_id, notebook_id=notebook_id, title=title,
                    content=content, source_path=source_path))
    db.flush()
    db.commit()
    return ws, db.get(Page, page_id)


class FakeWikiLlm:
    """按 context 分流的确定性 fake LLM（同步 runner；旧路径经 async 包装复用）。

    - wiki-ingest-page → ingest JSON（worthy + ops，可 per-call 覆盖 ingest_ops）。
    - wiki-synthesis 且 versioned_content=True → 版本化 JSON（按 prompt 含版本生成）；
    - wiki-synthesis 其他 → {summary, content}。
    记录调用 context 序列，便于断言「识别恰好一次、合成恰好一次」。
    """

    def __init__(self, ops=None, *, versioned_content=False, synthesis=None):
        self.ops = ops
        self.versioned_content = versioned_content
        self.synthesis = synthesis
        self.contexts: list[str] = []
        self.prompts: list[str] = []

    def __call__(self, messages, context="", timeout=120.0):
        self.contexts.append(context)
        prompt = (messages[0]["content"] if messages else "") or ""
        self.prompts.append(prompt)
        if context == "wiki-ingest-page":
            if self.ops is None:
                return {"worthy": True, "ops": [
                    {"action": "create", "title": "水箱维护流程", "category": "操作指南"}]}
            return {"worthy": True, "ops": self.ops}
        if self.synthesis is not None:
            return self.synthesis
        if self.versioned_content:
            versions = []
            for label in ("4.0", "3.0", "2.0"):
                if label in prompt:
                    versions.append({
                        "version": label,
                        "content": f"{label} 使用安装接口，路径 /etc/titan-v{label[0]}/config",
                        "diff_notice": "",
                    })
            return {
                "summary": "Titan 安装说明",
                "common": "安装前关闭服务。",
                "versions": versions,
                "unversioned": "",
            }
        return {"summary": "水箱维护流程摘要", "content": SYN_BODY}


def _async_llm(llm):
    """旧 Builder 期望 async llm(messages, context) → dict。"""
    async def _call(messages, context="", timeout=120.0):
        return llm(messages, context=context, timeout=timeout)
    return _call


def _run_old_build(db, pages, llm):
    """旧路径：单页/增量 build_wiki_from_pages（len<=1 → 增量识别+合成）。"""
    return asyncio.run(builder.build_wiki_from_pages(
        db, pages, llm_json=_async_llm(llm), commit=True))


def _run_old_rebuild(db, wiki_id, llm):
    return asyncio.run(builder.rebuild_wiki_from_sources(db, wiki_id, _async_llm(llm), commit=True))


def _graph_noop(**kw):
    return None


def _run_new_page(db, ws_id, page_id, llm, graph=None):
    executor.configure_external_runners(llm_runner=llm, graph_runner=graph or _graph_noop)
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="page_changed",
        trigger_object_id=page_id, workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    return executed


def _run_new_rebuild(db, ws_id, wiki_id, llm, graph=None):
    executor.configure_external_runners(llm_runner=llm, graph_runner=graph or _graph_noop)
    run = executor.create_run(
        db, pipeline_key=PIPELINE_KEY, trigger_type="manual_rebuild",
        wiki_page_id=wiki_id, workspace_id=ws_id,
    )
    db.commit()
    executed = executor.execute_run(db, run.id)
    db.refresh(executed)
    return executed


# ---------------------------------------------------------------------------
# DB 语义快照（排除 id / 时间戳 / workspace uuid）
# ---------------------------------------------------------------------------


def _ws_key(db, ws_id):
    if not ws_id:
        return None
    ws = db.get(WikiWorkspace, ws_id)
    return ws.key if ws is not None else ws_id


def _section_semantics(sec: WikiSection) -> dict:
    return {
        "section_type": sec.section_type,
        "content": sec.content or "",
        "order_index": sec.order_index,
        "version_label": sec.version_label,
        "is_common": sec.is_common,
        "content_origin": sec.content_origin,
        "merge_policy": sec.merge_policy,
        "locked": bool(sec.locked),
        "version_status": sec.version_status,
        "diff_notice": sec.diff_notice or "",
    }


def _sections(db, rev_id):
    rows = (
        db.query(WikiSection)
        .filter(WikiSection.revision_id == rev_id)
        .order_by(WikiSection.order_index, WikiSection.version_label, WikiSection.content)
        .all()
    )
    return [_section_semantics(s) for s in rows]


def _revision_semantics(db, rev_id):
    rev = db.get(WikiRevision, rev_id)
    if rev is None:
        return None
    return {
        "summary": rev.summary or "",
        "status": rev.status,
        "edit_type": rev.edit_type,
        "source_hash": rev.source_hash,
        "sections": _sections(db, rev.id),
    }


def _version_sources(db, wiki_id):
    rows = (
        db.query(WikiVersionSource)
        .filter(WikiVersionSource.wiki_page_id == wiki_id)
        .order_by(WikiVersionSource.version_label, WikiVersionSource.page_id)
        .all()
    )
    return [
        {"version_label": v.version_label, "page_id": v.page_id, "acl_scope": v.acl_scope}
        for v in rows
    ]


def _wiki_semantics(db) -> list[dict]:
    """全部 WikiPage 的语义快照（无 id / 时间戳 / workspace uuid）。"""
    out = []
    for wp in sorted(db.query(WikiPage).all(), key=lambda w: (w.title or "", w.id)):
        out.append({
            "title": wp.title,
            "category": wp.category,
            "status": wp.status,
            "dirty": bool(wp.dirty),
            "summary": wp.summary or "",
            "latest_version": wp.latest_version,
            "acl_scope": wp.acl_scope,
            "workspace_key": _ws_key(db, wp.workspace_id),
            "source_page_ids": sorted(_parse_source_ids(wp.source_page_ids)),
            "revision": _revision_semantics(db, wp.current_revision_id),
            "version_sources": _version_sources(db, wp.id),
        })
    return out


def _parse_source_ids(raw) -> list[str]:
    try:
        return list(json.loads(raw or "[]"))
    except (TypeError, ValueError):
        return []


def _page_state(db) -> dict:
    """Page 级终态语义（dirty / compiled_hash / last_error）。"""
    return {
        p.id: {
            "wiki_dirty": bool(p.wiki_dirty),
            "wiki_compiled_content_hash": p.wiki_compiled_content_hash,
            "wiki_last_error": p.wiki_last_error,
        }
        for p in db.query(Page).order_by(Page.id).all()
    }


def _db_refresh(db):
    db.expire_all()


# ---------------------------------------------------------------------------
# 1. 单源 create → publish 等价
# ---------------------------------------------------------------------------


def test_equiv_single_page_create_publish(db, db_b, wiki_pipeline):
    ws_a, page_a = _seed(db, notebook_id="nb-1", page_id="p1")
    ws_b, page_b = _seed(db_b, notebook_id="nb-1", page_id="p1")
    llm_a = FakeWikiLlm()
    llm_b = FakeWikiLlm()

    old = _run_old_build(db, [page_a], llm_a)
    assert old["created"] == 1

    run = _run_new_page(db_b, ws_b, page_b.id, llm_b)
    assert run.status == "succeeded"
    assert run.output_revision_id

    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)
    assert _page_state(db_b) == _page_state(db)

    # 语义抽查：published / workspace 非空 / source 归属 / Page 清理
    wiki_b = db_b.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
    assert wiki_b.status == "published" and wiki_b.dirty is False
    assert wiki_b.workspace_id == ws_b
    assert "p1" in _parse_source_ids(wiki_b.source_page_ids)
    assert _page_state(db_b)["p1"]["wiki_dirty"] is False
    assert llm_b.contexts.count("wiki-ingest-page") == 1
    assert llm_b.contexts.count("wiki-synthesis") == 1


# ---------------------------------------------------------------------------
# 2. 更新已有 Wiki（内容变化 → update 而非 create）等价
# ---------------------------------------------------------------------------


def test_equiv_content_change_update_existing_wiki(db, db_b, wiki_pipeline):
    ws_a, page_a = _seed(db, notebook_id="nb-1", page_id="p1")
    ws_b, page_b = _seed(db_b, notebook_id="nb-1", page_id="p1")

    _run_old_build(db, [page_a], FakeWikiLlm())
    _run_new_page(db_b, ws_b, page_b.id, FakeWikiLlm())
    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)

    update_ops = [{"action": "update", "title": "水箱维护流程", "category": "操作指南"}]
    for _db, page in ((db, page_a), (db_b, page_b)):
        fresh = _db.get(Page, page.id)
        fresh.content = "水箱维护需要每四小时检查一次水位并补充软化水，记录温度曲线。"
        _db.commit()

    llm_a = FakeWikiLlm(ops=update_ops)
    llm_b = FakeWikiLlm(ops=update_ops)
    old2 = _run_old_build(db, [db.get(Page, page_a.id)], llm_a)
    assert old2["updated"] == 1
    run2 = _run_new_page(db_b, ws_b, page_b.id, llm_b)
    assert run2.status == "succeeded"

    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)
    assert _page_state(db_b) == _page_state(db)

    # 不新建（两库均唯一一篇 title）、revision 有第二条历史
    for s in (db, db_b):
        assert s.query(WikiPage).filter(WikiPage.title == "水箱维护流程").count() == 1
        wiki = s.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
        assert s.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count() == 2
        assert SYN_BODY in _facts_content(s, wiki)


def _facts_content(db, wiki):
    rev = db.get(WikiRevision, wiki.current_revision_id)
    sec = (
        db.query(WikiSection)
        .filter(WikiSection.revision_id == rev.id,
                WikiSection.section_type == "facts",
                WikiSection.version_label.in_((None, "unversioned")))
        .first()
    )
    return sec.content or "" if sec else None


# ---------------------------------------------------------------------------
# 3. 版本化 create → publish 等价（common + 版本块 + unversioned / latest /
#    WikiVersionSource 对齐）
# ---------------------------------------------------------------------------


def test_equiv_versioned_page_create_publish(db, db_b, wiki_pipeline):
    ws_a = _seed_shared(db, notebook_id="nb-20", page_id="p20",
                        title="Titan 2.0 安装说明",
                        content="Titan 2.0 使用旧版安装接口，配置路径 /etc/titan-v2/config。安装前关闭服务。")[0]
    ws_b = _seed_shared(db_b, notebook_id="nb-20", page_id="p20",
                        title="Titan 2.0 安装说明",
                        content="Titan 2.0 使用旧版安装接口，配置路径 /etc/titan-v2/config。安装前关闭服务。")[0]

    llm_a = FakeWikiLlm(ops=CREATE_TITAN_OPS, versioned_content=True)
    llm_b = FakeWikiLlm(ops=CREATE_TITAN_OPS, versioned_content=True)
    old = _run_old_build(db, [db.get(Page, "p20")], llm_a)
    assert old["created"] == 1
    run = _run_new_page(db_b, ws_b.id, "p20", llm_b)
    assert run.status == "succeeded"

    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)
    assert _page_state(db_b) == _page_state(db)

    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
        assert wiki.latest_version == "2.0"
        rev = s.get(WikiRevision, wiki.current_revision_id)
        labels = {
            sec.version_label
            for sec in s.query(WikiSection).filter(WikiSection.revision_id == rev.id).all()
        }
        assert "common" in labels and "2.0" in labels
        srcs = {
            v.version_label for v in
            s.query(WikiVersionSource).filter(WikiVersionSource.wiki_page_id == wiki.id).all()
        }
        assert "2.0" in srcs


# ---------------------------------------------------------------------------
# 4. protected 版本块保留等价：先手动保护 → 再新 run/新 source 刷新 → 不覆盖
# ---------------------------------------------------------------------------


def test_equiv_protected_version_block_survives_refresh(db, db_b, wiki_pipeline):
    for s in (db, db_b):
        _seed_shared(s, notebook_id="nb-20", page_id="p20",
                     title="Titan 2.0 安装说明",
                     content="Titan 2.0 使用旧版安装接口，配置路径 /etc/titan-v2/config。安装前关闭服务。")
    _run_old_build(db, [db.get(Page, "p20")], FakeWikiLlm(ops=CREATE_TITAN_OPS, versioned_content=True))
    _run_new_page(db_b, "ws-shared", "p20", FakeWikiLlm(ops=CREATE_TITAN_OPS, versioned_content=True))
    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)

    # 各自手动保护 2.0 版本块（语义等价的人工编辑）
    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
        rev = s.get(WikiRevision, wiki.current_revision_id)
        sec = (
            s.query(WikiSection)
            .filter(WikiSection.revision_id == rev.id, WikiSection.version_label == "2.0")
            .first()
        )
        set_section_protection_current(s, wiki, sec.id, protected=True, updated_by="editor1")
        s.commit()
    _db_refresh(db)
    _db_refresh(db_b)
    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
        rev = s.get(WikiRevision, wiki.current_revision_id)
        sec = (
            s.query(WikiSection)
            .filter(WikiSection.revision_id == rev.id, WikiSection.version_label == "2.0")
            .first()
        )
        assert sec.merge_policy == "protected" and sec.content_origin == "manual"

    # 导入 3.0 来源 → update：2.0 块应保留人工内容，3.0 自动加入，latest=3.0
    for s in (db, db_b):
        _seed_shared(s, notebook_id="nb-30", page_id="p30",
                     title="Titan 3.0 安装说明",
                     content="Titan 3.0 使用新版安装接口，配置路径 /etc/titan-v3/config。安装前关闭服务。",
                     ws_id="ws-shared")

    old2 = _run_old_build(db, [db.get(Page, "p30")],
                          FakeWikiLlm(ops=UPDATE_TITAN_OPS, versioned_content=True))
    assert old2["updated"] >= 1
    run2 = _run_new_page(db_b, "ws-shared", "p30",
                         FakeWikiLlm(ops=UPDATE_TITAN_OPS, versioned_content=True))
    assert run2.status == "succeeded"

    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)
    assert _page_state(db_b) == _page_state(db)

    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "Titan 安装说明").first()
        assert wiki.latest_version == "3.0"
        assert "p20" in _parse_source_ids(wiki.source_page_ids)
        assert "p30" in _parse_source_ids(wiki.source_page_ids)
        rev = s.get(WikiRevision, wiki.current_revision_id)
        secs = {
            sec.version_label: sec
            for sec in s.query(WikiSection).filter(WikiSection.revision_id == rev.id).all()
        }
        # 2.0 人工保护保留（内容 origin manual，不受自动刷新覆盖）
        assert secs["2.0"].merge_policy == "protected"
        assert secs["2.0"].content_origin == "manual"
        # 3.0 自动加入且可刷新
        assert "3.0" in secs
        assert secs["3.0"].merge_policy == "auto"


# ---------------------------------------------------------------------------
# 5. not_worthy / 短内容等价（无产品写；Page 终态一致）
# ---------------------------------------------------------------------------


def test_equiv_short_content_not_worthy(db, db_b, wiki_pipeline):
    ws_a, page_a = _seed(db, notebook_id="nb-1", page_id="p1", title="短", content="短")
    ws_b, page_b = _seed(db_b, notebook_id="nb-1", page_id="p1", title="短", content="短")

    old = _run_old_build(db, [page_a], FakeWikiLlm())
    assert old["not_worthy"] == 1

    run = _run_new_page(db_b, ws_b, page_b.id, FakeWikiLlm())
    assert run.status == "succeeded"
    assert run.output_revision_id is None

    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)
    assert _page_state(db_b) == _page_state(db)
    assert db.query(WikiPage).count() == 0 and db_b.query(WikiPage).count() == 0
    # not_worthy 清 dirty（镜像 _finalize_page），不视为故障
    assert _page_state(db)["p1"]["wiki_dirty"] is False
    assert _page_state(db)["p1"]["wiki_last_error"] is None


# ---------------------------------------------------------------------------
# 6. dirty Wiki 重建等价：manual_rebuild（新） vs rebuild_wiki_from_sources（旧）
# ---------------------------------------------------------------------------


def test_equiv_manual_rebuild_dirty_wiki(db, db_b, wiki_pipeline):
    ws_a, page_a = _seed(db, notebook_id="nb-1", page_id="p1")
    ws_b, page_b = _seed(db_b, notebook_id="nb-1", page_id="p1")

    _run_old_build(db, [page_a], FakeWikiLlm())
    _run_new_page(db_b, ws_b, page_b.id, FakeWikiLlm())
    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)

    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
        wiki.dirty = True
        wiki.status = "draft"
        s.commit()

    llm_a = FakeWikiLlm()
    llm_b = FakeWikiLlm()
    wiki_a = db.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
    old_rebuild = _run_old_rebuild(db, wiki_a.id, llm_a)
    assert old_rebuild["status"] == "success"
    wiki_b = db_b.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
    run = _run_new_rebuild(db_b, ws_b, wiki_b.id, llm_b)
    assert run.status == "succeeded"
    assert run.output_revision_id

    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)
    assert _page_state(db_b) == _page_state(db)
    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
        assert wiki.status == "published" and wiki.dirty is False


# ---------------------------------------------------------------------------
# 7. 多来源累积 + 聚合重建等价（两 Page 归并一篇 Wiki；Map-Reduce 两阶段
#    build_wiki_from_pages 本期不迁移，此测试只覆盖单 page 触发累积语义）
# ---------------------------------------------------------------------------


def test_equiv_multi_source_accumulate_and_rebuild(db, db_b, wiki_pipeline):
    # 同一 notebook（同一默认 workspace）内两篇来源页
    ws_a, p1 = _seed(db, notebook_id="nb-1", page_id="p1")
    _seed(db, notebook_id="nb-1", page_id="p2",
          title="水箱维护流程补充", content="软化水补给采用自动加药装置，每两小时补充一次。")
    ws_b, _ = _seed(db_b, notebook_id="nb-1", page_id="p1")
    _seed(db_b, notebook_id="nb-1", page_id="p2",
          title="水箱维护流程补充", content="软化水补给采用自动加药装置，每两小时补充一次。")

    update_ops = [{"action": "update", "title": "水箱维护流程", "category": "操作指南"}]
    _run_old_build(db, [p1], FakeWikiLlm())
    _run_old_build(db, [db.get(Page, "p2")], FakeWikiLlm(ops=update_ops))
    _run_new_page(db_b, ws_b, "p1", FakeWikiLlm())
    _run_new_page(db_b, ws_b, "p2", FakeWikiLlm(ops=update_ops))

    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)
    assert _page_state(db_b) == _page_state(db)
    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
        assert set(_parse_source_ids(wiki.source_page_ids)) == {"p1", "p2"}
        assert s.query(WikiRevision).filter(WikiRevision.wiki_page_id == wiki.id).count() == 2

    # dirty 化 → 两侧重建：聚合两来源重合成一篇
    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
        wiki.dirty = True
        wiki.status = "draft"
        s.commit()
    _run_old_rebuild(db, db.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first().id,
                     FakeWikiLlm())
    run = _run_new_rebuild(db_b, ws_b,
                           db_b.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first().id,
                           FakeWikiLlm())
    assert run.status == "succeeded"

    _db_refresh(db)
    _db_refresh(db_b)
    assert _wiki_semantics(db_b) == _wiki_semantics(db)
    assert _page_state(db_b) == _page_state(db)
    for s in (db, db_b):
        wiki = s.query(WikiPage).filter(WikiPage.title == "水箱维护流程").first()
        assert set(_parse_source_ids(wiki.source_page_ids)) == {"p1", "p2"}
        assert wiki.status == "published" and wiki.dirty is False
