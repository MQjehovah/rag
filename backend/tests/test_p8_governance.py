"""P8-BE-02：治理概览聚合 API 测试（Phase H 语义迁移）。

旧治理 API（/api/governance/**）已彻底删除，返回 404。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.core import jwt_utils
from app.main import app
from app.models.database import init_db


@pytest.fixture()
def gov_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _connection_record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    def _normal():
        return {"id": "u2", "username": "user", "groups": [], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    yield client, _admin, _normal
    app.dependency_overrides.clear()
    engine.dispose()


def test_governance_overview_gone(gov_client):
    """Phase H：治理概览 API 已删除，普通用户与管理员均 404。"""
    client, _admin, normal = gov_client
    app.dependency_overrides[jwt_utils.get_current_user] = normal
    assert client.get("/api/governance/overview").status_code == 404
    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    assert client.get("/api/governance/overview").status_code == 404


def test_governance_quality_gone(gov_client):
    client, _admin, _normal = gov_client
    assert client.get("/api/governance/quality").status_code == 404
