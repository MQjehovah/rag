"""阶段 8A：演化实验只读控制台后端验收（离线；不触发模型/迁移/写库）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.jwt_utils import get_current_user
from app.core.skill_evolution import gating as gate, orchestrator as orch
from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.trace_sampling import group_workspace_id
from app.models.evolution import ITER_DONE, ITER_STEP_DONE, RUN_COMPLETED

BACKEND = Path(__file__).resolve().parent.parent
DS_V3 = BACKEND / "eval/wiki_evolution/datasets/wiki-default-v3"
REPO_SEED = BACKEND / "eval/wiki_evolution/skills/seed-default-v1"


@pytest.fixture(scope="module")
def app_and_env(tmp_path_factory):
    from app.main import app
    base = tmp_path_factory.mktemp("console")
    business = base / "business.db"
    root = runenv.ensure_experiment_root(base / "lab")
    empty = runenv.ensure_experiment_root(base / "empty-root")
    settings.database_url = f"sqlite:///{(business).as_posix()}"
    settings.wikiskill_console_enabled = True
    settings.wikiskill_console_roots = json.dumps(
        {"lab": str(root), "empty": str(empty)})
    db = skill_store.session_for(root)
    try:
        seed = skill_store.import_seed(db, skill_store.load_package(
            REPO_SEED, source_type="builtin_seed"))
        v = skill_store.get_version(db, seed)
        ds = load_dataset(DS_V3)
        ws = group_workspace_id(
            [t.group_id for t in ds.tasks if t.split == "train"][0])
        exp = gate.create_experiment(
            db, workspace_id=ws, domain="wiki_compile.default", dataset=ds,
            grader_version=ds.grader_version,
            runner_config={"profile": "faithful"}, pipeline_key="wiki.default",
            pipeline_version="3", runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=[t.task_id for t in ds.tasks if t.split == "val"],
            initial_members=[{"skill_id": v.skill_id,
                              "version_id": v.version_id,
                              "content_hash": v.content_hash, "seq": v.seq}])
        run = orch.create_run(
            db, experiment_id=exp.experiment_id, workspace_id=ws,
            domain="wiki_compile.default", dataset=ds,
            init_mode="business", max_iterations=3,
            budget={"max_model_calls": 90, "max_tool_calls": 40,
                    "max_seconds": 3600},
            runner_config={"profile": "faithful", "real": True,
                           "mode": "real"},
            train_task_ids=[t.task_id for t in ds.tasks if t.split == "train"],
            experience="full")
        row = db.get(__import__("app.models.evolution",
                                fromlist=["EvolutionRun"]).EvolutionRun,
                     run.run_id)
        row.status = RUN_COMPLETED
        row.stop_reason = "max_iterations"
        row.current_iteration = 3
        row.used_model_calls = 12
        row.used_tool_calls = 4
        db.commit()
        it = orch._new_iteration(db, row, 1)
        it.status = ITER_DONE
        it.step = ITER_STEP_DONE
        it.train_execution_ids_json = json.dumps(["exec-tra-001"])
        it.no_action = True
        db.commit()
        exp_id = str(exp.experiment_id)
        run_id = str(run.run_id)
    finally:
        db.close()
    runs_dir = root / "runs" / "exec-tra-001"
    runs_dir.mkdir(parents=True)
    (runs_dir / "meta.json").write_text(json.dumps({
        "execution_id": "exec-tra-001", "dataset_version": "wiki-default-v3",
        "domain": "wiki_compile.default", "split": "train",
        "group_id": "g-train", "run_status": "succeeded",
        "outcome": {"failure_kind": None},
        "candidate": {"revision_id": "rev-x"},
        "created_at": "2026-09-07T00:00:00"}, ensure_ascii=False),
        encoding="utf-8")
    return {"app": app, "root": root, "empty": empty,
            "exp": exp_id, "run": run_id}


@pytest.fixture(scope="module")
def client(app_and_env):
    app = app_and_env["app"]
    with TestClient(app) as c:
        yield c


def _as_admin(app, value: dict | None):
    if value is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: value


def test_unauth_and_nonadmin_rejected(app_and_env, client):
    app = app_and_env["app"]
    _as_admin(app, None)
    r = client.get("/api/evolution-console/experiments", params={"root": "lab"})
    assert r.status_code in (401, 403)
    _as_admin(app, {"id": "u1", "username": "u", "groups": ["company"]})
    r = client.get("/api/evolution-console/status")
    assert r.status_code == 403
    _as_admin(app, None)


def test_disabled_state_and_missing_db_no_side_effects(app_and_env, client,
                                                       monkeypatch):
    app = app_and_env["app"]
    _as_admin(app, {"id": "a", "groups": ["__local_admin__"]})
    monkeypatch.setattr(settings, "wikiskill_console_enabled", False)
    assert client.get("/api/evolution-console/status").json()["console"] == "disabled"
    monkeypatch.setattr(settings, "wikiskill_console_enabled", True)
    empty = app_and_env["empty"]
    r = client.get("/api/evolution-console/experiments", params={"root": "empty"})
    assert r.status_code == 503
    assert not (empty / "skill_store.db").exists()
    assert not (empty / "experiment.db").exists()


def test_read_only_real_records_and_unknown_usage(app_and_env, client):
    app = app_and_env["app"]
    _as_admin(app, {"id": "a", "groups": ["__local_admin__"]})
    exp_id, run_id = app_and_env["exp"], app_and_env["run"]
    body = client.get("/api/evolution-console/experiments",
                      params={"root": "lab"}).json()
    assert body["total"] >= 1
    assert any(x["experiment_id"] == exp_id for x in body["items"])
    detail = client.get(f"/api/evolution-console/experiments/{exp_id}",
                        params={"root": "lab"}).json()
    assert detail["experiment"]["dataset_version"] == "wiki-default-v3"
    assert detail["runs_summary"][0]["run_id"] == run_id
    rd = client.get(f"/api/evolution-console/runs/{run_id}",
                    params={"root": "lab"}).json()
    assert rd["run"]["status"] == "completed"
    assert rd["run"]["stop_reason"] == "max_iterations"
    assert rd["run"]["used_model_calls"] == 12
    b = rd["budget"]
    assert b["model_cap"] == 90 and b["cost"] is None
    assert b["token_usage"] is None and b["usage_unknown"] is True
    assert b["model_mode"] == "real"
    sk = client.get(f"/api/evolution-console/experiments/{exp_id}/skills",
                    params={"root": "lab"}).json()
    assert sk["versions"] and sk["versions"][0]["content_immutable"] is True
    for ep in ("gate-history", "evaluations"):
        x = client.get(f"/api/evolution-console/experiments/{exp_id}/{ep}",
                       params={"root": "lab"}).json()
        assert isinstance(x["items"], list)
    tr = client.get(f"/api/evolution-console/runs/{run_id}/trajectories",
                    params={"root": "lab"}).json()
    assert tr["total"] >= 1
    assert tr["items"][0]["execution_id"] == "exec-tra-001"


def test_scope_and_path_boundaries_no_model_no_mutation(app_and_env, client):
    app = app_and_env["app"]
    _as_admin(app, {"id": "a", "groups": ["__local_admin__"]})
    for bad_root in ("nope", "../etc"):
        assert client.get("/api/evolution-console/experiments",
                          params={"root": bad_root}).status_code == 404
    assert client.get("/api/evolution-console/experiments/exp-missing",
                      params={"root": "lab"}).status_code == 404
    assert client.get("/api/evolution-console/experiments/..%2F..%2Fx",
                      params={"root": "lab"}).status_code == 404
    db_file = app_and_env["root"] / "skill_store.db"
    before = hashlib.sha256(db_file.read_bytes()).hexdigest()
    for path in (f"/api/evolution-console/runs/{app_and_env['run']}",
                 f"/api/evolution-console/experiments/{app_and_env['exp']}"):
        assert client.get(path, params={"root": "lab"}).status_code == 200
    assert hashlib.sha256(db_file.read_bytes()).hexdigest() == before
    _as_admin(app, None)


def test_whitelist_sanitize_symlink_and_diff_scope(app_and_env, client):
    import sqlite3
    import urllib.parse as _up
    app = app_and_env["app"]
    _as_admin(app, {"id": "a", "groups": ["__local_admin__"]})
    root = app_and_env["root"]
    exp_id, run_id = app_and_env["exp"], app_and_env["run"]
    # 注入带“密钥/正文/脚本”的集合与 meta 内容（只读接口应透出白名单）
    db_file = root / "skill_store.db"
    uri = "file:" + _up.quote(db_file.resolve().as_posix())
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("UPDATE evolution_experiments SET current_skill_set_json=? "
                     "WHERE experiment_id=?",
                     ('{"members": [{"skill_id": "default", "version_id": "default:0001",'
                      ' "content_hash": "h1", "seq": 1, "secret": "sk-secret-x",'
                      ' "skill_md": "<script>alert(1)</script>正文全文", "leak": true}]}',
                      exp_id))
        conn.execute("UPDATE evolution_iterations SET train_execution_ids_json=? "
                     "WHERE run_id=?",
                     ('["exec-tra-001", "exec-escape"]', run_id))
        conn.commit()
    finally:
        conn.close()
    # 根外目录 + 符号链接（Windows 可能无权限创建链接 → 跳过仍须安全）
    outside = root.parent / "outside-evil"
    outside.mkdir(exist_ok=True)
    (outside / "meta.json").write_text(json.dumps({
        "execution_id": "exec-escape", "run_status": "succeeded",
        "secret": "sk-outside", "content": "<img onerror=alert(2)>",
        "candidate": {"revision_id": "r"}}), encoding="utf-8")
    runs_dir = root / "runs"
    try:
        (runs_dir / "exec-escape").symlink_to(outside, target_is_directory=True)
        link_created = True
    except OSError:
        link_created = False
    d = client.get(f"/api/evolution-console/experiments/{exp_id}",
                   params={"root": "lab"}).json()
    raw = client.get(f"/api/evolution-console/experiments/{exp_id}",
                     params={"root": "lab"}).text
    assert "sk-secret-x" not in raw and "alert(1)" not in raw and "正文全文" not in raw
    members = d["current_skill_set"]["members"]
    assert members and all(set(m) <= {"skill_id", "version_id", "content_hash", "seq"}
                           for m in members)
    sk = client.get(f"/api/evolution-console/experiments/{exp_id}/skills",
                    params={"root": "lab"}).json()
    assert sk["content_diff_supported"] is True
    assert "diff" in sk["note"]
    tr = client.get(f"/api/evolution-console/runs/{run_id}/trajectories",
                    params={"root": "lab"}).json()
    allowed_keys = {"execution_id", "dataset_version", "domain", "split",
                    "group_id", "run_status", "failure_kind", "published",
                    "created_at"}
    assert all(set(it) <= allowed_keys for it in tr["items"])
    ids = [it["execution_id"] for it in tr["items"]]
    assert "exec-tra-001" in ids
    assert "exec-escape" not in ids          # 符号链接/根外目录被拒绝
    assert "sk-outside" not in tr
    if link_created:
        (runs_dir / "exec-escape").unlink()  # 清理链接
    _as_admin(app, None)
