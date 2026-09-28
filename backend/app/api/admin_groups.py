"""组注册表管理(需 group.manage):列表/引用计数、新建、删除拦截、成员管理。

规则:
- 组名 strip 后必填;`__` 前缀为内部标记(如 __local_admin__),API 不接受
- 删除注册表行前检查五张资源表(notebooks/wiki_pages/wiki_spaces/pipelines/
  compile_templates)的 group_id 引用,有引用 → 409;删除时同步清理同名成员关系
- 成员管理仅限本地账号(SSO/LDAP 用户组由登录同步维护);__local_admin__ 标记行
  永不因组管理操作被删除
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.core.security import has_permission, require_permission
from app.core.user_utils import _ensure_group_registry
from app.models.database import (
    CompileTemplate, Group, Notebook, Pipeline, User, UserGroup, WikiPage, WikiSpace,
)

router = APIRouter(prefix="/api/admin", tags=["RBAC 管理"])


class GroupCreate(BaseModel):
    name: str = ""


class MembersBody(BaseModel):
    add: list[str] = []
    remove: list[str] = []


def _get_group_or_404(group_id: str, db: Session) -> Group:
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="组不存在")
    return group


def _ref_count(db: Session, name: str) -> int:
    """五张资源表按 group_id 引用该组名的行数之和(与 seed 回填同口径)。"""
    total = 0
    for model in (Notebook, WikiPage, WikiSpace, Pipeline, CompileTemplate):
        total += db.query(func.count(model.id)).filter(model.group_id == name).scalar() or 0
    return total


def _member_count(db: Session, name: str) -> int:
    """按用户去重计数:历史重复成员行不应让计数大于成员列表长度。"""
    return (
        db.query(func.count(func.distinct(UserGroup.user_id)))
        .filter(UserGroup.group_name == name)
        .scalar() or 0
    )


def _group_out(db: Session, group: Group) -> dict:
    return {
        "id": group.id, "name": group.name, "source": group.source,
        "member_count": _member_count(db, group.name),
        "ref_count": _ref_count(db, group.name),
    }


def _members_out(db: Session, name: str) -> list[dict]:
    rows = (
        db.query(User)
        .join(UserGroup, UserGroup.user_id == User.id)
        .filter(UserGroup.group_name == name)
        .order_by(User.username.asc())
        .all()
    )
    return [
        {
            "id": u.id, "username": u.username,
            "name": u.name or "", "is_local": bool(u.is_local),
        }
        for u in rows
    ]


def _clean_user_ids(ids: list[str]) -> list[str]:
    """去空去重保序。"""
    out: list[str] = []
    for raw in ids:
        uid = (raw or "").strip()
        if uid and uid not in out:
            out.append(uid)
    return out


@router.get("/groups")
def list_groups(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    # 只读列表放行 user.manage(用户管理页的组下拉依赖);群组管理其余端点仍限 group.manage
    if not (has_permission(current_user, "group.manage") or has_permission(current_user, "user.manage")):
        raise HTTPException(status_code=403, detail="缺少权限: group.manage 或 user.manage")
    groups = db.query(Group).order_by(Group.name.asc()).all()
    # 历史回填:登录同步写入 user_groups 但注册表缺失的群组(早期数据)补登记(来源按 sso 记;
    # 角色名与内部 `__` 前缀由 _ensure_group_registry 过滤)
    known = {g.name for g in groups}
    extra = [
        name for (name,) in db.query(UserGroup.group_name).distinct().all()
        if name and name not in known
    ]
    if extra:
        _ensure_group_registry(db, extra, "sso")
        db.commit()
        groups = db.query(Group).order_by(Group.name.asc()).all()
    # 防御:内部标记组(如 __local_admin__)不进注册表,误入也不展示
    return {"items": [_group_out(db, g) for g in groups if not g.name.startswith("__")]}


@router.post("/groups")
def create_group(body: GroupCreate, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_permission(current_user, "group.manage")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="组名必填")
    if name.startswith("__"):
        raise HTTPException(status_code=400, detail="组名不可使用内部前缀")
    if db.query(Group).filter(Group.name == name).first() is not None:
        raise HTTPException(status_code=409, detail="组名已存在")
    group = Group(id=str(uuid.uuid4()), name=name, source="local")
    db.add(group)
    try:
        db.commit()
    except IntegrityError:
        # 并发创建同名组的唯一约束兜底
        db.rollback()
        raise HTTPException(status_code=409, detail="组名已存在") from None
    return _group_out(db, group)


@router.delete("/groups/{group_id}")
def delete_group(group_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    """删除注册表行并级联清理同名成员关系;有资源引用(五表)时 409。

    check-then-act:引用检查与删除之间存在并发窗口(期间新增的引用可能漏拦截);
    低并发管理场景可接受,与 admin_users 的守卫同模式。
    """
    require_permission(current_user, "group.manage")
    group = _get_group_or_404(group_id, db)
    if group.name.startswith("__"):
        # 防御:注册表本不应有内部标记组(seed 已跳过),误入也不允许经 API 删除
        raise HTTPException(status_code=400, detail="内部组不可删除")
    if _ref_count(db, group.name) > 0:
        raise HTTPException(status_code=409, detail="仍有资源引用该组")
    # 精确按 group_name 删同名成员关系;不会误伤 __local_admin__ 行
    db.query(UserGroup).filter(UserGroup.group_name == group.name).delete()
    db.delete(group)
    db.commit()
    return {"ok": True}


@router.get("/groups/{group_id}/members")
def list_members(group_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_permission(current_user, "group.manage")
    group = _get_group_or_404(group_id, db)
    return {"items": _members_out(db, group.name)}


@router.put("/groups/{group_id}/members")
def set_members(
    group_id: str,
    body: MembersBody,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """增删组成员(仅本地账号;同步用户的组由登录同步维护)。

    同一用户同现于 add/remove 时 remove 胜出(先加后删,净效果为移除)。
    """
    require_permission(current_user, "group.manage")
    group = _get_group_or_404(group_id, db)
    if group.name.startswith("__"):
        # 防御:注册表本不应有内部标记组(seed 已跳过),成员管理不得触碰 __ 行
        raise HTTPException(status_code=400, detail="内部组不可管理成员")
    add_ids = _clean_user_ids(body.add)
    remove_ids = _clean_user_ids(body.remove)
    # add/remove 中出现过的用户都要校验:不存在 → 400;非本地账号 → 400
    wanted = add_ids + [uid for uid in remove_ids if uid not in add_ids]
    if wanted:
        found = {u.id: u for u in db.query(User).filter(User.id.in_(wanted)).all()}
        missing = [uid for uid in wanted if uid not in found]
        if missing:
            raise HTTPException(status_code=400, detail=f"用户不存在: {missing}")
        if any(not found[uid].is_local for uid in wanted):
            raise HTTPException(status_code=400, detail="同步用户的组由登录同步维护")
    existing = {
        uid for (uid,) in db.query(UserGroup.user_id).filter(
            UserGroup.group_name == group.name, UserGroup.user_id.in_(add_ids)
        ).all()
    } if add_ids else set()
    for uid in add_ids:
        if uid not in existing:
            db.add(UserGroup(id=str(uuid.uuid4()), user_id=uid, group_name=group.name))
    if remove_ids:
        db.query(UserGroup).filter(
            UserGroup.group_name == group.name,
            UserGroup.user_id.in_(remove_ids),
        ).delete(synchronize_session=False)
    db.commit()
    return {"items": _members_out(db, group.name)}
