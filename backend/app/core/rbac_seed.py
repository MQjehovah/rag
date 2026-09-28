"""RBAC 启动 seed:内置角色、本地管理员补角色、组注册表回填。全部幂等。"""
import json
import uuid

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.database import (
    CompileTemplate, Group, Notebook, Pipeline, Role, User, UserGroup, UserRole,
    WikiPage, WikiSpace, get_session,
)

BUILTIN_ADMIN = "admin"
INTERNAL_ADMIN_GROUP = "__local_admin__"


def _is_internal_name(name: str | None) -> bool:
    """内部标记名(如 __local_admin__)不进入组注册表。"""
    return bool(name) and name.startswith("__")


def _ensure_admin_role(db: Session) -> Role:
    """get-or-create 内置 admin 角色;并发启动的唯一冲突回滚后回查。"""
    role = db.query(Role).filter(Role.name == BUILTIN_ADMIN).first()
    if role is not None:
        return role
    role = Role(
        id=str(uuid.uuid4()), name=BUILTIN_ADMIN, display_name="管理员",
        permissions=json.dumps(["*"]), is_system=True,
    )
    db.add(role)
    try:
        db.commit()
    except IntegrityError:
        # 另一进程已建同名角色:回滚后回查
        db.rollback()
        role = db.query(Role).filter(Role.name == BUILTIN_ADMIN).first()
        if role is None:
            raise
    return role


def _grant_admin_to_marked_local_users(db: Session, admin_role: Role) -> None:
    """带 __local_admin__ 标记但尚无 admin 角色的用户 → 补 admin 角色(幂等)。

    单向授予:标记消失不会自动回收已授予的 admin 角色,需在用户管理界面手动收回。
    """
    rows = (
        db.query(User)
        .join(UserGroup, UserGroup.user_id == User.id)
        .filter(UserGroup.group_name == INTERNAL_ADMIN_GROUP)
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
        try:
            db.commit()
        except IntegrityError:
            # 并发下其它进程已授予同一 (user, role):回滚后无需重试
            db.rollback()


def _backfill_group_registry(db: Session) -> None:
    """把既有 user_groups 与各资源表 group_id 去重回填组注册表(跳过内部标记)。"""
    names: set[str] = set()
    for (name,) in db.query(UserGroup.group_name).distinct():
        if not _is_internal_name(name):
            names.add(name)
    for column in (
        Notebook.group_id, WikiPage.group_id, WikiSpace.group_id,
        Pipeline.group_id, CompileTemplate.group_id,
    ):
        for (gid,) in db.query(column).distinct():
            if not _is_internal_name(gid):
                names.add(gid)
    existing = {g.name for g in db.query(Group).all()}
    for name in sorted(names - existing):
        db.add(Group(id=str(uuid.uuid4()), name=name, source="local"))
        try:
            db.commit()
        except IntegrityError:
            # 并发下其它进程已登记同名组:回滚该行后继续
            db.rollback()


def seed_rbac(engine: Engine) -> None:
    """启动幂等 seed(单 session):内置角色 + 管理员补角色 + 组注册表回填。"""
    db = get_session(engine)
    try:
        admin_role = _ensure_admin_role(db)
        _grant_admin_to_marked_local_users(db, admin_role)
        _backfill_group_registry(db)
    finally:
        db.close()
