import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.core.llm import call_llm_json, call_llm_text
from app.models.database import Notebook, Page, WikiPage, get_engine, get_session, init_db

logger = logging.getLogger(__name__)

IMAGE_RE = re.compile(r'!\[[^\]]*\]\([^)]*\)')

WIKI_PROMPT = """你是一个企业知识库 Wiki 编辑。知识库由多篇原始笔记蒸馏而来，你负责把新笔记的信息整合进 Wiki。

现有 Wiki 页面索引（标题 | 分类 | 摘要）：
{index}

新笔记：
标题：{title}
来源笔记本：{notebook}
内容：
{content}

规则：
1. 先判断笔记内容是否有价值；如果是垃圾、重复或信息量极低，返回空 ops。
2. 有新增知识时：
   - 如果现有页面能容纳，用 update 更新（给出该页面的完整新正文）；
   - 如果是新主题，用 create 新建页面；
   - 每篇笔记最多 create/update 共 2 个页面，聚焦核心知识，不要罗列流水账。
3. 分类从以下选择或自拟简洁分类：产品资料、操作指南、故障排查、开发技术、部署运维、业务流程。
4. 正文用 Markdown；页面间引用用 [[页面标题]] 语法；保留关键命令/代码；内容要具体可执行，不要泛泛而谈。
5. 每页给 30 字以内的摘要。
6. 只返回 JSON，不要其他内容：
{{"ops": [{{"action": "create", "title": "...", "category": "...", "content": "...", "summary": "..."}}, {{"action": "update", "title": "现有页面标题", "content": "完整新正文", "summary": "..."}}]}}
"""

MERGE_PROMPT = """你正在更新一个 Wiki 页面。请把"现有页面"和"新资料"合并成一份最终 Markdown 正文：
- 保留现有页面中仍然有效的内容（包括用户人工润色/修正过的段落），不要丢弃；
- 用新资料补充、修正、扩展；
- 删除已被新资料取代的过时内容；
- 保持原有结构，必要时新增小节。

现有页面《{title}》内容：
{current}

新资料（笔记《{note}》）：
{note_excerpt}

只输出合并后的 Markdown 正文，不要任何其他内容。"""


