import asyncio
import json

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Dict, Any
from collections import Counter, defaultdict
import logging

from app.models.database import Page, GraphEdge, GraphEntity, GraphEntityEdge, GraphCommunity, Notebook, get_engine, get_session, init_db
from app.models.schema import GraphDataResponse, GraphNodeResponse, GraphEdgeResponse, GraphStatsResponse
from app.core.rag import EmbeddingService
from app.core.graph import GraphBuilder
from app.core.entity_graph import EntityGraphStore
from app.core.graphrag import rebuild_communities, sync_image_assets
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
def get_graph_data(
    view: str = "pages",
    max_nodes: int = 400,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return a capped, importance-ranked slice of the graph.

    Rendering thousands of nodes/edges in the browser is slow, so we keep the
    most-connected ``max_nodes`` nodes per view and only the edges between
    them.
    """
    view = view if view in ("pages", "entities") else "pages"
    max_nodes = max(20, min(max_nodes, 1500))

    pages = _get_visible_pages(db, current_user)
    visible_ids = {p.id for p in pages}

    if view == "pages":
        all_edges = db.query(GraphEdge).filter(
            GraphEdge.source_id.in_(visible_ids)
        ).all()
        all_edges = [e for e in all_edges if e.target_id in visible_ids]

        degree: Dict[str, int] = Counter()
        for e in all_edges:
            degree[e.source_id] += 1
            degree[e.target_id] += 1

        ranked = sorted(visible_ids, key=lambda pid: degree.get(pid, 0), reverse=True)
        keep_ids = set(ranked[:max_nodes])

        nodes = []
        for p in pages:
            if p.id not in keep_ids:
                continue
            nodes.append(GraphNodeResponse(
                id=p.id,
                title=p.title or "无标题",
                notebook_id=p.notebook_id,
                link_count=degree.get(p.id, 0),
                kind="page",
            ))

        keep_edges = [e for e in all_edges if e.source_id in keep_ids and e.target_id in keep_ids]
        keep_edges.sort(key=lambda e: float(e.weight), reverse=True)
        keep_edges = keep_edges[: max(2000, max_nodes * 8)]

        edge_responses = []
        for e in keep_edges:
            edge_responses.append(GraphEdgeResponse(
                id=e.id,
                source_id=e.source_id,
                target_id=e.target_id,
                weight=float(e.weight),
                edge_type=e.edge_type,
            ))
        return GraphDataResponse(nodes=nodes, edges=edge_responses)

    # ---- entity view ----
    visible_list = list(visible_ids)
    entity_rows = db.query(GraphEntity).filter(
        GraphEntity.page_id.in_(visible_list)
    ).all()
    ent_by_id = {e.id: e for e in entity_rows}

    relation_rows = db.query(GraphEntityEdge).filter(
        GraphEntityEdge.page_id.in_(visible_list)
    ).all()
    for ee in relation_rows:
        for eid in (ee.source_entity_id, ee.target_entity_id):
            if eid not in ent_by_id:
                row = db.query(GraphEntity).filter(GraphEntity.id == eid).first()
                if row:
                    ent_by_id[row.id] = row

    degree = Counter()
    for ee in relation_rows:
        degree[ee.source_entity_id] += 1
        degree[ee.target_entity_id] += 1

    ent_community: Dict[str, str] = {}
    for cid, member_json in db.query(GraphCommunity.id, GraphCommunity.member_ids).all():
        try:
            for eid in json.loads(member_json or "[]"):
                ent_community[eid] = cid
        except Exception:
            pass

    ranked = sorted(ent_by_id, key=lambda eid: degree.get(eid, 0), reverse=True)
    keep_ent = set(ranked[:max_nodes])

    linked_page_ids = set()
    for ee in relation_rows:
        if ee.page_id in visible_ids and (ee.source_entity_id in keep_ent or ee.target_entity_id in keep_ent):
            linked_page_ids.add(ee.page_id)
    for ent in ent_by_id.values():
        if ent.id in keep_ent and ent.page_id in visible_ids:
            linked_page_ids.add(ent.page_id)
    keep_pages = set(list(linked_page_ids)[:120])
    page_by_id = {p.id: p for p in pages}

    nodes = []
    for eid in ranked[:max_nodes]:
        e = ent_by_id[eid]
        nodes.append(GraphNodeResponse(
            id=f"entity-{e.id}",
            title=e.name or "未命名实体",
            notebook_id=None,
            link_count=degree.get(e.id, 0),
            kind="entity",
            entity_type=e.entity_type,
            community=ent_community.get(e.id),
        ))
    for pid in keep_pages:
        p = page_by_id.get(pid)
        if p is None:
            continue
        nodes.append(GraphNodeResponse(
            id=pid,
            title=p.title or "无标题",
            notebook_id=p.notebook_id,
            link_count=0,
            kind="page",
        ))

    edges = []
    for ee in relation_rows:
        if ee.source_entity_id in keep_ent and ee.target_entity_id in keep_ent:
            edges.append(GraphEdgeResponse(
                id=f"er-{ee.id}",
                source_id=f"entity-{ee.source_entity_id}",
                target_id=f"entity-{ee.target_entity_id}",
                weight=float(ee.weight),
                edge_type="entity_relation",
                label=ee.relation or "",
            ))
    for eid in keep_ent:
        ent = ent_by_id[eid]
        if ent.page_id in keep_pages:
            edges.append(GraphEdgeResponse(
                id=f"pe-{ent.id}",
                source_id=ent.page_id,
                target_id=f"entity-{ent.id}",
                weight=1.0,
                edge_type="page_entity",
                label="提及",
            ))

    return GraphDataResponse(nodes=nodes, edges=edges)


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
            # Commit per page so the write transaction is not held across the
            # next LLM call (that would lock SQLite for the whole rebuild).
            db.commit()
        except Exception as e:
            errors += 1
            logger.warning(f"Entity extraction failed for {p.id}: {e}")
    return {
        "message": f"实体图谱构建完成：{total} 个实体/关系，失败 {errors} 页",
        "entities": total,
        "total_pages": len(pages),
        "errors": errors,
    }


_graphrag_status = {
    "running": False,
    "phase": "",
    "processed": 0,
    "total": 0,
    "message": "",
}
_graphrag_task: asyncio.Task | None = None


async def _complete_entities(engine, status):
    db = get_session(engine)
    try:
        pages = db.query(Page.id, Page.title, Page.content).all()
        todo = []
        for pid, title, content in pages:
            cnt = db.query(GraphEntity.id).filter(GraphEntity.page_id == pid).count()
            if cnt == 0 and (content or "").strip():
                todo.append((pid, title, content))
    finally:
        db.close()
    status["phase"] = "entity-extract"
    status["total"] = len(todo)
    status["processed"] = 0
    if not todo:
        status["message"] = "实体已全部抽取"
        return
    sem = asyncio.Semaphore(3)
    done = 0

    async def work(pid, title, content):
        nonlocal done
        async with sem:
            db = get_session(engine)
            try:
                await EntityGraphStore(db).extract_and_store(pid, title, content)
                db.commit()
            except Exception as e:
                logger.warning(f"Entity completion failed for {pid}: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass
            finally:
                db.close()
            done += 1
            status["processed"] = done
            status["message"] = f"实体抽取 {done}/{len(todo)}"

    await asyncio.gather(*(work(*t) for t in todo))


async def _run_graphrag():
    engine = get_engine(settings.database_url)
    init_db(engine)
    try:
        await _complete_entities(engine, _graphrag_status)
        await rebuild_communities(_graphrag_status)
        n = sync_image_assets(engine)
        _graphrag_status["message"] += f" | 图片资产 {n} 张"
    except Exception as e:
        logger.error(f"GraphRAG rebuild failed: {e}")
        _graphrag_status["running"] = False
        _graphrag_status["message"] = f"失败: {e}"


@router.post("/rebuild-communities")
async def rebuild_communities_endpoint(current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
    global _graphrag_task
    if _graphrag_status.get("running"):
        return {"started": False, "running": True, "message": "社区重建已在运行"}
    _graphrag_status.update(
        running=True, phase="start", processed=0, total=0, message="启动社区重建..."
    )
    _graphrag_task = asyncio.create_task(_run_graphrag())
    return {"started": True, "running": True}


@router.get("/community-status")
def community_status(current_user=Depends(get_current_user)):
    return _graphrag_status


@router.post("/rebuild-images")
def rebuild_images_endpoint(current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行")
    engine = get_engine(settings.database_url)
    init_db(engine)
    n = sync_image_assets(engine)
    return {
        "message": f"已同步 {n} 张图片资产",
        "multimodal_enabled": settings.multimodal_enabled,
    }
