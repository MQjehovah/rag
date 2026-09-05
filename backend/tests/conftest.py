"""backend 测试共享 fixtures 与 helper。

当前为空占位：Task 3 复用时再把 SSO 自签 token 的 RSA/JWKS helper 抽到这里。
"""

import pytest


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
