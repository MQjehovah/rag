"""B4 反例测试：冻结后 settings 漂移不生效、确认指纹、评审输出上限、旧记录拒绝。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.jwt_utils import get_current_user

BACKEND = Path(__file__).resolve().parent.parent
DEV_DS = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v2dev"


class _H(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _H.calls.append(body)
        out = json.dumps({"choices": [{"message": {"content": "{}"}}]},
                         ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def _admin(app, v):
    if v is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: v


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app.main import app
    from app.core.skill_evolution import runenv
    _H.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/chat"
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    biz = tmp_path / "b.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{(biz).as_posix()}")
    monkeypatch.setattr(settings, "wikiskill_console_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_console_roots",
                        json.dumps({"lab": str(root)}))
    monkeypatch.setattr(settings, "llm_api_url", url)
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
    monkeypatch.setattr(settings, "llm_model", "executor-frozen")
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-frozen")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", url)
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version", "pv1")
    monkeypatch.setattr(settings, "wikiskill_reviewer_timeout", 30.0)
    monkeypatch.setattr(settings, "wikiskill_reviewer_retries", 1)
    monkeypatch.setattr(settings, "wikiskill_reviewer_max_output_tokens", 512)
    for k, v in {"LLM_API_URL": url, "LLM_API_KEY": "stub-key",
                 "LLM_MODEL": "executor-frozen",
                 "WIKISKILL_REVIEWER_MODEL_ID": "reviewer-frozen",
                 "WIKISKILL_REVIEWER_API_URL": url,
                 "WIKISKILL_REVIEWER_PROMPT_VERSION": "pv1",
                 "WIKISKILL_REVIEWER_MAX_OUTPUT_TOKENS": "512",
                 "WIKISKILL_EVOLUTION_ADMIN_REAL_ENABLED": "true"}.items():
        monkeypatch.setenv(k, v)
    with TestClient(app) as c:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        yield {"app": app, "client": c, "root": root, "url": url}
    _admin(app, None)
    srv.shutdown()
    srv.server_close()


def _create_real(env, **kw):
    body = {"root": "lab", "dataset_version": "wiki-default-v2dev",
            "model_mode": "real", "review": "v2", "max_iterations": 1,
            "max_model_calls": 60, "max_tool_calls": 30, "max_seconds": 600}
    body.update(kw)
    return env["client"].post("/api/evolution-admin/experiments", json=body).json()


def _confirm(run_id, env):
    g = env["client"].get(f"/api/evolution-admin/runs/{run_id}",
                          params={"root": "lab"}).json()
    return {"root": "lab", "explicit_confirm": True,
            "confirm_dataset_version": "wiki-default-v2dev",
            "confirm_max_iterations": 1, "confirm_max_model_calls": 60,
            "confirm_max_tool_calls": 30, "confirm_max_seconds": 600,
            "confirm_config_fingerprint": g.get("config_fingerprint"),
            "confirm_reviewer_fingerprint": g.get("reviewer_fingerprint")}


def test_frozen_config_immune_to_settings_drift(env, monkeypatch):
    from app.core.skill_evolution import control
    from app.core.skill_evolution.config_freeze import build_actor_runner_frozen
    from app.core.skill_evolution.model_review import ReviewerConfig, create_reviewer
    created = _create_real(env)
    run_id = created["run_id"]
    fr = control.freeze_run_runtime_config(env["root"], run_id)
    assert fr["frozen"] is True and fr["config_fingerprint"]
    assert fr["reviewer_fingerprint"]
    # settings 漂移（模型/评审模型都换掉）
    monkeypatch.setattr(settings, "llm_model", "executor-EVIL")
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "reviewer-EVIL")
    cfg = control.runtime_fingerprints_of(env["root"], run_id)
    assert cfg["config_fingerprint"] == fr["config_fingerprint"]
    # 从冻结构造的角色 runner/reviewer 仍指向冻结模型并透传输出上限
    frozen = None
    from app.core.skill_evolution import skill_store
    db = skill_store.session_for(env["root"])
    from sqlalchemy import text as _t
    row = db.execute(_t("SELECT config_json FROM evolution_runs "
                        "WHERE run_id=:r"), {"r": run_id}).fetchone()
    frozen = json.loads(row.config_json)["runner"]["frozen"]
    db.close()
    runner = build_actor_runner_frozen("executor", frozen)
    runner([{"role": "user", "content": "synthesis"}], context="wiki-synthesis",
           timeout=5)
    assert _H.calls and _H.calls[-1]["model"] == "executor-frozen"
    rv = frozen["frozen_reviewer"]
    cfg_r = ReviewerConfig(mode="real", model_id=rv["model_id"],
                           prompt_version=rv["prompt_version"],
                           api_url=rv["api_url"], api_key_present=True,
                           timeout=30, retries=1,
                           max_output_tokens=rv.get("max_output_tokens"))
    rev = create_reviewer(cfg_r)
    rev.attach_sender(rev._send_http)
    try:
        rev.review(task_id="x", sources=["s"],
                   candidate_output={"sections": []},
                   checks=[{"id": "a", "check": "b"}])
    except Exception:
        pass  # stub 返回空对象 → invalid；重点验证载荷
    sent = _H.calls[-1]
    assert sent["model"] == "reviewer-frozen"
    assert sent.get("max_tokens") == 512
    assert "EVIL" not in json.dumps(sent, ensure_ascii=False)


def test_resume_fingerprint_confirm_and_legacy_reject(env, monkeypatch):
    from app.core.skill_evolution import control
    created = _create_real(env)
    run_id = created["run_id"]
    # 首次启动：确认缺省指纹允许（记录同时被冻结）
    r = env["client"].post(
        f"/api/evolution-admin/runs/{run_id}/start",
        json={"root": "lab", "explicit_confirm": True,
              "confirm_dataset_version": "wiki-default-v2dev",
              "confirm_max_iterations": 1, "confirm_max_model_calls": 60,
              "confirm_max_tool_calls": 30, "confirm_max_seconds": 600})
    assert r.status_code == 200, r.text
    fr = control.runtime_fingerprints_of(env["root"], run_id)
    assert fr["frozen"] is True
    # 错误指纹 → 拒绝（确认漂移）
    bad = _confirm(run_id, env)
    bad["confirm_config_fingerprint"] = "x" * 64
    r2 = env["client"].post(f"/api/evolution-admin/runs/{run_id}/start",
                            json=bad)
    assert r2.status_code == 409 and "指纹" in r2.json()["detail"]


def test_frozen_builders_missing_legacy_fails_closed(env):
    """旧记录无冻结字段 → 构造器明确失败（不以当前默认值补齐）。"""
    from app.core.skill_evolution.config_freeze import (
        FrozenConfigError, build_actor_runner_frozen)
    with pytest.raises(FrozenConfigError):
        build_actor_runner_frozen("executor", {})
    with pytest.raises(FrozenConfigError):
        build_actor_runner_frozen("proposer", {"roles": {"executor": {}}})
