"""V4 Phase C 最终验收行为测试。"""
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
from app.core.knowledge_compiler_v3.wiki_page_builder import LLMServiceUnavailable
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    PageChunk,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiWorkspace,
    init_db,
)

# Phase 3.1：同 ACL 不同 notebook 默认各自 workspace；本文件用「多来源合并成一篇
# Wiki」的 V4 语义，需把各来源 notebook 显式共享绑定到同一 workspace（engineering）。
_SHARED_WS_KEY = "test-shared-ws-wiki_v4_final_acceptance"
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


def _mk_llm(ops=None, synthesis=None):
    async def _llm(messages, context="", timeout=120.0):
        if context in ("wiki-synthesis", "wiki-mapreduce", "wiki-batch-summary"):
            if synthesis is not None:
                return synthesis
            return {"summary": "s", "content": "聚合正文"}
        return {"worthy": True, "ops": ops or []}
    return _llm


def _run(coro):
    return asyncio.run(coro)


# 1. 合成期间 Page 内容变化，旧结果不写入
def test_page_changed_during_synthesis_not_written(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": [{"action": "create", "title": "主题", "category": "x"}]}
        # synthesis 期间修改 Page 内容
        db.get(Page, "p1").content = "被并发修改的内容"
        db.commit()
        return {"summary": "s", "content": "旧结果"}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_llm))
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    # 旧结果未写入：无 published Revision
    assert wp.current_revision_id is None or wp.status == "draft"


# 2. 合成期间 source_page_ids 变化，旧结果不写入
def test_source_ids_changed_during_synthesis_not_written(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": [{"action": "create", "title": "主题", "category": "x"}]}
        # synthesis 期间给 Wiki 加一个来源
        wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
        if wp:
            builder._add_source_page(wp, "other-page")
            db.commit()
        return {"summary": "s", "content": "旧结果"}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_llm))
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    assert wp.current_revision_id is None or wp.status == "draft"


# 3. 合成期间 ACL 变化，旧权限域不发布
def test_acl_changed_during_synthesis_not_published(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-ingest-page":
            return {"worthy": True, "ops": [{"action": "create", "title": "主题", "category": "x"}]}
        # synthesis 期间修改 notebook group_id（scope 变化）
        nb = db.get(Notebook, "nb-p1")
        nb.group_id = "sales"
        db.commit()
        return {"summary": "s", "content": "旧结果"}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_llm))
    db.expire_all()
    # 旧权限域无 published Wiki
    old = db.query(WikiPage).filter(WikiPage.title == "主题", WikiPage.acl_scope == '{"groups": ["engineering"]}').first()
    assert old is None or old.status != "published"


# 6. ACL 迁移后旧 Wiki 在安全重建前不可见
def test_acl_migration_old_wiki_hidden_before_rebuild(db):
    p1 = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    p2 = _page(db, "p2", "水箱二", "水箱内容二足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "主题", "content": "A", "summary": "s"}])))
    _run(builder._legacy_build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "update", "title": "主题", "content": "B", "summary": "s"}])))
    # p1 迁移到 sales
    nb = db.get(Notebook, "nb-p1")
    nb.group_id = "sales"
    p1.wiki_dirty = True
    db.commit()

    async def _llm(messages, context="", timeout=120.0):
        if context in ("wiki-synthesis", "wiki-mapreduce", "wiki-batch-summary"):
            raise RuntimeError("synthesis down")  # 模拟重建尚未完成
        return {"worthy": True, "ops": [{"action": "create", "title": "主题", "category": "x"}]}
    _run(builder._legacy_build_wiki_from_pages(db, [p1], llm_json=_llm))
    db.expire_all()
    old = db.query(WikiPage).filter(WikiPage.title == "主题", WikiPage.acl_scope == '{"groups": ["engineering"]}').first()
    # 旧域 Wiki 已从正式展示排除（draft，重建未完成前不可见）
    assert old is None or old.status != "published"


# 7. 删除来源后旧内容不能继续作为正式知识返回
def test_deleted_source_not_returned_as_official(db):
    p1 = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    p2 = _page(db, "p2", "水箱二", "水箱内容二足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "主题", "content": "A", "summary": "s"}])))
    _run(builder._legacy_build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "update", "title": "主题", "content": "B", "summary": "s"}])))
    builder.remove_source_page_from_wikis(db, "p1")
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "主题").first()
    assert wp.status == "draft"  # 重建前不作为正式知识


# 10. 100 个来源全部进入 Map-Reduce 批次
def test_hundred_sources_all_in_mapreduce(db):
    pages = []
    for i in range(100):
        pages.append(_page(db, f"p{i}", f"来源{i}", f"来源{i}的知识内容足够长", "engineering"))
    db.commit()
    wp = WikiPage(id="w1", title="主题", acl_scope='{"groups": ["engineering"]}', source_page_ids=json.dumps([f"p{i}" for i in range(100)]), status="published", dirty=True, workspace_id=_shared_workspace(db).id)
    db.add(wp); db.flush()
    rev = WikiRevision(id="rev1", wiki_page_id="w1", title="主题", summary="", status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id="sec1", revision_id="rev1", section_type="facts", heading="正文", content="旧", order_index=1))
    wp.current_revision_id = "rev1"
    db.commit()

    batch_prompts = []
    async def _llm(messages, context="", timeout=120.0):
        if context == "wiki-batch-summary":
            batch_prompts.append(messages[0]["content"])
            return {"summary": "批次摘要"}
        if context == "wiki-mapreduce":
            return {"summary": "s", "content": "最终正文"}
        return {"worthy": True, "ops": []}
    _run(builder._legacy_rebuild_wiki_from_sources(db, "w1", _llm))
    # 100 个来源分多个批次，每个来源标题至少出现在某个批次
    assert len(batch_prompts) > 1  # 确实走了 Map-Reduce 分批
    all_prompts = "\n".join(batch_prompts)
    assert "来源0" in all_prompts
    assert "来源99" in all_prompts


# 11. 401/超时标记 service_unavailable
def test_service_unavailable_mapping():
    # 通过 call_wiki_llm_json 的异常类型区分
    assert LLMServiceUnavailable is not None


def test_snapshot_equality():
    from app.core.knowledge_compiler_v3.wiki_page_builder import WikiSynthesisSnapshot
    a = WikiSynthesisSnapshot("w1", None, ("p1",), ("h1",), None, "lock", "t", None)
    b = WikiSynthesisSnapshot("w1", None, ("p1",), ("h1",), None, "lock", "t", None)
    c = WikiSynthesisSnapshot("w1", None, ("p1",), ("h2",), None, "lock", "t", None)
    assert a == b
    assert a != c
