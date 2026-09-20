"""backend 测试共享 fixtures 与 helper。

SSO 自签 token 的 RSA/JWKS helper 从 tests/core/test_sso_auth.py 抽出,
供 test_sso_auth.py 与 test_jwt_utils_sso.py 两个测试文件复用。
"""

import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk as jose_jwk

from app.config import settings

ISSUER = "https://sso.example.com"
AUDIENCE = "rag-dashboard"


def make_rsa_key(kid="k1"):
    """生成 RSA2048 私钥,返回 (私钥, 公钥 JWK dict)。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jose_jwk.RSAKey(key.public_key(), algorithm="RS256").to_dict()
    public_jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, public_jwk


def write_jwks(tmp_path, *public_jwks):
    """把若干公钥 JWK 写成临时 JWKS 文件,返回其 file:// URI。"""
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": list(public_jwks)}), encoding="utf-8")
    return path.as_uri()


def sign_token(payload, key, kid="k1"):
    """用 pyjwt 以 RS256 自签 token。"""
    headers = {"kid": kid} if kid else None
    return pyjwt.encode(payload, key, algorithm="RS256", headers=headers)


def valid_claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": "10086",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return claims


def enable_sso(monkeypatch, jwks_uri, issuer=ISSUER, audience=AUDIENCE):
    """monkeypatch settings 打开 SSO(settings 是单例 pydantic 对象,可改属性)。"""
    monkeypatch.setattr(settings, "sso_issuer", issuer)
    monkeypatch.setattr(settings, "sso_audience", audience)
    monkeypatch.setattr(settings, "sso_jwks_uri", jwks_uri)


@pytest.fixture
def sso_env(monkeypatch, tmp_path):
    """生成密钥+JWKS 并打开 SSO 配置,返回 (私钥, 公钥 JWK dict)。"""
    key, public_jwk = make_rsa_key()
    jwks_uri = write_jwks(tmp_path, public_jwk)
    enable_sso(monkeypatch, jwks_uri)
    return key, public_jwk


@pytest.fixture(autouse=True)
def _cleanup_sso_settings_cache():
    """每个测试前后重置 sso_auth 的 JWKS 模块级缓存，避免跨测试污染。"""
    from app.core import sso_auth

    def _reset():
        sso_auth._jwks_cache["data"] = None
        sso_auth._jwks_cache["fetched_at"] = 0.0
        sso_auth._jwks_cache["uri"] = ""

    _reset()
    yield
    _reset()


@pytest.fixture
def api_engine(tmp_path):
    """每测试一个独立 SQLite 临时库。

    api_client 与测试的种子数据共用同一 engine/文件，测试可直接 get_session
    写入行，端点读到的就是同一份数据。
    """
    from app.models.database import get_engine, init_db

    engine = get_engine(f"sqlite:///{tmp_path / 'api.db'}")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def api_client(api_engine):
    """带临时 SQLite 库的 TestClient；get_db 覆盖为指向该库的 session。

    get_db 实际是 app.api.deps 里的普通函数（被 Depends(get_db) 引用），
    因此 override key 就是 deps.get_db 本身。
    未注入登录态时请求会走真实 get_current_user 而被 401/403，
    需要登录态时配合 as_user fixture。
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.api import deps
    from app.models.database import get_session

    def _override_get_db():
        db = get_session(api_engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[deps.get_db] = _override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def as_user(api_client):
    """把 get_current_user 覆盖为指定 payload；返回 (groups, **overrides) -> payload。

    get_current_user 是 app.core.jwt_utils 里的普通函数（被 Depends 引用），
    所以直接以它为 override key。payload 形状对齐 build_user_payload。
    """
    from app.main import app
    from app.core.jwt_utils import get_current_user

    def _login(groups, **overrides):
        user = {
            "id": overrides.pop("id", "u-1"),
            "username": overrides.pop("username", "10086"),
            "display_name": "测试用户",
            "email": "",
            "is_local": False,
            "is_active": True,
            "groups": groups,
            "is_admin": False,
        }
        user.update(overrides)
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _login
    app.dependency_overrides.pop(get_current_user, None)
