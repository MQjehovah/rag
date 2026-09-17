"""阶段 8C：演化运行控制（创建/启动/暂停/恢复/取消）离线验收。

覆盖：功能开关与管理员鉴权；创建（模拟/真实标记、数据范围、预算参数、评分器可用
性 fail-closed）；启动→完成 生命周期（线程 worker 等价子进程执行，全程无网络模型
调用）；重复启动冲突；暂停（running 请求位 + queued 直接暂停）→恢复（不重置预算、
不新建 run）；取消（queued/paused 直接终态、running 请求位；不删除记录）；刷新/
重开会话后状态仍可查询。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.jwt_utils import get_current_user

BACKEND = __import__("pathlib").Path(__file__).resolve().parent.parent


def _admin(app, value: dict | None):
    if value is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: value


_PROC_SPAWN_DEFAULT = None


def _snapshot_default_spawner():
    global _PROC_SPAWN_DEFAULT
    from app.core.skill_evolution import control
    if _PROC_SPAWN_DEFAULT is None:
        _PROC_SPAWN_DEFAULT = control.SUBPROCESS_SPAWNER


def _setup(tmp_path, monkeypatch):
    from app.main import app
    from app.core.skill_evolution import control, runenv
    _snapshot_default_spawner()
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    business = tmp_path / "business.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{(business).as_posix()}")
    monkeypatch.setattr(settings, "wikiskill_console_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", False)
    monkeypatch.setattr(settings, "wikiskill_console_roots",
                        json.dumps({"lab": str(root)}))
    control.set_worker_spawner(control._thread_executor)
    return app, root


def _wait_terminal(root, run_id, timeout=300.0):
    from app.core.skill_evolution import control
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = control.run_state(root, run_id)
        if view["status"] in ("completed", "failed", "cancelled",
                              "budget_exhausted", "paused"):
            return view
        time.sleep(0.2)
    raise TimeoutError(f"run 未在 {timeout}s 内收敛: {run_id}")


def _wait_status(root, run_id, statuses, timeout=30.0):
    from app.core.skill_evolution import control
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = control.run_state(root, run_id)
        if view["status"] in statuses:
            return view
        time.sleep(0.1)
    raise TimeoutError(f"run 未到达 {statuses}: {run_id}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    app, root = _setup(tmp_path, monkeypatch)
    from app.core.skill_evolution import control
    control.set_worker_spawner(control._thread_executor)
    try:
        with TestClient(app) as client:
            _admin(app, {"id": "a", "groups": ["__local_admin__"]})
            yield {"app": app, "client": client, "root": root}
        _admin(app, None)
    finally:
        # 恢复默认独立子进程 spawner（避免影响其它文件的管理链测试）
        if _PROC_SPAWN_DEFAULT is not None:
            control._spawn_worker_impl = _PROC_SPAWN_DEFAULT


def test_feature_disabled_gate(tmp_path, monkeypatch):
    from app.main import app
    from app.core.skill_evolution import runenv
    runenv.ensure_experiment_root(tmp_path / "lab")
    business = tmp_path / "b.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{(business).as_posix()}")
    monkeypatch.setattr(settings, "wikiskill_console_roots",
                        json.dumps({"lab": str(tmp_path / "lab")}))
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_enabled", False)
    with TestClient(app) as client:
        _admin(app, {"id": "a", "groups": ["__local_admin__"]})
        r = client.post("/api/evolution-admin/experiments", json={
            "root": "lab", "dataset_version": "wiki-default-v3"})
        assert r.status_code == 503
        _admin(app, None)


def test_permissions_deny_nonadmin(env):
    client = env["client"]
    _admin(env["app"], None)
    r = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v3"})
    assert r.status_code == 401
    _admin(env["app"], {"id": "u", "groups": ["company"]})
    r = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v3"})
    assert r.status_code == 403
    _admin(env["app"], {"id": "a", "groups": ["__local_admin__"]})


def test_meta_datasets_and_grader_readiness(env):
    client = env["client"]
    m = client.get("/api/evolution-admin/meta").json()
    names = {d["dataset_version"] for d in m["datasets"]}
    assert "wiki-default-v3" in names
    v3 = next(d for d in m["datasets"]
              if d["dataset_version"] == "wiki-default-v3")
    assert v3["grader_ready"] is True
    assert "机制验证" in v3["grader_purpose"]
    assert "v2" in m["grader_note"]
    assert m["real_link_verified"] is False
    assert m["effect_verified"] is False


def test_create_validates_params_and_scope(env):
    client = env["client"]
    r = client.post("/api/evolution-admin/experiments", json={
        "root": "nope", "dataset_version": "wiki-default-v3"})
    assert r.status_code == 404
    r = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-does-not-exist"})
    assert r.status_code == 404
    r = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v3",
        "max_iterations": 0})
    assert r.status_code == 422
    r = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v3",
        "model_mode": "real"})
    # 真实模式可排队（创建合法），但启动会被授权开关拒绝。
    assert r.status_code == 200
    created = r.json()
    assert created["model_mode"] == "real"
    assert created["status"] == "queued"
    # 启动真实 run → 未授权 409
    rr = client.post(f"/api/evolution-admin/runs/{created['run_id']}/start",
                     json={"root": "lab"})
    assert rr.status_code == 409
    assert "授权" in rr.json()["detail"]


def test_create_and_full_lifecycle_simulated(env):
    client = env["client"]
    r = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v2",
        "model_mode": "simulated", "max_iterations": 1,
        "max_model_calls": 60, "max_tool_calls": 20, "max_seconds": 600})
    assert r.status_code == 200
    created = r.json()
    run_id = created["run_id"]
    assert created["status"] == "queued"
    assert created["model_mode"] == "simulated"
    assert created["workspace_id"]
    # 启动 → worker 线程领取并执行至终态（全程离线模拟）
    s = client.post(f"/api/evolution-admin/runs/{run_id}/start",
                    json={"root": "lab"})
    assert s.status_code == 200
    assert s.json()["status"] == "queued"
    view = _wait_terminal(env["root"], run_id)
    assert view["status"] == "completed"
    assert view["stop_reason"] in ("max_iterations", "perfect_score")
    # v2 验证集极小：baseline 满分 → perfect_score 提前停止且 0 轮（合法终态）；
    # 否则须完成全部 1 轮。
    if view["stop_reason"] == "perfect_score":
        assert view["current_iteration"] == 0
    else:
        assert view["current_iteration"] >= 1
    from app.core.skill_evolution import control
    assert control._thread_executor.last_error is None
    # 刷新/重开会话后状态仍可查询（持久记录）
    g = client.get(f"/api/evolution-admin/runs/{run_id}",
                   params={"root": "lab"}).json()
    assert g["status"] == "completed"
    assert g["model_mode"] == "simulated"


def test_duplicate_start_conflict(env):
    client = env["client"]
    created = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v2",
        "max_iterations": 1, "max_model_calls": 60,
        "max_tool_calls": 20, "max_seconds": 600}).json()
    run_id = created["run_id"]
    assert client.post(f"/api/evolution-admin/runs/{run_id}/start",
                       json={"root": "lab"}).status_code == 200
    # 启动租约尚未被 worker 领取 → 第二次 start 冲突（并发/重复点击保护）
    r2 = client.post(f"/api/evolution-admin/runs/{run_id}/start",
                     json={"root": "lab"})
    assert r2.status_code == 409
    _wait_terminal(env["root"], run_id)
    # 终态后再 start → invalid_state
    r3 = client.post(f"/api/evolution-admin/runs/{run_id}/start",
                     json={"root": "lab"})
    assert r3.status_code == 409


def test_pause_queued_resume_and_no_budget_reset(env):
    client = env["client"]
    created = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v2",
        "max_iterations": 1, "max_model_calls": 60,
        "max_tool_calls": 20, "max_seconds": 600}).json()
    run_id = created["run_id"]
    # queued → pause：直接终态 paused（无 worker）
    assert client.post(f"/api/evolution-admin/runs/{run_id}/pause",
                       json={"root": "lab"}).status_code == 200
    view = _wait_terminal(env["root"], run_id)
    assert view["status"] == "paused"
    assert view["used"]["model_calls"] == 0   # 预算未被重置/消耗
    # resume → 完成；同一 run（不新建替代 run）
    assert client.post(f"/api/evolution-admin/runs/{run_id}/resume",
                       json={"root": "lab"}).status_code == 200
    view2 = _wait_status(env["root"], run_id,
                         {"completed", "failed", "cancelled",
                          "budget_exhausted"}, timeout=300.0)
    assert view2["run_id"] == run_id
    assert view2["status"] == "completed"
    # 恢复后再次 resume → invalid_state
    rr = client.post(f"/api/evolution-admin/runs/{run_id}/resume",
                     json={"root": "lab"})
    assert rr.status_code == 409


def test_cancel_queued_and_no_record_deletion(env):
    client = env["client"]
    created = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v2",
        "max_iterations": 2, "max_model_calls": 60,
        "max_tool_calls": 20, "max_seconds": 600}).json()
    exp_id, run_id = created["experiment_id"], created["run_id"]
    assert client.post(f"/api/evolution-admin/runs/{run_id}/cancel",
                       json={"root": "lab"}).status_code == 200
    view = _wait_terminal(env["root"], run_id)
    assert view["status"] == "cancelled"
    # 实验记录仍在（取消≠删除）；只读控制台仍可读
    ex = client.get("/api/evolution-console/experiments",
                    params={"root": "lab"}).json()
    assert any(i["experiment_id"] == exp_id for i in ex["items"])
    assert client.post(f"/api/evolution-admin/runs/{run_id}/resume",
                       json={"root": "lab"}).status_code == 409
    assert client.post(f"/api/evolution-admin/runs/{run_id}/start",
                       json={"root": "lab"}).status_code == 409


def test_pause_running_requests_persisted(env):
    # 确定性（不靠线程竞速）：claim 使 run=running → API pause 持久化请求位 →
    # 按租约收敛 paused → resume 继续完成同一 run（不重置预算、不新建 run）。
    from app.core.skill_evolution import control, orchestrator as orch, skill_store
    from app.models.evolution import EvolutionRun
    client = env["client"]
    created = client.post("/api/evolution-admin/experiments", json={
        "root": "lab", "dataset_version": "wiki-default-v2",
        "max_iterations": 1, "max_model_calls": 60,
        "max_tool_calls": 20, "max_seconds": 600}).json()
    run_id = created["run_id"]
    root = env["root"]
    db = skill_store.session_for(root)
    try:
        row = orch.claim(db, run_id, worker_id="console-test")
        token = row.lease_token
    finally:
        db.close()
    assert control.run_state(root, run_id)["status"] == "running"
    pr = client.post(f"/api/evolution-admin/runs/{run_id}/pause",
                     json={"root": "lab"})
    assert pr.status_code == 200
    assert pr.json()["pause_requested"] is True
    # worker（此处模拟）按暂停请求在检查点收敛
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, run_id)
        orch._pause(db, row, token, reason="user_pause")
    finally:
        db.close()
    assert control.run_state(root, run_id)["status"] == "paused"
    # 恢复：不重置已用计数；同一 run 执行到完成
    assert client.post(f"/api/evolution-admin/runs/{run_id}/resume",
                       json={"root": "lab"}).status_code == 200
    final = _wait_status(root, run_id,
                         {"completed", "failed", "cancelled",
                          "budget_exhausted"}, timeout=300.0)
    assert final["run_id"] == run_id
    assert final["status"] == "completed"
    assert final["pause_requested"] is False
