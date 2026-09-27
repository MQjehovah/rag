from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session
import uuid

from app.models.database import Page, PageChunk, Notebook
from app.core.hybrid import HybridIndex
from app.core.entity_graph import EntityGraphStore
from app.models.schema import NotebookCreate, NotebookUpdate, NotebookMove, NotebookResponse, NotebookListResponse
from app.core.rag import VectorStore
from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/notebooks", tags=["笔记本"])

@router.post("", response_model=NotebookResponse)
def create_notebook(data: NotebookCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    group_id = data.group_id
    if group_id and group_id not in current_user["groups"]:
        group_id = current_user["groups"][0] if current_user["groups"] else None
    if not group_id and current_user["groups"]:
        group_id = current_user["groups"][0]
    max_pos = db.query(func.max(Notebook.position)).filter(
        (Notebook.group_id == group_id) if group_id else (Notebook.group_id.is_(None))
    ).scalar()
    notebook = Notebook(
        id=str(uuid.uuid4()),
        name=data.name,
        group_id=group_id,
        description=data.description or '',
        embedding_profile_id=(data.embedding_profile_id or None),
        section=data.section or '',
        position=(max_pos or 0) + 1,
    )
    db.add(notebook)
    db.commit()
    db.refresh(notebook)
    return notebook

@router.get("", response_model=NotebookListResponse)
def list_notebooks(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    query = db.query(Notebook)
    if "__local_admin__" not in current_user["groups"]:
        query = query.filter((Notebook.group_id.in_(current_user["groups"])) | (Notebook.group_id.is_(None)))
    notebooks = query.order_by(Notebook.position.asc(), Notebook.created_at.asc()).all()

    unassigned_count = db.query(Page.id).filter(Page.notebook_id.is_(None)).count()

    return NotebookListResponse(
        notebooks=notebooks,
        unassigned_count=unassigned_count,
    )

@router.get("/{notebook_id}", response_model=NotebookResponse)
def get_notebook(notebook_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if "__local_admin__" not in current_user["groups"]:
        if notebook.group_id and notebook.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记本")
    return notebook

@router.put("/{notebook_id}", response_model=NotebookResponse)
def update_notebook(notebook_id: str, data: NotebookUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if "__local_admin__" not in current_user["groups"]:
        if notebook.group_id and notebook.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记本")
    if data.name is not None:
        notebook.name = data.name
    if data.description is not None:
        notebook.description = data.description
    if data.section is not None:
        notebook.section = data.section
    if data.embedding_profile_id is not None:
        # 允许传空串/null 表示"使用默认档案"
        notebook.embedding_profile_id = data.embedding_profile_id or None
    if data.group_id and data.group_id in current_user["groups"]:
        notebook.group_id = data.group_id
    db.commit()
    db.refresh(notebook)
    return notebook

@router.post("/{notebook_id}/move")
def move_notebook(notebook_id: str, data: NotebookMove, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """侧边栏笔记本排序 / 移动到分组。"""
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if "__local_admin__" not in current_user["groups"]:
        if notebook.group_id and notebook.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记本")
    if data.section is not None:
        notebook.section = data.section
    db.flush()
    q = db.query(Notebook).filter(Notebook.id != notebook_id)
    q = q.filter(Notebook.group_id == notebook.group_id) if notebook.group_id else q.filter(Notebook.group_id.is_(None))
    siblings = q.order_by(Notebook.position.asc(), Notebook.created_at.asc()).all()
    pos = max(0, min(int(data.position), len(siblings)))
    siblings.insert(pos, notebook)
    for i, n in enumerate(siblings):
        n.position = i
    db.commit()
    return {"message": "ok"}

@router.delete("/{notebook_id}")
def delete_notebook(notebook_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if "__local_admin__" not in current_user["groups"]:
        if notebook.group_id and notebook.group_id not in current_user["groups"]:
            raise HTTPException(status_code=403, detail="无权访问该笔记本")

    pages = db.query(Page).filter(Page.notebook_id == notebook_id).all()
    for page in pages:
        db.query(PageChunk).filter(PageChunk.page_id == page.id).delete()
        HybridIndex(db).delete_page(page.id)
        EntityGraphStore(db).delete_page(page.id)
        db.delete(page)

    db.delete(notebook)
    db.commit()
    return {"message": "删除成功"}
