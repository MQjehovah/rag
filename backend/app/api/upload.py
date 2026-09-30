from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
import uuid
import io
import re
import hashlib
import httpx
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from app.config import settings
from app.core.image_sign import sign_image_url, sign_proxy_url, verify_image_signature, verify_proxy_signature
from app.core.jwt_utils import get_current_user

router = APIRouter(prefix="/api/upload", tags=["文件上传"])

UPLOAD_DIR = Path("./data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ATTACHMENT_DIR = Path("./data/uploads/attachments")
ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_CACHE_DIR = Path("./data/image_cache")
IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 非图片附件大小上限(50MB)
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# 可执行类扩展名黑名单(小写比较,含脚本/安装包/动态库)
_BLOCKED_ATTACHMENT_EXTENSIONS = {
    ".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".msp", ".dll", ".sys",
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
    ".hta", ".jar", ".apk", ".app", ".sh", ".lnk", ".deb", ".rpm",
}

# 文件名中不安全/会破坏 URL 的字符:控制字符与 Windows 非法字符,另加 % # ?
_ATTACHMENT_BAD_CHARS = re.compile(r'[<>:"|?*%#\x00-\x1f\x7f]')
_MAX_ATTACHMENT_NAME_LENGTH = 120

# 只允许位图(raster)类型。刻意排除 image/svg+xml:SVG 可内嵌脚本,
# 同源返回会在本站点执行,造成存储型 XSS。
_ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif"}

_IMAGE_TYPE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/avif": "avif",
}

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


class SignRequest(BaseModel):
    urls: list[str] = Field(max_length=100)


def _sign_local_upload_path(raw: str, prefix: str) -> str | None:
    """为形状正确的本地上传路径签发签名;非法路径返回 None。

    形状要求恰好两段(date_dir/file_name)且段内无穿越字符,与图片一致。
    附件与图片共用同一签名机制,只是前缀不同。
    """
    rest = unquote(raw[len(prefix):]).split("?")[0]
    parts = [p for p in rest.split("/") if p]
    if len(parts) != 2 or any(p in ("..", ".") or "\\" in p or ".." in p for p in parts):
        return None
    return sign_image_url(raw, settings.image_sign_ttl_seconds)


@router.post("/images/sign")
def sign_images(data: SignRequest, current_user=Depends(get_current_user)):
    """把上传资源地址(本地上传路径或外链)换成带签名的可用 URL。"""
    out: list[str] = []
    for raw in data.urls:
        if raw.startswith("/api/upload/images/proxy?"):
            query = raw.partition("?")[2]
            params = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
            target = unquote(params.get("url", ""))
            out.append(sign_proxy_url(target, settings.image_sign_ttl_seconds))
        elif raw.startswith("/api/upload/images/"):
            # 只给形状正确的纯本地路径签名,绝不为任何可逃逸 uploads 目录的
            # 路径签发签名(否则签名校验反而会为路径穿越"背书")。
            out.append(_sign_local_upload_path(raw, "/api/upload/images/") or raw)
        elif raw.startswith("/api/upload/attachments/"):
            # 附件与图片同款签名机制(本地存储时使用)。
            out.append(_sign_local_upload_path(raw, "/api/upload/attachments/") or raw)
        else:
            out.append(raw)
    return {"urls": out}


@router.post("/image")
async def upload_image(file: UploadFile = File(...), current_user=Depends(get_current_user)):
    # 以 content-type 的 base type 为准做位图白名单,显式拒绝 image/svg+xml。
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    file_ext = _IMAGE_TYPE_EXTENSIONS.get(content_type)
    if file_ext is None:
        raise HTTPException(status_code=400, detail="仅支持 PNG/JPEG/GIF/WebP/BMP/AVIF 图片")

    # 扩展名从已校验的 content-type 推导,避免伪造 .svg 文件名绕过白名单。
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
                content_type=content_type
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

    url = f"{settings.public_base_path}/api/upload/images/{date_dir}/{file_name}"
    return {"url": url, "name": f"{date_dir}/{file_name}"}

@router.get("/images/{date_dir}/{file_name}")
def get_image(date_dir: str, file_name: str, sig: str | None = None, exp: int | None = None):
    signed_path = f"/api/upload/images/{date_dir}/{file_name}"
    if not verify_image_signature(signed_path, sig, exp):
        raise HTTPException(status_code=403, detail="图片签名无效或已过期")
    # 签名只证明路径字符串未被篡改,不证明它不会逃逸上传目录:路由按 "/" 分段,
    # 而 Windows 上反斜杠同为路径分隔符,必须再做真实解析后的包含性校验。
    base = UPLOAD_DIR.resolve()
    file_path = (UPLOAD_DIR / date_dir / file_name).resolve()
    if not file_path.is_relative_to(base) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(file_path, headers={"X-Content-Type-Options": "nosniff"})


def _sanitize_attachment_name(raw: str | None) -> str:
    """净化原始文件名:去路径、去控制字符/非法字符、限长,只保留纯文件名。"""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _ATTACHMENT_BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not name or name in (".", ".."):
        name = "attachment"
    if len(name) > _MAX_ATTACHMENT_NAME_LENGTH:
        dot = name.rfind(".")
        if 0 < dot and len(name) - dot <= 16:
            ext = name[dot:]
            name = name[: _MAX_ATTACHMENT_NAME_LENGTH - len(ext)] + ext
        else:
            name = name[:_MAX_ATTACHMENT_NAME_LENGTH]
    return name


