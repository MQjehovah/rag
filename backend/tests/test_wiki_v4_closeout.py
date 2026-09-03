"""V4 Phase C 最后一组收口验收测试。"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    SourceItem,
    SourceConnection,
    WikiWorkspace,
    init_db,
)

# Phase 3.1：同 ACL 不同 notebook 默认各自 workspace；本文件用「多来源合并成一篇
# Wiki」的 V4 语义，需把各来源 notebook 显式共享绑定到同一 workspace（engineering）。
_SHARED_WS_KEY = "test-shared-ws-wiki_v4_closeout"
_SHARED_WS_ACL = '{"groups": ["engineering"]}'
_SHARED_WS_SCOPE_ID = "group:engineering"


def _shared_workspace(db):
    """返回本文件共享 workspace（active，engineering），无则创建。"""
    ws = db.query(WikiWorkspace).filter(WikiWorkspace.key == _SHARED_WS_KEY).first()
    if ws is None:
        ws = WikiWorkspace(
            key=_SHARED_WS_KEY, name="shared-engineering",
            acl_scope=_SHARED_WS_ACL, scope_id=_SHARED_WS_SCOPE_ID, status="active",
        )
        db.add(ws)
        db.flush()
    return ws


def _bind_shared_workspace(db, notebook):
    """把 notebook 显式绑定到共享 workspace（仅 ACL 等价时绑定，不破坏隔离/scope 测试）。"""
    ws = _shared_workspace(db)
    scope = access_control.scope_from_notebook(db, notebook)
    if not access_control.acl_scope_equivalent(access_control.acl_json_for_scope(scope), ws.acl_scope):
        return
    db.add(NotebookWorkspaceBinding(
        notebook_id=notebook.id, workspace_id=ws.id, status="active",
    ))
    db.flush()


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _page(db, page_id, title, content, group_id):
    nb = Notebook(id=f"nb-{page_id}", name="n", group_id=group_id)
    db.add(nb); db.flush()
    _bind_shared_workspace(db, nb)
    db.add(Page(id=page_id, notebook_id=nb.id, title=title, content=content, wiki_dirty=True))
    db.flush()
    return db.get(Page, page_id)


def _mk_llm(ops=None, exc=None, synthesis=None):
    async def _llm(messages, context="", timeout=120.0):
        if exc:
            raise exc
        if context == "wiki-synthesis":
            if synthesis is not None:
                return synthesis
            contents = [op.get("content", "") for op in (ops or []) if op.get("content")]
            return {"summary": "合成摘要", "content": " | ".join(contents) if contents else "聚合正文"}
        return {"worthy": True, "ops": ops or []}
    return _llm


def _run(coro):
    return asyncio.run(coro)


def test_worthy_true_empty_ops_invalid_response():
    assert builder._llm_outcome({"worthy": True, "ops": []}).status == "invalid_response"


def test_all_illegal_ops_invalid_response_no_source_release(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "主题", "content": "正文", "summary": "s"}])))
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    assert "p1" in json.loads(wp.source_page_ids)

    async def _bad(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            return {"summary": "s", "content": "正文"}
        return {"worthy": True, "ops": [{"action": "delete", "title": "主题"}]}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_bad))
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    assert "p1" in json.loads(wp.source_page_ids or "[]")


def test_new_wiki_synthesis_fail_invisible_to_user(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    async def _fail_synthesis(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            raise RuntimeError("down")
        return {"worthy": True, "ops": [{"action": "create", "title": "主题", "category": "x"}]}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_fail_synthesis))
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    assert wp is not None
    assert wp.status == "draft"


def test_new_wiki_synthesis_fail_no_published_empty_revision(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    async def _fail_synthesis(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            raise RuntimeError("down")
        return {"worthy": True, "ops": [{"action": "create", "title": "主题", "category": "x"}]}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_fail_synthesis))
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    revs = db.query(WikiRevision).filter(WikiRevision.wiki_page_id == wp.id, WikiRevision.status == "published").all()
    assert len(revs) == 0
    assert wp.current_revision_id is None


def test_synthesis_fail_page_keeps_dirty(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    async def _fail_synthesis(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            raise RuntimeError("down")
        return {"worthy": True, "ops": [{"action": "create", "title": "主题", "category": "x"}]}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_fail_synthesis))
    db.expire_all()
    p = db.get(Page, "p1")
    assert p.wiki_dirty is True


def test_two_targets_one_fail_returns_partial(db):
    p = _page(db, "p1", "综合", "综合内容足够长", "engineering")
    db.commit()

    calls = {"n": 0}

    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            calls["n"] += 1
            if calls["n"] == 1:
                return {"summary": "s", "content": "主题A内容"}
            raise RuntimeError("fail second")
        return {"worthy": True, "ops": [
            {"action": "create", "title": "主题A", "category": "x"},
            {"action": "create", "title": "主题B", "category": "x"},
        ]}

    out = _run(builder._legacy_process_page_wiki(db, "p1", _llm, commit=True))
    assert out["status"] == "partial"
    db.expire_all()
    p = db.get(Page, "p1")
    assert p.wiki_dirty is True


def test_topic_migration_auto_resynthesizes_old_source(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    p2 = _page(db, "p2", "水箱二", "水箱内容二足够长", "engineering")
    db.commit()

    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            return {"summary": "s", "content": "聚合"}
        return {"worthy": True, "ops": [{"action": "create", "title": "主题A", "category": "x"}]}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_llm))
    _run(builder._legacy_build_wiki_from_pages(db, [p2], llm_json=_llm))

    async def _llm2(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            return {"summary": "s", "content": "聚合"}
        return {"worthy": True, "ops": [{"action": "create", "title": "主题B", "category": "x"}]}
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_llm2))
    db.expire_all()
    # 主题A 已解除 p1，只剩 p2 来源（自动用剩余来源重新合成，dirty 已清）
    a = db.query(WikiPage).filter(WikiPage.title == "主题A").first()
    assert json.loads(a.source_page_ids) == ["p2"]
    # 主题B 新增，含 p1
    b = db.query(WikiPage).filter(WikiPage.title == "主题B").first()
    assert "p1" in json.loads(b.source_page_ids)


def test_data_source_delete_auto_resynthesizes_remaining(db):
    p1 = _page(db, "p1", "来源一", "第一个来源知识A", "engineering")
    p2 = _page(db, "p2", "来源二", "第二个来源知识B", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "主题", "content": "A", "summary": "s"}])))
    _run(builder._legacy_build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "update", "title": "主题", "content": "B", "summary": "s"}])))
    result = builder.remove_source_page_from_wikis(db, "p1")
    assert len(result["dirty_remaining_wiki_ids"]) == 1


def _seed_source_page(db, page_id, state):
    db.add(SourceConnection(id="conn1", connector_key="dingtalk", name="钉钉"))
    db.flush()
    p = db.get(Page, page_id)
    p.source_type = "dingtalk"
    p.source_id = "ext-1"
    p.wiki_dirty = True
    db.add(SourceItem(id=f"si-{page_id}", connection_id="conn1", external_id="ext-1", page_id=page_id, state=state))
    db.commit()


def test_historical_deleted_source_page_not_recovered(db):
    _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _seed_source_page(db, "p1", "deleted")
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    ids = sch._query_dirty_page_ids(db, None)
    assert "p1" not in ids


def test_restored_page_reenters_recovery(db):
    _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _seed_source_page(db, "p1", "active")
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as sch
    ids = sch._query_dirty_page_ids(db, None)
    assert "p1" in ids


def test_three_sources_all_in_synthesis_prompt(db):
    p1 = _page(db, "p1", "来源一", "第一个来源知识", "engineering")
    p2 = _page(db, "p2", "来源二", "第二个来源知识", "engineering")
    p3 = _page(db, "p3", "来源三", "第三个来源知识", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "主题", "content": "A", "summary": "s"}])))
    _run(builder._legacy_build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "update", "title": "主题", "content": "B", "summary": "s"}])))
    captured = {}
    async def _llm3(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            captured["prompt"] = messages[0]["content"]
            return {"summary": "s", "content": "聚合"}
        return {"worthy": True, "ops": [{"action": "update", "title": "主题", "category": "x"}]}
    _run(builder._legacy_build_wiki_from_pages(db, [p3], llm_json=_llm3))
    for src in ("来源一", "来源二", "来源三"):
        assert src in captured["prompt"]


def test_full_build_synthesizes_each_wiki_once(db):
    p1 = _page(db, "p1", "水箱一", "内容一足够长", "engineering")
    p2 = _page(db, "p2", "水箱二", "内容二足够长", "engineering")
    db.commit()
    calls = {"n": 0}
    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-synthesis":
            calls["n"] += 1
            return {"summary": "s", "content": "聚合"}
        return {"worthy": True, "ops": [{"action": "create", "title": "水箱主题", "category": "x"}]}
    _run(builder._legacy_build_wiki_from_pages(db, [p1, p2], llm_json=_llm, dedupe_synthesis=True))
    assert calls["n"] == 1
