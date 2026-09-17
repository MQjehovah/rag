"""M3 最终验收：凭据端点受控绑定 + 新授权流程独立子进程链路。

A. 凭据绑定（endpoint guard）：
   - executor/maintainer/proposer/reviewer 发送路径在 HTTP 前校验受控允许名单；
   - 任意地址 + 全局密钥 → 拒绝且 HTTP 计数为 0；
   - 恢复/冻结路径不受 settings 漂移改绑：frozen runner 仍发冻结端点。

B. 新授权流程子进程验收（真实 Popen 独立子进程 worker，非 spawner 桩）：
   管理员 create → 无网络 start-preview（脱敏） → 确认完整指纹 start →
   独立子进程 cli evolution-run（经 worker_start_gate）→ 本地 HTTP stub 收到
   真实请求（auth/model）→ run 收敛。
   覆盖：预览后配置漂移 start 拒绝且零出站；错误指纹拒绝；重复 start 409；
   未确认/缺授权直接 CLI 零出站（见 stage8o，此处引用并复述一条）。
全程本地 stub + 虚构凭据；不触真实供应商。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings

BACKEND = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A. 端点受控绑定（发送前拒绝；零 HTTP）
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _patched_http_post(monkeypatch, calls):
    def _post(url, **kw):
        calls.append({"url": url, "headers": kw.get("headers", {}),
                      "payload": kw.get("json")})
        return _FakeResp(json.dumps({"ok": 1}))

    monkeypatch.setattr(httpx, "post", _post)
    return calls


def test_global_key_never_sent_to_arbitrary_endpoint(monkeypatch):
    """任意地址 + 全局密钥：发送前拒绝（endpoint_not_allowed），HTTP 计数 0。"""
    from app.core.skill_evolution import real_adapters as ra
    monkeypatch.setattr(settings, "llm_api_url", "http://allowed.example/v1")
    monkeypatch.setattr(settings, "llm_api_key", "global-secret")

    monkeypatch.setattr(settings, "wikiskill_allowed_llm_endpoints", "")
    calls = _patched_http_post(monkeypatch, [])
    cfg = ra.ModelConfig(mode="real", model="m", api_url="http://evil.invalid/v1",
                         api_key_present=True)
    with pytest.raises(ra.ModelBackendError) as ei:
        ra._cfg_http_chat(cfg, [{"role": "user", "content": "hi"}])
    assert "endpoint_not_allowed" in str(ei.value)
    assert calls == []                                  # HTTP 请求计数为零


def test_allowed_default_endpoint_sends_with_controlled_key(monkeypatch):
    from app.core.skill_evolution import real_adapters as ra
    monkeypatch.setattr(settings, "llm_api_url", "http://127.0.0.1:9999/v1/chat")
    monkeypatch.setattr(settings, "llm_api_key", "global-secret")
    monkeypatch.setattr(settings, "wikiskill_allowed_llm_endpoints", "")
    calls = _patched_http_post(monkeypatch, [])
    cfg = ra.ModelConfig(mode="real", model="m",
                         api_url="http://127.0.0.1:9999/v1/chat",
                         api_key_present=True)
    out = ra._cfg_http_chat(cfg, [{"role": "user", "content": "hi"}])
    assert out == {"ok": 1}
    assert len(calls) == 1
    assert calls[0]["headers"].get("Authorization") == "Bearer global-secret"
    assert calls[0]["url"].startswith("http://127.0.0.1:9999/")


def test_explicit_allowlist_overrides_defaults(monkeypatch):
    from app.core.skill_evolution import real_adapters as ra
    monkeypatch.setattr(settings, "llm_api_url", "http://allowed.example/v1")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "wikiskill_allowed_llm_endpoints",
                        "127.0.0.1:1234")
    calls = _patched_http_post(monkeypatch, [])
    cfg = ra.ModelConfig(mode="real", model="m",
                         api_url="http://allowed.example/v1",
                         api_key_present=True)
    with pytest.raises(ra.ModelBackendError):
        ra._cfg_http_chat(cfg, [])                     # 名单明确 → 默认端点也不放行
    assert calls == []
    ok_cfg = ra.ModelConfig(mode="real", model="m",
                            api_url="http://127.0.0.1:1234/v1",
                            api_key_present=True)
    ra._cfg_http_chat(ok_cfg, [])
    assert len(calls) == 1


def test_reviewer_send_respects_endpoint_allowlist(monkeypatch):
    """评审发送路径同受控端点校验；任意地址零 HTTP。"""
    from app.core.skill_evolution import model_review as mr
    monkeypatch.setattr(settings, "llm_api_url", "http://allowed.example/v1")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url",
                        "http://review.allowed.example/review")
    monkeypatch.setattr(settings, "wikiskill_allowed_llm_endpoints", "")
    calls = _patched_http_post(monkeypatch, [])
    cfg = mr.ReviewerConfig(mode="real", model_id="rv",
                            prompt_version="v1",
                            api_url="http://evil.invalid/review",
                            api_key_present=True)
    reviewer = mr.create_reviewer(cfg)
    with pytest.raises(mr.ReviewerError) as ei:
        reviewer._send_http([{"role": "user", "content": "x"}])
    assert "endpoint_not_allowed" in str(ei.value)
    assert calls == []


def test_frozen_runner_keeps_endpoint_despite_settings_drift(monkeypatch):
    """settings 漂移不得改绑：frozen runner 仍发冻结端点（发送层再校验名单）。"""
    from app.core.skill_evolution import config_freeze as cf
    monkeypatch.setattr(settings, "llm_api_url", "http://127.0.0.1:9999/v1/chat")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "frozen-model")
    monkeypatch.setattr(settings, "wikiskill_allowed_llm_endpoints",
                        "127.0.0.1:9999")
    block = cf.build_frozen_block(review=None)
    # 冻结后 settings 漂移到任意端点；受控名单仍只放行冻结端点
    monkeypatch.setattr(settings, "llm_api_url", "http://evil.invalid/v1")
    monkeypatch.setattr(settings, "wikiskill_allowed_llm_endpoints",
                        "127.0.0.1:9999")
    calls = _patched_http_post(monkeypatch, [])
    runner = cf.build_actor_runner_frozen("executor", block)
    out = runner([{"role": "user", "content": "hi"}])
    assert out == {"ok": 1}
    assert calls and calls[0]["url"].startswith("http://127.0.0.1:9999/")
    assert not calls[0]["url"].startswith("http://evil.invalid")


# ---------------------------------------------------------------------------
# B. 新授权流程：真实独立子进程 worker → 本地 HTTP stub
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    calls: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _StubHandler.calls.append({
            "auth": self.headers.get("Authorization"),
            "model": body.get("model"),
            "url": self.path,
            "messages": body.get("messages"),
        })
        content = json.dumps({"summary": "摘要", "content": "正文" + "x" * 60},
                             ensure_ascii=False)
        if "整合" in json.dumps(body.get("messages") or [], ensure_ascii=False):
            content = json.dumps({"worthy": True,
                                  "ops": [{"action": "create",
                                           "title": "主题8P",
                                           "category": "资料"}]},
                                 ensure_ascii=False)
        resp = json.dumps({"choices": [{"message": {"content": content}}]},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):
        pass


def _admin(app, value):
    from app.core.jwt_utils import get_current_user
    if value is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: value


@pytest.fixture()
def subproc_env(tmp_path, monkeypatch):
    """真实子进程链路环境：允许名单 = stub host；环境变量传给独立子进程。"""
    from app.main import app
    from app.core.skill_evolution import runenv
    _StubHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]
    stub_url = f"http://127.0.0.1:{port}/v1/chat"
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    monkeypatch.setattr(settings, "database_url",
                        f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    monkeypatch.setattr(settings, "wikiskill_console_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_allow_simulated_promotion",
                        False)
    monkeypatch.setattr(settings, "llm_api_url", stub_url)
    monkeypatch.setattr(settings, "llm_api_key", "stub-key-8p")
    monkeypatch.setattr(settings, "llm_model", "stub-model-8p")
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps({"e2e": {"credential_env": "FK_E2E",
                                            "endpoints": [stub_url],
                                            "allow_insecure": True}}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_require_provider_binding", True)
    monkeypatch.setenv("FK_E2E", "stub-key-8p")
    monkeypatch.setattr(settings, "wikiskill_allowed_llm_endpoints",
                        f"127.0.0.1:{port}")
    monkeypatch.setattr(settings, "wikiskill_console_roots",
                        json.dumps({"lab": str(root)}))
    # 环境变量：独立子进程重新读 settings（无 monkeypatch 继承）
    env_saved = {}
    for k, v in {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'b.db').as_posix()}",
        "LLM_API_URL": stub_url,
        "LLM_API_KEY": "stub-key-8p",
        "LLM_MODEL": "stub-model-8p",
        "WIKISKILL_EVOLUTION_ADMIN_REAL_ENABLED": "true",
        "WIKISKILL_ALLOWED_LLM_ENDPOINTS": f"127.0.0.1:{port}",
        "WIKISKILL_CONSOLE_ROOTS": json.dumps({"lab": str(root)}),
        "WIKISKILL_PROMOTION_ENV": "isolated-test",
        "PYTHONIOENCODING": "utf-8",
        "WIKISKILL_CREDENTIAL_PROVIDERS": json.dumps({
            "e2e": {"credential_env": "FK_E2E",
                    "endpoints": [stub_url],
                    "allow_insecure": True}}),
        "WIKISKILL_DEFAULT_PROVIDER": "e2e",
        "WIKISKILL_REVIEWER_PROVIDER": "e2e",
        "FK_E2E": "stub-key-8p",
    }.items():
        env_saved[k] = os.environ.get(k)
        os.environ[k] = v
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        yield {"client": client, "root": root, "server": server,
               "port": port, "run_id": None}
    _admin(app, None)
    for k, v in env_saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    server.shutdown()
    server.server_close()


def _wait_run(root, run_id, timeout=180):
    from app.core.skill_evolution import control
    deadline = time.time() + timeout
    final = None
    while time.time() < deadline:
        view = control.run_state(root, run_id)
        if view["status"] in ("completed", "failed", "cancelled", "paused",
                              "budget_exhausted"):
            final = view
            break
        time.sleep(0.3)
    return final


def test_full_authorized_subprocess_worker_hits_stub(subproc_env):
    """管理员 create → 无网络预览 → 确认指纹 start → 独立子进程 worker → stub。"""
    from app.core.skill_evolution import control
    env = subproc_env
    client = env["client"]
    created = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v2",
        "model_mode": "real", "max_iterations": 1,
        "max_model_calls": 12, "max_tool_calls": 6, "max_seconds": 300,
        "experience": "full"}).json()
    assert created["model_mode"] == "real"
    run_id = created["run_id"]
    # 1) 无网络配置预览（脱敏）
    pv = client.get(
        f"/api/evolution-admin/runs/{run_id}/start-preview",
        params={"root": "lab"})
    assert pv.status_code == 200
    preview = pv.json()
    assert preview["model_mode"] == "real"
    assert preview["config_fingerprint"]
    assert "stub-key-8p" not in json.dumps(preview)
    assert any(r["endpoint_host"] == f"127.0.0.1:{env['port']}"
               for r in preview["roles"])
    # 2) 未确认 → 拒绝（零出站由 409 先行保证）
    r0 = client.post(f"/api/evolution-admin/runs/{run_id}/start",
                     json={"root": "lab"})
    assert r0.status_code == 409
    # 3) 预览后配置漂移 → 拒绝且零出站（不静默冻结）
    import app.config as _cfg_mod
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "llm_api_url",
                        f"http://127.0.0.1:{env['port'] + 1}/evil")
    try:
        r1 = client.post(f"/api/evolution-admin/runs/{run_id}/start", json={
            "root": "lab", "explicit_confirm": True,
            "confirm_config_fingerprint": preview["config_fingerprint"]})
        assert r1.status_code == 409
        detail = r1.json()["detail"]
        assert ("漂移" in detail or "不一致" in detail or "不匹配" in detail
                or "不可用" in detail)
    finally:
        monkeypatch.undo()
    # 4) 恢复预览（settings 已复原）并确认完整指纹 → start
    pv2 = client.get(f"/api/evolution-admin/runs/{run_id}/start-preview",
                     params={"root": "lab"}).json()
    r2 = client.post(f"/api/evolution-admin/runs/{run_id}/start", json={
        "root": "lab", "explicit_confirm": True,
        "confirm_config_fingerprint": pv2["config_fingerprint"],
        "confirm_dataset_version": "wiki-default-v2",
        "confirm_max_iterations": 1,
        "confirm_max_model_calls": 12, "confirm_max_tool_calls": 6,
        "confirm_max_seconds": 300})
    assert r2.status_code == 200, r2.json()
    # 5) 重复 start（并发 CAS）→ 409，绝不双 worker
    r3 = client.post(f"/api/evolution-admin/runs/{run_id}/start", json={
        "root": "lab", "explicit_confirm": True,
        "confirm_config_fingerprint": pv2["config_fingerprint"]})
    assert r3.status_code == 409
    # 6) 独立子进程 worker（真实 Popen，经 cli worker_start_gate）→ stub 请求
    deadline = time.time() + 90
    while time.time() < deadline and not _StubHandler.calls:
        time.sleep(0.2)
    assert _StubHandler.calls, "独立子进程 worker 未命中 HTTP stub"
    first = _StubHandler.calls[0]
    assert first["auth"] == "Bearer stub-key-8p"
    assert first["model"] == "stub-model-8p"
    final = _wait_run(env["root"], run_id, timeout=120)
    assert final is not None, "run 未收敛"
    assert final["status"] in ("paused", "completed", "failed", "cancelled",
                               "budget_exhausted")
    assert control.model_mode_of(env["root"], run_id) == "real"
    assert final["used"]["model_calls"] >= 1
