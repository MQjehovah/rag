"""M3 最终项一：凭据—端点受控绑定（provider/config ID + 引用 + 端点 三关联）。

反例：
- 两个受控供应商不同虚构 key 互不串用；
- 端点合法但凭据引用不匹配 → 发送前拒绝（零 HTTP）；
- 冻结后改变供应商映射 → 明确拒绝（provider_mapping_changed）；
- 同一凭据引用轮换密钥不改变冻结模型与端点；
- 正式端点默认要求 HTTPS；日志/记录/DB/API 不含密钥。
全程本地 monkeypatch httpx（无真实网络）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import settings


class _FakeResp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": json.dumps({"ok": 1})}}]}


def _patch_http(monkeypatch, calls):
    def _post(url, **kw):
        calls.append({"url": url, "auth": (kw.get("headers") or {}).get(
            "Authorization"), "payload": kw.get("json"),
            "follow_redirects": kw.get("follow_redirects")})
        return _FakeResp()

    monkeypatch.setattr(httpx, "post", _post)
    return calls


def _providers_json():
    return json.dumps({
        "alpha": {"credential_env": "FK_ALPHA",
                  "endpoints": ["http://127.0.0.1:9001/v1/chat"],
                  "allow_insecure": True},
        "beta": {"credential_env": "FK_BETA",
                 "endpoints": ["http://127.0.0.1:9002/v1/chat"],
                 "allow_insecure": True},
    })


@pytest.fixture()
def prov_env(monkeypatch):
    monkeypatch.setenv("FK_ALPHA", "key-alpha-secret")
    monkeypatch.setenv("FK_BETA", "key-beta-secret")
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        _providers_json())
    monkeypatch.setattr(settings, "wikiskill_default_provider", "")
    monkeypatch.setattr(settings, "llm_api_key", "global-fallback-secret")
    monkeypatch.setattr(settings, "llm_model", "model-x")


def test_two_providers_never_cross_use_keys(prov_env, monkeypatch):
    from app.core.skill_evolution import real_adapters as ra
    calls = _patch_http(monkeypatch, [])
    ca = ra.resolve_real_config("executor",
                                override={"provider": "alpha",
                                          "llm_api_url": "http://127.0.0.1:9001/v1/chat"})
    cb = ra.resolve_real_config("executor",
                                override={"provider": "beta",
                                          "llm_api_url": "http://127.0.0.1:9002/v1/chat"})
    ra._cfg_http_chat(ca, [])
    ra._cfg_http_chat(cb, [])
    assert calls[0]["auth"] == "Bearer key-alpha-secret"
    assert calls[1]["auth"] == "Bearer key-beta-secret"
    assert "global-fallback-secret" not in calls[0]["auth"] + calls[1]["auth"]


def test_credential_reference_mismatch_refused_before_send(prov_env,
                                                           monkeypatch):
    """端点合法但凭据引用被改（settings 换成另一供应商凭据）→ 发送前拒绝。"""
    from app.core.skill_evolution import real_adapters as ra
    calls = _patch_http(monkeypatch, [])
    cfg = ra.resolve_real_config("executor",
                                 override={"provider": "alpha",
                                           "llm_api_url": "http://127.0.0.1:9001/v1/chat"})
    # settings：alpha 的凭据引用被改成 beta 的引用
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps({
                            "alpha": {"credential_env": "FK_BETA",
                                      "endpoints": ["http://127.0.0.1:9001/v1/chat"],
                                      "allow_insecure": True},
                            "beta": {"credential_env": "FK_BETA",
                                     "endpoints": ["http://127.0.0.1:9002/v1/chat"],
                                     "allow_insecure": True}}))
    with pytest.raises(ra.ModelBackendError) as ei:
        ra._cfg_http_chat(cfg, [])
    assert "credential_reference_changed" in str(ei.value)
    assert calls == []


def test_mapping_change_after_freeze_refused(prov_env, monkeypatch):
    from app.core.skill_evolution import real_adapters as ra
    calls = _patch_http(monkeypatch, [])
    cfg = ra.resolve_real_config("executor",
                                 override={"provider": "alpha",
                                           "llm_api_url": "http://127.0.0.1:9001/v1/chat"})
    # 冻结后：alpha 从映射移除 / 端点改变 → 明确拒绝
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps({
                            "alpha": {"credential_env": "FK_ALPHA",
                                      "endpoints": ["https://vendor-elsewhere/v1/chat"],
                                      "allow_insecure": False}}))
    with pytest.raises(ra.ModelBackendError) as ei:
        ra._cfg_http_chat(cfg, [])
    assert "provider_mapping_changed" in str(ei.value)
    assert calls == []


def test_key_rotation_same_reference_keeps_frozen_model_endpoint(
        prov_env, monkeypatch):
    from app.core.skill_evolution import real_adapters as ra
    calls = _patch_http(monkeypatch, [])
    cfg = ra.resolve_real_config("executor",
                                 override={"provider": "alpha",
                                           "llm_api_url": "http://127.0.0.1:9001/v1/chat"})
    ra._cfg_http_chat(cfg, [])
    monkeypatch.setenv("FK_ALPHA", "key-alpha-rotated")   # 同引用轮换
    ra._cfg_http_chat(cfg, [])
    assert calls[0]["auth"] == "Bearer key-alpha-secret"
    assert calls[1]["auth"] == "Bearer key-alpha-rotated"
    # 冻结模型与端点不变（同一 cfg 对象发送；payload.model/url 相同）
    assert calls[0]["payload"]["model"] == calls[1]["payload"]["model"]
    assert calls[0]["url"] == calls[1]["url"] == "http://127.0.0.1:9001/v1/chat"
    # 重定向关闭
    assert calls[0]["follow_redirects"] is False


def test_https_required_and_no_secret_in_records(prov_env, monkeypatch):
    from app.core.skill_evolution import real_adapters as ra
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps({
                            "sec": {"credential_env": "FK_ALPHA",
                                    "endpoints": ["https://vendor.example/v1/chat"],
                                    "allow_insecure": False}}))
    calls = _patch_http(monkeypatch, [])
    # 正式端点默认要求 HTTPS：http 端点被拒
    cfg_http = ra.resolve_real_config(
        "executor", override={"provider": "sec",
                              "llm_api_url": "http://vendor.example/v1/chat"})
    with pytest.raises(ra.ModelBackendError) as ei:
        ra._cfg_http_chat(cfg_http, [])
    assert "https_required" in str(ei.value)
    assert calls == []
    cfg_https = ra.resolve_real_config(
        "executor", override={"provider": "sec",
                              "llm_api_url": "https://vendor.example/v1/chat"})
    ra._cfg_http_chat(cfg_https, [])
    # 记录/冻结结构不落密钥（含 provider 引用为 env 名，非值）
    blob = json.dumps(cfg_https.to_record(), ensure_ascii=False)
    assert "key-alpha-secret" not in blob
    assert "credential_env" in blob and "FK_ALPHA" in blob


def test_records_and_frozen_no_secret(prov_env, monkeypatch):
    from app.core.skill_evolution import config_freeze as cf
    monkeypatch.setattr(settings, "wikiskill_default_provider", "alpha")
    monkeypatch.setattr(settings, "llm_api_url", "http://127.0.0.1:9001/v1/chat")
    frozen = cf.build_frozen_block(review=None)
    blob = json.dumps(frozen, ensure_ascii=False)
    for secret in ("key-alpha-secret", "key-beta-secret",
                   "global-fallback-secret"):
        assert secret not in blob
    roles = frozen["roles"]
    for fd in roles.values():
        assert "credential_env" in fd or "provider_id" not in fd or \
               fd.get("provider_id") is None
        assert "key-alpha-secret" not in json.dumps(fd)
