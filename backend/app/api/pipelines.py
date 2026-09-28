"""编译管道 API：把笔记本内容编译成 wiki 页（wiki / 接口文档 / 合集 / 变更记录 / 自定义）。"""
import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import settings
from app.core.jwt_utils import get_current_user
from app.core.pipeline import run_pipeline, preview_pipeline
from app.core.security import has_permission, require_permission
from app.models.database import (
    Notebook,
    Pipeline,
    PipelineRun,
    WikiPage,
    get_engine,
    get_session,
)
from app.models.schema import PipelineCreate, PipelineResponse, PipelineUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipelines", tags=["编译管道"])

_status: Dict[str, Dict[str, Any]] = {}
_tasks: Dict[str, asyncio.Task] = {}


def _is_admin(current_user) -> bool:
    return has_permission(current_user, "pipeline.manage")


def _visible(pipeline: Pipeline, current_user) -> bool:
    if _is_admin(current_user):
        return True
    return pipeline.group_id is None or pipeline.group_id in current_user["groups"]


def _to_response(pipeline: Pipeline, db: Session) -> PipelineResponse:
    running = bool(_status.get(pipeline.id, {}).get("running"))
    run = (
        db.query(PipelineRun)
        .filter(PipelineRun.pipeline_id == pipeline.id)
        .order_by(PipelineRun.started_at.desc())
        .first()
    )
    return PipelineResponse(
        id=pipeline.id,
        name=pipeline.name,
        description=pipeline.description or "",
        scope_type=pipeline.scope_type or "notebooks",
        notebook_ids=list(json.loads(pipeline.notebook_ids or "[]")),
        compiler_kind=pipeline.compiler_kind or "wiki",
        template_id=pipeline.template_id,
        prompt_template=pipeline.prompt_template or "",
        compile_rules=pipeline.compile_rules or "",
        compile_template=pipeline.compile_template or "",
        model=pipeline.model or "",
        target_category=pipeline.target_category or "",
        target_space_id=pipeline.target_space_id,
        auto_trigger=bool(pipeline.auto_trigger),
        incremental=bool(pipeline.incremental),
        group_id=pipeline.group_id,
        enabled=bool(pipeline.enabled),
        created_at=pipeline.created_at,
        updated_at=pipeline.updated_at,
        running=running,
        last_status=(run.status if run else ""),
        last_run_at=(run.finished_at or run.started_at) if run else None,
    )


