"""编译管道：把来源笔记本里的笔记，逐条 ingest 进目标空间的 wiki 页面树。

- 层级/结构由 LLM 结合管道 prompt 决定(引擎不设全局层级限制);
- 增量：仅编译上次成功运行后有更新的笔记; mode='full' 全量;
- 产出走 update 的 merge pass, 保留人工修改;
- 目标空间留空 = 默认空间; 所有笔记输出到空间内的 wiki 页面。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from app.models.database import CompileTemplate, Notebook, Page, Pipeline, PipelineRun, get_session, init_db
from app.core.wiki import ingest_note, resolve_space_id

logger = logging.getLogger(__name__)


def resolve_rule(db, pipeline: Pipeline) -> Dict[str, str]:
    """解析管道的有效编译规则: 模板优先(模板即编译方式), 无模板时用管道内联字段
    (兼容旧数据); 空项由编译步按 kind 以内置默认兜底。

    说明: 管道 UI 已不再选择编译方式, kind 由所选模板决定; 无模板时沿用管道
    自身字段(历史数据), 再回退内置默认。
    """
    if pipeline.template_id:
        t = db.query(CompileTemplate).filter(CompileTemplate.id == pipeline.template_id).first()
        if t is not None:
            return {
                "kind": t.compiler_kind or "wiki",
                "prompt": t.prompt or "",
                "rules": t.rules or "",
                "template": t.template or "",
            }
    return {
        "kind": pipeline.compiler_kind or "wiki",
        "prompt": pipeline.prompt_template or "",
        "rules": pipeline.compile_rules or "",
        "template": pipeline.compile_template or "",
    }

# 每个目标空间一把进程内锁, 序列化页面读写(后端单进程)
_space_locks: Dict[str, asyncio.Lock] = {}


def _space_lock(space_id: str) -> asyncio.Lock:
    lock = _space_locks.get(space_id)
    if lock is None:
        lock = asyncio.Lock()
        _space_locks[space_id] = lock
    return lock


def _collect_notes(db, pipeline: Pipeline, since: Optional[datetime]):
    if pipeline.scope_type == "notebooks":
        nb_ids = [x for x in json.loads(pipeline.notebook_ids or "[]") if x]
    elif pipeline.scope_type == "group":
        q = db.query(Notebook.id)
        if pipeline.group_id:
            q = q.filter(Notebook.group_id == pipeline.group_id)
        nb_ids = [r[0] for r in q.all()]
    else:
        nb_ids = [r[0] for r in db.query(Notebook.id).all()]

    notes = []
    for nb_id in nb_ids:
        q = db.query(Page).filter(
            Page.notebook_id == nb_id, Page.content.isnot(None), Page.content != ""
        )
        if since is not None:
            q = q.filter(Page.updated_at > since)
        notes.extend(q.order_by(Page.updated_at.asc()).all())
    return notes


async def run_pipeline(engine, pipeline_id: str, status: Dict[str, Any], mode: str = "incremental") -> None:
    init_db(engine)
    db = get_session(engine)
    run_id = str(uuid.uuid4())
    try:
        pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
        if not pipeline:
            status.update(running=False, message="管道不存在")
            return
        rule = resolve_rule(db, pipeline)
        kind = rule["kind"]
        prompt = rule["prompt"]
        rules = rule["rules"]
        template = rule["template"]
        model = pipeline.model or ""
        space_id = resolve_space_id(db, pipeline.target_space_id)

        since = None
        if mode != "full" and pipeline.incremental:
            last = (
                db.query(PipelineRun)
                .filter(PipelineRun.pipeline_id == pipeline.id, PipelineRun.status == "success")
                .order_by(PipelineRun.finished_at.desc())
                .first()
            )
            since = last.finished_at if last else None

        notes = _collect_notes(db, pipeline, since)
        space_name = ""
        from app.models.database import WikiSpace
        sp = db.query(WikiSpace).filter(WikiSpace.id == space_id).first()
        space_name = sp.name if sp else ""
    finally:
        db.close()

    status.update(total=len(notes), processed=0, changed=0, message="开始编译...")
    lock = _space_lock(space_id)

    changed = [0]
    done = [0]

    for note in notes:
        status.update(message=f"编译笔记 {done[0] + 1}/{len(notes)}：{note.title or '无标题'}")
        try:
            await ingest_note(
                engine, note, space_id, kind=kind, prompt=prompt, rules=rules,
                template=template, model=model, pipeline_id=pipeline_id, lock=lock,
            )
            changed[0] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("pipeline ingest failed %s: %s", note.id, e)
        done[0] += 1
        status.update(processed=done[0], changed=changed[0])

    # 写运行记录
    db = get_session(engine)
    try:
        run = db.query(PipelineRun).filter(PipelineRun.id == run_id).first()
        if run is None:
            run = PipelineRun(id=run_id, pipeline_id=pipeline_id)
            db.add(run)
        run.status = "success"
        run.processed = done[0]
        run.total = len(notes)
        run.changed = changed[0]
        run.message = f"编译完成：{changed[0]}/{len(notes)} 篇笔记"
        run.finished_at = datetime.now()
        db.commit()
    finally:
        db.close()
    status.update(running=False, message=f"编译完成：{changed[0]}/{len(notes)} 篇笔记")
    logger.info("pipeline %s done: %s", pipeline_id, changed[0])


async def preview_pipeline(engine, pipeline_id: str) -> Dict[str, Any]:
    """试编译来源范围内第一篇笔记(不落库), 返回 Markdown 预览。"""
    db = get_session(engine)
    try:
        pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
        if not pipeline:
            return {"ok": False, "error": "管道不存在"}
        space_id = resolve_space_id(db, pipeline.target_space_id)
        rule = resolve_rule(db, pipeline)
        notes = _collect_notes(db, pipeline, None)
        if not notes:
            return {"ok": False, "error": "来源范围内没有可编译的笔记"}
        note = notes[0]
        nb = db.query(Notebook.name).filter(Notebook.id == note.notebook_id).first()
        model = pipeline.model or ""
    finally:
        db.close()

    ops = await ingest_note(
        engine, note, space_id,
        kind=rule["kind"], prompt=rule["prompt"], rules=rule["rules"],
        template=rule["template"], model=model,
        pipeline_id=pipeline_id, dry_run=True,
    )
    if not ops:
        return {"ok": False, "error": "LLM 未返回内容(或判定笔记无价值)"}
    plan = [
        {"action": o.get("action", ""), "title": o.get("title", ""), "parent": o.get("parent", "")}
        for o in ops
    ]
    return {
        "ok": True,
        "notebook": nb[0] if nb else "",
        "title": note.title or "无标题",
        "summary": "",
        "plan": plan,
        "content": (ops[0].get("content") or ""),
    }
