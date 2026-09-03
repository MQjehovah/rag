"""V4 Phase B 补漏：接口级越权测试。

覆盖：
- 普通用户不能创建/修改/删除/索引 Page；admin 可以
- __public__ Page 对普通用户可见
- group A 用户搜索不到 group B Card/Page
- Chat 不召回 group B 内容
- Search 不返回 group B 内容
- Community 接口不返回 group B Community
- 混合 ACL 对非管理员 fail closed（Wiki 详情 404）
- 多管理员 LDAP 配置登录正确返回 is_admin
- wiki_editor 登录后获得编辑能力；不能编辑其他组 Wiki

全部使用文件型内存 SQLite（tmp_path），不触碰真实库。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core import jwt_utils
from app.core.retrieval import bm25
from app.main import app
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiWorkspace,
    init_db,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'acl.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "wiki_topic_enabled", True)
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)
    # 每个测试独立库，清空 BM25 全局缓存避免串扰
    bm25._INDEX_CACHE.clear()

    client = TestClient(app)
    yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


def _override(user: dict):
    app.dependency_overrides[jwt_utils.get_current_user] = lambda: user


def _db(engine):
    return sessionmaker(bind=engine)()


# ---------------------------------------------------------------------------
# Page 管理权限
# ---------------------------------------------------------------------------

def test_user_cannot_create_page(client):
    c, engine = client
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.post("/api/pages", json={"title": "新页", "content": "内容"})
    assert r.status_code == 403


def test_user_cannot_update_page(client):
    c, engine = client
    db = _db(engine)
    nb = Notebook(id="nb", name="n", group_id="group_a")
    db.add(nb); db.flush()
    db.add(Page(id="p1", title="t", content="c", notebook_id="nb"))
    db.commit(); db.close()
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.put("/api/pages/p1", json={"content": "改"})
    assert r.status_code == 403


def test_user_cannot_delete_page(client):
    c, engine = client
    db = _db(engine)
    nb = Notebook(id="nb", name="n", group_id="group_a")
    db.add(nb); db.flush()
    db.add(Page(id="p1", title="t", content="c", notebook_id="nb"))
    db.commit(); db.close()
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.delete("/api/pages/p1")
    assert r.status_code == 403


def test_user_cannot_index_page(client):
    c, engine = client
    db = _db(engine)
    nb = Notebook(id="nb", name="n", group_id="group_a")
    db.add(nb); db.flush()
    db.add(Page(id="p1", title="t", content="c", notebook_id="nb"))
    db.commit(); db.close()
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.post("/api/pages/p1/index")
    assert r.status_code == 403


def test_editor_cannot_manage_page(client):
    # wiki_editor 也只能编辑 Wiki，不能改原始 Page
    c, engine = client
    db = _db(engine)
    nb = Notebook(id="nb", name="n", group_id="group_a")
    db.add(nb); db.flush()
    db.add(Page(id="p1", title="t", content="c", notebook_id="nb"))
    db.commit(); db.close()
    _override({"id": "u1", "username": "e", "groups": ["group_a", "editors"]})
    r = c.put("/api/pages/p1", json={"content": "改"})
    assert r.status_code == 403


def test_admin_can_delete_page(client):
    c, engine = client
    db = _db(engine)
    nb = Notebook(id="nb", name="n", group_id="group_a")
    db.add(nb)
    db.flush()
    db.add(Page(id="p1", title="t", content="c", notebook_id="nb"))
    db.flush()
    # Phase 5.2.1：kill ON 删除要求 Page 已绑定 Wiki Workspace（未绑定 → 409）。
    db.add(WikiWorkspace(
        id="ws1", key="key-ws1", name="工程组",
        acl_scope='{"groups": ["group_a"]}', scope_id="group:group_a", status="active",
    ))
    db.flush()
    db.add(NotebookWorkspaceBinding(notebook_id="nb", workspace_id="ws1", status="active"))
    db.commit()
    db.close()
    from app.core.wiki_pipeline import registry
    from app.core.wiki_pipeline.pipelines.wiki_default import (
        register_default_pipeline,
        unregister_default_pipeline,
    )
    registry.REGISTRY.clear()
    register_default_pipeline()
    try:
        _override({"id": "u1", "username": "a", "groups": ["admins"]})
        r = c.delete("/api/pages/p1")
        assert r.status_code == 200
    finally:
        unregister_default_pipeline()
        registry.REGISTRY.clear()


# ---------------------------------------------------------------------------
# __public__ Page 可见性
# ---------------------------------------------------------------------------

def test_public_page_visible_to_user(client):
    c, engine = client
    db = _db(engine)
    nb_pub = Notebook(id="nb_pub", name="公开", group_id="__public__")
    nb_a = Notebook(id="nb_a", name="A", group_id="group_a")
    db.add_all([nb_pub, nb_a]); db.flush()
    db.add(Page(id="p_pub", title="公开页", content="c", notebook_id="nb_pub"))
    db.add(Page(id="p_a", title="A页", content="c", notebook_id="nb_a"))
    db.commit(); db.close()

    _override({"id": "u1", "username": "u", "groups": ["group_b"]})
    r = c.get("/api/pages")
    assert r.status_code == 200
    ids = {it["id"] for it in r.json()["items"]}
    assert "p_pub" in ids
    assert "p_a" not in ids


# ---------------------------------------------------------------------------
# 跨组隔离（Page/Wiki 驱动路径）
# ---------------------------------------------------------------------------


def _seed_pages_wiki(engine):
    from app.models.database import PageChunk
    db = _db(engine)
    nb_a = Notebook(id="nb_a", name="A", group_id="group_a")
    nb_b = Notebook(id="nb_b", name="B", group_id="group_b")
    db.add_all([nb_a, nb_b]); db.flush()
    db.add(Page(id="p_a", notebook_id="nb_a", title="A页", content="A组固定水箱步骤"))
    db.add(Page(id="p_b", notebook_id="nb_b", title="B页", content="B组固定水箱步骤"))
    db.flush()
    db.add(PageChunk(id="p_a-c0", page_id="p_a", chunk_index=0, content="A组固定水箱步骤", content_type="text"))
    db.add(PageChunk(id="p_b-c0", page_id="p_b", chunk_index=0, content="B组固定水箱步骤", content_type="text"))
    db.flush()

    def wiki(wid, title, group):
        page = WikiPage(id=wid, title=title, summary="", acl_scope=json.dumps({"groups": [group]}), status="published")
        db.add(page); db.flush()
        rev = WikiRevision(id=f"{wid}-rev", wiki_page_id=wid, title=title, summary="", status="published")
        db.add(rev); db.flush()
        db.add(WikiSection(id=f"{wid}-sec", revision_id=rev.id, section_type="facts", heading="正文", content=f"{title} 内容", order_index=1))
        page.current_revision_id = rev.id

    wiki("w_a", "A组水箱", "group_a")
    wiki("w_b", "B组水箱", "group_b")
    db.commit(); db.close()


def test_search_v2_hides_other_group(client):
    c, engine = client
    _seed_pages_wiki(engine)
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.post("/api/search/v2", json={"question": "固定水箱", "scope_id": "group:group_a"})
    assert r.status_code == 200
    data = r.json()
    wiki_ids = {w["wiki_page_id"] for w in data["wiki_results"]}
    raw_titles = {rr["title"] for rr in data["raw_results"]}
    # 只返回 group_a 内容，不返回 group_b
    assert "w_a" in wiki_ids
    assert "w_b" not in wiki_ids
    assert "A页" in raw_titles
    assert "B页" not in raw_titles


def test_chat_does_not_recall_other_group(client):
    c, engine = client
    _seed_pages_wiki(engine)
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.post("/api/chat", json={"query": "固定水箱", "scope_id": "group:group_a"})
    assert r.status_code == 200
    data = r.json()
    wiki_ids = {w["wiki_page_id"] for w in data["wiki_results"]}
    raw_titles = {rr["title"] for rr in data["raw_results"]}
    assert "w_a" in wiki_ids
    assert "w_b" not in wiki_ids
    assert "A页" in raw_titles
    assert "B页" not in raw_titles


def test_community_does_not_return_other_group(client):
    """V4 实体关系图谱 ACL：group_a 看不到 group_b 的节点/边/Community/计数/搜索提示。"""
    c, engine = client
    from app.models.database import PageChunk
    from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph

    db = _db(engine)
    nb_a = Notebook(id="nb_a", name="A", group_id="group_a")
    nb_b = Notebook(id="nb_b", name="B", group_id="group_b")
    db.add_all([nb_a, nb_b]); db.flush()
    db.add(Page(id="p_a", notebook_id="nb_a", title="A页", content=""))
    db.add(Page(id="p_b", notebook_id="nb_b", title="B页", content=""))
    db.flush()
    db.add(PageChunk(id="p_a-c0", page_id="p_a", chunk_index=0, content="电池模块属于 Titan 810"))
    db.add(PageChunk(id="p_b-c0", page_id="p_b", chunk_index=0, content="驱动电机属于 Skywalker 50"))
    db.commit()
    rebuild_page_graph(db, "p_a", commit=True)
    rebuild_page_graph(db, "p_b", commit=True)
    db.close()

    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    # Phase H：旧 /api/p5/graph/communities（Card 图谱）已彻底删除，404。
    assert c.get("/api/p5/graph/communities").status_code == 404

    # communities 端点：不返回 group_b 的 Community。
    r = c.get("/api/v4/graph/communities")
    assert r.status_code == 200
    comm_names = [cm["display_name"] for cm in r.json()["communities"]]
    assert not any("Skywalker" in cm or "驱动电机" in cm for cm in comm_names)

    # subgraph 端点：不返回 group_b 节点/边。
    r2 = c.get("/api/v4/graph/subgraph")
    assert r2.status_code == 200
    body = r2.json()
    names = [n["display_name"] for n in body["nodes"]]
    assert not any("Skywalker" in n or "驱动电机" in n for n in names)

    # search 端点：不能通过搜索推断 group_b 实体。
    r3 = c.get("/api/v4/graph/search", params={"q": "Skywalker"})
    assert r3.status_code == 200
    names3 = [e["display_name"] for e in r3.json()["entities"]]
    assert "Skywalker" not in names3

    # facets 端点：不泄露 group_b 实体类型/社区。
    r4 = c.get("/api/v4/graph/facets")
    assert r4.status_code == 200
    facets_json = json.dumps(r4.json(), ensure_ascii=False)
    assert "Skywalker" not in facets_json
    assert "驱动电机" not in facets_json


# ---------------------------------------------------------------------------
# 混合 ACL fail closed（接口级）
# ---------------------------------------------------------------------------

def test_mixed_acl_wiki_hidden_from_non_admin(client):
    c, engine = client
    db = _db(engine)
    page = WikiPage(
        id="w1", title="混合", acl_scope='{"groups": ["__public__", "group_a"]}',
        status="published",
    )
    db.add(page); db.flush()
    rev = WikiRevision(id="w1-rev", wiki_page_id="w1", title="混合", summary="", status="published")
    db.add(rev); db.flush()
    db.add(WikiSection(id="w1-sec", revision_id="w1-rev", section_type="summary", heading="摘要", content="c", order_index=0))
    page.current_revision_id = "w1-rev"
    db.commit(); db.close()

    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.get("/api/wiki/w1")
    assert r.status_code == 404  # 混合 ACL 非管理员不可见

    _override({"id": "u1", "username": "a", "groups": ["admins"]})
    r = c.get("/api/wiki/w1")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 登录角色返回
# ---------------------------------------------------------------------------

def test_me_returns_roles_for_editor(client):
    c, engine = client
    _override({"id": "u1", "username": "e", "groups": ["group_a", "editors"],
               "email": "", "display_name": "", "is_local": False})
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    data = r.json()
    assert data["is_admin"] is False
    assert data["is_wiki_editor"] is True
    assert data["roles"] == ["user", "wiki_editor"]


def test_me_returns_admin_for_multi_ldap(client, monkeypatch):
    c, engine = client
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins,superadmins")
    _override({"id": "u1", "username": "a", "groups": ["superadmins"],
               "email": "", "display_name": "", "is_local": False})
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    data = r.json()
    assert data["is_admin"] is True
    assert data["roles"] == ["user", "admin"]


def test_editor_cannot_edit_other_group_wiki(client):
    c, engine = client
    db = _db(engine)
    page = WikiPage(id="w1", title="组A", acl_scope='{"groups": ["group_a"]}', status="draft")
    db.add(page); db.flush()
    rev = WikiRevision(id="w1-rev", wiki_page_id="w1", title="组A", summary="", status="draft")
    db.add(rev); db.flush()
    db.add(WikiSection(id="w1-sec", revision_id="w1-rev", section_type="summary", heading="摘要", content="c", order_index=0))
    page.current_revision_id = "w1-rev"
    db.commit(); db.close()

    _override({"id": "u1", "username": "e", "groups": ["group_b", "editors"]})
    r = c.patch("/api/wiki/w1/revisions/w1-rev/sections/w1-sec", json={"content": "改"})
    assert r.status_code == 403
