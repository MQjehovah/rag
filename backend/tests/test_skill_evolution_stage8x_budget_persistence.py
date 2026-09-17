"""M3：整次 run 累计时间预算与全局工具预算持久化（离线；可注入时钟）。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.skill_evolution import orchestrator as orch
from app.core.skill_evolution import proposer as prop
from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.orchestrator import (
    BudgetExceeded,
    BudgetGuard,
    _start_active_segment,
    _stop_active_segment,
)
from app.core.skill_evolution.trace import TraceStore
from app.models.evolution import RUN_RUNNING, EvolutionRun

BACKEND = Path(__file__).resolve().parent.parent
P53 = "4f83c9e2a1d7"
P54 = "5a94d0e3b2c8"
BUDGET = {"max_model_calls": 20, "max_tool_calls": 2, "max_seconds": 10}


class FakeClock:
    def __init__(self, t=None):
        self.t = t or datetime(2026, 9, 1, 12, 0, 0)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def _new_run(root, run_id="run-b", budget=None, lease_s=600, tools=2, seconds=10):
    db = skill_store.session_for(root)
    try:
        b = dict(budget or {
            "max_model_calls": 20, "max_tool_calls": tools, "max_seconds": seconds})
        row = EvolutionRun(
            run_id=run_id, experiment_id="exp-b", workspace_id="ws_x",
            domain="wiki_compile.default", dataset_version="d",
            init_mode="paper",
            config_json=json.dumps({"budget": b, "train_task_ids": [],
                                    "runner": {}, "init_mode": "paper"}),
            initial_skill_set_json="{}", max_iterations=3,
            current_iteration=0, status=RUN_RUNNING,
            used_model_calls=0, used_tool_calls=0, used_estimated_chars=0,
            pause_requested=False, cancel_requested=False,
            lease_token="tok-1", lease_owner="w1",
            lease_expires_at=datetime(2026, 9, 1, 13, 0, 0))
        db.add(row)
        db.commit()
        return row.run_id
    finally:
        db.close()


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(orch, "CLOCK", c)
    return c


def test_active_seconds_pause_resume_and_no_extra_time(tmp_path, clock):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, seconds=10)
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-b")
        g = BudgetGuard(db, row)
        clock.advance(4)
        assert abs(g.active_elapsed() - 4.0) < 1e-6
        remaining = g.seconds_remaining()
        assert abs(remaining - 6.0) < 1e-6
        _stop_active_segment(row)
        db.commit()
        clock.advance(100)  # 暂停等待不计入
        assert abs(g.active_elapsed() - 4.0) < 1e-6
        assert abs(g.seconds_remaining() - 6.0) < 1e-6
        _start_active_segment(row)
        db.commit()
        clock.advance(3)
        assert abs(g.active_elapsed() - 7.0) < 1e-6
        assert abs(g.seconds_remaining() - 3.0) < 1e-6
        # 再次 resume 不得获得额外时间
        _stop_active_segment(row)
        db.commit()
        g2 = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        assert abs(g2.active_elapsed() - 7.0) < 1e-6
        assert abs(g2.seconds_remaining() - 3.0) < 1e-6
    finally:
        db.close()


def test_resume_when_time_budget_full_sends_nothing(tmp_path, clock):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, seconds=5)
    db = skill_store.session_for(root)
    sent = []
    try:
        row = db.get(EvolutionRun, "run-b")
        g = BudgetGuard(db, row)
        clock.advance(5)
        _stop_active_segment(row)
        db.commit()
        g2 = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        assert g2.seconds_remaining() <= 0
        with pytest.raises(BudgetExceeded):
            g2.reserve_model()
        wrapped = g2.wrap(lambda messages, context="", timeout=120.0: sent.append(1) or {"ok": 1})
        with pytest.raises(BudgetExceeded):
            wrapped([])
        assert sent == []
    finally:
        db.close()


def test_global_tool_budget_blocks_second_call_and_counts_failures(tmp_path, clock):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, tools=1)
    db = skill_store.session_for(root)
    try:
        g = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        mark = g.reserve_tool()
        try:
            raise RuntimeError("tool exploded")
        except RuntimeError:
            g.finish_tool(mark, attempted=True)
        row = db.get(EvolutionRun, "run-b")
        assert int(row.used_tool_calls) == 1
        assert g.tool_available() == 0
        with pytest.raises(BudgetExceeded, match="执行前阻止"):
            g.reserve_tool()
        assert int(db.get(EvolutionRun, "run-b").used_tool_calls) == 1
        assert g._tool_inflight_count() == 0
    finally:
        db.close()


def test_crash_recovers_tool_reservation_without_double_count(tmp_path, clock):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, tools=1)
    db = skill_store.session_for(root)
    try:
        g = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        g.reserve_tool()  # 崩溃，未 finish
        row = db.get(EvolutionRun, "run-b")
        assert int(row.used_tool_calls) == 0
        inflight = json.loads(row.reserved_in_flight_json)
        assert any(str(m.get("kind")).startswith("tool") for m in inflight)
        g2 = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        row = db.get(EvolutionRun, "run-b")
        assert int(row.used_tool_calls) == 1
        assert g2.tool_available() == 0
        assert g2._tool_inflight_count() == 0
        with pytest.raises(BudgetExceeded):
            g2.reserve_tool()
        # 再恢复不得双计数
        g3 = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        assert int(db.get(EvolutionRun, "run-b").used_tool_calls) == 1
        assert g3.tool_available() == 0
    finally:
        db.close()


def test_proposer_respects_global_remaining_one_tool(tmp_path, clock):
    from app.core.skill_evolution.contracts import load_dataset
    from app.core.skill_evolution.trace_sampling import group_workspace_id

    ds_dir = tmp_path / "ds"
    src = ds_dir / "sources"
    ref = ds_dir / "references"
    src.mkdir(parents=True)
    ref.mkdir()
    (src / "a.md").write_text("x", encoding="utf-8")
    (ref / "ref.json").write_text(json.dumps({
        "t1": {"expected_points": [{"id": "p1", "kind": "phrase", "text": "x"}],
               "forbidden": []},
    }), encoding="utf-8")
    gid = "g-train"
    (ds_dir / "dataset.json").write_text(json.dumps({
        "dataset_version": "wiki-offline-tool-v1",
        "domain": "wiki_compile.default",
        "grader_version": "wiki-default-grader/v1",
        "tasks": [{
            "task_id": "t1", "dataset_version": "wiki-offline-tool-v1",
            "domain": "wiki_compile.default", "split": "train",
            "group_id": gid, "input_snapshot_id": "s1",
            "instruction": "生成", "grader_version": "wiki-default-grader/v1",
            "reference_ref": "ref.json", "trigger": "manual_rebuild",
            "wiki_title": "T", "wiki_category": "资料",
            "sources": [{"doc_id": "d1", "title": "a", "file": "a.md"}],
        }],
    }), encoding="utf-8")
    ds = load_dataset(ds_dir)
    root = runenv.ensure_experiment_root(tmp_path / "lab")
    eid = "a" * 32
    run_dir = root / "runs" / eid
    run_dir.mkdir(parents=True)
    meta = {
        "execution_id": eid, "task_id": "t1", "split": "train",
        "dataset_version": ds.dataset_version, "domain": ds.domain,
        "group_id": gid, "created_at": "2026-09-09T00:00:00",
        "run_status": "succeeded",
        "outcome": {"run_ok": True, "published": True},
    }
    ts = TraceStore(run_dir, meta)
    ts.seal({"run_status": "succeeded"})
    ws = group_workspace_id(gid)
    _new_run(root, run_id="run-prop", tools=1)
    db = skill_store.session_for(root)
    tool_execs = {"n": 0}

    class TwoTool:
        def __init__(self):
            self.n = 0

        def __call__(self, messages, context="", timeout=120.0):
            self.n += 1
            if self.n <= 2:
                return {"tool": "read_trace", "args": {"execution_id": eid}}
            return {"action": {"type": "no_action",
                               "reason": "enough evidence gathered here",
                               "evidence_execution_ids": [],
                               "pattern_ids": [], "pattern_revision_ids": []}}

    orig = prop.ToolExecutor.call

    def counting_call(self, tool, args):
        tool_execs["n"] += 1
        return orig(self, tool, args)

    try:
        g = BudgetGuard(db, db.get(EvolutionRun, "run-prop"))
        prop.ToolExecutor.call = counting_call  # type: ignore[method-assign]
        summary = prop.run_proposer(
            root, ds, ws, parent_version_id=None,
            execution_ids=[eid], runner=TwoTool(),
            budget_guard=g, max_tool_calls=10)
        row = db.get(EvolutionRun, "run-prop")
        assert int(row.used_tool_calls) == 1
        assert tool_execs["n"] == 1
        assert summary.tool_calls == 1
        assert g.tool_available() == 0
        assert "BUDGET" in (summary.error_code or summary.status or "") or \
            summary.status in ("budget_exhausted", "failed") or \
            (summary.error_message or "").find("预算") >= 0
    finally:
        prop.ToolExecutor.call = orig  # type: ignore[method-assign]
        db.close()


def test_used_tool_calls_equals_attempted_and_no_negative(tmp_path, clock):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, tools=3)
    db = skill_store.session_for(root)
    try:
        g = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        attempted = 0
        for i in range(3):
            mark = g.reserve_tool()
            attempted += 1
            g.finish_tool(mark, attempted=True)
        row = db.get(EvolutionRun, "run-b")
        assert int(row.used_tool_calls) == attempted == 3
        assert g.tool_available() == 0
        assert g._tool_inflight_count() == 0
        with pytest.raises(BudgetExceeded):
            g.reserve_tool()
        assert int(db.get(EvolutionRun, "run-b").used_tool_calls) == 3
        assert g.model_available() >= 0
        assert g.tool_available() >= 0
    finally:
        db.close()


def _alembic(db_path: Path, *args):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    url = f"sqlite:///{db_path.as_posix()}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url={url}", *args],
        cwd=str(BACKEND), capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace")


def test_p53_to_p54_and_empty_to_head_sqlite(tmp_path):
    db = tmp_path / "mig.db"
    r = _alembic(db, "upgrade", P53)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(str(db))
    cols = {row[1] for row in con.execute("PRAGMA table_info(evolution_runs)")}
    assert "used_active_seconds" not in cols
    assert "active_segment_started_at" not in cols
    con.close()
    r = _alembic(db, "upgrade", P54)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(str(db))
    cols = {row[1] for row in con.execute("PRAGMA table_info(evolution_runs)")}
    assert "used_active_seconds" in cols
    assert "active_segment_started_at" in cols
    ver = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert ver == P54
    con.close()

    db2 = tmp_path / "fresh.db"
    r = _alembic(db2, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(str(db2))
    cols = {row[1] for row in con.execute("PRAGMA table_info(evolution_runs)")}
    assert "used_active_seconds" in cols
    ver = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    con.close()
    from tests.alembic_head import current_alembic_head
    assert ver == current_alembic_head() == P54

    db3 = tmp_path / "roundtrip.db"
    r = _alembic(db3, "upgrade", P54)
    assert r.returncode == 0, r.stderr
    r = _alembic(db3, "downgrade", P53)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(str(db3))
    cols = {row[1] for row in con.execute("PRAGMA table_info(evolution_runs)")}
    assert "used_active_seconds" not in cols
    con.close()
    r = _alembic(db3, "upgrade", P54)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(str(db3))
    cols = {row[1] for row in con.execute("PRAGMA table_info(evolution_runs)")}
    assert "used_active_seconds" in cols
    ver = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    con.close()
    assert ver == P54


def test_crash_takeover_settles_old_segment_remaining_never_increases(tmp_path, clock):
    from app.core.skill_evolution.orchestrator import LeaseConflict, claim
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, seconds=20)
    db = skill_store.session_for(root)
    remainings = []
    try:
        row = db.get(EvolutionRun, "run-b")
        row.status = "queued"
        row.lease_token = None
        row.lease_expires_at = None
        row.active_segment_started_at = None
        db.commit()
        row = claim(db, "run-b", worker_id="w1")
        g = BudgetGuard(db, row)
        clock.advance(2)
        assert orch.heartbeat(db, "run-b", row.lease_token) is True
        remainings.append(g.seconds_remaining())
        clock.advance(3)
        row = db.get(EvolutionRun, "run-b")
        row.lease_expires_at = clock.now()
        db.commit()
        row2 = claim(db, "run-b", worker_id="w2")
        g2 = BudgetGuard(db, row2)
        rem = g2.seconds_remaining()
        remainings.append(rem)
        assert rem <= remainings[0] + 1e-6
        assert rem < remainings[0]
        used = g2.active_elapsed()
        assert used >= 5.0 - 1e-6
        for i in range(3):
            clock.advance(2)
            row = db.get(EvolutionRun, "run-b")
            row.lease_expires_at = clock.now()
            db.commit()
            nxt = claim(db, "run-b", worker_id=f"w{i+3}")
            gi = BudgetGuard(db, nxt)
            remainings.append(gi.seconds_remaining())
            assert remainings[-1] <= remainings[-2] + 1e-6
        assert remainings[-1] < remainings[0]
    finally:
        db.close()


def test_active_lease_resume_cannot_preempt(tmp_path, clock):
    from app.core.skill_evolution.orchestrator import LeaseConflict, claim
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root)
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-b")
        row.status = "queued"
        row.lease_token = None
        row.lease_expires_at = None
        db.commit()
        claim(db, "run-b", worker_id="w1")
        with pytest.raises(LeaseConflict):
            claim(db, "run-b", worker_id="w2", resume_clear=True)
        with pytest.raises(LeaseConflict):
            claim(db, "run-b", worker_id="w2", resume_clear=False)
    finally:
        db.close()


def test_explicit_pause_wait_not_counted_and_terminal_stops(tmp_path, clock):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, seconds=50)
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-b")
        g = BudgetGuard(db, row)
        clock.advance(4)
        _stop_active_segment(row)
        db.commit()
        used_at_pause = float(row.used_active_seconds or 0)
        clock.advance(100)
        assert abs(float(db.get(EvolutionRun, "run-b").used_active_seconds) - used_at_pause) < 1e-6
        _start_active_segment(db.get(EvolutionRun, "run-b"))
        db.commit()
        g2 = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        assert abs(g2.active_elapsed() - used_at_pause) < 1e-6
        _stop_active_segment(db.get(EvolutionRun, "run-b"))
        db.get(EvolutionRun, "run-b").status = "completed"
        db.commit()
        clock.advance(30)
        g3 = BudgetGuard(db, db.get(EvolutionRun, "run-b"))
        assert abs(g3.active_elapsed() - float(
            db.get(EvolutionRun, "run-b").used_active_seconds or 0)) < 1e-6
    finally:
        db.close()


def test_heartbeat_then_claim_does_not_double_count(tmp_path, clock):
    from app.core.skill_evolution.orchestrator import claim
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, seconds=30)
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, "run-b")
        row.status = "queued"
        row.lease_token = None
        row.lease_expires_at = None
        db.commit()
        row = claim(db, "run-b", worker_id="w1")
        clock.advance(2)
        orch.heartbeat(db, "run-b", row.lease_token)
        used_after_hb = float(db.get(EvolutionRun, "run-b").used_active_seconds or 0)
        row = db.get(EvolutionRun, "run-b")
        row.lease_expires_at = clock.now() - timedelta(seconds=1)
        db.commit()
        row2 = claim(db, "run-b", worker_id="w2")
        used = float(row2.used_active_seconds or 0)
        assert used <= used_after_hb + 0.05
        assert used >= used_after_hb - 0.05 or used >= 1.9
    finally:
        db.close()


def test_max_wall_seconds_invocation_vs_run_cap(tmp_path, clock):
    root = runenv.ensure_experiment_root(tmp_path / "root")
    _new_run(root, seconds=10)
    db = skill_store.session_for(root)
    sent = []
    try:
        row = db.get(EvolutionRun, "run-b")
        g = BudgetGuard(db, row, invocation_max_seconds=2)
        clock.advance(2)
        with pytest.raises(BudgetExceeded):
            g.wrap(lambda messages, context="", timeout=120.0: sent.append(1) or {"ok": 1})([])
        assert sent == []
        assert g.seconds_remaining() <= 0
    finally:
        db.close()

    root2 = runenv.ensure_experiment_root(tmp_path / "root2")
    _new_run(root2, seconds=3)
    db = skill_store.session_for(root2)
    sent2 = []
    try:
        row = db.get(EvolutionRun, "run-b")
        g = BudgetGuard(db, row, invocation_max_seconds=100)
        clock.advance(3)
        with pytest.raises(BudgetExceeded):
            g.wrap(lambda messages, context="", timeout=120.0: sent2.append(1) or {"ok": 1})([])
        assert sent2 == []
        assert abs(g.active_elapsed() - 3.0) < 1e-6 or g.seconds_remaining() <= 0
    finally:
        db.close()


def test_renewer_interval_safe_when_max_seconds_tiny():
    from app.core.skill_evolution.orchestrator import LongCallRenewer
    r = LongCallRenewer(Path("."), "run-x", "tok", interval=min(5.0, max(1.0, 0 / 4 or 1.0)))
    assert r._interval >= 1.0
    r2 = orch.LongCallRenewer(Path("."), "run-x", "tok", interval=0)
    assert r2._interval >= 1.0
