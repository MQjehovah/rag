"""V4 Phase B：统一权限服务单元测试。

覆盖：
- RBAC 角色判定（user / wiki_editor / admin）
- ACL 范围解析（company / group / admin / unknown）
- 对象级 can_view / can_edit / can_manage
- 集合查询 get_visible_*
- fail closed（范围缺失 / JSON 无法解析 / 无归属页面）
- __local_admin__ 本地兼容

全部使用内存 SQLite（StaticPool），不触碰真实库。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core import access_control
from app.models.database import (
    Notebook,
    Page,
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
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def roles(monkeypatch):
    """固定角色组映射：admins 组=admin，editors 组=wiki_editor。"""
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins")
    monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
    return settings


def _user(groups):
    return {"id": "u1", "username": "u", "groups": groups}


# ---------------------------------------------------------------------------
# 角色判定
# ---------------------------------------------------------------------------

def test_is_admin_local(roles):
    assert access_control.is_admin(_user(["__local_admin__"])) is True
    assert access_control.is_admin(_user(["staff"])) is False


def test_is_admin_ldap_mapping(roles):
    assert access_control.is_admin(_user(["admins"])) is True
    assert access_control.is_admin(_user(["editors"])) is False


def test_is_wiki_editor(roles):
    assert access_control.is_wiki_editor(_user(["editors"])) is True
    assert access_control.is_wiki_editor(_user(["staff"])) is False
    # admin 单独判定，不算 editor（更高角色）
    assert access_control.is_wiki_editor(_user(["admins"])) is False


def test_user_roles(roles):
    assert access_control.user_roles(_user(["staff"])) == {"user"}
    assert access_control.user_roles(_user(["editors"])) == {"user", "wiki_editor"}
    assert access_control.user_roles(_user(["admins"])) == {"user", "admin"}


def test_is_admin_empty_groups(roles):
    assert access_control.is_admin(_user([])) is False
    assert access_control.is_admin(_user(None)) is False


# ---------------------------------------------------------------------------
# 范围解析
# ---------------------------------------------------------------------------

def test_scope_from_group_id_company():
    assert access_control.scope_from_group_id(None).kind == "company"
    assert access_control.scope_from_group_id("__public__").kind == "company"


def test_scope_from_group_id_admin(roles):
    assert access_control.scope_from_group_id("__local_admin__").kind == "admin"
    assert access_control.scope_from_group_id("admins").kind == "admin"


def test_scope_from_group_id_group(roles):
    s = access_control.scope_from_group_id("staff")
    assert s.kind == "group"
    assert s.groups == frozenset({"staff"})


def test_scope_from_acl_group():
    s = access_control.scope_from_acl('{"groups": ["staff"]}')
    assert s.kind == "group"
    assert s.groups == frozenset({"staff"})


def test_scope_from_acl_public():
    assert access_control.scope_from_acl('{"groups": ["__public__"]}').kind == "company"


def test_scope_from_acl_admin(roles):
    assert access_control.scope_from_acl('{"groups": ["admins"]}').kind == "admin"


def test_scope_from_acl_explicit(roles):
    assert access_control.scope_from_acl('{"scope": "company"}').kind == "company"
    assert access_control.scope_from_acl('{"scope": "admin"}').kind == "admin"


def test_scope_from_acl_unknown():
    # 缺失 / 无法解析 / 空 groups → fail closed
    assert access_control.scope_from_acl(None).kind == "unknown"
    assert access_control.scope_from_acl("").kind == "unknown"
    assert access_control.scope_from_acl("not json").kind == "unknown"
    assert access_control.scope_from_acl('{"groups": []}').kind == "unknown"
    # scope=group 无具体 groups → unknown（最小权限）
    assert access_control.scope_from_acl('{"scope": "group"}').kind == "unknown"


def test_scope_from_acl_legacy_array_format(roles):
    """旧格式 JSON 数组：["engineering"] → group。"""
    s = access_control.scope_from_acl('["engineering"]')
    assert s.kind == "group"
    assert s.groups == frozenset({"engineering"})
    # 空数组 → unknown
    assert access_control.scope_from_acl("[]").kind == "unknown"


def test_scope_from_acl_mixed_public_group_fail_closed(roles):
    """混合 public + 业务组 → 非管理员 fail closed（不得扩大可见范围）。"""
    s = access_control.scope_from_acl('{"groups": ["__public__", "group_a"]}')
    assert s.kind == "unknown"


def test_scope_from_acl_multi_group(roles):
    """多个不同业务组 → group（J-1 多组授权：任一组成员的用户即可见）。

    旧语义（Phase B）把多组视为权限域混合冲突 → unknown；J-1 明确支持
    「Notebook 可同时授权两个业务组」，同一文件不复制，多组是合法授权方式。
    """
    s = access_control.scope_from_acl('{"groups": ["group_a", "group_b"]}')
    assert s.kind == "group"
    assert s.groups == frozenset({"group_a", "group_b"})


def test_scope_from_acl_admin_group_fail_closed(roles):
    """管理员组 + 业务组混合 → unknown。"""
    s = access_control.scope_from_acl('{"groups": ["admins", "group_a"]}')
    assert s.kind == "unknown"


def test_scope_from_acl_explicit_conflict_fail_closed(roles):
    """显式 scope 与 groups 冲突 → unknown。"""
    s = access_control.scope_from_acl('{"scope": "group", "groups": ["__public__"]}')
    assert s.kind == "unknown"


def test_can_view_wiki_mixed_acl_fail_closed(db, roles):
    """混合 ACL 的 Wiki 对非管理员不可见，对管理员可见。"""
    wp = WikiPage(id="w1", title="混合", acl_scope='{"groups": ["__public__", "group_a"]}')
    db.add(wp); db.commit()
    assert access_control.can_view_wiki(db, _user(["group_a"]), wp) is False
    assert access_control.can_view_wiki(db, _user(["admins"]), wp) is True


# ---------------------------------------------------------------------------
# 对象级判断
# ---------------------------------------------------------------------------

def test_can_view_notebook_company(db, roles):
    nb = Notebook(id="nb1", name="公开", group_id=None)
    db.add(nb); db.commit()
    assert access_control.can_view_notebook(db, _user(["staff"]), nb) is True


def test_can_view_notebook_group(db, roles):
    nb = Notebook(id="nb1", name="组内", group_id="staff")
    db.add(nb); db.commit()
    assert access_control.can_view_notebook(db, _user(["staff"]), nb) is True
    assert access_control.can_view_notebook(db, _user(["other"]), nb) is False
    assert access_control.can_view_notebook(db, _user(["admins"]), nb) is True


def test_can_view_page_fail_closed_no_notebook(db, roles):
    page = Page(id="p1", title="无归属", notebook_id=None)
    db.add(page); db.commit()
    # 管理员可见全部；普通用户对无归属页面 fail closed
    assert access_control.can_view_page(db, _user(["admins"]), page) is True
    assert access_control.can_view_page(db, _user(["staff"]), page) is False


def test_can_view_page_group(db, roles):
    nb = Notebook(id="nb1", name="组内", group_id="staff")
    db.add(nb); db.flush()
    page = Page(id="p1", title="t", notebook_id="nb1")
    db.add(page); db.commit()
    assert access_control.can_view_page(db, _user(["staff"]), page) is True
    assert access_control.can_view_page(db, _user(["other"]), page) is False


def test_can_view_wiki_direct_scope(db, roles):
    # company
    wp = WikiPage(id="w1", title="公开", acl_scope='{"groups": ["__public__"]}')
    db.add(wp); db.commit()
    assert access_control.can_view_wiki(db, _user(["staff"]), wp) is True
    # group
    wp2 = WikiPage(id="w2", title="组内", acl_scope='{"groups": ["staff"]}')
    db.add(wp2); db.commit()
    assert access_control.can_view_wiki(db, _user(["staff"]), wp2) is True
    assert access_control.can_view_wiki(db, _user(["other"]), wp2) is False


def test_can_view_wiki_fail_closed(db, roles):
    # acl_scope 缺失 → 非管理员 fail closed
    wp = WikiPage(id="w1", title="缺失范围", acl_scope=None)
    db.add(wp); db.commit()
    assert access_control.can_view_wiki(db, _user(["staff"]), wp) is False
    assert access_control.can_view_wiki(db, _user(["admins"]), wp) is True


def test_can_edit_wiki(db, roles):
    wp = WikiPage(id="w1", title="组内", acl_scope='{"groups": ["staff"]}')
    db.add(wp); db.commit()
    # 普通用户不能编辑
    assert access_control.can_edit_wiki(db, _user(["staff"]), wp) is False
    # wiki_editor 可编辑自己可见的
    assert access_control.can_edit_wiki(db, _user(["staff", "editors"]), wp) is True
    # 管理员可编辑全部（包括不可见范围）
    assert access_control.can_edit_wiki(db, _user(["admins"]), wp) is True


def test_can_edit_wiki_editor_cannot_cross_group(db, roles):
    wp = WikiPage(id="w1", title="组A", acl_scope='{"groups": ["group_a"]}')
    db.add(wp); db.commit()
    # editor 属于 group_b，看不到 group_a 的 wiki → 不能编辑
    assert access_control.can_edit_wiki(db, _user(["group_b", "editors"]), wp) is False


def test_can_manage_sources(roles):
    assert access_control.can_manage_sources(_user(["admins"])) is True
    assert access_control.can_manage_sources(_user(["__local_admin__"])) is True
    assert access_control.can_manage_sources(_user(["editors"])) is False
    assert access_control.can_manage_sources(_user(["staff"])) is False


def test_can_manage_pages(roles):
    # 只有 admin 能管理原始 Page
    assert access_control.can_manage_pages(_user(["admins"])) is True
    assert access_control.can_manage_pages(_user(["__local_admin__"])) is True
    assert access_control.can_manage_pages(_user(["editors"])) is False
    assert access_control.can_manage_pages(_user(["staff"])) is False


def test_role_abilities(roles):
    a = access_control.role_abilities(["staff"])
    assert a == {"is_admin": False, "is_wiki_editor": False, "roles": ["user"]}
    e = access_control.role_abilities(["staff", "editors"])
    assert e["is_admin"] is False and e["is_wiki_editor"] is True
    assert e["roles"] == ["user", "wiki_editor"]
    adm = access_control.role_abilities(["admins"])
    assert adm["is_admin"] is True and adm["is_wiki_editor"] is False
    assert adm["roles"] == ["user", "admin"]


def test_role_abilities_multi_admin_ldap(monkeypatch):
    # 逗号分隔多个管理员组（LDAP 组为 cn 简单名，不含逗号）
    monkeypatch.setattr(settings, "ldap_group_map_admin", "admins,superadmins")
    a = access_control.role_abilities(["superadmins"])
    assert a["is_admin"] is True


# ---------------------------------------------------------------------------
# 集合查询
# ---------------------------------------------------------------------------

def test_get_visible_page_ids(db, roles):
    nb_public = Notebook(id="nb_pub", name="公开", group_id=None)
    nb_a = Notebook(id="nb_a", name="A", group_id="group_a")
    nb_b = Notebook(id="nb_b", name="B", group_id="group_b")
    db.add_all([nb_public, nb_a, nb_b]); db.flush()
    db.add_all([
        Page(id="p1", title="pub", notebook_id="nb_pub"),
        Page(id="p2", title="a", notebook_id="nb_a"),
        Page(id="p3", title="b", notebook_id="nb_b"),
    ]); db.commit()

    assert access_control.get_visible_page_ids(db, _user(["group_a"])) == {"p1", "p2"}
    assert access_control.get_visible_page_ids(db, _user(["group_b"])) == {"p1", "p3"}
    assert access_control.get_visible_page_ids(db, _user(["admins"])) == {"p1", "p2", "p3"}


def test_get_visible_wiki_page_ids(db, roles):
    db.add_all([
        WikiPage(id="w1", title="公开", acl_scope='{"groups": ["__public__"]}'),
        WikiPage(id="w2", title="A", acl_scope='{"groups": ["group_a"]}'),
        WikiPage(id="w3", title="B", acl_scope='{"groups": ["group_b"]}'),
    ]); db.commit()

    assert access_control.get_visible_wiki_page_ids(db, _user(["group_a"])) == {"w1", "w2"}
    assert access_control.get_visible_wiki_page_ids(db, _user(["admins"])) == {"w1", "w2", "w3"}
