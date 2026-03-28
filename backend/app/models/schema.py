from pydantic import BaseModel
from typing import Optional
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