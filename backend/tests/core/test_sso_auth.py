"""SSO/OIDC token 校验测试:用临时 RSA 自签 token + file:// JWKS 验证 verify_sso_token。

helper(make_rsa_key/write_jwks/sign_token/valid_claims/enable_sso/sso_env)已抽到
tests/conftest.py 供 test_jwt_utils_sso.py 复用;本文件保持可独立运行。

依赖:pyjwt(自签 RS256 token 用,已在解释器中安装)、python-jose[cryptography](验签)。
"""

import time

import pytest

from app.config import settings
from app.core import sso_auth
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


# ---- 浏览器 SSO 登录:授权 URL / state / 回调受众 ----


def _enable_login(monkeypatch, issuer=ISSUER):
    monkeypatch.setattr(settings, "sso_issuer", issuer)
    monkeypatch.setattr(settings, "sso_client_id", "rag")
    monkeypatch.setattr(settings, "sso_redirect_uri", "https://ai.example.com/rag/api/auth/oidc/callback")


def test_sso_login_enabled_requires_client_and_redirect(monkeypatch):
    monkeypatch.setattr(settings, "sso_issuer", ISSUER)
    monkeypatch.setattr(settings, "sso_client_id", "")
    monkeypatch.setattr(settings, "sso_redirect_uri", "")
    assert sso_auth.sso_login_enabled() is False
    _enable_login(monkeypatch)
    assert sso_auth.sso_login_enabled() is True


def test_build_authorize_url_carries_client_redirect_and_state(monkeypatch):
    _enable_login(monkeypatch)
    url = sso_auth.build_authorize_url("st-123")
    assert url.startswith(ISSUER + "/authorize?")
    assert "client_id=rag" in url
    assert "state=st-123" in url
    assert "response_type=code" in url
    assert url.count("redirect_uri=") == 1


def test_build_authorize_url_without_login_config(monkeypatch):
    monkeypatch.setattr(settings, "sso_issuer", "")
    monkeypatch.setattr(settings, "sso_client_id", "")
    monkeypatch.setattr(settings, "sso_redirect_uri", "")
    with pytest.raises(SsoAuthError, match="not configured"):
        sso_auth.build_authorize_url("st")


def test_state_is_one_time():
    st = sso_auth.new_state()
    assert sso_auth.validate_state(st) is True
    assert sso_auth.validate_state(st) is False


def test_state_rejects_unknown_and_expired(monkeypatch):
    assert sso_auth.validate_state("nope") is False
    monkeypatch.setattr(sso_auth, "STATE_TTL_SECONDS", -1)
    st = sso_auth.new_state()
    assert sso_auth.validate_state(st) is False


def test_verify_sso_token_accepts_explicit_audience_override(sso_env):
    """回调拿到的 id_token 受众是本系统 client_id,应能覆盖资源轨受众。"""
    key, _ = sso_env
    token = sign_token(valid_claims(aud="rag"), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)
    assert verify_sso_token(token, audience="rag")["sub"] == "10086"


# ---- 回调端点:建号 / 组映射 / 错误分支 ----


def _prepare_callback(monkeypatch, sso_env, token_aud="rag", target="/rag/login"):
    key, _ = sso_env
    _enable_login(monkeypatch)
    monkeypatch.setattr(settings, "sso_redirect_target", target)
    monkeypatch.setattr(settings, "sso_audience", "dashboard-gateway")
    monkeypatch.setattr(settings, "ldap_group_map_admin", "")

    def _fake_exchange(code: str) -> str:  # noqa: ARG001
        return sign_token(valid_claims(aud=token_aud, name="张三", roles=["admin"]), key)

    from app.api import auth as auth_module

    monkeypatch.setattr(auth_module.sso_auth, "exchange_code", _fake_exchange)
    return auth_module


def test_oidc_callback_provisions_user_with_admin_group(api_client, sso_env, monkeypatch, api_engine):
    auth_module = _prepare_callback(monkeypatch, sso_env)
    state = auth_module.sso_auth.new_state()
    res = api_client.get(
        f"/api/auth/oidc/callback?code=ok&state={state}", follow_redirects=False
    )
    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("/rag/login?sso_token=")

    from app.models.database import User, UserGroup, get_session

    db = get_session(api_engine)
    try:
        user = db.query(User).filter(User.username == "10086").first()
        assert user is not None and user.display_name == "张三"
        groups = [g.group_name for g in db.query(UserGroup).filter(UserGroup.user_id == user.id).all()]
    finally:
        db.close()
    assert "admin" in groups
    assert "__local_admin__" in groups


def test_oidc_callback_rejects_replayed_state(api_client, sso_env, monkeypatch):
    auth_module = _prepare_callback(monkeypatch, sso_env)
    state = auth_module.sso_auth.new_state()
    first = api_client.get(
        f"/api/auth/oidc/callback?code=ok&state={state}", follow_redirects=False
    )
    assert first.status_code == 302
    second = api_client.get(
        f"/api/auth/oidc/callback?code=ok&state={state}", follow_redirects=False
    )
    assert second.status_code == 302
    assert "error=" in second.headers["location"]
    assert "sso_token=" not in second.headers["location"]


def test_oidc_callback_rejects_wrong_audience(api_client, sso_env, monkeypatch):
    auth_module = _prepare_callback(monkeypatch, sso_env, token_aud="someone-else")
    state = auth_module.sso_auth.new_state()
    res = api_client.get(
        f"/api/auth/oidc/callback?code=ok&state={state}", follow_redirects=False
    )
    assert res.status_code == 302
    assert "error=" in res.headers["location"]
    assert "sso_token=" not in res.headers["location"]


def test_oidc_callback_missing_params(api_client, sso_env, monkeypatch):
    _prepare_callback(monkeypatch, sso_env)
    res = api_client.get("/api/auth/oidc/callback", follow_redirects=False)
    assert res.status_code == 302
    assert "error=" in res.headers["location"]


def test_sso_start_redirects_to_issuer(api_client, sso_env, monkeypatch):
    _prepare_callback(monkeypatch, sso_env)
    res = api_client.get("/api/auth/sso/start", follow_redirects=False)
    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith(ISSUER + "/authorize?")
    assert "client_id=rag" in location


def test_sso_start_404_when_not_configured(api_client, monkeypatch):
    monkeypatch.setattr(settings, "sso_issuer", "")
    monkeypatch.setattr(settings, "sso_client_id", "")
    monkeypatch.setattr(settings, "sso_redirect_uri", "")
    res = api_client.get("/api/auth/sso/start", follow_redirects=False)
    assert res.status_code == 404

