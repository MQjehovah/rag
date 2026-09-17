"""M3 后端最小闭环反例（start 预览→确认→受控执行 / worker 启动资格门）。

覆盖：
- start_preview 无网络（simulated：无角色/指纹；real：roles 仅 host+布尔引用，
  不泄漏密钥值；不发探测请求）；
- 预览后配置漂移 → start 拒绝（frozen_mismatch）且不静默冻结新配置；
- 确认指纹一致 → 冻结持久化并 spawn；
- worker_start_gate：real 未冻结/未授权拒绝；simulated 独立显式、不升级 real；
- CLI evolution-run 缺授权在首个网络请求前拒绝（子进程 rc!=0 + code）。
全部本地 stub/虚构凭据；不调用真实模型、不操作生产。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import settings


BACKEND = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND / "eval" / "wiki_evolution"


def _new_lab(tmp_path, monkeypatch, *, real: bool = False):
    """受控创建实验 + run（create 不触网络；real 仅排队）。"""
    monkeypatch.setattr(settings, "wikiskill_promotion_env", "isolated-test")
    monkeypatch.setattr(settings,
                        "wikiskill_evolution_allow_simulated_promotion", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    root = tmp_path / "lab"
    root.mkdir(parents=True, exist_ok=True)
    if real:
        # 虚构凭据 + 本地 stub 端点（仅配置值，不发送）
        monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled",
                            True)
        monkeypatch.setattr(settings, "llm_api_url",
                            "http://127.0.0.1:9/v1/chat/completions")
        monkeypatch.setattr(settings, "llm_api_key", "fk-stub-secret")
        monkeypatch.setattr(settings, "llm_model", "stub-model/v1")
        monkeypatch.setattr(settings, "wikiskill_require_provider_binding", True)
        monkeypatch.setattr(settings, "wikiskill_credential_providers",
                            json.dumps({"e2e": {
                                "credential_env": "FK_E2E",
                                "endpoints": ["http://127.0.0.1:9/v1/chat/completions"],
                                "allow_insecure": True}}))
        monkeypatch.setattr(settings, "wikiskill_default_provider", "e2e")
        monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "e2e")
        monkeypatch.setenv("FK_E2E", "fk-stub-secret")
    from app.core.skill_evolution import control
    created = control.create_experiment_and_run(
        root, dataset_version="wiki-default-v3", init_mode="business",
        max_iterations=2,
        budget={"max_model_calls": 10, "max_tool_calls": 4,
                "max_seconds": 120},
        model_mode="real" if real else "simulated", experience="full")
    return root, created["run_id"]


def _config_of(root, run_id) -> dict:
    from app.core.skill_evolution import skill_store
    db = skill_store.session_for(root)
    try:
        row = db.execute(text(
            "SELECT config_json FROM evolution_runs WHERE run_id=:r"),
            {"r": run_id}).fetchone()
        return json.loads(row.config_json)
    finally:
        db.close()


def _store_config(root, run_id, cfg: dict) -> None:
    from app.core.skill_evolution import skill_store
    db = skill_store.session_for(root)
    try:
        db.execute(text(
            "UPDATE evolution_runs SET config_json=:j WHERE run_id=:r"),
            {"j": json.dumps(cfg, ensure_ascii=False, sort_keys=True),
             "r": run_id})
        db.commit()
    finally:
        db.close()


def test_start_preview_simulated_no_network(tmp_path, monkeypatch):
    from app.core.skill_evolution import control
    root, run_id = _new_lab(tmp_path, monkeypatch, real=False)
    view = control.start_preview(root, run_id)
    assert view["model_mode"] == "simulated"
    assert view["roles"] == [] and view["config_fingerprint"] is None
    assert "simulated" in view["preview_note"]
    blob = json.dumps(view, ensure_ascii=False)
    assert "api_key" not in blob
    # worker 门：simulated 显式通过且不升级 real
    gate = control.worker_start_gate(root, run_id)
    assert gate["model_mode"] == "simulated" and gate["authorized"] is True
    assert gate.get("frozen") is False


def test_start_preview_real_no_secret_and_prefreeze_conflict(tmp_path,
                                                             monkeypatch):
    from app.core.skill_evolution import control
    root, run_id = _new_lab(tmp_path, monkeypatch, real=True)
    view = control.start_preview(root, run_id)
    assert view["model_mode"] == "real"
    assert view["config_fingerprint"]
    assert any(r["role"] == "executor" for r in view["roles"])
    blob = json.dumps(view, ensure_ascii=False)
    assert "fk-stub-secret" not in blob          # 密钥不展示
    assert "api_key_present" in blob             # 仅布尔受控引用
    # 预览后配置漂移 → start 拒绝且不静默冻结新配置
    monkeypatch.setattr(settings, "llm_model", "drifted-model/v2")
    with pytest.raises(control.ControlError) as ei:
        control.start(root, run_id, actor="t",
                      confirm={"explicit": True,
                               "config_fingerprint": view["config_fingerprint"]})
    assert ei.value.code == "frozen_mismatch"
    cfg = _config_of(root, run_id)
    assert not (cfg.get("runner") or {}).get("frozen")   # 未冻结新配置


def test_start_with_matching_preview_fingerprint_freeze_and_spawn(
        tmp_path, monkeypatch):
    from app.core.skill_evolution import control
    root, run_id = _new_lab(tmp_path, monkeypatch, real=True)
    spawned = {}

    def _spawn(root_, run_, worker_):
        spawned["ok"] = True
        return {"pid": 1234, "worker": worker_}

    monkeypatch.setattr(control, "_spawn_worker", _spawn)
    view = control.start_preview(root, run_id)
    out = control.start(root, run_id, actor="t",
                        confirm={"explicit": True,
                                 "config_fingerprint":
                                     view["config_fingerprint"]})
    assert out["model_mode"] == "real"
    assert spawned.get("ok") is True
    cfg = _config_of(root, run_id)
    frozen = (cfg.get("runner") or {}).get("frozen")
    assert frozen and frozen.get("fingerprint") == view["config_fingerprint"]
    # worker 门通过（持久化启动授权）
    gate = control.worker_start_gate(root, run_id)
    assert gate["model_mode"] == "real" and gate["frozen"] is True


def test_worker_gate_real_without_freeze_refused(tmp_path, monkeypatch):
    from app.core.skill_evolution import control
    root, run_id = _new_lab(tmp_path, monkeypatch, real=True)
    cfg = _config_of(root, run_id)
    runner = cfg.setdefault("runner", {})
    runner["frozen"] = None if False else None  # 保持未冻结
    with pytest.raises(control.ControlError) as ei:
        control.worker_start_gate(root, run_id)
    assert ei.value.code == "frozen_missing"


def test_cli_evolution_run_refused_before_network(tmp_path, monkeypatch):
    """缺启动授权直接 CLI 调用：在首个 HTTP 请求前拒绝（子进程，rc!=0）。"""
    root, run_id = _new_lab(tmp_path, monkeypatch, real=True)
    cfg = _config_of(root, run_id)
    ds_dir = (cfg.get("runner") or {}).get("dataset_dir") or \
        str(ROOT_DIR / "wiki-default-v3")
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled",
                        False)  # 子进程继承 false（生产默认）
    env = {k: v for k, v in __import__("os").environ.items()}
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "app.core.skill_evolution.cli",
         "evolution-run", "--root", str(root), "--dataset", ds_dir,
         "--run", run_id, "--worker", "t-cli"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(BACKEND), env=env, timeout=120)
    assert proc.returncode != 0
    assert "real_not_authorized" in proc.stdout + proc.stderr
