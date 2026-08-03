from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, List
from datetime import datetime
import hashlib
import uuid
import logging

from app.models.database import Page, Notebook, PageChunk, get_session, get_engine, init_db
from app.core.rag import EmbeddingService, VectorStore
from app.core.dingtalk import DingTalkClient
from app.core.jwt_utils import get_current_user
from app.config import settings

router = APIRouter(prefix="/api/dingtalk", tags=["钉钉同步"])

logger = logging.getLogger(__name__)

_engine = None
_session = None

SYNC_STATUS = {
    "running": False,
    "progress": "",
    "total": 0,
    "imported": 0,
    "skipped": 0,
    "errors": 0,
    "last_sync": "",
}


def get_db():
    global _engine, _session
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session


class SyncRequest(BaseModel):
    notebook_id: Optional[str] = None
    notebook_name: str = "钉钉知识库"
    space_id: Optional[str] = None


class SyncSelectedRequest(BaseModel):
    notebook_id: Optional[str] = None
    notebook_name: str = "钉钉知识库"
    docs: List[dict]


def _markdown_document(doc: dict) -> str:
    """将钉钉文档正文和来源信息统一整理为 Markdown。"""
    title = str(doc.get("title") or "无标题").replace("\r", " ").replace("\n", " ").strip()
    workspace = str(doc.get("space_name") or "").strip()
    path = str(doc.get("path") or "").strip()
    body = str(doc.get("content") or "").strip()

    metadata = []
    if workspace:
        metadata.append(f"> 来源：钉钉知识库 / {workspace}")
    if path:
        metadata.append(f"> 路径：{path}")

    parts = [f"# {title}"]
    if metadata:
        parts.append("\n".join(metadata))
    if body:
        parts.append(body)
    return "\n\n".join(parts).strip() + "\n"


async def _import_document(
    db: Session,
    vec_store: VectorStore,
    emb_svc: EmbeddingService,
    notebook_id: str,
    doc: dict,
) -> str:
    """按钉钉节点 ID 增量写入一篇文档，并保证页面与向量同事务提交。"""
    source_id = str(doc.get("id") or "").strip()
    if not source_id:
        raise ValueError("钉钉文档缺少节点 ID")

    title = str(doc.get("title") or "无标题").strip()
    markdown = _markdown_document(doc)
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()

    page = db.query(Page).filter(
        Page.source_type == "dingtalk",
        Page.source_id == source_id,
    ).first()

    if page is not None and page.content_hash == content_hash:
        page.source_path = doc.get("path") or ""
        page.source_space_id = (
            doc.get("space_id") or settings.dingtalk_knowledge_base_id or ""
        )
        page.last_synced_at = datetime.now()
        db.commit()
        return "skipped"

    chunks = await emb_svc.encode_chunks(markdown)
    if not chunks:
        raise RuntimeError("Embedding 服务未返回有效分块")

    try:
        if page is None:
            page = Page(id=str(uuid.uuid4()), notebook_id=notebook_id)
            db.add(page)

        page.title = title
        page.content = markdown
        page.keywords = ",".join(
            EmbeddingService.extract_keywords(title + " " + markdown, 20)
        )
        page.source_type = "dingtalk"
        page.source_id = source_id
        page.source_path = doc.get("path") or ""
        page.source_space_id = (
            doc.get("space_id") or settings.dingtalk_knowledge_base_id or ""
        )
        page.content_hash = content_hash
        page.last_synced_at = datetime.now()
        db.flush()

        await vec_store.add_page_chunks(page.id, chunks)
        db.commit()
        return "imported"
    except Exception:
        db.rollback()
        raise


async def _do_sync_selected(notebook_id: str, selected_docs: List[dict]):
    global SYNC_STATUS
    SYNC_STATUS["running"] = True
    SYNC_STATUS["progress"] = "正在下载文档内容..."
    SYNC_STATUS["imported"] = 0
    SYNC_STATUS["skipped"] = 0
    SYNC_STATUS["errors"] = 0
    SYNC_STATUS["total"] = len(selected_docs)

    try:
        client = DingTalkClient()
        emb_svc = EmbeddingService()

        engine = get_engine(settings.database_url)
        db = get_session(engine)
        vec_store = VectorStore(db)

        SYNC_STATUS["progress"] = f"正在下载 0/{SYNC_STATUS['total']}..."

        docs = await client.collect_selected_docs(selected_docs)

        SYNC_STATUS["total"] = len(docs)
        SYNC_STATUS["progress"] = f"下载完成，开始导入 0/{SYNC_STATUS['total']}..."

        for i, doc in enumerate(docs):
            try:
                result = await _import_document(
                    db, vec_store, emb_svc, notebook_id, doc
                )
                SYNC_STATUS[result] += 1
                handled = SYNC_STATUS["imported"] + SYNC_STATUS["skipped"]
                SYNC_STATUS["progress"] = (
                    f"已处理 {handled}/{SYNC_STATUS['total']} "
                    f"(写入 {SYNC_STATUS['imported']}，跳过 {SYNC_STATUS['skipped']})"
                )
            except Exception as e:
                db.rollback()
                SYNC_STATUS["errors"] += 1
                logger.error(f"Import failed for doc {i}: {e}")

        db.close()
        await client.close()
        await emb_svc.close()

        SYNC_STATUS["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        SYNC_STATUS["progress"] = (
            f"完成: {SYNC_STATUS['imported']} 写入, "
            f"{SYNC_STATUS['skipped']} 未变化, {SYNC_STATUS['errors']} 失败"
        )
    except Exception as e:
        SYNC_STATUS["progress"] = f"同步失败: {e}"
        logger.error(f"DingTalk sync failed: {e}")
    finally:
        SYNC_STATUS["running"] = False


