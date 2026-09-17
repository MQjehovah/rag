"""阶段核查 1：管理入口 → worker → orchestrator → 真实适配器完整链路（HTTP stub）。

验证：管理员创建 real run → start（显式确认 + 授权）→ worker（build_run_actors）
用真实适配器把请求发给本地 HTTP stub（不是模拟实现）；stub 收到带凭据的 chat 请求
即证明管理入口已接通真实链路；若 worker 意外落到模拟实现，测试会因模拟入口被触发
而失败（哨兵抛错）。全程不接触真实供应商；run 通过 cancel 收敛，不做效果断言。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.jwt_utils import get_current_user


class _StubHandler(BaseHTTPRequestHandler):
    calls: list = []
    fail = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _StubHandler.calls.append({
            "auth": self.headers.get("Authorization"),
            "model": body.get("model"),
            "messages": body.get("messages"),
        })
        content = json.dumps({"summary": "摘要", "content": "正文" + "x" * 60},
                             ensure_ascii=False)
        if "整合" in json.dumps(body.get("messages") or [], ensure_ascii=False):
            content = json.dumps({"worthy": True,
                                  "ops": [{"action": "create",
                                           "title": "主题8E",
                                           "category": "资料"}]},
                                 ensure_ascii=False)
        resp = json.dumps({"choices": [{"message": {"content": content}}]},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):  # quiet
        pass


def _admin(app, value):
    if value is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: value


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app.main import app
    from app.core.skill_evolution import control, runenv
    _StubHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    business = tmp_path / "business.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{(business).as_posix()}")
    monkeypatch.setattr(settings, "wikiskill_console_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_allow_simulated_promotion",
                        False)
    monkeypatch.setattr(settings, "llm_api_url", f"http://127.0.0.1:{port}/v1/chat")
    monkeypatch.setattr(settings, "llm_api_key", "stub-key")

            # 正式 real provider 门禁：受控虚构 provider（键经非秘密 env 引用）
    _endpoints = [str(settings.llm_api_url or "")]
    if getattr(settings, "wikiskill_reviewer_api_url", None):
        _endpoints.append(str(settings.wikiskill_reviewer_api_url))
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        json.dumps({"e2e": {"credential_env": "FK_E2E",
                                            "endpoints": _endpoints,
                                            "allow_insecure": True}}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "e2e")
    monkeypatch.setattr(settings, "wikiskill_require_provider_binding", True)
    monkeypatch.setenv("FK_E2E", "stub-key")
    monkeypatch.setattr(settings, "llm_model", "stub-model")
    monkeypatch.setattr(settings, "wikiskill_console_roots",
                        json.dumps({"lab": str(root)}))
    control.set_worker_spawner(control._thread_executor)
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        yield {"app": app, "client": client, "root": root, "server": server}
    _admin(app, None)
    server.shutdown()
    server.server_close()


def test_admin_real_run_chain_hits_http_stub(env, monkeypatch):
    # 哨兵：真实模式下任何模拟实现被实例化 → 立即抛错（证明未走模拟路径）。
    from app.core.skill_evolution import runner as _runmod
    def _boom(self, *a, **k):
        raise AssertionError("simulated executor must not run in real mode")
    monkeypatch.setattr(_runmod.SimulatedModel, "__init__", _boom)
    client = env["client"]
    created = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v2",
        "model_mode": "real", "max_iterations": 1,
        "max_model_calls": 12, "max_tool_calls": 6, "max_seconds": 300,
        "experience": "full"}).json()
    assert created["model_mode"] == "real"
    run_id = created["run_id"]
    # 1) 缺少显式确认 → 拒绝
    r0 = client.post(f"/api/evolution-admin/runs/{run_id}/start",
                     json={"root": "lab"})
    assert r0.status_code == 409
    assert "显式确认" in r0.json()["detail"]
    # 2) 确认参数与记录不一致 → 拒绝（数据范围/预算必须绑定运行记录）
    r1 = client.post(f"/api/evolution-admin/runs/{run_id}/start", json={
        "root": "lab", "explicit_confirm": True,
        "confirm_dataset_version": "wiki-default-v5",
        "confirm_max_iterations": 1, "confirm_max_model_calls": 12,
        "confirm_max_tool_calls": 6, "confirm_max_seconds": 300})
    assert r1.status_code == 409
    assert "不一致" in r1.json()["detail"]
    # 3) 正确确认 → 启动（worker 真实适配器 → HTTP stub）
    r2 = client.post(f"/api/evolution-admin/runs/{run_id}/start", json={
        "root": "lab", "explicit_confirm": True,
        "confirm_dataset_version": "wiki-default-v2",
        "confirm_max_iterations": 1, "confirm_max_model_calls": 12,
        "confirm_max_tool_calls": 6, "confirm_max_seconds": 300})
    assert r2.status_code == 200
    # 4) worker 必须构造 real 适配器（绝不落到模拟）
    from app.core.skill_evolution import control
    actors, mode = control.build_run_actors(env["root"], run_id)
    assert mode == "real"
    assert actors.executor_runner is not None
    assert actors.maintainer_runner is not None
    assert actors.proposer_factory is not None
    # 5) 等待 stub 收到真实请求（模拟实现被调用即判失败：哨兵见下）
    deadline = time.time() + 60
    while time.time() < deadline and not _StubHandler.calls:
        time.sleep(0.2)
    assert _StubHandler.calls, "真实链路未命中 HTTP stub（可能误用模拟实现）"
    first = _StubHandler.calls[0]
    assert first["auth"] == "Bearer stub-key"
    assert first["model"] == "stub-model"
    assert isinstance(first["messages"], list)
    # 收敛：stub 不满足 maintainer/proposer 协议时 worker 会在有限重试后 pause
    # （maintain_failed）——这本身证明完整编排在真实适配器上推进且未回退模拟。
    deadline = time.time() + 90
    final = None
    while time.time() < deadline:
        view = control.run_state(env["root"], run_id)
        if view["status"] in ("completed", "failed", "cancelled", "paused",
                              "budget_exhausted"):
            final = view
            break
        time.sleep(0.3)
    assert final is not None, "真实链路 run 未收敛（worker 卡死或未领取）"
    assert final["status"] in ("paused", "completed", "failed", "cancelled",
                               "budget_exhausted")
    # 模型模式绑定 run 记录（非由运行结果推导）
    assert control.model_mode_of(env["root"], run_id) == "real"
    assert final["used"]["model_calls"] >= 1
