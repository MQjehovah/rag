from pydantic import BaseModel, field_validator
from typing import Any, Dict, List, Optional
from datetime import datetime


class NotebookBase(BaseModel):
    name: str

class NotebookCreate(NotebookBase):
    group_id: Optional[str] = None
    description: str = ''
    embedding_profile_id: Optional[str] = None
    section: str = ''

class NotebookUpdate(BaseModel):
    name: Optional[str] = None
    group_id: Optional[str] = None
    description: Optional[str] = None
    embedding_profile_id: Optional[str] = None
    section: Optional[str] = None

class NotebookMove(BaseModel):
    position: int = 0
    section: Optional[str] = None

class NotebookResponse(NotebookBase):
    id: str
    group_id: Optional[str] = None
    description: str = ''
    embedding_profile_id: Optional[str] = None
    position: int = 0
    section: str = ''
    created_at: datetime
    updated_at: datetime

    @field_validator('description', mode='before')
    @classmethod
    def _coerce_description(cls, v):
        # 历史行在迁移补列后 description 可能为 NULL,统一归一为空串
        return v or ''

    class Config:
        from_attributes = True


class NotebookListResponse(BaseModel):
    notebooks: List[NotebookResponse]
    unassigned_count: int


class EmbeddingProfileBase(BaseModel):
    name: str
    kind: str = 'openai'
    api_url: str
    model: str
    api_key: str = ''
    dimensions: int = 1024


class EmbeddingProfileCreate(EmbeddingProfileBase):
    is_default: bool = False


class EmbeddingProfileUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    api_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    dimensions: Optional[int] = None


class EmbeddingProfileResponse(EmbeddingProfileBase):
    id: str
    is_default: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PipelineCreate(BaseModel):
    name: str
    description: str = ''
    scope_type: str = 'notebooks'           # notebooks | group | all
    notebook_ids: List[str] = []
    compiler_kind: str = 'wiki'             # wiki | api_doc | markdown | changelog | custom
    prompt_template: str = ''
    model: str = ''
    target_category: str = ''
    incremental: bool = True
    group_id: Optional[str] = None
    enabled: bool = True


class PipelineUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    scope_type: Optional[str] = None
    notebook_ids: Optional[List[str]] = None
    compiler_kind: Optional[str] = None
    prompt_template: Optional[str] = None
    model: Optional[str] = None
    target_category: Optional[str] = None
    incremental: Optional[bool] = None
    group_id: Optional[str] = None
    enabled: Optional[bool] = None


class PipelineResponse(BaseModel):
    id: str
    name: str
    description: str = ''
    scope_type: str = 'notebooks'
    notebook_ids: List[str] = []
    compiler_kind: str = 'wiki'
    prompt_template: str = ''
    model: str = ''
    target_category: str = ''
    incremental: bool = True
    group_id: Optional[str] = None
    enabled: bool = True
    created_at: datetime
    updated_at: datetime
    running: bool = False
    last_status: str = ''
    last_run_at: Optional[datetime] = None

class PageBase(BaseModel):
    title: str = '无标题'
    content: str = ''
    notebook_id: Optional[str] = None
    icon: str = ''
    cover: str = ''
    cover_offset: int = 50
    parent_id: Optional[str] = None

class PageCreate(PageBase):
    pass

class PageUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    notebook_id: Optional[str] = None
    icon: Optional[str] = None
    cover: Optional[str] = None
    cover_offset: Optional[int] = None
    status: Optional[str] = None

class PageMove(BaseModel):
    parent_id: Optional[str] = None
    position: int = 0

class PageViewUpdate(BaseModel):
    view_type: str = 'doc'

class CommentCreate(BaseModel):
    content: str = ''

class PageResponse(PageBase):
    id: str
    position: int = 0
    share_token: Optional[str] = None
    cover_offset: int = 50
    view_type: str = 'doc'
    status: str = ''
    created_at: datetime
    updated_at: datetime

    @field_validator('icon', 'cover', 'view_type', 'status', mode='before')
    @classmethod
    def _coerce_text(cls, v):
        return v or ''

    class Config:
        from_attributes = True

class PageListItem(BaseModel):
    id: str
    title: str = '无标题'
    notebook_id: Optional[str] = None
    parent_id: Optional[str] = None
    position: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class PageListResponse(BaseModel):
    items: List[PageListItem]
    total: int
    page: int
    page_size: int

class GraphNodeResponse(BaseModel):
    id: str
    title: str
    notebook_id: Optional[str] = None
    link_count: int = 0
    kind: str = 'page'
    entity_type: Optional[str] = None
    community: Optional[str] = None

class GraphEdgeResponse(BaseModel):
    id: str
    source_id: str
    target_id: str
    weight: float
    edge_type: str
    label: str = ''

class GraphDataResponse(BaseModel):
    nodes: List[GraphNodeResponse]
    edges: List[GraphEdgeResponse]

class GraphStatsResponse(BaseModel):
    total_nodes: int
    total_edges: int
    avg_connections: float
    clusters: int
    total_entities: int = 0
    total_relations: int = 0

class EnhancedSearchResult(BaseModel):
    id: str
    title: str
    content: str
    score: float
    source: str
    chunks: List[Dict[str, Any]] = []

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

class LoginResponse(BaseModel):
    token: str
    user: UserResponse

class GroupResponse(BaseModel):
    group_name: str
