from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from typing import Optional
import uuid
import io
import os
from datetime import datetime
from pathlib import Path

from app.config import settings

router = APIRouter(prefix="/api/upload", tags=["文件上传"])

UPLOAD_DIR = Path("./data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

try:
    from minio import Minio
    minio_client = None
    
    def get_minio_client():
        global minio_client
        if minio_client is None:
            minio_client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure
            )
            try:
                if not minio_client.bucket_exists(settings.minio_bucket):
                    minio_client.make_bucket(settings.minio_bucket)
            except Exception:
                pass
        return minio_client
    
    MINIO_AVAILABLE = True
except ImportError:
    MINIO_AVAILABLE = False
    minio_client = None

@router.post("/image")
async def upload_image(file: UploadFile = File(...)):
    """上传图片"""
    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    
    filename = file.filename or "image.jpg"
    file_ext = filename.split('.')[-1] if '.' in filename else 'jpg'
    file_name = f"{uuid.uuid4()}.{file_ext}"
    date_dir = datetime.now().strftime('%Y%m%d')
    
    if MINIO_AVAILABLE:
        try:
            client = get_minio_client()
            object_name = f"{date_dir}/{file_name}"
            file_content = await file.read()
            
            client.put_object(
                settings.minio_bucket,
                object_name,
                io.BytesIO(file_content),
                length=len(file_content),
                content_type=file.content_type
            )
            
            if settings.minio_secure:
                url = f"https://{settings.minio_endpoint}/{settings.minio_bucket}/{object_name}"
            else:
                url = f"http://{settings.minio_endpoint}/{settings.minio_bucket}/{object_name}"
            
            return {"url": url, "name": object_name}
        except Exception as e:
            pass
    
    date_path = UPLOAD_DIR / date_dir
    date_path.mkdir(parents=True, exist_ok=True)
    
    file_path = date_path / file_name
    content = await file.read()
    with open(file_path, 'wb') as f:
        f.write(content)
    
    url = f"/api/upload/images/{date_dir}/{file_name}"
    return {"url": url, "name": f"{date_dir}/{file_name}"}

@router.get("/images/{date_dir}/{file_name}")
async def get_image(date_dir: str, file_name: str):
    """获取本地图片"""
    file_path = UPLOAD_DIR / date_dir / file_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(file_path)