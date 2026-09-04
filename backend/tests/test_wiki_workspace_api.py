"""Phase 3.1：WikiWorkspace API 测试。

覆盖：
- admin 创建 / 非 admin 403 / 同 ACL 多 workspace 创建成功（不再 409）/
  提供 key 冲突 409 / 非法 acl 400 / key 可选由系统生成
- GET 列表与详情可见性（普通用户仅可见；不可见 404）
- PATCH admin-only（status Literal 校验）；绑定 ACL 等价 / 不等价 400 / 冲突 409 /
  notebook 404 / archived workspace 绑定 409
- 解绑 404；GET {id}/wikis；wiki.py list_wiki workspace_id 过滤
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core import jwt_utils
from app.main import app
from app.models.database import (
    Notebook,
    Page,
    WikiPage,
    init_db,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'ws.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr(settings, "wiki_topic_enabled", True)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)

    db = sessionmaker(bind=engine)()

    def _admin():
        return {"id": "u-admin", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _eng():
        return {"id": "u-eng", "username": "eng", "groups": ["engineering"], "is_admin": False}

    def _sales():
        return {"id": "u-sales", "username": "sales", "groups": ["sales"], "is_admin": False}

    c = TestClient(app)
    yield c, db, _admin, _eng, _sales
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def _override(user):
    app.dependency_overrides[jwt_utils.get_current_user] = user


def _seed_workspaces(db, *, with_bindings=False, with_wikis=False):
    from app.core.wiki_workspace import service
    from app.core.wiki_workspace.schemas import WorkspaceCreate

    eng_ws = service.create_workspace(db, WorkspaceCreate(
        name="工程工作区", acl_scope='{"groups": ["engineering"]}',
    ), created_by="u-admin")
    sales_ws = service.create_workspace(db, WorkspaceCreate(
        name="销售工作区", acl_scope='{"groups": ["sales"]}',
    ), created_by="u-admin")
    db.commit()

    if with_bindings:
        nb_eng = Notebook(id="nb-eng", name="工程知识库", group_id="engineering")
        nb_sales = Notebook(id="nb-sales", name="销售知识库", group_id="sales")
        db.add_all([nb_eng, nb_sales])
        db.flush()
        service.bind_notebook(db, eng_ws, nb_eng, created_by="u-admin")
        service.bind_notebook(db, sales_ws, nb_sales, created_by="u-admin")
        db.commit()

    if with_wikis:
        db.add_all([
            WikiPage(id="w-eng", title="水箱安装", summary="s", acl_scope='{"groups": ["engineering"]}',
                     status="published", workspace_id=eng_ws.id, dirty=False),
            WikiPage(id="w-sales", title="报价方案", summary="s", acl_scope='{"groups": ["sales"]}',
                     status="published", workspace_id=sales_ws.id, dirty=False),
        ])
        db.commit()
    return eng_ws, sales_ws


def test_admin_create_and_list(client):
    c, db, _admin, _eng, _sales = client
    _override(_admin)
    r = c.post("/api/wiki-workspaces", json={
        "name": "研发组", "acl_scope": '{"groups": ["engineering"]}',
    })
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("ws_")  # 系统生成唯一 key
    assert body["scope_id"] == "group:engineering"
    assert body["status"] == "active"

    r2 = c.get("/api/wiki-workspaces")
    assert r2.status_code == 200
    assert len(r2.json()["workspaces"]) == 1


def test_non_admin_cannot_create(client):
    c, db, _admin, _eng, _sales = client
    _override(_eng)
    r = c.post("/api/wiki-workspaces", json={"name": "x", "acl_scope": '{"groups": ["engineering"]}'})
    assert r.status_code == 403


def test_same_acl_multiple_workspaces_allowed(client):
    """同 ACL 可创建多个 workspace（不再因 scope 冲突 409），key 各自系统生成。"""
    c, db, _admin, _eng, _sales = client
    _override(_admin)
    r1 = c.post("/api/wiki-workspaces", json={"name": "研发组A", "acl_scope": '{"groups": ["engineering"]}'})
    assert r1.status_code == 201
    r2 = c.post("/api/wiki-workspaces", json={"name": "研发组B", "acl_scope": '{"groups": ["engineering"]}'})
    assert r2.status_code == 201
    assert r1.json()["key"] != r2.json()["key"]
    assert r1.json()["scope_id"] == r2.json()["scope_id"] == "group:engineering"


def test_create_with_explicit_key_conflict_409(client):
    """手工提供 key 且已存在 → 409（key_conflict，独立业务 key 冲突，与 ACL 无关）。"""
    c, db, _admin, _eng, _sales = client
    _override(_admin)
    r1 = c.post("/api/wiki-workspaces", json={
        "name": "A", "key": "manual_key_1", "acl_scope": '{"groups": ["engineering"]}',
    })
    assert r1.status_code == 201
    assert r1.json()["key"] == "manual_key_1"
    # 不同 ACL 但同 key → 仍 409（key 是独立业务身份）
    r2 = c.post("/api/wiki-workspaces", json={
        "name": "B", "key": "manual_key_1", "acl_scope": '{"groups": ["sales"]}',
    })
    assert r2.status_code == 409


def test_create_invalid_acl_400(client):
    c, db, _admin, _eng, _sales = client
    _override(_admin)
    r = c.post("/api/wiki-workspaces", json={"name": "x", "acl_scope": "not-json"})
    assert r.status_code == 400
    r2 = c.post("/api/wiki-workspaces", json={"name": "x"})
    assert r2.status_code == 400


def test_get_workspace_visibility(client):
    c, db, _admin, _eng, _sales = client
    eng_ws, sales_ws = _seed_workspaces(db)
    # admin 全部可见
    _override(_admin)
    assert c.get(f"/api/wiki-workspaces/{sales_ws.id}").status_code == 200
    # engineering 用户可见 engineering，sales 不可见
    _override(_eng)
    assert c.get(f"/api/wiki-workspaces/{eng_ws.id}").status_code == 200
    assert c.get(f"/api/wiki-workspaces/{sales_ws.id}").status_code == 404
    # sales 用户相反
    _override(_sales)
    assert c.get(f"/api/wiki-workspaces/{sales_ws.id}").status_code == 200
    assert c.get(f"/api/wiki-workspaces/{eng_ws.id}").status_code == 404
    # 不存在 → 404
    assert c.get("/api/wiki-workspaces/not-exist").status_code == 404


def test_get_list_visibility(client):
    c, db, _admin, _eng, _sales = client
    _seed_workspaces(db)
    _override(_eng)
    ids = {w["id"] for w in c.get("/api/wiki-workspaces").json()["workspaces"]}
    assert len(ids) == 1  # 只看到 engineering
    _override(_sales)
    ids = {w["id"] for w in c.get("/api/wiki-workspaces").json()["workspaces"]}
    assert len(ids) == 1
    _override(_admin)
    ids = {w["id"] for w in c.get("/api/wiki-workspaces").json()["workspaces"]}
    assert len(ids) == 2


def test_patch_workspace_admin_only(client):
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db)
    _override(_eng)
    r = c.patch(f"/api/wiki-workspaces/{eng_ws.id}", json={"name": "改名"})
    assert r.status_code == 403
    _override(_admin)
    r = c.patch(f"/api/wiki-workspaces/{eng_ws.id}", json={"name": "研发组", "description": "d"})
    assert r.status_code == 200
    assert r.json()["name"] == "研发组"
    r = c.patch("/api/wiki-workspaces/not-exist", json={"name": "x"})
    assert r.status_code == 404


def test_patch_status_literal_validation(client):
    """PATCH status 非法值 → 422（Literal 校验）；合法 active/archived 通过。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db)
    _override(_admin)
    assert c.patch(f"/api/wiki-workspaces/{eng_ws.id}", json={"status": "bogus"}).status_code == 422
    assert c.patch(f"/api/wiki-workspaces/{eng_ws.id}", json={"status": "archived"}).status_code == 200
    assert c.patch(f"/api/wiki-workspaces/{eng_ws.id}", json={"status": "active"}).status_code == 200


