from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from collections import defaultdict
import re
import math

from app.core.rag import EmbeddingService, VectorStore
from app.core.graph import GraphBuilder
from app.models.database import Page, GraphEdge, get_session, get_engine, init_db
from app.models.schema import EnhancedSearchResult, EnhancedSearchResponse
from app.config import settings

router = APIRouter(prefix="/api/search", tags=["搜索"])

_embedding_service = None
_vector_store = None
_engine = None
_session = None

def get_rag_services():
    global _embedding_service, _vector_store
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    if _vector_store is None:
        _vector_store = VectorStore()
    return _embedding_service, _vector_store

def get_db():
    global _engine, _session
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5


@router.post("")
async def search(request: SearchRequest, rag=Depends(get_rag_services), db=Depends(get_db)):
    embedding_service, vector_store = rag

    try:
        query_embedding = await embedding_service.encode(request.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding失败: {str(e)}")

    scores: Dict[str, Dict[str, Any]] = {}

    try:
        vec_results = await vector_store.search(query_embedding, request.top_k * 3)
        for i, (doc_id, meta, dist) in enumerate(zip(
            vec_results.get("ids", []),
            vec_results.get("metadatas", []),
            vec_results.get("distances", []),
        )):
            page_id = meta.get("page_id", doc_id) if meta else doc_id
            if page_id not in scores:
                scores[page_id] = {"title": "", "content": "", "score": 0.0, "sources": set()}
            sim = 1 - dist if dist else 0
            scores[page_id]["score"] += sim * 3.0
            scores[page_id]["sources"].add("vector")
    except Exception:
        pass

    try:
        query_kw = GraphBuilder.extract_keywords(request.query, 10)
        pages = db.query(Page).all()
        for p in pages:
            text = (p.title or "") + " " + (p.content or "")
            page_kw = GraphBuilder.extract_keywords(text, 20)
            overlap = query_kw & page_kw
            if overlap:
                kw_score = len(overlap) / max(len(query_kw), 1)
                if p.id not in scores:
                    scores[p.id] = {"title": p.title, "content": p.content or "", "score": 0.0, "sources": set()}
                title_bonus = 0.5 if any(kw in (p.title or "") for kw in overlap) else 0.0
                scores[p.id]["score"] += (kw_score + title_bonus) * 2.0
                scores[p.id]["sources"].add("keyword")
                scores[p.id]["title"] = scores[p.id]["title"] or p.title or ""
                scores[p.id]["content"] = scores[p.id]["content"] or p.content or ""
    except Exception:
        pass

    graph_expanded = 0
    try:
        seed_ids = list(scores.keys())
        edges = db.query(GraphEdge).all()
        adj: Dict[str, List[tuple]] = defaultdict(list)
        for e in edges:
            adj[e.source_id].append((e.target_id, float(e.weight)))
            adj[e.target_id].append((e.source_id, float(e.weight)))

        pages_map = {p.id: p for p in db.query(Page).all()}

        for seed_id in seed_ids:
            for neighbor_id, edge_weight in adj.get(seed_id, []):
                decay = 0.5
                if neighbor_id not in scores:
                    p = pages_map.get(neighbor_id)
                    if p:
                        scores[neighbor_id] = {
                            "title": p.title or "",
                            "content": (p.content or "")[:200],
                            "score": 0.0,
                            "sources": set(),
                        }
                    graph_expanded += 1
                if neighbor_id in scores:
                    seed_score = scores[seed_id]["score"]
                    scores[neighbor_id]["score"] += seed_score * decay * edge_weight
                    scores[neighbor_id]["sources"].add("graph")
    except Exception:
        pass

    sorted_results = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    top_results = sorted_results[:request.top_k]

    results = []
    for page_id, data in top_results:
        results.append(EnhancedSearchResult(
            id=page_id,
            title=data["title"],
            content=data["content"][:300] if data["content"] else "",
            score=round(data["score"], 4),
            source="+".join(sorted(data["sources"])) if data["sources"] else "unknown",
        ))

    return EnhancedSearchResponse(
        results=results,
        total=len(results),
        graph_expanded=graph_expanded,
    )
