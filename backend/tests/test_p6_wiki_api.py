"""P6-BE-08 + P20：Wiki API 测试。

覆盖：主题目录、详情、revisions、发布、回滚。Windows 用 StaticPool。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import jwt_utils
from app.main import app
from app.models.database import (
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)


@pytest.fixture()
def wiki_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    monkeypatch.setattr("app.config.settings.wiki_topic_enabled", True)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)

    def _admin():
        return {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}

    app.dependency_overrides[jwt_utils.get_current_user] = _admin
    client = TestClient(app)
    yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


def _seed(engine):
    db = sessionmaker(bind=engine)()
    page = WikiPage(id="wp1", title="主题A", summary="摘要", status="draft")
    db.add(page)
    db.flush()
    rev = WikiRevision(id="rev1", wiki_page_id=page.id, title="主题A", summary="摘要", status="draft")
    db.add(rev)
    db.flush()
    db.add(WikiSection(id="sec1", revision_id=rev.id, section_type="summary", heading="摘要", content="内容", order_index=0))
    page.current_revision_id = rev.id
    db.commit()
    db.close()


def test_list_wiki(wiki_client):
    client, engine = wiki_client
    _seed(engine)
    r = client.get("/api/wiki")
    assert r.status_code == 200
    assert len(r.json()["pages"]) == 1


def test_get_wiki_detail_with_sections(wiki_client):
    client, engine = wiki_client
    _seed(engine)
    r = client.get("/api/wiki/wp1")
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "主题A"
    assert len(data["sections"]) == 1
    assert data["sections"][0]["section_type"] == "summary"


def test_list_revisions(wiki_client):
    client, engine = wiki_client
    _seed(engine)
    r = client.get("/api/wiki/wp1/revisions")
    assert r.status_code == 200
    assert len(r.json()["revisions"]) == 1


def test_publish_revision(wiki_client):
    client, engine = wiki_client
    _seed(engine)
    r = client.post("/api/wiki/wp1/publish", json={"revision_id": "rev1"})
    assert r.status_code == 200
    assert r.json()["superseded"] == []


def test_rollback(wiki_client):
    client, engine = wiki_client
    _seed(engine)
    r = client.post("/api/wiki/wp1/rollback/rev1")
    assert r.status_code == 200