async def _read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """分块读入并强制大小上限;超过即 413,避免把超大文件整体读进内存后才判断。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="附件超过 50MB 大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/attachment")
async def upload_attachment(file: UploadFile = File(...), current_user=Depends(get_current_user)):
    """上传非图片附件(≤50MB)。存储与访问复用图片同一套机制(MinIO 优先,本地签名兜底)。"""
    original_name = _sanitize_attachment_name(file.filename)
    ext = Path(original_name).suffix.lower()
    if ext in _BLOCKED_ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持上传可执行文件")

    content_type = (file.content_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    file_content = await _read_limited(file, MAX_ATTACHMENT_BYTES)

    date_dir = datetime.now().strftime("%Y%m%d")
    stored_name = f"{uuid.uuid4().hex[:12]}-{original_name}"

    if MINIO_AVAILABLE:
        try:
            client = get_minio_client()
            object_name = f"attachments/{date_dir}/{stored_name}"

            client.put_object(
                settings.minio_bucket,
                object_name,
                io.BytesIO(file_content),
                length=len(file_content),
                content_type=content_type
            )

            scheme = "https" if settings.minio_secure else "http"
            url = f"{scheme}://{settings.minio_endpoint}/{settings.minio_bucket}/{object_name}"
            return {"url": url, "name": original_name, "size": len(file_content), "mime": content_type}
        except Exception:
            pass

    date_path = ATTACHMENT_DIR / date_dir
    date_path.mkdir(parents=True, exist_ok=True)

    file_path = date_path / stored_name
    with open(file_path, "wb") as f:
        f.write(file_content)

    url = f"{settings.public_base_path}/api/upload/attachments/{date_dir}/{stored_name}"
    return {"url": url, "name": original_name, "size": len(file_content), "mime": content_type}


@router.get("/attachments/{date_dir}/{file_name}")
def get_attachment(date_dir: str, file_name: str, sig: str | None = None, exp: int | None = None):
    """下载附件。与图片一致:HMAC 签名 + 真实解析后的目录包含性校验。"""
    signed_path = f"/api/upload/attachments/{date_dir}/{file_name}"
    if not verify_image_signature(signed_path, sig, exp):
        raise HTTPException(status_code=403, detail="附件签名无效或已过期")
    base = ATTACHMENT_DIR.resolve()
    file_path = (ATTACHMENT_DIR / date_dir / file_name).resolve()
    if not file_path.is_relative_to(base) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="附件不存在")
    return FileResponse(file_path, headers={"X-Content-Type-Options": "nosniff"})


def _assert_public_host(url: str) -> None:
    """拒绝解析到内网/回环/链路本地/多播/CGNAT 等地址的目标,防 SSRF。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if not host:
        raise HTTPException(status_code=400, detail="无效的图片地址")
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析图片主机")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # 正向白名单:只放行全球可路由地址,顺带拒绝多播与 CGNAT(100.64.0.0/10)。
        if not ip.is_global or ip.is_multicast:
            raise HTTPException(status_code=403, detail="不允许访问内网地址")


@router.get("/images/proxy")
async def proxy_image(url: str, sig: str | None = None, exp: int | None = None):
    """Fetch an external image server-side and stream it back.

    Some image hosts (e.g. Alibaba OSS buckets) reject browser requests with a
    Referer header, which every cross-origin <img> sends.  Fetching without a
    Referer from the backend bypasses that, with a disk cache so we only fetch
    each URL once.
    """
    if not verify_proxy_signature(url, sig, exp):
        raise HTTPException(status_code=403, detail="图片签名无效或已过期")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="只支持 http/https 图片")
    _assert_public_host(url)
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cached = list(IMAGE_CACHE_DIR.glob(cache_key + ".*"))
    if cached:
        return FileResponse(cached[0], headers={"X-Content-Type-Options": "nosniff"})
    max_bytes = settings.image_proxy_max_bytes
    try:
        # 刻意不跟随重定向:SSRF 校验只针对请求时的 URL,若允许 3xx 跳转到
        # 内网地址,校验就被绕过。重定向一律视为失败,保持校验权威。
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            async with client.stream("GET", url) as resp:
                if 300 <= resp.status_code < 400:
                    raise HTTPException(status_code=502, detail="图片获取失败: 不支持重定向")
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "image/png")
                # 只回源位图类型,拒绝 text/html、image/svg+xml 等同源可执行内容。
                base_type = content_type.split(";")[0].strip().lower()
                if base_type not in _ALLOWED_IMAGE_TYPES:
                    raise HTTPException(status_code=400, detail="不支持的图片类型")
                # Content-Length 可能缺失或被伪造,仅作快速拒绝;真正的上限由
                # 下面的字节计数器把关。流式读入保证内存中最多驻留 max_bytes。
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise HTTPException(status_code=413, detail="图片超过大小限制")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(status_code=413, detail="图片超过大小限制")
                    chunks.append(chunk)
                content = b"".join(chunks)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"图片获取失败: {str(e)[:120]}")
    ext = _IMAGE_TYPE_EXTENSIONS[base_type]
    cache_path = IMAGE_CACHE_DIR / f"{cache_key}.{ext}"
    try:
        cache_path.write_bytes(content)
    except Exception:
        pass
    return Response(
        content=content,
        media_type=base_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )
