from pydantic import BaseModel
from typing import Any, Dict, List, Optional
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


class NotebookListResponse(BaseModel):
    notebooks: List[NotebookResponse]
    unassigned_count: int

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

class PageResponse(PageBase):
    id: str
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

class GraphNodeResponse(BaseModel):
    id: str
    title: str
    notebook_id: Optional[str] = None
    link_count: int = 0
    kind: str = 'page'
    entity_type: Optional[str] = None

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
