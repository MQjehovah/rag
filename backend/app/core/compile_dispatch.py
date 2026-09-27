"""笔记变更时的编译派发。

规则：
- 找出 enabled & auto_trigger 且**范围覆盖**该笔记所在笔记本的管道 → 对每条管道仅编译这一条笔记;
- 若没有任何显式自动管道覆盖 → 走「内置默认管道」(默认空间 + wiki 文体);
- 限流：按 (笔记, 管道) 默认 60s；正在运行中再次变更 → 标记 pending, 结束后补跑一次。
"""
import asyncio
import json
import logging
import time
from typing import Dict, Set, Tuple

from app.config import settings
from app.core.pipeline import _space_lock, resolve_rule
from app.core.wiki import ingest_note, refresh_note_wiki, resolve_space_id
from app.models.database import (
    Notebook,
    Page,
    Pipeline,
    get_engine,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)

COOLDOWN = 60.0
_last: Dict[Tuple[str, str], float] = {}
_inflight: Set[Tuple[str, str]] = set()
_pending: Set[Tuple[str, str]] = set()


def _pipeline_covers(pipeline: Pipeline, notebook_id, notebook_group_id) -> bool:
    if pipeline.scope_type == "all":
        return True
    if pipeline.scope_type == "group":
        if not pipeline.group_id:
            return True
        return bool(notebook_group_id) and notebook_group_id == pipeline.group_id
    # notebooks
    try:
        ids = json.loads(pipeline.notebook_ids or "[]")
    except Exception:
        ids = []
    return bool(notebook_id) and notebook_id in ids


async def _ingest_for_pipeline(engine, pipeline_id: str, note_id: str) -> None:
    key = (pipeline_id, note_id)
    _inflight.add(key)
    try:
        db = get_session(engine)
        try:
            pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
            note = db.query(Page).filter(Page.id == note_id).first()
            if not pipeline or not note or not (note.content or "").strip():
                return
            space_id = resolve_space_id(db, pipeline.target_space_id)
            rule = resolve_rule(db, pipeline)
            model = pipeline.model or ""
        finally:
            db.close()
        try:
            await ingest_note(engine, note, space_id, kind=rule["kind"], prompt=rule["prompt"],
                              rules=rule["rules"], template=rule["template"], model=model,
                              pipeline_id=pipeline_id, lock=_space_lock(space_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("auto compile failed (%s/%s): %s", pipeline_id, note_id, e)
    finally:
        _inflight.discard(key)
        if key in _pending:
            _pending.discard(key)
            _last[key] = time.time()
            asyncio.create_task(_ingest_for_pipeline(engine, pipeline_id, note_id))


def _enqueue(engine, pipeline_id: str, note_id: str) -> None:
    key = (pipeline_id, note_id)
    now = time.time()
    if key in _inflight:
        _pending.add(key)
        return
    if now - _last.get(key, 0.0) < COOLDOWN:
        return
    _last[key] = now
    asyncio.create_task(_ingest_for_pipeline(engine, pipeline_id, note_id))


async def dispatch_note_compile(note_id: str) -> None:
    """笔记保存/创建后调用：为该笔记派发编译。"""
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        note = db.query(Page).filter(Page.id == note_id).first()
        if note is None or not (note.content or "").strip():
            return
        nb_group = None
        if note.notebook_id:
            nb = db.query(Notebook.group_id).filter(Notebook.id == note.notebook_id).first()
            nb_group = nb[0] if nb else None
        pipelines = db.query(Pipeline).filter(
            Pipeline.enabled.is_(True), Pipeline.auto_trigger.is_(True)
        ).all()
        covering = [p for p in pipelines if _pipeline_covers(p, note.notebook_id, nb_group)]
    finally:
        db.close()

    if not covering:
        await refresh_note_wiki(note_id)
        return
    for p in covering:
        _enqueue(engine, p.id, note_id)
