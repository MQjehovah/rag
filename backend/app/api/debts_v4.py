"""V4 Phase F 知识债务 API（新端点，不依赖 Card/KO/owner/review）。

- GET /api/v4/debts → 当前用户可见债务（V4 字段，不返回 owner/Card/审核）。
- visible_scope_ids：普通用户 = company + 所属业务组；管理员 = 全部。
- 显式 scope 过滤需校验权限；跨组不得泄露记录存在性。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.core.retrieval.debt_service import (
    list_debts_for_scopes,
    validated_scope,
    visible_scope_ids,
)
from app.models.database import Notebook

router = APIRouter(prefix="/api/v4/debts", tags=["V4 知识债务"])


def _minimal_debt_view(debt) -> dict:
    """J-4 极简展示：只返回标题与状态，不泄露次数/人数/scope/时间/用户身份。"""
    title = (debt.original_query or debt.related_question or debt.normalized_query or "").strip()
    return {"id": debt.id, "title": title, "status": debt.status}


@router.get("/scopes")
def list_scopes(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """返回当前用户可选 scope（供前端 scope 选择）。"""
    scopes = visible_scope_ids(current_user)
    if scopes is None:
        # 管理员：返回全部可选（company + 全部 group，含 admin 特殊项）
        from app.core import access_control
        all_scopes = ["company"]
        groups = set()
        for row in db.query(Notebook.group_id).distinct().all():
            if row[0] and row[0] not in ("__public__", "__local_admin__"):
                groups.add(row[0])
        for g in sorted(groups):
            all_scopes.append(f"group:{g}")
        all_scopes.append("admin")
        return {"scopes": [{"value": s, "label": _scope_label(s)} for s in all_scopes]}
    return {"scopes": [{"value": s, "label": _scope_label(s)} for s in scopes]}


def _scope_label(scope_id: str) -> str:
    if scope_id == "company":
        return "全公司"
    if scope_id == "admin":
        return "管理员"
    if scope_id.startswith("group:"):
        return f"组：{scope_id[6:]}"
    return scope_id


@router.get("")
def list_debts(
    status: Optional[str] = "open",
    scope: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """返回当前用户可见范围的债务（V4 字段，fail closed）。"""
    scopes = visible_scope_ids(current_user)

    scope_filter = None
    if scope is not None:
        # 显式 scope 过滤：校验权限（管理员可任意，普通用户只能选可见 scope）
        if scopes is None:
            # 管理员
            scope_filter = scope
        else:
            if scope not in scopes:
                raise HTTPException(status_code=403, detail="无权访问该权限域")
            scope_filter = scope

    debts = list_debts_for_scopes(
        db, visible_scope_ids=scopes, status=status, scope_filter=scope_filter, limit=limit
    )
    # J-4 极简展示：只返回标题与状态（展示去重/折叠由前端处理，不修改真实数据）。
    return {"debts": [_minimal_debt_view(d) for d in debts]}
