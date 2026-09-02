"""V4 债务键控纯函数（v1，冻结版本）。

供运行时（debt_service）与 Alembic reconciliation migration 共用同一份
版本化稳定算法。本模块必须保持自包含：不得 import jieba / settings / LDAP /
access_control / 数据库模型，否则 cluster_key 与 scope 解析将随环境、
hash seed 或未来业务代码变化而不再确定。

算法版本：v1（2026 Phase H 封板）。
- normalize_query：NFKC + 小写 + 去空白/标点，保留中文/英文/数字。
- build_cluster_key：sha256(scope_id + ':' + normalized_query)。不依赖 jieba
  分词——规范化查询字符串本身即稳定键，精确去重由数据库唯一约束保证；
  相似问题合并由 debt_service 的相似度逻辑独立处理，不进入本键。
- scope_id_from_acl：从 acl_scope JSON 解析规范 scope_id（company / admin /
  group:<名> / unknown），管理员组冻结为本地试点组 __local_admin__（V4 计划
  8.2「仅本地试点」；生产 LDAP 管理员组需 H-2 前人工确认并另行处理）。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

SCOPE_COMPANY = "company"
SCOPE_ADMIN = "admin"
SCOPE_GROUP_PREFIX = "group:"
SCOPE_UNKNOWN = "unknown"

# 冻结的本地试点管理员组与公开标记（不读 settings）。
_ADMIN_GROUPS = frozenset({"__local_admin__"})
_PUBLIC_MARKERS = frozenset({"__public__"})


def normalize_query(question: str) -> str:
    """确定性规范化：NFKC + 小写 + 去空白/标点，保留中文/英文/数字。"""
    q = unicodedata.normalize("NFKC", question or "")
    q = q.lower()
    return re.sub(r"[^\w一-鿿]", "", q)


def build_cluster_key(scope_id: str, normalized_query: str) -> str:
    """稳定 cluster_key：基于 scope_id + 规范化查询字符串，不依赖 jieba 分词。"""
    return hashlib.sha256(f"{scope_id}:{normalized_query}".encode("utf-8")).hexdigest()


def _scope_from_group_set(gs: set[str]) -> str:
    """按最小权限原则从去重后的组集合判定 scope（company/group/admin/unknown）。"""
    if not gs:
        return SCOPE_UNKNOWN
    has_public = bool(gs & _PUBLIC_MARKERS)
    has_admin = bool(gs & _ADMIN_GROUPS)
    business = gs - _PUBLIC_MARKERS - _ADMIN_GROUPS
    if has_public and not has_admin and not business:
        return SCOPE_COMPANY
    if has_admin and not has_public and not business:
        return SCOPE_ADMIN
    if not has_public and not has_admin and len(business) == 1:
        return SCOPE_GROUP_PREFIX + next(iter(business))
    # 混合 public/group、admin/group、多业务组 → unknown（fail closed）。
    return SCOPE_UNKNOWN


def scope_id_from_acl(acl_json: str | None) -> str:
    """从 acl_scope JSON 解析规范 scope_id（冻结版本）。

    支持历史格式：
    - {"groups": ["__public__"]}            → company
    - {"groups": ["<组名>"]}                → group:<组名>
    - {"groups": ["__local_admin__"]}       → admin
    - ["engineering"]                       → 旧数组格式 → group:engineering
    - {"scope": "company"/"admin"}          → 显式（无 groups）
    - 混合 / 多组 / group 无 groups         → unknown（fail closed）
    """
    if not acl_json:
        return SCOPE_UNKNOWN
    try:
        data = json.loads(acl_json)
    except (TypeError, ValueError):
        return SCOPE_UNKNOWN

    if isinstance(data, list):
        gs = {str(x).strip() for x in data if x is not None and str(x).strip()}
        return _scope_from_group_set(gs)

    if not isinstance(data, dict):
        return SCOPE_UNKNOWN

    explicit = data.get("scope")
    groups_raw = data.get("groups")
    gs = (
        {str(x).strip() for x in groups_raw if x is not None and str(x).strip()}
        if isinstance(groups_raw, list)
        else set()
    )
    if gs:
        resolved = _scope_from_group_set(gs)
        # 显式 scope 与 groups 判定冲突 → unknown（fail closed）。
        # resolved 为 scope_id 字符串（company / admin / group:<名> / unknown）。
        if explicit == SCOPE_COMPANY and resolved != SCOPE_COMPANY:
            return SCOPE_UNKNOWN
        if explicit == SCOPE_ADMIN and resolved != SCOPE_ADMIN:
            return SCOPE_UNKNOWN
        if explicit == "group" and not resolved.startswith(SCOPE_GROUP_PREFIX):
            return SCOPE_UNKNOWN
        return resolved

    if explicit == SCOPE_COMPANY:
        return SCOPE_COMPANY
    if explicit == SCOPE_ADMIN:
        return SCOPE_ADMIN
    # group 无 groups / 其他 → unknown
    return SCOPE_UNKNOWN
