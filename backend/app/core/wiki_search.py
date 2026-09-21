"""wiki 页面的语义(向量)检索。

Postgres 走 pgvector ANN;其他方言回退为内存 numpy 余弦(页量级小,可接受)。
所有路径都返回空列表而非抛异常,检索失败不应让调用方崩溃。
"""
import json
import logging
from typing import Any, Dict, List, Tuple

import numpy as np
from sqlalchemy import text

from app.api.search_common import visible_wiki_filter
from app.models.database import WikiPage

logger = logging.getLogger(__name__)


def _visibility_sql(current_user) -> Tuple[str, Dict[str, Any]]:
    """构造与 visible_wiki_filter 等价的裸 SQL 可见性条件与参数。

    管理员不过滤;否则 "group_id IS NULL OR group_id IN (:vg0, ...)"。
    两种实现的等价性由 tests/core/test_wiki_search.py 的等价性测试锁定。
    """
    groups = (current_user or {}).get("groups") or []
    if "__local_admin__" in groups:
        return "1 = 1", {}

    params: Dict[str, Any] = {}
    placeholders = []
    for i, g in enumerate(groups):
        key = f"vg{i}"
        params[key] = g
        placeholders.append(f":{key}")
    if placeholders:
        cond = f"(group_id IS NULL OR group_id IN ({','.join(placeholders)}))"
    else:
        cond = "group_id IS NULL"
    return cond, params


def _row_to_dict(row) -> Dict[str, Any]:
    distance = float(row[5])
    return {
        "id": row[0],
        "title": row[1] or "",
        "summary": row[2] or "",
        "content": row[3] or "",
        "category": row[4] or "",
        "score": 1.0 - distance,
        "distance": distance,
    }


def _search_numpy(db, query_vec, query_norm, limit: int, current_user) -> List[Dict[str, Any]]:
    """SQLite/其他方言回退:加载可见且有向量的页面,内存余弦。"""
    rows = (
        db.query(WikiPage)
        .filter(visible_wiki_filter(current_user))
        .filter(WikiPage.embedding.isnot(None))
        .all()
    )
    candidates = []
    for p in rows:
        try:
            emb = json.loads(p.embedding) if p.embedding else None
            if not emb:
                continue
            vec = np.asarray(emb, dtype=float)
            vec_norm = float(np.linalg.norm(vec))
            if vec_norm <= 0:
                continue
            sim = float(np.dot(query_vec, vec) / (query_norm * vec_norm))
        except Exception:
            continue  # 坏向量单独跳过,不影响其他页面
        distance = 1.0 - sim
        candidates.append({
            "id": p.id,
            "title": p.title or "",
            "summary": p.summary or "",
            "content": p.content or "",
            "category": p.category or "",
            "score": 1.0 - distance,
            "distance": distance,
        })
    candidates.sort(key=lambda x: x["distance"])
    return candidates[:limit]


def search_wiki(
    db,
    query_embedding,
    limit: int = 5,
    current_user=None,
) -> List[Dict[str, Any]]:
    """语义检索可见 wiki 页面,按相关度(距离升序)返回。

    返回 [{id, title, summary, content, category, score, distance}]。
    空/零查询向量直接返回 [];Postgres 路径异常时回退 numpy。
    """
    user = current_user or {"groups": []}
    try:
        query_vec = np.asarray(query_embedding, dtype=float)
    except (TypeError, ValueError):
        return []
    if query_vec.ndim != 1 or query_vec.size == 0:
        return []
    query_norm = float(np.linalg.norm(query_vec))
    if query_norm == 0.0:
        return []

    top_k = int(limit)
    dialect = ""
    try:
        dialect = db.bind.dialect.name
    except Exception:
        dialect = ""

    if dialect == "postgresql":
        try:
            emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
            cond, params = _visibility_sql(user)
            params = dict(params)
            params["query_emb"] = emb_str
            params["limit"] = top_k
            result = db.execute(text(
                "SELECT id, title, summary, content, category, "
                "embedding_vec <=> CAST(:query_emb AS vector) AS distance "
                f"FROM wiki_pages WHERE {cond} AND embedding_vec IS NOT NULL "
                "ORDER BY embedding_vec <=> CAST(:query_emb AS vector) "
                "LIMIT :limit"
            ), params)
            return [_row_to_dict(r) for r in result.fetchall()]
        except Exception as e:
            logger.warning(f"wiki pgvector 检索失败,回退 numpy: {e}")

    return _search_numpy(db, query_vec, query_norm, top_k, user)
