import json
import logging
from typing import List, Dict, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.rag import EmbeddingService, VectorStore, RerankerService
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
    reranker_svc = RerankerService()
    try:
        query_embedding = await embedding_service.encode(query)
    except Exception as e:
        await embedding_service.close()
        logger.error(f"Embedding failed: {e}")
        return []

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
    try:
        query_kw = EmbeddingService.extract_keywords(query, 10, fine_grained=True)
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
                        ks = len(overlap) / max(len(query_kw), 1)
                        tb = 0.3 if any(kw in (row[1] or "") for kw in overlap) else 0.0
                        kw_scores[pid] = min(ks + tb, 1.0)
    except Exception as e:
        logger.warning(f"Keyword search error: {e}")

    await embedding_service.close()

    candidate_ids = set(vec_scores.keys()) | set(kw_scores.keys())
    if not candidate_ids:
        return []

    pages = db.query(Page).filter(Page.id.in_(list(candidate_ids))).all()
    page_map = {p.id: p for p in pages}

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
            rerank_results = await reranker_svc.rerank(query, docs, top_k=10)
            for r in rerank_results:
                idx = r.get("index", 0)
                if idx < len(rerank_candidates):
                    rr_scores[rerank_candidates[idx]["id"]] = r.get("relevance_score", 0.0)
        except Exception as e:
            logger.warning(f"Reranker error: {e}")

    W_VEC, W_KW, W_RR = 1.0, 1.5, 5.0
    scored = []
    for pid in candidate_ids:
        v = vec_scores.get(pid, 0.0)
        k = kw_scores.get(pid, 0.0)
        r = rr_scores.get(pid, 0.0)
        final = v * W_VEC + k * W_KW + r * W_RR
        scored.append((pid, final))

    scored.sort(key=lambda x: -x[1])
    top_ids = [pid for pid, _ in scored[:5]]

    results = []
    for pid in top_ids:
        p = page_map.get(pid)
        if p:
            content = p.content or ""
            if len(content) > 3000:
                content = content[:3000] + "\n...(内容过长已截断)"
            results.append({
                "id": pid,
                "title": p.title or "",
                "content": content,
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


class SaveNoteRequest(BaseModel):
    query: str
    answer: str


async def _call_llm_json(messages: list, context: str = "") -> dict:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            settings.llm_api_url,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.llm_model,
                "messages": messages,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        logger.info(f"LLM raw response ({len(content)} chars): {content[:2000]}")
        if not content or not content.strip():
            logger.warning(f"LLM empty response [{context}]")
            return {}

        import re
        json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', content)
        if json_match:
            content = json_match.group(1)

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        brace_depth = 0
        start = -1
        for i, ch in enumerate(content):
            if ch == '{':
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0 and start >= 0:
                    candidate = content[start:i + 1]
                    try:
                        result = json.loads(candidate)
                        logger.info(f"LLM parsed result [{context}]: {json.dumps(result, ensure_ascii=False)[:2000]}")
                        return result
                    except json.JSONDecodeError:
                        continue

        logger.warning(f"LLM response could not be parsed as JSON [{context}]: {content[:500]}")
        return {}


def _get_kb_context(db: Session) -> dict:
    notebooks = db.query(Notebook).all()
    nb_list = [{"id": nb.id, "name": nb.name} for nb in notebooks]
    pages = db.query(Page.id, Page.title, Page.notebook_id).order_by(Page.updated_at.desc()).limit(100).all()
    page_list = [{"id": p[0], "title": p[1], "notebook_id": p[2]} for p in pages]
    return {"notebooks": nb_list, "pages": page_list}


ORGANIZE_PROMPT = """你是一个企业知识库管理助手。请分析以下原始内容，完成知识整理和归类。

当前知识库结构：
笔记本列表：{kb_notebooks}
笔记列表（最近100篇）：{kb_pages}

原始内容（来源：{source}）：
{raw_content}

请完成以下任务：
1. 判断内容是否值得保存为知识笔记。如果内容毫无价值（如广告、导航栏、版权声明等），设置 should_save=false
2. 如果值得保存，将原始内容整理为结构化的 Markdown 格式，去除无效信息（广告、导航、版权、重复内容等），保留核心知识
3. 决定归类方式：
   - "create_notebook": 创建新笔记本 + 新笔记（当现有笔记本都不合适时）
   - "create_note": 在现有笔记本中创建新笔记
   - "update_note": 更新某篇已有笔记的内容（当内容是对已有笔记的补充时）

请以 JSON 格式返回：
{{
  "should_save": true/false,
  "action": "create_notebook" / "create_note" / "update_note",
  "title": "笔记标题",
  "notebook_id": "现有笔记本ID（create_note/update_note时）",
  "new_notebook_name": "新笔记本名称（create_notebook时）",
  "update_page_id": "要更新的笔记ID（update_note时）",
  "content": "整理后的Markdown内容",
  "summary": "100字以内摘要"
}}
只返回 JSON，不要其他内容。"""


@router.post("/save-note")
async def save_note(request: SaveNoteRequest, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if not settings.llm_api_url:
        raise HTTPException(status_code=500, detail="未配置 LLM API")

    kb = _get_kb_context(db)
    prompt = ORGANIZE_PROMPT.format(
        kb_notebooks=json.dumps(kb["notebooks"], ensure_ascii=False),
        kb_pages=json.dumps(kb["pages"], ensure_ascii=False),
        source="AI对话",
        raw_content=f"用户问题：{request.query}\n\nAI回答：{request.answer}",
    )

    result = await _call_llm_json([{"role": "user", "content": prompt}], context="save-note")

    return {
        "should_save": result.get("should_save", True),
        "action": result.get("action", "create_note"),
        "title": result.get("title", request.query[:50]),
        "notebook_id": result.get("notebook_id"),
        "new_notebook_name": result.get("new_notebook_name"),
        "update_page_id": result.get("update_page_id"),
        "content": result.get("content", f"## {request.query}\n\n{request.answer}"),
        "summary": result.get("summary", ""),
        "notebooks": kb["notebooks"],
        "pages": kb["pages"],
    }


async def _extract_text_from_bytes(content: bytes, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    elif ext in ("docx", "doc"):
        import io
        import xml.etree.ElementTree as ET
        import zipfile
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            doc_xml = z.read("word/document.xml")
            tree = ET.fromstring(doc_xml)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paragraphs = tree.findall(".//w:p", ns)
            lines = []
            for p in paragraphs:
                texts = [t.text for t in p.findall(".//w:t", ns) if t.text]
                if texts:
                    lines.append("".join(texts))
            return "\n".join(lines)
    elif ext in ("txt", "md", "csv", "json"):
        return content.decode("utf-8", errors="ignore")
    else:
        return content.decode("utf-8", errors="ignore")


def _extract_main_content(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                      "iframe", "noscript", "form", "button", "input", "select"]):
        tag.decompose()

    for tag_id in ["comments", "sidebar", "footer", "header", "nav", "menu",
                    "advertisement", "ad", "cookie", "banner", "popup", "modal"]:
        for el in soup.find_all(id=lambda x: x and tag_id in x.lower()):
            el.decompose()
    for cls in ["comment", "sidebar", "footer", "header", "nav", "menu",
                "ad", "advertisement", "cookie", "banner", "popup", "modal",
                "social", "share", "related", "recommend", "pagination"]:
        for el in soup.find_all(class_=lambda x: x and cls in " ".join(x).lower()):
            el.decompose()

    article = soup.find("article")
    if article:
        main = article
    else:
        candidates = soup.find_all(["div", "section", "main"])
        main = max(candidates, key=lambda el: len(el.get_text(strip=True))) if candidates else soup.body

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    for tag in main.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        level = int(tag.name[1])
        tag.replace_with(f"\n{'#' * level} {tag.get_text(strip=True)}\n")

    for tag in main.find_all("p"):
        tag.replace_with(f"\n{tag.get_text(strip=True)}\n")

    for tag in main.find_all("li"):
        tag.replace_with(f"\n- {tag.get_text(strip=True)}")

    for tag in main.find_all("br"):
        tag.replace_with("\n")

    for tag in main.find_all(["a"]):
        href = tag.get("href", "")
        text = tag.get_text(strip=True)
        if href and text:
            tag.replace_with(f"[{text}]({href})")
        else:
            tag.replace_with(text)

    text = main.get_text()
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    content = "\n".join(lines)
    return title, content


@router.post("/import/file")
async def import_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not settings.llm_api_url:
        raise HTTPException(status_code=500, detail="未配置 LLM API")

    content_bytes = await file.read()
    text = await _extract_text_from_bytes(content_bytes, file.filename or "file.txt")

    if not text.strip():
        raise HTTPException(status_code=400, detail="无法提取文本内容")

    kb = _get_kb_context(db)
    prompt = ORGANIZE_PROMPT.format(
        kb_notebooks=json.dumps(kb["notebooks"], ensure_ascii=False),
        kb_pages=json.dumps(kb["pages"], ensure_ascii=False),
        source=f"文件: {file.filename}",
        raw_content=text[:6000],
    )

    result = await _call_llm_json([{"role": "user", "content": prompt}], context="import-file")

    return {
        "should_save": result.get("should_save", True),
        "action": result.get("action", "create_note"),
        "title": result.get("title", file.filename or "导入文件"),
        "notebook_id": result.get("notebook_id"),
        "new_notebook_name": result.get("new_notebook_name"),
        "update_page_id": result.get("update_page_id"),
        "content": result.get("content", text),
        "summary": result.get("summary", ""),
        "notebooks": kb["notebooks"],
        "pages": kb["pages"],
    }


class ImportUrlRequest(BaseModel):
    url: str


@router.post("/import/url")
async def import_url(request: ImportUrlRequest, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if not settings.llm_api_url:
        raise HTTPException(status_code=500, detail="未配置 LLM API")

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(request.url)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"抓取URL失败: {str(e)}")

    page_title, text = _extract_main_content(html)

    if not text.strip():
        raise HTTPException(status_code=400, detail="无法提取网页正文内容")

    kb = _get_kb_context(db)
    prompt = ORGANIZE_PROMPT.format(
        kb_notebooks=json.dumps(kb["notebooks"], ensure_ascii=False),
        kb_pages=json.dumps(kb["pages"], ensure_ascii=False),
        source=f"网页: {page_title or request.url}",
        raw_content=text[:6000],
    )

    result = await _call_llm_json([{"role": "user", "content": prompt}], context="import-url")

    return {
        "should_save": result.get("should_save", True),
        "action": result.get("action", "create_note"),
        "title": result.get("title", page_title or request.url),
        "notebook_id": result.get("notebook_id"),
        "new_notebook_name": result.get("new_notebook_name"),
        "update_page_id": result.get("update_page_id"),
        "content": result.get("content", text),
        "summary": result.get("summary", ""),
        "notebooks": kb["notebooks"],
        "pages": kb["pages"],
    }


class ImportTextRequest(BaseModel):
    text: str


@router.post("/import/text")
async def import_text(request: ImportTextRequest, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if not settings.llm_api_url:
        raise HTTPException(status_code=500, detail="未配置 LLM API")

    if not request.text.strip():
        raise HTTPException(status_code=400, detail="文本内容为空")

    kb = _get_kb_context(db)
    prompt = ORGANIZE_PROMPT.format(
        kb_notebooks=json.dumps(kb["notebooks"], ensure_ascii=False),
        kb_pages=json.dumps(kb["pages"], ensure_ascii=False),
        source="用户粘贴文本",
        raw_content=request.text[:6000],
    )

    result = await _call_llm_json([{"role": "user", "content": prompt}], context="import-text")

    return {
        "should_save": result.get("should_save", True),
        "action": result.get("action", "create_note"),
        "title": result.get("title", "导入文本"),
        "notebook_id": result.get("notebook_id"),
        "new_notebook_name": result.get("new_notebook_name"),
        "update_page_id": result.get("update_page_id"),
        "content": result.get("content", request.text),
        "summary": result.get("summary", ""),
        "notebooks": kb["notebooks"],
        "pages": kb["pages"],
    }
