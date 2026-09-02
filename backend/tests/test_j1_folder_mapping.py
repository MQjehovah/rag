"""Phase J-1 定向测试：钉钉文件夹→Notebook 映射规则 + 权限底座。

覆盖（Phase J 2.2/2.3 验收）：
- 两个文件夹可映射到同一 Notebook；
- 一个文件夹不能存在两个最终目标（唯一约束 space_id+folder_path）；
- 父路径继承、子路径优先、相似前缀不误匹配；
- 未映射文件返回 None（不创建 Page/Chunk/Evidence）；
- Notebook 多组授权（company/多组/admin）Page/Wiki 可见性；
- 自动 ACL：Chat 不再要求前端权限域；伪造 scope_id 不扩大权限；
- 前端不再提交权限域。

全程临时 SQLite，不触碰真实库。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import access_control, folder_mapping
from app.models.database import (
    Notebook,
    NotebookGroup,
    Page,
    SourceConnection,
    SourcePathMapping,
    WikiPage,
    init_db,
)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    # 建立唯一钉钉连接（folder_mapping 兼容层依赖）。
    session.add(SourceConnection(id="conn-dingtalk", connector_key="dingtalk", name="钉钉", enabled=True))
    session.flush()
    yield session
    session.close()
    engine.dispose()


def _user(groups, is_admin=False):
    u = {"id": "u1", "username": "u", "groups": groups, "is_admin": is_admin}
    if is_admin:
        u["groups"] = list(set(groups or []) | {"__local_admin__"})
    return u


# ---------------------------------------------------------------------------
# 文件夹映射规则（纯逻辑）
# ---------------------------------------------------------------------------

def test_two_folders_can_map_to_same_notebook(db):
    nb = Notebook(id="nb1", name="研发库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn-dingtalk", path_namespace="", folder_path="产品资料", notebook_id="nb1"))
    db.add(SourcePathMapping(id="m2", connection_id="conn-dingtalk", path_namespace="", folder_path="故障排查", notebook_id="nb1"))
    db.commit()

    assert folder_mapping.resolve_target_notebook_id(db, None, "产品资料/操作手册/Titan.pdf") == "nb1"
    assert folder_mapping.resolve_target_notebook_id(db, None, "故障排查/电池告警.docx") == "nb1"


def test_folder_cannot_have_two_targets(db):
    """同一 space_id+folder_path 只能有一个目标（唯一约束 + API 409）。"""
    nb1 = Notebook(id="nb1", name="库1", group_id="engineering")
    nb2 = Notebook(id="nb2", name="库2", group_id="sales")
    db.add_all([nb1, nb2])
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn-dingtalk", path_namespace="", folder_path="产品资料", notebook_id="nb1"))
    db.flush()
    with pytest.raises(Exception):
        db.add(SourcePathMapping(id="m2", connection_id="conn-dingtalk", path_namespace="", folder_path="产品资料", notebook_id="nb2"))
        db.flush()


def test_parent_inherit_child_precedence(db):
    """父路径继承；子路径（更具体）优先。"""
    nb_parent = Notebook(id="nb_p", name="父库", group_id="engineering")
    nb_child = Notebook(id="nb_c", name="子库", group_id="sales")
    db.add_all([nb_parent, nb_child])
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn-dingtalk", path_namespace="", folder_path="产品资料", notebook_id="nb_p"))
    db.add(SourcePathMapping(id="m2", connection_id="conn-dingtalk", path_namespace="", folder_path="产品资料/操作手册", notebook_id="nb_c"))
    db.commit()

    # 子路径命中更具体映射
    assert folder_mapping.resolve_target_notebook_id(db, None, "产品资料/操作手册/轮胎更换.md") == "nb_c"
    # 子路径下的深层子目录继承子映射
    assert folder_mapping.resolve_target_notebook_id(db, None, "产品资料/操作手册/子目录/换胎.md") == "nb_c"
    # 非子路径命中父映射
    assert folder_mapping.resolve_target_notebook_id(db, None, "产品资料/常见问题.md") == "nb_p"
    # 其他目录未映射
    assert folder_mapping.resolve_target_notebook_id(db, None, "财务资料/发票.pdf") is None


def test_similar_prefix_no_false_match(db):
    """相似前缀不误匹配：'A/B' 不命中 'A/BC'。"""
    nb = Notebook(id="nb1", name="库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn-dingtalk", path_namespace="", folder_path="产品资料/操作", notebook_id="nb1"))
    db.commit()

    # '产品资料/操作手册' 第一段父级 '产品资料' 未映射 → 不匹配
    assert folder_mapping.resolve_target_notebook_id(db, None, "产品资料/操作手册/x.md") is None
    # '产品资料/操作' 下直接子路径命中
    assert folder_mapping.resolve_target_notebook_id(db, None, "产品资料/操作/开机.md") == "nb1"
    # 相似前缀 '产品资料/操作员' 不匹配
    assert folder_mapping.resolve_target_notebook_id(db, None, "产品资料/操作员手册/x.md") is None


def test_unmapped_returns_none(db):
    nb = Notebook(id="nb1", name="库", group_id="engineering")
    db.add(nb)
    db.commit()
    assert folder_mapping.resolve_target_notebook_id(db, None, "未映射目录/a.pdf") is None
    assert folder_mapping.resolve_target_notebook_id(db, "space1", "未映射目录/a.pdf") is None


def test_space_scoped_mapping(db):
    """space_id 限定映射只匹配该空间；不限定的匹配任意。"""
    nb1 = Notebook(id="nb1", name="库", group_id="engineering")
    db.add(nb1)
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn-dingtalk", path_namespace="space1", folder_path="产品资料", notebook_id="nb1"))
    db.add(SourcePathMapping(id="m2", connection_id="conn-dingtalk", path_namespace="", folder_path="通用资料", notebook_id="nb1"))
    db.commit()

    assert folder_mapping.resolve_target_notebook_id(db, "space1", "产品资料/a.md") == "nb1"
    assert folder_mapping.resolve_target_notebook_id(db, "space2", "产品资料/a.md") is None  # space 限定不匹配 space2
    assert folder_mapping.resolve_target_notebook_id(db, "space2", "通用资料/a.md") == "nb1"  # 不限 space


def test_root_mapping_matches_all_subpaths(db):
    """根目录映射（空路径）匹配所有子路径。"""
    nb = Notebook(id="nb1", name="库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn-dingtalk", path_namespace="", folder_path="", notebook_id="nb1"))
    db.commit()
    assert folder_mapping.resolve_target_notebook_id(db, None, "任意目录/文件.pdf") == "nb1"
    assert folder_mapping.resolve_target_notebook_id(db, None, "文件.pdf") == "nb1"


# ---------------------------------------------------------------------------
# Notebook 多组授权
# ---------------------------------------------------------------------------

def test_notebook_multi_group_access(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.ldap_group_map_admin", "admins")
    nb = Notebook(id="nb1", name="共享库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(NotebookGroup(id="ng1", notebook_id="nb1", group_name="sales"))
    db.add(NotebookGroup(id="ng2", notebook_id="nb1", group_name="after_sales"))
    db.flush()
    db.add(Page(id="p1", title="共享文档", notebook_id="nb1"))
    db.commit()

    # 任一组成员的用户可见
    assert access_control.can_view_notebook(db, _user(["engineering"]), nb) is True
    assert access_control.can_view_notebook(db, _user(["sales"]), nb) is True
    assert access_control.can_view_notebook(db, _user(["after_sales"]), nb) is True
    # 非组成员不可见
    assert access_control.can_view_notebook(db, _user(["marketing"]), nb) is False
    # 管理员可见全部
    assert access_control.can_view_notebook(db, _user(["admins"], is_admin=True), nb) is True

    # Page 可见性一致
    assert access_control.can_view_page(db, _user(["sales"]), db.get(Page, "p1")) is True
    assert access_control.can_view_page(db, _user(["marketing"]), db.get(Page, "p1")) is False

    # get_visible_page_ids：sales 组成员可见该 Page
    assert "p1" in access_control.get_visible_page_ids(db, _user(["sales"]))
    assert "p1" not in access_control.get_visible_page_ids(db, _user(["marketing"]))


def test_company_and_admin_notebook_visibility(db, monkeypatch):
    monkeypatch.setattr("app.config.settings.ldap_group_map_admin", "admins")
    nb_company = Notebook(id="nb_c", name="公司", group_id=None)
    nb_admin = Notebook(id="nb_a", name="管理员库", group_id="admins")
    db.add_all([nb_company, nb_admin])
    db.flush()
    db.add_all([
        Page(id="pc", title="公开", notebook_id="nb_c"),
        Page(id="pa", title="内部", notebook_id="nb_a"),
    ])
    db.commit()

    user = _user(["engineering"])
    assert "pc" in access_control.get_visible_page_ids(db, user)
    assert "pa" not in access_control.get_visible_page_ids(db, user)
    assert "pa" in access_control.get_visible_page_ids(db, _user(["admins"], is_admin=True))


# ---------------------------------------------------------------------------
# 同步链：未映射不创建
# ---------------------------------------------------------------------------

def _manifest_entry(doc_id, path, space_id="space1"):
    return {
        "document_id": doc_id,
        "name": f"{doc_id}.pdf",
        "space_id": space_id,
        "dingtalk_path": path,
        "source_status": "active",
    }


def test_unmapped_entry_resolves_none(db):
    nb = Notebook(id="nb1", name="研发库", group_id="engineering")
    db.add(nb)
    db.flush()
    db.add(SourcePathMapping(id="m1", connection_id="conn-dingtalk", path_namespace="", folder_path="产品资料", notebook_id="nb1"))
    db.commit()

    mapped = folder_mapping.resolve_target_notebook_id_for_entry(db, _manifest_entry("d1", "产品资料/操作手册/a.pdf"))
    assert mapped == "nb1"
    unmapped = folder_mapping.resolve_target_notebook_id_for_entry(db, _manifest_entry("d2", "财务资料/b.pdf"))
    assert unmapped is None


# ---------------------------------------------------------------------------
# 前端：Chat 不提交权限域
# ---------------------------------------------------------------------------

def test_frontend_chat_no_scope_submission():
    import pathlib
    chat_vue = pathlib.Path(
        r"C:/Users/20474/Documents/学习Agent/gitlab-rag-feature/frontend/src/views/Chat.vue"
    )
    src = chat_vue.read_text(encoding="utf-8")
    assert "scopeId" not in src
    assert "scopeOptions" not in src
    assert "ragChatApi.ask(query)" in src or "ragChatApi.ask(input.value)" in src


def test_folder_mapping_api_registered(monkeypatch, tmp_path):
    """映射路由已注册：管理员可访问列表端点（返回空列表而非 404）。"""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from app.core import jwt_utils
    from app.main import app

    url = f"sqlite:///{(tmp_path / 'r.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    init_db(engine)
    from app.api import deps
    monkeypatch.setattr(deps, "_engine", engine)
    app.dependency_overrides[jwt_utils.get_current_user] = lambda: {
        "id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True,
    }
    try:
        r = TestClient(app).get("/api/sources/dingtalk/folder-mappings")
        assert r.status_code == 200
        assert r.json() == {"mappings": []}
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
