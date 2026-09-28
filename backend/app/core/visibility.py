"""资源可见性谓词:notebook / wiki_space / wiki_page 的单一语义实现。

语义(2026-09-28 统一可见性 + 资源级追加授权):
  admin(has_permission(user, "*"), 含 __local_admin__ 桥接) → True
  ACL 命中(用户∈resource_acl.user 或 用户组∈resource_acl.group) → True
  visibility == 'public' → True
  visibility == 'dept'   → group_id IN user.groups
  visibility == 'self'   → owner_id == user.id
  visibility IS NULL(legacy 行) → group_id IS NULL OR group_id IN user.groups

ACL 是追加式:在三级可见性之外额外授权,不缩小原可见范围;管理员仍全见。
页面可见性派生自所属资源(不新增页面列):
  notebook 页:所属笔记本可见(notebook_id IS NULL 视为公共)
  wiki 页:所属空间可见(space_id 为空 → 默认空间 = 公共) AND 页面自身 group 规则

同文件提供三种等价的判定形态,由测试锁定一致性:
  resource_visible / notebook_visible / wiki_space_visible / wiki_page_visible  (Python)
  notebook_visible_condition / wiki_space_visible_condition /
  wiki_page_visible_condition                                                    (SQLAlchemy)
  wiki_page_visible_sql                                                          (裸 SQL, 供 pgvector)
"""
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import and_, or_, select, true
from sqlalchemy.sql.elements import ColumnElement

from app.core.security import has_permission
from app.models.database import Notebook, ResourceAcl, WikiPage, WikiSpace

VISIBILITIES = ("self", "dept", "public")
DEFAULT_VISIBILITY = "dept"

# resource_acl.resource_type 取值
ACL_NOTEBOOK = "notebook"
ACL_WIKI_SPACE = "wiki_space"

AclSet = Set[Tuple[str, str]]


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


def acl_matches(user, acl: Optional[AclSet]) -> bool:
    """ACL 命中:任一 (subject_type, subject_id) 与当前用户匹配即 True。"""
    if not acl:
        return False
    uid = user_id(user)
    groups = set(user_groups(user))
    for subject_type, subject_id in acl:
        if subject_type == "user":
            if uid and subject_id == uid:
                return True
        elif subject_type == "group" and subject_id in groups:
            return True
    return False


def resource_visible(user, owner_id: Optional[str], visibility: Optional[str], group_id: Optional[str],
                     acl: Optional[AclSet] = None) -> bool:
    """通用规则:notebook 与 wiki_space 共用同一对 (visibility, group_id, owner_id, acl) 判定。"""
    if has_permission(user or {}, "*"):
        return True
    if acl_matches(user, acl):
        return True
    if visibility == "public":
        return True
    if visibility == "self":
        return bool(owner_id) and owner_id == user_id(user)
    if visibility == "dept":
        return bool(group_id) and group_id in user_groups(user)
    # legacy(visibility 为空): 组为空 = 公共, 否则限本组
    return group_id is None or group_id in user_groups(user)


def notebook_visible(user, notebook, acl: Optional[AclSet] = None) -> bool:
    """兼容 ORM 行与 dict(响应/测试夹具);acl 为空时行为与旧签名一致。"""
    return resource_visible(
        user,
        _attr(notebook, "owner_id"),
        _attr(notebook, "visibility"),
        _attr(notebook, "group_id"),
        acl=acl,
    )


def wiki_space_visible(user, space, acl: Optional[AclSet] = None) -> bool:
    return resource_visible(
        user,
        _attr(space, "owner_id"),
        _attr(space, "visibility"),
        _attr(space, "group_id"),
        acl=acl,
    )


