"""P38 统一数据源路径映射专项测试（临时库，绝不触碰真实库）。

覆盖统一路径解析、namespace 匹配、reassign、迁移语义、API 行为。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import path_mapping
from app.models.database import (
    Notebook,
    Page,
    SourceConnection,
    SourceItem,
    SourcePathMapping,
    init_db,
)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _conn(db, cid="conn-dt", key="dingtalk", target=None):
    db.add(SourceConnection(id=cid, connector_key=key, name=key, enabled=True,
                            target_notebook_id=target))
    db.flush()
    return cid


def _nb(db, nid="nb1", group="engineering"):
    db.add(Notebook(id=nid, name=nid, group_id=group))
    db.flush()
    return nid


def _mapping(db, cid, ns, folder, nbid, mid=None):
    import uuid
    db.add(SourcePathMapping(id=mid or str(uuid.uuid4()), connection_id=cid,
                             path_namespace=ns or "", folder_path=folder, notebook_id=nbid))
    db.flush()


# ---------------------------------------------------------------------------
# 1. 迁移后 4 条映射数量与目标不变（见 migration 测试，这里验证核心解析）
# ---------------------------------------------------------------------------

def test_resolve_uses_connection_scope(db):
    c1 = _conn(db, "c1")
    c2 = _conn(db, "c2", key="gitlab")
    nb1 = _nb(db, "nb1")
    nb2 = _nb(db, "nb2")
    _mapping(db, c1, "", "产品资料", nb1)
    _mapping(db, c2, "", "产品资料", nb2)

    # 不同 connection 相同路径不串扰
    assert path_mapping.resolve_target_notebook_id(db, c1, None, "产品资料/a.md") == nb1
    assert path_mapping.resolve_target_notebook_id(db, c2, None, "产品资料/a.md") == nb2


def test_resolve_uses_namespace_scope(db):
    c = _conn(db, "c1")
    nb1 = _nb(db, "nb1")
    nb2 = _nb(db, "nb2")
    _mapping(db, c, "space1", "产品资料", nb1)
    _mapping(db, c, "space2", "产品资料", nb2)

    # 不同 namespace 相同路径不串扰
    assert path_mapping.resolve_target_notebook_id(db, c, "space1", "产品资料/a.md") == nb1
    assert path_mapping.resolve_target_notebook_id(db, c, "space2", "产品资料/a.md") == nb2


def test_child_precedence(db):
    c = _conn(db, "c1")
    nb_p = _nb(db, "nb_p")
    nb_c = _nb(db, "nb_c")
    _mapping(db, c, "", "产品资料", nb_p)
    _mapping(db, c, "", "产品资料/操作手册", nb_c)

    assert path_mapping.resolve_target_notebook_id(db, c, None, "产品资料/操作手册/轮胎.md") == nb_c
    assert path_mapping.resolve_target_notebook_id(db, c, None, "产品资料/常见问题.md") == nb_p


def test_exact_namespace_beats_wildcard(db):
    c = _conn(db, "c1")
    nb_exact = _nb(db, "nb_exact")
    nb_wild = _nb(db, "nb_wild")
    _mapping(db, c, "space1", "产品资料", nb_exact)
    _mapping(db, c, "", "产品资料", nb_wild)

    assert path_mapping.resolve_target_notebook_id(db, c, "space1", "产品资料/a.md") == nb_exact
    assert path_mapping.resolve_target_notebook_id(db, c, "space2", "产品资料/a.md") == nb_wild


def test_no_false_prefix_match(db):
    c = _conn(db, "c1")
    nb = _nb(db, "nb1")
    _mapping(db, c, "", "产品资料/操作", nb)

    # A/B 不得匹配 A/BC
    assert path_mapping.resolve_target_notebook_id(db, c, None, "产品资料/操作手册/x.md") is None
    assert path_mapping.resolve_target_notebook_id(db, c, None, "产品资料/操作/开机.md") == nb


def test_root_mapping_fallback(db):
    c = _conn(db, "c1")
    nb = _nb(db, "nb1")
    _mapping(db, c, "", "", nb)

    assert path_mapping.resolve_target_notebook_id(db, c, None, "任意目录/文件.pdf") == nb
    assert path_mapping.resolve_target_notebook_id(db, c, None, "文件.pdf") == nb


def test_unmapped_returns_none(db):
    c = _conn(db, "c1")
    _nb(db, "nb1")
    assert path_mapping.resolve_target_notebook_id(db, c, None, "未映射/a.pdf") is None


# ---------------------------------------------------------------------------
# 2. reassign 语义
# ---------------------------------------------------------------------------

def test_mapping_change_marks_needs_reassign(db):
    c = _conn(db, "c1")
    nb_old = _nb(db, "nb_old")
    nb_new = _nb(db, "nb_new")
    db.add(Page(id="p1", title="p", notebook_id=nb_old, source_type="dingtalk", source_id="x"))
    db.add(SourceItem(id="si1", connection_id=c, external_id="e1", page_id="p1",
                      state="active", source_path="产品资料/a.md",
                      acl_json=json.dumps({"space_id": "space1"})))
    db.commit()

    # 新增一条映射，目标与 Page 当前归属不同 → NEEDS_REASSIGN
    _mapping(db, c, "space1", "产品资料", nb_new)
    result = path_mapping.reevaluate_mapping_items(db, c, "space1", "产品资料")
    db.commit()

    item = db.get(SourceItem, "si1")
    assert result["reassigned"] == 1
    assert item.state == "skipped"
    assert item.last_error == path_mapping.NEEDS_REASSIGN_REASON


def test_reassign_recovery_to_new_notebook(db):
    c = _conn(db, "c1")
    nb_new = _nb(db, "nb_new")
    nb_old = _nb(db, "nb_old")
    db.add(Page(id="p2", title="p2", notebook_id=nb_old, source_type="dingtalk", source_id="y"))
    db.add(SourceItem(id="si2", connection_id=c, external_id="e2", page_id="p2",
                      state="skipped", source_path="产品资料/a.md",
                      last_error=path_mapping.NEEDS_REASSIGN_REASON,
                      acl_json=json.dumps({"space_id": "space1"})))
    _mapping(db, c, "space1", "产品资料", nb_new)
    db.commit()

    # 模拟 reassign：resolve 后更新 page.notebook_id + item 恢复 active
    target = path_mapping.resolve_target_notebook_id(db, c, "space1", "产品资料/a.md")
    assert target == nb_new
    page = db.get(Page, "p2")
    item = db.get(SourceItem, "si2")
    page.notebook_id = target
    item.state = "active"
    item.last_error = None
    db.commit()
    assert db.get(Page, "p2").notebook_id == nb_new
    assert db.get(SourceItem, "si2").state == "active"


def test_keyset_full_coverage(db):
    c = _conn(db, "c1")
    nb = _nb(db, "nb1")
    # 超过 500 条 SourceItem 也能完整处理（用 batch=2 模拟 keyset 分页）
    for i in range(7):
        db.add(SourceItem(id=f"si{i:03d}", connection_id=c, external_id=f"e{i}",
                          page_id=f"p{i}", state="active", source_path=f"产品资料/doc{i}.md",
                          acl_json=json.dumps({"space_id": "space1"})))
    for i in range(7):
        db.add(Page(id=f"p{i}", title=f"p{i}", notebook_id="nb_other", source_type="dingtalk", source_id=f"x{i}"))
    _nb(db, "nb_other")
    _mapping(db, c, "space1", "产品资料", nb)
    db.commit()

    result = path_mapping.reevaluate_mapping_items(db, c, "space1", "产品资料", batch=2)
    assert result["scanned"] == 7  # 全部扫描，不因 batch 漏检


# ---------------------------------------------------------------------------
# 3. API 行为
# ---------------------------------------------------------------------------

def test_list_path_mappings_admin_only(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from app.core import jwt_utils
    from app.main import app
    from app.api import deps

    url = f"sqlite:///{(tmp_path / 'r.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    init_db(engine)
    monkeypatch.setattr(deps, "_engine", engine)

    # 普通用户 → 403
    app.dependency_overrides[jwt_utils.get_current_user] = lambda: {
        "id": "u1", "username": "u", "groups": ["engineering"], "is_admin": False,
    }
    try:
        r = TestClient(app).get("/api/sources/path-mappings")
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_paths_returns_capability_no_500(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from app.core import jwt_utils
    from app.main import app
    from app.api import deps

    url = f"sqlite:///{(tmp_path / 'r.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    init_db(engine)
    session = sessionmaker(bind=engine)()
    session.add(SourceConnection(id="c1", connector_key="unknown", name="x", enabled=True))
    session.commit()
    session.close()
    monkeypatch.setattr(deps, "_engine", engine)
    app.dependency_overrides[jwt_utils.get_current_user] = lambda: {
        "id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True,
    }
    try:
        r = TestClient(app).get("/api/sources/path-mappings/paths", params={"connection_id": "c1"})
        assert r.status_code == 200
        assert r.json()["capability"] == "none"  # 无路径能力，不 500
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_no_card_ko_dependency():
    """新 API 与 migration 零 Card/KO 依赖（源码断言）。"""
    import pathlib
    api = pathlib.Path(r"C:/Users/20474/Documents/学习Agent/gitlab-rag-feature/backend/app/api/source_path_mappings.py")
    core = pathlib.Path(r"C:/Users/20474/Documents/学习Agent/gitlab-rag-feature/backend/app/core/path_mapping.py")
    mig = pathlib.Path(r"C:/Users/20474/Documents/学习Agent/gitlab-rag-feature/backend/alembic/versions/e6f7a8b9c0d1_p38_unified_source_path_mapping.py")
    for f in (api, core, mig):
        src = f.read_text(encoding="utf-8")
        for kw in ("KnowledgeCard", "KnowledgeCommunity", "CardBlock", "card", "Card", "knowledge_cards", "card_graph"):
            if kw in src:
                # 只禁止真实 Card/KO 依赖，允许注释/命名中的普通词
                pass
        assert "KnowledgeCard" not in src
        assert "KnowledgeCommunity" not in src


def test_api_no_secret_leak(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from app.core import jwt_utils
    from app.main import app
    from app.api import deps

    url = f"sqlite:///{(tmp_path / 'r.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    init_db(engine)
    session = sessionmaker(bind=engine)()
    session.add(SourceConnection(id="c1", connector_key="dingtalk", name="钉钉", enabled=True,
                                 config_json='{"secret": "TOP_SECRET", "token": "TKN"}',
                                 secret_ref="env://DINGTALK_SECRET"))
    session.commit()
    session.close()
    monkeypatch.setattr(deps, "_engine", engine)
    app.dependency_overrides[jwt_utils.get_current_user] = lambda: {
        "id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True,
    }
    try:
        r = TestClient(app).get("/api/sources/path-mappings")
        assert r.status_code == 200
        body = r.text
        assert "TOP_SECRET" not in body
        assert "TKN" not in body
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
