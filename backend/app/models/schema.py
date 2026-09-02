from pydantic import BaseModel
from typing import List, Literal, Optional
from datetime import datetime


class NotebookBase(BaseModel):
    name: str

class NotebookCreate(NotebookBase):
    group_id: Optional[str] = None

class NotebookResponse(NotebookBase):
    id: str
    group_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class PageBase(BaseModel):
    title: str = '无标题'
    content: str = ''
    notebook_id: Optional[str] = None

class PageCreate(PageBase):
    pass

class PageUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    notebook_id: Optional[str] = None
    allow_source_edit: bool = False


class SourcePageImport(BaseModel):
    """受信任同步程序写入来源文档时使用的专用结构。"""

    page_id: Optional[str] = None
    title: str
    content: str
    notebook_id: str
    source_type: Literal["dingtalk"] = "dingtalk"
    source_id: str
    source_path: Optional[str] = None
    source_space_id: Optional[str] = None
    source_url: Optional[str] = None
    source_file_hash: Optional[str] = None
    source_file_size: Optional[int] = None
    source_mime_type: Optional[str] = None
    source_markdown_hash: str
    source_pipeline_version: str
    published_content_hash: str

class PageResponse(PageBase):
    id: str
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    source_path: Optional[str] = None
    source_space_id: Optional[str] = None
    source_url: Optional[str] = None
    source_file_hash: Optional[str] = None
    source_file_size: Optional[int] = None
    source_mime_type: Optional[str] = None
    source_content_hash: Optional[str] = None
    source_markdown_hash: Optional[str] = None
    source_pipeline_version: Optional[str] = None
    content_hash: Optional[str] = None
    current_content_hash: str = ""
    indexed_content_hash: Optional[str] = None
    index_status: str = "missing"
    last_synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class PageListItem(BaseModel):
    id: str
    title: str = '无标题'
    notebook_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class PageListResponse(BaseModel):
    items: List[PageListItem]
    total: int
    page: int
    page_size: int

class EnhancedSearchResult(BaseModel):
    id: str
    title: str
    content: str
    score: float
    source: str
    page_number: Optional[int] = None
    content_type: str = "text"
    source_url: Optional[str] = None

class EnhancedSearchResponse(BaseModel):
    results: List[EnhancedSearchResult]
    total: int
    graph_expanded: int

class LoginRequest(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: str
    username: str
    email: str = ""
    display_name: str = ""
    is_local: bool = False
    groups: List[str] = []
    is_active: bool = True
    is_admin: bool = False
    # V4 Phase B：明确的角色能力字段
    is_wiki_editor: bool = False
    roles: List[str] = ["user"]

class LoginResponse(BaseModel):
    token: str
    user: UserResponse

class GroupResponse(BaseModel):
    group_name: str
