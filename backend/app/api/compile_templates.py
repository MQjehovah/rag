"""编译模板库：可单独维护的「编译规则」(提示词/规则/输出模板), 供管道选用。"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.models.database import CompileTemplate
from app.models.schema import (
    CompileTemplateCreate,
    CompileTemplateResponse,
    CompileTemplateUpdate,
)

router = APIRouter(prefix="/api/compile-templates", tags=["编译模板"])


@router.get("", response_model=list[CompileTemplateResponse])
def list_templates(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    return db.query(CompileTemplate).order_by(CompileTemplate.created_at.asc()).all()


@router.post("", response_model=CompileTemplateResponse)
def create_template(data: CompileTemplateCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板名称不能为空")
    group_id = data.group_id
    if not group_id and current_user["groups"]:
        group_id = current_user["groups"][0]
    t = CompileTemplate(
        id=str(uuid.uuid4()), name=name, description=data.description or "",
        compiler_kind=data.compiler_kind or "wiki",
        prompt=data.prompt or "", rules=data.rules or "", template=data.template or "",
        group_id=group_id,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _get_or_404(template_id: str, db: Session) -> CompileTemplate:
    t = db.query(CompileTemplate).filter(CompileTemplate.id == template_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="模板不存在")
    return t


@router.get("/{template_id}", response_model=CompileTemplateResponse)
def get_template(template_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    return _get_or_404(template_id, db)


@router.put("/{template_id}", response_model=CompileTemplateResponse)
def update_template(template_id: str, data: CompileTemplateUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    t = _get_or_404(template_id, db)
    for field in ("name", "description", "compiler_kind", "prompt", "rules", "template", "group_id"):
        value = getattr(data, field)
        if value is not None:
            setattr(t, field, value)
    db.commit()
    db.refresh(t)
    return t


@router.delete("/{template_id}")
def delete_template(template_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    t = _get_or_404(template_id, db)
    db.delete(t)
    db.commit()
    return {"message": "已删除"}
