"""P7 灰度 Feature Flag API（V3 计划 12.4）。

- GET  /api/p7/flags   → 当前 Feature Flag 状态
- POST /api/p7/flags   → 切换 Flag（管理员，仅支持已知开关）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_db
from app.core.feature_flags import KNOWN_FLAGS, feature_enabled, set_feature_flag
from app.core.jwt_utils import get_current_user, is_admin_user

router = APIRouter(prefix="/api/p7", tags=["P7 灰度"])

# 已知 Feature Flag（V4 Phase H 清理后仅保留仍在使用的开关）
_FLAGS = [
    "wiki_topic_enabled",
    "wiki_pipeline_default_enabled",
    "source_hub_enabled",
    "dingtalk_connector_enabled",
    "gitlab_connector_enabled",
]

assert set(_FLAGS) == KNOWN_FLAGS, "Feature Flag API 必须暴露全部已知开关"


class FlagTogglePayload(BaseModel):
    flag: str
    value: bool


def _require_admin(current_user: dict) -> None:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")


@router.get("/flags")
def get_flags(
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    return {"flags": {flag: feature_enabled(db, flag) for flag in _FLAGS}}


@router.post("/flags")
def toggle_flag(
    payload: FlagTogglePayload,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    _require_admin(current_user)
    if payload.flag not in _FLAGS:
        raise HTTPException(status_code=400, detail=f"未知 Flag: {payload.flag}")
    value = set_feature_flag(
        db, payload.flag, payload.value, current_user.get("id")
    )
    return {"flag": payload.flag, "value": value}
