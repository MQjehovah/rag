import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import settings
from app.core.entity_graph import EntityGraphStore
from app.core.llm import call_llm_json
from app.core.rag import EmbeddingService
from app.models.database import (
    GraphCommunity,
    GraphEntity,
    GraphEntityEdge,
    ImageAsset,
    Page,
    get_engine,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)

try:
    import networkx as nx
    from community import community_louvain
    GRAPH_LIBS = True
except Exception:
    nx = None
    community_louvain = None
    GRAPH_LIBS = False

IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

COMMUNITY_PROMPT = """你是一个知识库分析助手。以下是一个实体社区中的实体和关系：

实体：
{entities}

实体间关系：
{relations}

请给这个社区起一个 12 字以内的标题，并写一段 200 字以内的摘要：说明该社区围绕什么主题、包含哪些关键实体、实体间的主要关系。
只返回 JSON：{{"title": "...", "summary": "..."}}"""


def _extract_entities_relations(db: Session):
    entities = db.query(GraphEntity.id, GraphEntity.name, GraphEntity.entity_type).all()
    ent_map = {e[0]: {"name": e[1], "type": e[2]} for e in entities}
    edges = db.query(GraphEntityEdge.source_entity_id, GraphEntityEdge.target_entity_id, GraphEntityEdge.relation).all()
    valid_edges = [
        (s, t, r)
        for s, t, r in edges
        if s in ent_map and t in ent_map
    ]
    return ent_map, valid_edges


def detect_communities(db: Session) -> Dict[str, List[str]]:
    """Louvain communities: label -> list of entity ids."""
    ent_map, valid_edges = _extract_entities_relations(db)
    if not GRAPH_LIBS or not ent_map:
        return {}
    g = nx.Graph()
    for eid in ent_map:
        g.add_node(eid)
    for s, t, _r in valid_edges:
        if g.has_edge(s, t):
            g[s][t]["weight"] += 1.0
        else:
            g.add_edge(s, t, weight=1.0)
    partition = community_louvain.best_partition(g, weight="weight", random_state=42)
    communities: Dict[str, List[str]] = {}
    for eid, label in partition.items():
        communities.setdefault(str(label), []).append(eid)
    return communities


async def _summarize_community(
    ent_map: Dict[str, Dict[str, str]],
    entity_ids: List[str],
) -> Optional[Dict[str, Any]]:
    ent_lines = []
    rel_lines = []
    ent_set = set(entity_ids)
    for eid in entity_ids:
        e = ent_map.get(eid)
        if e:
            ent_lines.append(f"- {e['name']}（{e.get('type') or '未知类型'}）")
    db = get_session(get_engine(settings.database_url))
    try:
        rows = db.query(GraphEntityEdge.source_entity_id, GraphEntityEdge.target_entity_id, GraphEntityEdge.relation).filter(
            GraphEntityEdge.source_entity_id.in_(entity_ids),
            GraphEntityEdge.target_entity_id.in_(entity_ids),
        ).all()
    finally:
        db.close()
    for s, t, r in rows[:80]:
        rel_lines.append(f"- {ent_map.get(s, {}).get('name', s)} {r or '关联'} {ent_map.get(t, {}).get('name', t)}")
    if not ent_lines:
        return None
    prompt = COMMUNITY_PROMPT.format(
        entities="\n".join(ent_lines[:60]),
        relations="\n".join(rel_lines) or "(无)",
    )
    try:
        result = await call_llm_json(
            [{"role": "user", "content": prompt}],
            context="community-summary",
            timeout=180.0,
        )
    except Exception as e:
        logger.warning(f"Community summary failed: {e}")
        return None
    title = (result.get("title") or "").strip() if isinstance(result, dict) else ""
    summary = (result.get("summary") or "").strip() if isinstance(result, dict) else ""
    if not summary:
        return None
    return {"title": title[:50], "summary": summary}


async def rebuild_communities(status: Dict[str, Any]) -> None:
    engine = get_engine(settings.database_url)
    init_db(engine)
    status["running"] = True
    status["phase"] = "communities"
    status["message"] = "加载实体图..."

    db = get_session(engine)
    try:
        ent_map, _ = _extract_entities_relations(db)
        communities = detect_communities(db)
    finally:
        db.close()

    if not communities:
        status["running"] = False
        status["message"] = "没有可用的实体图，请先重建实体"
        return

    # clear old communities
    db = get_session(engine)
    try:
        db.query(GraphCommunity).delete()
        db.commit()
    finally:
        db.close()

    emb_svc = EmbeddingService()
    sem = asyncio.Semaphore(3)
    started = time.time()
    total = len(communities)
    done = 0
    created = 0

    async def worker(label: str, entity_ids: List[str]):
        nonlocal done, created
        async with sem:
            item = await _summarize_community(ent_map, entity_ids)
            if item:
                try:
                    emb = await emb_svc.encode(item["summary"][:800])
                except Exception:
                    emb = []
                db = get_session(engine)
                try:
                    db.add(GraphCommunity(
                        id=str(uuid.uuid4()),
                        level=1,
                        title=item["title"],
                        summary=item["summary"],
                        member_ids=json.dumps(entity_ids, ensure_ascii=False),
                        embedding=json.dumps(emb) if emb else None,
                    ))
                    db.commit()
                    created += 1
                finally:
                    db.close()
            done += 1
            status["processed"] = done
            status["total"] = total
            status["message"] = f"社区摘要 {done}/{total}"

    tasks = [asyncio.create_task(worker(label, ids)) for label, ids in communities.items()]
    if tasks:
        await asyncio.gather(*tasks)
    await emb_svc.close()

    status["running"] = False
    status["message"] = (
        f"社区摘要完成：{created} 个社区，"
        f"用时 {round((time.time() - started) / 60, 1)} 分钟"
    )


def search_communities(
    db: Session,
    query_embedding: List[float],
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    rows = db.query(
        GraphCommunity.id,
        GraphCommunity.title,
        GraphCommunity.summary,
        GraphCommunity.embedding,
    ).all()
    if not rows:
        return []
    q = np.array(query_embedding)
    qn = np.linalg.norm(q)
    if qn == 0:
        return []
    scored = []
    for cid, title, summary, emb_json in rows:
        if not emb_json:
            continue
        try:
            v = np.array(json.loads(emb_json))
        except Exception:
            continue
        vn = np.linalg.norm(v)
        if vn == 0:
            continue
        sim = float(np.dot(q, v) / (qn * vn))
        scored.append((sim, cid, title, summary))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"id": cid, "title": title, "summary": summary, "score": round(s, 4)}
        for s, cid, title, summary in scored[:top_k]
    ]


def sync_image_assets(engine) -> int:
    """Scan pages for image URLs and record them (multimodal pre-support).
    OCR/caption/embedding are only populated when multimodal is enabled."""
    db = get_session(engine)
    try:
        pages = db.query(Page.id, Page.content).all()
        total = 0
        for pid, content in pages:
            for alt, url in IMAGE_RE.findall(content or ""):
                if not url or url.startswith("data:"):
                    continue
                exists = db.query(ImageAsset.id).filter(
                    ImageAsset.page_id == pid,
                    ImageAsset.url == url,
                ).first()
                if exists:
                    continue
                db.add(ImageAsset(
                    id=str(uuid.uuid4()),
                    page_id=pid,
                    url=url,
                    alt=alt or "",
                ))
                total += 1
        db.commit()
        return total
    finally:
        db.close()
