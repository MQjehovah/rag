from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, List
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


async def _do_sync(notebook_id: str, space_id: str = None):
    global SYNC_STATUS
    SYNC_STATUS["running"] = True
    SYNC_STATUS["progress"] = "连接钉钉..."
    SYNC_STATUS["imported"] = 0
    SYNC_STATUS["errors"] = 0
    SYNC_STATUS["total"] = 0

    try:
        client = DingTalkClient()
        emb_svc = EmbeddingService()

        engine = get_engine(settings.database_url)
        db = get_session(engine)

        SYNC_STATUS["progress"] = "获取文档列表..."
        docs = await client.collect_all_docs(space_id)
        SYNC_STATUS["total"] = len(docs)
        SYNC_STATUS["progress"] = f"发现 {len(docs)} 个文档，开始导入..."

        vec_store = VectorStore(db)

        for i, doc in enumerate(docs):
            try:
                title = doc.get("title", f"文档_{i}")
                content = doc.get("content", "")

                existing = db.query(Page).filter(
                    Page.title == title,
                    Page.notebook_id == notebook_id,
                ).first()

                if existing:
                    existing.content = content
                    existing.keywords = ",".join(
                        EmbeddingService.extract_keywords(title + " " + content, 20)
                    )
                    page_id = existing.id
                    db.commit()
                else:
                    page = Page(
                        id=str(uuid.uuid4()),
                        title=title,
                        content=content,
                        notebook_id=notebook_id,
                        keywords=",".join(
                            EmbeddingService.extract_keywords(title + " " + content, 20)
                        ),
                    )
                    db.add(page)
                    db.commit()
                    db.refresh(page)
                    page_id = page.id

                if content and content.strip():
                    try:
                        chunks = await emb_svc.encode_chunks(content, title)
                        if chunks:
                            await vec_store.add_page_chunks(page_id, chunks)
                    except Exception as e:
                        logger.warning(f"Index failed for {title}: {e}")

                SYNC_STATUS["imported"] += 1
                SYNC_STATUS["progress"] = f"已导入 {SYNC_STATUS['imported']}/{SYNC_STATUS['total']}"
            except Exception as e:
                SYNC_STATUS["errors"] += 1
                logger.error(f"Import failed for doc {i}: {e}")

        db.close()
        await client.close()
        await emb_svc.close()

        from datetime import datetime
        SYNC_STATUS["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        SYNC_STATUS["progress"] = f"完成: {SYNC_STATUS['imported']} 成功, {SYNC_STATUS['errors']} 失败"
    except Exception as e:
        SYNC_STATUS["progress"] = f"同步失败: {e}"
        logger.error(f"DingTalk sync failed: {e}")
    finally:
        SYNC_STATUS["running"] = False


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
