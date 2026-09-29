"""用户相关公共工具:组同步与 payload 组装,供 jwt_utils 双轨鉴权共用。"""

import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.core.rbac_seed import INTERNAL_ADMIN_GROUP
from app.core.security import parse_permissions
from app.models.database import Group, Role, User, UserGroup, UserRole

# SSO claims 会把 roles 并入 groups(历史可见性语义), 但这些是角色名而非群组:
# 注册表/群组管理/ACL 部门选项不应登记它们。
_ROLE_LIKE_NAMES = {"user", "admin", "default", "supreme", "service"}


def _ensure_group_registry(db: Session, groups: list[str], source: str) -> None:
    """把同步来的群组登记进群组注册表: 不存在则新建(内部 `__` 前缀与角色名跳过)。"""
    names = [
        g.strip() for g in (groups or [])
        if g and g.strip() and not g.strip().startswith("__")
        and g.strip().lower() not in _ROLE_LIKE_NAMES
    ]
    if not names:
        return
    existing = {name for (name,) in db.query(Group.name).filter(Group.name.in_(names)).all()}
    for name in names:
        if name not in existing:
            db.add(Group(id=str(uuid.uuid4()), name=name, source=source))
            existing.add(name)


def sync_user_groups(db: Session, user, groups: list[str], source: str = "sso"):
    """把 user 的组全量同步为 groups(删旧插新), 并把群组登记进注册表(不存在则新建)。

    空 groups 是否清空由调用方决定:本函数不做为空短路,调用方在无需
    改动时直接不调用即可(见 jwt_utils._resolve_sso_user)。
    source 标记来源(sso/ldap), 供群组管理页展示; 内部 `__` 前缀组不进注册表。
    """
    db.query(UserGroup).filter(UserGroup.user_id == user.id).delete()
    for g in groups:
        db.add(UserGroup(id=str(uuid.uuid4()), user_id=user.id, group_name=g))
    _ensure_group_registry(db, groups, source)
    db.commit()


def build_user_payload(db: Session, user: User) -> dict:
    """组装 get_current_user 返回的 dict。

    HS256 轨与 SSO 轨共用同一形状,杜绝两套 payload 漂移。
    """
    current_groups = [
        ug.group_name
        for ug in db.query(UserGroup).filter(UserGroup.user_id == user.id).all()
    ]
    role_rows = (
        db.query(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(UserRole.user_id == user.id)
        .order_by(Role.name)
        .all()
    )
    roles = [{"name": r.name, "display_name": r.display_name} for r in role_rows]
    permissions: list[str] = []
    for r in role_rows:
        for p in parse_permissions(r.permissions):
            if p not in permissions:
                permissions.append(p)
    # 管理员标记组并入 "*",使前端 payload 自洽;判定侧 effective_permissions 仍兜底补
    if INTERNAL_ADMIN_GROUP in current_groups and "*" not in permissions:
        permissions.append("*")
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "name": user.name or "",
        "work_id": user.work_id or "",
        "phone": user.phone or "",
        "department": user.department or "",
        "is_local": user.is_local,
        "is_active": user.is_active,
        "groups": current_groups,
        "is_admin": (
            settings.ldap_group_map_admin in current_groups
            if settings.ldap_group_map_admin
            else False
        ),
        "roles": roles,
        "permissions": permissions,
    }
