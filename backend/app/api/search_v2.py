"""搜索 API（Phase G 默认检索，V4 计划 5.6/6.3）。

默认 Search 只检索 Wiki、Page、Chunk/Evidence，不查询 Card/KO：
- Wiki：status=published 且 current Revision 归属合法的 Wiki 主题页；
- Raw：可见/有效 Page 的 PageChunk（BM25 + 可选 Dense/Rerank 降级）。

scope 与 ACL 全链路生效：选定 scope 通过 `_scope_override` 约束 Wiki 与 Raw 检索，
返回 Wiki 页面链接或原始来源链接，不通过标题/摘要/计数/引用泄露其他 scope 内容。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.core.retrieval.degradation import build_raw_view, build_wiki_view
from app.core.retrieval.raw_retriever import RawDocumentRetriever
from app.core.retrieval.wiki_retriever import retrieve_wiki
from app.api.rag_chat import resolve_scope

router = APIRouter(prefix="/api/search", tags=["知识搜索"])

logger = logging.getLogger(__name__)


class SearchV2Request(BaseModel):
    question: str
    scope_id: Optional[str] = None
    top_k: int = 5


def run_search_query(
    db: Session,
    current_user: dict,
    question: str,
    scope_id: str | None,
    top_k: int = 5,
) -> dict:
    """默认 Search 服务（J-1 自动权限）：Wiki 优先 → Raw Chunk，不查询 Card/KO。

    - scope_id 缺省 → 自动全可见范围；提供则校验越权（伪造不扩大权限）。
    - 返回的 scope_id 为 None 表示自动全范围（供前端展示，不用于权限）。
    """
    resolved = resolve_scope(current_user, scope_id)
    scoped_user = {**current_user, "_scope_override": resolved} if resolved else current_user

    # Wiki 检索（published + Revision 归属合法，先经 ACL）
    wiki_result = retrieve_wiki(db, scoped_user, question, top_k=top_k)

    # Raw 检索（Page/Chunk，BM25 + 可选 Dense/Rerank 降级；不查 Card/KO）
    raw_result = RawDocumentRetriever(db).retrieve(
        db, question, scoped_user, retrieval_round=1
    )

    wiki_view = build_wiki_view(db, wiki_result.hits)
    raw_view = build_raw_view(db, raw_result.hits)

    return {
        "scope_id": resolved,
        "wiki_results": wiki_view,
        "raw_results": raw_view,
        "total": len(wiki_view) + len(raw_view),
        "degraded_reasons": list(raw_result.degraded),
    }


@router.post("/v2")
async def search_v2(
    request: SearchV2Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """默认 Search：Wiki / Page / Chunk / Evidence，不查询 Card/KO。"""
    return run_search_query(
        db, current_user, request.question, request.scope_id, top_k=request.top_k
    )
