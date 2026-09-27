"""编译管道：把某个范围内的笔记本笔记编译成 wiki 页。

- 输出统一落到 ``wiki_pages``（供 Wiki 浏览），带 ``pipeline_id``/``source_key`` 标记；
- 内置编译方式：wiki(蒸馏) / api_doc(接口文档) / markdown(合集) / changelog(变更) / custom；
- 增量：``incremental=True`` 时只编译"自上次成功运行后有更新"的笔记本。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.core.llm import call_llm_text
from app.models.database import (
    Notebook,
    Page,
    Pipeline,
    PipelineRun,
    WikiPage,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)

MAX_UNIT_CHARS = 60000

KIND_LABELS = {
    "wiki": "知识",
    "api_doc": "接口文档",
    "markdown": "文档",
    "changelog": "变更记录",
    "custom": "自定义",
}

PROMPTS = {
    "wiki": (
        "你是企业知识库编辑。下面是同一笔记本内的若干篇笔记，请把它们去重、合并、提炼成"
        "一篇结构化、可长期维护的 wiki 页正文（Markdown）。要求：合并同类信息、保留关键步骤/"
        "命令/参数/结论；不要罗列原始笔记标题；不要编造笔记中不存在的事实。"
        "只输出 Markdown 正文，第一行用 `# 标题` 给出页面标题。"
    ),
    "api_doc": (
        "你是接口文档工程师。根据下面的笔记，整理成规范的接口文档（Markdown）。要求包含："
        "概述、鉴权方式、接口清单（每个接口给出 方法/路径/请求参数/返回字段/错误码）、调用示例。"
        "信息以笔记为准，缺失处标注「待补充」。只输出 Markdown，第一行用 `# 标题` 给出标题。"
    ),
    "markdown": (
        "把下面的笔记整理成一篇通顺的 Markdown 文档：合理分节、顺序清晰、术语一致。"
        "只输出 Markdown，第一行用 `# 标题` 给出标题。"
    ),
    "changelog": (
        "把下面的笔记整理成变更记录（Markdown）：按时间或版本倒序，每条包含 变更点/影响/负责人(若有)。"
        "只输出 Markdown，第一行用 `# 标题` 给出标题。"
    ),
}


def _kind_prompt(kind: str, template: str) -> str:
    if kind == "custom":
        return template.strip() or PROMPTS["wiki"]
    return PROMPTS.get(kind, PROMPTS["wiki"])


def _first_heading(text: str, fallback: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip() or fallback
        if s:
            return s[:60]
    return fallback


def _summary(text: str, limit: int = 300) -> str:
    body = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        body.append(s)
        if sum(len(x) for x in body) >= limit:
            break
    return (" ".join(body))[:limit]


async def _compile_unit(
    kind: str,
    template: str,
    notebook_name: str,
    notes: List[Dict[str, str]],
    llm_model_override: str = "",
) -> Optional[Tuple[str, str, str]]:
    joined = "\n\n".join(
        f"### {n['title']}\n\n{n['content']}" for n in notes
    )[:MAX_UNIT_CHARS]
    sys_prompt = _kind_prompt(kind, template)
    user = f"笔记本：{notebook_name}\n\n{joined}"
    text = await call_llm_text(
        [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
        context=f"pipeline-{kind}",
        timeout=300.0,
        model=llm_model_override,
    )
    if not text.strip():
        return None
    title = _first_heading(text, notebook_name)
    return title, _summary(text), text


async def run_pipeline(engine, pipeline_id: str, status: Dict[str, Any]) -> None:
    """执行一个编译管道；status 为可变进度字典(调用方展示)。"""
    init_db(engine)

    def _set(**kw):
        status.update(kw)

    db = get_session(engine)
    run_id = str(uuid.uuid4())
    try:
        pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
        if not pipeline:
            _set(running=False, message="管道不存在")
            return
        kind = pipeline.compiler_kind or "wiki"
        template = pipeline.prompt_template or ""

        # 1) 解析来源笔记本
        if pipeline.scope_type == "notebooks":
            nb_ids = [x for x in json.loads(pipeline.notebook_ids or "[]") if x]
        elif pipeline.scope_type == "group":
            q = db.query(Notebook.id)
            if pipeline.group_id:
                q = q.filter(Notebook.group_id == pipeline.group_id)
            nb_ids = [r[0] for r in q.all()]
        else:
            nb_ids = [r[0] for r in db.query(Notebook.id).all()]

        # 2) 增量起点：上次成功运行的完成时间
        since = None
        if pipeline.incremental:
            last = (
                db.query(PipelineRun)
                .filter(PipelineRun.pipeline_id == pipeline.id, PipelineRun.status == "success")
                .order_by(PipelineRun.finished_at.desc())
                .first()
            )
            since = last.finished_at if last else None

        # 3) 组装编译单元(每个笔记本一个单元)
        units: List[Tuple[Notebook, List[Dict[str, str]], List[str]]] = []
        for nb_id in nb_ids:
            nb = db.query(Notebook).filter(Notebook.id == nb_id).first()
            if nb is None:
                continue
            q = db.query(Page).filter(Page.notebook_id == nb_id, Page.content.isnot(None), Page.content != "")
            if since is not None:
                q = q.filter(Page.updated_at > since)
            pages = q.order_by(Page.updated_at.desc()).all()
            if not pages:
                continue
            notes = [{"id": p.id, "title": p.title or "无标题", "content": p.content or ""} for p in pages]
            units.append((nb, notes, [p.id for p in pages]))

        _set(total=len(units), processed=0, changed=0, message="开始编译...")

        # 4) 逐单元编译 + upsert 到 wiki_pages
        changed = 0
        for idx, (nb, notes, note_ids) in enumerate(units, start=1):
            _set(message=f"正在编译：{nb.name} ({idx}/{len(units)})")
            compiled = await _compile_unit(kind, template, nb.name, notes, pipeline.model or "")
            if compiled is None:
                _set(processed=idx)
                continue
            title, summary, content = compiled
            source_key = f"{pipeline.id}:{nb.id}"
            page = db.query(WikiPage).filter(WikiPage.source_key == source_key).first()
            if page is None:
                # 优先复用同名 wiki 页，避免 title 唯一约束冲突
                page = db.query(WikiPage).filter(WikiPage.title == title).first()
            if page is None:
                page = WikiPage(
                    id=str(uuid.uuid4()),
                    title=title,
                    category=(pipeline.target_category or KIND_LABELS.get(kind, "编译产物")),
                    group_id=pipeline.group_id or nb.group_id,
                    source_key=source_key,
                )
                db.add(page)
            else:
                page.title = title
                if pipeline.target_category:
                    page.category = pipeline.target_category
                if not page.group_id:
                    page.group_id = pipeline.group_id or nb.group_id
                page.source_key = source_key
            page.content = content
            page.summary = summary
            page.pipeline_id = pipeline.id
            page.source_note_ids = json.dumps(note_ids, ensure_ascii=False)
            db.commit()
            changed += 1
            _set(processed=idx, changed=changed)

            # 刷新该页向量(默认档案),失败不阻断
            try:
                from app.core.wiki_embedding import embed_wiki_pages
                await embed_wiki_pages(engine, [page.id])
            except Exception as e:  # noqa: BLE001
                logger.warning("wiki 页向量刷新失败 %s: %s", page.id, e)

        # 5) 写运行记录
        run = db.query(PipelineRun).filter(PipelineRun.id == run_id).first()
        if run is None:
            run = PipelineRun(id=run_id, pipeline_id=pipeline.id)
            db.add(run)
        run.status = "success"
        run.processed = len(units)
        run.total = len(units)
        run.changed = changed
        run.message = f"编译完成：{changed}/{len(units)} 个笔记本产出"
        run.finished_at = datetime.now()
        db.commit()
        _set(running=False, message=run.message)
        logger.info("pipeline %s done: %s", pipeline.name, run.message)
    except Exception as e:  # noqa: BLE001
        logger.exception("pipeline run failed: %s", e)
        try:
            run = db.query(PipelineRun).filter(PipelineRun.id == run_id).first()
            if run is None:
                run = PipelineRun(id=run_id, pipeline_id=pipeline_id, status="failed")
                db.add(run)
            run.status = "failed"
            run.error = str(e)[:2000]
            run.finished_at = datetime.now()
            db.commit()
        except Exception:  # noqa: BLE001
            pass
        _set(running=False, message=f"编译失败: {e}")
    finally:
        db.close()


async def preview_pipeline(engine, pipeline_id: str, max_notes: int = 8) -> Dict[str, Any]:
    """试编译第一个来源笔记本(不落库),返回 Markdown 供预览。"""
    db = get_session(engine)
    try:
        pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
        if not pipeline:
            return {"ok": False, "error": "管道不存在"}
        nb_id = None
        if pipeline.scope_type == "notebooks":
            ids = [x for x in json.loads(pipeline.notebook_ids or "[]") if x]
            nb_id = ids[0] if ids else None
        if nb_id is None:
            nb = db.query(Notebook).order_by(Notebook.updated_at.desc()).first()
        else:
            nb = db.query(Notebook).filter(Notebook.id == nb_id).first()
        if nb is None:
            return {"ok": False, "error": "没有可用于试编译的笔记本"}
        pages = (
            db.query(Page)
            .filter(Page.notebook_id == nb.id, Page.content.isnot(None), Page.content != "")
            .order_by(Page.updated_at.desc())
            .limit(max_notes)
            .all()
        )
        notes = [{"id": p.id, "title": p.title or "无标题", "content": p.content or ""} for p in pages]
        compiled = await _compile_unit(
            pipeline.compiler_kind or "wiki", pipeline.prompt_template or "", nb.name, notes,
            pipeline.model or "",
        )
        if compiled is None:
            return {"ok": False, "error": "LLM 未返回内容(检查 LLM 配置)"}
        title, summary, content = compiled
        return {"ok": True, "notebook": nb.name, "title": title, "summary": summary, "content": content}
    finally:
        db.close()
