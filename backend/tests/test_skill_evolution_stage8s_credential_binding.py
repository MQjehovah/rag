"""凭据绑定终审反例（stage8s）：executor/maintainer/proposer/reviewer 受控凭据。

覆盖（凭据终审清单）：
A. 角色（executor/maintainer/proposer 共用同一 resolve/发送路径）：
  1) settings key 为空 + raw override['llm_api_key'] → 明确拒绝，0 HTTP，错误无 key；
  2) provider 映射存在但 credential_env 环境变量缺失 → 启动/发送前拒绝，0 HTTP；
  3) provider credential_env 存在 → 实际 Authorization 用该 env，绝不用 settings key；
  4) env key=ROLE-KEY vs settings key=EVIL-GLOBAL → 只发送 Bearer ROLE-KEY；
  5) provider endpoint 不匹配 → 发送前拒绝；
  6) 冻结后 provider map 的 credential_env 引用改变 → 发送前拒绝；
  7) 冻结后 provider map 的 allowed endpoint 改变 → 发送前拒绝；
  8) 同一 credential_env 的值 KEY-1→KEY-2 轮换：fingerprint 不变，第二次请求用 KEY-2，
     记录不出现 KEY-1/KEY-2；
  9) enforce_https=true + http endpoint → 发送前拒绝；
 10) 兼容模式（require=false + settings key 存在）可用 settings key，但仍不接受 raw
     override key。
B. reviewer（ReviewerConfig/冻结/_send_http）：
  1) executor 与 reviewer 不同 provider/env → 各自只用各自 key；
  2) settings key=EVIL-GLOBAL → provider-bound reviewer 不得使用；
  3) reviewer credential_env 缺失 → start-preview/构造期 fail-closed，0 HTTP；
  4) reviewer provider 未注册 → 拒绝；
  5) reviewer endpoint 不属于 reviewer provider → 拒绝；
  6) reviewer provider map 冻结后漂移 → 拒绝发送；
  7) reviewer env key 轮换：fingerprint 不变，后续请求用新值；
  8) reviewer frozen JSON 含 provider 绑定字段且无 key 值；
  9) reviewer fingerprint：provider_id/credential_env/allowed_endpoints 变化 → 变；
     secret 值变化 → 不变；
 10) 旧 frozen_reviewer 缺 provider 字段：provider-required real run 拒绝（frozen_missing），
     不得以当前 settings 补齐；
 11) reviewer HTTP payload 仍携带冻结 model/URL/timeout/max_tokens；
 12) reviewer 调用与 guard 式 sender 计数 1:1（端到端精确计数另由 mgmt 管理链与
     浏览器五角色 stub 断言）。
C. 脱敏：哨兵值不得出现在 to_record/frozen/异常文本/预览类 JSON。
本文件只允许在测试替身内存中比对 Authorization（不写日志/文件/验收报告）。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import settings

EXEC_KEY = "EXECUTOR-SECRET-MUST-NOT-LEAK"
REV_KEY = "REVIEWER-SECRET-MUST-NOT-LEAK"
EVIL = "EVIL-GLOBAL-MUST-NOT-BE-USED"


# ---------------------------------------------------------------------------
# 本地替身：记录 Authorization（仅内存）+ 返回各角色可解析 JSON
# ---------------------------------------------------------------------------
class _AuthEcho(BaseHTTPRequestHandler):
    captured: list = []
    count: int = 0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).captured.append({
            "auth": self.headers.get("Authorization"),
            "model": (body.get("model") or body.get("model_id")),
            "url": f"http://{self.server.server_address[0]}:{self.server.server_address[1]}/chat",
            "payload": body,
        })
        type(self).count += 1
        # executor 走 _cfg_http_chat（JSON dict）；reviewer 走 _parse_json_object（dict）
        content = {"summary": "stub", "content": "ok"} if (
            "评审" not in (body.get("messages") or [{}])[0].get("content", "")) \
            else {"identity": "model:rv@pv1",
                  "items": [{"check_id": "c1", "verdict": "pass",
                             "reason": "stub pass"}]}
        out = json.dumps({"choices": [{"message": {"content": json.dumps(
            content, ensure_ascii=False)}}]}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@pytest.fixture()
def auth_srv():
    _AuthEcho.captured = []
    _AuthEcho.count = 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AuthEcho)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/chat"
    try:
        yield url
    finally:
        srv.shutdown()
        srv.server_close()


def _providers(exec_url: str, rev_url: str | None = None) -> dict:
    rev_url = rev_url or exec_url
    return {
        "exec-prov": {"credential_env": "EXEC_ENV",
                      "endpoints": [exec_url], "allow_insecure": True},
        "rev-prov": {"credential_env": "REV_ENV",
                     "endpoints": [rev_url], "allow_insecure": True},
    }


def _set_rails(monkeypatch, *, providers: dict | None = None,
               default: str = "", reviewer: str = "",
               require: bool = True, llm_key: str = "",
               exec_env_val: str = EXEC_KEY, rev_env_val: str = REV_KEY,
               exec_url: str = "", rev_url: str = "",
               llm_url: str = "", llm_model: str = "model-x",
               rev_model: str = "reviewer-m", rev_prompt: str = "pv1"):
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps(providers or {}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", default)
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", reviewer)
    monkeypatch.setattr(settings, "wikiskill_require_provider_binding", require)
    monkeypatch.setattr(settings, "llm_api_key", llm_key)
    monkeypatch.setattr(settings, "llm_api_url", llm_url)
    monkeypatch.setattr(settings, "llm_model", llm_model)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", rev_model)
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", rev_url)
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", rev_prompt)
    monkeypatch.setenv("EXEC_ENV", exec_env_val)
    monkeypatch.setenv("REV_ENV", rev_env_val)
    monkeypatch.delenv("EXEC_ENV", raising=False)
    monkeypatch.delenv("REV_ENV", raising=False)
    monkeypatch.setenv("EXEC_ENV", exec_env_val)
    monkeypatch.setenv("REV_ENV", rev_env_val)


def _role_cfg(monkeypatch, url: str, role="executor", provider="exec-prov",
              env="EXEC_ENV"):
    from app.core.skill_evolution.real_adapters import resolve_real_config
    return resolve_real_config(role, override={
        "provider": provider, "llm_api_url": url, "model": "m-x"})


# ---------------------------------------------------------------------------
# A. 角色（executor 路径即 maintainer/proposer 同一实现）
# ---------------------------------------------------------------------------
def test_A1_raw_override_key_rejected_no_http(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, resolve_real_config)
    _set_rails(monkeypatch, llm_key="", require=True)   # 无 provider、无 settings key
    with pytest.raises(ModelBackendError) as ei:
        resolve_real_config("executor", override={
            "llm_api_url": auth_srv, "model": "m", "llm_api_key": EXEC_KEY})
    assert "raw_credential_override_forbidden" in str(ei.value)
    assert EXEC_KEY not in str(ei.value)
    assert _AuthEcho.count == 0


def test_A2_provider_env_missing_refused(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, resolve_real_config)
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               llm_key=EVIL)
    monkeypatch.delenv("EXEC_ENV", raising=False)       # credential_env 缺失
    with pytest.raises(ModelBackendError) as ei:
        resolve_real_config("executor", override={
            "provider": "exec-prov", "llm_api_url": auth_srv, "model": "m"})
    assert "credential_unavailable" in str(ei.value)
    assert EXEC_KEY not in str(ei.value)
    assert _AuthEcho.count == 0


def test_A3_provider_env_used_not_settings_key(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import build_executor_runner
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               llm_key=EVIL)
    cfg = _role_cfg(monkeypatch, auth_srv)
    assert cfg.provider_id == "exec-prov"
    out = build_executor_runner(cfg)([{"role": "user", "content": "hi"}])
    assert out.get("summary") == "stub"
    assert _AuthEcho.count == 1
    assert _AuthEcho.captured[0]["auth"] == "Bearer " + EXEC_KEY
    assert EVIL not in (_AuthEcho.captured[0]["auth"] or "")


def test_A4_never_uses_evil_global(monkeypatch, auth_srv):
    # 与 A3 同一语义，用哨兵名显式记录（executor key ≠ EVIL-GLOBAL）。
    from app.core.skill_evolution.real_adapters import build_executor_runner
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               llm_key=EVIL)
    cfg = _role_cfg(monkeypatch, auth_srv)
    build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    auth = _AuthEcho.captured[0]["auth"]
    assert auth == "Bearer " + EXEC_KEY
    assert "Bearer " + EVIL != auth


def test_A5_provider_endpoint_mismatch_refused(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, build_executor_runner, resolve_real_config)
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               llm_key=EVIL)
    # 端口不同（host/path 相同）：resolve 预检通过，发送前按受控端点严格拒绝
    other = "http://127.0.0.1:59991/chat"
    cfg = resolve_real_config("executor", override={
        "provider": "exec-prov", "llm_api_url": other, "model": "m"})
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert _AuthEcho.count == 0


def test_A6_mapping_env_reference_changed_after_freeze(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, build_executor_runner, resolve_real_config)
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               llm_key=EVIL)
    cfg = resolve_real_config("executor", override={
        "provider": "exec-prov", "llm_api_url": auth_srv, "model": "m"})
    # 冻结后服务端把 exec-prov 的凭据引用换成 REV_ENV
    pm = _providers(auth_srv)
    pm["exec-prov"]["credential_env"] = "REV_ENV"
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps(pm))
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "credential_reference_changed" in str(ei.value)
    assert _AuthEcho.count == 0


def test_A7_mapping_endpoint_changed_after_freeze(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, build_executor_runner, resolve_real_config)
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               llm_key=EVIL)
    cfg = resolve_real_config("executor", override={
        "provider": "exec-prov", "llm_api_url": auth_srv, "model": "m"})
    pm = _providers(auth_srv)
    pm["exec-prov"]["endpoints"] = ["http://127.0.0.1:59992/chat"]
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps(pm))
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert _AuthEcho.count == 0


def test_A8_role_key_rotation_fingerprint_stable(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        build_executor_runner, resolve_real_config)
    from app.core.skill_evolution.config_freeze import role_frozen_from_live
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               llm_key=EVIL)
    cfg = resolve_real_config("executor", override={
        "provider": "exec-prov", "llm_api_url": auth_srv, "model": "m"})
    fp1 = role_frozen_from_live("executor", override={
        "provider": "exec-prov", "llm_api_url": auth_srv, "model": "m"})
    build_executor_runner(cfg)([{"role": "user", "content": "k1"}])
    assert _AuthEcho.captured[-1]["auth"] == "Bearer " + EXEC_KEY
    monkeypatch.setenv("EXEC_ENV", "KEY-2-ROTATED")     # 值轮换（引用名不变）
    fp2 = role_frozen_from_live("executor", override={
        "provider": "exec-prov", "llm_api_url": auth_srv, "model": "m"})
    assert fp1.fingerprint() == fp2.fingerprint()       # 轮换不改配置指纹
    build_executor_runner(cfg)([{"role": "user", "content": "k2"}])
    assert _AuthEcho.captured[-1]["auth"] == "Bearer KEY-2-ROTATED"
    blob = json.dumps(fp2.to_dict())
    assert "KEY-1" not in blob and "KEY-2-ROTATED" not in blob
    assert EXEC_KEY not in blob


def test_A9_https_required_refused_before_send(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, build_executor_runner, resolve_real_config)
    _set_rails(monkeypatch, providers={
        "sec": {"credential_env": "EXEC_ENV", "endpoints": [auth_srv],
                "allow_insecure": False}}, default="sec", llm_key=EVIL)
    cfg = resolve_real_config("executor", override={
        "provider": "sec", "llm_api_url": auth_srv, "model": "m"})
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "https_required" in str(ei.value)
    assert _AuthEcho.count == 0


def test_A10_legacy_requires_binding_off_and_settings_key(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, build_executor_runner, resolve_real_config)
    _set_rails(monkeypatch, require=False, llm_key=EVIL, llm_url=auth_srv)
    cfg = resolve_real_config("executor")               # legacy：settings key
    assert cfg.provider_id is None
    build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert _AuthEcho.captured[-1]["auth"] == "Bearer " + EVIL
    # 即便兼容模式，raw override key 仍被明确拒绝
    with pytest.raises(ModelBackendError) as ei:
        resolve_real_config("executor", override={
            "llm_api_url": auth_srv, "model": "m", "llm_api_key": EXEC_KEY})
    assert "raw_credential_override_forbidden" in str(ei.value)
    assert EXEC_KEY not in str(ei.value)


# ---------------------------------------------------------------------------
# B. reviewer
# ---------------------------------------------------------------------------
def _rev(monkeypatch, url: str, *, provider="rev-prov", env="REV_ENV",
         llm_key: str = EVIL, endpoints=None):
    from app.core.skill_evolution.model_review import (
        ReviewerConfig, create_reviewer)
    cfg = ReviewerConfig(
        mode="real", model_id="reviewer-m", prompt_version="pv1",
        api_url=url, api_key_present=True, timeout=10.0, retries=1,
        provider_id=provider, credential_env=env,
        allowed_endpoints=tuple(endpoints or [url]), enforce_https=False)
    return create_reviewer(cfg)


def test_B1_executor_reviewer_keys_isolated(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        build_executor_runner, resolve_real_config)
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    ecfg = resolve_real_config("executor", override={
        "provider": "exec-prov", "llm_api_url": auth_srv, "model": "m"})
    build_executor_runner(ecfg)([{"role": "user", "content": "e"}])
    assert _AuthEcho.captured[-1]["auth"] == "Bearer " + EXEC_KEY
    rev = _rev(monkeypatch, auth_srv)
    out = rev._send_http([{"role": "user", "content": "评审任务"}])
    assert out.get("items")
    assert _AuthEcho.count == 2
    assert _AuthEcho.captured[-1]["auth"] == "Bearer " + REV_KEY
    auths = [c["auth"] for c in _AuthEcho.captured]
    assert all(a != "Bearer " + EVIL for a in auths)
    assert EXEC_KEY in auths[0] and REV_KEY in auths[1]


def test_B2_reviewer_never_uses_evil_global(monkeypatch, auth_srv):
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    rev = _rev(monkeypatch, auth_srv)
    rev._send_http([{"role": "user", "content": "评审任务"}])
    assert _AuthEcho.captured[-1]["auth"] == "Bearer " + REV_KEY
    assert "Bearer " + EVIL != _AuthEcho.captured[-1]["auth"]


def test_B3_reviewer_env_missing_fail_closed(monkeypatch, auth_srv):
    from app.core.skill_evolution.model_review import ReviewerError
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    monkeypatch.delenv("REV_ENV", raising=False)
    rev = _rev(monkeypatch, auth_srv)
    with pytest.raises(ReviewerError) as ei:
        rev._send_http([{"role": "user", "content": "评审任务"}])
    assert "credential_unavailable" in str(ei.value)
    assert REV_KEY not in str(ei.value)
    assert _AuthEcho.count == 0
    # 冻结期（start-preview/start 前）同样 fail-closed
    from app.core.skill_evolution.config_freeze import (
        FrozenConfigError, reviewer_frozen_from_live)
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", auth_srv)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-m")
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "pv1")
    with pytest.raises(FrozenConfigError) as fe:
        reviewer_frozen_from_live()
    assert fe.value.code == "reviewer_credential_unavailable"


def test_B4_reviewer_provider_unregistered(monkeypatch, auth_srv):
    from app.core.skill_evolution.config_freeze import (
        FrozenConfigError, reviewer_frozen_from_live)
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="ghost-prov", llm_key=EVIL)
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", auth_srv)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-m")
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "pv1")
    with pytest.raises(FrozenConfigError) as fe:
        reviewer_frozen_from_live()
    assert fe.value.code == "reviewer_provider_required"
    assert "ghost-prov" in str(fe.value)
    assert _AuthEcho.count == 0


def test_B5_reviewer_endpoint_not_in_provider(monkeypatch, auth_srv):
    from app.core.skill_evolution.config_freeze import (
        FrozenConfigError, reviewer_frozen_from_live)
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    other = "http://127.0.0.1:59993/chat"   # 不在 rev-prov 端点
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", other)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-m")
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "pv1")
    with pytest.raises(FrozenConfigError) as fe:
        reviewer_frozen_from_live()
    assert fe.value.code == "reviewer_endpoint_not_bound"
    assert _AuthEcho.count == 0
    # 发送路径同语义（provider_mapping_changed）且 0 HTTP
    from app.core.skill_evolution.model_review import ReviewerError
    rev = _rev(monkeypatch, other, endpoints=[auth_srv])
    with pytest.raises(ReviewerError) as ei:
        rev._send_http([{"role": "user", "content": "评审任务"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert _AuthEcho.count == 0


def test_B6_reviewer_mapping_drift_after_freeze(monkeypatch, auth_srv):
    from app.core.skill_evolution.model_review import ReviewerError
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    rev = _rev(monkeypatch, auth_srv)
    # 冻结后：rev-prov 引用换成 EXEC_ENV → credential_reference_changed
    pm = _providers(auth_srv)
    pm["rev-prov"]["credential_env"] = "EXEC_ENV"
    monkeypatch.setattr(settings, "wikiskill_credential_providers", json.dumps(pm))
    with pytest.raises(ReviewerError) as ei:
        rev._send_http([{"role": "user", "content": "评审任务"}])
    assert "credential_reference_changed" in str(ei.value)
    assert _AuthEcho.count == 0
    # 端点漂移 → provider_mapping_changed
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    rev2 = _rev(monkeypatch, auth_srv)
    pm2 = _providers(auth_srv)
    pm2["rev-prov"]["endpoints"] = ["http://127.0.0.1:59994/chat"]
    monkeypatch.setattr(settings, "wikiskill_credential_providers", json.dumps(pm2))
    with pytest.raises(ReviewerError) as ei:
        rev2._send_http([{"role": "user", "content": "评审任务"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert _AuthEcho.count == 0


def test_B7_reviewer_key_rotation_fingerprint_stable(monkeypatch, auth_srv):
    from app.core.skill_evolution.config_freeze import reviewer_frozen_from_live
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", auth_srv)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-m")
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "pv1")
    f1 = reviewer_frozen_from_live()
    rev = _rev(monkeypatch, auth_srv)
    rev._send_http([{"role": "user", "content": "评审任务"}])
    assert _AuthEcho.captured[-1]["auth"] == "Bearer " + REV_KEY
    monkeypatch.setenv("REV_ENV", "REV-ROTATED-2")       # 值轮换（引用名不变）
    f2 = reviewer_frozen_from_live()
    assert f1["fingerprint"] == f2["fingerprint"]
    rev._send_http([{"role": "user", "content": "评审任务"}])
    assert _AuthEcho.captured[-1]["auth"] == "Bearer REV-ROTATED-2"
    blob = json.dumps(f2)
    assert REV_KEY not in blob and "REV-ROTATED-2" not in blob


def test_B8_reviewer_frozen_has_binding_without_key(monkeypatch, auth_srv):
    from app.core.skill_evolution.config_freeze import reviewer_frozen_from_live
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", auth_srv)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-m")
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "pv1")
    d = reviewer_frozen_from_live()
    assert d["provider_id"] == "rev-prov"
    assert d["credential_env"] == "REV_ENV"
    assert d["allowed_endpoints"] == [auth_srv]
    assert d["enforce_https"] is False
    blob = json.dumps(d, ensure_ascii=False)
    assert REV_KEY not in blob and EXEC_KEY not in blob and EVIL not in blob
    assert d.get("fingerprint")


def test_B9_reviewer_fingerprint_sensitivity(monkeypatch, auth_srv):
    from app.core.skill_evolution.model_review import ReviewerConfig
    base = dict(mode="real", model_id="reviewer-m", prompt_version="pv1",
                api_url=auth_srv, api_key_present=True, timeout=10.0,
                retries=1, provider_id="rev-prov", credential_env="REV_ENV",
                allowed_endpoints=(auth_srv,), enforce_https=False)
    fp0 = ReviewerConfig(**base).fingerprint()
    a = dict(base, provider_id="other-prov")
    b = dict(base, credential_env="OTHER_ENV")
    c = dict(base, allowed_endpoints=(auth_srv + "x",))
    d = dict(base, allowed_endpoints=("http://127.0.0.1:9/a",
                                      "http://127.0.0.1:9/b"))   # 顺序扰动
    e = dict(base, allowed_endpoints=("http://127.0.0.1:9/b",
                                      "http://127.0.0.1:9/a"))
    assert ReviewerConfig(**a).fingerprint() != fp0     # provider 变 → 变
    assert ReviewerConfig(**b).fingerprint() != fp0     # env 名变 → 变
    assert ReviewerConfig(**c).fingerprint() != fp0     # 端点变 → 变
    assert ReviewerConfig(**d).fingerprint() == ReviewerConfig(**e).fingerprint()
    monkeypatch.setenv("REV_ENV", "REV-ROTATED-3")      # 值轮换 → 指纹不变
    assert ReviewerConfig(**base).fingerprint() == fp0


def test_B10_old_frozen_reviewer_fail_closed(monkeypatch, auth_srv):
    from app.core.skill_evolution.config_freeze import (
        FrozenConfigError, build_reviewer_frozen)
    old = {"model_id": "old-model", "prompt_version": "old-pv",
           "api_url": auth_srv, "api_key_present": True, "timeout": 9.0,
           "retries": 1, "max_output_tokens": None,
           "template_version": "t", "template_hash": "h",
           "fingerprint": "old-fp"}                     # 无 provider 绑定字段
    frozen = {"roles": {}, "frozen_reviewer": old, "schema": "runtime-config-freeze/v1"}
    # provider-required 环境：必须拒绝且不得以当前 settings 补齐
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", require=True, llm_key=EVIL)
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", auth_srv)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "current-model")
    with pytest.raises(FrozenConfigError) as fe:
        build_reviewer_frozen(frozen)
    assert fe.value.code == "frozen_missing"
    # require=false 旧兼容才允许 legacy 恢复（model 仍取冻结值，不读当前 settings）
    _set_rails(monkeypatch, require=False, llm_key=EVIL, llm_url=auth_srv)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "current-model")
    rev = build_reviewer_frozen(frozen)
    assert rev.identity == "model:old-model@old-pv"
    assert rev._cfg.model_id == "old-model"


def test_B11_reviewer_payload_uses_frozen_fields(monkeypatch, auth_srv):
    from app.core.skill_evolution.model_review import ReviewerConfig
    from app.core.skill_evolution.model_review import ChatSemanticReviewer
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    cfg = ReviewerConfig(mode="real", model_id="frozen-model-9",
                         prompt_version="pv9", api_url=auth_srv,
                         api_key_present=True, timeout=7.0, retries=1,
                         max_output_tokens=512, provider_id="rev-prov",
                         credential_env="REV_ENV", allowed_endpoints=(auth_srv,))
    rev = ChatSemanticReviewer(cfg)
    rev._send_http([{"role": "user", "content": "评审任务"}], timeout=7.0)
    cap = _AuthEcho.captured[-1]
    assert cap["payload"]["model"] == "frozen-model-9"
    assert cap["payload"]["max_tokens"] == 512
    assert cap["url"] == auth_srv
    assert cap["auth"] == "Bearer " + REV_KEY


def test_B12_reviewer_guard_sender_count_1to1(monkeypatch, auth_srv):
    """guard 式 sender 包裹：每次 review() 恰好 1 次 HTTP/计数（端到端预算精确
    相等另由 mgmt 管理链与五角色浏览器 stub 断言 used==sum(role_counts)）。"""
    from app.core.skill_evolution.model_review import ChatSemanticReviewer
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL)
    rev = _rev(monkeypatch, auth_srv)
    calls = []

    def counted(messages, context="", timeout=None):
        calls.append(context)
        return rev._send_http(messages, context=context, timeout=timeout)
    rev.attach_sender(counted)
    res = rev.review(task_id="t", sources=["s1"],
                     candidate_output={"x": 1},
                     checks=[{"id": "c1", "check": "覆盖要点", "detail": "d",
                              "boundary": "b"}])
    assert res["identity"]
    assert len(calls) == 1 and _AuthEcho.count == 1


# ---------------------------------------------------------------------------
# C. 脱敏扫描
# ---------------------------------------------------------------------------
def test_C_sentinels_nowhere(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        build_executor_runner, resolve_real_config)
    from app.core.skill_evolution.config_freeze import (
        build_frozen_block, reviewer_frozen_from_live)
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL, llm_url=auth_srv,
               llm_model="m-x")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", auth_srv)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-m")
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "pv1")
    sentinels = [EXEC_KEY, REV_KEY, EVIL]
    cfg = resolve_real_config("executor", override={
        "provider": "exec-prov", "llm_api_url": auth_srv, "model": "m"})
    blob_record = json.dumps(cfg.to_record())
    block = build_frozen_block(review="v2")
    blob_frozen = json.dumps(block)
    rv = reviewer_frozen_from_live()
    blob_rev = json.dumps(rv)
    # 成功请求（载荷含在内存替身里，不落任何记录）
    build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert _AuthEcho.count == 1
    for s in sentinels:
        assert s not in blob_record, s
        assert s not in blob_frozen, s
        assert s not in blob_rev, s
    assert block.get("frozen_reviewer", {}).get("provider_id") == "rev-prov"
    assert block.get("frozen_reviewer", {}).get("credential_env") == "REV_ENV"


# ===========================================================================
# E. 端点授权 P1（结构化 URL 校验；先反例后修复）
# ===========================================================================
# 允许端点身份 = scheme + hostname + effective port + path prefix（分段边界）。
# 旧实现（url.startswith / host-only）会错误放行同 host 非授权路径、/v1→/v10
# 前缀碰撞与跨 scheme；以下参数化用例必须在修复后全部通过，修复前关键用例为红。

_URL = "https://api.example.test"


def _u(path):
    return _URL + path


_ACCEPT = [
    # 1. 完全一致
    (f"{_URL}/v1/chat", [f"{_URL}/v1/chat"]),
    # 2. 授权路径的真实子路径
    (f"{_URL}/v1/chat/sub", [f"{_URL}/v1/chat"]),
    (f"{_URL}/v1/chat/a/b", [f"{_URL}/v1/chat"]),
    # 3. scheme/hostname/effective port 一致（https 默认 443 与显式 :443 等价）
    (f"{_URL}/v1", [f"{_URL}/v1"]),
    (f"{_URL}:443/v1", [f"{_URL}/v1"]),
    (f"{_URL}/v1", [f"{_URL}:443/v1"]),
    # 4. http 默认 80 与显式 :80 等价
    ("http://api.example.test/v1", ["http://api.example.test/v1"]),
    ("http://api.example.test:80/v1", ["http://api.example.test/v1"]),
    ("http://api.example.test/v1", ["http://api.example.test:80/v1"]),
    # 5. 根路径覆盖同一 origin 合法路径
    (f"{_URL}/anything/here", [f"{_URL}/"]),
    # 6. allowed 无 query：目标合法路径可带普通 query
    (f"{_URL}/v1/chat?x=1&y=2", [f"{_URL}/v1/chat"]),
    # 7. IPv4 / 本地域名 / 本地 HTTP stub 端口
    ("http://127.0.0.1:28773/chat", ["http://127.0.0.1:28773/chat"]),
    ("http://localhost:9001/v1", ["http://localhost:9001/v1"]),
    ("http://127.0.0.1:9000/", ["http://127.0.0.1:9000/"]),
    # 8. IPv6（解析层匹配，不联网）
    ("http://[::1]/v1", ["http://[::1]/v1"]),
    # 9. 尾部斜杠稳定规范化
    (f"{_URL}/v1/", [f"{_URL}/v1"]),
    (f"{_URL}/v1", [f"{_URL}/v1/"]),
    (f"{_URL}/v1/chat/", [f"{_URL}/v1/chat/"]),
    # 10. 无害百分号编码（同串等价）
    (f"{_URL}/v1/caf%C3%A9", [f"{_URL}/v1/caf%C3%A9"]),
]

_REJECT = [
    # 同主机不同路径 / startswith 前缀碰撞（任务已复现三项）
    (f"{_URL}/evil", [f"{_URL}/v1/chat"]),
    (f"{_URL}/v10", [f"{_URL}/v1"]),
    ("http://api.example.test/evil", [f"{_URL}/v1"]),          # 跨 scheme
    (f"{_URL}/v1/evil", [f"{_URL}/v1/chat"]),
    ("http://api.example.test/v1/chat", [f"{_URL}/v1/chat"]),  # http→https
    ("https://api.example.test/v1", ["http://api.example.test/v1"]),  # https→http
    # 端口
    (f"{_URL}:444/v1", [f"{_URL}/v1"]),
    ("http://api.example.test:81/v1", ["http://api.example.test:80/v1"]),
    ("http://api.example.test:80/v1", ["http://api.example.test:81/v1"]),
    (f"{_URL}:443/v1", [f"{_URL}:444/v1"]),
    # hostname
    ("https://other.example.test/v1", [f"{_URL}/v1"]),
    ("https://api.example.test./v1", [f"{_URL}/v1"]),  # 未做尾点归一 → 视为不同
    # 缺少 scheme / hostname
    ("api.example.test/v1", [f"{_URL}/v1"]),
    (f"{_URL}/v1", ["api.example.test/v1"]),
    ("https:///v1", [f"{_URL}/v1"]),
    ("https://:443/v1", [f"{_URL}/v1"]),
    # userinfo / fragment
    ("https://user:pass@api.example.test/v1", [f"{_URL}/v1"]),
    (f"{_URL}/v1", ["https://user:pass@api.example.test/v1"]),
    (f"{_URL}/v1#frag", [f"{_URL}/v1"]),
    # 反斜杠 / 控制字符
    ("https://api.example.test/v1\\evil", [f"{_URL}/v1"]),
    ("https://api.example.test/v1/..\\evil", [f"{_URL}/v1"]),
    ("https://api.example.test/\x00evil", [f"{_URL}/v1"]),
    # 点路径逃逸
    (f"{_URL}/v1/../admin", [f"{_URL}/v1"]),
    (f"{_URL}/v1/./x", [f"{_URL}/v1"]),
    (f"{_URL}/./v1", [f"{_URL}/v1"]),
    # 百分号编码逃逸（点/斜杠/反斜杠/NUL/控制字符）
    (f"{_URL}/v1/%2e%2e/admin", [f"{_URL}/v1"]),
    (f"{_URL}/v1/%2e/x", [f"{_URL}/v1"]),
    (f"{_URL}/v1%2f..%2fadmin", [f"{_URL}/v1"]),
    (f"{_URL}/v1%5cadmin", [f"{_URL}/v1"]),
    (f"{_URL}/v1/%00admin", [f"{_URL}/v1"]),
    (f"{_URL}/v1/%0aadmin", [f"{_URL}/v1"]),
    # malformed / 非法端口
    ("https://api.example.test:0/v1", [f"{_URL}/v1"]),
    ("https://api.example.test:99999999/v1", [f"{_URL}/v1"]),
    ("https://api.example.test:abc/v1", [f"{_URL}/v1"]),
    ("https://[bad/v1", [f"{_URL}/v1"]),
    ("://no-scheme", [f"{_URL}/v1"]),
    # 端点含 query：目标不同 query 不得扩大授权
    (f"{_URL}/v1?x=evil", [f"{_URL}/v1?x=good"]),
    (f"{_URL}/v1?x=good&y=1", [f"{_URL}/v1?x=good"]),
    (f"{_URL}/v1?x=goodish", [f"{_URL}/v1?x=good"]),   # query 前缀匹配禁止
]


@pytest.mark.parametrize("url,allowed", _ACCEPT)
def test_E_endpoint_in_accepts_authorized(url, allowed):
    from app.core.skill_evolution.credential_binding import endpoint_in
    assert endpoint_in(url, allowed) is True


@pytest.mark.parametrize("url,allowed", _REJECT)
def test_E_endpoint_in_rejects_unauthorized(url, allowed):
    from app.core.skill_evolution.credential_binding import endpoint_in
    assert endpoint_in(url, allowed) is False


def test_E_endpoint_in_malformed_allowed_never_raises():
    from app.core.skill_evolution.credential_binding import endpoint_in
    for bad in ("://", "https://[bad/v1", "http:///x", "",
                "https://api.example.test:99999/v1",
                "https://user:pw@h/v1"):
        # 稳定返回布尔（False 或 True 都可），绝不抛原始解析异常
        assert isinstance(endpoint_in(f"{_URL}/v1", [bad]), bool)
        assert isinstance(endpoint_in(bad, [f"{_URL}/v1"]), bool)


# ---- 发送级拒绝（executor 与 reviewer 共用 validate_frozen_send；0 HTTP） ----
_HOSTILE = [
    (f"{_URL}/evil", [f"{_URL}/v1/chat"], "同 host 非授权路径"),
    (f"{_URL}/v10", [f"{_URL}/v1"], "v1/v10 前缀碰撞"),
    ("http://api.example.test/v1/chat", [f"{_URL}/v1/chat"], "跨 scheme"),
    (f"{_URL}:444/v1", [f"{_URL}/v1"], "端口不同"),
    ("https://other.test/v1", [f"{_URL}/v1"], "host 不同"),
    (f"{_URL}/v1/../admin", [f"{_URL}/v1"], "点路径逃逸"),
    (f"{_URL}/v1%2f..%2fadmin", [f"{_URL}/v1"], "编码斜杠逃逸"),
]


def _set_provider_pair(monkeypatch, auth_srv):
    _set_rails(monkeypatch, providers=_providers(auth_srv), default="exec-prov",
               reviewer="rev-prov", llm_key=EVIL, llm_url=auth_srv,
               llm_model="m-x")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", auth_srv)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-m")
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "pv1")


@pytest.mark.parametrize("hostile,allowed,why", _HOSTILE)
def test_E_executor_send_rejects_hostile_url_zero_http(monkeypatch, auth_srv,
                                                       hostile, allowed, why):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, ModelConfig, build_executor_runner)
    _set_provider_pair(monkeypatch, auth_srv)
    cfg = ModelConfig(mode="real", model="m-x", api_url=hostile,
                      api_key_present=True,
                      provider_id="exec-prov", credential_env="EXEC_ENV",
                      allowed_endpoints=tuple(allowed), enforce_https=False)
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "provider_mapping_changed" in str(ei.value), why
    assert _AuthEcho.count == 0
    assert _AuthEcho.count == 0


@pytest.mark.parametrize("hostile,allowed,why", _HOSTILE)
def test_E_reviewer_send_rejects_hostile_url_zero_http(monkeypatch, auth_srv,
                                                       hostile, allowed, why):
    from app.core.skill_evolution.model_review import (
        ReviewerConfig, ReviewerError, create_reviewer)
    _set_provider_pair(monkeypatch, auth_srv)
    cfg = ReviewerConfig(mode="real", model_id="reviewer-m",
                         prompt_version="pv1", api_url=hostile,
                         api_key_present=True, provider_id="rev-prov",
                         credential_env="REV_ENV",
                         allowed_endpoints=tuple(allowed), enforce_https=False)
    rev = create_reviewer(cfg)
    with pytest.raises(ReviewerError) as ei:
        rev._send_http([{"role": "user", "content": "评审任务"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert _AuthEcho.count == 0


# ===========================================================================
# F. 端点授权最后补丁（预检 host / query 精确 / 多层编码）
# ===========================================================================
# A. endpoint_declared：不同 hostname 必须拒绝（根路径授权也不得跨 host）。
_DECL_REJECT = [
    ("https://evil.example/v1/chat", ["https://good.example/v1/chat"]),
    ("https://evil.example/v1", ["https://good.example/"]),      # 根路径跨 host
    ("https://good.example/x", ["https://other.example/"]),      # 根路径跨 host
    ("http://127.0.0.2:9001/v1", ["http://127.0.0.1:9001/v1"]),  # IPv4 不同
    ("http://[::2]/v1", ["http://[::1]/v1"]),                    # IPv6 不同
]
_DECL_ACCEPT = [
    ("https://good.example/v1/chat", ["https://good.example/v1/chat"]),
    ("https://good.example/v1/chat/sub", ["https://good.example/v1/chat"]),
    ("http://127.0.0.1:9001/v1", ["http://127.0.0.1:9001/v1"]),
    # 分层语义保留：http URL 先过声明预检，发送门禁再抛 https_required
    ("http://vendor.example/v1/chat", ["https://vendor.example/v1/chat"]),
]


@pytest.mark.parametrize("url,allowed", _DECL_REJECT)
def test_F_declared_rejects_cross_hostname(url, allowed):
    from app.core.skill_evolution.credential_binding import endpoint_declared
    assert endpoint_declared(url, allowed) is False


@pytest.mark.parametrize("url,allowed", _DECL_ACCEPT)
def test_F_declared_accepts_same_hostname(url, allowed):
    from app.core.skill_evolution.credential_binding import endpoint_declared
    assert endpoint_declared(url, allowed) is True


# B. allowed 含 query：路径与 query 都必须精确相等，禁止子路径扩张。
_QUERY_REJECT = [
    ("https://api.example/v1/sub?key=ok", ["https://api.example/v1?key=ok"]),
    ("https://api.example/v1/sub?key=ok", ["https://api.example/v1/?key=ok"]),
    ("https://api.example/v1?key=bad", ["https://api.example/v1?key=ok"]),
    ("https://api.example/v1?key=o", ["https://api.example/v1?key=ok"]),  # 前缀碰撞
    ("https://api.example/v1?key=ok&x=1", ["https://api.example/v1?key=ok"]),  # 追加
    ("https://api.example/v1/sub?key=ok&x=1", ["https://api.example/v1?key=ok"]),
    ("https://api.example/v1/other?key=ok", ["https://api.example/v1?key=ok"]),
]
_QUERY_ACCEPT = [
    ("https://api.example/v1?key=ok", ["https://api.example/v1?key=ok"]),
    ("https://api.example:443/v1?key=ok",
     ["https://api.example/v1?key=ok"]),   # 显式默认端口等价
    ("https://api.example/v1?key=ok",
     ["https://api.example:443/v1?key=ok"]),
    # allowed 无 query：仍允许授权路径（含子路径）携带普通 query
    ("https://api.example/v1/sub?key=ok", ["https://api.example/v1"]),
]


@pytest.mark.parametrize("url,allowed", _QUERY_REJECT)
def test_F_query_rejects_subpath_or_diff_query(url, allowed):
    from app.core.skill_evolution.credential_binding import endpoint_in
    assert endpoint_in(url, allowed) is False


@pytest.mark.parametrize("url,allowed", _QUERY_ACCEPT)
def test_F_query_accepts_exact_or_plain_query(url, allowed):
    from app.core.skill_evolution.credential_binding import endpoint_in
    assert endpoint_in(url, allowed) is True


# C. 多层百分号编码与 malformed escape：一律拒绝（fail-closed）。
_ENC_REJECT = [
    ("https://api.example/v1/%252e%252e/admin", ["https://api.example/v1"]),
    ("https://api.example/v1/%252E%252E/admin", ["https://api.example/v1"]),
    ("https://api.example/v1/%252fadmin", ["https://api.example/v1"]),
    ("https://api.example/v1/%255cadmin", ["https://api.example/v1"]),
    ("https://api.example/v1/%25252e%25252e/admin", ["https://api.example/v1"]),
    ("https://api.example/v1/%252e./admin", ["https://api.example/v1"]),   # 混合
    ("https://api.example/v1/%2500admin", ["https://api.example/v1"]),     # 多层 NUL
    ("https://api.example/v1/%250aadmin", ["https://api.example/v1"]),     # 多层控制
    ("https://api.example/v1/%2e%2e/admin", ["https://api.example/v1"]),   # 单层仍拒
    ("https://api.example/v1/%", ["https://api.example/v1"]),              # malformed
    ("https://api.example/v1/%2", ["https://api.example/v1"]),             # malformed
    ("https://api.example/v1/%GG", ["https://api.example/v1"]),            # malformed
    ("https://api.example/v1/%", ["https://api.example/v1/%2"]),           # 两侧 malformed
]
_ENC_ACCEPT = [
    ("https://api.example/v1/caf%C3%A9", ["https://api.example/v1/caf%C3%A9"]),
    ("https://api.example/v1/a%62", ["https://api.example/v1/a%62"]),  # 安全单层编码
    ("https://api.example/v1/%E4%B8%AD", ["https://api.example/v1/%E4%B8%AD"]),
]


@pytest.mark.parametrize("url,allowed", _ENC_REJECT)
def test_F_multilayer_encoding_rejected(url, allowed):
    from app.core.skill_evolution.credential_binding import endpoint_in
    assert endpoint_in(url, allowed) is False


@pytest.mark.parametrize("url,allowed", _ENC_ACCEPT)
def test_F_safe_encoding_still_accepted(url, allowed):
    from app.core.skill_evolution.credential_binding import endpoint_in
    assert endpoint_in(url, allowed) is True


def test_E_send_frozen_endpoints_empty_fails_closed(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, ModelConfig, build_executor_runner)
    _set_provider_pair(monkeypatch, auth_srv)
    cfg = ModelConfig(mode="real", model="m-x", api_url=auth_srv,
                      api_key_present=True, provider_id="exec-prov",
                      credential_env="EXEC_ENV", allowed_endpoints=(),
                      enforce_https=False)
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert "冻结端点集为空" in str(ei.value)
    assert _AuthEcho.count == 0


def test_E_send_current_map_endpoints_empty_fails_closed(monkeypatch, auth_srv):
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, ModelConfig, build_executor_runner)
    _set_provider_pair(monkeypatch, auth_srv)
    pm = _providers(auth_srv)
    pm["exec-prov"]["endpoints"] = []
    monkeypatch.setattr(settings, "wikiskill_credential_providers", json.dumps(pm))
    cfg = ModelConfig(mode="real", model="m-x", api_url=auth_srv,
                      api_key_present=True, provider_id="exec-prov",
                      credential_env="EXEC_ENV",
                      allowed_endpoints=(auth_srv,), enforce_https=False)
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert "当前 provider 映射 endpoints 为空" in str(ei.value)
    assert _AuthEcho.count == 0


def test_E_send_url_hits_only_current_map_rejected(monkeypatch, auth_srv):
    """只命中当前映射、不命中冻结端点 → 拒绝（双重集合同时生效）。"""
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, ModelConfig, build_executor_runner)
    _set_provider_pair(monkeypatch, auth_srv)
    pm = _providers(auth_srv)
    pm["exec-prov"]["endpoints"] = [auth_srv]      # 当前映射含 auth_srv
    monkeypatch.setattr(settings, "wikiskill_credential_providers", json.dumps(pm))
    other_ok = auth_srv + "/sub"                    # 冻结端点是 auth_srv/sub
    cfg = ModelConfig(mode="real", model="m-x", api_url=auth_srv,
                      api_key_present=True, provider_id="exec-prov",
                      credential_env="EXEC_ENV",
                      allowed_endpoints=(other_ok,), enforce_https=False)
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert _AuthEcho.count == 0


def test_E_send_url_hits_only_frozen_rejected(monkeypatch, auth_srv):
    """只命中冻结端点、不命中当前映射 → 拒绝（既有映射漂移语义扩展）。"""
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, ModelConfig, build_executor_runner)
    _set_provider_pair(monkeypatch, auth_srv)
    pm = _providers(auth_srv)
    pm["exec-prov"]["endpoints"] = [auth_srv + "/other"]   # 当前映射不含 auth_srv
    monkeypatch.setattr(settings, "wikiskill_credential_providers", json.dumps(pm))
    cfg = ModelConfig(mode="real", model="m-x", api_url=auth_srv,
                      api_key_present=True, provider_id="exec-prov",
                      credential_env="EXEC_ENV",
                      allowed_endpoints=(auth_srv,), enforce_https=False)
    with pytest.raises(ModelBackendError) as ei:
        build_executor_runner(cfg)([{"role": "user", "content": "x"}])
    assert "provider_mapping_changed" in str(ei.value)
    assert _AuthEcho.count == 0
