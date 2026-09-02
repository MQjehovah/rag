"""V4 Phase C：Wiki 编辑立即生效 + 权限（API 层）。

覆盖：
- wiki_editor 可直接编辑当前已发布 Wiki，立即生效
- 普通用户编辑 403
- 跨组 editor 编辑 403
- 编辑后产生新 Revision，历史 Revision 不变

使用 tmp 文件 SQLite，不触碰真实库。
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
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'wiki_v4.db').as_posix()}"
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

    client = TestClient(app)
    yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


def _override(user: dict):
    app.dependency_overrides[jwt_utils.get_current_user] = lambda: user


def _seed_current_wiki(engine, acl='{"groups": ["group_a"]}'):
    db = sessionmaker(bind=engine)()
    page = WikiPage(id="wp1", title="主题", acl_scope=acl, status="published")
    db.add(page); db.flush()
    rev = WikiRevision(id="rev1", wiki_page_id="wp1", title="主题", summary="", status="published", edit_type="auto")
    db.add(rev); db.flush()
    db.add(WikiSection(id="sec1", revision_id="rev1", section_type="facts", heading="正文", content="旧内容", order_index=1, locked=False))
    page.current_revision_id = "rev1"
    db.commit(); db.close()


def test_editor_can_edit_current_wiki(client):
    c, engine = client
    _seed_current_wiki(engine)
    _override({"id": "u1", "username": "e", "groups": ["group_a", "editors"]})
    r = c.patch("/api/wiki/wp1/revisions/rev1/sections/sec1", json={"content": "新内容"})
    assert r.status_code == 200

    db = sessionmaker(bind=engine)()
    page = db.get(WikiPage, "wp1")
    assert page.current_revision_id != "rev1"  # 产生新 revision
    new_rev = db.get(WikiRevision, page.current_revision_id)
    assert new_rev.edit_type == "manual"
    assert new_rev.updated_by == "e"
    new_sec = db.query(WikiSection).filter(WikiSection.revision_id == new_rev.id, WikiSection.section_type == "facts").first()
    assert new_sec.content == "新内容"
    assert new_sec.locked is True
    # 历史 revision 不变
    old_sec = db.get(WikiSection, "sec1")
    assert old_sec.content == "旧内容"
    db.close()


def test_user_cannot_edit_current_wiki(client):
    c, engine = client
    _seed_current_wiki(engine)
    _override({"id": "u1", "username": "u", "groups": ["group_a"]})
    r = c.patch("/api/wiki/wp1/revisions/rev1/sections/sec1", json={"content": "新内容"})
    assert r.status_code == 403


def test_editor_cannot_edit_other_group_wiki(client):
    c, engine = client
    _seed_current_wiki(engine, acl='{"groups": ["group_a"]}')
    _override({"id": "u1", "username": "e", "groups": ["group_b", "editors"]})
    r = c.patch("/api/wiki/wp1/revisions/rev1/sections/sec1", json={"content": "新内容"})
    assert r.status_code == 403


def test_admin_can_edit_current_wiki(client):
    c, engine = client
    _seed_current_wiki(engine, acl='{"groups": ["group_a"]}')
    _override({"id": "u1", "username": "a", "groups": ["admins"]})
    r = c.patch("/api/wiki/wp1/revisions/rev1/sections/sec1", json={"content": "管理员改"})
    assert r.status_code == 200
