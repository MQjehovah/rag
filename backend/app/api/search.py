from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict, Any

from app.core.rag import EmbeddingService, VectorStore

router = APIRouter(prefix="/api/search", tags=["搜索"])

_embedding_service = None
_vector_store = None

def get_rag_services():
    global _embedding_service, _vector_store
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    if _vector_store is None:
        _vector_store = VectorStore()
    return _embedding_service, _vector_store

class SearchRequest(BaseModel):
    query: str
    top_k: int = 5

class SearchResult(BaseModel):
    id: str
    title: str
    content: str
    score: float

@router.post("")
async def search(request: SearchRequest, rag = Depends(get_rag_services)):
    """向量搜索"""
    embedding_service, vector_store = rag
    
    try:
        query_embedding = await embedding_service.encode(request.query)
        results = await vector_store.search(query_embedding, request.top_k)
        
        search_results = []
        for i, (doc, meta, dist) in enumerate(zip(
            results.get("documents", []),
            results.get("metadatas", []),
            results.get("distances", [])
        )):
            lines = doc.split('\n', 1)
            title = meta.get("title", "") if meta else (lines[0] if lines else "")
            content = lines[1] if len(lines) > 1 else doc
            
            search_results.append({
                "id": results["ids"][i],
                "title": title,
                "content": content[:200],
                "score": 1 - dist if dist else 0
            })
        
        return {"results": search_results, "total": len(search_results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")