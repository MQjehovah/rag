from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
import logging

from app.core.rag import EmbeddingService, RerankerService
from app.models.schema import EnhancedSearchResult, EnhancedSearchResponse
from app.api.deps import get_db
from app.core.retrieval import RetrievalPipeline
from app.core.jwt_utils import get_current_user

router = APIRouter(prefix="/api/search", tags=["搜索"])

_reranker = None
_embedding_service = None

logger = logging.getLogger(__name__)

def get_reranker():
    global _reranker
    if _reranker is None:
        _reranker = RerankerService()
    return _reranker


def get_embedding_service():
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service

class SearchRequest(BaseModel):
    query: str
    top_k: int = 5


@router.post("")
async def search(request: SearchRequest, db: Session = Depends(get_db), reranker_svc=Depends(get_reranker), current_user=Depends(get_current_user)):
    pipeline = RetrievalPipeline(
        db,
        embedding_svc=get_embedding_service(),
        reranker_svc=reranker_svc,
    )
    outcome = await pipeline.retrieve(request.query, current_user, top_k=request.top_k)

    results = []
    for r in outcome["results"]:
        results.append(EnhancedSearchResult(
            id=r["id"],
            title=r["title"],
            content=(r["content"] or "")[:300],
            score=r["score"],
            source="+".join(r["sources"]) if r["sources"] else "unknown",
            chunks=r["chunks"],
        ))

    return EnhancedSearchResponse(
        results=results,
        total=len(results),
        graph_expanded=outcome["graph_expanded"],
    )
