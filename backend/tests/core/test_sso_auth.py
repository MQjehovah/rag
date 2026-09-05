"""SSO/OIDC token 校验测试:用临时 RSA 自签 token + file:// JWKS 验证 verify_sso_token。

helper(make_rsa_key/write_jwks/sign_token/valid_claims/enable_sso/sso_env)已抽到
tests/conftest.py 供 test_jwt_utils_sso.py 复用;本文件保持可独立运行。

依赖:pyjwt(自签 RS256 token 用,已在解释器中安装)、python-jose[cryptography](验签)。
"""

import time

import pytest

from app.config import settings
from app.core.sso_auth import SsoAuthError, verify_sso_token
from tests.conftest import (
    ISSUER,
    enable_sso,
    make_rsa_key,
    sign_token,
    valid_claims,
    write_jwks,
)


def test_verify_sso_token_accepts_valid_token(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(), key)
    claims = verify_sso_token(token)
    assert claims["sub"] == "10086"
    assert claims["iss"] == ISSUER


def test_verify_sso_token_rejects_bad_signature(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token + "tampered")


def test_verify_sso_token_rejects_wrong_audience(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(aud="some-other-app"), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)


def test_verify_sso_token_rejects_expired_token(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(exp=int(time.time()) - 60), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)


def test_verify_sso_token_rejects_wrong_issuer(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(iss="https://evil.example.com"), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)


def test_verify_sso_token_disabled_when_issuer_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "sso_issuer", "")
    monkeypatch.setattr(settings, "sso_jwks_uri", "")
    monkeypatch.setattr(settings, "sso_audience", "")
    token = "not.even.a.jwt"
    with pytest.raises(SsoAuthError, match="[Nn]ot configured"):
        verify_sso_token(token)


def test_verify_sso_token_falls_back_to_first_key_without_kid(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(), key, kid=None)
    assert verify_sso_token(token)["sub"] == "10086"


def test_verify_sso_token_selects_key_by_kid(monkeypatch, tmp_path):
    key1, jwk1 = make_rsa_key(kid="k1")
    key2, jwk2 = make_rsa_key(kid="k2")
    enable_sso(monkeypatch, write_jwks(tmp_path, jwk1, jwk2))
    token = sign_token(valid_claims(), key2, kid="k2")
    assert verify_sso_token(token)["sub"] == "10086"


def test_verify_sso_token_rejects_unknown_kid(monkeypatch, tmp_path):
    key1, jwk1 = make_rsa_key(kid="k1")
    _, jwk2 = make_rsa_key(kid="k2")
    enable_sso(monkeypatch, write_jwks(tmp_path, jwk1, jwk2))
    token = sign_token(valid_claims(), key1, kid="ghost")
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)
