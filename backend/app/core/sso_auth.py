"""SSO/OIDC token 校验:从 issuer 的 JWKS 拉取公钥验 RS256 签名(带缓存)。

与 app.core.jwt_utils(自家 HS256 token)相互独立。本模块专验 SSO 签发的
RS256 token,通过则返回 claims,其中 sub 视为工号,供 D-ready 的
get_current_user 接入使用。未配置 sso_issuer 时按 SSO 禁用处理。
"""

import json
import threading
import time
import urllib.request
from urllib.parse import urlparse
from urllib.request import url2pathname

from jose import exceptions as jose_exceptions
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt

from app.config import settings

ALGORITHM = "RS256"

JWKS_CACHE_TTL_SECONDS = 300


class SsoAuthError(Exception):
    """SSO token 校验失败(未配置、JWKS 拉取失败或签名/声明无效)。"""


# 模块级 JWKS 缓存:同一 sso_jwks_uri 在 TTL 内只拉一次,简单锁防并发重复拉取
_jwks_cache = {"uri": "", "data": None, "fetched_at": 0.0}
_jwks_lock = threading.Lock()


def _read_jwks(uri: str) -> dict:
    """从 uri 读取 JWKS 文档,支持 http(s) 与 file:// 两种 scheme。"""
    scheme = urlparse(uri).scheme
    try:
        if scheme in ("http", "https"):
            with urllib.request.urlopen(uri, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        if scheme == "file":
            path = url2pathname(urlparse(uri).path)
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError) as e:
        raise SsoAuthError(f"拉取 JWKS 失败({uri}): {e}") from e
    raise SsoAuthError(f"不支持的 JWKS URI scheme: {scheme!r}")


def _get_jwks() -> dict:
    """带缓存的 JWKS 获取:TTL 内命中缓存,过期或换 URI 时加锁重拉。"""
    uri = settings.sso_jwks_uri
    if not uri:
        raise SsoAuthError("SSO not configured")
    cache = _jwks_cache
    now = time.time()
    if (
        cache["uri"] == uri
        and cache["data"] is not None
        and now - cache["fetched_at"] < JWKS_CACHE_TTL_SECONDS
    ):
        return cache["data"]
    with _jwks_lock:
        if (
            cache["uri"] == uri
            and cache["data"] is not None
            and time.time() - cache["fetched_at"] < JWKS_CACHE_TTL_SECONDS
        ):
            return cache["data"]
        data = _read_jwks(uri)
        cache["uri"] = uri
        cache["data"] = data
        cache["fetched_at"] = time.time()
        return data


def _find_key(kid: str | None) -> dict:
    """在 JWKS keys 中定位公钥:优先按 kid 匹配,无 kid 时取第一把兜底。"""
    keys = _get_jwks().get("keys") or []
    if not keys:
        raise SsoAuthError("JWKS 文档缺少 keys")
    if kid:
        for item in keys:
            if item.get("kid") == kid:
                return item
        raise SsoAuthError(f"JWKS 中找不到 kid={kid!r}")
    return keys[0]


def _public_key(kid: str | None):
    """把 JWK dict 构造成可验签的公钥对象(cryptography RSAKey)。"""
    jwk_dict = _find_key(kid)
    return jose_jwk.construct(jwk_dict, algorithm=ALGORITHM)


def verify_sso_token(token: str) -> dict:
    """校验 SSO 签发的 RS256 token,通过则返回 claims(含 sub 工号)。

    未配置 sso_issuer 视为 SSO 禁用;iss/aud 不匹配、签名无效、过期、
    缺少 sub(工号)等一律抛 SsoAuthError。
    """
    if not settings.sso_issuer:
        raise SsoAuthError("SSO not configured")
    try:
        header = jose_jwt.get_unverified_header(token)
    except jose_exceptions.JWTError as e:
        raise SsoAuthError(f"SSO token header 无效: {e}") from e
    kid = header.get("kid")
    try:
        key = _public_key(kid)
        claims = jose_jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            issuer=settings.sso_issuer,
            audience=settings.sso_audience or None,
        )
    except SsoAuthError:
        raise
    except jose_exceptions.JWTError as e:
        raise SsoAuthError(f"SSO token 无效: {e}") from e
    if not claims.get("sub"):
        raise SsoAuthError("SSO token 缺少 sub(工号)")
    return claims
