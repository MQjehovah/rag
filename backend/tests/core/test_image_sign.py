"""图片签名 URL 的单元测试与签名端点测试。

签名的关键性质:绑定「对象标识(路径 / 外链目标)+ 过期时间」,
路径或目标被篡改、签名被截断、签名过期都必须校验失败。
"""
from urllib.parse import parse_qs, quote, urlparse

from app.core.image_sign import (
    sign_image_url,
    sign_proxy_url,
    verify_image_signature,
    verify_proxy_signature,
)

LOCAL_PATH = "/api/upload/images/20240101/a.png"
PROXY_TARGET = "https://example.com/pic/a.png"


def _query(url: str) -> dict:
    """把签名 URL 的 query 解析为 {key: value}。"""
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# ---- 本地上传路径:签路径本身 ----

def test_local_signature_verifies():
    signed = sign_image_url(LOCAL_PATH)
    q = _query(signed)
    assert signed.startswith(LOCAL_PATH + "?")
    assert verify_image_signature(LOCAL_PATH, q["sig"], int(q["exp"]))


def test_local_signature_tampered_path_fails():
    q = _query(sign_image_url(LOCAL_PATH))
    assert not verify_image_signature("/api/upload/images/20240101/b.png", q["sig"], int(q["exp"]))


def test_local_signature_expired_fails():
    q = _query(sign_image_url(LOCAL_PATH, ttl_seconds=-1))
    assert not verify_image_signature(LOCAL_PATH, q["sig"], int(q["exp"]))


def test_local_signature_truncated_or_wrong_fails():
    q = _query(sign_image_url(LOCAL_PATH))
    exp = int(q["exp"])
    assert not verify_image_signature(LOCAL_PATH, q["sig"][:-4], exp)
    assert not verify_image_signature(LOCAL_PATH, "0" * 64, exp)
    assert not verify_image_signature(LOCAL_PATH, None, exp)
    assert not verify_image_signature(LOCAL_PATH, q["sig"], None)


def test_local_sign_preserves_existing_query():
    signed = sign_image_url(LOCAL_PATH + "?v=2")
    assert "v=2" in signed
    q = _query(signed)
    assert verify_image_signature(LOCAL_PATH, q["sig"], int(q["exp"]))


# ---- 外链代理:签被代理的目标 URL ----

def test_proxy_signature_verifies():
    signed = sign_proxy_url(PROXY_TARGET)
    q = _query(signed)
    assert signed.startswith("/api/upload/images/proxy?url=")
    assert verify_proxy_signature(PROXY_TARGET, q["sig"], int(q["exp"]))


def test_proxy_signature_tampered_target_fails():
    q = _query(sign_proxy_url(PROXY_TARGET))
    assert not verify_proxy_signature("https://evil.example.com/x.png", q["sig"], int(q["exp"]))


def test_proxy_signature_expired_fails():
    q = _query(sign_proxy_url(PROXY_TARGET, ttl_seconds=-1))
    assert not verify_proxy_signature(PROXY_TARGET, q["sig"], int(q["exp"]))


def test_proxy_signature_truncated_or_wrong_fails():
    q = _query(sign_proxy_url(PROXY_TARGET))
    exp = int(q["exp"])
    assert not verify_proxy_signature(PROXY_TARGET, q["sig"][:-4], exp)
    assert not verify_proxy_signature(PROXY_TARGET, "0" * 64, exp)
    assert not verify_proxy_signature(PROXY_TARGET, None, None)


def test_proxy_sign_round_trips_target_with_query():
    target = "https://example.com/pic.png?x=1&y=2"
    q = _query(sign_proxy_url(target))
    assert verify_proxy_signature(target, q["sig"], int(q["exp"]))


# ---- 签名端点:登录后可换取签名 ----

def test_sign_endpoint_returns_signed_urls(api_client, as_user):
    as_user([])
    raw_proxy = "/api/upload/images/proxy?url=" + quote(PROXY_TARGET, safe="")
    res = api_client.post(
        "/api/upload/images/sign",
        json={"urls": [LOCAL_PATH, raw_proxy, "https://cdn.example.com/raw.png"]},
    )
    assert res.status_code == 200
    urls = res.json()["urls"]

    lq = _query(urls[0])
    assert verify_image_signature(LOCAL_PATH, lq["sig"], int(lq["exp"]))

    pq = _query(urls[1])
    assert verify_proxy_signature(PROXY_TARGET, pq["sig"], int(pq["exp"]))

    # 非图片地址原样返回,不强行签名
    assert urls[2] == "https://cdn.example.com/raw.png"


def test_sign_endpoint_requires_auth(api_client):
    """未登录不可换取签名。

    无 Authorization 头时 FastAPI HTTPBearer 默认以 403 拒绝;
    携带无效 token 时真实 get_current_user 返回 401。
    两条路径都证明端点受保护。
    """
    payload = {"urls": [LOCAL_PATH]}
    assert api_client.post("/api/upload/images/sign", json=payload).status_code == 403
    bad = {"Authorization": "Bearer not-a-real-token"}
    assert api_client.post("/api/upload/images/sign", json=payload, headers=bad).status_code == 401
