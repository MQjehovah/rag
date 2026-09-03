"""V4 Phase C 第二次补漏验收测试。

覆盖第二次补漏第 10 点要求（18 项）中尚未覆盖的场景。
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.knowledge_compiler_v3 import wiki_refresh_scheduler as scheduler
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
_SHARED_WS_KEY = "test-shared-ws-wiki_v4_acceptance2"
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


# 3. 新 Page LLM 不可用时 wiki_dirty=True，恢复后能补建
def test_new_page_llm_unavailable_then_recovers(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm(exc=RuntimeError("down"))))
    db.expire_all()
    p = db.get(Page, "p1")
    assert p.wiki_dirty is True
    assert p.wiki_last_error == "service_unavailable"

    # 恢复后补建
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "摘要"}])))
    db.expire_all()
    p = db.get(Page, "p1")
    assert p.wiki_dirty is False
    assert db.query(WikiPage).filter(WikiPage.title == "水箱").count() == 1


# 5. Page 主题 A → B，A 解除来源，B 添加来源
def test_page_topic_migration_reconciles_sources(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "主题A", "content": "内容A", "summary": "a"}])))
    a = db.query(WikiPage).filter(WikiPage.title == "主题A").first()
    assert "p1" in json.loads(a.source_page_ids)

    # 主题迁移：A → B
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "主题B", "content": "内容B", "summary": "b"}])))
    db.expire_all()
    a = db.query(WikiPage).filter(WikiPage.title == "主题A").first()
    b = db.query(WikiPage).filter(WikiPage.title == "主题B").first()
    assert "p1" not in json.loads(a.source_page_ids or "[]")  # A 解除
    assert "p1" in json.loads(b.source_page_ids or "[]")       # B 添加


# 6. not_worthy 解除旧 Wiki 来源
def test_not_worthy_reconciles_sources(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "主题A", "content": "内容A", "summary": "a"}])))
    a = db.query(WikiPage).filter(WikiPage.title == "主题A").first()
    assert "p1" in json.loads(a.source_page_ids)

    # 显式 not_worthy → 解除来源，Wiki archived（唯一来源）
    async def _not_worthy(messages, context="", timeout=120.0):
        return {"worthy": False, "ops": []}
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_not_worthy))
    db.expire_all()
    a = db.query(WikiPage).filter(WikiPage.title == "主题A").first()
    assert a.status == "archived"
    assert "p1" not in json.loads(a.source_page_ids or "[]")


# 8. 删除多个来源之一 → Wiki dirty，使用剩余来源
def test_delete_one_of_multiple_sources_keeps_dirty(db):
    p1 = _page(db, "p1", "水箱一", "水箱内容一足够长", "engineering")
    p2 = _page(db, "p2", "水箱二", "水箱内容二足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "摘要"}])))
    # p2 也加入同一主题
    _run(builder._legacy_build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "update", "title": "水箱", "content": "正文", "summary": "摘要"}])))
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert set(json.loads(wp.source_page_ids)) == {"p1", "p2"}

    result = builder.remove_source_page_from_wikis(db, "p1")
    assert wp.id in result["dirty_remaining_wiki_ids"]
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.status == "draft"  # 安全：重建前从正式检索/展示中排除
    assert wp.dirty is True       # 待用剩余来源重建后重新 published
    assert json.loads(wp.source_page_ids) == ["p2"]


# 9. notebook_id/权限域变化后旧域不再可见
def test_notebook_scope_change_removes_from_old_scope(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "摘要"}])))
    old_wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert old_wp.acl_scope == '{"groups": ["engineering"]}'

    # 权限域变化：notebook group_id → sales
    nb = db.get(Notebook, f"nb-p1")
    nb.group_id = "sales"
    p.notebook_id = nb.id  # notebook_id 不变但 group_id 变了，scope 变化
    p.wiki_dirty = True
    db.commit()

    # 用新 scope 重新构建（_legacy_process_page_wiki 会检测 scope 变化）
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "摘要"}])))
    db.expire_all()
    # 旧 engineering 域 Wiki 已 archived（无来源）
    old_wp = db.query(WikiPage).filter(WikiPage.title == "水箱", WikiPage.acl_scope == '{"groups": ["engineering"]}').first()
    assert old_wp is None or old_wp.status == "archived"


# 13. RuntimeFeatureFlag=false 时调度器不执行
def test_scheduler_skips_when_disabled(db, monkeypatch):
    monkeypatch.setattr(settings, "wiki_topic_enabled", False)
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    # schedule 只 submit 线程，线程内会检查 feature flag。这里直接验证开关判断逻辑
    assert scheduler._wiki_refresh_enabled(db) is False


# 15. LLM 调用期间 Page 被更新，旧结果被丢弃
def test_stale_llm_result_discarded(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()

    # 构造 LLM 在返回前修改 Page
    async def _slow_llm(messages, context="", timeout=120.0):
        # 模拟 LLM 期间 Page 被更新
        fresh = db.get(Page, "p1")
        fresh.content = "内容被并发修改了"
        fresh.wiki_dirty = True
        db.commit()
        return {"ops": [{"action": "create", "title": "主题", "content": "旧结果", "summary": "s"}]}

    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_slow_llm))
    db.expire_all()
    p = db.get(Page, "p1")
    # 输入变化 → 旧结果丢弃，保持 dirty
    assert p.wiki_dirty is True
    assert p.wiki_last_error == "input_changed_during_llm"
    # 未创建旧结果的 Wiki
    assert db.query(WikiPage).filter(WikiPage.title == "主题").count() == 0


# 17. 无来源 Wiki 不能保持 published
def test_no_source_wiki_cannot_be_published(db):
    p = _page(db, "p1", "水箱", "水箱内容足够长", "engineering")
    db.commit()
    _run(builder._legacy_build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "摘要"}])))
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.status == "published"

    # 删除唯一来源 → archived，不能保持 published
    builder.remove_source_page_from_wikis(db, "p1")
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp.status == "archived"
