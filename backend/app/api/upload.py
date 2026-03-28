from fastapi import APIRouter, UploadFile, File, HTTPException
from typing import Optional
import uuid
import io
from datetime import datetime

from app.config import settings

router = APIRouter(prefix="/api/upload", tags=["文件上传"])

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
    """上传图片到MinIO"""
    if not MINIO_AVAILABLE:
        raise HTTPException(status_code=500, detail="MinIO未安装")
    
    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    
    try:
        client = get_minio_client()
        
        file_ext = file.filename.split('.')[-1] if '.' in (file.filename or '') else 'jpg'
        object_name = f"{datetime.now().strftime('%Y%m%d')}/{uuid.uuid4()}.{file_ext}"
        
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
        
        return {
            "url": url,
            "name": object_name
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")

@router.get("/presign-url")
async def get_presign_url(filename: str):
    """获取预签名上传URL"""
    if not MINIO_AVAILABLE:
        raise HTTPException(status_code=500, detail="MinIO未安装")
    
    try:
        client = get_minio_client()
        
        object_name = f"{datetime.now().strftime('%Y%m%d')}/{uuid.uuid4()}-{filename}"
        
        url = client.presigned_put_object(
            settings.minio_bucket,
            object_name
        )
        
        return {
            "upload_url": url,
            "object_name": object_name
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取URL失败: {str(e)}")