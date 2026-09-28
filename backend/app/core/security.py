"""RBAC 判定助手:角色→权限键。

兼容:payload 里 permissions 缺失(旧测试夹具/旧缓存)视为空;
groups 含 __local_admin__(SSO/LDAP 管理员标记)视为拥有 "*"。
"""
import json
from typing import List

from fastapi import HTTPException, status

from app.core.rbac_seed import INTERNAL_ADMIN_GROUP


def parse_permissions(raw: str) -> List[str]:
    """解析角色 permissions JSON:必须是 list,元素必须是非空 str。

    非法 JSON / 非 list / 非 str 项一律丢弃,不抛异常(脏数据不放行)。
    """
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, str) and p]


def effective_permissions(user: dict) -> List[str]:
    """取用户有效权限:非 dict 按空处理;权限/组各自做 list 守卫(fail-closed)。"""
    if not isinstance(user, dict):
        return []
    raw_perms = user.get("permissions")
    perms = list(raw_perms) if isinstance(raw_perms, list) else []
    groups = user.get("groups")
    if isinstance(groups, list) and INTERNAL_ADMIN_GROUP in groups:
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
