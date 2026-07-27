from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from collections import defaultdict
from sqlalchemy import or_
from sqlalchemy.orm import Session
import logging

from app.core.rag import EmbeddingService, VectorStore, RerankerService
from app.models.database import Page, GraphEdge, Notebook, get_session, get_engine, init_db
from app.models.schema import EnhancedSearchResult, EnhancedSearchResponse
from app.core.jwt_utils import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/search", tags=["搜索"])

_engine = None
_session = None
_reranker = None

logger = logging.getLogger(__name__)

def get_db():
    global _engine, _session
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session

def get_reranker():
    global _reranker
    if _reranker is None:
        _reranker = RerankerService()
    return _reranker

def _get_visible_page_ids(db, current_user) -> set:
    if "__local_admin__" in current_user["groups"]:
        return set(p[0] for p in db.query(Page.id).all())
    visible_nb_ids = db.query(Notebook.id).filter(
        or_(Notebook.group_id.in_(current_user["groups"]), Notebook.group_id.is_(None))
    ).subquery()
    return set(p[0] for p in db.query(Page.id).filter(Page.notebook_id.in_(visible_nb_ids)).all())


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5


@router.post("")
async def search(request: SearchRequest, db: Session = Depends(get_db), reranker_svc=Depends(get_reranker), current_user=Depends(get_current_user)):
    embedding_service = EmbeddingService()

    try:
        query_embedding = await embedding_service.encode(request.query)
    except Exception as e:
        await embedding_service.close()
        raise HTTPException(status_code=500, detail=f"Embedding失败: {str(e)}")

    visible_ids = _get_visible_page_ids(db, current_user)

    vec_scores: Dict[str, float] = {}
    vec_store = VectorStore(db)
    try:
        vec_results = await vec_store.search(query_embedding, settings.vector_recall_k)
        for item in vec_results:
            page_id = item["page_id"]
            if page_id not in visible_ids:
                continue
            sim = 1.0 - item["distance"]
            if sim < 0.35:
                continue
            if page_id not in vec_scores or sim > vec_scores[page_id]:
                vec_scores[page_id] = sim
    except Exception as e:
        logger.warning(f"Vector search error: {e}")

    kw_scores: Dict[str, float] = {}
    content_snippets: Dict[str, str] = {}
    try:
        query_kw = EmbeddingService.extract_keywords(request.query, 10, fine_grained=True)
        if query_kw:
            kw_like_conditions = []
            params = {}
            for i, kw in enumerate(query_kw):
                kw_like_conditions.append(f"keywords LIKE :kw{i}")
                params[f"kw{i}"] = f"%{kw}%"

            if kw_like_conditions:
                from sqlalchemy import text as sql_text
                where_clause = " OR ".join(kw_like_conditions)
                if visible_ids:
                    placeholders = ",".join([f":vid{i}" for i in range(len(visible_ids))])
                    for i, vid in enumerate(visible_ids):
                        params[f"vid{i}"] = vid
                    where_clause = f"({where_clause}) AND id IN ({placeholders})"

                result = db.execute(
                    sql_text(f"SELECT id, title, content, keywords FROM pages WHERE {where_clause}"),
                    params
                )
                for row in result.fetchall():
                    pid = row[0]
                    if pid not in visible_ids:
                        continue
                    page_kw_str = row[3] or ""
                    page_kw = set(page_kw_str.split(",")) if page_kw_str else set()
                    overlap = set()
                    for qkw in query_kw:
                        for pkw in page_kw:
                            if qkw in pkw or pkw in qkw:
                                overlap.add(qkw)
                                break
                    if overlap:
                        kw_score = len(overlap) / max(len(query_kw), 1)
                        title_bonus = 0.3 if any(kw in (row[1] or "") for kw in overlap) else 0.0
                        kw_scores[pid] = min(kw_score + title_bonus, 1.0)
                        content_snippets[pid] = (row[2] or "")[:300]
    except Exception as e:
        logger.warning(f"Keyword search error: {e}")

    candidate_ids = set(vec_scores.keys()) | set(kw_scores.keys())
    if not candidate_ids:
        await embedding_service.close()
        return EnhancedSearchResponse(results=[], total=0, graph_expanded=0)

    for pid in vec_scores:
        if pid not in content_snippets:
            content_snippets[pid] = ""

    candidate_pages = db.query(Page).filter(Page.id.in_(list(candidate_ids))).all()
    page_map = {p.id: p for p in candidate_pages}

    rr_scores: Dict[str, float] = {}
    rerank_candidates = []
    for pid in candidate_ids:
        p = page_map.get(pid)
        if p:
            rerank_candidates.append({
                "id": pid,
                "text": (p.title or "") + " " + (p.content or "")[:500],
            })

    if rerank_candidates and len(rerank_candidates) > 1:
        try:
            docs = [c["text"] for c in rerank_candidates]
            rerank_results = await reranker_svc.rerank(request.query, docs, top_k=request.top_k * 2)
            for r in rerank_results:
                idx = r.get("index", 0)
                if idx < len(rerank_candidates):
                    pid = rerank_candidates[idx]["id"]
                    rr_scores[pid] = r.get("relevance_score", 0.0)
        except Exception as e:
            logger.warning(f"Reranker error: {e}")

    scores: Dict[str, Dict[str, Any]] = {}
    W_VEC, W_KW, W_RR = 1.0, 1.5, 5.0
    for pid in candidate_ids:
        v = vec_scores.get(pid, 0.0)
        k = kw_scores.get(pid, 0.0)
        r = rr_scores.get(pid, 0.0)
        final = v * W_VEC + k * W_KW + r * W_RR
        sources = set()
        if v > 0:
            sources.add("vector")
        if k > 0:
            sources.add("keyword")
        if r > 0:
            sources.add("reranker")
        scores[pid] = {"score": final, "sources": sources, "content_snippet": content_snippets.get(pid, "")}

    graph_expanded = 0
    try:
        seed_ids = list(scores.keys())[:20]
        if seed_ids and len(seed_ids) > 1:
            edges = db.query(GraphEdge).filter(
                (GraphEdge.source_id.in_(seed_ids)) | (GraphEdge.target_id.in_(seed_ids))
            ).all()
            adj: Dict[str, List[tuple]] = defaultdict(list)
            for e in edges:
                adj[e.source_id].append((e.target_id, float(e.weight)))
                adj[e.target_id].append((e.source_id, float(e.weight)))

            for seed_id in seed_ids:
                for neighbor_id, edge_weight in adj.get(seed_id, []):
                    if neighbor_id in visible_ids and neighbor_id not in scores:
                        p = page_map.get(neighbor_id)
                        if p is None:
                            p = db.query(Page).filter(Page.id == neighbor_id).first()
                        if p:
                            scores[neighbor_id] = {
                                "score": scores[seed_id]["score"] * 0.5 * edge_weight,
                                "sources": {"graph"},
                                "content_snippet": (p.content or "")[:200],
                            }
                            page_map[neighbor_id] = p
                            graph_expanded += 1
    except Exception as e:
        logger.warning(f"Graph expansion error: {e}")

    sorted_results = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
    top_results = sorted_results[:request.top_k]

    results = []
    for page_id, data in top_results:
        p = page_map.get(page_id)
        title = p.title if p else ""
        content = data.get("content_snippet", (p.content or "")[:300] if p else "")
        results.append(EnhancedSearchResult(
            id=page_id,
            title=title,
            content=content[:300],
            score=round(data["score"], 4),
            source="+".join(sorted(data["sources"])) if data["sources"] else "unknown",
        ))

    await embedding_service.close()
    return EnhancedSearchResponse(
        results=results,
        total=len(results),
        graph_expanded=graph_expanded,
    )
