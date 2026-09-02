"""P7-BE-03：Feature Flag API 测试（Phase H 语义）。

Phase H 后仅保留仍在使用的 flag：wiki_topic_enabled、source_hub_enabled、
dingtalk_connector_enabled、gitlab_connector_enabled。旧 Card/KO/治理 flag 已删除。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import jwt_utils
from app.main import app


@pytest.fixture()
def flags_client(monkeypatch):
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import StaticPool
    from app.models.database import init_db

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    yield client, settings
    app.dependency_overrides.clear()
    engine.dispose()


def test_get_flags(flags_client):
    client, _settings = flags_client
    r = client.get("/api/p7/flags")
    assert r.status_code == 200
    flags = r.json()["flags"]
    # 保留 flag
    assert "wiki_topic_enabled" in flags
    assert "source_hub_enabled" in flags
    assert "dingtalk_connector_enabled" in flags
    assert "gitlab_connector_enabled" in flags
    # 旧 flag 已删除
    assert "card_v3_enabled" not in flags
    assert "unified_retrieval_enabled" not in flags
    assert "card_graph_enabled" not in flags
    assert "source_card_compile_enabled" not in flags
    assert "legacy_debt_card_enabled" not in flags
    assert "legacy_readonly" not in flags


def test_toggle_flag(flags_client):
    client, _settings = flags_client
    original = _settings.wiki_topic_enabled
    try:
        r = client.post("/api/p7/flags", json={"flag": "wiki_topic_enabled", "value": True})
        assert r.status_code == 200
        assert _settings.wiki_topic_enabled is True
    finally:
        _settings.wiki_topic_enabled = original  # 还原


def test_toggle_invalid_flag(flags_client):
    client, _settings = flags_client
    r = client.post("/api/p7/flags", json={"flag": "nonexistent", "value": True})
    assert r.status_code == 400


def test_legacy_ko_flags_are_unknown(flags_client):
    client, _settings = flags_client
    r = client.post("/api/p7/flags", json={"flag": "legacy_ko_write_enabled", "value": True})
    assert r.status_code == 400


def test_legacy_search_flag_is_unknown(flags_client):
    client, _settings = flags_client
    r = client.post("/api/p7/flags", json={"flag": "legacy_search_visible", "value": False})
    assert r.status_code == 400


def test_toggle_requires_admin(flags_client, monkeypatch):
    client, _settings = flags_client
    def _normal():
        return {"id": "u2", "username": "user", "groups": [], "is_admin": False}

    app.dependency_overrides[jwt_utils.get_current_user] = _normal
    r = client.post("/api/p7/flags", json={"flag": "wiki_topic_enabled", "value": True})
    assert r.status_code == 403
