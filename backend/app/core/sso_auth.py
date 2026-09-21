"""SSO/OIDC token 校验:从 issuer 的 JWKS 拉取公钥验 RS256 签名(带缓存)。

与 app.core.jwt_utils(自家 HS256 token)相互独立。本模块专验 SSO 签发的
RS256 token,通过则返回 claims,其中 sub 视为工号,供 D-ready 的
get_current_user 接入使用。未配置 sso_issuer 时按 SSO 禁用处理。
"""

import base64
import json
import secrets
import threading
import time
import urllib.parse
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


def verify_sso_token(token: str, audience: str | None = None) -> dict:
    """校验 SSO 签发的 RS256 token,通过则返回 claims(含 sub 工号)。

    未配置 sso_issuer 视为 SSO 禁用;iss/aud 不匹配、签名无效、过期、
    缺少 sub(工号)等一律抛 SsoAuthError。

    audience 显式传入时优先于 sso_audience 设置:授权码回调拿到的 id_token
    的 aud 是本系统自己的 client_id,而资源服务器场景(sso_audience)认的是
    调用方(如 dashboard)的 client_id,两者需要区分。
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
            audience=audience or settings.sso_audience or None,
        )
    except SsoAuthError:
        raise
    except jose_exceptions.JWTError as e:
        raise SsoAuthError(f"SSO token 无效: {e}") from e
    if not claims.get("sub"):
        raise SsoAuthError("SSO token 缺少 sub(工号)")
    return claims


# ---- 授权码流程(浏览器 SSO 登录)----

STATE_TTL_SECONDS = 600

_states: dict[str, float] = {}
_states_lock = threading.Lock()


def sso_login_enabled() -> bool:
    """浏览器 SSO 登录是否可用:issuer + client_id + redirect_uri 齐备。"""
    return bool(settings.sso_issuer and settings.sso_client_id and settings.sso_redirect_uri)


def build_authorize_url(state: str) -> str:
    """构造 SSO OIDC 授权跳转 URL(浏览器访问, 故用配置的 issuer 公网地址)。"""
    issuer = (settings.sso_issuer or "").rstrip("/")
    if not sso_login_enabled() or not issuer:
        raise SsoAuthError("SSO login not configured")
    params = {
        "response_type": "code",
        "client_id": settings.sso_client_id,
        "redirect_uri": settings.sso_redirect_uri,
        "state": state,
        "scope": "openid profile",
    }
    return issuer + "/authorize?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> str:
    """authorization_code → token 交换, 返回 id_token 字符串。

    同时携带 client_secret_basic(Authorization 头)与 client_secret_post(client_secret
    字段), 兼容两种支持方式。缺 id_token 抛 SsoAuthError。
    """
    issuer = (settings.sso_issuer or "").rstrip("/")
    if not sso_login_enabled() or not issuer:
        raise SsoAuthError("SSO login not configured")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.sso_redirect_uri,
        "client_id": settings.sso_client_id,
    }
    secret = settings.sso_client_secret
    if secret:
        form["client_secret"] = secret
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(issuer + "/token", data=data, method="POST")
    if secret:
        basic = base64.b64encode(f"{settings.sso_client_id}:{secret}".encode()).decode("ascii")
        req.add_header("Authorization", "Basic " + basic)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise SsoAuthError(f"SSO token 交换失败: {e}") from e
    id_token = payload.get("id_token")
    if not id_token:
        raise SsoAuthError("SSO token 响应缺少 id_token")
    return id_token


def new_state() -> str:
    """生成一次性 state(随机短串)并记录时间戳。"""
    st = secrets.token_urlsafe(16)
    with _states_lock:
        _states[st] = time.time()
    return st


def validate_state(state: str) -> bool:
    """校验并消费 state: 过期/不存在返回 False。"""
    with _states_lock:
        found = _states.pop(state, None)
    if found is None:
        return False
    return (time.time() - found) <= STATE_TTL_SECONDS
