"""RBAC 判定助手:角色→权限键。

兼容:payload 里 permissions 缺失(旧测试夹具/旧缓存)视为空;
groups 含 __local_admin__(SSO/LDAP 管理员标记)视为拥有 "*"。
"""
from typing import List

from fastapi import HTTPException, status

from app.core.rbac_seed import INTERNAL_ADMIN_GROUP


def effective_permissions(user: dict) -> List[str]:
    perms = list(user.get("permissions") or [])
    if INTERNAL_ADMIN_GROUP in (user.get("groups") or []):
        if "*" not in perms:
            perms.append("*")
    return perms


def has_permission(user: dict, key: str) -> bool:
    perms = effective_permissions(user)
    return "*" in perms or key in perms


def require_permission(user: dict, key: str) -> None:
    if not has_permission(user, key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"缺少权限: {key}",
        )