@router.get("", response_model=List[PipelineResponse])
def list_pipelines(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    rows = db.query(Pipeline).order_by(Pipeline.updated_at.desc()).all()
    return [_to_response(p, db) for p in rows if _visible(p, current_user)]


@router.post("", response_model=PipelineResponse)
def create_pipeline(data: PipelineCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    group_id = data.group_id
    if group_id and group_id not in current_user["groups"] and not _is_admin(current_user):
        group_id = current_user["groups"][0] if current_user["groups"] else None
    if not group_id and not _is_admin(current_user) and current_user["groups"]:
        group_id = current_user["groups"][0]
    pipeline = Pipeline(
        id=str(uuid.uuid4()),
        name=data.name,
        description=data.description or "",
        scope_type=data.scope_type or "notebooks",
        notebook_ids=json.dumps(data.notebook_ids or [], ensure_ascii=False),
        compiler_kind=data.compiler_kind or "wiki",
        template_id=(data.template_id or None),
        prompt_template=data.prompt_template or "",
        compile_rules=data.compile_rules or "",
        compile_template=data.compile_template or "",
        model=data.model or "",
        target_category=data.target_category or "",
        target_space_id=(data.target_space_id or None),
        auto_trigger=bool(data.auto_trigger),
        incremental=bool(data.incremental),
        group_id=group_id,
        enabled=bool(data.enabled),
    )
    db.add(pipeline)
    db.commit()
    db.refresh(pipeline)
    return _to_response(pipeline, db)


@router.get("/compile-templates")
def compile_templates(current_user=Depends(get_current_user)):
    """内置编译规则(提示词/输出模板) + 通用规则 + 固定骨架，供前端预填/编辑模板。"""
    require_permission(current_user, "pipeline.manage")
    from app.core.wiki import COMMON_RULES, INGEST_PROMPT, KIND_PROMPTS, KIND_TEMPLATES
    kinds = {
        k: {"prompt": KIND_PROMPTS.get(k, ""), "template": KIND_TEMPLATES.get(k, "")}
        for k in KIND_PROMPTS
    }
    return {"kinds": kinds, "rules": COMMON_RULES, "skeleton": INGEST_PROMPT}


def _get_or_404(pipeline_id: str, db: Session, current_user) -> Pipeline:
    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pipeline or not _visible(pipeline, current_user):
        raise HTTPException(status_code=404, detail="编译管道不存在")
    return pipeline


@router.get("/{pipeline_id}", response_model=PipelineResponse)
def get_pipeline(pipeline_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    return _to_response(_get_or_404(pipeline_id, db, current_user), db)


@router.put("/{pipeline_id}", response_model=PipelineResponse)
def update_pipeline(pipeline_id: str, data: PipelineUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    pipeline = _get_or_404(pipeline_id, db, current_user)
    if data.name is not None:
        pipeline.name = data.name
    if data.description is not None:
        pipeline.description = data.description
    if data.scope_type is not None:
        pipeline.scope_type = data.scope_type
    if data.notebook_ids is not None:
        pipeline.notebook_ids = json.dumps(data.notebook_ids, ensure_ascii=False)
    if data.compiler_kind is not None:
        pipeline.compiler_kind = data.compiler_kind
    if data.template_id is not None:
        pipeline.template_id = data.template_id or None
    if data.prompt_template is not None:
        pipeline.prompt_template = data.prompt_template
    if data.compile_rules is not None:
        pipeline.compile_rules = data.compile_rules
    if data.compile_template is not None:
        pipeline.compile_template = data.compile_template
    if data.model is not None:
        pipeline.model = data.model
    if data.target_category is not None:
        pipeline.target_category = data.target_category
    if data.target_space_id is not None:
        pipeline.target_space_id = data.target_space_id or None
    if data.auto_trigger is not None:
        pipeline.auto_trigger = bool(data.auto_trigger)
    if data.incremental is not None:
        pipeline.incremental = bool(data.incremental)
    if data.enabled is not None:
        pipeline.enabled = bool(data.enabled)
    if data.group_id is not None and (data.group_id in current_user["groups"] or _is_admin(current_user)):
        pipeline.group_id = data.group_id or None
    db.commit()
    db.refresh(pipeline)
    return _to_response(pipeline, db)


@router.delete("/{pipeline_id}")
def delete_pipeline(pipeline_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    pipeline = _get_or_404(pipeline_id, db, current_user)
    if not _is_admin(current_user) and pipeline.group_id and pipeline.group_id not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="无权删除该管道")
    db.delete(pipeline)
    db.commit()
    return {"message": "删除成功"}


@router.post("/{pipeline_id}/run")
async def run_pipeline_endpoint(pipeline_id: str, mode: str = "incremental", db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    pipeline = _get_or_404(pipeline_id, db, current_user)
    if _status.get(pipeline.id, {}).get("running"):
        return {"started": False, "running": True, "message": "该管道正在运行"}
    run_mode = "full" if mode == "full" else "incremental"
    _status[pipeline.id] = {"running": True, "processed": 0, "total": 0, "changed": 0, "message": "启动编译..."}
    engine = db.get_bind()
    _tasks[pipeline.id] = asyncio.create_task(run_pipeline(engine, pipeline.id, _status[pipeline.id], run_mode))
    return {"started": True, "running": True, "mode": run_mode}


@router.get("/{pipeline_id}/status")
def pipeline_status(pipeline_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    _get_or_404(pipeline_id, db, current_user)
    return _status.get(pipeline_id, {"running": False, "processed": 0, "total": 0, "changed": 0, "message": ""})


@router.get("/{pipeline_id}/runs")
def pipeline_runs(pipeline_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    _get_or_404(pipeline_id, db, current_user)
    rows = (
        db.query(PipelineRun)
        .filter(PipelineRun.pipeline_id == pipeline_id)
        .order_by(PipelineRun.started_at.desc())
        .limit(30)
        .all()
    )
    return [
        {
            "id": r.id,
            "status": r.status,
            "processed": r.processed,
            "total": r.total,
            "changed": r.changed,
            "message": r.message,
            "error": r.error,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
        }
        for r in rows
    ]


@router.post("/{pipeline_id}/preview")
async def pipeline_preview(pipeline_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    _get_or_404(pipeline_id, db, current_user)
    engine = get_engine(settings.database_url)
    return await preview_pipeline(engine, pipeline_id)


@router.get("/{pipeline_id}/pages")
def pipeline_pages(pipeline_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    require_permission(current_user, "pipeline.manage")
    _get_or_404(pipeline_id, db, current_user)
    rows = (
        db.query(WikiPage)
        .filter(WikiPage.pipeline_id == pipeline_id)
        .order_by(WikiPage.updated_at.desc())
        .all()
    )
    return [
        {
            "id": w.id,
            "title": w.title,
            "category": w.category or "",
            "summary": w.summary or "",
            "updated_at": w.updated_at,
        }
        for w in rows
    ]
