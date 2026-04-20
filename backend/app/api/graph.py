from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from collections import Counter, defaultdict
import math

from app.models.database import Page, GraphEdge, get_session, get_engine, init_db
from app.models.schema import GraphDataResponse, GraphNodeResponse, GraphEdgeResponse, GraphStatsResponse
from app.core.rag import EmbeddingService, VectorStore
from app.core.graph import GraphBuilder
from app.config import settings

router = APIRouter(prefix="/api/graph", tags=["知识图谱"])

_engine = None
_session = None

def get_db():
    global _engine, _session
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session


@router.get("/data")
async def get_graph_data(db: Session = Depends(get_db)):
    pages = db.query(Page).all()
    edges = db.query(GraphEdge).all()

    edge_map: Dict[str, int] = Counter()
    for e in edges:
        edge_map[e.source_id] += 1
        edge_map[e.target_id] += 1

    nodes = []
    for p in pages:
        nodes.append(GraphNodeResponse(
            id=p.id,
            title=p.title or "无标题",
            notebook_id=p.notebook_id,
            link_count=edge_map.get(p.id, 0),
        ))

    edge_responses = []
    for e in edges:
        edge_responses.append(GraphEdgeResponse(
            id=e.id,
            source_id=e.source_id,
            target_id=e.target_id,
            weight=float(e.weight),
            edge_type=e.edge_type,
        ))

    return GraphDataResponse(nodes=nodes, edges=edge_responses)


@router.get("/stats")
async def get_graph_stats(db: Session = Depends(get_db)):
    total_nodes = db.query(Page).count()
    total_edges = db.query(GraphEdge).count()
    avg_conn = (total_edges * 2 / total_nodes) if total_nodes > 0 else 0.0

    edges = db.query(GraphEdge).all()
    visited: set = set()
    adj: Dict[str, List[str]] = defaultdict(list)
    for e in edges:
        adj.setdefault(e.source_id, []).append(e.target_id)
        adj.setdefault(e.target_id, []).append(e.source_id)

    def dfs(node: str):
        stack = [node]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            for nb in adj.get(n, []):
                if nb not in visited:
                    stack.append(nb)

    all_page_ids = {p.id for p in db.query(Page).all()}
    clusters = 0
    for pid in all_page_ids:
        if pid not in visited:
            dfs(pid)
            clusters += 1

    return GraphStatsResponse(
        total_nodes=total_nodes,
        total_edges=total_edges,
        avg_connections=round(avg_conn, 2),
        clusters=clusters,
    )


@router.post("/rebuild")
async def rebuild_graph(db: Session = Depends(get_db)):
    pages = db.query(Page).all()
    if not pages:
        return {"message": "没有笔记，跳过构建"}

    embedding_service = EmbeddingService()
    embeddings: Dict[str, List[float]] = {}
    for p in pages:
        try:
            text = (p.title or "") + " " + (p.content or "")
            if text.strip():
                emb = await embedding_service.encode(text)
                if emb:
                    embeddings[p.id] = emb
        except Exception:
            pass

    await embedding_service.close()

    builder = GraphBuilder()
    count = builder.build_graph(pages, embeddings, db)
    return {"message": f"图谱构建完成，共 {count} 条边"}
