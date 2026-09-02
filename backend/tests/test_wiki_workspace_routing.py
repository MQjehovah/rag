"""Phase 3.1：WikiWorkspace 路由/workspace key/ACL 等价 + Topic Router workspace 隔离测试。

覆盖：
- workspace_key_for_notebook / generate_manual_workspace_key / normalize_scope_id /
  acl_scope_equivalent / acl_json_for_scope
- ensure_notebook_workspace（同 ACL 多 notebook 各自默认 workspace；显式绑定共享；
  disabled binding / UNKNOWN fail closed）
- 未绑定 Page → Topic Router fail closed（no_workspace + dirty + error）
- active binding DB 不变量：并发双 session 只有一个成功（IntegrityError）
- archived workspace 禁止绑定 / 不路由
- Topic Router 强制 workspace：_load_scope_wikis 缺 workspace_id → ValueError
- 相同 ACL 不同 workspace 隔离（Topic 查询双条件 workspace_id + acl_scope）
- ACL 不等价绑定拒绝 / 删除绑定不静默移动；发布前 workspace 不一致 → stale_input
全部使用内存 SQLite + Mock LLM，不触碰真实库。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.core.knowledge_compiler_v3 import wiki_page_builder as builder
from app.core.wiki_workspace import routing, service
from app.models.database import (
    Base,
    MANAGED_WORKSPACE_TABLES,
    Notebook,
    NotebookGroup,
    NotebookWorkspaceBinding,
    Page,
    SchemaNotReadyError,
    WikiPage,
    WikiWorkspace,
    check_managed_migrations,
    init_db,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.abspath(os.path.join(BACKEND_ROOT, ".venv", "Scripts", "python.exe"))


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


def _mk_llm(ops=None, synthesis=None, exc=None):
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


def _notebook(db, nb_id, group_id, name="n"):
    nb = Notebook(id=nb_id, name=name, group_id=group_id)
    db.add(nb)
    db.flush()
    return nb


def _page(db, page_id, nb_id, title, content):
    db.add(Page(id=page_id, notebook_id=nb_id, title=title, content=content, wiki_dirty=True))
    db.flush()
    return db.get(Page, page_id)


def _create_ws(db, ws_id, key, acl_scope, scope_id, name="ws"):
    ws = WikiWorkspace(id=ws_id, key=key, name=name, acl_scope=acl_scope, scope_id=scope_id, status="active")
    db.add(ws)
    db.flush()
    return ws


# ---------------------------------------------------------------------------
# 确定性函数单元
# ---------------------------------------------------------------------------

def test_workspace_key_for_notebook():
    k1 = access_control.workspace_key_for_notebook("nb1")
    assert k1.startswith("ws_nb_") and len(k1) == len("ws_nb_") + 24
    assert k1 == access_control.workspace_key_for_notebook("nb1")  # 同 notebook 幂等
    assert k1 != access_control.workspace_key_for_notebook("nb2")  # 不同 notebook 不同 key
    assert not hasattr(access_control, "deterministic_workspace_key")


def test_generate_manual_workspace_key_unique():
    k1 = access_control.generate_manual_workspace_key()
    k2 = access_control.generate_manual_workspace_key()
    assert k1.startswith("ws_") and k2.startswith("ws_")
    assert len(k1) == 3 + 12 and len(k2) == 3 + 12
    assert k1 != k2  # 系统生成唯一 key


def test_normalize_scope_id():
    assert access_control.normalize_scope_id(access_control.scope_from_acl('{"groups": ["__public__"]}')) == "company"
    assert access_control.normalize_scope_id(access_control.scope_from_acl('{"groups": ["__local_admin__"]}')) == "admin"
    # 多组排序稳定
    s = access_control.scope_from_acl('{"groups": ["b", "a"]}')
    assert access_control.normalize_scope_id(s) == "group:a,b"


def test_acl_json_for_scope_matches_builder_encoding():
    for acl in ('{"groups": ["__public__"]}', '{"groups": ["__local_admin__"]}', '{"groups": ["engineering"]}'):
        scope = access_control.scope_from_acl(acl)
        assert access_control.acl_json_for_scope(scope) == builder._scope_to_acl_json(scope)


def test_acl_scope_equivalent():
    assert access_control.acl_scope_equivalent('{"groups": ["engineering"]}', '{"groups": ["engineering"]}') is True
    # 组序不同但集合相同 → 等价（历史组序可能不同）
    assert access_control.acl_scope_equivalent('{"groups": ["b", "a"]}', '{"groups": ["a", "b"]}') is True
    assert access_control.acl_scope_equivalent('{"groups": ["__public__"]}', '{"groups": ["__public__"]}') is True
    assert access_control.acl_scope_equivalent('{"groups": ["engineering"]}', '{"groups": ["sales"]}') is False
    assert access_control.acl_scope_equivalent(None, '{"groups": ["engineering"]}') is False
    assert access_control.acl_scope_equivalent("not-json", '{"groups": ["engineering"]}') is False
    assert access_control.acl_scope_equivalent('{"groups": []}', '{"groups": ["engineering"]}') is False


# ---------------------------------------------------------------------------
# 路由：ensure / resolve
# ---------------------------------------------------------------------------

def test_same_acl_notebooks_default_to_different_workspaces(db):
    """同 ACL 两个 notebook 无绑定 → 各自默认 workspace（key/id 均不同，各 1 active binding）。"""
    nb1 = _notebook(db, "nb1", "engineering")
    nb2 = _notebook(db, "nb2", "engineering")
    db.commit()
    ws1 = routing.ensure_notebook_workspace(db, nb1)
    assert ws1 is not None
    db.commit()
    ws2 = routing.ensure_notebook_workspace(db, nb2)
    db.commit()

    assert ws1 is not None and ws2 is not None
    assert ws1.key != ws2.key                      # 各自私用空间 key
    assert ws1.id != ws2.id
    assert ws1.acl_scope == ws2.acl_scope == '{"groups": ["engineering"]}'
    # 各 1 active binding
    assert db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1", NotebookWorkspaceBinding.status == "active"
    ).count() == 1
    assert db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb2", NotebookWorkspaceBinding.status == "active"
    ).count() == 1


def test_ensure_notebook_workspace_same_notebook_idempotent(db):
    """同一 notebook 重复 ensure → 复用同一 workspace + 同一 binding。"""
    nb = _notebook(db, "nb1", "engineering")
    db.commit()
    ws1 = routing.ensure_notebook_workspace(db, nb)
    db.commit()
    ws2 = routing.ensure_notebook_workspace(db, nb)
    db.commit()
    assert ws1.id == ws2.id
    assert db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1", NotebookWorkspaceBinding.status == "active"
    ).count() == 1


def test_ensure_notebook_workspace_unknown_scope_fail_closed(db):
    nb = _notebook(db, "nb1", "__public__")
    db.add(NotebookGroup(notebook_id="nb1", group_name="engineering"))
    db.commit()
    ws = routing.ensure_notebook_workspace(db, nb)
    assert ws is None  # UNKNOWN → fail closed


def test_ensure_notebook_workspace_disabled_binding_fail_closed(db):
    nb = _notebook(db, "nb1", "engineering")
    ws = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    db.add(NotebookWorkspaceBinding(id="b1", notebook_id="nb1", workspace_id="ws1", status="disabled"))
    db.commit()
    got = routing.ensure_notebook_workspace(db, nb)
    assert got is None  # disabled binding 不覆盖管理员意图


def test_ensure_notebook_workspace_reuses_private_workspace_by_key(db):
    """同 notebook key 的 workspace 已存在且 active → 复用（该 notebook 默认私用空间）。"""
    nb = _notebook(db, "nb1", "engineering")
    key = access_control.workspace_key_for_notebook("nb1")
    ws = _create_ws(db, "ws1", key, '{"groups": ["engineering"]}', "group:engineering")
    db.commit()
    got = routing.ensure_notebook_workspace(db, nb)
    assert got is not None and got.id == ws.id
    db.commit()
    assert db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1", NotebookWorkspaceBinding.status == "active"
    ).count() == 1


def test_ensure_notebook_workspace_other_notebook_same_acl_not_reused(db):
    """同 ACL 另一 notebook 的已存在 workspace（scope 相同但 notebook key 不同）不复用。"""
    nb1 = _notebook(db, "nb1", "engineering")
    nb2 = _notebook(db, "nb2", "engineering")
    db.commit()
    ws1 = routing.ensure_notebook_workspace(db, nb1)
    db.commit()
    # nb2 无 binding，不得复用 nb1 的 workspace（scope 全局查找已废弃）
    ws2 = routing.ensure_notebook_workspace(db, nb2)
    db.commit()
    assert ws1.id != ws2.id


def test_resolve_workspace_for_page_unbound_none(db):
    nb = _notebook(db, "nb1", "engineering")
    p = _page(db, "p1", "nb1", "主题", "内容足够长")
    db.commit()
    assert routing.resolve_workspace_for_page(db, p) is None  # 无 binding（只读不自动建）
    assert routing.page_workspace_id(db, p) is None


# ---------------------------------------------------------------------------
# Topic Router：未绑定 fail closed
# ---------------------------------------------------------------------------

def test_unbound_page_fail_closed_disabled_binding(db):
    nb = _notebook(db, "nb1", "engineering")
    ws = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    db.add(NotebookWorkspaceBinding(id="b1", notebook_id="nb1", workspace_id="ws1", status="disabled"))
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()
    out = _run(builder.process_page_wiki(db, "p1", _mk_llm([{"action": "create", "title": "水箱", "content": "x", "summary": "s"}])))
    assert out["status"] == "no_workspace"
    db.expire_all()
    fresh = db.get(Page, "p1")
    assert fresh.wiki_dirty is True
    assert fresh.wiki_last_error == "no_workspace_binding"
    assert db.query(WikiPage).count() == 0  # 不回退建 Wiki


def test_unbound_page_fail_closed_unknown_scope(db):
    nb = _notebook(db, "nb1", "__public__")
    db.add(NotebookGroup(notebook_id="nb1", group_name="engineering"))
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()
    out = _run(builder.process_page_wiki(db, "p1", _mk_llm([{"action": "create", "title": "水箱", "content": "x", "summary": "s"}])))
    assert out["status"] == "no_workspace"
    db.expire_all()
    fresh = db.get(Page, "p1")
    assert fresh.wiki_dirty is True
    assert fresh.wiki_last_error == "no_workspace_binding"
    assert db.query(WikiPage).count() == 0


# ---------------------------------------------------------------------------
# 显式绑定共享：workspace 合并
# ---------------------------------------------------------------------------

def test_explicit_binding_required_for_workspace_merge(db):
    """同 ACL notebook 不绑定不合并；admin 显式绑定两 notebook 到同一 workspace → 合并。

    注意：notebook 已由自动路由产生默认 active binding 时，绑定共享 workspace 需先解绑
    默认绑定（unbind 软禁用 → bind 复用/重建）。
    """
    nb1 = _notebook(db, "nb1", "engineering")
    nb2 = _notebook(db, "nb2", "engineering")
    db.commit()
    p1 = _page(db, "p1", "nb1", "来源一", "第一个来源知识A足够长")
    p2 = _page(db, "p2", "nb2", "来源二", "第二个来源知识B足够长")
    db.commit()

    # 不绑定：各自默认 workspace，主题不合并（同标题各建各的）
    _run(builder.build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "A", "summary": "s"}])))
    _run(builder.build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "B", "summary": "s"}])))
    db.expire_all()
    assert db.query(WikiWorkspace).count() == 2  # 两个默认 workspace
    assert db.query(WikiPage).filter(
        WikiPage.acl_scope == '{"groups": ["engineering"]}', WikiPage.status == "published"
    ).count() == 2  # 不合并：各自独立主题

    # admin 显式合并：建共享 workspace，先解绑默认私用绑定，再显式绑定两个 notebook
    eng_ws = _create_ws(db, "ws-shared", "ws_shared_key", '{"groups": ["engineering"]}', "group:engineering")
    d1 = routing.resolve_workspace_for_notebook(db, db.get(Notebook, "nb1"))
    d2 = routing.resolve_workspace_for_notebook(db, db.get(Notebook, "nb2"))
    assert d1 is not None and d2 is not None and d1.id != d2.id
    service.unbind_notebook(db, d1, "nb1")
    service.unbind_notebook(db, d2, "nb2")
    service.bind_notebook(db, eng_ws, db.get(Notebook, "nb1"), created_by=None)
    service.bind_notebook(db, eng_ws, db.get(Notebook, "nb2"), created_by=None)
    db.commit()
    db.expire_all()
    # 显式绑定后：两个 notebook 路由到同一共享 workspace
    assert routing.resolve_workspace_for_notebook(db, db.get(Notebook, "nb1")).id == eng_ws.id
    assert routing.resolve_workspace_for_notebook(db, db.get(Notebook, "nb2")).id == eng_ws.id
    assert db.query(WikiWorkspace).filter(WikiWorkspace.status == "active").count() == 3
    # 同一共享 workspace 下，两个 notebook 的新 Page 触发同标题 → 合并为同一主题
    p3 = _page(db, "p3", "nb1", "来源三", "第三个来源知识C足够长")
    p4 = _page(db, "p4", "nb2", "来源四", "第四个来源知识D足够长")
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p3], llm_json=_mk_llm([{"action": "create", "title": "共享水箱", "content": "C", "summary": "s"}])))
    _run(builder.build_wiki_from_pages(db, [p4], llm_json=_mk_llm([{"action": "update", "title": "共享水箱", "content": "D", "summary": "s"}])))
    db.expire_all()
    merged = db.query(WikiPage).filter(WikiPage.workspace_id == eng_ws.id, WikiPage.title == "共享水箱").all()
    assert len(merged) == 1
    assert set(json.loads(merged[0].source_page_ids)) == {"p3", "p4"}


def test_multi_notebook_explicit_binding_same_workspace_merges(db):
    """改写：同 ACL 两个 notebook 显式绑定同一 workspace 后，Page 主题自动合并。"""
    nb1 = _notebook(db, "nb1", "engineering")
    nb2 = _notebook(db, "nb2", "engineering")
    ws = _create_ws(db, "ws1", "ws_shared_1", '{"groups": ["engineering"]}', "group:engineering")
    service.bind_notebook(db, ws, nb1, created_by=None)
    service.bind_notebook(db, ws, nb2, created_by=None)
    db.commit()
    p1 = _page(db, "p1", "nb1", "来源一", "第一个来源知识A")
    p2 = _page(db, "p2", "nb2", "来源二", "第二个来源知识B")
    db.commit()

    _run(builder.build_wiki_from_pages(db, [p1], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "A", "summary": "s"}])))
    _run(builder.build_wiki_from_pages(db, [p2], llm_json=_mk_llm([{"action": "update", "title": "水箱", "content": "B", "summary": "s"}])))
    db.expire_all()

    wikis = db.query(WikiPage).filter(WikiPage.acl_scope == '{"groups": ["engineering"]}').all()
    assert len(wikis) == 1  # 显式绑定 → 两个 notebook 的 Page 合并到同一主题
    assert wikis[0].workspace_id == ws.id
    assert set(json.loads(wikis[0].source_page_ids)) == {"p1", "p2"}


def test_auto_created_wiki_has_workspace_id(db):
    nb = _notebook(db, "nb1", "engineering")
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()
    stats = _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "摘要"}])))
    assert stats["created"] == 1
    db.expire_all()
    wp = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wp is not None
    assert wp.workspace_id is not None  # 写入端强制非空


# ---------------------------------------------------------------------------
# 相同 ACL 不同 workspace 隔离
# ---------------------------------------------------------------------------

def test_same_acl_different_workspace_isolation(db):
    """两个 workspace 相同 ACL 不同 key（显式构造）：Topic 查询级隔离 + 编译不触碰。"""
    ws_a = _create_ws(db, "ws-a", "ws_key_a", '{"groups": ["engineering"]}', "group:engineering", name="A")
    ws_b = _create_ws(db, "ws-b", "ws_key_b", '{"groups": ["engineering"]}', "group:engineering", name="B")
    nb_a = _notebook(db, "nb-a", "engineering")
    nb_b = _notebook(db, "nb-b", "engineering")
    service.bind_notebook(db, ws_a, nb_a, created_by=None)
    service.bind_notebook(db, ws_b, nb_b, created_by=None)
    db.commit()
    p_a = _page(db, "pa", "nb-a", "工程主题A", "工程主题A的内容足够长")
    p_b = _page(db, "pb", "nb-b", "工程主题B", "工程主题B的内容足够长")
    db.commit()

    _run(builder.build_wiki_from_pages(db, [p_a], llm_json=_mk_llm([{"action": "create", "title": "主题A", "content": "a", "summary": "s"}])))
    _run(builder.build_wiki_from_pages(db, [p_b], llm_json=_mk_llm([{"action": "create", "title": "主题B", "content": "b", "summary": "s"}])))
    db.expire_all()

    wa = db.query(WikiPage).filter(WikiPage.title == "主题A").first()
    wb = db.query(WikiPage).filter(WikiPage.title == "主题B").first()
    assert wa.workspace_id == "ws-a"
    assert wb.workspace_id == "ws-b"

    # Topic 查询双条件：workspace_id + acl_scope，不回退同 ACL 全局 Wiki
    scope = access_control.scope_from_acl('{"groups": ["engineering"]}')
    idx_a = builder._load_scope_wikis(db, scope, "ws-a")
    idx_b = builder._load_scope_wikis(db, scope, "ws-b")
    assert "主题a" in idx_a and "主题b" not in idx_a
    assert "主题b" in idx_b and "主题a" not in idx_b


# ---------------------------------------------------------------------------
# Topic Router：workspace_id 必填（堵 None 全量回退）
# ---------------------------------------------------------------------------

def test_topic_router_requires_workspace_id(db):
    """_load_scope_wikis 不传 workspace_id → TypeError（必填 str）；传 None → ValueError。"""
    scope = access_control.scope_from_acl('{"groups": ["engineering"]}')
    with pytest.raises(TypeError):
        builder._load_scope_wikis(db, scope)
    with pytest.raises(ValueError):
        builder._load_scope_wikis(db, scope, None)


def test_topic_router_snapshot_requires_workspace_id(db):
    """_load_scope_wikis_from_snapshot 不传 workspace_id → TypeError；传 None → ValueError。"""
    with pytest.raises(TypeError):
        builder._load_scope_wikis_from_snapshot(db, '{"groups": ["engineering"]}')
    with pytest.raises(ValueError):
        builder._load_scope_wikis_from_snapshot(db, '{"groups": ["engineering"]}', None)


# ---------------------------------------------------------------------------
# ACL 不等价绑定拒绝 / 删除绑定不静默移动
# ---------------------------------------------------------------------------

def test_bind_notebook_acl_not_equivalent_rejected(db):
    ws = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    nb = _notebook(db, "nb1", "sales")
    db.commit()
    with pytest.raises(service.WorkspaceError):
        service.bind_notebook(db, ws, nb, created_by=None)


def test_bind_notebook_conflict_other_workspace(db):
    ws1 = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    ws2 = _create_ws(db, "ws2", "ws_key_2", '{"groups": ["sales"]}', "group:sales")
    nb = _notebook(db, "nb1", "engineering")
    service.bind_notebook(db, ws1, nb, created_by=None)
    db.commit()
    with pytest.raises(service.WorkspaceConflict):
        service.bind_notebook(db, ws2, nb, created_by=None)  # 已绑定其他 workspace


def test_unbind_does_not_silently_move_wiki(db):
    ws1 = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    ws2 = _create_ws(db, "ws2", "ws_key_2", '{"groups": ["engineering"]}', "group:engineering", name="B")
    nb = _notebook(db, "nb1", "engineering")
    service.bind_notebook(db, ws1, nb, created_by=None)
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()

    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "旧正文", "summary": "s"}])))
    db.expire_all()
    old_wiki = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert old_wiki.workspace_id == "ws1"

    # 解绑 ws1，改绑 ws2 → 重新构建
    service.unbind_notebook(db, ws1, "nb1")
    service.bind_notebook(db, ws2, nb, created_by=None)
    db.commit()

    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "新正文", "summary": "s"}])))
    db.expire_all()
    old_wiki = db.query(WikiPage).filter(WikiPage.title == "水箱", WikiPage.workspace_id == "ws1").first()
    new_wiki = db.query(WikiPage).filter(WikiPage.title == "水箱", WikiPage.workspace_id == "ws2").first()
    # 旧 Wiki 不静默移动，仍留在 ws1；新 Wiki 建在 ws2
    assert old_wiki is not None and old_wiki.workspace_id == "ws1"
    assert new_wiki is not None and new_wiki.workspace_id == "ws2"


# ---------------------------------------------------------------------------
# archived workspace：禁止绑定 / 不路由
# ---------------------------------------------------------------------------

def test_archived_workspace_cannot_bind(db):
    """绑定 archived workspace → 拒绝（WorkspaceArchivedError）。"""
    ws1 = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    ws1.status = "archived"
    nb = _notebook(db, "nb1", "engineering")
    db.commit()
    with pytest.raises(service.WorkspaceArchivedError):
        service.bind_notebook(db, db.get(WikiWorkspace, "ws1"), nb, created_by=None)


def test_archived_workspace_not_routable(db):
    """binding 命中的 workspace 为 archived → resolve/ensure fail closed，Page 不路由。"""
    ws1 = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    nb = _notebook(db, "nb1", "engineering")
    service.bind_notebook(db, ws1, nb, created_by=None)
    db.commit()
    ws1 = db.get(WikiWorkspace, ws1.id)
    ws1.status = "archived"
    db.commit()

    db.expire_all()
    assert routing.resolve_workspace_for_notebook(db, nb) is None

    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()
    assert routing.resolve_workspace_for_page(db, p) is None
    assert routing.ensure_notebook_workspace(db, nb) is None
    # Topic Router 级 fail closed：保持 dirty + no_workspace_binding
    out = _run(builder.process_page_wiki(db, "p1", _mk_llm([{"action": "create", "title": "水箱", "content": "x", "summary": "s"}])))
    assert out["status"] == "no_workspace"
    db.expire_all()
    fresh = db.get(Page, "p1")
    assert fresh.wiki_dirty is True
    assert fresh.wiki_last_error == "no_workspace_binding"


# ---------------------------------------------------------------------------
# 发布前 workspace 校验
# ---------------------------------------------------------------------------

def test_rebuild_workspace_mismatch_fails_closed(db):
    ws1 = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    ws2 = _create_ws(db, "ws2", "ws_key_2", '{"groups": ["engineering"]}', "group:engineering", name="B")
    nb = _notebook(db, "nb1", "engineering")
    service.bind_notebook(db, ws1, nb, created_by=None)
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()

    _run(builder.build_wiki_from_pages(db, [p], llm_json=_mk_llm([{"action": "create", "title": "水箱", "content": "正文", "summary": "s"}])))
    db.expire_all()
    wiki = db.query(WikiPage).filter(WikiPage.title == "水箱").first()
    assert wiki.workspace_id == "ws1"

    # 把 notebook 改绑到 ws2：来源 Page workspace 与 wiki.workspace_id 不一致
    service.unbind_notebook(db, ws1, "nb1")
    service.bind_notebook(db, ws2, nb, created_by=None)
    wiki.dirty = True
    db.commit()

    out = _run(builder.rebuild_wiki_from_sources(db, wiki.id, _mk_llm(synthesis={"summary": "s", "content": "新"})))
    assert out["status"] == "stale_input"
    db.expire_all()
    wiki = db.get(WikiPage, wiki.id)
    assert wiki.dirty is True  # 保持 dirty，不发布


def test_rebuild_wiki_missing_workspace_id_archived(db):
    """dirty wiki 无 workspace_id → rebuild 直接 archived（fail closed，不再 stale 循环）。"""
    ws1 = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    nb = _notebook(db, "nb1", "engineering")
    service.bind_notebook(db, ws1, nb, created_by=None)
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()
    # 构造 legacy 无 workspace_id 的 dirty wiki（迁移期 NULL 残留）
    wp = WikiPage(id="legacy-ws", title="旧水箱", acl_scope='{"groups": ["engineering"]}',
                  status="draft", dirty=True, source_page_ids='["p1"]', workspace_id=None)
    db.add(wp)
    db.commit()
    out = _run(builder.rebuild_wiki_from_sources(db, "legacy-ws", _mk_llm(synthesis={"summary": "s", "content": "x"})))
    assert out["status"] == "archived"
    db.expire_all()
    fresh = db.get(WikiPage, "legacy-ws")
    assert fresh.status == "archived"
    assert fresh.dirty is False


def test_refresh_dirty_wikis_archives_missing_workspace_id(db):
    """refresh_dirty_wikis 对 workspace_id 为 None 的 dirty wiki → archived。"""
    ws1 = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    nb = _notebook(db, "nb1", "engineering")
    service.bind_notebook(db, ws1, nb, created_by=None)
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()
    db.add(WikiPage(id="legacy-ws2", title="旧", acl_scope='{"groups": ["engineering"]}',
                    status="draft", dirty=True, source_page_ids='["p1"]', workspace_id=None))
    db.commit()
    out = _run(builder.refresh_dirty_wikis(db, llm_json=_mk_llm()))
    assert out["archived"] == 1
    db.expire_all()
    fresh = db.get(WikiPage, "legacy-ws2")
    assert fresh.status == "archived"
    assert fresh.dirty is False


def test_refresh_dirty_wikis_archives_source_workspace_unresolvable(db):
    """来源 Page workspace 无法解析（disabled binding）→ refresh 归档该 wiki。"""
    ws1 = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    nb = _notebook(db, "nb1", "engineering")
    service.bind_notebook(db, ws1, nb, created_by=None)
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()
    wp = WikiPage(id="w1", title="水箱", acl_scope='{"groups": ["engineering"]}',
                  status="published", dirty=True, source_page_ids='["p1"]', workspace_id=ws1.id)
    db.add(wp)
    db.commit()
    # 解绑（disabled）：来源 Page 不再解析到 ws1
    service.unbind_notebook(db, ws1, "nb1")
    db.commit()
    out = _run(builder.refresh_dirty_wikis(db, llm_json=_mk_llm()))
    assert out["archived"] == 1
    db.expire_all()
    fresh = db.get(WikiPage, "w1")
    assert fresh.status == "archived"
    assert fresh.dirty is False


# ---------------------------------------------------------------------------
# 同标题跨 workspace 隔离（Topic Router 强制 workspace 编译隔离）
# ---------------------------------------------------------------------------

def test_same_acl_cross_workspace_topic_isolation(db):
    """两个同 ACL workspace 各放同标题 wiki，A 编译不触碰 B（不误 merge）。"""
    ws_a = _create_ws(db, "ws-a", "ws_key_a", '{"groups": ["engineering"]}', "group:engineering", name="A")
    ws_b = _create_ws(db, "ws-b", "ws_key_b", '{"groups": ["engineering"]}', "group:engineering", name="B")
    nb_a = _notebook(db, "nb-a", "engineering")
    nb_b = _notebook(db, "nb-b", "engineering")
    service.bind_notebook(db, ws_a, nb_a, created_by=None)
    service.bind_notebook(db, ws_b, nb_b, created_by=None)
    db.commit()
    p_a = _page(db, "pa", "nb-a", "工程来源A", "同标题知识A内容足够长")
    p_b = _page(db, "pb", "nb-b", "工程来源B", "同标题知识B内容足够长")
    db.commit()

    _run(builder.build_wiki_from_pages(db, [p_a], llm_json=_mk_llm([{"action": "create", "title": "同标题", "content": "A", "summary": "s"}])))
    _run(builder.build_wiki_from_pages(db, [p_b], llm_json=_mk_llm([{"action": "create", "title": "同标题", "content": "B", "summary": "s"}])))
    db.expire_all()
    wa = db.query(WikiPage).filter(WikiPage.title == "同标题", WikiPage.workspace_id == "ws-a").first()
    wb = db.query(WikiPage).filter(WikiPage.title == "同标题", WikiPage.workspace_id == "ws-b").first()
    assert wa is not None and wb is not None
    assert wa.id != wb.id
    assert set(json.loads(wa.source_page_ids)) == {"pa"}
    assert set(json.loads(wb.source_page_ids)) == {"pb"}

    # 触发 ws-b 编译更新 → A 不受影响
    db.expire_all()
    p_b = db.get(Page, "pb")
    p_b.content = "同标题知识B更新后内容足够长"
    db.commit()
    _run(builder.build_wiki_from_pages(db, [p_b], llm_json=_mk_llm([{"action": "update", "title": "同标题", "content": "B-new", "summary": "s"}])))
    db.expire_all()
    wa = db.query(WikiPage).filter(WikiPage.id == wa.id).first()
    wb = db.query(WikiPage).filter(WikiPage.id == wb.id).first()
    assert set(json.loads(wa.source_page_ids)) == {"pa"}
    assert set(json.loads(wb.source_page_ids)) == {"pb"}
    assert wa.dirty is False  # A 未被触碰
    assert wb.dirty is False


# ---------------------------------------------------------------------------
# unbind 软禁用（保留）
# ---------------------------------------------------------------------------

def test_unbind_soft_disable_prevents_auto_reroute(db):
    """unbind 软禁用：disabled binding 保留，后续 dirty Page 不再被自动重建绑定。"""
    nb1 = _notebook(db, "nb1", "engineering")
    db.commit()
    ws1 = routing.ensure_notebook_workspace(db, nb1)
    assert ws1 is not None
    db.commit()

    service.unbind_notebook(db, ws1, "nb1")
    db.commit()
    db.expire_all()
    bindings = db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1"
    ).all()
    assert len(bindings) == 1 and bindings[0].status == "disabled"

    # 下次 dirty Page 进入 Topic Router：不再自动重建（fail closed）
    p = _page(db, "p1", "nb1", "水箱", "水箱内容足够长")
    db.commit()
    out = _run(builder.process_page_wiki(db, "p1", _mk_llm([{"action": "create", "title": "水箱", "content": "x", "summary": "s"}])))
    assert out["status"] == "no_workspace"
    db.expire_all()
    assert db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1",
        NotebookWorkspaceBinding.status == "active",
    ).count() == 0  # 未自动重建 active binding
    # 重新绑定：disabled 复用为 active
    rebind = service.bind_notebook(db, ws1, db.get(Notebook, "nb1"), created_by=None)
    assert rebind.status == "active"
    db.commit()
    assert db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1",
        NotebookWorkspaceBinding.status == "active",
    ).count() == 1


# ---------------------------------------------------------------------------
# active binding DB 不变量：并发双 session（部分唯一索引兜底）
# ---------------------------------------------------------------------------

def test_active_binding_unique_under_two_sessions(tmp_path):
    """两个独立 Session/连接并发为同一 notebook 建两个 active binding → 只有一个成功。

    第二个 flush 命中 DB 部分唯一索引 ux_nb_ws_binding_active → IntegrityError。
    """
    url = f"sqlite:///{(tmp_path / 'binding_uniq.db').as_posix()}"
    db_file = str(tmp_path / "binding_uniq.db")
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    s1 = sessionmaker(bind=engine)()
    s1.add(Notebook(id="nb1", name="n1", group_id="engineering"))
    s1.add(WikiWorkspace(id="w1", key="k1", name="w1", acl_scope='{"groups": ["engineering"]}', scope_id="group:engineering"))
    s1.add(WikiWorkspace(id="w2", key="k2", name="w2", acl_scope='{"groups": ["engineering"]}', scope_id="group:engineering"))
    s1.commit()

    s2 = sessionmaker(bind=engine)()
    s1.add(NotebookWorkspaceBinding(id="b1", notebook_id="nb1", workspace_id="w1", status="active"))
    s1.commit()
    s2.add(NotebookWorkspaceBinding(id="b2", notebook_id="nb1", workspace_id="w2", status="active"))
    with pytest.raises(IntegrityError):
        s2.commit()

    s1.close()
    s2.close()
    engine.dispose()
    # 最终只有一个 active binding
    con = sqlite3.connect(db_file)
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM notebook_workspace_bindings WHERE notebook_id='nb1' AND status='active'"
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1


# ---------------------------------------------------------------------------
# 修复轮：workspace schema guard fail closed（含部分唯一索引）
# ---------------------------------------------------------------------------

def _schema_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def test_workspace_tables_missing_fail_closed():
    """已有 core 表但缺 wiki_workspaces / notebook_workspace_bindings → 报缺表 + init_db 抛错。"""
    eng = _schema_engine()
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE notebook_workspace_bindings")
        conn.exec_driver_sql("DROP TABLE wiki_workspaces")
    missing = check_managed_migrations(eng)
    assert "wiki_workspaces" in missing
    assert "notebook_workspace_bindings" in missing
    with pytest.raises(SchemaNotReadyError) as exc_info:
        init_db(eng)
    assert "schema_not_ready" in str(exc_info.value)
    eng.dispose()


def test_wiki_pages_workspace_id_column_missing_fail_closed():
    """已有 wiki_pages 但缺 workspace_id 列（未执行 P41）→ check_managed_migrations 报缺列。"""
    eng = _schema_engine()
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        # SQLite 不允许 DROP COLUMN 被 FK 引用的列；从 sqlite_master.sql 重建无该列的表
        ddl = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='wiki_pages'"
        ).scalar()
        lines = [ln.rstrip() for ln in ddl.splitlines() if "workspace_id" not in ln]
        for i in range(len(lines) - 2, 0, -1):
            if lines[i].endswith(","):
                lines[i] = lines[i][:-1]
                break
        conn.exec_driver_sql("DROP TABLE wiki_pages")
        conn.exec_driver_sql("\n".join(lines))
    missing = check_managed_migrations(eng)
    assert "wiki_pages.workspace_id" in missing
    with pytest.raises(SchemaNotReadyError):
        init_db(eng)
    eng.dispose()


def test_workspace_binding_fk_ondelete_wrong_fail_closed():
    """notebook_workspace_bindings 的 FK ondelete 缺失/错误 → 精确校验 fail closed。"""
    eng = _schema_engine()
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE notebook_workspace_bindings")
        conn.exec_driver_sql(
            "CREATE TABLE notebook_workspace_bindings ("
            " id VARCHAR(36) NOT NULL PRIMARY KEY,"
            " notebook_id VARCHAR(36) NOT NULL REFERENCES notebooks(id),"
            " workspace_id VARCHAR(36) NOT NULL REFERENCES wiki_workspaces(id),"
            " status VARCHAR(32) NOT NULL, created_by VARCHAR(36),"
            " created_at DATETIME, updated_at DATETIME)"
        )
        conn.exec_driver_sql("CREATE INDEX ix_notebook_workspace_bindings_notebook_id ON notebook_workspace_bindings (notebook_id)")
        conn.exec_driver_sql("CREATE INDEX ix_notebook_workspace_bindings_workspace_id ON notebook_workspace_bindings (workspace_id)")
        conn.exec_driver_sql("CREATE UNIQUE INDEX ux_nb_ws_binding ON notebook_workspace_bindings (notebook_id, workspace_id)")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX ux_nb_ws_binding_active ON notebook_workspace_bindings (notebook_id) WHERE status='active'"
        )
    missing = check_managed_migrations(eng)
    assert missing  # ondelete 缺失必须被检出
    assert any("ondelete" in m for m in missing)
    with pytest.raises(SchemaNotReadyError):
        init_db(eng)
    eng.dispose()


def test_workspace_partial_unique_index_missing_fail_closed():
    """手工建表缺 ux_nb_ws_binding_active 部分唯一索引 → guard 检出（fail closed）。"""
    eng = _schema_engine()
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP TABLE notebook_workspace_bindings")
        conn.exec_driver_sql(
            "CREATE TABLE notebook_workspace_bindings ("
            " id VARCHAR(36) NOT NULL PRIMARY KEY,"
            " notebook_id VARCHAR(36) NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,"
            " workspace_id VARCHAR(36) NOT NULL REFERENCES wiki_workspaces(id) ON DELETE CASCADE,"
            " status VARCHAR(32) NOT NULL, created_by VARCHAR(36),"
            " created_at DATETIME, updated_at DATETIME)"
        )
        conn.exec_driver_sql("CREATE INDEX ix_notebook_workspace_bindings_notebook_id ON notebook_workspace_bindings (notebook_id)")
        conn.exec_driver_sql("CREATE INDEX ix_notebook_workspace_bindings_workspace_id ON notebook_workspace_bindings (workspace_id)")
        conn.exec_driver_sql("CREATE UNIQUE INDEX ux_nb_ws_binding ON notebook_workspace_bindings (notebook_id, workspace_id)")
        # 故意缺 ux_nb_ws_binding_active
    missing = check_managed_migrations(eng)
    assert any("partial_unique_name=ux_nb_ws_binding_active" in m for m in missing)
    with pytest.raises(SchemaNotReadyError):
        init_db(eng)
    eng.dispose()


# ---------------------------------------------------------------------------
# 修复轮：P41 表级迁移往返（upgrade head → downgrade P38 → upgrade head）
# ---------------------------------------------------------------------------

def _indexes(db_file, table):
    con = sqlite3.connect(db_file)
    try:
        return {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
            )
        }
    finally:
        con.close()


def test_alembic_p41_workspace_migration_roundtrip(tmp_path):
    """空库 upgrade head 建出 workspace 结构；downgrade P38 移除；upgrade head 恢复。

    额外验证 ux_nb_ws_binding_active 部分唯一索引随 upgrade 存在、随 downgrade 消失。
    """
    db_file = (tmp_path / "p41_mig.db").as_posix()
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_file}"

    def run(args):
        r = subprocess.run([PY, "-m", "alembic"] + args, cwd=BACKEND_ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env)
        return r.returncode == 0, r.stdout or "", r.stderr or ""

    def tables():
        con = sqlite3.connect(db_file)
        try:
            return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()

    def page_cols():
        con = sqlite3.connect(db_file)
        try:
            return {c[1] for c in con.execute("PRAGMA table_info(wiki_pages)")}
        finally:
            con.close()

    ok, _, err = run(["upgrade", "head"])
    assert ok, f"upgrade head failed: {err}"
    assert "wiki_workspaces" in tables()
    assert "notebook_workspace_bindings" in tables()
    assert "workspace_id" in page_cols()
    assert "ux_nb_ws_binding_active" in _indexes(db_file, "notebook_workspace_bindings")

    ok, _, err = run(["downgrade", "e6f7a8b9c0d1"])  # P38：移除 P41 新增结构
    assert ok, f"downgrade P38 failed: {err}"
    assert "wiki_workspaces" not in tables()
    assert "notebook_workspace_bindings" not in tables()
    assert "workspace_id" not in page_cols()
    assert "ux_nb_ws_binding_active" not in _indexes(db_file, "notebook_workspace_bindings")

    ok, _, err = run(["upgrade", "head"])
    assert ok, f"upgrade head (roundtrip) failed: {err}"
    assert "wiki_workspaces" in tables()
    assert "notebook_workspace_bindings" in tables()
    assert "workspace_id" in page_cols()
    assert "ux_nb_ws_binding_active" in _indexes(db_file, "notebook_workspace_bindings")


# ---------------------------------------------------------------------------
# Phase 4.2：Topic Router workspace_id 必填（含空串）+ rebind 保留历史 + CHECK
# ---------------------------------------------------------------------------

def test_topic_router_requires_non_empty_workspace_id(db):
    """workspace_id 缺省/None/空串一律 ValueError（禁止按 ACL 全量回退）。"""
    scope = access_control.scope_from_acl('{"groups": ["engineering"]}')
    with pytest.raises(ValueError):
        builder._load_scope_wikis(db, scope, "")
    with pytest.raises(ValueError):
        builder._load_scope_wikis_from_snapshot(db, '{"groups": ["engineering"]}', "")


def test_rebind_same_workspace_reactivates_disabled_binding_row(db):
    """unbind（软禁用）保留历史记录；重新绑定同一 workspace 复用同一条 binding 行
    （id 不变、不新建重复）——不删历史、不复制记录。"""
    nb = _notebook(db, "nb1", "engineering")
    ws = _create_ws(db, "ws1", "ws_key_1", '{"groups": ["engineering"]}', "group:engineering")
    db.commit()
    b1 = service.bind_notebook(db, ws, nb, created_by=None)
    db.commit()
    binding_id = b1.id
    service.unbind_notebook(db, ws, "nb1")
    db.commit()
    db.expire_all()
    row = db.get(NotebookWorkspaceBinding, binding_id)
    assert row is not None and row.status == "disabled"  # 历史保留（不删行）
    # 重新绑定同一 workspace：disabled → active，复用同一条记录。
    b2 = service.bind_notebook(db, db.get(WikiWorkspace, "ws1"), db.get(Notebook, "nb1"), created_by=None)
    db.commit()
    assert b2.id == binding_id and b2.status == "active"
    rows = db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1"
    ).all()
    assert len(rows) == 1 and rows[0].status == "active"


def test_db_check_workspace_and_binding_status(db):
    """Phase 4.2 DB CHECK：wiki_workspaces.status 与 notebook_workspace_bindings.status
    非法取值被 SQLite 拒绝（raw SQL 断言）。"""
    db.execute(text(
        "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, status) "
        "VALUES ('w-ok', 'k-ok', 'w', '{\"groups\": [\"engineering\"]}', 'group:engineering', 'active')"
    ))
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO wiki_workspaces (id, key, name, acl_scope, scope_id, status) "
            "VALUES ('w-bad', 'k-bad', 'w', '{\"groups\": [\"engineering\"]}', 'group:engineering', 'bogus')"
        ))
        db.commit()
    db.rollback()
    assert db.get(WikiWorkspace, "w-ok") is not None

    db.execute(text("INSERT INTO notebooks (id, name, group_id) VALUES ('nb1', 'n', 'engineering')"))
    db.commit()
    db.execute(text(
        "INSERT INTO notebook_workspace_bindings (id, notebook_id, workspace_id, status) "
        "VALUES ('b-ok', 'nb1', 'w-ok', 'active')"
    ))
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO notebook_workspace_bindings (id, notebook_id, workspace_id, status) "
            "VALUES ('b-bad', 'nb1', 'w-ok', 'bogus')"
        ))
        db.commit()
    db.rollback()


def test_rebind_other_workspace_preserves_disabled_history(db):
    """跨 workspace 重新绑定：保留指向旧 workspace 的 disabled binding 历史，不删除。

    Phase 4.2：disabled 历史是可追溯审计记录，不得在换绑时清空。
    """
    nb = _notebook(db, "nb1", "engineering")
    ws_a = _create_ws(db, "wsa", "ws_key_a", '{"groups": ["engineering"]}', "group:engineering")
    ws_b = _create_ws(db, "wsb", "ws_key_b", '{"groups": ["engineering"]}', "group:engineering")
    db.commit()
    b1 = service.bind_notebook(db, ws_a, nb, created_by=None)
    db.commit()
    service.unbind_notebook(db, ws_a, "nb1")  # 软禁用指向 ws_a 的 binding
    db.commit()
    db.expire_all()
    # 重新绑定到另一个（ACL 等价）workspace ws_b。
    b2 = service.bind_notebook(db, db.get(WikiWorkspace, "wsb"), db.get(Notebook, "nb1"), created_by=None)
    db.commit()
    db.expire_all()
    rows = db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1"
    ).all()
    by_ws = {r.workspace_id: r.status for r in rows}
    # 旧 ws_a 的 disabled 历史保留；新 ws_b 为 active；两者可共存（部分唯一只约束 active）。
    assert by_ws.get("wsa") == "disabled"
    assert by_ws.get("wsb") == "active"
    assert len(rows) == 2

