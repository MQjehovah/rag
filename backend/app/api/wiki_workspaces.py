"""Phase 3：独立 WikiWorkspace API。

- GET    /api/wiki-workspaces                             → 普通用户仅可见；admin 全部
- POST   /api/wiki-workspaces                             → admin 创建（name/description/acl_scope 或 scope_id）
- GET    /api/wiki-workspaces/{id}                        → 可见则返回，不可见 404（不泄露存在性）
- PATCH  /api/wiki-workspaces/{id}                        → admin 改 name/description/status
- POST   /api/wiki-workspaces/{id}/notebooks/{notebook_id} → admin 绑定（ACL 完全等价校验）
- DELETE /api/wiki-workspaces/{id}/notebooks/{notebook_id} → admin 解绑
- GET    /api/wiki-workspaces/{id}/wikis                  → 该 workspace 内可见 wiki

403 语义：admin-only 写操作非 admin → 403。
404 语义：资源不存在或不可见 → 404。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core import access_control
from app.core.jwt_utils import get_current_user, is_admin_user
from app.core.wiki_workspace.schemas import (
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceUpdate,
    WorkspaceWikiOut,
)
from app.core.wiki_workspace import service
from app.core.wiki_workspace.service import (
    WorkspaceArchivedError,
    WorkspaceConflict,
    WorkspaceError,
    WorkspaceKeyConflict,
)
from app.models.database import Notebook, WikiWorkspace

router = APIRouter(prefix="/api/wiki-workspaces", tags=["Wiki 工作区"])

logger = logging.getLogger(__name__)


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


def _serialize_workspace(ws: WikiWorkspace) -> dict:
    return {
        "id": ws.id,
        "key": ws.key,
        "name": ws.name,
        "description": ws.description,
        "acl_scope": ws.acl_scope,
        "scope_id": ws.scope_id,
        "status": ws.status,
        "created_by": ws.created_by,
        "created_at": ws.created_at.isoformat() if ws.created_at else None,
        "updated_at": ws.updated_at.isoformat() if ws.updated_at else None,
    }


def _serialize_wiki(wp) -> dict:
    return {
        "id": wp.id,
        "title": wp.title,
        "summary": wp.summary or "",
        "status": wp.status,
        "category": wp.category or "未分类",
        "workspace_id": wp.workspace_id,
        "updated_at": wp.updated_at.isoformat() if wp.updated_at else None,
    }


@router.get("")
def list_workspaces(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """普通用户只返回有权限访问的 workspace；admin 全部。"""
    rows = service.list_visible_workspaces(db, current_user)
    return {"workspaces": [_serialize_workspace(ws) for ws in rows]}


@router.post("", status_code=201)
def create_workspace(
    payload: WorkspaceCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    created_by = current_user.get("id") or current_user.get("username")
    try:
        ws = service.create_workspace(db, payload, created_by=created_by)
    except WorkspaceKeyConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except WorkspaceConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except WorkspaceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return _serialize_workspace(ws)


@router.get("/{workspace_id}")
def get_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    ws = service.get_visible_workspace(db, current_user, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    return _serialize_workspace(ws)


@router.patch("/{workspace_id}")
def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    ws = db.get(WikiWorkspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    service.update_workspace(db, ws, payload)
    db.commit()
    return _serialize_workspace(ws)


@router.post("/{workspace_id}/notebooks/{notebook_id}")
def bind_notebook(
    workspace_id: str,
    notebook_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """admin 绑定：ACL 完全等价校验（fail closed，不等拒绝）。"""
    _require_admin(current_user)
    ws = db.get(WikiWorkspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    notebook = db.get(Notebook, notebook_id)
    if notebook is None:
        raise HTTPException(status_code=404, detail="Notebook 不存在")
    created_by = current_user.get("id") or current_user.get("username")
    try:
        binding = service.bind_notebook(db, ws, notebook, created_by=created_by)
    except WorkspaceArchivedError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except WorkspaceConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except WorkspaceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {
        "binding_id": binding.id,
        "notebook_id": notebook.id,
        "workspace_id": ws.id,
        "status": binding.status,
    }


@router.delete("/{workspace_id}/notebooks/{notebook_id}")
def unbind_notebook(
    workspace_id: str,
    notebook_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """admin 软解绑：binding 置 disabled（保留记录，不物理删除）。

    解绑后该 Notebook 的 Page 不再自动路由（fail closed），后续 dirty Page 进入
    Topic Router 也不会被 ensure_notebook_workspace 自动重建绑定（真正表达「永久
    不路由」）。重新绑定走 POST bind——存在 disabled binding 时复用之置 active。
    已 disabled 重复解绑幂等返回 200；notebook 与该 workspace 从无绑定 → 404。
    """
    _require_admin(current_user)
    ws = db.get(WikiWorkspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    try:
        service.unbind_notebook(db, ws, notebook_id)
    except WorkspaceError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    return {"message": "已解绑", "workspace_id": ws.id, "notebook_id": notebook_id}


@router.get("/{workspace_id}/wikis")
def list_workspace_wikis(
    workspace_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    ws = service.get_visible_workspace(db, current_user, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="工作区不存在")
    rows = service.list_workspace_wikis(db, ws, current_user)
    return {"workspace_id": ws.id, "wikis": [_serialize_wiki(wp) for wp in rows]}
