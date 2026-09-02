import json
import logging
from typing import List, Dict, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import settings
from app.models.database import Page, Notebook
from app.core.jwt_utils import get_current_user
from app.api.rag_chat import run_chat_query

router = APIRouter(prefix="/api/chat", tags=["AI问答"])

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    query: str
    scope_id: str | None = None


@router.post("")
async def chat(request: ChatRequest, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Phase G 默认 Chat：复用 /api/rag-chat 的封板服务（Wiki 优先分层检索 + 降级 + 债务）。

    不再查询 Published Card / KnowledgeObject。响应契约为 JSON（build_rag_response），
    与 /api/rag-chat 状态语义完全一致。
    """
    return await run_chat_query(db, current_user, request.query, request.scope_id)


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