def test_bind_notebook_acl_equivalent(client):
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db)
    db.add(Notebook(id="nb1", name="工程知识库", group_id="engineering"))
    db.commit()
    _override(_admin)
    r = c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1")
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == eng_ws.id
    assert body["notebook_id"] == "nb1"
    # 幂等：重复绑定同 workspace → 200
    r2 = c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1")
    assert r2.status_code == 200


def test_bind_notebook_acl_mismatch_400(client):
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db)
    db.add(Notebook(id="nb1", name="销售知识库", group_id="sales"))
    db.commit()
    _override(_admin)
    r = c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1")
    assert r.status_code == 400  # ACL 不等价拒绝绑定


def test_bind_notebook_archived_workspace_409(client):
    """绑定 archived workspace → 409（拒绝，区别于 404 资源不存在）。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db)
    _override(_admin)
    assert c.patch(f"/api/wiki-workspaces/{eng_ws.id}", json={"status": "archived"}).status_code == 200
    db.add(Notebook(id="nb1", name="工程知识库", group_id="engineering"))
    db.commit()
    r = c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1")
    assert r.status_code == 409


def test_bind_notebook_conflict_409_and_unbind(client):
    c, db, _admin, _eng, _sales = client
    eng_ws, sales_ws = _seed_workspaces(db)
    db.add(Notebook(id="nb1", name="工程知识库", group_id="engineering"))
    db.commit()
    _override(_admin)
    assert c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1").status_code == 200
    # 已绑定其他 workspace → 409
    assert c.post(f"/api/wiki-workspaces/{sales_ws.id}/notebooks/nb1").status_code == 409
    # 解绑（软禁用：binding 保留 disabled）
    assert c.delete(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1").status_code == 200
    # 软解绑幂等：已 disabled 重复解绑仍 200（无 active binding 可解绑 ≠ 无绑定）
    assert c.delete(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1").status_code == 200
    # 从无绑定记录的 notebook/workspace 组合 → 404
    assert c.delete(f"/api/wiki-workspaces/{sales_ws.id}/notebooks/nb1").status_code == 404


def test_bind_notebook_not_found_404(client):
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db)
    _override(_admin)
    assert c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb-missing").status_code == 404
    assert c.post("/api/wiki-workspaces/ws-missing/notebooks/nb1").status_code == 404


def test_list_workspace_wikis_visibility(client):
    c, db, _admin, _eng, _sales = client
    eng_ws, sales_ws = _seed_workspaces(db, with_wikis=True)
    _override(_eng)
    r = c.get(f"/api/wiki-workspaces/{eng_ws.id}/wikis")
    assert r.status_code == 200
    assert [w["id"] for w in r.json()["wikis"]] == ["w-eng"]
    # sales 用户看 sales workspace
    _override(_sales)
    r = c.get(f"/api/wiki-workspaces/{sales_ws.id}/wikis")
    assert [w["id"] for w in r.json()["wikis"]] == ["w-sales"]
    # sales 用户不可见 eng workspace → 404
    assert c.get(f"/api/wiki-workspaces/{eng_ws.id}/wikis").status_code == 404


def test_list_wiki_workspace_id_filter(client):
    c, db, _admin, _eng, _sales = client
    eng_ws, sales_ws = _seed_workspaces(db, with_wikis=True)
    _override(_admin)
    r = c.get("/api/wiki", params={"workspace_id": eng_ws.id})
    assert r.status_code == 200
    pages = r.json()["pages"]
    assert [p["id"] for p in pages] == ["w-eng"]
    assert pages[0]["workspace_id"] == eng_ws.id
    r2 = c.get("/api/wiki", params={"workspace_id": sales_ws.id})
    assert [p["id"] for p in r2.json()["pages"]] == ["w-sales"]


def test_create_with_scope_id(client):
    c, db, _admin, _eng, _sales = client
    _override(_admin)
    r = c.post("/api/wiki-workspaces", json={"name": "组A", "scope_id": "group:a,b"})
    assert r.status_code == 201
    assert r.json()["scope_id"] == "group:a,b"
    assert json.loads(r.json()["acl_scope"])["groups"] == ["a", "b"]
    # 冲突的 scope_id/acl_scope → 400
    r2 = c.post("/api/wiki-workspaces", json={"name": "x", "scope_id": "group:c", "acl_scope": '{"groups": ["engineering"]}'})
    assert r2.status_code == 400


def test_bind_unbind_non_admin_403(client):
    """普通用户调 POST bind / DELETE unbind → 403（admin-only 写操作）。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db, with_bindings=True)
    db.add(Notebook(id="nb-x", name="新知识库", group_id="engineering"))
    db.commit()
    _override(_eng)
    assert c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb-x").status_code == 403
    assert c.delete(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb-eng").status_code == 403
    _override(_sales)
    assert c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb-x").status_code == 403
    assert c.delete(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb-eng").status_code == 403


def test_unbind_soft_disables_and_rebind_reactivates(client):
    """软解绑：DELETE 后 binding 保留 disabled；POST rebind 复用为 active 而非 409。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, sales_ws = _seed_workspaces(db)
    db.add(Notebook(id="nb1", name="工程知识库", group_id="engineering"))
    db.commit()
    from app.models.database import NotebookWorkspaceBinding
    _override(_admin)
    assert c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1").status_code == 200
    assert c.delete(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1").status_code == 200
    db.expire_all()
    bindings = db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1"
    ).all()
    assert len(bindings) == 1 and bindings[0].status == "disabled"  # 软禁用保留记录
    # 重新绑定同 workspace：disabled → active（复用，不 409）
    r = c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1")
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    db.expire_all()
    assert db.query(NotebookWorkspaceBinding).filter(
        NotebookWorkspaceBinding.notebook_id == "nb1"
    ).count() == 1


def test_archived_workspace_hidden_from_normal_users(client):
    """archived workspace：普通用户 list 不可见 / detail 404；admin 全量可见（管理）。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db)
    _override(_admin)
    r = c.patch(f"/api/wiki-workspaces/{eng_ws.id}", json={"status": "archived"})
    assert r.status_code == 200
    assert r.json()["status"] == "archived"
    # admin 仍可见（管理用途）
    assert c.get("/api/wiki-workspaces").json()["workspaces"][0]["status"] == "archived"
    assert c.get(f"/api/wiki-workspaces/{eng_ws.id}").status_code == 200
    # 普通用户：list 不出现、detail 404
    _override(_eng)
    ids = {w["id"] for w in c.get("/api/wiki-workspaces").json()["workspaces"]}
    assert eng_ws.id not in ids
    assert c.get(f"/api/wiki-workspaces/{eng_ws.id}").status_code == 404


def test_read_bindings_admin_active_summary_only(client):
    """admin GET {ws}/notebooks：仅 active 绑定摘要，白名单字段，不含敏感键。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db, with_bindings=True)
    _override(_admin)
    r = c.get(f"/api/wiki-workspaces/{eng_ws.id}/notebooks")
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == eng_ws.id
    assert body["workspace_name"] == "工程工作区"
    assert [b["notebook_name"] for b in body["bindings"]] == ["工程知识库"]
    assert all(b["status"] == "active" for b in body["bindings"])
    assert all(b["created_at"] for b in body["bindings"])
    assert all(b["updated_at"] for b in body["bindings"])  # updated_at 存在（非 null）
    for b in body["bindings"]:
        # 绑定摘要白名单：仅允许这 6 个键，绝不出现 group_id/源路径/ACL/连接等敏感字段。
        assert set(b.keys()) == {
            "binding_id", "notebook_id", "notebook_name", "status", "created_at", "updated_at",
        }
    raw = json.dumps(body, ensure_ascii=False).lower()
    for token in ("group_id", "source_path", "acl", "connection", "created_by"):
        assert token not in raw


def test_read_bindings_non_admin_403_and_missing_ws_404(client):
    """GET {ws}/notebooks admin-only：普通用户先 403（不可见也先 403），不存在 ws → 404。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, sales_ws = _seed_workspaces(db, with_bindings=True)
    _override(_eng)
    assert c.get(f"/api/wiki-workspaces/{eng_ws.id}/notebooks").status_code == 403
    # sales workspace 对 engineering 不可见 → 仍 403（先鉴权，不泄露存在性）
    assert c.get(f"/api/wiki-workspaces/{sales_ws.id}/notebooks").status_code == 403
    assert c.get("/api/wiki-workspaces/ws-missing/notebooks").status_code == 403
    _override(_sales)
    assert c.get(f"/api/wiki-workspaces/{eng_ws.id}/notebooks").status_code == 403
    assert c.get(f"/api/wiki-workspaces/{sales_ws.id}/notebooks").status_code == 403
    _override(_admin)
    r = c.get("/api/wiki-workspaces/ws-missing/notebooks")
    assert r.status_code == 404
    assert r.json()["detail"] == "工作区不存在"


def test_read_bindings_read_only_disabled_hidden(client):
    """GET {ws}/notebooks 只读：disabled 绑定不返回；前后 binding 状态/数量与 workspace 数量不变。"""
    c, db, _admin, _eng, _sales = client
    from app.models.database import NotebookWorkspaceBinding, WikiWorkspace
    eng_ws, _ = _seed_workspaces(db, with_bindings=False)
    db.add(Notebook(id="nb1", name="待移除知识库", group_id="engineering"))
    db.commit()
    _override(_admin)
    # 先绑定再解绑 → 产生 disabled 历史绑定（软解绑保留记录）
    assert c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1").status_code == 200
    assert c.delete(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb1").status_code == 200

    def _snapshot():
        db.expire_all()
        return {
            "bindings": db.query(NotebookWorkspaceBinding).filter(
                NotebookWorkspaceBinding.workspace_id == eng_ws.id
            ).count(),
            "active": db.query(NotebookWorkspaceBinding).filter(
                NotebookWorkspaceBinding.workspace_id == eng_ws.id,
                NotebookWorkspaceBinding.status == "active",
            ).count(),
            "workspaces": db.query(WikiWorkspace).count(),
        }

    before = _snapshot()
    assert before["bindings"] == 1 and before["active"] == 0  # disabled 历史保留，无 active
    r = c.get(f"/api/wiki-workspaces/{eng_ws.id}/notebooks")
    assert r.status_code == 200
    assert r.json()["bindings"] == []  # disabled 绑定不出现在结果中
    assert _snapshot() == before  # GET 零写入：binding 数量/状态、workspace 数量均不变


def test_list_workspaces_field_redaction(client):
    """列表裁剪：普通用户不含 key/acl_scope/scope_id/created_by；admin 含全部字段。"""
    c, db, _admin, _eng, _sales = client
    _seed_workspaces(db)
    _override(_eng)
    workspaces = c.get("/api/wiki-workspaces").json()["workspaces"]
    assert len(workspaces) == 1
    assert set(workspaces[0].keys()) == {
        "id", "name", "description", "status", "created_at", "updated_at",
    }
    _override(_admin)
    workspaces = c.get("/api/wiki-workspaces").json()["workspaces"]
    assert len(workspaces) == 2
    full = {
        "id", "key", "name", "description", "acl_scope", "scope_id",
        "status", "created_by", "created_at", "updated_at",
    }
    assert all(set(w.keys()) == full for w in workspaces)


def test_workspace_detail_field_redaction(client):
    """详情裁剪：普通用户 GET {id} 不含 key/acl_scope/scope_id/created_by；admin 含。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db)
    _override(_eng)
    body = c.get(f"/api/wiki-workspaces/{eng_ws.id}").json()
    assert set(body.keys()) == {
        "id", "name", "description", "status", "created_at", "updated_at",
    }
    # 404 语义不变：sales 用户不可见 eng_ws
    _override(_sales)
    assert c.get(f"/api/wiki-workspaces/{eng_ws.id}").status_code == 404
    _override(_admin)
    body = c.get(f"/api/wiki-workspaces/{eng_ws.id}").json()
    full = {
        "id", "key", "name", "description", "acl_scope", "scope_id",
        "status", "created_by", "created_at", "updated_at",
    }
    assert set(body.keys()) == full


def test_read_bindings_sorted_by_notebook_name(client):
    """两个不同名称 notebook 绑同一 workspace：按 notebook name 升序稳定返回。"""
    c, db, _admin, _eng, _sales = client
    eng_ws, _ = _seed_workspaces(db, with_bindings=False)
    # 先 add 后 bind 的顺序与名称顺序相反，验证输出按名称而非绑定顺序。
    db.add_all([
        Notebook(id="nb-b", name="Beta 知识库", group_id="engineering"),
        Notebook(id="nb-a", name="Alpha 知识库", group_id="engineering"),
    ])
    db.commit()
    _override(_admin)
    assert c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb-b").status_code == 200
    assert c.post(f"/api/wiki-workspaces/{eng_ws.id}/notebooks/nb-a").status_code == 200
    r = c.get(f"/api/wiki-workspaces/{eng_ws.id}/notebooks")
    assert r.status_code == 200
    assert [b["notebook_name"] for b in r.json()["bindings"]] == [
        "Alpha 知识库", "Beta 知识库",
    ]
