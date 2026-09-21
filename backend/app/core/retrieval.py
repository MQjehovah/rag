import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

import numpy as np
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.api.search_common import get_visible_page_ids
from app.config import settings
from app.core.entity_graph import EntityGraphStore
from app.core.hybrid import HybridIndex
from app.core.llm import call_llm_json
from app.core.rag import EmbeddingService, RerankerService, VectorStore
from app.core.wiki_search import search_wiki
from app.models.database import GraphEdge

logger = logging.getLogger(__name__)


REWRITE_PROMPT = """你是知识库检索助手。把用户的问题改写成最多2个不同的子问题或角度，用于检索召回，保持中文，覆盖不同的措辞和隐含信息。

用户问题: {query}

只返回 JSON，不要其他内容: {{"queries": ["子问题1", "子问题2"]}}"""

COMPLEX_MARKERS = (
    "和", "与", "以及", "或", "还是", "怎么", "如何", "为什么",
    "区别", "对比", "哪些", "什么", "分别", "总结", "所有", "包括",
)


def _needs_rewrite(query: str) -> bool:
    """Only spend an LLM call on queries complex enough to benefit from
    rewriting; short/simple queries recall fine with the original text."""
    q = (query or "").strip()
    if not q:
        return False
    if len(q) >= 30:
        return True
    return len(q) >= settings.query_rewrite_min_len and any(m in q for m in COMPLEX_MARKERS)


def _rrf(rankings: List[List[str]], k: int = 60) -> Dict[str, float]:
    scores: Dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, pid in enumerate(ranking):
            scores[pid] += 1.0 / (k + rank + 1)
    return dict(scores)


def _merge_rank(existing: List[str], new: List[str]) -> List[str]:
    seen = set(existing)
    merged = list(existing)
    for pid in new:
        if pid not in seen:
            seen.add(pid)
            merged.append(pid)
    return merged


def _chunk_placeholders(ids: List[str]) -> List[List[str]]:
    return [ids[i:i + 500] for i in range(0, len(ids), 500)]


def _fetch_embeddings(db, page_ids: List[str]) -> Dict[str, Any]:
    """Best chunk embedding per page (for MMR diversity)."""
    embs: Dict[str, Any] = {}
    if not page_ids:
        return embs
    for chunk in _chunk_placeholders(page_ids):
        ph = ",".join(f":v{i}" for i in range(len(chunk)))
        rows = db.execute(sql_text(
            f"SELECT page_id, embedding FROM page_chunks "
            f"WHERE page_id IN ({ph}) AND embedding IS NOT NULL "
            f"ORDER BY chunk_index"
        ), {f"v{i}": pid for i, pid in enumerate(chunk)}).fetchall()
        for pid, emb_json in rows:
            if pid in embs:
                continue
            try:
                embs[pid] = np.array(json.loads(emb_json))
            except Exception:
                pass
    return embs


def _mmr_rank(
    page_ids: List[str],
    rel_scores: Dict[str, float],
    embs: Dict[str, Any],
    lam: float = 0.7,
    top_k: int = 5,
) -> List[str]:
    """Maximal Marginal Relevance: trade relevance against redundancy."""
    selected: List[str] = []
    remaining = list(page_ids)
    while len(selected) < top_k and remaining:
        best_pid = None
        best_val = float("-inf")
        for pid in remaining:
            rel = rel_scores.get(pid, 0.0)
            sim = 0.0
            if selected and pid in embs:
                for s in selected:
                    if s not in embs:
                        continue
                    v1, v2 = embs[pid], embs[s]
                    n1 = float(np.linalg.norm(v1))
                    n2 = float(np.linalg.norm(v2))
                    if n1 > 0 and n2 > 0:
                        sim = max(sim, float(np.dot(v1, v2) / (n1 * n2)))
            mmr = lam * rel - (1.0 - lam) * sim
            if mmr > best_val:
                best_val = mmr
                best_pid = pid
        if best_pid is None:
            break
        selected.append(best_pid)
        remaining.remove(best_pid)
    return selected


