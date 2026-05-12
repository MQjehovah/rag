import json
import logging
import uuid
import asyncio
from typing import List, Dict, Any, Generator

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.models.database import Page, Notebook, get_session, get_engine, init_db
from app.core.jwt_utils import get_current_user

router = APIRouter(prefix="/api/organize", tags=["自动整理"])

logger = logging.getLogger(__name__)

_engine = None
_session = None

ORGANIZE_BATCH_PROMPT = """你是一个企业知识库管理助手。请分析以下笔记，完成全面整理。

当前笔记本列表：
{notebooks}

待整理笔记（共{total}篇，当前第{batch}批）：
{pages_info}

请完成以下任务：
1. 为每篇笔记推荐最合适的归类（现有笔记本ID，或建议创建新笔记本）
2. 为缺少摘要的笔记生成100字以内摘要
3. 为缺少关键词的笔记生成关键词（逗号分隔）
4. 识别内容重复或高度相似的笔记对
5. 识别笔记间的知识关联

以 JSON 格式返回：
{{
  "actions": [
    {{
      "page_id": "笔记ID",
      "action": "move/create_notebook/update",
      "notebook_id": "目标笔记本ID（move时）",
      "new_notebook_name": "新笔记本名称（create_notebook时）",
      "summary": "摘要",
      "keywords": "关键词1,关键词2"
    }}
  ],
  "duplicates": [
    {{"page_id_1": "ID1", "page_id_2": "ID2", "reason": "原因"}}
  ],
  "new_notebooks": [
    {{"name": "建议的新笔记本名称", "description": "用途说明"}}
  ]
}}
只返回 JSON，不要其他内容。"""


def get_db():
    global _engine, _session
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session


async def _call_llm_json(messages: list) -> dict:
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
        logger.info(f"[organize] LLM raw response ({len(content)} chars): {content[:3000]}")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            import re
            m = re.search(r'\{[\s\S]*\}', content)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
            logger.warning(f"[organize] Failed to parse LLM response: {content[:500]}")
            return {}


def _apply_actions(db: Session, result: dict, batch_pages: list) -> dict:
    stats = {"moved": 0, "created_notebooks": 0, "updated": 0, "skipped": 0}

    page_map = {p.id: p for p in batch_pages}
    actions = result.get("actions", [])

    nb_name_cache = {}
    for action in actions:
        page_id = action.get("page_id")
        page = page_map.get(page_id)
        if not page:
            try:
                page = db.query(Page).filter(Page.id == page_id).first()
            except Exception:
                continue
        if not page:
            continue

        act = action.get("action", "update")

        if act == "create_notebook":
            new_name = action.get("new_notebook_name", "")
            if new_name and new_name not in nb_name_cache:
                existing = db.query(Notebook).filter(Notebook.name == new_name).first()
                if existing:
                    nb_name_cache[new_name] = existing.id
                else:
                    nb = Notebook(id=str(uuid.uuid4()), name=new_name)
                    db.add(nb)
                    db.flush()
                    nb_name_cache[new_name] = nb.id
                    stats["created_notebooks"] += 1
            nb_id = nb_name_cache.get(new_name)
            if nb_id:
                page.notebook_id = nb_id
                stats["moved"] += 1

        elif act == "move":
            nb_id = action.get("notebook_id")
            if nb_id:
                page.notebook_id = nb_id
                stats["moved"] += 1

        summary = action.get("summary")
        keywords = action.get("keywords")
        if summary and not page.keywords:
            page.keywords = keywords
        if keywords:
            page.keywords = keywords
        stats["updated"] += 1

    for new_nb in result.get("new_notebooks", []):
        name = new_nb.get("name", "")
        if name and name not in nb_name_cache:
            existing = db.query(Notebook).filter(Notebook.name == name).first()
            if not existing:
                nb = Notebook(id=str(uuid.uuid4()), name=name)
                db.add(nb)
                stats["created_notebooks"] += 1

    db.commit()
    return stats


async def run_organize(db: Session) -> Generator[str, None, None]:
    if not settings.llm_api_url:
        yield f"data: {json.dumps({'type': 'error', 'content': '未配置 LLM API'}, ensure_ascii=False)}\n\n"
        return

    notebooks = db.query(Notebook).all()
    nb_list = [{"id": nb.id, "name": nb.name} for nb in notebooks]
    nb_json = json.dumps(nb_list, ensure_ascii=False)

    pages = db.query(Page).order_by(Page.updated_at.desc()).all()
    total = len(pages)

    if total == 0:
        yield f"data: {json.dumps({'type': 'done', 'stats': {'total': 0}}, ensure_ascii=False)}\n\n"
        return

    yield f"data: {json.dumps({'type': 'progress', 'message': f'开始整理 {total} 篇笔记...'}, ensure_ascii=False)}\n\n"

    batch_size = 8
    all_stats = {"moved": 0, "created_notebooks": 0, "updated": 0, "skipped": 0}

    for i in range(0, total, batch_size):
        batch = pages[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        yield f"data: {json.dumps({'type': 'progress', 'message': f'分析第 {batch_num}/{total_batches} 批...'}, ensure_ascii=False)}\n\n"

        pages_info = ""
        for p in batch:
            content_preview = (p.content or "")[:200]
            pages_info += f"\nID: {p.id}\n标题: {p.title}\n笔记本ID: {p.notebook_id}\n关键词: {p.keywords or '无'}\n内容预览: {content_preview}\n---"

        prompt = ORGANIZE_BATCH_PROMPT.format(
            notebooks=nb_json,
            total=total,
            batch=batch_num,
            pages_info=pages_info,
        )

        try:
            result = await _call_llm_json([{"role": "user", "content": prompt}])
            logger.info(f"[organize] Batch {batch_num} parsed result: {json.dumps(result, ensure_ascii=False)[:3000]}")
            stats = _apply_actions(db, result, batch)
            for k in all_stats:
                all_stats[k] += stats.get(k, 0)
        except Exception as e:
            logger.error(f"Organize batch {batch_num} error: {e}")
            yield f"data: {json.dumps({'type': 'warning', 'message': f'第 {batch_num} 批处理失败: {str(e)[:100]}'}, ensure_ascii=False)}\n\n"

    nb_list_after = [{"id": nb.id, "name": nb.name} for nb in db.query(Notebook).all()]
    yield f"data: {json.dumps({'type': 'done', 'stats': all_stats, 'notebooks': nb_list_after}, ensure_ascii=False)}\n\n"


@router.post("")
async def organize(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if "__local_admin__" not in current_user["groups"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行自动整理")

    async def generate():
        async for chunk in run_organize(db):
            yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream")


_organize_running = False


def run_organize_sync():
    global _organize_running
    if _organize_running:
        logger.info("Auto-organize already running, skipping")
        return
    _organize_running = True
    try:
        engine = get_engine(settings.database_url)
        init_db(engine)
        db = get_session(engine)

        async def _run():
            async for chunk in run_organize(db):
                logger.info(chunk.strip())

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_run())
        loop.close()
        db.close()
        logger.info("Auto-organize completed")
    except Exception as e:
        logger.error(f"Auto-organize failed: {e}")
    finally:
        _organize_running = False


def start_scheduler():
    if not settings.auto_organize_enabled:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            run_organize_sync,
            "interval",
            hours=settings.auto_organize_interval_hours,
            id="auto_organize",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(f"Auto-organize scheduler started (every {settings.auto_organize_interval_hours}h)")
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")