def wiki_page_visible(user, page, space=None, space_acl: Optional[AclSet] = None) -> bool:
    """页面自身 group 规则 AND 所属空间可见;space_id 为空视为默认空间(公共)。

    单资源判定需要调用方给出 space 行(space_id 非空却查不到时按不可见处理);
    space_acl 为所属空间的追加授权集合。
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
    return wiki_space_visible(user, space, acl=space_acl)


# ---------------- ACL 读写(资源级追加授权) ----------------

def load_acl(db, resource_type: str, resource_id: Optional[str]) -> AclSet:
    """读取资源的追加授权集合 {(subject_type, subject_id)};1 次小查询。"""
    if not resource_id:
        return set()
    rows = db.query(ResourceAcl.subject_type, ResourceAcl.subject_id).filter(
        ResourceAcl.resource_type == resource_type,
        ResourceAcl.resource_id == resource_id,
    ).all()
    return {(r[0], r[1]) for r in rows}


def load_acl_lists(db, resource_type: str, resource_id: Optional[str]) -> Tuple[List[str], List[str]]:
    """读取资源 ACL 的两个列表 (acl_users, acl_groups),供响应预填。"""
    acl = load_acl(db, resource_type, resource_id)
    return (
        sorted(sid for st, sid in acl if st == "user"),
        sorted(sid for st, sid in acl if st == "group"),
    )


def delete_acl(db, resource_type: str, resource_id: Optional[str]) -> None:
    """删除资源的全部 ACL 行(资源删除时清理);调用方负责 commit。"""
    if not resource_id:
        return
    db.query(ResourceAcl).filter(
        ResourceAcl.resource_type == resource_type,
        ResourceAcl.resource_id == resource_id,
    ).delete(synchronize_session=False)


def replace_acl(db, resource_type: str, resource_id: str,
                users: Optional[List[str]] = None, groups: Optional[List[str]] = None) -> None:
    """替换式写入资源 ACL:DELETE 后 INSERT,去重、剔除空值;调用方负责 commit。"""
    delete_acl(db, resource_type, resource_id)
    seen: AclSet = set()
    for subject_type, values in (("user", users or []), ("group", groups or [])):
        for raw in values:
            sid = (raw or "").strip() if isinstance(raw, str) else ""
            if not sid or (subject_type, sid) in seen:
                continue
            seen.add((subject_type, sid))
            db.add(ResourceAcl(
                id=str(uuid.uuid4()),
                resource_type=resource_type,
                resource_id=resource_id,
                subject_type=subject_type,
                subject_id=sid,
            ))


def resource_visible_db(db, user, resource_type: str, resource) -> bool:
    """单资源判定便捷函数:带 1 次 ACL 小查询;resource 为 ORM 行或 dict。"""
    if resource is None:
        return False
    if has_permission(user or {}, "*"):
        return True
    acl = load_acl(db, resource_type, _attr(resource, "id"))
    return resource_visible(
        user,
        _attr(resource, "owner_id"),
        _attr(resource, "visibility"),
        _attr(resource, "group_id"),
        acl=acl,
    )


# ---------------- SQLAlchemy 条件(列表过滤) ----------------

def _legacy_visibility_condition(model) -> ColumnElement:
    """legacy 行:visibility 为 NULL/空串, 按旧 group 语义。"""
    return and_(
        or_(model.visibility.is_(None), model.visibility == ""),
        model.group_id.is_(None),
    )


def _acl_condition(user, model, resource_type: str) -> Optional[ColumnElement]:
    """ACL 追加子句:model.id ∈ 命中当前用户的 resource_acl.resource_id。"""
    uid = user_id(user)
    groups = user_groups(user)
    subject_conds: List[ColumnElement] = []
    if uid:
        subject_conds.append(and_(
            ResourceAcl.subject_type == "user",
            ResourceAcl.subject_id == uid,
        ))
    if groups:
        subject_conds.append(and_(
            ResourceAcl.subject_type == "group",
            ResourceAcl.subject_id.in_(groups),
        ))
    if not subject_conds:
        return None
    return model.id.in_(
        select(ResourceAcl.resource_id).where(
            ResourceAcl.resource_type == resource_type,
            or_(*subject_conds),
        )
    )


def resource_visible_condition(user, model, resource_type: Optional[str] = None) -> ColumnElement:
    """notebook/wiki_space 通用 SQL 条件;resource_type 提供时追加 ACL 子句。"""
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
    if resource_type:
        acl_cond = _acl_condition(user, model, resource_type)
        if acl_cond is not None:
            conds.append(acl_cond)
    return or_(*conds)


def notebook_visible_condition(user) -> ColumnElement:
    return resource_visible_condition(user, Notebook, resource_type=ACL_NOTEBOOK)


def wiki_space_visible_condition(user) -> ColumnElement:
    return resource_visible_condition(user, WikiSpace, resource_type=ACL_WIKI_SPACE)


def wiki_page_visible_condition(user) -> ColumnElement:
    """wiki_pages 谓词:页面 group 规则 AND (空间为空 或 所属空间可见(含空间 ACL))。"""
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

def _resource_acl_sql(user, prefix: str, resource_type: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """resource_visible_condition 里 ACL 子句的裸 SQL 等价物;参数名加 prefix 防冲突。

    生成 `id IN (SELECT resource_id FROM resource_acl WHERE ...)`:调用方 FROM 的表
    主键为 id(notebooks/wiki_spaces),内层 resource_id 属于 resource_acl。
    """
    uid = user_id(user)
    groups = user_groups(user)
    params: Dict[str, Any] = {}
    subject_parts: List[str] = []
    if uid:
        key = f"{prefix}au"
        params[key] = uid
        subject_parts.append(f"(subject_type = 'user' AND subject_id = :{key})")
    if groups:
        keys = []
        for i, g in enumerate(groups):
            key = f"{prefix}ag{i}"
            params[key] = g
            keys.append(f":{key}")
        subject_parts.append(f"(subject_type = 'group' AND subject_id IN ({','.join(keys)}))")
    if not subject_parts:
        return None, {}
    cond = (
        f"id IN (SELECT resource_id FROM resource_acl "
        f"WHERE resource_type = '{resource_type}' AND ({' OR '.join(subject_parts)}))"
    )
    return cond, params


def _resource_visible_sql(user, prefix: str, resource_type: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
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
    if resource_type:
        acl_cond, acl_params = _resource_acl_sql(user, prefix, resource_type)
        if acl_cond:
            params.update(acl_params)
            parts.append(acl_cond)
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
    space_rule, space_params = _resource_visible_sql(user, prefix="sg", resource_type=ACL_WIKI_SPACE)
    params.update(space_params)
    cond = (
        f"{page_rule} AND "
        f"((space_id IS NULL OR space_id = '') OR space_id IN "
        f"(SELECT id FROM wiki_spaces WHERE {space_rule}))"
    )
    return cond, params
