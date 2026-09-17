"""batch-test 状态封存、租约续租与崩溃窗口（离线 stub；不宣称 exactly-once）。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from app.core.skill_evolution import experiment7 as ex7
from app.core.skill_evolution import orchestrator as orch
from app.core.skill_evolution import skill_store
from app.core.wiki_pipeline import executor as wiki_exec
from app.core.wiki_pipeline import registry as wiki_reg
from tests.test_skill_evolution_stage8y_formal_batch import (
    _mini_ds,
    _real_http_stub,
)


@pytest.fixture(autouse=True)
def _iso():
    wiki_reg.REGISTRY.clear()
    wiki_exec.reset_external_runners()
    yield
    wiki_exec.reset_external_runners()
    wiki_reg.REGISTRY.clear()


def _confirm(root: Path, ds, status: dict) -> dict:
    man = json.loads((root / "plan" / "batch-manifest.json").read_text(encoding="utf-8"))
    return {
        "test_fingerprint": status["test_confirm_fingerprint"],
        "dataset_version": ds.dataset_version,
        "plan_hash": man["plan_hash"],
        "budget_ok": True,
        "config_fingerprint": status["config_fingerprint"],
    }


def _real_cfg(url: str) -> dict:
    return {
        "provider": "stubp", "credential_ref": "FK_BATCH",
        "model": "frozen-stub-model", "api_url": url,
        "timeout": 30, "retries": 0, "max_output_tokens": 256,
    }


def _freeze_real(tmp_path, monkeypatch, settings, *, budget=None):
    Handler, server, url = _real_http_stub(monkeypatch, settings)
    ds = _mini_ds(tmp_path / "ds")
    root = tmp_path / "batch"
    status = ex7.create_batch(
        root, ds, runs={"A": 1, "B": 1, "C": 1, "D": 1},
        model_mode="real", iterations=1, real_config=_real_cfg(url),
        test_budget=budget or {"max_model_calls": 20, "max_seconds": 3600,
                               "max_tool_calls": 20})
    ex7.run_batch(root, ds, confirm={
        "config_fingerprint": status["config_fingerprint"],
        "dataset_version": ds.dataset_version, "budget_ok": True})
    return root, ds, status, _confirm(root, ds, status), Handler, server, url


def _state_files(root: Path):
    return (root / "plan" / "batch-test-state.json",
            root / "plan" / "batch-test-state.sha256")


def test_batch_test_state_tamper_matrix_zero_http(tmp_path, monkeypatch):
    from app.config import settings
    root, ds, status, confirm, Handler, server, url = _freeze_real(
        tmp_path, monkeypatch, settings,
        budget={"max_model_calls": 1, "max_seconds": 3600, "max_tool_calls": 10})
    try:
        n_run = len(Handler.calls)
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        n_mid = len(Handler.calls)
        assert n_mid > n_run
        sp, sealp = _state_files(root)
        original = sp.read_text(encoding="utf-8")
        original_seal = sealp.read_text(encoding="utf-8") if sealp.is_file() else ""
        original_used = json.loads(original).get("used_model_calls")

        def restore():
            sp.write_text(original, encoding="utf-8")
            if original_seal:
                sealp.write_text(original_seal, encoding="utf-8")
            elif sealp.is_file():
                sealp.unlink()

        def apply_json(mut):
            blob = json.loads(original)
            mut(blob)
            sp.write_text(json.dumps(blob, ensure_ascii=False, indent=2,
                                     sort_keys=True), encoding="utf-8")

        def all_done(blob):
            blob["completed"] = ["A/1", "B/1", "C/1", "D/1"]
            reports = dict(blob.get("reports") or {})
            fake = {"protocol": "B", "replicate": 1, "test": {
                "passed": 99, "total": 1, "protocol": "passed_over_total_v1",
                "per_task": []}}
            for key in ("A/1", "B/1", "C/1", "D/1"):
                reports[key] = fake
            blob["reports"] = reports

        def score(blob):
            reports = dict(blob.get("reports") or {})
            key = (blob.get("completed") or ["A/1"])[0]
            row = dict(reports.get(key) or {})
            test = dict(row.get("test") or {})
            test["passed"] = 99
            row["test"] = test
            reports[key] = row
            blob["reports"] = reports

        def ids(blob):
            blob["run_id"] = "run_forged"
            blob["experiment_id"] = "exp_forged"

        def budget(blob):
            b = dict(blob.get("budget") or {})
            b["max_model_calls"] = 999
            blob["budget"] = b

        def drop_schema(blob):
            blob.pop("schema", None)

        tampers = [
            ("completed-all", all_done),
            ("report-score", score),
            ("run-ids", ids),
            ("budget", budget),
            ("missing-schema", drop_schema),
        ]
        for name, mut in tampers:
            apply_json(mut)
            with pytest.raises(ex7.BatchError):
                ex7.evaluate_batch_test(root, ds, confirm=confirm)
            assert len(Handler.calls) == n_mid, name
            restore()

        assert sealp.is_file()
        sealp.write_text("sha256:" + ("0" * 64) + "\n", encoding="utf-8")
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(Handler.calls) == n_mid
        restore()
        sealp.unlink()
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(Handler.calls) == n_mid
        restore()
        sealp.write_text("{not-json", encoding="utf-8")
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(Handler.calls) == n_mid
        restore()

        root2 = tmp_path / "batch2"
        status2 = ex7.create_batch(
            root2, ds, runs={"A": 1, "B": 1, "C": 1, "D": 1},
            model_mode="real", iterations=1, real_config=_real_cfg(url),
            test_budget={"max_model_calls": 20, "max_seconds": 3600,
                         "max_tool_calls": 20})
        ex7.run_batch(root2, ds, confirm={
            "config_fingerprint": status2["config_fingerprint"],
            "dataset_version": ds.dataset_version, "budget_ok": True})
        n_copy = len(Handler.calls)
        (root2 / "plan" / "batch-test-state.json").write_text(
            original, encoding="utf-8")
        if original_seal:
            (root2 / "plan" / "batch-test-state.sha256").write_text(
                original_seal, encoding="utf-8")
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(root2, ds, confirm=_confirm(root2, ds, status2))
        assert len(Handler.calls) == n_copy
        live = json.loads((root / "plan" / "batch-test-state.json").read_text(
            encoding="utf-8"))
        assert live.get("used_model_calls") == original_used
        db = skill_store.session_for(Path(live["lab"]))
        try:
            from app.models.evolution import EvolutionRun
            row = db.get(EvolutionRun, live["run_id"])
            assert int(row.used_model_calls or 0) == original_used
        finally:
            db.close()
    finally:
        server.shutdown()
        server.server_close()


def test_long_call_renewer_blocks_second_claimant(tmp_path, monkeypatch):
    from app.config import settings
    from app.core.skill_evolution import config_freeze
    from app.models.evolution import EvolutionRun
    root, ds, status, confirm, Handler, server, url = _freeze_real(
        tmp_path, monkeypatch, settings,
        budget={"max_model_calls": 1, "max_seconds": 3600, "max_tool_calls": 10})
    monkeypatch.setattr(orch, "DEFAULT_LEASE_SECONDS", 2)
    try:
        n_run = len(Handler.calls)
        started = threading.Event()
        inner = config_freeze.build_actor_runner_frozen
        calls = {"n": 0}

        def slow_build(role, frozen):
            runner = inner(role, frozen)

            def blocked(messages, context="", timeout=120.0):
                calls["n"] += 1
                if calls["n"] == 1:
                    started.set()
                    time.sleep(6)
                return runner(messages, context=context, timeout=timeout)
            return blocked

        monkeypatch.setattr(config_freeze, "build_actor_runner_frozen", slow_build)
        err = []

        def run1():
            try:
                ex7.evaluate_batch_test(root, ds, confirm=confirm)
            except Exception as exc:  # noqa: BLE001
                err.append(exc)

        t = threading.Thread(target=run1)
        t.start()
        assert started.wait(20)
        st = json.loads((root / "plan" / "batch-test-state.json").read_text(
            encoding="utf-8"))
        lab = Path(st["lab"])
        db = skill_store.session_for(lab)
        try:
            with pytest.raises(orch.LeaseConflict):
                orch.claim(db, st["run_id"], worker_id="second")
            row = db.get(EvolutionRun, st["run_id"])
            assert row is not None
            assert row.lease_owner == "batch-test"
        finally:
            db.close()
        n_during = len(Handler.calls)
        t.join(timeout=30)
        assert not t.is_alive()
        n_after = len(Handler.calls)
        assert n_after - n_run == 1
        assert n_during - n_run <= 1
        assert calls["n"] == 1
        final = json.loads((root / "plan" / "batch-test-state.json").read_text(
            encoding="utf-8"))
        assert len(final.get("completed") or []) == 1
        reports = final.get("reports") or {}
        assert len(reports) == 1
        used = int(final.get("used_model_calls") or 0)
        assert used == 1
        db2 = skill_store.session_for(Path(final["lab"]))
        try:
            row = db2.get(EvolutionRun, final["run_id"])
            assert int(row.used_model_calls or 0) == used
        finally:
            db2.close()
    finally:
        server.shutdown()
        server.server_close()


def test_crash_after_eval_before_completed_is_at_least_once(tmp_path, monkeypatch):
    from app.config import settings
    root, ds, status, confirm, Handler, server, url = _freeze_real(
        tmp_path, monkeypatch, settings,
        budget={"max_model_calls": 20, "max_seconds": 3600, "max_tool_calls": 20})
    try:
        n_run = len(Handler.calls)
        real_eval = ex7.evaluate_frozen_test

        def crash_after(*a, **k):
            out = real_eval(*a, **k)
            raise RuntimeError("crash-after-executor")

        monkeypatch.setattr(ex7, "evaluate_frozen_test", crash_after)
        with pytest.raises(RuntimeError, match="crash-after-executor"):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        st = json.loads((root / "plan" / "batch-test-state.json").read_text(
            encoding="utf-8"))
        used = int(st.get("used_model_calls") or 0)
        assert used >= 1
        done = list(st.get("completed") or [])
        assert "A/1" not in done
        n_crash = len(Handler.calls)
        assert n_crash > n_run
        monkeypatch.setattr(ex7, "evaluate_frozen_test", real_eval)
        out = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        st2 = json.loads((root / "plan" / "batch-test-state.json").read_text(
            encoding="utf-8"))
        assert int(st2.get("used_model_calls") or 0) >= used
        assert "A/1" in (st2.get("completed") or [])
        a_rep = (st2.get("reports") or {}).get("A/1") or next(
            r for r in out["reports"] if r.get("protocol") == "A")
        assert a_rep.get("duplicated_after_uncertain_outcome") is True
        assert "exactly-once" not in json.dumps(out).lower()
        n_resume = len(Handler.calls)
        assert n_resume > n_crash
        n_done = len(Handler.calls)
        out2 = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(out2["reports"]) == 4
        assert len(Handler.calls) == n_done
    finally:
        server.shutdown()
        server.server_close()


def _authority(root: Path):
    from app.models.evolution import EvolutionRun
    st = json.loads((root / "plan" / "batch-test-state.json").read_text(
        encoding="utf-8"))
    seal = (root / "plan" / "batch-test-state.sha256").read_text(
        encoding="utf-8").strip().removeprefix("sha256:")
    file_hash = ex7._sha256_path(root / "plan" / "batch-test-state.json")
    db = skill_store.session_for(Path(st["lab"]))
    try:
        row = db.get(EvolutionRun, st["run_id"])
        cfg = json.loads(row.config_json or "{}")
        return st, seal, file_hash, row, cfg
    finally:
        db.close()


def test_stale_worker_completed_cas_loses_to_new_claimant(tmp_path, monkeypatch):
    from datetime import timedelta
    from app.config import settings
    from app.models.evolution import EvolutionRun
    root, ds, status, confirm, Handler, server, url = _freeze_real(
        tmp_path, monkeypatch, settings,
        budget={"max_model_calls": 20, "max_seconds": 3600, "max_tool_calls": 20})
    race = {}

    def hijack(ctx):
        if ctx.get("renewer") is not None:
            ctx["renewer"].stop()
        db2 = skill_store.session_for(ctx["lab"])
        try:
            row = db2.get(EvolutionRun, ctx["run_id"])
            row.lease_expires_at = orch.CLOCK.now() - timedelta(seconds=5)
            db2.commit()
        finally:
            db2.close()
        db3 = skill_store.session_for(ctx["lab"])
        try:
            claimed_b = orch.claim(db3, ctx["run_id"], worker_id="batch-test-b")
            race["b_token"] = claimed_b.lease_token
            race["b_owner"] = claimed_b.lease_owner
            race["b_cfg"] = claimed_b.config_json
            race["used"] = int(claimed_b.used_model_calls or 0)
            race["run_id"] = ctx["run_id"]
            race["lab"] = ctx["lab"]
        finally:
            db3.close()

    monkeypatch.setattr(ex7, "before_completed_state_commit", hijack)
    try:
        n_run = len(Handler.calls)
        with pytest.raises((orch.LeaseConflict, ex7.BatchError)) as ei:
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        blob = str(ei.value) + str(getattr(ei.value, "__cause__", "") or "")
        assert "rowcount=0" in blob
        assert ex7.last_test_state_cas_rowcount == 0
        assert n_run < len(Handler.calls)

        db = skill_store.session_for(race["lab"])
        try:
            row = db.get(EvolutionRun, race["run_id"])
            assert row.lease_token == race["b_token"]
            assert row.lease_owner == "batch-test-b"
            assert row.lease_token != ""
            cfg = json.loads(row.config_json or "{}")
            auth = cfg.get("test_state") or {}
            assert "A/1" not in (auth.get("completed") or [])
            assert not (auth.get("reports") or {}).get("A/1")
            used = int(row.used_model_calls or 0)
            assert used >= 1
            assert used == race["used"]
            assert cfg.get("test_state_sha256") == json.loads(
                race["b_cfg"]).get("test_state_sha256")
        finally:
            db.close()

        file_state = json.loads(
            (root / "plan" / "batch-test-state.json").read_text(encoding="utf-8"))
        assert "A/1" not in (file_state.get("completed") or [])

        dbp = skill_store.session_for(race["lab"])
        try:
            fresh = dbp.get(EvolutionRun, race["run_id"])
            orch._pause(dbp, fresh, race["b_token"], reason="handoff")
        finally:
            dbp.close()

        monkeypatch.setattr(ex7, "before_completed_state_commit", None)
        n_mid = len(Handler.calls)
        out = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        st2, seal, file_hash, row2, cfg2 = _authority(root)
        assert int(row2.used_model_calls or 0) >= used
        assert "A/1" in (st2.get("completed") or [])
        a_rep = (st2.get("reports") or {}).get("A/1")
        assert a_rep.get("duplicated_after_uncertain_outcome") is True
        assert len(Handler.calls) > n_mid
        n_done = len(Handler.calls)
        out2 = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(out2["reports"]) == 4
        assert len(Handler.calls) == n_done
        assert seal == file_hash == cfg2.get("test_state_sha256")
    finally:
        monkeypatch.setattr(ex7, "before_completed_state_commit", None)
        server.shutdown()
        server.server_close()


def test_batch_test_state_cas_succeeds_and_resume_zero_http(tmp_path, monkeypatch):
    from app.config import settings
    root, ds, status, confirm, Handler, server, url = _freeze_real(
        tmp_path, monkeypatch, settings,
        budget={"max_model_calls": 20, "max_seconds": 3600, "max_tool_calls": 20})
    try:
        n_run = len(Handler.calls)
        out = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert ex7.last_test_state_cas_rowcount == 1
        assert len(out["reports"]) == 4
        st, seal, file_hash, row, cfg = _authority(root)
        assert seal == file_hash
        assert file_hash == cfg.get("test_state_sha256")
        auth = cfg.get("test_state") or {}
        assert set(auth.get("completed") or []) == {"A/1", "B/1", "C/1", "D/1"}
        assert set(st.get("completed") or []) == {"A/1", "B/1", "C/1", "D/1"}
        assert len(st.get("reports") or {}) == 4
        assert int(row.used_model_calls or 0) == int(st.get("used_model_calls") or 0)
        n_done = len(Handler.calls)
        assert n_done > n_run
        out2 = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(out2["reports"]) == 4
        assert len(Handler.calls) == n_done
        st2, seal2, file_hash2, row2, cfg2 = _authority(root)
        assert seal2 == file_hash2 == cfg2.get("test_state_sha256")
        assert int(row2.used_model_calls or 0) == int(row.used_model_calls or 0)
    finally:
        server.shutdown()
        server.server_close()


def test_partial_official_state_install_repairs_then_requires_retry(
        tmp_path, monkeypatch):
    """DB 已提交而 JSON/sidecar 只替换一半时，先修镜像且本次零请求。"""
    from app.config import settings

    root, ds, status, confirm, Handler, server, url = _freeze_real(
        tmp_path, monkeypatch, settings,
        budget={"max_model_calls": 20, "max_seconds": 3600,
                "max_tool_calls": 20})
    try:
        out = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(out["reports"]) == 4
        n_done = len(Handler.calls)
        state_path, seal_path = _state_files(root)
        state, seal, file_hash, row, cfg = _authority(root)
        digest = cfg["test_state_sha256"]
        assert cfg["test_state"] == state

        # CAS 成功、新 JSON 已安装，但 sidecar 尚未安装。
        seal_path.write_text("sha256:" + ("0" * 64) + "\n", encoding="utf-8")
        with pytest.raises(ex7.BatchError, match="已从 DB 权威快照恢复；请重试"):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(Handler.calls) == n_done
        assert hashlib.sha256(state_path.read_bytes()).hexdigest() == digest
        assert seal_path.read_text(encoding="utf-8").strip() == f"sha256:{digest}"

        resumed = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(resumed["reports"]) == 4
        assert len(Handler.calls) == n_done

        # CAS 成功，但正式双文件均尚未安装。
        state_path.unlink()
        seal_path.unlink()
        with pytest.raises(ex7.BatchError, match="已从 DB 权威快照恢复；请重试"):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(Handler.calls) == n_done
        assert state_path.is_file() and seal_path.is_file()

        # DB 指向的内容寻址快照损坏时，必须拒绝且不能安装正式状态。
        snapshot = root / "plan" / f"btsnap-{digest[:16]}.json"
        snapshot_original = snapshot.read_bytes()
        state_path.unlink()
        snapshot.write_text("{}", encoding="utf-8")
        with pytest.raises(ex7.BatchError, match="内容寻址快照缺失或损坏"):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        assert len(Handler.calls) == n_done
        assert not state_path.exists()
        snapshot.write_bytes(snapshot_original)
    finally:
        server.shutdown()
        server.server_close()
