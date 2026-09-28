"""角色与权限键目录管理(需 role.manage)。"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.core.permissions import PERMISSION_CATALOG, VALID_PERMISSION_KEYS
from app.core.security import has_permission, parse_permissions, require_permission
from app.models.database import Role, UserRole

router = APIRouter(prefix="/api/admin", tags=["RBAC 管理"])


class RoleBody(BaseModel):
    name: str = ""
    display_name: str = ""
    # None 表示"本次不改权限"(PUT 部分更新);POST 视为空列表
    permissions: list[str] | None = None


def _validate_permissions(perms: list[str]) -> list[str]:
    """校验权限键并去重保序:通配 * 仅限内置管理员角色;未知键 → 400。"""
    if "*" in perms:
        raise HTTPException(status_code=400, detail="* 仅限内置管理员角色")
    bad = [p for p in perms if p not in VALID_PERMISSION_KEYS]
    if bad:
        raise HTTPException(status_code=400, detail=f"未知权限键: {bad}")
    return list(dict.fromkeys(perms))


def _role_out(role: Role, user_count: int = 0) -> dict:
    return {
        "id": role.id, "name": role.name, "display_name": role.display_name,
        "permissions": parse_permissions(role.permissions),
        "is_system": bool(role.is_system), "user_count": user_count,
    }


def _get_or_404(role_id: str, db: Session) -> Role:
    role = db.query(Role).filter(Role.id == role_id).first()
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return role


@router.get("/permissions")
def list_permissions(current_user=Depends(get_current_user)):
    require_permission(current_user, "role.manage")
    return PERMISSION_CATALOG


@router.get("/roles")
def list_roles(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    # 只读列表放行 user.manage(用户管理页的角色下拉依赖);增删改与权限目录仍限 role.manage
    if not (has_permission(current_user, "role.manage") or has_permission(current_user, "user.manage")):
        raise HTTPException(status_code=403, detail="缺少权限: role.manage 或 user.manage")
    roles = db.query(Role).order_by(Role.is_system.desc(), Role.name.asc()).all()
    counts = dict(
        db.query(UserRole.role_id, func.count(UserRole.id))
        .group_by(UserRole.role_id).all()
    )
    return {"items": [_role_out(r, counts.get(r.id, 0)) for r in roles]}


@router.post("/roles")
def create_role(body: RoleBody, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_permission(current_user, "role.manage")
    name = body.name.strip()
    display_name = body.display_name.strip()
    if not name or not display_name:
        raise HTTPException(status_code=400, detail="name/display_name 必填")
    if db.query(Role).filter(Role.name == name).first() is not None:
        raise HTTPException(status_code=409, detail="角色名已存在")
    role = Role(
        id=str(uuid.uuid4()), name=name, display_name=display_name,
        permissions=json.dumps(_validate_permissions(body.permissions or [])), is_system=False,
    )
    db.add(role)
    try:
        db.commit()
    except IntegrityError:
        # 并发创建同名角色的唯一约束兜底
        db.rollback()
        raise HTTPException(status_code=409, detail="角色名已存在") from None
    return _role_out(role)


@router.put("/roles/{role_id}")
def update_role(role_id: str, body: RoleBody, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_permission(current_user, "role.manage")
    role = _get_or_404(role_id, db)
    if role.is_system:
        raise HTTPException(status_code=400, detail="内置角色不可修改")
    if body.display_name.strip():
        role.display_name = body.display_name.strip()
    # 部分更新:permissions 缺省(None)时保留原权限,不再静默清空
    if body.permissions is not None:
        role.permissions = json.dumps(_validate_permissions(body.permissions))
    db.commit()
    return _role_out(role)


@router.delete("/roles/{role_id}")
def delete_role(role_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_permission(current_user, "role.manage")
    role = _get_or_404(role_id, db)
    if role.is_system:
        raise HTTPException(status_code=400, detail="内置角色不可删除")
    if db.query(UserRole).filter(UserRole.role_id == role_id).first() is not None:
        raise HTTPException(status_code=409, detail="仍有用户使用该角色,请先解绑")
    db.delete(role)
    db.commit()
    return {"ok": True}
