import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.core.llm import call_llm_json
from app.models.database import (
    Notebook,
    Page,
    SourceItem,
    get_engine,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)

INGEST_PROMPT = """你是一个企业知识库编辑。从数据源获取到一条原始内容，请分析并整理成一篇结构化笔记。

数据源：{source}
原始标题：{title}
原始内容：
{content}

要求：
1. 默认 should_save=true：只要内容包含可复用的信息（问题现象、原因分析、解决方案、步骤、参数、结论），即使很短也必须保存。
2. 仅当内容确属以下情况才 should_save=false：纯广告/推广、无意义闲聊、与工作无关、内容为空或完全重复。
3. 保存时整理成 Markdown 笔记：去噪、提炼核心、保留关键信息（步骤/命令/结论/代码/参数），标题精炼、信息完整。
4. 给出 3-5 个关键词（逗号分隔，中文/英文均可）。
5. 只返回 JSON，不要其他内容：
{{"should_save": true, "title": "精炼标题", "content": "Markdown 正文", "keywords": "kw1,kw2,kw3"}}"""


def _get_or_create_notebook(engine, name: str) -> str:
    db = get_session(engine)
    try:
        nb = db.query(Notebook).filter(Notebook.name == name).first()
        if nb is None:
            nb = Notebook(id=str(uuid.uuid4()), name=name)
            db.add(nb)
            db.commit()
            db.refresh(nb)
        return nb.id
    finally:
        db.close()


async def _index_imported(engine, page_id: str, title: str, content: str, keywords: str) -> None:
    """Vector + BM25 indexing (no LLM enrichment) + wiki refresh."""
    from app.core.hybrid import HybridIndex
    from app.core.rag import EmbeddingService, VectorStore

    emb = EmbeddingService()
    try:
        chunks = await emb.encode_chunks(content, title, enrich_context=False)
        db = get_session(engine)
        try:
            if chunks:
                await VectorStore(db).add_page_chunks(page_id, chunks)
            if not keywords:
                keywords = ",".join(EmbeddingService.extract_keywords(
                    (title or "") + " " + (content or ""), 20
                ))
            page = db.query(Page).filter(Page.id == page_id).first()
            if page:
                page.keywords = keywords
            HybridIndex(db).index_page(page_id, title, content, keywords)
            db.commit()
        finally:
            db.close()
    finally:
        await emb.close()

    from app.core.wiki import refresh_note_wiki
    try:
        await refresh_note_wiki(page_id)
    except Exception as e:
        logger.warning(f"Ingest wiki refresh failed for {page_id}: {e}")


async def analyze_and_upsert(
    engine,
    source_key: str,
    notebook_name: str,
    item_key: str,
    item_title: str,
    item_content: str,
    item_updated: Optional[datetime] = None,
    max_content: int = 6000,
) -> bool:
    """LLM analyze one source item -> create/update a note -> full index
    (vectors + BM25 + entity graph + wiki refresh)."""
    init_db(engine)
    result: Dict[str, Any] = {}
    try:
        result = await call_llm_json(
            [{"role": "user", "content": INGEST_PROMPT.format(
                source=source_key,
                title=(item_title or "")[:200],
                content=(item_content or "")[:max_content],
            )}],
            context=f"ingest-{source_key}",
            timeout=180.0,
        )
    except Exception as e:
        logger.warning(f"Ingest LLM failed for {source_key}/{item_key}: {e}")
        result = {}

    should_save = bool(result.get("should_save", True)) if isinstance(result, dict) else True
    title = ((result.get("title") if isinstance(result, dict) else None) or item_title or item_key).strip()
    content = ((result.get("content") if isinstance(result, dict) else None) or item_content or "").strip()
    keywords = ((result.get("keywords") if isinstance(result, dict) else None) or "").strip()

    notebook_id = _get_or_create_notebook(engine, notebook_name)
    db = get_session(engine)
    try:
        row = db.query(SourceItem).filter(
            SourceItem.source_key == source_key,
            SourceItem.item_key == item_key,
        ).first()
        page = db.query(Page).filter(Page.id == row.page_id).first() if row and row.page_id else None

        if not should_save or not content:
            if row is None:
                db.add(SourceItem(
                    id=str(uuid.uuid4()),
                    source_key=source_key,
                    item_key=item_key,
                    source_updated=item_updated,
                    skipped=True,
                ))
            else:
                row.skipped = True
                row.source_updated = item_updated
            db.commit()
            return False

        if page is None:
            page = Page(
                id=str(uuid.uuid4()),
                title=title,
                content=content,
                notebook_id=notebook_id,
                keywords=keywords,
            )
            db.add(page)
            db.commit()
            db.refresh(page)
            if row is None:
                db.add(SourceItem(
                    id=str(uuid.uuid4()),
                    source_key=source_key,
                    item_key=item_key,
                    page_id=page.id,
                    source_updated=item_updated,
                ))
            else:
                row.page_id = page.id
                row.skipped = False
                row.source_updated = item_updated
            db.commit()
        else:
            page.title = title
            page.content = content
            page.notebook_id = notebook_id
            if keywords:
                page.keywords = keywords
            if row:
                row.source_updated = item_updated
                row.skipped = False
            db.commit()
        page_id = page.id
    finally:
        db.close()

    try:
        await _index_imported(engine, page_id, title, content, keywords)
    except Exception as e:
        logger.warning(f"Ingest index failed for {page_id}: {e}")
    return True
