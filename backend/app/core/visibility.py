"""资源可见性谓词:notebook / wiki_space / wiki_page 的单一语义实现。

语义(2026-09-28 统一可见性):
  admin(has_permission(user, "*"), 含 __local_admin__ 桥接) → True
  visibility == 'public' → True
  visibility == 'dept'   → group_id IN user.groups
  visibility == 'self'   → owner_id == user.id
  visibility IS NULL(legacy 行) → group_id IS NULL OR group_id IN user.groups

页面可见性派生自所属资源(不新增页面列):
  notebook 页:所属笔记本可见(notebook_id IS NULL 视为公共)
  wiki 页:所属空间可见(space_id 为空 → 默认空间 = 公共) AND 页面自身 group 规则

同文件提供三种等价的判定形态,由测试锁定一致性:
  resource_visible / notebook_visible / wiki_space_visible / wiki_page_visible  (Python)
  notebook_visible_condition / wiki_space_visible_condition /
  wiki_page_visible_condition                                                    (SQLAlchemy)
  wiki_page_visible_sql                                                          (裸 SQL, 供 pgvector)
"""
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, or_, select, true
from sqlalchemy.sql.elements import ColumnElement

from app.core.security import has_permission
from app.models.database import Notebook, WikiPage, WikiSpace

VISIBILITIES = ("self", "dept", "public")
DEFAULT_VISIBILITY = "dept"


def parse_visibility(value: Optional[str], default: Optional[str] = DEFAULT_VISIBILITY) -> str:
    """归一请求里的 visibility:空串→default;非法值抛 ValueError 由端点转 400。"""
    v = (value or "").strip() or default or ""
    if v not in VISIBILITIES:
        raise ValueError("可见性仅支持 self/dept/public")
    return v


def user_groups(user) -> List[str]:
    groups = (user or {}).get("groups") if isinstance(user, dict) else None
    return list(groups) if isinstance(groups, list) else []


def user_id(user) -> Optional[str]:
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    return uid or None


def _attr(record, name: str):
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def resource_visible(user, owner_id: Optional[str], visibility: Optional[str], group_id: Optional[str]) -> bool:
    """通用规则:notebook 与 wiki_space 共用同一对 (visibility, group_id, owner_id) 判定。"""
    if has_permission(user or {}, "*"):
        return True
    if visibility == "public":
        return True
    if visibility == "self":
        return bool(owner_id) and owner_id == user_id(user)
    if visibility == "dept":
        return bool(group_id) and group_id in user_groups(user)
    # legacy(visibility 为空): 组为空 = 公共, 否则限本组
    return group_id is None or group_id in user_groups(user)


def notebook_visible(user, notebook) -> bool:
    """兼容 ORM 行与 dict(响应/测试夹具)。"""
    return resource_visible(
        user,
        _attr(notebook, "owner_id"),
        _attr(notebook, "visibility"),
        _attr(notebook, "group_id"),
    )


def wiki_space_visible(user, space) -> bool:
    return resource_visible(
        user,
        _attr(space, "owner_id"),
        _attr(space, "visibility"),
        _attr(space, "group_id"),
    )


def wiki_page_visible(user, page, space=None) -> bool:
    """页面自身 group 规则 AND 所属空间可见;space_id 为空视为默认空间(公共)。

    单资源判定需要调用方给出 space 行(space_id 非空却查不到时按不可见处理)。
    """
    if has_permission(user or {}, "*"):
        return True
    group_id = _attr(page, "group_id")
    if group_id is not None and group_id not in user_groups(user):
        return False
    space_id = _attr(page, "space_id")
    if not space_id:
        return True
    if space is None:
        return False
    return wiki_space_visible(user, space)


# ---------------- SQLAlchemy 条件(列表过滤) ----------------

def _legacy_visibility_condition(model) -> ColumnElement:
    """legacy 行:visibility 为 NULL/空串, 按旧 group 语义。"""
    return and_(
        or_(model.visibility.is_(None), model.visibility == ""),
        model.group_id.is_(None),
    )


