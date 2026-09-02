"""V4 Phase J-4 回答反馈 / 访问申请 API。

- POST /api/v4/answer-feedback        → 提交 👍/👎（answer_id + helpful + reason + note）。
- POST /api/v4/answer-needed          → 「我仍需要这个答案」（仅 answer_id）。
- GET  /api/v4/access-requests        → 访问申请列表（仅管理员）。
- POST /api/v4/access-requests/{id}/revalidate → 手动重验证（仅管理员）。

所有身份、问题、版本、服务状态和权限上下文由后端通过 answer_id 的服务端快照读取，
不信任前端伪造字段。普通用户响应不含后台分类结果与隐藏命中信息。
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core import access_control
from app.core.jwt_utils import get_current_user
from app.core.retrieval.feedback_service import (
    NOTE_MAX_LENGTH,
    list_access_requests,
    record_answer_needed,
    revalidate_access_request,
    save_answer_feedback,
    serialize_access_request,
)

router = APIRouter(prefix="/api/v4", tags=["V4 回答反馈与访问申请"])


class AnswerFeedbackRequest(BaseModel):
    answer_id: str
    helpful: bool
    reason: Optional[Literal["incorrect", "incomplete"]] = None
    note: Optional[str] = Field(default=None, max_length=NOTE_MAX_LENGTH)


class AnswerNeededRequest(BaseModel):
    answer_id: str


def _require_admin(current_user: dict) -> None:
    if not access_control.is_admin(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


@router.post("/answer-feedback")
def submit_answer_feedback(
    request: AnswerFeedbackRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """提交回答反馈。answer_id 只能由原回答用户使用；其他用户 404（不泄露存在性）。"""
    result = save_answer_feedback(
        db,
        answer_id=request.answer_id,
        current_user=current_user,
        helpful=request.helpful,
        reason=request.reason,
        note=request.note,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="反馈不存在或无权操作")
    return {"ok": True}


@router.post("/answer-needed")
def submit_answer_needed(
    request: AnswerNeededRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """「我仍需要这个答案」。answer_id 只能由原回答用户使用；不满足条件时静默返回。"""
    result = record_answer_needed(db, answer_id=request.answer_id, current_user=current_user)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="回答不存在或无权操作")
    # 统一安全提示，不泄露后台分类结果。
    return {"ok": True}


@router.get("/access-requests")
def list_access_request_endpoint(
    status: Optional[str] = "open",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """访问申请列表（仅管理员）。"""
    _require_admin(current_user)
    requests = list_access_requests(db, status=status)
    return {"requests": [serialize_access_request(r) for r in requests]}


@router.post("/access-requests/{request_id}/revalidate")
def revalidate_access_request_endpoint(
    request_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """手动重验证单条访问申请（仅管理员）。"""
    _require_admin(current_user)
    resolved = revalidate_access_request(db, request_id)
    return {"resolved": resolved}
