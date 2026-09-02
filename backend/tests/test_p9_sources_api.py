"""P9 API 测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import jwt_utils
from app.main import app
from app.models.database import Notebook, RuntimeFeatureFlag, get_engine, get_session, init_db
from app.sources.registry import register


@pytest.fixture()
def sources_client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'sources.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    # 非钉钉 Connector 创建必需的目标知识库（带权限组）。
    db.add(Notebook(id="nb_target", name="目标库", group_id="engineering"))
    db.commit()
    db.close()
    monkeypatch.setattr("app.config.settings.database_url", url)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)

    # 注册一个 fake connector
    register("fake", lambda config: None)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _normal():
        return {"id": "u2", "username": "user", "groups": [], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    yield client, _admin, _normal
    app.dependency_overrides.clear()
    engine.dispose()


def test_list_sources_empty(sources_client):
    client, _admin, _normal = sources_client
    r = client.get("/api/sources")
    assert r.status_code == 200
    assert "connections" in r.json()
    assert "connectors" in r.json()
    assert "fake" in r.json()["connectors"]


def test_create_connection_requires_admin(sources_client):
    client, _admin, normal = sources_client
    app.dependency_overrides[jwt_utils.get_current_user] = normal
    r = client.post("/api/sources/connections", json={"connector_key": "fake", "name": "c", "target_notebook_id": "nb_target"})
    assert r.status_code == 403
    app.dependency_overrides[jwt_utils.get_current_user] = _admin


def test_create_and_list_connection(sources_client):
    client, _admin, _normal = sources_client
    r = client.post("/api/sources/connections", json={"connector_key": "fake", "name": "测试连接", "target_notebook_id": "nb_target"})
    assert r.status_code == 200
    # 不回显 secret
    assert "secret_ref" in r.json()

    r = client.get("/api/sources")
    assert len(r.json()["connections"]) == 1


def test_trigger_sync_and_cancel(sources_client):
    client, _admin, _normal = sources_client
    r = client.post("/api/sources/connections", json={"connector_key": "fake", "name": "c", "target_notebook_id": "nb_target"})
    conn_id = r.json()["id"]

    r = client.post(f"/api/sources/connections/{conn_id}/sync", json={"mode": "incremental"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    r = client.post(f"/api/sources/runs/{run_id}/cancel")
    assert r.status_code == 200


def test_duplicate_active_sync_is_rejected(sources_client):
    client, _admin, _normal = sources_client
    conn_id = client.post(
        "/api/sources/connections", json={"connector_key": "fake", "name": "single-run", "target_notebook_id": "nb_target"}
    ).json()["id"]
    first = client.post(f"/api/sources/connections/{conn_id}/sync", json={"mode": "incremental"})
    second = client.post(f"/api/sources/connections/{conn_id}/sync", json={"mode": "incremental"})
    assert first.status_code == 200
    assert second.status_code == 409


def test_sync_rejected_when_source_hub_disabled(sources_client):
    client, _admin, _normal = sources_client
    conn_id = client.post(
        "/api/sources/connections", json={"connector_key": "fake", "name": "disabled-hub", "target_notebook_id": "nb_target"}
    ).json()["id"]
    assert client.post(
        "/api/p7/flags", json={"flag": "source_hub_enabled", "value": False}
    ).status_code == 200
    response = client.post(f"/api/sources/connections/{conn_id}/sync", json={"mode": "incremental"})
    assert response.status_code == 409


def test_invalid_sync_mode_rejected(sources_client):
    client, _admin, _normal = sources_client
    r = client.post("/api/sources/connections", json={"connector_key": "fake", "name": "c", "target_notebook_id": "nb_target"})
    conn_id = r.json()["id"]
    r = client.post(f"/api/sources/connections/{conn_id}/sync", json={"mode": "bogus"})
    assert r.status_code == 400


def test_unknown_connector_rejected(sources_client):
    client, _admin, _normal = sources_client
    r = client.post("/api/sources/connections", json={"connector_key": "nonexistent", "name": "c"})
    assert r.status_code == 400


def test_non_dingtalk_requires_target_notebook(sources_client):
    """J-1 最终遗留：非钉钉 Connector 缺少 target_notebook_id → 400；钉钉除外。"""
    client, _admin, _normal = sources_client
    r = client.post("/api/sources/connections", json={"connector_key": "fake", "name": "无目标"})
    assert r.status_code == 400
    assert "目标知识库" in r.json()["detail"]


def test_dingtalk_no_target_notebook_required(sources_client):
    """J-1 遗留修复：钉钉连接不再强制 target_notebook_id（文件归属由文件夹映射决定）。

    旧语义（P9）：钉钉连接必须绑定目标知识库 → 400。
    新语义（J-1）：钉钉归属完全由文件夹映射决定，连接可以不绑定 → 200。
    """
    client, _admin, _normal = sources_client
    from app.sources.dingtalk import register_dingtalk_connector
    register_dingtalk_connector()
    r = client.post("/api/sources/connections", json={"connector_key": "dingtalk", "name": "钉钉知识库"})
    assert r.status_code == 200, r.text
    assert r.json()["target_notebook_id"] is None


def test_company_notebook_valid_target(sources_client):
    """J-1 最终返工：company Notebook（group_id=None）是合法 target（不按 group_id 是否为空判断）。"""
    client, _admin, _normal = sources_client
    from app.sources.dingtalk import register_dingtalk_connector
    from app.models.database import Notebook
    from app.api import deps
    register_dingtalk_connector()
    db = deps.get_session(deps.get_shared_engine())
    nb = Notebook(id="nb-company", name="公司库", group_id=None)  # company 合法
    db.add(nb)
    db.commit()
    db.close()
    r = client.post("/api/sources/connections", json={
        "connector_key": "dingtalk", "name": "钉钉知识库", "target_notebook_id": "nb-company",
    })
    assert r.status_code == 200, r.text


def test_unknown_notebook_rejected_as_target(sources_client):
    """J-1 最终返工：unknown（权限域无法确定）Notebook 不能作为 Connector 或映射目标。"""
    client, _admin, _normal = sources_client
    from app.sources.dingtalk import register_dingtalk_connector
    from app.models.database import Notebook, NotebookGroup
    from app.api import deps
    register_dingtalk_connector()
    db = deps.get_session(deps.get_shared_engine())
    # unknown = 混合 __public__ 与业务组 → scope_from_notebook 返回 unknown
    nb = Notebook(id="nb-unknown", name="未知库", group_id="__public__")
    db.add(nb)
    db.add(NotebookGroup(id="ng1", notebook_id="nb-unknown", group_name="engineering"))
    db.commit()
    db.close()
    # Connector 创建拒绝 unknown
    r = client.post("/api/sources/connections", json={
        "connector_key": "dingtalk", "name": "钉钉知识库", "target_notebook_id": "nb-unknown",
    })
    assert r.status_code == 400
    assert "unknown" in r.json()["detail"]


def test_duplicate_connection_rejected(sources_client):
    client, _admin, _normal = sources_client
    payload = {"connector_key": "fake", "name": "重复连接", "target_notebook_id": "nb_target"}
    assert client.post("/api/sources/connections", json=payload).status_code == 200
    r = client.post("/api/sources/connections", json=payload)
    assert r.status_code == 409
    assert "同名" in r.json()["detail"]


def test_create_does_not_store_secret(sources_client):
    client, _admin, _normal = sources_client
    r = client.post("/api/sources/connections", json={
        "connector_key": "fake", "name": "含密钥", "config_json": {"app_secret": "SECRET", "token": "T"},
        "target_notebook_id": "nb_target",
    })
    assert r.status_code == 200
    body = r.json()
    assert "SECRET" not in str(body)
    assert "config_json" in body
    # secret_ref 只回布尔，不回显内容
    assert isinstance(body["secret_ref"], bool)
