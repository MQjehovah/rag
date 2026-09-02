"""Phase J-1 定向测试：Notebook 多组 + 文件夹映射 API + Chat 自动 ACL。

覆盖：
- 映射 API：管理员 CRUD；普通用户 403；重复路径 409；未知 Notebook 400；
- Notebook 多组：替换额外授权组（管理员）；普通用户不能修改；
- Chat 自动 ACL：不提交 scope 也检索；伪造 scope 不扩大权限；
- Chat 伪造旧 scope_id 越权 → 403（不扩大权限）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import jwt_utils
from app.main import app
from app.models.database import (
    Notebook,
    NotebookGroup,
    Page,
    SourceConnection,
    User,
    UserGroup,
    get_engine,
    get_session,
    init_db,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'j1.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(User(id="u1", username="admin", is_local=True))
    db.add(UserGroup(user_id="u1", group_name="__local_admin__"))
    db.add(User(id="u2", username="engineering_user", is_local=True))
    db.add(UserGroup(user_id="u2", group_name="engineering"))
    db.add(UserGroup(user_id="u2", group_name="sales"))
    db.add(User(id="u3", username="marketing_user", is_local=True))
    db.add(UserGroup(user_id="u3", group_name="marketing"))
    db.add(Notebook(id="nb1", name="研发库", group_id="engineering"))
    db.add(Notebook(id="nb2", name="销售库", group_id="sales"))
    db.flush()
    db.add(NotebookGroup(id="ng1", notebook_id="nb1", group_name="sales"))
    # P38 后 folder mapping 需唯一钉钉 SourceConnection（否则 400「无法唯一确定连接」）
    db.add(SourceConnection(id="conn-dt", connector_key="dingtalk", name="钉钉知识库",
                            enabled=True, target_notebook_id="nb1", config_json="{}"))
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _eng_sales():
        return {"id": "u2", "username": "engineering_user", "groups": ["engineering", "sales"], "is_admin": False}

    def _mkt():
        return {"id": "u3", "username": "marketing_user", "groups": ["marketing"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    c = TestClient(app)
    yield c, _admin, _eng_sales, _mkt
    app.dependency_overrides.clear()
    engine.dispose()


# ---------------------------------------------------------------------------
# 映射 API
# ---------------------------------------------------------------------------

def test_mapping_crud_admin(client):
    c, _admin, _eng, _mkt = client
    # 新建
    r = c.post("/api/sources/dingtalk/folder-mappings", json={
        "space_id": None,
        "folder_path": "产品资料",
        "notebook_id": "nb1",
    })
    assert r.status_code == 200, r.text
    mapping = r.json()
    assert mapping["notebook_name"] == "研发库"
    mid = mapping["id"]

    # 列表
    r = c.get("/api/sources/dingtalk/folder-mappings")
    assert r.status_code == 200
    assert len(r.json()["mappings"]) == 1

    # 重复路径 → 409
    r = c.post("/api/sources/dingtalk/folder-mappings", json={
        "folder_path": "产品资料", "notebook_id": "nb2",
    })
    assert r.status_code == 409

    # 未知 Notebook → 400
    r = c.post("/api/sources/dingtalk/folder-mappings", json={
        "folder_path": "产品资料/操作手册", "notebook_id": "not-exist",
    })
    assert r.status_code == 400

    # 空路径 → 400
    r = c.post("/api/sources/dingtalk/folder-mappings", json={
        "folder_path": "  ", "notebook_id": "nb1",
    })
    assert r.status_code == 400

    # 更新
    r = c.put(f"/api/sources/dingtalk/folder-mappings/{mid}", json={
        "folder_path": "产品资料/操作手册", "notebook_id": "nb2",
    })
    assert r.status_code == 200
    assert r.json()["notebook_id"] == "nb2"

    # 删除
    r = c.delete(f"/api/sources/dingtalk/folder-mappings/{mid}")
    assert r.status_code == 200
    r = c.get("/api/sources/dingtalk/folder-mappings")
    assert len(r.json()["mappings"]) == 0


def test_mapping_non_admin_forbidden(client):
    from app.main import app as _app
    c, _admin, _eng, _mkt = client
    # 普通用户不能读/写映射
    _app.dependency_overrides[jwt_utils.get_current_user] = _eng
    r = c.get("/api/sources/dingtalk/folder-mappings")
    assert r.status_code == 403
    r = c.post("/api/sources/dingtalk/folder-mappings", json={
        "folder_path": "产品资料", "notebook_id": "nb1",
    })
    assert r.status_code == 403
    r = c.delete("/api/sources/dingtalk/folder-mappings/x")
    assert r.status_code == 403
    _app.dependency_overrides[jwt_utils.get_current_user] = _admin


# ---------------------------------------------------------------------------
# Notebook 多组 API
# ---------------------------------------------------------------------------

def test_notebook_groups_management(client):
    from app.main import app as _app
    c, _admin, _eng, _mkt = client
    # 管理员读取多组
    r = c.get("/api/notebooks/nb1/groups")
    assert r.status_code == 200
    assert {g["group_name"] for g in r.json()} == {"sales"}

    # 整体替换多组（J-1 最终返工：复用原子服务——主组=第一个组，额外组=其余）
    r = c.put("/api/notebooks/nb1/groups", json={"group_names": ["sales", "marketing"]})
    assert r.status_code == 200
    assert r.json()["group_id"] == "sales"          # 主组 = 第一个组
    assert set(r.json()["groups"]) == {"marketing"}  # 返回额外组
    # 完整多组授权 = 主组 + 额外组：engineering（旧主组）用户仍可见（marketing 额外组）
    _app.dependency_overrides[jwt_utils.get_current_user] = _eng
    ids = {nb["id"] for nb in c.get("/api/notebooks").json()}
    assert "nb1" in ids
    _app.dependency_overrides[jwt_utils.get_current_user] = _mkt
    ids = {nb["id"] for nb in c.get("/api/notebooks").json()}
    assert "nb1" in ids, "marketing（额外组）成员应可见"
    _app.dependency_overrides[jwt_utils.get_current_user] = _admin

    # 未知组名 → 400
    r = c.put("/api/notebooks/nb1/groups", json={"group_names": ["fake_group"]})
    assert r.status_code == 400

    # 普通用户不能修改
    _app.dependency_overrides[jwt_utils.get_current_user] = _eng
    r = c.put("/api/notebooks/nb1/groups", json={"group_names": ["marketing"]})
    assert r.status_code == 403
    _app.dependency_overrides[jwt_utils.get_current_user] = _admin


def test_notebook_list_visible_by_multi_group(client):
    from app.main import app as _app
    c, _admin, _eng, _mkt = client
    # engineering+sales 用户可见 nb1（engineering 主组 + sales 额外组）与 nb2
    _app.dependency_overrides[jwt_utils.get_current_user] = _eng
    r = c.get("/api/notebooks")
    assert r.status_code == 200
    ids = {nb["id"] for nb in r.json()}
    assert "nb1" in ids and "nb2" in ids
    # marketing 用户不可见 nb1/nb2
    _app.dependency_overrides[jwt_utils.get_current_user] = _mkt
    r = c.get("/api/notebooks")
    ids = {nb["id"] for nb in r.json()}
    assert "nb1" not in ids and "nb2" not in ids
    _app.dependency_overrides[jwt_utils.get_current_user] = _admin


# ---------------------------------------------------------------------------
# Chat 自动 ACL
# ---------------------------------------------------------------------------

def test_chat_auto_scope_no_scope_required(client, monkeypatch):
    """J-1：Chat 不提交权限域，多组用户自动检索（不再 400 scope_required）。"""
    from app.main import app as _app
    c, _admin, _eng, _mkt = client
    from app.api import rag_chat
    calls = []

    class _FakeOutcome:
        mode = "none"
        wiki_results = None
        raw_results = None

    class _FakeOrch:
        async def retrieve_async(self, query, user):
            calls.append(user)
            return _FakeOutcome()

    monkeypatch.setattr(rag_chat, "build_default_retrieval_orchestrator", lambda db: _FakeOrch())
    _app.dependency_overrides[jwt_utils.get_current_user] = _eng
    r = c.post("/api/chat", json={"query": "水箱容量"})
    assert r.status_code == 200
    assert calls, "orchestrator 应被调用（自动范围）"
    assert "_scope_override" not in calls[0], "自动模式不应注入 scope_override"
    _app.dependency_overrides[jwt_utils.get_current_user] = _admin


def test_chat_forged_scope_cannot_expand(client, monkeypatch):
    """J-1：伪造旧 scope_id 不能扩大权限（越权 → 403）。"""
    from app.main import app as _app
    c, _admin, _eng, _mkt = client
    from app.api import rag_chat
    calls = []

    class _FakeOrch:
        async def retrieve_async(self, query, user):
            calls.append(1)
            return None

    monkeypatch.setattr(rag_chat, "build_default_retrieval_orchestrator", lambda db: _FakeOrch())
    # marketing 用户伪造 group:engineering → 403
    _app.dependency_overrides[jwt_utils.get_current_user] = _mkt
    r = c.post("/api/chat", json={"query": "水箱容量", "scope_id": "group:engineering"})
    assert r.status_code == 403
    assert calls == [], "越权 scope 应在检索前终止"
    # marketing 用户伪造 admin → 403
    r = c.post("/api/chat", json={"query": "水箱容量", "scope_id": "admin"})
    assert r.status_code == 403
    _app.dependency_overrides[jwt_utils.get_current_user] = _admin


def test_chat_auto_scope_respects_visibility(monkeypatch, tmp_path):
    """J-1：自动范围检索必须只看到用户可见内容（ACL 在 limit 前生效）。"""
    from sqlalchemy import create_engine

    from app.core import access_control as ac
    from app.models.database import Notebook, NotebookGroup, Page as P, init_db

    url = f"sqlite:///{(tmp_path / 'v.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    init_db(engine)
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=engine)()
    nb = Notebook(id="n1", name="共享", group_id="engineering")
    s.add(nb)
    s.flush()
    s.add(NotebookGroup(id="g1", notebook_id="n1", group_name="sales"))
    s.flush()
    s.add(P(id="p1", title="共享文档", notebook_id="n1"))
    s.add(P(id="p2", title="营销", notebook_id=None))
    s.commit()

    # sales 用户可见 p1（多组授权），不可见 p2（无归属）
    visible = ac.get_visible_page_ids(s, {"id": "u", "groups": ["sales"]})
    assert "p1" in visible
    assert "p2" not in visible
    s.close()
    engine.dispose()
