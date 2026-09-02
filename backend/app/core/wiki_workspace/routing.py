"""Phase 3.1：WikiWorkspace 路由（确定性 Notebook → Workspace）。

核心规则：
- resolve_workspace_for_page / resolve_workspace_for_notebook：只读解析（不自动创建），
  无 active binding → None（fail closed 由调用方处理）。
- ensure_notebook_workspace：确定性自动路由——
  * 有 active binding → 返回绑定 workspace（显式共享优先）；
  * 无 binding → 按 workspace_key_for_notebook(notebook_id) 派生 key 建 workspace
    （若同 key 已存在且 active 则复用 —— 该 notebook 的默认私用空间，
    绝不匹配其他 notebook 的 key），并建 binding；
  * scope UNKNOWN / disabled binding（管理员意图保持未绑定）→ None（fail closed）；
  * binding 命中的 workspace 已 archived → None（不自动向归档区写入新内容）。
- page_workspace_id：发布前逐来源 Page 校验用（严格只读）。
不再按 scope_id 全局查找复用：同 ACL 不同 notebook 各自建 workspace，
合并必须走显式绑定（admin 建 workspace + bind_notebook）。
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core import access_control
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiWorkspace,
)


def _active_binding(db: Session, notebook_id: str) -> NotebookWorkspaceBinding | None:
    return (
        db.query(NotebookWorkspaceBinding)
        .filter(
            NotebookWorkspaceBinding.notebook_id == notebook_id,
            NotebookWorkspaceBinding.status == "active",
        )
        .first()
    )


def resolve_workspace_for_notebook(db: Session, notebook: Notebook) -> WikiWorkspace | None:
    """Notebook → 绑定的 active workspace；无 binding / binding disabled → None。

    binding 命中的 workspace 若 status != 'active'（archived/删除）→ 不可路由（None，
    fail closed，archived workspace 不再接收新内容）。
    """
    if notebook is None or not notebook.id:
        return None
    binding = _active_binding(db, notebook.id)
    if binding is None:
        return None
    workspace = db.get(WikiWorkspace, binding.workspace_id)
    if workspace is None or workspace.status != "active":
        return None
    return workspace


def resolve_workspace_for_page(db: Session, page: Page) -> WikiWorkspace | None:
    """Page → 其 Notebook 绑定的 active workspace；无归属/无 binding → None。"""
    if page is None or not page.notebook_id:
        return None
    notebook = db.get(Notebook, page.notebook_id)
    if notebook is None:
        return None
    return resolve_workspace_for_notebook(db, notebook)


def page_workspace_id(db: Session, page: Page) -> str | None:
    """来源 Page 的实际 workspace_id（严格只读，发布前校验用）。"""
    ws = resolve_workspace_for_page(db, page)
    return ws.id if ws is not None else None


def _disabled_binding(db: Session, notebook_id: str) -> NotebookWorkspaceBinding | None:
    """任一 binding（active/disabled）存在即视为管理员意图已声明。

    仅用于「无 active binding」时区分：完全无 binding（可自动建默认私用空间）vs
    存在 disabled binding（管理员已解绑/明确不路由 → fail closed，不自动重建）。
    """
    return (
        db.query(NotebookWorkspaceBinding)
        .filter(NotebookWorkspaceBinding.notebook_id == notebook_id)
        .first()
    )


def ensure_notebook_workspace(db: Session, notebook: Notebook) -> WikiWorkspace | None:
    """确定性路由：active binding 显式共享优先；无 binding → 按 notebook key 建私用空间。

    - 已有 active binding → 返回绑定 workspace（binding 命中的 workspace 已 archived
      或不存在 → None fail closed）；
    - 无 active binding 但存在任一 binding（disabled 残留）→ None（不覆盖管理员意图，
      真正表达「永久不路由」，unbind 后不再被自动重建）；
    - 完全无 binding → scope=scope_from_notebook；UNKNOWN → None（fail closed）；
      否则用 workspace_key_for_notebook(notebook.id) 派生 key 建 workspace
      （若同 key 已存在且 active → 复用该 workspace —— 该 notebook 的默认私用空间，
      绝不匹配其他 notebook 的 key；同 key 已 archived → None fail closed）+ active binding。
    """
    if notebook is None or not notebook.id:
        return None
    binding = _active_binding(db, notebook.id)
    if binding is not None:
        workspace = db.get(WikiWorkspace, binding.workspace_id)
        if workspace is None or workspace.status != "active":
            # archived workspace 不应接收新内容 → fail closed
            return None
        return workspace
    if _disabled_binding(db, notebook.id) is not None:
        # 已有 binding（active/disabled）但无 active：管理员意图保持未绑定 → fail closed。
        return None
    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        return None
    scope_id = access_control.normalize_scope_id(scope)
    key = access_control.workspace_key_for_notebook(notebook.id)
    workspace = db.query(WikiWorkspace).filter(WikiWorkspace.key == key).first()
    if workspace is not None and workspace.status != "active":
        # 该 notebook 默认私用空间的 workspace 已被归档 → 不自动向其写入新内容（fail closed）
        return None
    if workspace is None:
        workspace = WikiWorkspace(
            id=str(uuid.uuid4()),
            key=key,
            name=scope_id,
            description=None,
            acl_scope=access_control.acl_json_for_scope(scope),
            scope_id=scope_id,
            status="active",
        )
        db.add(workspace)
        db.flush()
    binding = NotebookWorkspaceBinding(
        id=str(uuid.uuid4()),
        notebook_id=notebook.id,
        workspace_id=workspace.id,
        status="active",
    )
    db.add(binding)
    db.flush()
    return workspace
