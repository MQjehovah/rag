"""Phase 3.1：WikiWorkspace 业务逻辑（CRUD / 绑定 / 可见性）。

- 创建：key 由调用方可选提供（payload.key，独立业务 key）或系统生成
  （generate_manual_workspace_key）。同 ACL 可创建多个 workspace（scope_id/acl_scope
  只是权限/审计字段，不参与 key）；payload 提供 key 且已存在 → WorkspaceKeyConflict。
- 绑定：ACL 完全等价校验（fail closed，不等拒绝）；禁止绑定非 active（archived）workspace；
  DB 部分唯一索引 ux_nb_ws_binding_active 兜底「一个 Notebook 同时最多一个 active
  binding」，应用层仍先查重给出友好错误。
- 可见性：普通用户基于 acl_scope（复用 _scope_matches）；admin 全部。
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import access_control
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    WikiPage,
    WikiWorkspace,
)

# ---- 状态常量 ----
WS_STATUS_ACTIVE = "active"
WS_STATUS_ARCHIVED = "archived"
BINDING_STATUS_ACTIVE = "active"
BINDING_STATUS_DISABLED = "disabled"


class WorkspaceError(ValueError):
    """业务校验失败（映射为 400/409 的基类）。"""


class WorkspaceConflict(WorkspaceError):
    """冲突（映射为 409）：notebook 已绑定其他 active workspace。"""


class WorkspaceKeyConflict(WorkspaceError):
    """key 冲突（映射为 409）：手工提供 key 与既有 workspace 冲突，与 ACL 无关。"""


class WorkspaceArchivedError(WorkspaceError):
    """目标 workspace 非 active（archived/删除）（映射为 409）：禁止绑定。"""


def _scope_id_from_acl_json(acl_scope: str) -> str | None:
    """把规范化 acl_scope JSON 转回 scope_id；无法解析/unknown → None。"""
    scope = access_control.scope_from_acl(acl_scope)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        return None
    return access_control.normalize_scope_id(scope)


def _acl_json_from_scope_id(scope_id: str) -> str | None:
    """把 scope_id（company/admin/group:<...>）转回规范化 acl_scope JSON。"""
    sid = (scope_id or "").strip()
    if sid == access_control.SCOPE_COMPANY:
        return '{"groups": ["__public__"]}'
    if sid == access_control.SCOPE_ADMIN:
        return '{"groups": ["__local_admin__"]}'
    if sid.startswith("group:"):
        groups = [g.strip() for g in sid[len("group:"):].split(",") if g.strip()]
        if not groups:
            return None
        return json.dumps({"groups": sorted(set(groups))}, ensure_ascii=False)
    return None


def _resolve_create_scope(acl_scope: str | None, scope_id: str | None) -> tuple[str, str]:
    """解析创建请求的 (scope_id, acl_scope)；非法输入抛 WorkspaceError。

    规则：acl_scope 与 scope_id 二选一或同时给出；同时给出必须一致（规范化后）。
    """
    if acl_scope and scope_id:
        sid_from_acl = _scope_id_from_acl_json(acl_scope)
        acl_from_sid = _acl_json_from_scope_id(scope_id)
        if sid_from_acl is None or acl_from_sid is None:
            raise WorkspaceError("acl_scope 与 scope_id 均非法或无法解析")
        if not access_control.acl_scope_equivalent(acl_scope, acl_from_sid):
            raise WorkspaceError("acl_scope 与 scope_id 不一致")
        return sid_from_acl, access_control.acl_json_for_scope(access_control.scope_from_acl(acl_from_sid))
    if acl_scope:
        sid = _scope_id_from_acl_json(acl_scope)
        if sid is None:
            raise WorkspaceError("acl_scope 无法解析为合法权限域（company/group/admin）")
        scope = access_control.scope_from_acl(acl_scope)
        return sid, access_control.acl_json_for_scope(scope)
    if scope_id:
        acl = _acl_json_from_scope_id(scope_id)
        if acl is None:
            raise WorkspaceError("scope_id 格式非法（应为 company / admin / group:<逗号分隔组名>）")
        scope = access_control.scope_from_acl(acl)
        return access_control.normalize_scope_id(scope), access_control.acl_json_for_scope(scope)
    raise WorkspaceError("必须提供 acl_scope 或 scope_id 之一")


def create_workspace(db: Session, payload, created_by: str | None) -> WikiWorkspace:
    """创建 workspace。

    - key：调用方可选提供（payload.key，strip 非空即独立业务 key）或系统生成
      （generate_manual_workspace_key）；同 ACL 可创建多个 workspace（scope_id 只做
      权限/审计字段，不再派生默认 key）；
    - payload.key 与既有 workspace 冲突 → WorkspaceKeyConflict（409，与 ACL 无关）；
    - scope_id / acl_scope 仍基于 acl_json_for_scope + normalize_scope_id 解析并校验
      （二选一或一致），但不参与 key。
    """
    scope_id, acl_scope = _resolve_create_scope(payload.acl_scope, payload.scope_id)
    provided_key = (payload.key or "").strip()
    if provided_key:
        existing = db.query(WikiWorkspace).filter(WikiWorkspace.key == provided_key).first()
        if existing is not None:
            raise WorkspaceKeyConflict(f"已存在同 key 的 workspace：{existing.id}")
        key = provided_key
    else:
        key = access_control.generate_manual_workspace_key()
    ws = WikiWorkspace(
        id=str(uuid.uuid4()),
        key=key,
        name=(payload.name or "").strip() or scope_id,
        description=payload.description,
        acl_scope=acl_scope,
        scope_id=scope_id,
        status=(payload.status or WS_STATUS_ACTIVE),
        created_by=created_by,
    )
    db.add(ws)
    db.flush()
    return ws


def workspace_visible(db: Session, ws: WikiWorkspace, current_user: dict) -> bool:
    """workspace 对当前用户是否可见。

    - admin 全量可见（含 archived，用于管理）；
    - 普通用户：archived workspace 不可见（不接收也不浏览新内容），
      其余基于 acl_scope（复用 _scope_matches）。
    """
    if access_control.is_admin(current_user):
        return True
    if ws.status != "active":
        return False
    return access_control._scope_matches(access_control.scope_from_acl(ws.acl_scope), current_user)


def list_visible_workspaces(db: Session, current_user: dict) -> list[WikiWorkspace]:
    """普通用户只返回有权限访问的 workspace；admin 全部。"""
    rows = db.query(WikiWorkspace).all()
    return [ws for ws in rows if workspace_visible(db, ws, current_user)]


def get_visible_workspace(db: Session, current_user: dict, workspace_id: str) -> WikiWorkspace | None:
    """可见则返回，不可见/不存在 → None（调用方映射 404，不泄露存在性）。"""
    ws = db.get(WikiWorkspace, workspace_id)
    if ws is None:
        return None
    if not workspace_visible(db, ws, current_user):
        return None
    return ws


def update_workspace(db: Session, ws: WikiWorkspace, payload) -> WikiWorkspace:
    """修改 name / description / status（不修改 key / acl_scope / scope_id）。"""
    if payload.name is not None:
        ws.name = payload.name.strip() or ws.name
    if payload.description is not None:
        ws.description = payload.description
    if payload.status is not None:
        ws.status = payload.status
    db.flush()
    return ws


def bind_notebook(db: Session, ws: WikiWorkspace, notebook: Notebook, created_by: str | None) -> NotebookWorkspaceBinding:
    """绑定 Notebook → workspace（Phase 3.1）。

    - workspace 非 active（archived/删除）→ WorkspaceArchivedError（禁止绑定归档区）；
    - notebook 已有 active binding 指向本 workspace → 幂等（复用）；
    - notebook 已有 active binding 指向其他 workspace → WorkspaceConflict（先解绑再绑）；
    - notebook 有 disabled binding（unbind 软禁用残留）→ 复用为 active：指向本 workspace
      的直接重新激活；指向其他 workspace 的保留历史 disabled，另行为本 workspace 新建
      active binding（重新绑定路径，不删除历史记录）；
    - ACL 不等价 / notebook scope unknown → WorkspaceError（fail closed，拒绝绑定）；
    - DB 部分唯一索引 ux_nb_ws_binding_active 兜底：并发下第二个 active binding 会抛
      IntegrityError → 归一为 WorkspaceConflict（本 session 内查重通常先于它命中）。
    """
    if ws is None or ws.status != WS_STATUS_ACTIVE:
        raise WorkspaceArchivedError("workspace 非 active（已归档/已删除），禁止绑定")

    def _raise_conflict() -> None:
        raise WorkspaceConflict("Notebook 已绑定其他 workspace，请先解绑再重新绑定")

    rows = (
        db.query(NotebookWorkspaceBinding)
        .filter(NotebookWorkspaceBinding.notebook_id == notebook.id)
        .all()
    )
    for b in rows:
        if b.workspace_id == ws.id and b.status == BINDING_STATUS_ACTIVE:
            return b  # 幂等
    for b in rows:
        if b.status == BINDING_STATUS_ACTIVE:
            _raise_conflict()

    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        raise WorkspaceError("Notebook 权限域无法确定（UNKNOWN），拒绝绑定")
    notebook_acl = access_control.acl_json_for_scope(scope)
    if not access_control.acl_scope_equivalent(notebook_acl, ws.acl_scope):
        raise WorkspaceError("Notebook 权限域与 workspace ACL 不等价，拒绝绑定")

    try:
        for b in rows:
            if b.workspace_id == ws.id:
                # disabled → 重新激活（复用记录）
                b.status = BINDING_STATUS_ACTIVE
                db.flush()
                return b
        # 指向其他 workspace 的 disabled 历史一律保留（不删除：审计/可追溯）。
        # 到此不存在指向其他 workspace 的 active binding（已在上方 _raise_conflict），
        # 因此可直接为当前 workspace 新建 active binding，无需清理任何行。
        binding = NotebookWorkspaceBinding(
            id=str(uuid.uuid4()),
            notebook_id=notebook.id,
            workspace_id=ws.id,
            status=BINDING_STATUS_ACTIVE,
            created_by=created_by,
        )
        db.add(binding)
        db.flush()
        return binding
    except IntegrityError:
        # DB 部分唯一索引兜底：并发下第二个同 notebook active binding → 归一友好冲突。
        db.rollback()
        _raise_conflict()


def unbind_notebook(db: Session, ws: WikiWorkspace, notebook_id: str) -> None:
    """软解绑：把 notebook 指向该 workspace 的 binding 置 status='disabled'（保留记录）。

    disabled 后 resolve/ensure 一律返回 None（fail closed），后续 dirty Page 进入
    Topic Router 也不会自动重建绑定（真正表达「永久不路由」）。重新绑定走
    bind_notebook（复用 disabled binding 置 active）。无绑定记录 → WorkspaceError
    （调用方映射 404）；已 disabled → 幂等返回（重复解绑不报错）。
    """
    binding = (
        db.query(NotebookWorkspaceBinding)
        .filter(
            NotebookWorkspaceBinding.notebook_id == notebook_id,
            NotebookWorkspaceBinding.workspace_id == ws.id,
        )
        .first()
    )
    if binding is None:
        raise WorkspaceError("Notebook 与该 workspace 无绑定")
    if binding.status != BINDING_STATUS_DISABLED:
        binding.status = BINDING_STATUS_DISABLED
        db.flush()


def list_workspace_wikis(db: Session, ws: WikiWorkspace, current_user: dict) -> list[WikiPage]:
    """返回该 workspace 内当前用户可见的 wiki（普通用户仅 published）。"""
    rows = (
        db.query(WikiPage)
        .filter(WikiPage.workspace_id == ws.id)
        .all()
    )
    visible_ids = access_control.get_visible_wiki_page_ids(db, current_user)
    admin = access_control.is_admin(current_user)
    result = []
    for wp in rows:
        if wp.id not in visible_ids:
            continue
        if not admin and wp.status != "published":
            continue
        result.append(wp)
    return result
