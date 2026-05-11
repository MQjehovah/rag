import json
import logging
from typing import List, Dict, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.rag import EmbeddingService, VectorStore, RerankerService
from app.core.graph import GraphBuilder
from app.models.database import Page, Notebook, get_session, get_engine, init_db
from app.core.jwt_utils import get_current_user

router = APIRouter(prefix="/api/chat", tags=["AI问答"])

logger = logging.getLogger(__name__)

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


def _get_visible_page_ids(db, current_user) -> set:
    if "__local_admin__" in current_user["groups"]:
        return set(p[0] for p in db.query(Page.id).all())
    visible_nb_ids = db.query(Notebook.id).filter(
        or_(Notebook.group_id.in_(current_user["groups"]), Notebook.group_id.is_(None))
    ).subquery()
    return set(p[0] for p in db.query(Page.id).filter(Page.notebook_id.in_(visible_nb_ids)).all())


class ChatRequest(BaseModel):
    query: str


SYSTEM_PROMPT = """你是一个基于企业知识库的智能助手。请根据以下参考资料回答用户的问题。

要求：
- 只根据参考资料中的信息回答，不要编造内容
- 如果参考资料中没有相关信息，请诚实说明
- 回答时引用来源笔记的标题
- 使用中文回答"""

RAG_PROMPT_TEMPLATE = """参考资料：
{context}

---

用户问题：{query}

请根据以上参考资料回答问题。"""


async def _search_notes(query: str, db: Session, current_user) -> List[Dict[str, Any]]:
    embedding_service = EmbeddingService()
    try:
        query_embedding = await embedding_service.encode(query)
    except Exception as e:
        await embedding_service.close()
        logger.error(f"Embedding failed: {e}")
        return []

    visible_ids = _get_visible_page_ids(db, current_user)
    scores: Dict[str, Dict[str, Any]] = {}

    vec_store = VectorStore(db)
    try:
        vec_results = await vec_store.search(query_embedding, settings.vector_recall_k)
        for item in vec_results:
            page_id = item["page_id"]
            if page_id not in visible_ids:
                continue
            sim = 1.0 - item["distance"]
            if page_id not in scores:
                scores[page_id] = {"score": 0.0, "content": item["content"][:500]}
            scores[page_id]["score"] += sim * 3.0
    except Exception as e:
        logger.warning(f"Vector search error: {e}")

    try:
        query_kw = GraphBuilder.extract_keywords(query, 10)
        if query_kw:
            from sqlalchemy import text as sql_text
            kw_like_conditions = []
            params = {}
            for i, kw in enumerate(query_kw):
                kw_like_conditions.append(f"keywords LIKE :kw{i}")
                params[f"kw{i}"] = f"%{kw}%"
            if kw_like_conditions:
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
                    page_kw = set(row[3].split(",")) if row[3] else set()
                    overlap = query_kw & page_kw
                    if overlap:
                        kw_score = len(overlap) / max(len(query_kw), 1)
                        if pid not in scores:
                            scores[pid] = {"score": 0.0, "content": (row[2] or "")[:500]}
                        scores[pid]["score"] += kw_score * 2.0
    except Exception as e:
        logger.warning(f"Keyword search error: {e}")

    await embedding_service.close()

    if not scores:
        return []

    sorted_items = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)[:8]
    candidate_ids = [pid for pid, _ in sorted_items]
    pages = db.query(Page).filter(Page.id.in_(candidate_ids)).all()
    page_map = {p.id: p for p in pages}

    results = []
    for pid, data in sorted_items:
        p = page_map.get(pid)
        if p:
            results.append({
                "id": pid,
                "title": p.title or "",
                "content": data["content"],
            })
    return results


@router.post("")
async def chat(request: ChatRequest, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if not settings.llm_api_url:
        raise HTTPException(status_code=500, detail="未配置 LLM API")

    notes = await _search_notes(request.query, db, current_user)

    context_parts = []
    sources = []
    for note in notes:
        context_parts.append(f"【{note['title']}】\n{note['content']}")
        sources.append({"id": note["id"], "title": note["title"]})

    context = "\n\n".join(context_parts) if context_parts else "未找到相关笔记"
    user_message = RAG_PROMPT_TEMPLATE.format(context=context, query=request.query)

    async def generate():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    settings.llm_api_url,
                    headers={
                        "Authorization": f"Bearer {settings.llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.llm_model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_message},
                        ],
                        "stream": True,
                    },
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield f"data: {json.dumps({'type': 'content', 'content': content}, ensure_ascii=False)}\n\n"
                        except json.JSONDecodeError:
                            continue

                yield f"data: {json.dumps({'type': 'sources', 'sources': sources}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM API error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'content': f'LLM API 调用失败: {e.response.status_code}'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Chat error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