def _clean_content(content: str, limit: int = 4000) -> str:
    if not content:
        return ""
    cleaned = IMAGE_RE.sub("[图片]", content)
    cleaned = re.sub(r'data:image/[^)]+', '[base64图片]', cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "\n...(内容过长已截断)"
    return cleaned


def _index_text(pages: Dict[str, Dict[str, Any]], limit: int = 6000) -> str:
    if not pages:
        return "(暂无页面)"
    lines = []
    for p in pages.values():
        summary = (p.get("summary") or "").replace("\n", " ")[:60]
        lines.append(f"- {p.get('title', '')} | {p.get('category', '') or '未分类'} | {summary}")
        if sum(len(l) for l in lines) > limit:
            break
    return "\n".join(lines)


def _find_page(pages: Dict[str, Dict[str, Any]], title: str) -> Optional[Dict[str, Any]]:
    for key, p in pages.items():
        if key.lower() == title.strip().lower():
            return p
    return None


def _apply_ops(
    pages: Dict[str, Dict[str, Any]],
    ops: List[Dict[str, Any]],
    note_id: str,
) -> List[Dict[str, Any]]:
    changed = []
    for op in ops or []:
        action = op.get("action")
        title = (op.get("title") or "").strip()
        if not title or not action:
            continue
        content = (op.get("content") or "").strip()
        if not content:
            continue
        summary = (op.get("summary") or "").strip()[:200]
        category = (op.get("category") or "").strip()[:128]
        existing = _find_page(pages, title)
        if action == "update" and existing is not None:
            existing["content"] = content
            if summary:
                existing["summary"] = summary
            if category:
                existing["category"] = category
            existing["sources"].add(note_id)
            changed.append(existing)
        else:
            new_page = {
                "id": str(uuid.uuid4()),
                "title": title,
                "category": category or "未分类",
                "content": content,
                "summary": summary,
                "sources": {note_id},
            }
            pages[title] = new_page
            changed.append(new_page)
    return changed


def _persist(engine, changed: List[Dict[str, Any]]) -> None:
    if not changed:
        return
    db = get_session(engine)
    try:
        for p in changed:
            row = db.query(WikiPage).filter(WikiPage.title == p["title"]).first()
            if row is None:
                row = WikiPage(id=p["id"], title=p["title"])
                db.add(row)
            row.category = p["category"] or "未分类"
            row.content = p["content"]
            row.summary = p["summary"]
            row.source_note_ids = json.dumps(sorted(p["sources"]), ensure_ascii=False)
        db.commit()
    except Exception as e:
        logger.warning(f"Wiki persist error: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _load_pages(engine) -> Dict[str, Dict[str, Any]]:
    pages: Dict[str, Dict[str, Any]] = {}
    db = get_session(engine)
    try:
        for p in db.query(WikiPage).all():
            pages[p.title] = {
                "id": p.id,
                "title": p.title,
                "category": p.category or "未分类",
                "content": p.content or "",
                "summary": p.summary or "",
                "sources": set(json.loads(p.source_note_ids or "[]")),
            }
    finally:
        db.close()
    return pages


async def _ingest_one(
    note_id: str,
    title: str,
    notebook: str,
    content: str,
    pages: Dict[str, Dict[str, Any]],
    engine,
    lock: Optional[asyncio.Lock] = None,
) -> None:
    """Distill one note into the wiki. Update ops get a merge pass so any
    human refinements on existing pages survive recompilation."""
    async def _read_index():
        if lock:
            async with lock:
                return _index_text(pages)
        return _index_text(pages)

    index = await _read_index()
    prompt = WIKI_PROMPT.format(
        index=index,
        title=title or "无标题",
        notebook=notebook or "未分类",
        content=_clean_content(content or ""),
    )
    try:
        result = await call_llm_json(
            [{"role": "user", "content": prompt}],
            context="wiki-ingest",
            timeout=180.0,
        )
        ops = result.get("ops", []) if isinstance(result, dict) else []
    except Exception as e:
        logger.warning(f"Wiki ingest failed for {note_id}: {e}")
        return

    # merge pass: for updates, show the LLM the current page so manual
    # refinements are preserved
    final_ops: List[Dict[str, Any]] = []
    for op in ops or []:
        if op.get("action") == "update" and op.get("title"):
            current = _find_page(pages, op["title"])
            if current is not None:
                merge_prompt = MERGE_PROMPT.format(
                    title=current["title"],
                    current=(current["content"] or "")[:6000],
                    note=title or "无标题",
                    note_excerpt=_clean_content(content or "", 3000),
                )
                try:
                    merged_text = await call_llm_text(
                        [{"role": "user", "content": merge_prompt}],
                        context="wiki-merge",
                        timeout=180.0,
                    )
                    if merged_text:
                        op["content"] = merged_text
                except Exception as e:
                    logger.warning(f"Wiki merge failed for {op['title']}: {e}")
        final_ops.append(op)

    if lock:
        async with lock:
            changed = _apply_ops(pages, final_ops, note_id)
            _persist(engine, changed)
    else:
        changed = _apply_ops(pages, final_ops, note_id)
        _persist(engine, changed)


async def refresh_note_wiki(note_id: str) -> None:
    """Single-note ingest, called when a note is created/updated."""
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        note = db.query(Page).filter(Page.id == note_id).first()
        if not note or not (note.content or "").strip():
            return
        notebook = ""
        if note.notebook_id:
            nb = db.query(Notebook.name).filter(Notebook.id == note.notebook_id).first()
            notebook = nb[0] if nb else ""
    finally:
        db.close()
    pages = _load_pages(engine)
    await _ingest_one(
        note.id,
        note.title or "",
        notebook,
        note.content or "",
        pages,
        engine,
    )


async def build_wiki(
    status: Dict[str, Any],
    concurrency: int = 3,
) -> None:
    """Distill all notes into wiki pages (Karpathy-style incremental ingest)."""
    engine = get_engine(settings.database_url)
    init_db(engine)

    db = get_session(engine)
    try:
        notes = db.query(
            Page.id, Page.title, Page.notebook_id, Page.content
        ).filter(
            Page.content.isnot(None), Page.content != ""
        ).all()
        notebook_names = {}
        for nb_id, nb_name in db.query(Notebook.id, Notebook.name).all():
            notebook_names[nb_id] = nb_name
    finally:
        db.close()

    status["total"] = len(notes)
    status["processed"] = 0
    status["running"] = True
    status["message"] = "加载笔记完成，开始蒸馏"

    pages = _load_pages(engine)
    lock = asyncio.Lock()
    processed = 0
    started = time.time()

    async def worker(note):
        nonlocal processed
        await _ingest_one(
            note[0],
            note[1],
            notebook_names.get(note[2], ""),
            note[3],
            pages,
            engine,
            lock,
        )
        processed += 1
        status["processed"] = processed
        status["message"] = f"已蒸馏 {processed}/{len(notes)} 篇"

    sem = asyncio.Semaphore(concurrency)

    async def guarded(note):
        async with sem:
            await worker(note)

    tasks = [asyncio.create_task(guarded(n)) for n in notes]
    if tasks:
        await asyncio.gather(*tasks)

    status["running"] = False
    status["message"] = (
        f"Wiki 编译完成：{len(pages)} 个页面，"
        f"用时 {round((time.time() - started) / 60, 1)} 分钟"
    )
    logger.info(f"Wiki build done: {len(pages)} pages")
