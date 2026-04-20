from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class NotebookBase(BaseModel):
    name: str

class NotebookCreate(NotebookBase):
    pass

class NotebookResponse(NotebookBase):
    id: str
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

class PageResponse(PageBase):
    id: str
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

class GraphNodeResponse(BaseModel):
    id: str
    title: str
    notebook_id: Optional[str] = None
    link_count: int = 0

class GraphEdgeResponse(BaseModel):
    id: str
    source_id: str
    target_id: str
    weight: float
    edge_type: str

class GraphDataResponse(BaseModel):
    nodes: List[GraphNodeResponse]
    edges: List[GraphEdgeResponse]

class GraphStatsResponse(BaseModel):
    total_nodes: int
    total_edges: int
    avg_connections: float
    clusters: int

class EnhancedSearchResult(BaseModel):
    id: str
    title: str
    content: str
    score: float
    source: str

class EnhancedSearchResponse(BaseModel):
    results: List[EnhancedSearchResult]
    total: int
    graph_expanded: int