"""模型健康 API（P13-MODEL-06，V3 计划 8.5）。

GET /api/admin/model-health → embedding/reranker 的健康状态。
普通用户不显示内部地址和错误正文（仅管理员可见）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_db
from app.core.embedding.health import get_model_health
from app.core.jwt_utils import get_current_user, is_admin_user

router = APIRouter(prefix="/api/admin", tags=["模型健康"])


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可查看模型健康")


@router.get("/model-health")
async def model_health(
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    return await get_model_health()
