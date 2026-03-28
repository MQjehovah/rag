from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
from datetime import datetime

from app.models.database import Page, Notebook, get_session, get_engine, init_db
from app.models.schema import PageCreate, PageUpdate, PageResponse
from app.core.rag import EmbeddingService, VectorStore
from app.config import settings

router = APIRouter(prefix="/api/pages", tags=["笔记"])

_engine = None
_session = None
_embedding_service = None
_vector_store = None

def get_db():
    global _engine, _session
    if _engine is None:
        db_url = getattr(settings, 'database_url', 'sqlite:///./data/notes.db')
        _engine = get_engine(db_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session

def get_rag_services():
    global _embedding_service, _vector_store
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    if _vector_store is None:
        _vector_store = VectorStore()
    return _embedding_service, _vector_store

@router.post("", response_model=PageResponse)
async def create_page(data: PageCreate, db: Session = Depends(get_db), rag = Depends(get_rag_services)):
    """创建笔记"""
    page = Page(id=str(uuid.uuid4()), title=data.title, content=data.content, notebook_id=data.notebook_id)
    db.add(page)
    db.commit()
    db.refresh(page)
    
    if data.content:
        try:
            embedding_service, vector_store = rag
            text = f"{data.title}\n{data.content}"
            chunks = await embedding_service.encode_chunks(text)
            if chunks:
                await vector_store.add_page_chunks(page.id, page.title, chunks)
        except:
            pass
    
    return page

@router.get("", response_model=List[PageResponse])
async def list_pages(notebook_id: Optional[str] = None, db: Session = Depends(get_db)):
    """笔记列表"""
    query = db.query(Page)
    if notebook_id:
        query = query.filter(Page.notebook_id == notebook_id)
    pages = query.order_by(Page.updated_at.desc()).all()
    return pages

@router.get("/{page_id}", response_model=PageResponse)
async def get_page(page_id: str, db: Session = Depends(get_db)):
    """获取笔记"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return page

@router.put("/{page_id}", response_model=PageResponse)
async def update_page(page_id: str, data: PageUpdate, db: Session = Depends(get_db), rag = Depends(get_rag_services)):
    """更新笔记"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    
    if data.title is not None:
        page.title = data.title
    if data.content is not None:
        page.content = data.content
    if data.notebook_id is not None:
        page.notebook_id = data.notebook_id
    page.updated_at = datetime.now()
    
    db.commit()
    db.refresh(page)
    
    try:
        embedding_service, vector_store = rag
        text = f"{page.title}\n{page.content}"
        chunks = await embedding_service.encode_chunks(text)
        if chunks:
            await vector_store.add_page_chunks(page.id, page.title, chunks)
    except:
        pass
    
    return page

@router.delete("/{page_id}")
async def delete_page(page_id: str, db: Session = Depends(get_db), rag = Depends(get_rag_services)):
    """删除笔记"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    
    db.delete(page)
    db.commit()
    
    try:
        _, vector_store = rag
        await vector_store.delete_page(page_id)
    except:
        pass
    
    return {"message": "删除成功"}

@router.post("/{page_id}/index")
async def index_page(page_id: str, db: Session = Depends(get_db), rag = Depends(get_rag_services)):
    """手动触发RAG索引"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="笔记不存在")
    
    try:
        embedding_service, vector_store = rag
        text = f"{page.title}\n{page.content}"
        chunks = await embedding_service.encode_chunks(text)
        
        if chunks:
            await vector_store.add_page_chunks(page.id, page.title, chunks)
            return {"message": f"索引成功，共 {len(chunks)} 个分块"}
        else:
            return {"message": "内容为空，未创建索引"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"索引失败: {str(e)}")