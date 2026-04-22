from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Dict, Any
from collections import Counter, defaultdict

from app.models.database import Page, GraphEdge, Notebook, PageChunk, get_session, get_engine, init_db
from app.models.schema import GraphDataResponse, GraphNodeResponse, GraphEdgeResponse, GraphStatsResponse
from app.core.rag import EmbeddingService
from app.core.graph import GraphBuilder
from app.core.jwt_utils import get_current_user
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


def _get_visible_pages(db, current_user):
    if "__local_admin__" in current_user["groups"]:
        return db.query(Page).all()
    visible_nb_ids = db.query(Notebook.id).filter(
        or_(Notebook.group_id.in_(current_user["groups"]), Notebook.group_id.is_(None))
    ).subquery()
    return db.query(Page).filter(Page.notebook_id.in_(visible_nb_ids)).all()


@router.get("/data")
async def get_graph_data(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    pages = _get_visible_pages(db, current_user)
    visible_ids = {p.id for p in pages}
    edges = db.query(GraphEdge).filter(
        GraphEdge.source_id.in_(visible_ids)
    ).all()
    edges = [e for e in edges if e.target_id in visible_ids]

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
async def get_graph_stats(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    pages = _get_visible_pages(db, current_user)
    visible_ids = {p.id for p in pages}
    total_nodes = len(pages)
    all_edges = db.query(GraphEdge).filter(
        GraphEdge.source_id.in_(visible_ids)
    ).all()
    edges = [e for e in all_edges if e.target_id in visible_ids]
    total_edges = len(edges)
    avg_conn = (total_edges * 2 / total_nodes) if total_nodes > 0 else 0.0

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

    clusters = 0
    for pid in visible_ids:
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
async def rebuild_graph(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    pages = _get_visible_pages(db, current_user)
    if not pages:
        return {"message": "没有笔记，跳过构建"}

    builder = GraphBuilder()
    count = builder.build_graph(pages, db)
    return {"message": f"图谱构建完成，共 {count} 条边"}
