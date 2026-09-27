"""知识编译引擎(泛化版)。

把「笔记」逐条 ingest 进某个**空间**的 wiki 页面树:
- 由 LLM 决定 create/update 哪些页面、以及挂在哪个父页面下(层级由管道 prompt 约束);
- update 走 merge pass, 保留人工修改;
- 经典全局蒸馏 = 以「默认空间 + wiki 文体」调用同一引擎(内置默认管道)。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.core.llm import call_llm_json, call_llm_text
from app.core.wiki_embedding import embed_wiki_pages
from app.models.database import (
    DEFAULT_SPACE_NAME,
    Notebook,
    Page,
    WikiPage,
    WikiSpace,
    get_engine,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)

IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)]*)\)')

# 各文体的风格指令(供统一 ingest prompt 使用)
KIND_STYLES = {
    "wiki": (
        "把它整理成知识库条目: 去重、合并同类信息, 提炼成可长期维护、结构清晰的 Markdown;"
        "保留关键步骤/命令/参数/结论; 不要罗列原始笔记标题。"
    ),
    "api_doc": (
        "整理成规范的接口文档(Markdown): 概述、鉴权、接口清单(方法/路径/请求参数/返回字段/错误码)、调用示例;"
        "信息以笔记为准, 缺失处标注「待补充」。"
    ),
    "markdown": "整理成通顺的 Markdown 文档: 合理分节、顺序清晰、术语一致。",
    "changelog": "整理成变更记录(Markdown): 按时间或版本倒序, 每条包含 变更点/影响/负责人(若有)。",
    "custom": "",  # 由模板提供
}

INGEST_PROMPT = """你是企业知识库编辑。请把下面这篇新笔记的信息整合进知识库(空间：{space})。

现有页面(缩进表示层级 | 标题 | 分类 | 摘要)：
{index}

新笔记：
标题：{title}
来源笔记本：{notebook}
内容：
{content}

文体要求：
{style}

规则：
1. 先判断笔记是否有价值；垃圾、重复或信息量极低则返回空 ops。
2. 有新增知识时：
   - 现有页面能容纳 → update(给出该页完整新正文)；
   - 新主题 → create(给出 title/category/parent/content/summary)；
   - 每篇笔记最多 create/update 共 2 个页面，聚焦核心知识。
3. parent 填"现有页面标题"表示挂到该父页下；不确定或应作为顶层则填空字符串 ""。
4. 正文用 Markdown；页面间引用用 [[页面标题]]；保留关键命令/代码；图片保留笔记中的 Markdown 图片语法(仅用笔记里出现的 URL)。
5. 每页给 30 字以内的摘要。
6. 只返回 JSON，不要其他内容：
{{"ops": [{{"action": "create", "title": "...", "category": "...", "parent": "", "content": "...", "summary": "..."}}, {{"action": "update", "title": "现有页面标题", "content": "完整新正文", "summary": "..."}}]}}
"""

MERGE_PROMPT = """你正在更新一个知识库页面。请把"现有页面"和"新资料"合并成一份最终 Markdown 正文：
- 保留现有页面中仍然有效的内容(包括用户人工润色/修正过的段落)，不要丢弃；
- 用新资料补充、修正、扩展；删除已被新资料取代的过时内容；
- 保留现有页面中的图片链接；保持原有结构，必要时新增小节。

现有页面《{title}》内容：
{current}

新资料(笔记《{note}》)：
{note_excerpt}

