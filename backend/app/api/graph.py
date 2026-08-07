from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Dict, Any
from collections import Counter, defaultdict
import logging

from app.models.database import Page, GraphEdge, GraphEntity, GraphEntityEdge, Notebook
from app.models.schema import GraphDataResponse, GraphNodeResponse, GraphEdgeResponse, GraphStatsResponse
from app.core.rag import EmbeddingService
from app.core.graph import GraphBuilder
from app.core.entity_graph import EntityGraphStore
from app.api.deps import get_db
from app.core.jwt_utils import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/graph", tags=["知识图谱"])

logger = logging.getLogger(__name__)

def _get_visible_pages(db, current_user):
    if "__local_admin__" in current_user["groups"]:
        return db.query(Page).all()
    visible_nb_ids = db.query(Notebook.id).filter(
        or_(Notebook.group_id.in_(current_user["groups"]), Notebook.group_id.is_(None))
    ).subquery()
    return db.query(Page).filter(
        or_(Page.notebook_id.is_(None), Page.notebook_id.in_(visible_nb_ids))
    ).all()


@router.get("/data")
def get_graph_data(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
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

    # ---- entity-level graph (LightRAG-style) ----
    entity_nodes = []
    entity_edges = []
    page_entity_links = []
    if visible_ids:
        visible_list = list(visible_ids)
        entity_rows = db.query(GraphEntity).filter(
            GraphEntity.page_id.in_(visible_list)
        ).all()
        ent_by_id = {e.id: e for e in entity_rows}

        entity_edge_rows = db.query(GraphEntityEdge).filter(
            GraphEntityEdge.page_id.in_(visible_list)
        ).all()
        for ee in entity_edge_rows:
            for eid in (ee.source_entity_id, ee.target_entity_id):
                if eid not in ent_by_id:
                    row = db.query(GraphEntity).filter(GraphEntity.id == eid).first()
                    if row:
                        ent_by_id[row.id] = row

        ent_degree: Dict[str, int] = Counter()
        for ee in entity_edge_rows:
            ent_degree[ee.source_entity_id] += 1
            ent_degree[ee.target_entity_id] += 1

        for e in ent_by_id.values():
            entity_nodes.append(GraphNodeResponse(
                id=f"entity-{e.id}",
                title=e.name or "未命名实体",
                notebook_id=None,
                link_count=ent_degree[e.id] + (1 if e.page_id in visible_ids else 0),
                kind="entity",
                entity_type=e.entity_type,
            ))

        for ee in entity_edge_rows:
            entity_edges.append(GraphEdgeResponse(
                id=f"er-{ee.id}",
                source_id=f"entity-{ee.source_entity_id}",
                target_id=f"entity-{ee.target_entity_id}",
                weight=float(ee.weight),
                edge_type="entity_relation",
                label=ee.relation or "",
            ))

        for e in ent_by_id.values():
            if e.page_id in visible_ids:
                page_entity_links.append(GraphEdgeResponse(
                    id=f"pe-{e.id}",
                    source_id=e.page_id,
                    target_id=f"entity-{e.id}",
                    weight=1.0,
                    edge_type="page_entity",
                    label="提及",
                ))

    return GraphDataResponse(
        nodes=nodes + entity_nodes,
        edges=edge_responses + entity_edges + page_entity_links,
    )


@router.get("/stats")
def get_graph_stats(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
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

    if visible_ids:
        visible_list = list(visible_ids)
        total_entities = db.query(GraphEntity).filter(
            GraphEntity.page_id.in_(visible_list)
        ).count()
        total_relations = db.query(GraphEntityEdge).filter(
            GraphEntityEdge.page_id.in_(visible_list)
        ).count()
    else:
        total_entities = 0
        total_relations = 0

    return GraphStatsResponse(
        total_nodes=total_nodes,
        total_edges=total_edges,
        avg_connections=round(avg_conn, 2),
        clusters=clusters,
        total_entities=total_entities,
        total_relations=total_relations,
    )


@router.post("/rebuild")
def rebuild_graph(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    pages = _get_visible_pages(db, current_user)
    if not pages:
        return {"message": "没有笔记，跳过构建"}

    builder = GraphBuilder()
    count = builder.build_graph(pages, db)
    return {"message": f"图谱构建完成，共 {count} 条边"}


@router.post("/rebuild-entities")
async def rebuild_entities(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")

    pages = _get_visible_pages(db, current_user)
    store = EntityGraphStore(db)
    total = 0
    errors = 0
    for p in pages:
        try:
            total += await store.extract_and_store(p.id, p.title, p.content)
        except Exception as e:
            errors += 1
            logger.warning(f"Entity extraction failed for {p.id}: {e}")
    return {
        "message": f"实体图谱构建完成：{total} 个实体/关系，失败 {errors} 页",
        "entities": total,
        "total_pages": len(pages),
        "errors": errors,
    }
