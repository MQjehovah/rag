"""权限解析:角色并集、* 通配、__local_admin__ 兼容、payload 输出、API 透传。"""
import json
import uuid

import pytest
from fastapi import HTTPException

from app.models.database import Role, User, UserGroup, UserRole, get_engine, get_session, init_db
from app.core.security import has_permission, parse_permissions, require_permission
from app.core.user_utils import build_user_payload


def _engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'perm.db'}")
    init_db(engine)
    return engine


def _user_with_roles(db, groups=(), role_perms=()):
    """建用户并挂角色;role_perms 元素可为 list(自动转 JSON)或原始字符串。"""
    u = User(id=str(uuid.uuid4()), username="u", is_active=True)
    db.add(u)
    for g in groups:
        db.add(UserGroup(id=str(uuid.uuid4()), user_id=u.id, group_name=g))
    for perms in role_perms:
        raw = perms if isinstance(perms, str) else json.dumps(perms)
        r = Role(id=str(uuid.uuid4()), name=str(uuid.uuid4()), permissions=raw)
        db.add(r)
        db.add(UserRole(id=str(uuid.uuid4()), user_id=u.id, role_id=r.id))
    db.commit()
    return u


def test_has_permission_union(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    try:
        user = _user_with_roles(db, role_perms=[["sources.manage"], ["embedding.manage"]])
        payload = build_user_payload(db, user)
        assert has_permission(payload, "sources.manage")
        assert has_permission(payload, "embedding.manage")
        assert not has_permission(payload, "user.manage")
    finally:
        db.close()


def test_star_wildcard(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    try:
        user = _user_with_roles(db, role_perms=[["*"]])
        payload = build_user_payload(db, user)
        assert has_permission(payload, "anything.at.all")
    finally:
        db.close()


def test_local_admin_marker_implies_star(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    try:
        user = _user_with_roles(db, groups=["__local_admin__"])
        payload = build_user_payload(db, user)
        assert has_permission(payload, "user.manage")
    finally:
        db.close()


def test_payload_shape_and_backward_compat(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    try:
        user = _user_with_roles(db, role_perms=[["sources.manage"]])
        payload = build_user_payload(db, user)
        assert "permissions" in payload and "roles" in payload
        for k in ("id", "username", "email", "display_name", "is_local", "is_active", "groups"):
            assert k in payload
    finally:
        db.close()
    # 旧测试夹具形如 {"groups": [...]} 的裸 dict 也要能判定(缺 permissions 键)
    assert not has_permission({"groups": ["普通组"]}, "sources.manage")


def test_permissions_deduplicated_across_roles(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    try:
        user = _user_with_roles(
            db, role_perms=[["sources.manage"], ["sources.manage", "sources.manage"]]
        )
        payload = build_user_payload(db, user)
        assert payload["permissions"] == ["sources.manage"]
    finally:
        db.close()


def test_malformed_role_permissions_ignored(tmp_path):
    """非法 JSON / 非 list JSON / 非 str 项:不炸且不放行。"""
    engine = _engine(tmp_path)
    db = get_session(engine)
    try:
        user = _user_with_roles(
            db, role_perms=['"a*b"', '{"*":1}', "{bad json", '["ok", 1, "", null]']
        )
        payload = build_user_payload(db, user)
        assert payload["permissions"] == ["ok"]
        assert not has_permission(payload, "a*b")
        assert not has_permission(payload, "sources.manage")
        assert has_permission(payload, "ok")
    finally:
        db.close()


def test_parse_permissions_strict():
    assert parse_permissions('["a", 1, "", null, "b"]') == ["a", "b"]
    assert parse_permissions('"a*b"') == []
    assert parse_permissions('{"*": 1}') == []
    assert parse_permissions("{bad") == []
    assert parse_permissions("") == []
    assert parse_permissions(None) == []


def test_fail_closed_on_malformed_payload():
    """非 dict / permissions 或 groups 为非 list:一律不授权。"""
    assert not has_permission(None, "sources.manage")
    assert not has_permission("not-a-dict", "sources.manage")
    assert not has_permission({"permissions": "*"}, "sources.manage")
    assert not has_permission({"groups": "__local_admin__"}, "sources.manage")


def test_require_permission_raises_403():
    user = {"permissions": ["sources.manage"], "groups": []}
    require_permission(user, "sources.manage")  # 有权限不抛
    with pytest.raises(HTTPException) as exc:
        require_permission(user, "user.manage")
    assert exc.value.status_code == 403


def test_me_endpoint_passes_roles_and_permissions(api_client, as_user):
    as_user(
        ["普通组"],
        roles=[{"name": "ops", "display_name": "运维"}],
        permissions=["sources.manage"],
    )
    resp = api_client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["roles"] == [{"name": "ops", "display_name": "运维"}]
    assert data["permissions"] == ["sources.manage"]


def test_login_response_includes_role_permissions(api_engine, api_client):
    from app.api.auth import pwd_context

    db = get_session(api_engine)
    try:
        user = User(
            id=str(uuid.uuid4()), username="perm-user", is_local=True,
            password_hash=pwd_context.hash("secret123"), is_active=True,
        )
        role = Role(
            id=str(uuid.uuid4()), name="ops", display_name="运维",
            permissions=json.dumps(["sources.manage"]),
        )
        db.add(user)
        db.add(role)
        db.add(UserRole(id=str(uuid.uuid4()), user_id=user.id, role_id=role.id))
        db.commit()
    finally:
        db.close()

    resp = api_client.post(
        "/api/auth/login", json={"username": "perm-user", "password": "secret123"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["permissions"] == ["sources.manage"]
    assert body["user"]["roles"] == [{"name": "ops", "display_name": "运维"}]
