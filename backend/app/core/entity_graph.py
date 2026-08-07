import logging
import uuid
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import settings
from app.core.hybrid import tokenize
from app.core.llm import call_llm_json
from app.models.database import GraphEntity, GraphEntityEdge

logger = logging.getLogger(__name__)


ENTITY_PROMPT = """你是知识图谱抽取助手。从下面的笔记中抽取实体和实体间关系。

实体类型建议: 人物、组织、项目、系统、技术栈、产品、概念、版本号、文档。

只抽取文本中明确出现的信息，不要编造。实体名保持原文。

笔记标题: {title}

笔记内容:
{content}

只返回 JSON，格式如下，不要其他内容:
{{
  "entities": [{{"name": "实体名", "type": "实体类型"}}],
  "relations": [{{"source": "实体名", "relation": "关系描述(如 依赖/属于/负责/使用)", "target": "实体名"}}]
}}
"""


class EntityGraphStore:
    """Entity-level knowledge graph (LightRAG-style).

    Entities and relations are extracted per page (LLM, optional) and stored
    in ``graph_entities`` / ``graph_entity_edges``.  Retrieval can then expand
    candidates through entities matched in the query, catching links that pure
    vector/keyword search would miss (A depends on B, B belongs to project C).
    """

    def __init__(self, db: Session):
        self.db = db

    async def extract_and_store(
        self,
        page_id: str,
        title: str = "",
        content: str = "",
    ) -> int:
        if not settings.entity_graph_enabled or not settings.llm_api_url:
            return 0
        if not (content or "").strip():
            return 0
        try:
            result = await call_llm_json(
                [{"role": "user", "content": ENTITY_PROMPT.format(
                    title=title or "无",
                    content=(content or "")[:8000],
                )}],
                context="entity-graph",
            )
        except Exception as e:
            logger.warning(f"Entity extraction failed for {page_id}: {e}")
            return 0

        # call_llm_json returns {} when the LLM is unavailable/unparseable.
        # In that case keep the existing entity graph instead of wiping it.
        if not result:
            return 0
        entities = result.get("entities", []) if isinstance(result, dict) else []
        relations = result.get("relations", []) if isinstance(result, dict) else []
        return self.store(page_id, entities, relations)

    def store(
        self,
        page_id: str,
        entities: List[Dict[str, Any]],
        relations: List[Dict[str, Any]],
    ) -> int:
        self.db.query(GraphEntityEdge).filter(GraphEntityEdge.page_id == page_id).delete()
        self.db.query(GraphEntity).filter(GraphEntity.page_id == page_id).delete()

        name_to_id: Dict[str, str] = {}
        count = 0
        for ent in entities:
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            if name in name_to_id:
                continue
            existing_id = self.db.query(GraphEntity.id).filter(GraphEntity.name == name).first()
            if existing_id:
                entity_id = existing_id[0]
            else:
                entity = GraphEntity(
                    id=str(uuid.uuid4()),
                    name=name,
                    entity_type=(ent.get("type") or "").strip() or None,
                    page_id=page_id,
                )
                self.db.add(entity)
                entity_id = entity.id
                count += 1
            name_to_id[name] = entity_id

        edge_count = 0
        for rel in relations:
            src = name_to_id.get((rel.get("source") or "").strip())
            tgt = name_to_id.get((rel.get("target") or "").strip())
            relation = (rel.get("relation") or "").strip()
            if not src or not tgt or src == tgt:
                continue
            self.db.add(GraphEntityEdge(
                id=str(uuid.uuid4()),
                source_entity_id=src,
                target_entity_id=tgt,
                relation=relation,
                page_id=page_id,
                weight=1.0,
            ))
            edge_count += 1

        self.db.flush()
        return count + edge_count

    def delete_page(self, page_id: str) -> None:
        self.db.query(GraphEntityEdge).filter(GraphEntityEdge.page_id == page_id).delete()
        self.db.query(GraphEntity).filter(GraphEntity.page_id == page_id).delete()
        self.db.flush()

    def expand_candidates(
        self,
        query: str,
        candidate_ids: Optional[Set[str]] = None,
        visible_ids: Optional[Set[str]] = None,
        limit: int = 100,
    ) -> Dict[str, float]:
        """Return {page_id: boost} for pages linked to entities in the query."""
        tokens = tokenize(query)
        if not tokens:
            return {}

        likes = []
        params: Dict[str, Any] = {}
        for i, tok in enumerate(tokens):
            likes.append(f"name LIKE :t{i}")
            params[f"t{i}"] = f"%{tok}%"
        like_sql = " OR ".join(likes)

        rows = self.db.execute(
            sql_text(
                f"SELECT id, name, page_id FROM graph_entities WHERE {like_sql} LIMIT {limit}"
            ),
            params,
        ).fetchall()
        if not rows:
            return {}

        boosts: Dict[str, float] = {}
        entity_ids = []
        for eid, name, pid in rows:
            entity_ids.append(eid)
            if pid and (not visible_ids or pid in visible_ids):
                boosts[pid] = max(boosts.get(pid, 0.0), 1.0)

        if entity_ids:
            ids = list(entity_ids)
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                placeholders = ",".join(f":e{j}" for j in range(len(chunk)))
                edge_rows = self.db.execute(
                    sql_text(
                        f"SELECT source_entity_id, target_entity_id, page_id "
                        f"FROM graph_entity_edges "
                        f"WHERE source_entity_id IN ({placeholders}) "
                        f"OR target_entity_id IN ({placeholders})"
                    ),
                    {**{f"e{j}": eid for j, eid in enumerate(chunk)},
                     **{f"e{j + len(chunk)}": eid for j, eid in enumerate(chunk)}},
                ).fetchall()
                for src, tgt, pid in edge_rows:
                    if pid and (not visible_ids or pid in visible_ids):
                        boosts[pid] = max(boosts.get(pid, 0.0), 0.5)

        return boosts
