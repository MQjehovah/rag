"""用户管理(需 user.manage):列表/新建本地号/禁用/重置密码/角色与组分配。

规则:
- 新建仅本地账号(username/password 必填,密码 bcrypt 落库)
- POST /password 仅本地账号可重置
- PUT /roles 按角色 name 全量替换(自己/他人均守护最后管理员来源)
- PUT /groups 仅本地账号可改(SSO/LDAP 用户组由登录同步维护),__local_admin__ 标记行保留
- 自我保护:不能禁用自己;不能移除自己最后一个管理员来源(角色 or __local_admin__)
- user.manage 等效高权:可授予任意角色/重置任意本地账号密码,授予需谨慎
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.core.rbac_seed import INTERNAL_ADMIN_GROUP
from app.core.security import parse_permissions, require_permission
from app.models.database import Group, Role, User, UserGroup, UserRole

router = APIRouter(prefix="/api/admin", tags=["RBAC 管理"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str = ""
    email: str = ""
    roles: list[str] = []
    groups: list[str] = []


class UserUpdate(BaseModel):
    display_name: str | None = None
    email: str | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str


class RolesBody(BaseModel):
    roles: list[str] = []


class GroupsBody(BaseModel):
    groups: list[str] = []


def _get_user_or_404(user_id: str, db: Session) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _role_infos(db: Session, user_id: str) -> list[dict]:
    rows = (
        db.query(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(UserRole.user_id == user_id)
        .order_by(Role.name.asc())
        .all()
    )
    return [{"name": r.name, "display_name": r.display_name} for r in rows]


def _group_names(db: Session, user_id: str) -> list[str]:
    rows = (
        db.query(UserGroup.group_name)
        .filter(UserGroup.user_id == user_id)
        .order_by(UserGroup.group_name.asc())
        .all()
    )
    return [name for (name,) in rows]


def _is_admin_capable(db: Session, user_id: str) -> bool:
    """有 __local_admin__ 标记,或任一角色的 permissions 含 "*"。"""
    marked = (
        db.query(UserGroup)
        .filter(UserGroup.user_id == user_id, UserGroup.group_name == INTERNAL_ADMIN_GROUP)
        .first()
    )
    if marked is not None:
        return True
    rows = (
        db.query(Role.permissions)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(UserRole.user_id == user_id)
        .all()
    )
    return any("*" in parse_permissions(raw) for (raw,) in rows)


def _count_active_admin_capable(db: Session) -> int:
    """系统内仍启用且具备管理员能力的用户数(用户量小,逐行判定可接受)。

    check-then-act 存在并发窗口(两名管理员同时互相降级/禁用可能同时通过校验);
    低并发管理场景可接受,如需强一致可在事务内用 PG with_for_update() 锁行。
    """
    active_ids = [uid for (uid,) in db.query(User.id).filter(User.is_active.is_(True)).all()]
    return sum(1 for uid in active_ids if _is_admin_capable(db, uid))


def _user_out_data(user: User, roles: list[dict], groups: list[str]) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "is_local": bool(user.is_local),
        "is_active": bool(user.is_active),
        "roles": roles,
        "groups": groups,
        "is_marked_admin": INTERNAL_ADMIN_GROUP in groups,
    }


def _user_out(db: Session, user: User) -> dict:
    """单用户组装;列表路径用 _user_out_data + 批量查询避免 N+1。"""
    return _user_out_data(user, _role_infos(db, user.id), _group_names(db, user.id))


def _clean_group_names(names: list[str]) -> list[str]:
    """去空去重保序;__ 前缀内部标记由登录同步维护,API 不接受。"""
    out: list[str] = []
    for raw in names:
        name = (raw or "").strip()
        if name and not name.startswith("__") and name not in out:
            out.append(name)
    return out


def _register_groups(db: Session, names: list[str]) -> None:
    """组名不在注册表则补登 source='local'(随调用方事务提交)。"""
    wanted = set(_clean_group_names(names))
    if not wanted:
        return
    existing = {name for (name,) in db.query(Group.name).filter(Group.name.in_(wanted)).all()}
    for name in sorted(wanted - existing):
        db.add(Group(id=str(uuid.uuid4()), name=name, source="local"))


def _resolve_roles(db: Session, names: list[str]) -> list[Role]:
    """角色名列表 → Role 列表(去重保序);不存在的角色名 → 400。"""
    wanted: list[str] = []
    for raw in names:
        name = (raw or "").strip()
        if name and name not in wanted:
            wanted.append(name)
    if not wanted:
        return []
    by_name = {r.name: r for r in db.query(Role).filter(Role.name.in_(wanted)).all()}
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise HTTPException(status_code=400, detail=f"角色不存在: {missing}")
    return [by_name[name] for name in wanted]


@router.get("/users")
def list_users(
    query: str = "",
    page: int = 1,
    page_size: int = 20,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_permission(current_user, "user.manage")
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    q = db.query(User)
    keyword = query.strip()
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(
            User.username.like(like),
            User.display_name.like(like),
            User.email.like(like),
        ))
    total = q.count()
    users = q.order_by(User.username.asc()).offset((page - 1) * page_size).limit(page_size).all()
    # 先取本页 ids,再 2 条批量查询组装,避免逐用户 N+1
    ids = [u.id for u in users]
    role_map: dict[str, list[dict]] = {uid: [] for uid in ids}
    group_map: dict[str, list[str]] = {uid: [] for uid in ids}
    if ids:
        rows = (
            db.query(UserRole.user_id, Role.name, Role.display_name)
            .join(Role, Role.id == UserRole.role_id)
            .filter(UserRole.user_id.in_(ids))
            .order_by(Role.name.asc())
            .all()
        )
        for uid, name, display_name in rows:
            role_map[uid].append({"name": name, "display_name": display_name})
        rows = (
            db.query(UserGroup.user_id, UserGroup.group_name)
            .filter(UserGroup.user_id.in_(ids))
            .order_by(UserGroup.group_name.asc())
            .all()
        )
        for uid, name in rows:
            group_map[uid].append(name)
    return {
        "items": [_user_out_data(u, role_map[u.id], group_map[u.id]) for u in users],
        "total": total,
    }


@router.post("/users")
def create_user(body: UserCreate, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_permission(current_user, "user.manage")
    username = body.username.strip()
    if not username or not body.password.strip():
        raise HTTPException(status_code=400, detail="username/password 必填")
    if db.query(User).filter(User.username == username).first() is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    roles = _resolve_roles(db, body.roles)
    groups = _clean_group_names(body.groups)
    _register_groups(db, groups)
    user = User(
        id=str(uuid.uuid4()), username=username, email=body.email.strip(),
        display_name=body.display_name.strip(),
        is_local=True, is_active=True, password_hash=pwd_context.hash(body.password),
    )
    db.add(user)
    for role in roles:
        db.add(UserRole(id=str(uuid.uuid4()), user_id=user.id, role_id=role.id))
    for name in groups:
        db.add(UserGroup(id=str(uuid.uuid4()), user_id=user.id, group_name=name))
    try:
        db.commit()
    except IntegrityError:
        # 并发创建同名用户的唯一约束兜底
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from None
    return _user_out(db, user)


@router.put("/users/{user_id}")
def update_user(
    user_id: str,
    body: UserUpdate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_permission(current_user, "user.manage")
    # 自我保护先于 404:当前用户即使库中查不到,也不能通过 API 禁用自己
    if body.is_active is False and user_id == current_user.get("id"):
        raise HTTPException(status_code=400, detail="不能禁用自己")
    user = _get_user_or_404(user_id, db)
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.email is not None:
        user.email = body.email
    if body.is_active is False:
        # 目标已禁用时再置 False 是空操作,只需防"禁掉最后一个在用的管理员"
        if user.is_active and _is_admin_capable(db, user.id) and _count_active_admin_capable(db) == 1:
            raise HTTPException(status_code=400, detail="不能禁用最后一个管理员")
    if body.is_active is not None:
        user.is_active = body.is_active
    db.commit()
    return _user_out(db, user)


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: str,
    body: PasswordReset,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_permission(current_user, "user.manage")
    user = _get_user_or_404(user_id, db)
    if not user.is_local:
        raise HTTPException(status_code=400, detail="仅本地账号可重置密码")
    if not body.password.strip():
        raise HTTPException(status_code=400, detail="密码不能为空")
    # 改密不会吊销已签发的 JWT(jwt_expire_minutes 默认 720 分钟);需立即失效请改用禁用账号
    user.password_hash = pwd_context.hash(body.password)
    db.commit()
    return {"ok": True}


@router.put("/users/{user_id}/roles")
def set_roles(
    user_id: str,
    body: RolesBody,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_permission(current_user, "user.manage")
    user = _get_user_or_404(user_id, db)
    roles = _resolve_roles(db, body.roles)
    has_marker = INTERNAL_ADMIN_GROUP in _group_names(db, user.id)
    keeps_star = any("*" in parse_permissions(r.permissions) for r in roles)
    if user.id == current_user.get("id"):
        # 自我保护:移除自己最后一个管理员来源(无标记且新角色无 * 且当前确为管理员)才拦
        if not has_marker and not keeps_star and _is_admin_capable(db, user.id):
            raise HTTPException(status_code=400, detail="不能移除自己的管理员角色")
    elif (
        user.is_active
        and not has_marker
        and not keeps_star
        and _is_admin_capable(db, user.id)
        and _count_active_admin_capable(db) == 1
    ):
        # 他人:不能把系统内唯一在用的管理员降级(与禁用保护对称)
        raise HTTPException(status_code=400, detail="不能移除最后一个管理员")
    db.query(UserRole).filter(UserRole.user_id == user.id).delete()
    for role in roles:
        db.add(UserRole(id=str(uuid.uuid4()), user_id=user.id, role_id=role.id))
    db.commit()
    return _user_out(db, user)


@router.put("/users/{user_id}/groups")
def set_groups(
    user_id: str,
    body: GroupsBody,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_permission(current_user, "user.manage")
    user = _get_user_or_404(user_id, db)
    if not user.is_local:
        raise HTTPException(status_code=400, detail="同步用户的组由登录同步维护")
    groups = _clean_group_names(body.groups)
    _register_groups(db, groups)
    # 只替换非标记组:__local_admin__ 由登录同步/seed 维护,API 不清除也不伪造
    db.query(UserGroup).filter(
        UserGroup.user_id == user.id,
        UserGroup.group_name != INTERNAL_ADMIN_GROUP,
    ).delete()
    for name in groups:
        db.add(UserGroup(id=str(uuid.uuid4()), user_id=user.id, group_name=name))
    db.commit()
    return _user_out(db, user)
