"""Evidence API（P1-BE-09 + P1-FE-04）。

- GET  /api/evidence?page_id={id}    → 按 Page 查全部 Evidence
- GET  /api/evidence?asset_id={id}   → 按 Asset 查 Observation
- GET  /api/evidence/{evidence_id}   → 单条 Evidence 定位详情
- POST /api/evidence/observations    → 管理员补充/修改 manual Observation（P1-FE-04）

查询两参数互斥；不传任何参数返回 400。查询鉴权：get_current_user +
get_visible_page_ids 越权过滤（W13）。写接口仅管理员。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core import evidence_query
from app.core.jwt_utils import get_current_user, get_visible_page_ids, is_admin_user
from app.models.database import VisionAnalysisJob

router = APIRouter(prefix="/api/evidence", tags=["证据"])


class ManualObservationPayload(BaseModel):
    asset_id: str
    content: str


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可补充图片功能说明")


def _visible(db: Session, current_user: dict) -> set:
    return get_visible_page_ids(db, current_user)


@router.get("")
def list_evidence(
    page_id: Optional[str] = None,
    asset_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    given = [x for x in (page_id, asset_id) if x]
    if len(given) != 1:
        raise HTTPException(status_code=400, detail="请且仅提供 page_id / asset_id 之一")

    visible = _visible(db, current_user)
    if page_id:
        items = evidence_query.query_by_page(db, page_id, visible)
        return {"page_id": page_id, "evidence": items}
    obs = evidence_query.query_by_asset(db, asset_id, visible)
    return {"asset_id": asset_id, "observations": obs}


def _serialize_vision_job(job: VisionAnalysisJob) -> dict:
    return {
        "id": job.id,
        "page_id": job.page_id,
        "status": job.status,
        "trigger": job.trigger,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "error_category": job.error_category,
        "error_message": job.error_message,
        "result_json": job.result_json,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.get("/vision-jobs")
def list_vision_jobs(
    status: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    query = db.query(VisionAnalysisJob)
    if status:
        query = query.filter(VisionAnalysisJob.status == status)
    rows = query.order_by(VisionAnalysisJob.created_at.desc()).limit(min(max(limit, 1), 500)).all()
    return {"jobs": [_serialize_vision_job(row) for row in rows]}


@router.post("/vision-jobs/{job_id}/retry")
def retry_failed_vision_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    from app.core.vision_analysis import retry_vision_job
    try:
        job = retry_vision_job(db, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize_vision_job(job)


@router.post("/vision-jobs/run")
def run_vision_jobs(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    from app.core.vision_analysis import process_vision_analysis_jobs
    return process_vision_analysis_jobs(db)


@router.get("/{evidence_id}")
def get_evidence(
    evidence_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    visible = _visible(db, current_user)
    detail = evidence_query.get_evidence_detail(db, evidence_id, visible)
    if detail is None:
        raise HTTPException(status_code=404, detail="Evidence 不存在或无权访问")
    return detail


@router.post("/observations")
def create_manual_observation(
    payload: ManualObservationPayload,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """管理员补充/修改图片功能说明，生成 manual Observation（P1-FE-04）。"""
    _require_admin(current_user)
    if not payload.asset_id.strip():
        raise HTTPException(status_code=400, detail="asset_id 不能为空")
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="功能说明内容不能为空")
    obs = evidence_query.upsert_manual_observation(
        db, payload.asset_id.strip(), payload.content.strip()
    )
    return obs
