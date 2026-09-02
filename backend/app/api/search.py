"""搜索 API（V4 Phase G）。

`/api/search` 与 `/api/search/v2` 共用同一个新服务 `run_search_query`（Wiki + Page/Chunk，
不查询 Card/KO）。旧的 EmbeddingService/VectorStore/Reranker Search 不再作为默认可达接口。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.api.search_v2 import run_search_query

router = APIRouter(prefix="/api/search", tags=["搜索"])


class SearchRequest(BaseModel):
    query: str
    scope_id: Optional[str] = None
    top_k: int = 5


@router.post("")
async def search(
    request: SearchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """默认 Search：复用新 Wiki + Page/Chunk 服务，scope 校验在检索前完成。"""
    return run_search_query(
        db, current_user, request.query, request.scope_id, top_k=request.top_k
    )