async def _do_sync(notebook_id: str, space_id: str = None):
    global SYNC_STATUS
    SYNC_STATUS["running"] = True
    SYNC_STATUS["progress"] = "连接钉钉..."
    SYNC_STATUS["imported"] = 0
    SYNC_STATUS["skipped"] = 0
    SYNC_STATUS["errors"] = 0
    SYNC_STATUS["total"] = 0

    try:
        client = DingTalkClient()
        emb_svc = EmbeddingService()

        engine = get_engine(settings.database_url)
        db = get_session(engine)

        SYNC_STATUS["progress"] = "正在获取文档列表..."

        collected_docs = []
        async def on_doc_collected(doc, count):
            collected_docs.append(doc)
            SYNC_STATUS["total"] = count
            SYNC_STATUS["progress"] = f"正在获取文档 ({count})..."

        docs = await client.collect_all_docs(space_id, on_progress=on_doc_collected)
        SYNC_STATUS["total"] = len(docs)
        SYNC_STATUS["progress"] = f"发现 {len(docs)} 个文档，开始导入..."

        vec_store = VectorStore(db)

        for i, doc in enumerate(docs):
            try:
                result = await _import_document(
                    db, vec_store, emb_svc, notebook_id, doc
                )
                SYNC_STATUS[result] += 1
                handled = SYNC_STATUS["imported"] + SYNC_STATUS["skipped"]
                SYNC_STATUS["progress"] = (
                    f"已处理 {handled}/{SYNC_STATUS['total']} "
                    f"(写入 {SYNC_STATUS['imported']}，跳过 {SYNC_STATUS['skipped']})"
                )
            except Exception as e:
                db.rollback()
                SYNC_STATUS["errors"] += 1
                logger.error(f"Import failed for doc {i}: {e}")

        db.close()
        await client.close()
        await emb_svc.close()

        SYNC_STATUS["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        SYNC_STATUS["progress"] = (
            f"完成: {SYNC_STATUS['imported']} 写入, "
            f"{SYNC_STATUS['skipped']} 未变化, {SYNC_STATUS['errors']} 失败"
        )
    except Exception as e:
        SYNC_STATUS["progress"] = f"同步失败: {e}"
        logger.error(f"DingTalk sync failed: {e}")
    finally:
        SYNC_STATUS["running"] = False


@router.get("/docs")
async def list_docs(
    space_id: Optional[str] = None,
    current_user=Depends(get_current_user),
):
    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=400, detail="请先配置钉钉参数")

    client = DingTalkClient()
    try:
        docs = await client.list_all_docs(space_id)
        return {"total": len(docs), "docs": docs}
    finally:
        await client.close()


@router.post("/sync-selected")
async def start_sync_selected(
    req: SyncSelectedRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if SYNC_STATUS["running"]:
        raise HTTPException(status_code=409, detail="同步正在进行中")

    if not req.docs:
        raise HTTPException(status_code=400, detail="请选择要同步的文档")

    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=400, detail="请先配置 DINGTALK_APP_KEY 和 DINGTALK_APP_SECRET")

    notebook_id = req.notebook_id
    if not notebook_id:
        nb = db.query(Notebook).filter(Notebook.name == req.notebook_name).first()
        if not nb:
            nb = Notebook(id=str(uuid.uuid4()), name=req.notebook_name)
            db.add(nb)
            db.commit()
            db.refresh(nb)
        notebook_id = nb.id

    background_tasks.add_task(_do_sync_selected, notebook_id, req.docs)
    return {"message": "同步已启动", "notebook_id": notebook_id}


@router.post("/sync")
async def start_sync(
    req: SyncRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if SYNC_STATUS["running"]:
        raise HTTPException(status_code=409, detail="同步正在进行中")

    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=400, detail="请先配置 DINGTALK_APP_KEY 和 DINGTALK_APP_SECRET")

    notebook_id = req.notebook_id
    if not notebook_id:
        nb = db.query(Notebook).filter(Notebook.name == req.notebook_name).first()
        if not nb:
            nb = Notebook(id=str(uuid.uuid4()), name=req.notebook_name)
            db.add(nb)
            db.commit()
            db.refresh(nb)
        notebook_id = nb.id

    background_tasks.add_task(_do_sync, notebook_id, req.space_id)
    return {"message": "同步已启动", "notebook_id": notebook_id}


@router.get("/status")
async def get_sync_status(current_user=Depends(get_current_user)):
    return SYNC_STATUS


@router.get("/spaces")
async def list_spaces(current_user=Depends(get_current_user)):
    if not settings.dingtalk_app_key or not settings.dingtalk_app_secret:
        raise HTTPException(status_code=400, detail="请先配置钉钉参数")

    client = DingTalkClient()
    try:
        spaces = await client.list_workspaces()
        return spaces
    finally:
        await client.close()
