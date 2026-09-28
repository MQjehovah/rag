from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session
import uuid

from app.models.database import Page, PageChunk, Notebook
from app.core.hybrid import HybridIndex
from app.core.entity_graph import EntityGraphStore
from app.models.schema import NotebookCreate, NotebookUpdate, NotebookMove, NotebookResponse, NotebookListResponse
from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.core.security import has_permission
from app.core.visibility import (
    ACL_NOTEBOOK,
    delete_acl,
    load_acl_lists,
    notebook_visible_condition,
    parse_visibility,
    replace_acl,
    resource_visible_db,
)

router = APIRouter(prefix="/api/notebooks", tags=["笔记本"])


def _parse_visibility(value):
    try:
        return parse_visibility(value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _group_for_visibility(visibility, current_user, requested=None):
    """按可见性决定 group_id:dept→本人组(优先请求的组), public/self→NULL。"""
    if visibility != 'dept':
        return None
    groups = current_user["groups"]
    if requested and requested in groups:
        return requested
    return groups[0] if groups else None


def notebook_writable(db: Session, user, notebook) -> bool:
    """写/读单资源闸门现状:notebook.manage 或可见性域内(含资源 ACL)。"""
    return has_permission(user, "notebook.manage") or resource_visible_db(db, user, ACL_NOTEBOOK, notebook)


def _notebook_response(db: Session, notebook: Notebook) -> NotebookResponse:
    """响应带资源 ACL 预填字段(GET /{id} 与 PUT 回包共用)。"""
    resp = NotebookResponse.model_validate(notebook)
    resp.acl_users, resp.acl_groups = load_acl_lists(db, ACL_NOTEBOOK, notebook.id)
    return resp


@router.post("", response_model=NotebookResponse)
def create_notebook(data: NotebookCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    visibility = _parse_visibility(data.visibility or 'dept')
    group_id = _group_for_visibility(visibility, current_user, data.group_id)
    max_pos = db.query(func.max(Notebook.position)).filter(
        (Notebook.group_id == group_id) if group_id else (Notebook.group_id.is_(None))
    ).scalar()
    notebook = Notebook(
        id=str(uuid.uuid4()),
        name=data.name,
        group_id=group_id,
        visibility=visibility,
        owner_id=current_user["id"],
        description=data.description or '',
        icon=data.icon or '',
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
    if not has_permission(current_user, "notebook.manage"):
        query = query.filter(notebook_visible_condition(current_user))
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
    if not notebook_writable(db, current_user, notebook):
        raise HTTPException(status_code=403, detail="无权访问该笔记本")
    return _notebook_response(db, notebook)

@router.put("/{notebook_id}", response_model=NotebookResponse)
def update_notebook(notebook_id: str, data: NotebookUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if not notebook_writable(db, current_user, notebook):
        raise HTTPException(status_code=403, detail="无权访问该笔记本")
    manages = has_permission(current_user, "notebook.manage")
    wants_acl = data.acl_users is not None or data.acl_groups is not None
    if wants_acl and not manages:
        raise HTTPException(status_code=403, detail="仅管理员可配置资源授权")
    if data.name is not None:
        notebook.name = data.name
    if data.description is not None:
        notebook.description = data.description
    if data.icon is not None:
        notebook.icon = data.icon
    if data.section is not None:
        notebook.section = data.section
    if data.embedding_profile_id is not None:
        # 允许传空串/null 表示"使用默认档案"
        notebook.embedding_profile_id = data.embedding_profile_id or None
    if data.group_id and data.group_id in current_user["groups"]:
        notebook.group_id = data.group_id
    if data.visibility is not None:
        visibility = _parse_visibility(data.visibility)
        notebook.visibility = visibility
        notebook.group_id = _group_for_visibility(visibility, current_user, notebook.group_id)
        if visibility == 'self' and not notebook.owner_id:
            # legacy 笔记本转 private 时补记当前操作者为 owner, 否则无人可见
            notebook.owner_id = current_user["id"]
    if wants_acl:
        # 替换式保存:DELETE 后 INSERT(去重、剔除空值),追加授权不改变原可见性
        replace_acl(db, ACL_NOTEBOOK, notebook.id, data.acl_users or [], data.acl_groups or [])
    db.commit()
    db.refresh(notebook)
    return _notebook_response(db, notebook)

@router.post("/{notebook_id}/move")
def move_notebook(notebook_id: str, data: NotebookMove, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """侧边栏笔记本排序 / 移动到分组。"""
    notebook = db.query(Notebook).filter(Notebook.id == notebook_id).first()
    if not notebook:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    if not notebook_writable(db, current_user, notebook):
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
    if not notebook_writable(db, current_user, notebook):
        raise HTTPException(status_code=403, detail="无权访问该笔记本")

    pages = db.query(Page).filter(Page.notebook_id == notebook_id).all()
    for page in pages:
        db.query(PageChunk).filter(PageChunk.page_id == page.id).delete()
        HybridIndex(db).delete_page(page.id)
        EntityGraphStore(db).delete_page(page.id)
        db.delete(page)

    delete_acl(db, ACL_NOTEBOOK, notebook_id)
    db.delete(notebook)
    db.commit()
    return {"message": "删除成功"}
