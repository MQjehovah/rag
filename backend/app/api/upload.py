from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse, Response
import uuid
import io
import os
import hashlib
import httpx
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.core.jwt_utils import get_current_user

router = APIRouter(prefix="/api/upload", tags=["文件上传"])

UPLOAD_DIR = Path("./data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_CACHE_DIR = Path("./data/image_cache")
IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

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
async def upload_image(file: UploadFile = File(...), current_user=Depends(get_current_user)):
    if not file.content_type or not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="仅支持图片文件")

    filename = file.filename or "image.jpg"
    file_ext = filename.split('.')[-1] if '.' in filename else 'jpg'
    file_name = f"{uuid.uuid4()}.{file_ext}"
    date_dir = datetime.now().strftime('%Y%m%d')

    file_content = await file.read()

    if MINIO_AVAILABLE:
        try:
            client = get_minio_client()
            object_name = f"{date_dir}/{file_name}"

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
        except Exception:
            pass

    date_path = UPLOAD_DIR / date_dir
    date_path.mkdir(parents=True, exist_ok=True)

    file_path = date_path / file_name
    with open(file_path, 'wb') as f:
        f.write(file_content)

    url = f"/api/upload/images/{date_dir}/{file_name}"
    return {"url": url, "name": f"{date_dir}/{file_name}"}

@router.get("/images/{date_dir}/{file_name}")
def get_image(date_dir: str, file_name: str):
    file_path = UPLOAD_DIR / date_dir / file_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(file_path)


@router.get("/images/proxy")
async def proxy_image(url: str):
    """Fetch an external image server-side and stream it back.

    Some image hosts (e.g. Alibaba OSS buckets) reject browser requests with a
    Referer header, which every cross-origin <img> sends.  Fetching without a
    Referer from the backend bypasses that, with a disk cache so we only fetch
    each URL once.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="只支持 http/https 图片")
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cached = list(IMAGE_CACHE_DIR.glob(cache_key + ".*"))
    if cached:
        return FileResponse(cached[0])
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.content
            content_type = resp.headers.get("content-type", "image/png")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"图片获取失败: {str(e)[:120]}")
    ext = content_type.split("/")[-1].split(";")[0].strip() or "bin"
    if not ext or len(ext) > 8:
        ext = "bin"
    cache_path = IMAGE_CACHE_DIR / f"{cache_key}.{ext}"
    try:
        cache_path.write_bytes(content)
    except Exception:
        pass
    return Response(content=content, media_type=content_type)
