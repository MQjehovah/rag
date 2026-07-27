from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List
import uuid

from app.models.database import Page, PageChunk, Notebook, get_session, get_engine, init_db
from app.models.schema import NotebookCreate, NotebookResponse
from app.core.rag import VectorStore
from app.core.jwt_utils import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/notebooks", tags=["笔记本"])

_engine = None
_session = None

def get_db():
    global _engine, _session
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session

@router.post("", response_model=NotebookResponse)
async def create_notebook(data: NotebookCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    group_id = data.group_id
    if group_id and group_id not in current_user["groups"]:
        group_id = current_user["groups"][0] if current_user["groups"] else None
    if not group_id and current_user["groups"]:
        group_id = current_user["groups"][0]
    notebook = Notebook(id=str(uuid.uuid4()), name=data.name, group_id=group_id)
    db.add(notebook)
    db.commit()
    db.refresh(notebook)
    return notebook

@router.get("", response_model=List[NotebookResponse])
async def list_notebooks(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    query = db.query(Notebook)
    if "__local_admin__" not in current_user["groups"]:
        query = query.filter((Notebook.group_id.in_(current_user["groups"])) | (Notebook.group_id.is_(None)))
    notebooks = query.order_by(Notebook.updated_at.desc()).all()
    return notebooks

@router.get("/{notebook_id}", response_model=NotebookResponse)
async def get_notebook(notebook_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if "__local_admin__" not in current_user["groups"]:
        if notebook.group_id and notebook.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记本")
    return notebook

@router.put("/{notebook_id}", response_model=NotebookResponse)
async def update_notebook(notebook_id: str, data: NotebookCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if "__local_admin__" not in current_user["groups"]:
        if notebook.group_id and notebook.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记本")
    notebook.name = data.name
    if data.group_id and data.group_id in current_user["groups"]:
        notebook.group_id = data.group_id
    db.commit()
    db.refresh(notebook)
    return notebook

@router.delete("/{notebook_id}")
async def delete_notebook(notebook_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if "__local_admin__" not in current_user["groups"]:
        if notebook.group_id and notebook.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记本")

    pages = db.query(Page).filter(Page.notebook_id == notebook_id).all()
    for page in pages:
        db.query(PageChunk).filter(PageChunk.page_id == page.id).delete()
        db.delete(page)

    db.delete(notebook)
    db.commit()
    return {"message": "删除成功"}
