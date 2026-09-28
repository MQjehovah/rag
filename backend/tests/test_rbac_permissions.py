"""权限解析:角色并集、* 通配、__local_admin__ 兼容、payload 输出。"""
import json
import uuid

from app.models.database import Role, User, UserGroup, UserRole, get_engine, get_session, init_db
from app.core.security import has_permission
from app.core.user_utils import build_user_payload


def _engine(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'perm.db'}")
    init_db(engine)
    return engine


def _user_with_roles(db, groups=(), role_perms=()):
    u = User(id=str(uuid.uuid4()), username="u", is_active=True)
    db.add(u)
    for g in groups:
        db.add(UserGroup(id=str(uuid.uuid4()), user_id=u.id, group_name=g))
    for perms in role_perms:
        r = Role(id=str(uuid.uuid4()), name=str(uuid.uuid4()), permissions=json.dumps(perms))
        db.add(r)
        db.add(UserRole(id=str(uuid.uuid4()), user_id=u.id, role_id=r.id))
    db.commit()
    return u


def test_has_permission_union(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    user = _user_with_roles(db, role_perms=[["sources.manage"], ["embedding.manage"]])
    payload = build_user_payload(db, user)
    assert has_permission(payload, "sources.manage")
    assert has_permission(payload, "embedding.manage")
    assert not has_permission(payload, "user.manage")


def test_star_wildcard(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    user = _user_with_roles(db, role_perms=[["*"]])
    payload = build_user_payload(db, user)
    assert has_permission(payload, "anything.at.all")


def test_local_admin_marker_implies_star(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    user = _user_with_roles(db, groups=["__local_admin__"])
    payload = build_user_payload(db, user)
    assert has_permission(payload, "user.manage")


def test_payload_shape_and_backward_compat(tmp_path):
    engine = _engine(tmp_path)
    db = get_session(engine)
    user = _user_with_roles(db, role_perms=[["sources.manage"]])
    payload = build_user_payload(db, user)
    assert "permissions" in payload and "roles" in payload
    for k in ("id", "username", "email", "display_name", "is_local", "is_active", "groups"):
        assert k in payload
    # 旧测试夹具形如 {"groups": [...]} 的裸 dict 也要能判定(缺 permissions 键)
    assert not has_permission({"groups": ["普通组"]}, "sources.manage")