def resource_visible_condition(user, model) -> ColumnElement:
    """notebook/wiki_space 通用 SQL 条件。"""
    if has_permission(user or {}, "*"):
        return true()
    groups = user_groups(user)
    uid = user_id(user)
    conds: List[ColumnElement] = [model.visibility == "public"]
    if groups:
        conds.append(and_(
            or_(model.visibility.is_(None), model.visibility == ""),
            or_(model.group_id.is_(None), model.group_id.in_(groups)),
        ))
        conds.append(and_(model.visibility == "dept", model.group_id.in_(groups)))
    else:
        conds.append(_legacy_visibility_condition(model))
    if uid:
        conds.append(and_(model.visibility == "self", model.owner_id == uid))
    return or_(*conds)


def notebook_visible_condition(user) -> ColumnElement:
    return resource_visible_condition(user, Notebook)


def wiki_space_visible_condition(user) -> ColumnElement:
    return resource_visible_condition(user, WikiSpace)


def wiki_page_visible_condition(user) -> ColumnElement:
    """wiki_pages 谓词:页面 group 规则 AND (空间为空 或 所属空间可见)。"""
    if has_permission(user or {}, "*"):
        return true()
    groups = user_groups(user)
    if groups:
        page_rule = or_(WikiPage.group_id.is_(None), WikiPage.group_id.in_(groups))
    else:
        page_rule = WikiPage.group_id.is_(None)
    space_rule = or_(
        WikiPage.space_id.is_(None),
        WikiPage.space_id == "",
        WikiPage.space_id.in_(select(WikiSpace.id).where(wiki_space_visible_condition(user))),
    )
    return and_(space_rule, page_rule)


# ---------------- 裸 SQL(pgvector 检索;列名不限定, 由调用方 FROM 决定) ----------------

def _resource_visible_sql(user, prefix: str) -> Tuple[str, Dict[str, Any]]:
    """resource_visible_condition 的裸 SQL 等价物;参数名加 prefix 防冲突。"""
    if has_permission(user or {}, "*"):
        return "1 = 1", {}
    groups = user_groups(user)
    uid = user_id(user)
    params: Dict[str, Any] = {}
    keys = []
    for i, g in enumerate(groups):
        key = f"{prefix}g{i}"
        params[key] = g
        keys.append(f":{key}")
    in_list = f"group_id IN ({','.join(keys)})" if keys else "1 = 0"
    parts = [
        "visibility = 'public'",
        f"((visibility IS NULL OR visibility = '') AND (group_id IS NULL OR {in_list}))",
        f"(visibility = 'dept' AND {in_list})",
    ]
    if uid:
        key = f"{prefix}u"
        params[key] = uid
        parts.append(f"(visibility = 'self' AND owner_id = :{key})")
    return "(" + " OR ".join(parts) + ")", params


def wiki_page_visible_sql(user) -> Tuple[str, Dict[str, Any]]:
    """wiki_page_visible_condition 的裸 SQL 等价物,供 pgvector 查询拼接。"""
    if has_permission(user or {}, "*"):
        return "1 = 1", {}
    groups = user_groups(user)
    params: Dict[str, Any] = {}
    keys = []
    for i, g in enumerate(groups):
        key = f"vg{i}"
        params[key] = g
        keys.append(f":{key}")
    if keys:
        page_rule = f"(group_id IS NULL OR group_id IN ({','.join(keys)}))"
    else:
        page_rule = "group_id IS NULL"
    space_rule, space_params = _resource_visible_sql(user, prefix="sg")
    params.update(space_params)
    cond = (
        f"{page_rule} AND "
        f"((space_id IS NULL OR space_id = '') OR space_id IN "
        f"(SELECT id FROM wiki_spaces WHERE {space_rule}))"
    )
    return cond, params
