"""图片签名 URL 的单元测试与签名端点测试。

签名的关键性质:绑定「对象标识(路径 / 外链目标)+ 过期时间」,
路径或目标被篡改、签名被截断、签名过期都必须校验失败。
"""
from urllib.parse import parse_qs, quote, urlparse

import pytest
from fastapi import HTTPException

from app.config import settings
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


# ---- 本地图片接口:sig/exp 校验 ----

def test_local_image_serves_with_valid_signature(api_client, tmp_path, monkeypatch):
    from app.api import upload

    monkeypatch.setattr(upload, "UPLOAD_DIR", tmp_path)
    (tmp_path / "20240101").mkdir()
    (tmp_path / "20240101" / "a.png").write_bytes(b"\x89PNG")
    res = api_client.get(sign_image_url(LOCAL_PATH))
    assert res.status_code == 200
    assert res.content == b"\x89PNG"


def test_local_image_missing_file_is_404_not_403(api_client, tmp_path, monkeypatch):
    from app.api import upload

    monkeypatch.setattr(upload, "UPLOAD_DIR", tmp_path)
    res = api_client.get(sign_image_url("/api/upload/images/20240101/missing.png"))
    assert res.status_code == 404


def test_local_image_rejects_bad_signature(api_client):
    path = "/api/upload/images/20240101/a.png"
    assert api_client.get(path).status_code == 403
    assert api_client.get(path + "?sig=0&exp=99999999999").status_code == 403
    assert api_client.get(path + "?sig=abc&exp=99999999999").status_code == 403
    assert api_client.get(sign_image_url(path, ttl_seconds=-1)).status_code == 403
    tampered = sign_image_url(path).replace("20240101", "20240102")
    assert api_client.get(tampered).status_code == 403


# ---- 外链代理:SSRF 与大小防护 ----

def _fake_getaddrinfo(ip):
    import socket

    def _fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def test_assert_public_host_accepts_public(monkeypatch):
    import socket

    from app.api.upload import _assert_public_host

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    _assert_public_host("http://example.com/pic.png")


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254"])
def test_assert_public_host_rejects_private(monkeypatch, ip):
    import socket

    from app.api.upload import _assert_public_host

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
    with pytest.raises(HTTPException) as exc:
        _assert_public_host("http://internal.example.com/pic.png")
    assert exc.value.status_code == 403


def test_assert_public_host_rejects_unresolvable(monkeypatch):
    import socket

    from app.api.upload import _assert_public_host

    def _boom(host, port, *args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(HTTPException) as exc:
        _assert_public_host("http://nope.invalid/x.png")
    assert exc.value.status_code == 400


def test_proxy_rejects_bad_signature(api_client):
    target = "http://example.com/pic.png"
    endpoint = "/api/upload/images/proxy"
    assert api_client.get(endpoint, params={"url": target}).status_code == 403

    q = _query(sign_proxy_url(target))
    assert api_client.get(endpoint, params={"url": target, "sig": "bad", "exp": q["exp"]}).status_code == 403

    expired = _query(sign_proxy_url(target, ttl_seconds=-1))
    assert api_client.get(endpoint, params={"url": target, "sig": expired["sig"], "exp": expired["exp"]}).status_code == 403

    assert api_client.get(
        endpoint,
        params={"url": "http://evil.example.com/pic.png", "sig": q["sig"], "exp": q["exp"]},
    ).status_code == 403


def test_proxy_rejects_private_host_even_with_valid_signature(api_client, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.1"))
    signed = sign_proxy_url("http://internal.example.com/pic.png")
    assert api_client.get(signed).status_code == 403


class _FakeStreamResponse:
    def __init__(self, chunks, content_type="image/png", content_length=None):
        self._chunks = chunks
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


def _patch_httpx(monkeypatch, response):
    from app.api import upload

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url):
            return _FakeStreamCtx(response)

    monkeypatch.setattr(upload.httpx, "AsyncClient", _FakeClient)


def test_proxy_serves_image_with_valid_signature(api_client, tmp_path, monkeypatch):
    from app.api import upload

    monkeypatch.setattr(upload, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(upload, "_assert_public_host", lambda url: None)
    _patch_httpx(monkeypatch, _FakeStreamResponse([b"PNG", b"DATA"]))
    res = api_client.get(sign_proxy_url("http://example.com/pic.png"))
    assert res.status_code == 200
    assert res.content == b"PNGDATA"


def test_proxy_rejects_oversized_body(api_client, tmp_path, monkeypatch):
    from app.api import upload

    monkeypatch.setattr(upload, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(upload, "_assert_public_host", lambda url: None)
    monkeypatch.setattr(settings, "image_proxy_max_bytes", 8)
    _patch_httpx(monkeypatch, _FakeStreamResponse([b"12345", b"67890"]))
    res = api_client.get(sign_proxy_url("http://example.com/big.png"))
    assert res.status_code == 413


