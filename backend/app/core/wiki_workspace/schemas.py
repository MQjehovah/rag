"""Phase 3.1：WikiWorkspace API 请求/响应模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

# workspace / binding 状态字面量（Phase 3.1：service 层用常量，schema 层 Literal 校验）。
WorkspaceStatus = Literal["active", "archived"]
BindingStatus = Literal["active", "disabled"]


class WorkspaceCreate(BaseModel):
    name: str
    description: Optional[str] = None
    # 可选业务 key（独立身份）；缺省由系统生成唯一 key。
    key: Optional[str] = None
    # 规范化权限域：提供 acl_scope（规范化 JSON）或 scope_id（company/admin/group:<...>）二选一。
    # scope_id/acl_scope 只是权限/审计字段，不参与默认 key；同 ACL 可创建多个 workspace。
    acl_scope: Optional[str] = None
    scope_id: Optional[str] = None
    status: WorkspaceStatus = "active"


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[WorkspaceStatus] = None


class WorkspaceOut(BaseModel):
    id: str
    key: str
    name: str
    description: Optional[str] = None
    acl_scope: str
    scope_id: str
    status: str
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class WorkspaceWikiOut(BaseModel):
    id: str
    title: str
    summary: str
    status: str
    category: str
    workspace_id: Optional[str] = None
    updated_at: Optional[str] = None
