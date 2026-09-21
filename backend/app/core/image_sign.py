"""图片 URL 的 HMAC 签名:解决 <img> 无法携带 Authorization 头的问题。

签名绑定「被请求对象的标识 + 过期时间」,由已登录用户调用
POST /api/upload/images/sign 换取;图片接口校验签名后放行。
"""
import hashlib
import hmac
import time
from urllib.parse import quote

from app.config import settings

DEFAULT_TTL_SECONDS = 3600


def _secret() -> bytes:
    key = (getattr(settings, "image_sign_secret", "") or "").strip() or settings.jwt_secret_key
    return key.encode("utf-8")


def _digest(message: str, exp: int) -> str:
    return hmac.new(_secret(), f"{message}:{exp}".encode("utf-8"), hashlib.sha256).hexdigest()


# ---- 本地上传图片:签名对象是不含 query 的路径 ----

def sign_image_url(path: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """返回带 sig/exp 的路径(只签路径本身,忽略并保留已有 query)。"""
    base, _, query = path.partition("?")
    exp = int(time.time()) + ttl_seconds
    sig = _digest(base, exp)
    sep = "&" if query else ""
    return f"{base}?{query}{sep}sig={sig}&exp={exp}"


def verify_image_signature(path: str, sig: str | None, exp: int | None) -> bool:
    if not sig or not exp:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_digest(path, exp), sig)


# ---- 外链代理:签名对象是被代理的目标 URL ----

def sign_proxy_url(target: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """返回带签名的完整代理路径 /api/upload/images/proxy?url=...&sig=...&exp=..."""
    exp = int(time.time()) + ttl_seconds
    sig = _digest(target, exp)
    return f"/api/upload/images/proxy?url={quote(target, safe='')}&sig={sig}&exp={exp}"


def verify_proxy_signature(target: str, sig: str | None, exp: int | None) -> bool:
    if not sig or not exp:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_digest(target, exp), sig)