async def _rewrite_query(query: str) -> List[str]:
    if not settings.query_rewrite_enabled or not settings.llm_api_url:
        return []
    if not _needs_rewrite(query):
        return []
    try:
        result = await call_llm_json(
            [{"role": "user", "content": REWRITE_PROMPT.format(query=query)}],
            context="query-rewrite",
        )
        queries = [
            q.strip()
            for q in result.get("queries", [])
            if isinstance(q, str) and q.strip()
        ]
        return queries[:2]
    except Exception as e:
        logger.warning(f"Query rewrite failed: {e}")
        return []


class RetrievalPipeline:
    """Unified retrieval: rewrite -> multi-path recall -> RRF -> rerank ->
    entity expansion -> graph expansion -> ranked results with chunk citations.
    """

    def __init__(
        self,
        db: Session,
        embedding_svc: Optional[EmbeddingService] = None,
        reranker_svc: Optional[RerankerService] = None,
    ):
        self.db = db
        self.embedding_svc = embedding_svc or EmbeddingService()
        self.reranker_svc = reranker_svc
        self.vector = VectorStore(db)
        self.hybrid = HybridIndex(db)
        self.entities = EntityGraphStore(db)

    async def retrieve(
        self,
        query: str,
        current_user,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        visible_ids = await asyncio.to_thread(get_visible_page_ids, self.db, current_user)
        # 可见笔记为空不能提前返回:wiki 有独立的可见性,仍要召回。
        # has_notes 只用来跳过依赖 visible_ids 的笔记召回与后处理。
        has_notes = bool(visible_ids)

        queries = [query]
        queries += await _rewrite_query(query)

        try:
            embeddings = await self.embedding_svc.encode_batch(queries)
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            return {"results": [], "queries": queries, "graph_expanded": 0}

        recall_k = settings.vector_recall_k
        vector_rank: List[str] = []
        bm25_rank: List[str] = []
        best_chunks: Dict[str, Dict[str, Any]] = {}

        # 笔记召回依赖 visible_ids;无可见笔记时整段跳过,但保留 embeddings 供 wiki 召回使用。
        if has_notes:
            for q, emb in zip(queries, embeddings):
                try:
                    vec_results = await self.vector.search(emb, recall_k, visible_ids)
                except Exception as e:
                    logger.warning(f"Vector search error: {e}")
                    vec_results = []

                per_page: Dict[str, Dict[str, Any]] = {}
                for item in vec_results:
                    pid = item["page_id"]
                    if pid not in per_page:
                        per_page[pid] = item
                for pid, item in per_page.items():
                    best = best_chunks.get(pid)
                    if best is None or item["distance"] < best["distance"]:
                        best_chunks[pid] = {
                            "distance": item["distance"],
                            "content": item.get("content", ""),
                            "context": item.get("context", "") or "",
                            "chunk_index": item.get("chunk_index", 0),
                        }
                vector_rank = _merge_rank(vector_rank, [i["page_id"] for i in vec_results])

                if settings.hybrid_bm25_enabled:
                    try:
                        bm = await asyncio.to_thread(
                            self.hybrid.search, q, visible_ids, recall_k
                        )
                        bm25_rank = _merge_rank(bm25_rank, [pid for pid, _ in bm])
                    except Exception as e:
                        logger.warning(f"BM25 search error: {e}")

        # wiki 召回:与页面召回并列参与 RRF。id 加 wiki: 前缀,避免与 pages.id 撞车。
        wiki_hits: List[Dict[str, Any]] = []
        if embeddings:
            try:
                wiki_hits = await asyncio.to_thread(
                    search_wiki, self.db, embeddings[0], recall_k, current_user
                )
            except Exception as e:
                logger.warning(f"Wiki search error: {e}")
                wiki_hits = []
        wiki_map: Dict[str, Dict[str, Any]] = {
            f"wiki:{w['id']}": w for w in wiki_hits
        }
        wiki_rank = list(wiki_map.keys())

        rrf_scores = _rrf([vector_rank, bm25_rank, wiki_rank])
        candidates = sorted(rrf_scores, key=rrf_scores.get, reverse=True)

        entity_boost: Dict[str, float] = {}
        # 空 visible_ids 会让 expand_candidates 失去可见性约束(其内部用 not visible_ids 放行全部),
        # 因此无可见笔记时必须跳过实体扩展。
        if has_notes and settings.entity_graph_enabled:
            try:
                entity_boost = await asyncio.to_thread(
                    self.entities.expand_candidates, query, None, visible_ids
                )
            except Exception as e:
                logger.warning(f"Entity expansion error: {e}")
            for pid in entity_boost:
                if pid not in candidates:
                    candidates.append(pid)

        candidate_ids = candidates[: max(recall_k, 80)]
        for pid in entity_boost:
            if pid not in candidate_ids:
                candidate_ids.append(pid)
        page_map: Dict[str, Page] = {}
        if has_notes and candidate_ids:
            ids = list(candidate_ids)
            for chunk in _chunk_placeholders(ids):
                ph = ",".join(f":v{i}" for i in range(len(chunk)))
                rows = self.db.execute(
                    sql_text(
                        f"SELECT id, title, content, keywords FROM pages "
                        f"WHERE id IN ({ph})"
                    ),
                    {f"v{i}": pid for i, pid in enumerate(chunk)},
                ).fetchall()
                for rid, title, content, keywords in rows:
                    page_map[rid] = {
                        "id": rid,
                        "title": title or "",
                        "content": content or "",
                        "keywords": keywords or "",
                    }

        rr_scores: Dict[str, float] = {}
        rerank_candidates = []
        for pid in candidate_ids:
            p = page_map.get(pid)
            if p:
                rerank_candidates.append({
                    "id": pid,
                    "text": (p["title"] or "") + " " + (p["content"] or "")[:500],
                })

        rr_available = (
            self.reranker_svc is not None
            and bool(self.reranker_svc.api_url)
            and len(rerank_candidates) > 1
        )
        if rr_available:
            try:
                docs = [c["text"] for c in rerank_candidates]
                rerank_results = await self.reranker_svc.rerank(
                    query, docs, top_k=max(top_k * 3, 15)
                )
                for r in rerank_results:
                    idx = r.get("index", 0)
                    if idx < len(rerank_candidates):
                        rr_scores[rerank_candidates[idx]["id"]] = r.get("relevance_score", 0.0)
            except Exception as e:
                logger.warning(f"Reranker error: {e}")
                rr_available = False

        rrf_max = max(rrf_scores.values(), default=1.0) or 1.0
        rr_max = max(rr_scores.values(), default=1.0) or 1.0

        final: Dict[str, float] = {}
        for pid in candidate_ids:
            score = rrf_scores.get(pid, 0.0) / rrf_max
            if rr_available:
                score = 0.4 * score + 0.6 * (rr_scores.get(pid, 0.0) / rr_max)
            score += entity_boost.get(pid, 0.0)
            final[pid] = score

        sources_map: Dict[str, Set[str]] = defaultdict(set)
        vec_set = set(vector_rank)
        bm_set = set(bm25_rank)
        wiki_set = set(wiki_rank)
        for pid in final:
            if pid in vec_set:
                sources_map[pid].add("vector")
            if pid in bm_set:
                sources_map[pid].add("keyword")
            if pid in wiki_set:
                sources_map[pid].add("wiki")
            if entity_boost.get(pid):
                sources_map[pid].add("entity")
            if rr_scores.get(pid, 0.0) > 0:
                sources_map[pid].add("reranker")

        graph_expanded = 0
        try:
            # 图扩展只在笔记之间进行;无可见笔记时没有可扩展的种子。
            seeds = sorted(final, key=final.get, reverse=True)[: max(top_k * 2, 10)] if has_notes else []
            if seeds:
                seed_chunks = _chunk_placeholders(seeds)
                edges = []
                for chunk in seed_chunks:
                    ph = ",".join(f":s{i}" for i in range(len(chunk)))
                    edges.extend(self.db.execute(
                        sql_text(
                            f"SELECT source_id, target_id, weight FROM graph_edges "
                            f"WHERE source_id IN ({ph}) OR target_id IN ({ph})"
                        ),
                        {f"s{i}": pid for i, pid in enumerate(chunk)},
                    ).fetchall())
                adj: Dict[str, List[tuple]] = defaultdict(list)
                for src, tgt, weight in edges:
                    adj[src].append((tgt, float(weight)))
                    adj[tgt].append((src, float(weight)))
                for seed in seeds:
                    for neighbor, weight in adj.get(seed, []):
                        if neighbor in visible_ids and neighbor not in final:
                            final[neighbor] = final[seed] * 0.5 * weight
                            if neighbor not in page_map:
                                row = self.db.execute(
                                    sql_text(
                                        "SELECT id, title, content, keywords FROM pages WHERE id = :pid"
                                    ),
                                    {"pid": neighbor},
                                ).fetchone()
                                if row:
                                    page_map[neighbor] = {
                                        "id": row[0],
                                        "title": row[1] or "",
                                        "content": row[2] or "",
                                        "keywords": row[3] or "",
                                    }
                            sources_map[neighbor].add("graph")
                            graph_expanded += 1
        except Exception as e:
            logger.warning(f"Graph expansion error: {e}")

        if settings.mmr_enabled and len(final) > 1:
            candidates = list(final.keys())[:40]
            # wiki 没有 page_chunks 向量,_fetch_embeddings 取不到,sim 恒为 0。
            # 若混入 MMR 会按错误的相似度排序甚至被 top_k 截掉,因此把它排除在 MMR 候选之外,
            # 先按融合分为 wiki 保留名额,其余名额交给 MMR 从笔记里选,最后按"MMR 笔记在前、wiki 在后"合并。
            wiki_ids = [pid for pid in candidates if pid.startswith("wiki:")]
            note_ids = [pid for pid in candidates if not pid.startswith("wiki:")]
            wiki_ids.sort(key=lambda p: final.get(p, 0.0), reverse=True)
            note_slots = max(top_k - len(wiki_ids), 0)
            ranked_note_ids = _mmr_rank(
                note_ids,
                final,
                _fetch_embeddings(self.db, note_ids),
                lam=settings.mmr_lambda,
                top_k=note_slots,
            )
            ranked = [(pid, final[pid]) for pid in ranked_note_ids]
            ranked += [(pid, final[pid]) for pid in wiki_ids]
        else:
            ranked = sorted(final.items(), key=lambda x: x[1], reverse=True)
        results = []
        for pid, score in ranked[:top_k]:
            if pid.startswith("wiki:"):
                # wiki 结果从独立的 wiki_map 取文段,不能用 pages 的 page_map
                w = wiki_map.get(pid) or {}
                summary = w.get("summary") or ""
                content = w.get("content") or ""
                snippet = summary or content[:300]
                results.append({
                    "id": pid,
                    "title": w.get("title") or "",
                    "content": snippet,
                    "score": round(score, 4),
                    "sources": sorted(sources_map.get(pid, set())),
                    "chunks": [{"content": summary or content[:300]}],
                })
                continue
            p = page_map.get(pid) or {}
            chunk = best_chunks.get(pid)
            snippet = chunk["content"] if chunk and chunk["content"] else (p.get("content") or "")[:300]
            chunks = []
            if chunk and chunk["content"]:
                chunks.append({
                    "content": chunk["content"],
                    "context": chunk.get("context", ""),
                    "chunk_index": chunk.get("chunk_index", 0),
                })
            if not chunks:
                rows = self.db.execute(
                    sql_text(
                        "SELECT content, COALESCE(context, ''), chunk_index FROM page_chunks "
                        "WHERE page_id = :pid ORDER BY chunk_index LIMIT 1"
                    ),
                    {"pid": pid},
                ).fetchall()
                if rows:
                    chunks.append({"content": rows[0][0], "context": rows[0][1], "chunk_index": rows[0][2]})
            results.append({
                "id": pid,
                "title": p.get("title") or "",
                "content": snippet,
                "score": round(score, 4),
                "sources": sorted(sources_map.get(pid, set())),
                "chunks": chunks[:3],
            })

        return {
            "results": results,
            "queries": queries,
            "graph_expanded": graph_expanded,
        }
