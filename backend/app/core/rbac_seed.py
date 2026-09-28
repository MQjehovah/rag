"""RBAC 启动 seed:内置角色、本地管理员补角色、组注册表回填。全部幂等。"""
import json
import uuid

from sqlalchemy.engine import Engine

from app.models.database import (
    Group, Notebook, Role, User, UserGroup, UserRole, WikiPage, get_session,
)

BUILTIN_ADMIN = "admin"
INTERNAL_GROUP_MARKER = "__local_admin__"


def _ensure_admin_role(db) -> Role:
    role = db.query(Role).filter(Role.name == BUILTIN_ADMIN).first()
    if role is None:
        role = Role(
            id=str(uuid.uuid4()), name=BUILTIN_ADMIN, display_name="管理员",
            permissions=json.dumps(["*"]), is_system=True,
        )
        db.add(role)
        db.commit()
    return role


def _grant_admin_to_marked_local_users(db, admin_role: Role) -> None:
    """带 __local_admin__ 标记但无任何角色的用户 → 补 admin 角色(幂等)。"""
    rows = (
        db.query(User)
        .join(UserGroup, UserGroup.user_id == User.id)
        .filter(UserGroup.group_name == INTERNAL_GROUP_MARKER)
        .all()
    )
    changed = False
    for u in rows:
        exists = db.query(UserRole).filter(
            UserRole.user_id == u.id, UserRole.role_id == admin_role.id
        ).first()
        if exists is None:
            db.add(UserRole(id=str(uuid.uuid4()), user_id=u.id, role_id=admin_role.id))
            changed = True
    if changed:
        db.commit()


def _backfill_group_registry(engine: Engine) -> None:
    """把既有 user_groups 与资源表 group_id 去重回填组注册表(跳过内部标记)。"""
    db = get_session(engine)
    try:
        names: set[str] = set()
        for (name,) in db.query(UserGroup.group_name).distinct():
            if name and not name.startswith("__"):
                names.add(name)
        for (gid,) in db.query(Notebook.group_id).distinct():
            if gid:
                names.add(gid)
        for (gid,) in db.query(WikiPage.group_id).distinct():
            if gid:
                names.add(gid)
        existing = {g.name for g in db.query(Group).all()}
        for name in sorted(names - existing):
            db.add(Group(id=str(uuid.uuid4()), name=name, source="local"))
        db.commit()
    finally:
        db.close()


def seed_rbac(engine: Engine) -> None:
    db = get_session(engine)
    try:
        admin_role = _ensure_admin_role(db)
        _grant_admin_to_marked_local_users(db, admin_role)
    finally:
        db.close()
    _backfill_group_registry(engine)
