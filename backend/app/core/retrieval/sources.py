"""检索适配器。

- retrieve_pages：复用现有 VectorStore 检索 Page
- RetrievedKnowledge：知识审判层 DTO（不再从 knowledge_objects 表召回）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.core.rag import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class RetrievedKnowledge:
    """审判层候选。字段名保留兼容 KnowledgeJudge / 既有测试。"""
    card_id: str
    title: str
    body: str
    type: str
    scope: dict
    confidence: float
    status: str
    source_page_id: Optional[str]
    score: float
    valid_to: Optional[str] = None
    superseded_by: Optional[str] = None
    evidence_ids: list[str] = None
    structured: str = "{}"
    source_mime_type: Optional[str] = None
    source_locator: Optional[str] = None


async def retrieve_pages(
    db: Session,
    *,
    question: str,
    top_k: int = 10,
    embedding_service=None,
    vector_store: Optional[VectorStore] = None,
    visible_page_ids: Optional[set[str]] = None,
) -> list[dict]:
    """辅助检索 Page：复用 VectorStore.search。"""
    if embedding_service is None:
        raise ValueError("retrieve_pages 需要 embedding_service（测试可注入 mock）")
    if visible_page_ids is not None and not visible_page_ids:
        return []

    query_emb = await embedding_service.encode(question)
    store = vector_store or VectorStore(db)
    results = await store.search(query_emb, top_k)
    filtered = [
        item for item in results
        if visible_page_ids is None or item["page_id"] in visible_page_ids
    ]
    return [
        {
            "page_id": item["page_id"],
            "content": item.get("content") or "",
            "distance": item.get("distance", 1.0),
        }
        for item in filtered
    ]
