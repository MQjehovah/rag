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

# 各文体的"提示词"(角色 + 目标), 供编译规则默认值使用
KIND_PROMPTS = {
    "wiki": "你是企业知识库编辑。把新笔记蒸馏、整合为可长期维护的知识库条目(Markdown)，面向读者、结构清晰、准确可执行。",
    "api_doc": "你是接口文档工程师。把新笔记中与接口相关的信息整理为规范、完整的接口文档(Markdown)。",
    "markdown": "你是文档编辑。把新笔记整理为通顺、规范的 Markdown 技术文档。",
    "changelog": "你是发布/变更记录编辑。把新笔记整理为结构化、可追溯的变更记录(Markdown)。",
    "custom": "",
}

# 通用规则(约束 LLM 的编译行为, 所有文体共用; 用户规则会追加在其后)
COMMON_RULES = """1. 只使用「新笔记」中提供的信息，不得编造；信息缺失处标注「待补充」。
2. 蒸馏而非摘抄：去重、合并同类信息、改写为通顺条目，剔除口语、寒暄与时间序流水账；不要整段照搬原文。
3. 原样保留：命令、代码、参数名、路径、数值、专有名词，以及笔记中的图片 Markdown 语法(仅用笔记里出现的 URL)。
4. 一页一主题：一个页面聚焦一个概念/流程；派生细节放入该页小节，不要混入其他主题。
5. 优先 update 现有页面，仅在确为新主题时 create；每篇笔记最多 create/update 共 2 个页面。
6. 保留人工修改：update 时不得丢弃现有页面中被人工润色/更正的内容，仅在确有新信息时改动。
7. 冲突处理：同一事实不一致时保留较新/更具体者，并在正文注明来源差异。
8. parent：填"现有页面标题"以挂到该父页下(同空间已存在)；不确定或应作为顶层则填空字符串 ""。
9. 交叉引用：相关页面用 [[页面标题]]。
10. 语言：简体中文；风格：准确、简洁、可执行。
11. 标题：简短的偏正名词短语(如「SW50 日常清洁作业流程」)，不要用句子。
12. 摘要：30 字以内，概括整页。"""

# 各文体的"输出模板"(页面正文结构骨架)
KIND_TEMPLATES = {
    "wiki": """# <页面标题>

（开篇 1-2 句：这是什么 / 解决什么问题）

## 概述
## 适用场景
## 操作步骤
1. 步骤 → 结果
## 关键参数 / 注意事项
- 
## 常见问题
- Q: … A: …
## 相关页面
- [[页面标题]]""",
    "api_doc": """# <接口名>

## 概述
## 鉴权
## 接口清单
### <方法> <路径>
- 请求参数：…
- 返回字段：…
- 错误码：…
## 调用示例
```bash
# 示例
```
## 相关页面
- [[页面标题]]""",
    "markdown": """# <文档标题>

## 背景
## 正文
（按内容合理分节）
## 小结
## 相关页面
- [[页面标题]]""",
    "changelog": """# <标题：变更记录>

（按时间/版本倒序）

## [YYYY-MM-DD] 变更点
- 影响：
- 负责人：
## 相关页面
- [[页面标题]]""",
    "custom": "",
}

INGEST_PROMPT = """<role>
{role}
</role>

<space>{space}</space>

<existing_pages>
索引格式：缩进表示层级 | 标题 | 分类 | 摘要
{index}
</existing_pages>

<new_note>
标题：{title}
来源笔记本：{notebook}
内容：
{content}
</new_note>

<rules>
{rules}
</rules>

<output_template>
页面正文尽量遵循下面的结构模板(可增删小节，但保持层次一致；模板中的 <页面标题> 等占位符请替换为实际内容)：
{template}
</output_template>

<output_format>
只返回 JSON，不要使用 Markdown 代码围栏，不要任何解释或前后缀。形状：
{{"ops": [
  {{"action": "create", "title": "新页面标题", "category": "分类", "parent": "父页面标题或空字符串", "content": "完整 Markdown 正文", "summary": "30字以内摘要"}},
  {{"action": "update", "title": "现有页面标题", "content": "合并后的完整 Markdown 正文", "summary": "30字以内摘要"}}
]}}
若笔记无价值/重复/信息量过低，返回 {{"ops": []}}。
</output_format>"""

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


def _build_prompt(kind: str, prompt: str, rules: str, template: str, space_name: str,
                  index: str, title: str, notebook: str, content: str) -> str:
    role = (prompt or "").strip() or KIND_PROMPTS.get(kind) or KIND_PROMPTS["wiki"]
    extra_rules = (rules or "").strip()
    all_rules = COMMON_RULES + ("\n" + extra_rules if extra_rules else "")
    tmpl = (template or "").strip() or KIND_TEMPLATES.get(kind, "")
    return INGEST_PROMPT.format(
        role=role, space=space_name, index=index, title=title or "无标题",
        notebook=notebook or "未分类", content=_clean_content(content or ""),
        rules=all_rules, template=tmpl or "(无特殊结构，按内容合理分节)",
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
    prompt: str = "",
    rules: str = "",
    template: str = "",
    model: str = "",
    pipeline_id: Optional[str] = None,
    lock: Optional[asyncio.Lock] = None,
    dry_run: bool = False,
) -> Any:
    """把一篇笔记 ingest 进空间; dry_run 时返回 ops 计划, 否则返回 None。"""
    async def _read_index():
        if lock:
            async with lock:
                return _index_text(pages)
        return _index_text(pages)

    index = await _read_index()
    prompt = _build_prompt(kind, prompt, rules, template, space_name, index, title, notebook, content)
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

    if dry_run:
        # 预览：返回 ops 计划(建/并哪些页、标题、父级), 不落库
        return final_ops

    if lock:
        async with lock:
            changed = _apply_ops(pages, final_ops, note_id)
            changed_ids = _persist(engine, changed, space_id, pipeline_id)
    else:
        changed = _apply_ops(pages, final_ops, note_id)
        changed_ids = _persist(engine, changed, space_id, pipeline_id)

    if changed_ids:
        await embed_wiki_pages(engine, changed_ids)
    return None


async def ingest_note(
    engine,
    note,
    space_id: str,
    kind: str = "wiki",
    prompt: str = "",
    rules: str = "",
    template: str = "",
    model: str = "",
    pipeline_id: Optional[str] = None,
    lock: Optional[asyncio.Lock] = None,
    dry_run: bool = False,
) -> Any:
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
        space_id, space_name, kind, prompt, rules, template, model, pipeline_id, lock, dry_run,
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
                              DEFAULT_SPACE_NAME, kind="wiki", lock=lock)
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
                              DEFAULT_SPACE_NAME, kind="wiki", lock=lock)
            processed[0] += 1
            status.update(processed=processed[0], message=f"已蒸馏 {processed[0]}/{len(notes)}")

    await asyncio.gather(*(worker(n) for n in notes))
    status.update(running=False, message=(
        f"Wiki 编译完成：{len(pages)} 个页面，用时 {round((time.time() - started) / 60, 1)} 分钟"
    ))
    await embed_wiki_pages(engine)
    logger.info(f"Wiki build done: {len(pages)} pages")