只输出合并后的 Markdown 正文，不要任何其他内容。"""


def ensure_default_space(db: Session) -> WikiSpace:
    """取或建「默认空间」。"""
    space = db.query(WikiSpace).filter(WikiSpace.name == DEFAULT_SPACE_NAME).first()
    if space is None:
        space = WikiSpace(
            id=str(uuid.uuid4()), name=DEFAULT_SPACE_NAME, icon="📄",
            description="未指定空间的编译产物", position=0, group_id=None,
        )
        db.add(space)
        db.commit()
        db.refresh(space)
    return space


def resolve_space_id(db: Session, space_id: Optional[str]) -> str:
    """空 -> 默认空间 id。"""
    if space_id:
        return space_id
    return ensure_default_space(db).id


def _clean_content(content: str, limit: int = 4000, max_images: int = 12) -> str:
    if not content:
        return ""
    counter = [0]

    def _repl(m):
        alt = (m.group(1) or "").strip()
        url = (m.group(2) or "").strip()
        if url.startswith("data:") or counter[0] >= max_images:
            return "[图片]"
        if url.startswith("/api/files/") or url.startswith("/files/") or "docmost.xzrobot.com" in url:
            return "[图片]"
        counter[0] += 1
        return f"![{alt or '图片'}]({url})"

    cleaned = IMAGE_RE.sub(_repl, content)
    cleaned = re.sub(r'data:image/[^)]+', '[base64图片]', cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "\n...(内容过长已截断)"
    return cleaned


def _find_page(pages: Dict[str, Dict[str, Any]], title: str) -> Optional[Dict[str, Any]]:
    for key, p in pages.items():
        if key.lower() == (title or "").strip().lower():
            return p
    return None


def _depth_of(page: Dict[str, Any], pages: Dict[str, Dict[str, Any]]) -> int:
    d = 0
    cur = page
    seen = set()
    while cur and cur.get("parent_title"):
        title = (cur["parent_title"] or "").strip()
        if not title or title.lower() in seen:
            break
        seen.add(title.lower())
        parent = _find_page(pages, title)
        if not parent:
            break
        d += 1
        cur = parent
        if d > 50:
            break
    return d


def _index_text(pages: Dict[str, Dict[str, Any]], limit: int = 8000) -> str:
    if not pages:
        return "(暂无页面)"
    ordered = sorted(pages.values(), key=lambda p: (_depth_of(p, pages), p.get("title", "")))
    lines = []
    for p in ordered:
        summary = (p.get("summary") or "").replace("\n", " ")[:60]
        indent = "  " * _depth_of(p, pages)
        lines.append(f"{indent}- {p.get('title', '')} | {p.get('category', '') or '未分类'} | {summary}")
        if sum(len(x) for x in lines) > limit:
            break
    return "\n".join(lines)


def _apply_ops(
    pages: Dict[str, Dict[str, Any]],
    ops: List[Dict[str, Any]],
    note_id: str,
) -> List[Dict[str, Any]]:
    changed: List[Dict[str, Any]] = []
    seen_ids = set()

    def _push(p):
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            changed.append(p)

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
        parent_title = (op.get("parent") or "").strip()
        existing = _find_page(pages, title)
        if existing is not None:  # update 或 同名 create 都就地更新
            existing["content"] = content
            if summary:
                existing["summary"] = summary
            if category:
                existing["category"] = category
            if parent_title:
                existing["parent_title"] = parent_title
            existing["sources"].add(note_id)
            _push(existing)
        else:
            new_page = {
                "id": str(uuid.uuid4()),
                "title": title,
                "category": category or "未分类",
                "content": content,
                "summary": summary,
                "parent_title": parent_title,
                "sources": {note_id},
            }
            pages[title] = new_page
            _push(new_page)
    return changed


def _load_pages(engine, space_id: str) -> Dict[str, Dict[str, Any]]:
    pages: Dict[str, Dict[str, Any]] = {}
    db = get_session(engine)
    try:
        rows = db.query(WikiPage).filter(WikiPage.space_id == space_id).all()
        id2title = {r.id: r.title for r in rows}
        for p in rows:
            pages[p.title] = {
                "id": p.id,
                "title": p.title,
                "category": p.category or "未分类",
                "content": p.content or "",
                "summary": p.summary or "",
                "parent_title": id2title.get(p.parent_id, "") if p.parent_id else "",
                "sources": set(json.loads(p.source_note_ids or "[]")),
            }
    finally:
        db.close()
    return pages


def _persist(engine, changed: List[Dict[str, Any]], space_id: str, pipeline_id: Optional[str] = None) -> List[str]:
    if not changed:
        return []
    db = get_session(engine)
    written: List[str] = []
    try:
        rows = []
        for p in changed:
            row = db.query(WikiPage).filter(
                WikiPage.space_id == space_id, WikiPage.title == p["title"]
            ).first()
            new_row = row is None
            if new_row:
                row = WikiPage(id=p["id"], title=p["title"], space_id=space_id)
                db.add(row)
            row.category = p.get("category") or "未分类"
            row.content = p.get("content") or ""
            row.summary = p.get("summary") or ""
            row.source_note_ids = json.dumps(sorted(p.get("sources") or []), ensure_ascii=False)
            if pipeline_id:
                row.pipeline_id = pipeline_id
            rows.append((row, new_row, p))
        db.flush()
        # 解析父页面 + 给新页面分配 position
        for row, _new_row, p in rows:
            parent_title = (p.get("parent_title") or "").strip()
            if parent_title and parent_title.lower() != (row.title or "").lower():
                parent = (
                    db.query(WikiPage)
                    .filter(WikiPage.space_id == space_id, WikiPage.title == parent_title)
                    .first()
                )
                if parent is not None:
                    row.parent_id = parent.id
            if row.position is None:
                row.position = 0
        db.flush()
        for row, new_row, _p in rows:
            if new_row:
                max_pos = (
                    db.query(func.max(WikiPage.position))
                    .filter(WikiPage.space_id == space_id, WikiPage.parent_id == row.parent_id)
                    .scalar()
                )
                row.position = (max_pos or 0) + 1
            written.append(row.id)
        db.commit()
        return written
    except Exception as e:
        logger.warning(f"Wiki persist error: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return []
    finally:
        db.close()


def _build_prompt(kind: str, template: str, space_name: str, index: str,
                  title: str, notebook: str, content: str) -> str:
    # 自定义模板对任意编译方式都生效(用于约束 LLM 的编译行为); 留空则用内置文体风格
    style = (template or "").strip() or KIND_STYLES.get(kind, KIND_STYLES["wiki"])
    return INGEST_PROMPT.format(
        space=space_name, index=index, title=title or "无标题",
        notebook=notebook or "未分类", content=_clean_content(content or ""), style=style,
    )


async def _ingest_one(
    note_id: str,
    title: str,
    notebook: str,
    content: str,
    pages: Dict[str, Dict[str, Any]],
    engine,
    space_id: str,
    space_name: str,
    kind: str = "wiki",
    template: str = "",
    model: str = "",
    pipeline_id: Optional[str] = None,
    lock: Optional[asyncio.Lock] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """把一篇笔记 ingest 进空间; 返回(首个)生成正文(供预览), 或 None。"""
    async def _read_index():
        if lock:
            async with lock:
                return _index_text(pages)
        return _index_text(pages)

    index = await _read_index()
    prompt = _build_prompt(kind, template, space_name, index, title, notebook, content)
    try:
        result = await call_llm_json(
            [{"role": "user", "content": prompt}],
            context=f"pipeline-{kind}",
            timeout=300.0,
        )
        ops = result.get("ops", []) if isinstance(result, dict) else []
    except Exception as e:
        logger.warning(f"ingest failed for {note_id}: {e}")
        return None

    # merge pass: update 时把现有页面交给 LLM 合并, 保留人工修改
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
                    merged = await call_llm_text(
                        [{"role": "user", "content": merge_prompt}],
                        context="pipeline-merge", timeout=300.0, model=model,
                    )
                    if merged:
                        op["content"] = merged
                except Exception as e:
                    logger.warning(f"merge failed for {op.get('title')}: {e}")
        final_ops.append(op)

    preview_text = (final_ops[0].get("content") if final_ops else None)
    if dry_run:
        return preview_text

    if lock:
        async with lock:
            changed = _apply_ops(pages, final_ops, note_id)
            changed_ids = _persist(engine, changed, space_id, pipeline_id)
    else:
        changed = _apply_ops(pages, final_ops, note_id)
        changed_ids = _persist(engine, changed, space_id, pipeline_id)

    if changed_ids:
        await embed_wiki_pages(engine, changed_ids)
    return preview_text


async def ingest_note(
    engine,
    note,
    space_id: str,
    kind: str = "wiki",
    template: str = "",
    model: str = "",
    pipeline_id: Optional[str] = None,
    lock: Optional[asyncio.Lock] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """对单条笔记执行编译(note: Page 行或含 id/title/notebook_id/content 的对象)。"""
    db = get_session(engine)
    try:
        notebook = ""
        if getattr(note, "notebook_id", None):
            nb = db.query(Notebook.name).filter(Notebook.id == note.notebook_id).first()
            notebook = nb[0] if nb else ""
        space = db.query(WikiSpace).filter(WikiSpace.id == space_id).first()
        space_name = space.name if space else DEFAULT_SPACE_NAME
    finally:
        db.close()
    pages = _load_pages(engine, space_id)
    return await _ingest_one(
        note.id, note.title or "", notebook, note.content or "", pages, engine,
        space_id, space_name, kind, template, model, pipeline_id, lock, dry_run,
    )


# ---------------- 经典默认管道(默认空间 + wiki 文体) ----------------

async def refresh_note_wiki(note_id: str) -> None:
    """单条笔记 ingest 到默认空间(笔记保存时触发, 无显式自动管道时的兜底)。"""
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        note = db.query(Page).filter(Page.id == note_id).first()
        if not note or not (note.content or "").strip():
            return
        space = ensure_default_space(db)
        space_id = space.id
    finally:
        db.close()
    await ingest_note(engine, note, space_id, kind="wiki")


async def refresh_stale_wiki(status: Dict[str, Any]) -> None:
    """重编默认空间里有变化的笔记。"""
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        space = ensure_default_space(db)
        space_id = space.id
        space_pages = db.query(WikiPage.id, WikiPage.source_note_ids, WikiPage.updated_at).filter(
            WikiPage.space_id == space_id
        ).all()
        note_updated = dict(db.query(Page.id, Page.updated_at).all())
        stale = set()
        for _pid, src_json, page_updated in space_pages:
            try:
                src_ids = json.loads(src_json or "[]")
            except Exception:
                src_ids = []
            for sid in src_ids:
                if sid in note_updated and note_updated[sid] is not None:
                    if page_updated is None or note_updated[sid] > page_updated:
                        stale.add(sid)
    finally:
        db.close()

    if not stale:
        status.update(running=False, processed=0, total=0, message="没有需要刷新的页面")
        return

    db = get_session(engine)
    try:
        notes = db.query(Page).filter(Page.id.in_(stale)).all()
    finally:
        db.close()

    pages = _load_pages(engine, space_id)
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(3)
    done = [0]
    status.update(running=True, total=len(notes), processed=0, message=f"刷新 {len(notes)} 篇有变化的笔记")

    async def worker(n):
        async with sem:
            await _ingest_one(n.id, n.title or "", "", n.content or "", pages, engine, space_id,
                              DEFAULT_SPACE_NAME, "wiki", "", "", None, lock)
            done[0] += 1
            status.update(processed=done[0], message=f"刷新 {done[0]}/{len(notes)}")

    await asyncio.gather(*(worker(n) for n in notes))
    await embed_wiki_pages(engine)
    status.update(running=False, message=f"刷新完成：{len(notes)} 篇笔记重新编译")


async def build_wiki(status: Dict[str, Any], concurrency: int = 3) -> None:
    """全量蒸馏默认空间(经典全局编译)。"""
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        space = ensure_default_space(db)
        space_id = space.id
        notes = db.query(Page).filter(Page.content.isnot(None), Page.content != "").all()
    finally:
        db.close()

    status.update(total=len(notes), processed=0, running=True, message="加载笔记完成，开始蒸馏")
    pages = _load_pages(engine, space_id)
    lock = asyncio.Lock()
    processed = [0]
    started = time.time()
    sem = asyncio.Semaphore(concurrency)

    async def worker(n):
        async with sem:
            await _ingest_one(n.id, n.title or "", "", n.content or "", pages, engine, space_id,
                              DEFAULT_SPACE_NAME, "wiki", "", "", None, lock)
            processed[0] += 1
            status.update(processed=processed[0], message=f"已蒸馏 {processed[0]}/{len(notes)}")

    await asyncio.gather(*(worker(n) for n in notes))
    status.update(running=False, message=(
        f"Wiki 编译完成：{len(pages)} 个页面，用时 {round((time.time() - started) / 60, 1)} 分钟"
    ))
    await embed_wiki_pages(engine)
    logger.info(f"Wiki build done: {len(pages)} pages")
