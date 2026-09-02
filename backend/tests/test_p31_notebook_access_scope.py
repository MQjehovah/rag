"""P31：Notebook 权限范围（group_id）配置接口测试。

group_id 是访问权限范围，不是负责人；本测试不引入 owner 概念。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import jwt_utils
from app.main import app
from app.models.database import Notebook, User, UserGroup, get_engine, get_session, init_db


@pytest.fixture()
def nb_client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'nb.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(User(id="u1", username="admin", is_local=True))
    db.add(UserGroup(user_id="u1", group_name="company_group"))
    db.add(UserGroup(user_id="u1", group_name="company_group"))  # 去重验证
    db.add(Notebook(id="nb", name="钉钉知识库", group_id=None))
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _normal():
        return {"id": "u2", "username": "user", "groups": ["company_group"], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    yield client, _admin, _normal
    app.dependency_overrides.clear()
    engine.dispose()


def test_non_admin_cannot_read_access_scopes(nb_client):
    client, _admin, normal = nb_client
    app.dependency_overrides[jwt_utils.get_current_user] = normal
    r = client.get("/api/notebooks/access-scopes")
    assert r.status_code == 403
    app.dependency_overrides[jwt_utils.get_current_user] = _admin


def test_non_admin_cannot_update_access_scope(nb_client):
    client, _admin, normal = nb_client
    app.dependency_overrides[jwt_utils.get_current_user] = normal
    r = client.patch("/api/notebooks/nb/access-scope", json={"group_id": "__local_admin__"})
    assert r.status_code == 403
    app.dependency_overrides[jwt_utils.get_current_user] = _admin


def test_list_access_scopes_deduplicates_and_marks_local(nb_client):
    client, _admin, _normal = nb_client
    r = client.get("/api/notebooks/access-scopes")
    assert r.status_code == 200
    scopes = r.json()["scopes"]
    ids = [s["id"] for s in scopes]
    # __local_admin__ 始终存在，且 type=local
    local = next(s for s in scopes if s["id"] == "__local_admin__")
    assert local["type"] == "local"
    # company_group 去重后仅一条，type=ldap
    assert ids.count("company_group") == 1
    assert next(s for s in scopes if s["id"] == "company_group")["type"] == "ldap"
    # 无空字符串
    assert "" not in ids


def test_update_access_scope_notebook_not_found(nb_client):
    client, _admin, _normal = nb_client
    r = client.patch("/api/notebooks/not-exist/access-scope", json={"group_id": "__local_admin__"})
    assert r.status_code == 404


def test_update_access_scope_unknown_group_rejected(nb_client):
    client, _admin, _normal = nb_client
    r = client.patch("/api/notebooks/nb/access-scope", json={"group_id": "fake_group"})
    assert r.status_code == 400


def test_update_access_scope_known_group_succeeds(nb_client):
    client, _admin, _normal = nb_client
    r = client.patch("/api/notebooks/nb/access-scope", json={"group_id": "company_group"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "nb"
    assert body["group_id"] == "company_group"


def test_update_access_scope_local_admin_succeeds(nb_client):
    client, _admin, _normal = nb_client
    r = client.patch("/api/notebooks/nb/access-scope", json={"group_id": "__local_admin__"})
    assert r.status_code == 200
    assert r.json()["group_id"] == "__local_admin__"
