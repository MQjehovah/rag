"""嵌入模型档案 API：可配置多个 embedding 模型，笔记本可指定其一；并提供全库重建。"""
import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import settings
from app.core.jwt_utils import get_current_user
from app.core.security import has_permission, require_permission
from app.models.database import (
    EmbeddingProfile,
    Notebook,
    Page,
    WikiPage,
    get_engine,
    get_session,
)
from app.models.schema import (
    EmbeddingProfileCreate,
    EmbeddingProfileResponse,
    EmbeddingProfileUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/embeddings", tags=["嵌入模型"])

_reindex_status: Dict[str, Any] = {"running": False, "processed": 0, "total": 0, "errors": 0, "message": ""}
_reindex_task: asyncio.Task | None = None


def _is_admin(current_user) -> bool:
    return has_permission(current_user, "embedding.manage")


def _require_admin(current_user) -> None:
    require_permission(current_user, "embedding.manage")


@router.get("/profiles", response_model=List[EmbeddingProfileResponse])
def list_profiles(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    rows = db.query(EmbeddingProfile).order_by(EmbeddingProfile.created_at.asc()).all()
    is_admin = _is_admin(current_user)
    responses = []
    for row in rows:
        resp = EmbeddingProfileResponse.model_validate(row, from_attributes=True)
        # api_key 属敏感信息,非管理员不回传(前端非管理员也只能只读查看)
        if not is_admin:
            resp.api_key = ""
        responses.append(resp)
    return responses


@router.post("/profiles", response_model=EmbeddingProfileResponse)
def create_profile(data: EmbeddingProfileCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    _require_admin(current_user)
    profile = EmbeddingProfile(
        id=str(uuid.uuid4()),
        name=data.name,
        kind=data.kind or "openai",
        api_url=data.api_url,
        api_key=data.api_key or "",
        model=data.model,
        dimensions=int(data.dimensions or 1024),
        is_default=False,
    )
    db.add(profile)
    db.flush()
    if data.is_default or db.query(EmbeddingProfile).count() == 1:
        _set_default(db, profile)
    db.commit()
    db.refresh(profile)
    return profile


def _set_default(db: Session, profile: EmbeddingProfile) -> None:
    db.query(EmbeddingProfile).update({EmbeddingProfile.is_default: False})
    profile.is_default = True


@router.put("/profiles/{profile_id}", response_model=EmbeddingProfileResponse)
def update_profile(profile_id: str, data: EmbeddingProfileUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    _require_admin(current_user)
    profile = db.query(EmbeddingProfile).filter(EmbeddingProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    for field in ("name", "kind", "api_url", "model"):
        value = getattr(data, field)
        if value is not None:
            setattr(profile, field, value)
    if data.api_key is not None:
        profile.api_key = data.api_key
    if data.dimensions is not None:
        profile.dimensions = int(data.dimensions)
    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    _require_admin(current_user)
    profile = db.query(EmbeddingProfile).filter(EmbeddingProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    in_use = db.query(Notebook).filter(Notebook.embedding_profile_id == profile_id).count()
    if in_use:
        raise HTTPException(status_code=409, detail=f"仍有 {in_use} 个笔记本在使用该档案，请先切换")
    was_default = bool(profile.is_default)
    db.delete(profile)
    db.commit()
    if was_default:
        nxt = db.query(EmbeddingProfile).order_by(EmbeddingProfile.created_at.asc()).first()
        if nxt:
            _set_default(db, nxt)
            db.commit()
    return {"message": "删除成功"}


@router.post("/profiles/{profile_id}/default", response_model=EmbeddingProfileResponse)
def make_default(profile_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    _require_admin(current_user)
    profile = db.query(EmbeddingProfile).filter(EmbeddingProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    _set_default(db, profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/profiles/{profile_id}/test")
async def test_profile(profile_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    _require_admin(current_user)
    from app.core.rag import EmbeddingService, resolve_embedding_spec
    profile = db.query(EmbeddingProfile).filter(EmbeddingProfile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在")
    spec = resolve_embedding_spec(db, profile_id)
    svc = EmbeddingService(spec)
    try:
        vec = await svc.encode("嵌入连通性测试 embedding test")
    finally:
        await svc.close()
    if not vec:
        return {"ok": False, "error": "未返回向量（检查 api_url / model / api_key）"}
    return {"ok": True, "dimensions": len(vec), "configured_dimensions": profile.dimensions,
            "match": len(vec) == int(profile.dimensions or 0)}


@router.post("/reindex")
async def reindex(current_user=Depends(get_current_user)):
    """按各笔记本的嵌入档案，全库重建笔记向量（后台）。"""
    _require_admin(current_user)
    global _reindex_task
    if _reindex_status.get("running"):
        return {"started": False, "running": True, "message": "重建已在运行"}
    _reindex_status.update(running=True, processed=0, total=0, errors=0, message="启动重建...")
    engine = get_engine(settings.database_url)
    _reindex_task = asyncio.create_task(_reindex_all(engine))
    return {"started": True, "running": True}


@router.get("/reindex-status")
def reindex_status(current_user=Depends(get_current_user)):
    return _reindex_status


async def _reindex_all(engine) -> None:
    from app.core.hybrid import HybridIndex
    from app.core.rag import EmbeddingService, VectorStore, embedding_spec_for_notebook

    db = get_session(engine)
    try:
        page_rows = db.query(Page.id, Page.notebook_id, Page.title, Page.content).filter(
            Page.content.isnot(None), Page.content != ""
        ).all()
        _reindex_status.update(total=len(page_rows), processed=0, errors=0, message="重建中...")
    finally:
        db.close()

    svc_cache: Dict[Any, Any] = {}
    for idx, (page_id, notebook_id, title, content) in enumerate(page_rows, start=1):
        try:
            db = get_session(engine)
            try:
                spec = embedding_spec_for_notebook(db, notebook_id)
            finally:
                db.close()
            key = spec.get("id")
            svc = svc_cache.get(key)
            if svc is None:
                svc = EmbeddingService(spec)
                svc_cache[key] = svc
            chunks = await svc.encode_chunks(content or "", title or "", enrich_context=False)
            db = get_session(engine)
            try:
                store = VectorStore(db)
                store.delete_page_chunks(page_id)
                if chunks:
                    await store.add_page_chunks(page_id, chunks, profile_id=key)
                keywords = EmbeddingService.extract_keywords((title or "") + " " + (content or ""), 20)
                page = db.query(Page).filter(Page.id == page_id).first()
                if page:
                    page.keywords = ",".join(keywords)
                HybridIndex(db).index_page(page_id, title or "", content or "", ",".join(keywords))
                db.commit()
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            _reindex_status["errors"] += 1
            logger.error("reindex failed %s: %s", page_id, e)
        _reindex_status["processed"] = idx

    note_msg = (
        f"笔记重建 {_reindex_status.get('processed', 0)}/{_reindex_status.get('total', 0)}"
        f"（失败 {_reindex_status.get('errors', 0)}）"
    )
    _reindex_status.update(message=f"{note_msg}；重建知识库(wiki)向量...")
    wiki_msg = ""
    try:
        db = get_session(engine)
        try:
            wiki_ids = [r[0] for r in db.query(WikiPage.id).all()]
        finally:
            db.close()
        if wiki_ids:
            from app.core.wiki_embedding import embed_wiki_pages
            stats = await embed_wiki_pages(engine, wiki_ids)
            wiki_msg = f"wiki {stats['embedded']}/{stats['total']}（失败 {stats['errors']}）"
    except Exception as e:  # noqa: BLE001
        logger.error("wiki reindex failed: %s", e)
        wiki_msg = "wiki 重建失败"

    for svc in svc_cache.values():
        await svc.close()
    _reindex_status.update(running=False, message=f"{note_msg}；{wiki_msg}" if wiki_msg else note_msg)
