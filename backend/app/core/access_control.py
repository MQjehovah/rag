"""统一权限服务（V4 Phase B）。

职责：集中实现 RBAC 角色判定与 ACL 范围判定，供所有 API 复用。

模型（V4 8.1）：
- RBAC 角色决定「用户能做什么」：user / wiki_editor / admin。
- ACL 范围决定「用户能看到什么」：company / group / admin。
- 不使用负责人（owner）概念，不让 LLM 参与权限判断。

权限继承（V4 8.3）：
    SourceConnection → Notebook → Page → Chunk/Evidence → Wiki
        → Entity/Relation → Community → Chat/Search

设计要点：
- 复用现有字段，不新增数据库迁移：
    * Notebook.group_id           → 权限范围（None=company，具体组名=group，
      __local_admin__=admin）。
    * WikiPage.acl_scope          → JSON {"groups": [...]}，Wiki 的直接权限载体。
    * KnowledgeCommunity.acl_scope → JSON {"groups": [...]}，Community 的直接权限载体。
- 管理员判定不再只依赖 __local_admin__：保留其为本地试点管理员，同时支持
  settings.ldap_group_map_admin（正式 LDAP/SSO 管理员组）。
- fail closed：范围缺失 / JSON 无法解析 / 对象归属无法确定时，非管理员一律拒绝。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.database import (
    Notebook,
    NotebookGroup,
    Page,
    SourceConnection,
    SourceItem,
    WikiPage,
)

logger = logging.getLogger(__name__)

# ---- 角色常量 ----
ROLE_USER = "user"
ROLE_WIKI_EDITOR = "wiki_editor"
ROLE_ADMIN = "admin"

# ---- 范围常量 ----
SCOPE_COMPANY = "company"   # 所有已登录用户可见
SCOPE_GROUP = "group"       # 仅匹配组可见
SCOPE_ADMIN = "admin"       # 仅管理员可见
SCOPE_UNKNOWN = "unknown"   # 无法确定归属 → fail closed

# 本地试点管理员组名（V4 8.2：仅允许本地试点，不作为正式生产权限组）。
_LOCAL_ADMIN_GROUP = "__local_admin__"

# 公开标记：acl_scope 的 groups 中出现该值时视为 company 范围。
_PUBLIC_MARKERS = {"__public__"}


@dataclass(frozen=True)
class AccessScope:
    """解析后的权限范围。

    kind: company / group / admin / unknown。
    groups: kind == group 时的可见组集合。
    """

    kind: str
    groups: frozenset[str] = frozenset()


def _configured_admin_groups() -> set[str]:
    """正式管理员组 = 本地试点组 + 配置的 LDAP/SSO 管理员组。

    settings.ldap_group_map_admin 支持逗号分隔多个组名。
    """
    raw = (settings.ldap_group_map_admin or "").strip()
    names = {g.strip() for g in raw.split(",") if g.strip()}
    names.add(_LOCAL_ADMIN_GROUP)
    return names


def _configured_editor_groups() -> set[str]:
    """wiki_editor 角色对应的组名集合（逗号分隔）。"""
    raw = (settings.ldap_group_map_wiki_editor or "").strip()
    return {g.strip() for g in raw.split(",") if g.strip()}


# ---------------------------------------------------------------------------
# 角色判定（RBAC）
# ---------------------------------------------------------------------------

def is_admin(current_user: dict) -> bool:
    """是否为管理员。仅依赖 groups，避免与 get_current_user 的预计算字段互相递归。"""
    if not current_user:
        return False
    groups = set(current_user.get("groups") or [])
    return bool(groups & _configured_admin_groups())


def is_wiki_editor(current_user: dict) -> bool:
    """是否为 wiki_editor 角色（不含 admin；admin 是更高角色，单独判断）。"""
    if not current_user:
        return False
    groups = set(current_user.get("groups") or [])
    return bool(groups & _configured_editor_groups())


def role_groups() -> set[str]:
    """返回所有角色组（admin + editor），供 scope 计算排除，避免误当业务知识组。"""
    return _configured_admin_groups() | _configured_editor_groups()


def business_groups(current_user: dict) -> set[str]:
    """用户所属的业务知识组（排除 admin/editor 角色组）。

    LDAP admin/editor 角色组只决定「能做什么」，不构成独立的知识权限域；
    不能被误当成普通业务知识组。
    """
    if not current_user:
        return set()
    groups = set(current_user.get("groups") or [])
    role = role_groups()
    return {g for g in groups if g not in role}


def user_roles(current_user: dict) -> set[str]:
    """返回当前用户的角色集合（至少包含 user）。"""
    roles = {ROLE_USER}
    if is_admin(current_user):
        roles.add(ROLE_ADMIN)
    if is_wiki_editor(current_user):
        roles.add(ROLE_WIKI_EDITOR)
    return roles


# ---------------------------------------------------------------------------
# 范围解析与匹配（ACL）
# ---------------------------------------------------------------------------

def scope_from_group_id(group_id: str | None) -> AccessScope:
    """从 Notebook.group_id 解析范围。

    None / __public__ → company；__local_admin__ 或配置的管理员组 → admin；
    其他非空组名 → group。
    """
    if not group_id:
        return AccessScope(SCOPE_COMPANY)
    g = str(group_id).strip()
    if g in _PUBLIC_MARKERS:
        return AccessScope(SCOPE_COMPANY)
    if g in _configured_admin_groups():
        return AccessScope(SCOPE_ADMIN)
    return AccessScope(SCOPE_GROUP, frozenset({g}))


def notebook_access_groups(db: Session, notebook: Notebook) -> set[str]:
    """Notebook 的完整可访问组集合（J-1 多组授权）。

    = Notebook.group_id（主权限组，含 __public__/__local_admin__ 等标记）
      ∪ notebook_groups 表额外授权的业务组。

    注意：本函数只返回原始组名集合（含 role/管理组标记）；真正判定可见性
    应调用 scope_from_group_set / _scope_matches，由调用方决定 company/admin
    语义（不把管理员组混成业务知识组）。
    """
    groups = set()
    if notebook is not None:
        if notebook.group_id and str(notebook.group_id).strip():
            groups.add(str(notebook.group_id).strip())
        if notebook.id:
            for row in db.query(NotebookGroup.group_name).filter(
                NotebookGroup.notebook_id == notebook.id
            ).all():
                name = str(row[0] or "").strip()
                if name:
                    groups.add(name)
    return groups


def scope_from_notebook(db: Session, notebook: Notebook) -> AccessScope:
    """从 Notebook 的完整组集合解析范围（J-1）。

    - 集合为空 → company（沿用旧语义：group_id IS NULL = company）。
    - 仅 __public__ → company。
    - 包含管理员组 → admin（管理员专属 Notebook）。
    - 一个或多个业务组 → group（所有组成员可见）。
    - 混合 __public__ 与业务组 → unknown（fail closed，管理员可见全部）。
    """
    if notebook is None:
        return AccessScope(SCOPE_UNKNOWN)
    gs = notebook_access_groups(db, notebook)
    if not gs:
        return AccessScope(SCOPE_COMPANY)
    if gs <= _PUBLIC_MARKERS:
        return AccessScope(SCOPE_COMPANY)
    if gs & _configured_admin_groups():
        # 管理员组（含 __local_admin__）出现即视为 admin-only；混合业务组时
        # 也按 admin 处理（管理员可见全部，不会泄露给普通用户）。
        return AccessScope(SCOPE_ADMIN)
    business = gs - _PUBLIC_MARKERS - _configured_admin_groups()
    has_public = bool(gs & _PUBLIC_MARKERS)
    if not business:
        return AccessScope(SCOPE_UNKNOWN)
    if has_public:
        # 混合 __public__ 与业务组 → unknown（fail closed，与 acl_scope 解析一致）。
        logger.warning(
            "mixed_notebook_scope_rejected",
            extra={"notebook_id": getattr(notebook, "id", ""), "groups": sorted(gs)},
        )
        return AccessScope(SCOPE_UNKNOWN)
    return AccessScope(SCOPE_GROUP, frozenset(business))


def _scope_from_group_set(gs: set[str], raw: str = "") -> AccessScope:
    """按最小权限原则从去重后的组集合判定范围。

    - 仅 __public__ → company
    - 仅单个管理员组 → admin
    - 仅业务组（一个或多个）→ group（J-1 多组授权：任一组成员的用户即可见）
    - 其余（混合 public/group、admin/group）→ unknown（非管理员 fail closed）
    """
    if not gs:
        return AccessScope(SCOPE_UNKNOWN)
    has_public = bool(gs & _PUBLIC_MARKERS)
    has_admin = bool(gs & _configured_admin_groups())
    business = gs - _PUBLIC_MARKERS - _configured_admin_groups()

    if has_public and not has_admin and not business:
        return AccessScope(SCOPE_COMPANY)
    if has_admin and not has_public and not business:
        return AccessScope(SCOPE_ADMIN)
    if not has_public and not has_admin and business:
        return AccessScope(SCOPE_GROUP, frozenset(business))
    # 混合 public/group、admin/group → 最小权限：非管理员 fail closed，记录结构化警告。
    logger.warning(
        "mixed_acl_scope_rejected",
        extra={"reason": "mixed groups", "raw_scope": (raw or "")[:200]},
    )
    return AccessScope(SCOPE_UNKNOWN)


def scope_from_acl(acl_json: str | None) -> AccessScope:
    """从 acl_scope JSON 解析范围（Wiki / Community 的直接权限载体）。

    遵循最小权限原则，支持历史格式：
    - {"groups": ["__public__"]}                 → company
    - {"groups": ["<组名>"]}                     → group
    - {"groups": ["<管理员组>"]}                 → admin
    - ["engineering"]                            → 旧数组格式 → group
    - {"scope": "company"} / {"scope": "admin"}  → 显式（无 groups）
    - {"scope": "group"} 无 groups               → unknown（缺失 group）
    - 混合 public/group、admin/group、多业务组    → unknown（非管理员 fail closed）
    - 显式 scope 与 groups 冲突                  → unknown（fail closed）

    缺失 / 无法解析 → unknown（fail closed）。
    """
    if not acl_json:
        return AccessScope(SCOPE_UNKNOWN)
    try:
        data = json.loads(acl_json)
    except (TypeError, ValueError):
        return AccessScope(SCOPE_UNKNOWN)

    # 旧格式：JSON 数组，如 ["engineering"]
    if isinstance(data, list):
        gs = {str(x).strip() for x in data if x is not None and str(x).strip()}
        return _scope_from_group_set(gs, acl_json)

    if not isinstance(data, dict):
        return AccessScope(SCOPE_UNKNOWN)

    explicit = data.get("scope")
    groups_raw = data.get("groups")
    gs = (
        {str(x).strip() for x in groups_raw if x is not None and str(x).strip()}
        if isinstance(groups_raw, list)
        else set()
    )

    # 有 groups：按最小权限解析，再校验显式 scope 是否与 groups 冲突。
    if gs:
        resolved = _scope_from_group_set(gs, acl_json)
        if explicit in (SCOPE_COMPANY, SCOPE_GROUP, SCOPE_ADMIN) and explicit != resolved.kind:
            logger.warning(
                "mixed_acl_scope_rejected",
                extra={"reason": f"scope={explicit} conflicts with groups", "raw_scope": acl_json[:200]},
            )
            return AccessScope(SCOPE_UNKNOWN)
        return resolved

    # 无 groups：仅接受 company / admin 显式；group 缺失组 → unknown。
    if explicit == SCOPE_COMPANY:
        return AccessScope(SCOPE_COMPANY)
    if explicit == SCOPE_ADMIN:
        return AccessScope(SCOPE_ADMIN)
    if explicit == SCOPE_GROUP:
        logger.warning(
            "mixed_acl_scope_rejected",
            extra={"reason": "scope=group without groups", "raw_scope": acl_json[:200]},
        )
    return AccessScope(SCOPE_UNKNOWN)


def _scope_matches(scope: AccessScope, current_user: dict) -> bool:
    """范围是否对当前用户可见。异常一律 fail closed。"""
    if not current_user:
        return False
    if is_admin(current_user):
        return True  # 管理员可见全部
    if scope.kind == SCOPE_COMPANY:
        return True  # 所有登录用户可见
    if scope.kind == SCOPE_ADMIN:
        return False  # 非管理员不可见
    if scope.kind == SCOPE_GROUP:
        groups = set(current_user.get("groups") or [])
        return bool(groups & scope.groups)
    return False  # unknown → fail closed


# ---------------------------------------------------------------------------
# Phase 3：scope_id 规范化 / workspace key 派生 / ACL 完全等价
# ---------------------------------------------------------------------------

def normalize_scope_id(scope: AccessScope) -> str:
    """规范化权限域（Phase 3 路由）：company / admin / group:<逗号分隔的排序组名>。

    - 多业务组（J-1）也支持，排序稳定；
    - unknown → "unknown"（fail closed 由调用方依据 scope.kind 处理）。
    """
    if scope.kind == SCOPE_COMPANY:
        return SCOPE_COMPANY
    if scope.kind == SCOPE_ADMIN:
        return SCOPE_ADMIN
    if scope.kind == SCOPE_GROUP:
        return f"{SCOPE_GROUP}:{','.join(sorted(scope.groups))}"
    return SCOPE_UNKNOWN


def workspace_key_for_notebook(notebook_id: str) -> str:
    """Notebook 的确定性 workspace key（Phase 3.1 自动路由用）。

    按 notebook_id 确定性派生：`ws_nb_<sha256("wiki-workspace:nb:"+notebook_id)[:24]>`。
    同 notebook 幂等；不同 notebook 不同 key（同 ACL 不同 notebook 各自默认私用空间）。
    与 ACL scope 无关 —— scope 只是权限/审计字段，不参与默认 workspace key。
    """
    digest = hashlib.sha256(f"wiki-workspace:nb:{notebook_id}".encode("utf-8")).hexdigest()
    return f"ws_nb_{digest[:24]}"


def generate_manual_workspace_key() -> str:
    """手工创建 workspace 时的系统唯一 key：`ws_<uuid4().hex[:12]>`。

    仅在调用方未提供业务 key 时使用；保证不与 ACL/notebook 自动 key 语义耦合。
    """
    return "ws_" + uuid.uuid4().hex[:12]


def acl_json_for_scope(scope: AccessScope) -> str:
    """规范化 acl_scope JSON（与 wiki_page_builder._scope_to_acl_json 输出语义一致）。"""
    if scope.kind == SCOPE_COMPANY:
        return '{"groups": ["__public__"]}'
    if scope.kind == SCOPE_ADMIN:
        return '{"groups": ["__local_admin__"]}'
    return json.dumps({"groups": sorted(scope.groups)}, ensure_ascii=False)


def acl_scope_equivalent(a: str | None, b: str | None) -> bool:
    """两条规范化 acl_scope JSON 是否完全等价。

    对 JSON 做 json.loads 后比较排序 group 集合；company/admin 特殊值等价规则与
    _scope_matches 一致（company=__public__ 公开，admin=管理员组）。任一方无法解析
    或解析为 unknown（fail closed）→ False。
    """
    if not a or not b:
        return False
    sa = scope_from_acl(a)
    sb = scope_from_acl(b)
    if sa.kind == SCOPE_UNKNOWN or sb.kind == SCOPE_UNKNOWN:
        return False
    if sa.kind != sb.kind:
        return False
    return set(sa.groups) == set(sb.groups)


# ---------------------------------------------------------------------------
# 对象级判断
# ---------------------------------------------------------------------------

def can_view_notebook(db: Session, current_user: dict, notebook: Notebook) -> bool:
    """是否可见某个 Notebook（基于其 group_id + notebook_groups 多组授权）。"""
    if notebook is None:
        return False
    return _scope_matches(scope_from_notebook(db, notebook), current_user)


def can_view_page(db: Session, current_user: dict, page: Page) -> bool:
    """是否可见某个 Page。基于其 Notebook 的多组授权，缺归属 fail closed。

    J-1 最终封板：失效远程 Page（无同 Connector active SourceItem，含
    NEEDS_REASSIGN / FOLDER_NOT_MAPPED）对所有用户（含管理员）一律拒绝，
    避免通过正常检索链访问已移动/失效文件；管理员经数据源诊断入口查看。
    """
    if page is None:
        return False
    if page.id not in _filter_eligible_pages(db, {page.id}):
        return False
    if is_admin(current_user):
        return True
    if not page.notebook_id:
        # 无归属页面无法确定权限域 → 非管理员不可见（fail closed）。
        return False
    notebook = db.get(Notebook, page.notebook_id)
    if notebook is None:
        return False
    return _scope_matches(scope_from_notebook(db, notebook), current_user)


def can_view_wiki(db: Session, current_user: dict, wiki_page: WikiPage) -> bool:
    """是否可见某个 Wiki 主题页。直接基于其 acl_scope（不经过 Card 反推）。"""
    if wiki_page is None:
        return False
    return _scope_matches(scope_from_acl(wiki_page.acl_scope), current_user)


def can_edit_wiki(db: Session, current_user: dict, wiki_page: WikiPage) -> bool:
    """是否可编辑某个 Wiki。先满足可见，再要求 wiki_editor 或 admin。"""
    if not can_view_wiki(db, current_user, wiki_page):
        return False
    return is_admin(current_user) or is_wiki_editor(current_user)


def can_manage_sources(current_user: dict) -> bool:
    """是否可管理数据源（仅管理员）。"""
    return is_admin(current_user)


def can_manage_pages(current_user: dict) -> bool:
    """是否可管理原始 Page（创建/导入/修改/删除/索引），仅管理员。

    V4 角色模型：普通用户只能查看；wiki_editor 只能编辑 Wiki；原始资料
    的增删改与重建索引只有 admin 能做。
    """
    return is_admin(current_user)


def role_abilities(groups: list[str]) -> dict:
    """返回登录/me 接口所需的角色能力字段。

    结构：
        {"is_admin": bool, "is_wiki_editor": bool, "roles": ["user", ...]}
    roles 固定按 user → wiki_editor → admin 顺序，便于前端判断。
    """
    user = {"groups": groups or []}
    roles = user_roles(user)
    ordered = [r for r in (ROLE_USER, ROLE_WIKI_EDITOR, ROLE_ADMIN) if r in roles]
    return {
        "is_admin": is_admin(user),
        "is_wiki_editor": is_wiki_editor(user),
        "roles": ordered,
    }


# ---------------------------------------------------------------------------
# 集合查询（过滤发生在返回 / Rerank / LLM 上下文之前）
# ---------------------------------------------------------------------------

def eligible_remote_page_ids(
    db: Session,
    page_ids: set[str],
    page_source_types: dict[str, str],
) -> set[str]:
    """返回「存在与 Page.source_type 相同 Connector 的 active SourceItem」的 page_id。

    J-1 最终返工：统一远程 Page 有效性（所有入口复用）。
    - 手工 Page（source_type 为空）不要求 SourceItem → 由调用方放行；
    - 远程 Page 必须存在同 Connector 的 active SourceItem；NULL/deleted/error/
      skipped（含 NEEDS_REASSIGN / FOLDER_NOT_MAPPED）均算失效；
    - 错误 Connector 的 active SourceItem 不得使 Page 有效。
    用稳定状态（state == "active"）而非 last_error 字符串判断。
    """
    if not page_ids:
        return set()
    rows = (
        db.query(SourceItem.page_id, SourceConnection.connector_key)
        .join(SourceConnection, SourceConnection.id == SourceItem.connection_id)
        .filter(
            SourceItem.page_id.in_(page_ids),
            SourceItem.state == "active",
        )
        .all()
    )
    active: set[str] = set()
    for page_id, connector_key in rows:
        if page_id and page_source_types.get(page_id) == connector_key:
            active.add(page_id)
    return active


def get_visible_page_ids(db: Session, current_user: dict) -> set[str]:
    """当前用户可见的 Page id 集合。

    管理员可见全部；普通用户可见以下 Notebook 下的页面：
    - group_id IS NULL（company 范围）
    - group_id == "__public__"（company 范围）
    - group_id ∈ 用户所属组（group 范围）

    额外排除（J-1 最终返工）：
    - 待重新归属（NEEDS_REASSIGN）的旧 Page；
    - 无同 Connector active SourceItem 的失效远程 Page。

    scope 约束（V4 Phase F）：current_user 带 `_scope_override` 时，只返回该精确
    scope 的 Page（company/group:<名>/admin 严格隔离），用于选定权限域后的检索。
    """
    scope_override = current_user.get("_scope_override") if isinstance(current_user, dict) else None
    if scope_override:
        return get_scoped_page_ids(db, scope_override)
    if is_admin(current_user):
        # J-1 最终封板：管理员在普通检索链中同样排除失效远程 Page（无同 Connector
        # active SourceItem 的 skipped/NEEDS_REASSIGN），避免 Chat/Search/图谱使用
        # 失效内容；失效记录只经数据源诊断/同步错误/待重新归属入口查看。
        all_ids = {row[0] for row in db.query(Page.id).all()}
        return _filter_eligible_pages(db, all_ids)
    # J-1 遗留修复：只用业务组匹配，排除 admin/wiki_editor 角色组被当成知识组。
    groups = list(business_groups(current_user))
    # 无业务组用户仍可访问 company（group_id IS NULL）与 __public__ Notebook；
    # 不得因 groups=[] 提前返回空集合（J-1 遗留修复）。
    visible_nb_ids = [
        row[0]
        for row in db.query(Notebook.id).filter(
            or_(
                Notebook.group_id.is_(None),
                Notebook.group_id == "__public__",
                Notebook.group_id.in_(groups),
                Notebook.id.in_(
                    db.query(NotebookGroup.notebook_id)
                    .filter(NotebookGroup.group_name.in_(groups))
                ),
            )
        ).all()
    ]
    if not visible_nb_ids:
        return set()
    visible_page_ids = {
        row[0]
        for row in db.query(Page.id).filter(Page.notebook_id.in_(visible_nb_ids)).all()
    }
    # 统一远程 Page 有效性：无同 Connector active SourceItem 的远程 Page
    # （含 NEEDS_REASSIGN / FOLDER_NOT_MAPPED 等 skipped/失效状态）从普通用户
    # 可见链路排除；手工 Page（source_type 空）不要求 SourceItem。
    return _filter_eligible_pages(db, visible_page_ids)


def _filter_eligible_pages(db: Session, page_ids: set[str]) -> set[str]:
    """保留可见 Page 中有效者：手工 Page 放行，远程 Page 必须有 active SourceItem。"""
    if not page_ids:
        return set()
    rows = db.query(Page.id, Page.source_type).filter(Page.id.in_(page_ids)).all()
    local_ids = {pid for pid, st in rows if not st}
    remote_ids = {pid for pid, st in rows if st}
    if not remote_ids:
        return local_ids | remote_ids
    source_types = {pid: st for pid, st in rows}
    eligible_remote = eligible_remote_page_ids(db, remote_ids, source_types)
    return local_ids | eligible_remote


def get_visible_wiki_page_ids(db: Session, current_user: dict) -> set[str]:
    """当前用户可见的 WikiPage id 集合，直接基于 acl_scope。

    J-1 最终返工：archived（不可发布，来源已失效/权限已变更）的 Wiki 对普通用户
    立即不可见，不等待异步重建。
    """
    scope_override = current_user.get("_scope_override") if isinstance(current_user, dict) else None
    if scope_override:
        return get_scoped_wiki_page_ids(db, scope_override)
    if is_admin(current_user):
        return {row[0] for row in db.query(WikiPage.id).all()}
    rows = db.query(WikiPage.id, WikiPage.acl_scope, WikiPage.status).all()
    return {
        pid for pid, acl, status in rows
        if status != "archived"
        and _scope_matches(scope_from_acl(acl), current_user)
    }


def _scope_id_eq(scope_id: str, scope: AccessScope) -> bool:
    """精确 scope 相等（V4 Phase F）：company/group:<名>/admin 严格匹配。"""
    if scope_id == SCOPE_COMPANY:
        return scope.kind == SCOPE_COMPANY
    if scope_id == SCOPE_ADMIN:
        return scope.kind == SCOPE_ADMIN
    if scope_id.startswith("group:"):
        names = set(scope_id[len("group:"):].split(","))
        return scope.kind == SCOPE_GROUP and set(scope.groups) == names
    return False


def _notebook_ids_for_scope(db: Session, scope_id: str) -> list[str]:
    """精确 scope 对应的 Notebook id 列表（J-1 支持 notebook_groups 多组授权）。"""
    if scope_id == SCOPE_COMPANY:
        return [
            row[0] for row in db.query(Notebook.id).filter(
                or_(Notebook.group_id.is_(None), Notebook.group_id == "__public__")
            ).all()
        ]
    if scope_id == SCOPE_ADMIN:
        return [
            row[0] for row in db.query(Notebook.id).filter(
                or_(
                    Notebook.group_id.in_(_configured_admin_groups()),
                    Notebook.id.in_(
                        db.query(NotebookGroup.notebook_id).filter(
                            NotebookGroup.group_name.in_(_configured_admin_groups())
                        )
                    ),
                )
            ).all()
        ]
    if scope_id.startswith("group:"):
        names = scope_id[len("group:"):].split(",")
        return [
            row[0] for row in db.query(Notebook.id).filter(
                or_(
                    Notebook.group_id.in_(names),
                    Notebook.id.in_(
                        db.query(NotebookGroup.notebook_id).filter(
                            NotebookGroup.group_name.in_(names)
                        )
                    ),
                )
            ).all()
        ]
    return []


def get_scoped_page_ids(db: Session, scope_id: str) -> set[str]:
    """精确 scope 的 Page id 集合（scope 过滤先于任何 limit）。"""
    nb_ids = _notebook_ids_for_scope(db, scope_id)
    if not nb_ids:
        return set()
    scoped = {row[0] for row in db.query(Page.id).filter(Page.notebook_id.in_(nb_ids)).all()}
    # 统一远程 Page 有效性：失效远程 Page 同样排除（即使走 _scope_override 也不能绕过）。
    return _filter_eligible_pages(db, scoped)


def get_scoped_wiki_page_ids(db: Session, scope_id: str) -> set[str]:
    """精确 scope 的 WikiPage id 集合（排除 archived，来源失效立即不可见）。"""
    rows = db.query(WikiPage.id, WikiPage.acl_scope, WikiPage.status).all()
    return {
        pid for pid, acl, status in rows
        if status != "archived"
        and _scope_id_eq(scope_id, scope_from_acl(acl))
    }
