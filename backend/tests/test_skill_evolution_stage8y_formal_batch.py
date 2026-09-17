"""M4：正式 A/B/C/D 批次入口闭环（离线 stub；不宣称真实效果）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.skill_evolution import experiment7 as ex7
from app.core.skill_evolution import gating as gate
from app.core.skill_evolution import orchestrator as orch
from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.grader_registry import GRADER_V2
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution.orchestrator import Actors, create_run, execute
from app.core.skill_evolution.trace_sampling import group_workspace_id
from app.core.wiki_pipeline import executor as wiki_exec
from app.core.wiki_pipeline import registry as wiki_reg

RUNS = {"A": 1, "B": 1, "C": 1, "D": 1}
DS_VERSION = "wiki-offline-batch-v1"


@pytest.fixture(autouse=True)
def _iso():
    wiki_reg.REGISTRY.clear()
    wiki_exec.reset_external_runners()
    yield
    wiki_exec.reset_external_runners()
    wiki_reg.REGISTRY.clear()


def _task(tid, split, gid, version, grader):
    return {
        "task_id": tid, "dataset_version": version, "domain": "wiki_compile.default",
        "split": split, "group_id": gid, "input_snapshot_id": f"snap-{tid}",
        "instruction": "根据资料生成 Wiki。", "grader_version": grader,
        "reference_ref": "ref.json", "trigger": "manual_rebuild",
        "wiki_title": f"Wiki {tid}", "wiki_category": "资料",
        "sources": [{"doc_id": "d1", "title": "手册", "file": "a.md"}],
    }


def _mini_ds(tmp_path: Path, *, version=DS_VERSION, grader="wiki-default-grader/v1"):
    root = tmp_path / "dataset"
    src = root / "sources"
    ref = root / "references"
    src.mkdir(parents=True)
    ref.mkdir()
    (src / "a.md").write_text(
        "适用条件：室内安装。扭矩 25 N·m。电压 60V。关闭电源。", encoding="utf-8")
    refs = {}
    tasks = []
    for i in range(1, 5):
        tid = f"t{i:02d}"
        refs[tid] = {"expected_points": [
            {"id": "p1", "kind": "phrase", "text": "适用条件"},
            {"id": "p2", "kind": "value", "value": "25 N·m"},
        ], "forbidden": ["100 N·m"]}
        tasks.append(_task(tid, "train", "g-train", version, grader))
    refs["v01"] = {"expected_points": [
        {"id": "p1", "kind": "phrase", "text": "VAL-ONLY-SECRET-PHRASE-XYZ"},
    ], "forbidden": []}
    refs["s01"] = refs["t01"]
    tasks.append(_task("v01", "val", "g-val", version, grader))
    tasks.append(_task("s01", "test", "g-test", version, grader))
    (ref / "ref.json").write_text(json.dumps(refs, ensure_ascii=False), encoding="utf-8")
    (root / "dataset.json").write_text(json.dumps({
        "dataset_version": version, "domain": "wiki_compile.default",
        "grader_version": grader, "tasks": tasks,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_dataset(root)


def _stub(messages, context="", timeout=120.0):
    return {"summary": "s", "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。关闭电源。"}


def _create(tmp_path, ds=None, **kw):
    ds = ds or _mini_ds(tmp_path)
    root = tmp_path / "batch"
    runs = kw.pop("runs", RUNS)
    status = ex7.create_batch(root, ds, runs=runs, iterations=2, **kw)
    return root, ds, status


def test_default_protocol_and_dataset_version_written(tmp_path):
    a = ex7.default_protocol_config("A", 1, dataset_version=DS_VERSION)
    b = ex7.default_protocol_config("B", 1, dataset_version=DS_VERSION)
    c = ex7.default_protocol_config("C", 1, dataset_version=DS_VERSION)
    d = ex7.default_protocol_config("D", 1, dataset_version=DS_VERSION)
    assert a.evolve is False and b.evolve is False
    assert c.evolve is True and d.evolve is True
    assert a.seed_skill_versions == ()
    assert b.seed_skill_versions and b.seed_content_hash
    meta = ex7.seed_package_fingerprints()
    assert b.seed_skill_versions == (meta["version_id"],)
    assert b.seed_content_hash == meta["content_hash"]
    assert ex7.comparable_protocol_fields(c) == ex7.comparable_protocol_fields(d)
    root, ds, status = _create(tmp_path)
    assert status["dataset_version"] == DS_VERSION
    plan = ex7.load_batch_plan(root)
    assert plan["dataset_version"] == DS_VERSION
    for item in plan["runs"]:
        proto = json.loads(Path(item["dir"]).joinpath("protocol.json").read_text(
            encoding="utf-8"))
        assert proto["dataset_version"] == DS_VERSION
        assert proto["dataset_version"] != "wiki-default-v4"


def test_abcd_offline_batch_isolation_patterns_and_freeze(tmp_path):
    root, ds, _ = _create(tmp_path)
    boom = {"hit": 0}

    def _boom(*_a, **_k):
        boom["hit"] += 1
        raise RuntimeError("SimulatedModel 被调用：real-stub 路径不得静默回退")

    from app.core.skill_evolution.runner import SimulatedModel
    prev = SimulatedModel.__call__
    SimulatedModel.__call__ = _boom  # type: ignore[method-assign]
    try:
        out = ex7.run_batch(
            root, ds, executor_runner=_stub, simulated_forbidden=True)
    finally:
        SimulatedModel.__call__ = prev  # type: ignore[method-assign]
    assert boom["hit"] == 0
    by = {r["protocol"]: r for r in out["results"]}
    assert set(by) == {"A", "B", "C", "D"}
    assert by["A"]["skill_versions"] == []
    assert by["A"]["role_calls"]["maintainer"] == 0
    assert by["A"]["role_calls"]["proposer"] == 0
    assert by["A"]["role_calls"]["iterations"] == 0
    assert by["B"]["skill_versions"]
    b_ver = list(by["B"]["skill_versions"])
    assert by["B"]["role_calls"]["maintainer"] == 0
    assert by["B"]["role_calls"]["proposer"] == 0
    assert by["B"]["role_calls"]["iterations"] == 0
    assert by["C"]["status"] == "completed", by["C"]
    assert by["C"]["pattern_count"] == 0
    assert by["C"]["role_calls"]["iterations"] == 2
    assert by["C"]["role_calls"]["maintainer"] == 0
    assert by["D"]["status"] == "completed", by["D"]
    assert by["D"]["role_calls"]["iterations"] == 2
    assert by["D"]["pattern_count"] >= 1
    labs = {by[p]["lab"] for p in "ABCD"}
    assert len(labs) == 4
    ids = {by[p]["run_id"] for p in "ABCD"}
    assert len(ids) == 4
    st = ex7.batch_status(root, ds)
    assert st["all_frozen"] is True
    frozen_b = ex7.load_frozen_set(Path(by["B"]["dir"]))
    assert [m["version_id"] for m in frozen_b["members"]] == b_ver


def test_test_split_rejected_until_all_frozen_then_unified(tmp_path):
    root, ds, _ = _create(tmp_path)
    with pytest.raises(ex7.BatchError, match="未全部冻结"):
        ex7.evaluate_batch_test(root, ds, executor_runner=_stub)
    ex7.run_batch(root, ds, only=("A",), executor_runner=_stub)
    with pytest.raises(ex7.BatchError, match="未全部冻结"):
        ex7.evaluate_batch_test(root, ds, executor_runner=_stub)
    ex7.run_batch(root, ds, executor_runner=_stub)
    report = ex7.evaluate_batch_test(root, ds, executor_runner=_stub, consume=True)
    assert len(report["reports"]) == 4
    assert (root / f"test-consumed-{ds.dataset_version}.marker").is_file()
    for row in report["reports"]:
        assert row["test"]["total"] == 1
        assert "replicate" in row


def test_tampered_frozen_set_rejected(tmp_path):
    root, ds, _ = _create(tmp_path)
    ex7.run_batch(root, ds, executor_runner=_stub)
    plan = ex7.load_batch_plan(root)
    target = Path(plan["runs"][0]["dir"]) / ex7.FROZEN_SET_FILENAME
    data = json.loads(target.read_text(encoding="utf-8"))
    data["members_hash"] = "0" * 64
    target.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ex7.BatchError, match="members_hash"):
        ex7.evaluate_batch_test(root, ds, executor_runner=_stub)


def test_tampered_run_ownership_rejected(tmp_path):
    root, ds, _ = _create(tmp_path)
    ex7.run_batch(root, ds, executor_runner=_stub)
    plan = ex7.load_batch_plan(root)
    path = Path(plan["runs"][1]["dir"]) / ex7.FROZEN_SET_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["run_id"] = "run_tampered_other"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ex7.BatchError, match="不属于该 run"):
        ex7.evaluate_batch_test(root, ds, executor_runner=_stub)


def test_v2_grader_dispatch_no_v1_fallback(tmp_path):
    ds = _mini_ds(tmp_path / "v2", version="wiki-offline-batch-v2", grader=GRADER_V2)
    with pytest.raises(ex7.BatchError, match="不回退"):
        ex7.evaluate_frozen_test(
            runenv.ensure_experiment_root(tmp_path / "t"), ds,
            FrozenSkillSet.empty(), executor_runner=_stub, reviewer=None)


def test_independent_budgets_and_resume_same_run(tmp_path):
    root, ds, _ = _create(tmp_path, runs={"A": 1, "B": 1, "C": 1, "D": 1},
                          group_budgets={"A": {"max_model_calls": 8, "max_tool_calls": 80,
                                               "max_seconds": 3600}})
    first = ex7.run_batch(root, ds, only=("A",), executor_runner=_stub)
    a_id = first["results"][0]["run_id"]
    a_lab = first["results"][0]["lab"]
    resumed = ex7.resume_batch(root, ds, executor_runner=_stub)
    by = {r["protocol"]: r for r in resumed["results"] if not r.get("skipped")}
    st = ex7.batch_status(root, ds)
    a_row = next(r for r in st["runs"] if r["protocol"] == "A")
    assert a_row["run_id"] == a_id
    db_a = skill_store.session_for(Path(a_lab))
    db_d = skill_store.session_for(Path(by["D"]["lab"]))
    try:
        from app.models.evolution import EvolutionRun
        ra = db_a.get(EvolutionRun, a_id)
        rd = db_d.get(EvolutionRun, by["D"]["run_id"])
        assert ra is not None and rd is not None
        assert ra.run_id != rd.run_id
        assert int(ra.used_model_calls or 0) <= 8
    finally:
        db_a.close()
        db_d.close()

    root2, ds2, _ = _create(tmp_path / "r2")
    plan2 = ex7.load_batch_plan(root2)
    c_item = next(p for p in plan2["runs"] if p["protocol"] == "C")
    lab = runenv.ensure_experiment_root(ex7._replicate_root(root2, "C", 1))
    db = skill_store.session_for(lab)
    try:
        train_ids = [t.task_id for t in ds2.tasks if t.split == "train"]
        val_ids = [t.task_id for t in ds2.tasks if t.split == "val"]
        ws = group_workspace_id(next(
            t.group_id for t in ds2.tasks if t.split == "train"))
        exp_info = gate.create_experiment(
            db, workspace_id=ws, domain=ds2.domain, dataset=ds2,
            grader_version=ds2.grader_version,
            runner_config={"profile": "faithful", "review": "v1",
                           "val_task_ids": val_ids},
            pipeline_key="wiki.default", pipeline_version="3",
            runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=val_ids, initial_members=[])
        run = create_run(
            db, experiment_id=exp_info.experiment_id, workspace_id=ws,
            domain=ds2.domain, dataset=ds2, init_mode="paper",
            max_iterations=2, budget={"max_model_calls": 80, "max_tool_calls": 80,
                                      "max_seconds": 3600},
            runner_config={"profile": "faithful", "review": "v1"},
            train_task_ids=train_ids, experience="none", evolve=True)
        run_id = run.run_id
        orch.pause_request(db, run_id)
        ex7._write_run_state(Path(c_item["dir"]), {
            "run_id": run_id, "experiment_id": exp_info.experiment_id,
            "lab": str(lab), "protocol": "C", "replicate": 1,
        })
    finally:
        db.close()
    view = execute(lab, ds2, run_id, actors=Actors(
        execution_profile="faithful", executor_runner=_stub))
    assert view["status"] == "paused"
    again = ex7.resume_batch(root2, ds2, only=("C",), executor_runner=_stub)
    c_res = next(r for r in again["results"] if r.get("protocol") == "C")
    assert c_res["run_id"] == run_id
    assert Path(c_item["dir"]).joinpath(ex7.FROZEN_SET_FILENAME).is_file()


def test_cli_batch_commands_registered():
    from app.core.skill_evolution.cli import build_parser
    p = build_parser()
    sub = [a for a in p._actions if getattr(a, "choices", None)]
    choices = sub[0].choices
    for name in ("experiment-batch-create", "experiment-batch-run",
                 "experiment-batch-resume", "experiment-batch-status",
                 "experiment-batch-test"):
        assert name in choices


def test_paired_bootstrap_keeps_all_replicates_and_uncertainty():
    diffs = {"g1": [0.1], "g2": [-0.05]}
    out = ex7.paired_bootstrap(diffs, n_boot=50, seed=1)
    assert out["groups"] == 2
    assert out["ci"] is not None
    assert "不强行" in (out.get("note") or "")


def _freeze_all(tmp_path):
    root, ds, _ = _create(tmp_path)
    ex7.run_batch(root, ds, executor_runner=_stub)
    return root, ds


def test_frozen_set_missing_required_fields_rejected(tmp_path):
    root, ds = _freeze_all(tmp_path)
    plan = ex7.load_batch_plan(root)
    target = Path(plan["runs"][0]["dir"]) / ex7.FROZEN_SET_FILENAME
    original = json.loads(target.read_text(encoding="utf-8"))
    for field in ("members_hash", "run_id", "experiment_id", "schema",
                  "set_hash", "mode"):
        data = dict(original)
        data.pop(field, None)
        target.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(root, ds, executor_runner=_stub)
        target.write_text(json.dumps(original), encoding="utf-8")
    data = dict(original)
    data["protocol"] = "Z"
    target.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.evaluate_batch_test(root, ds, executor_runner=_stub)
    data = dict(original)
    data["replicate"] = 99
    target.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.evaluate_batch_test(root, ds, executor_runner=_stub)


def test_frozen_members_hash_self_consistent_but_db_mismatch_rejected(tmp_path):
    root, ds = _freeze_all(tmp_path)
    plan = ex7.load_batch_plan(root)
    b = next(p for p in plan["runs"] if p["protocol"] == "B")
    path = Path(b["dir"]) / ex7.FROZEN_SET_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("members"):
        data["members"][0]["content_hash"] = "f" * 64
        from app.core.skill_evolution.injector import set_hash_for_members
        from app.core.skill_evolution.contracts import canonical_json
        import hashlib
        members = data["members"]
        data["members_hash"] = hashlib.sha256(
            canonical_json({"members": [
                {k: m.get(k) for k in ("skill_id", "version_id", "content_hash")}
                for m in members
            ]}).encode("utf-8")).hexdigest()
        data["set_hash"] = set_hash_for_members(members)
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(root, ds, executor_runner=_stub)


def test_dataset_or_reference_or_protocol_or_manifest_tamper_rejected(tmp_path):
    root, ds, _ = _create(tmp_path)
    src = Path(ds.dataset_dir) / "sources" / "a.md"
    original_src = src.read_text(encoding="utf-8")
    src.write_text(original_src + "\nTAMPER", encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.run_batch(root, ds, executor_runner=_stub)
    with pytest.raises(ex7.BatchError):
        ex7.resume_batch(root, ds, executor_runner=_stub)
    src.write_text(original_src, encoding="utf-8")
    ref = Path(ds.dataset_dir) / "references" / "ref.json"
    original_ref = ref.read_text(encoding="utf-8")
    ref.write_text(original_ref.replace("适用条件", "TAMPER-REF"), encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.run_batch(root, ds, executor_runner=_stub)
    ref.write_text(original_ref, encoding="utf-8")
    proto = Path(ex7.load_batch_plan(root)["runs"][0]["dir"]) / "protocol.json"
    original_proto = proto.read_text(encoding="utf-8")
    payload = json.loads(original_proto)
    payload["budget"]["max_model_calls"] = 1
    proto.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.run_batch(root, ds, executor_runner=_stub)
    proto.write_text(original_proto, encoding="utf-8")
    man = root / "plan" / "batch-manifest.json"
    original_man = man.read_text(encoding="utf-8")
    blob = json.loads(original_man)
    blob["plan_hash"] = "0" * 64
    man.write_text(json.dumps(blob), encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.run_batch(root, ds, executor_runner=_stub)
    with pytest.raises(ex7.BatchError):
        ex7.batch_status(root, ds)
    man.write_text(original_man, encoding="utf-8")
    ex7.run_batch(root, ds, executor_runner=_stub)
    src.write_text(original_src + "\nTAMPER-AFTER-FREEZE", encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.resume_batch(root, ds, executor_runner=_stub)
    with pytest.raises(ex7.BatchError):
        ex7.evaluate_batch_test(root, ds, executor_runner=_stub)
    src.write_text(original_src, encoding="utf-8")


def test_second_cd_replicate_config_mismatch_rejected(tmp_path):
    ds = _mini_ds(tmp_path)
    root = tmp_path / "batch"
    with pytest.raises(ex7.BatchError):
        ex7.create_batch(
            root, ds, runs={"A": 1, "B": 1, "C": 2, "D": 2}, iterations=2,
            group_overrides={"C": {2: {"timeout": 9.0}}})


def test_untampered_batch_still_runs(tmp_path):
    root, ds, _ = _create(tmp_path)
    out = ex7.run_batch(root, ds, executor_runner=_stub)
    assert set(r["protocol"] for r in out["results"]) == {"A", "B", "C", "D"}
    report = ex7.evaluate_batch_test(root, ds, executor_runner=_stub)
    assert len(report["reports"]) == 4


def test_real_create_missing_config_fails_before_http(tmp_path, monkeypatch):
    ds = _mini_ds(tmp_path)
    hits = []

    def _no_net(*a, **k):
        hits.append(1)
        raise AssertionError("不得发 HTTP")

    monkeypatch.setattr("httpx.Client.post", _no_net)
    with pytest.raises(ex7.BatchError):
        ex7.create_batch(tmp_path / "b", ds, model_mode="real")
    assert hits == []


def test_real_batch_cli_local_stub_chain(tmp_path, monkeypatch):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from app.config import settings
    from app.core.skill_evolution import cli as evo_cli
    from app.core.skill_evolution.runner import SimulatedModel
    from app.core.skill_evolution.maintainer import SimulatedMaintainer
    from app.core.skill_evolution.orchestrator import PolicyProposer

    class Handler(BaseHTTPRequestHandler):
        calls: list = []

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = _json.loads(self.rfile.read(n) or b"{}")
            blob = _json.dumps(body.get("messages") or [], ensure_ascii=False)
            Handler.calls.append({
                "auth": self.headers.get("Authorization"),
                "model": body.get("model"),
                "max_tokens": body.get("max_tokens") or (body.get("max_completion_tokens")),
                "path": self.path,
                "blob": blob[:200],
            })
            if "wiki-maintain" in blob or "create_patterns" in blob or "授权训练执行采样" in blob:
                content = _json.dumps({
                    "create_patterns": [{
                        "title": "stub-pattern",
                        "phenomenon": "p",
                        "cause_hypothesis": "（待验证）h",
                        "suggestion": "s",
                        "applicability": "a",
                        "supporting_execution_ids": [],
                        "conflicting_execution_ids": [],
                    }],
                    "append_log": ["ok"],
                    "update_index": True,
                }, ensure_ascii=False)
            elif ("wiki-propose" in blob or "read_trace" in blob
                  or "read_index" in blob or "read_pattern" in blob
                  or "read_skill_history" in blob or "read_skill_version" in blob):
                content = _json.dumps({
                    "action": {"type": "no_action", "reason": "stub-complete",
                               "evidence_execution_ids": [],
                               "pattern_ids": [], "pattern_revision_ids": []},
                }, ensure_ascii=False)
            else:
                content = _json.dumps({
                    "summary": "s",
                    "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。关闭电源。",
                }, ensure_ascii=False)
            resp = _json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    monkeypatch.setattr(settings, "llm_api_url", url)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", "frozen-stub-model")
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_require_provider_binding", True)
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        _json.dumps({"stubp": {"credential_env": "FK_BATCH",
                                               "endpoints": [url],
                                               "allow_insecure": True}}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", "stubp")
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "stubp")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", url)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "frozen-stub-model")
    monkeypatch.setenv("FK_BATCH", "stub-key")

    boom = {"sim": 0, "maint": 0, "prop": 0, "pol": 0}

    def _b1(self, *a, **k):
        boom["sim"] += 1
        raise AssertionError("SimulatedModel")

    def _b2(self, *a, **k):
        boom["maint"] += 1
        raise AssertionError("SimulatedMaintainer")

    def _b3(self, *a, **k):
        boom["prop"] += 1
        raise AssertionError("SimulatedProposer")

    def _b4(self, *a, **k):
        boom["pol"] += 1
        raise AssertionError("PolicyProposer")

    monkeypatch.setattr(SimulatedModel, "__call__", _b1)
    monkeypatch.setattr(SimulatedMaintainer, "__call__", _b2)
    try:
        from app.core.skill_evolution.proposer import SimulatedProposer
        monkeypatch.setattr(SimulatedProposer, "__call__", _b3)
    except ImportError:
        pass
    monkeypatch.setattr(PolicyProposer, "__call__", _b4)

    ds = _mini_ds(tmp_path / "ds")
    root = tmp_path / "real-batch"
    real_cfg = {
        "provider": "stubp",
        "credential_ref": "FK_BATCH",
        "model": "frozen-stub-model",
        "api_url": url,
        "timeout": 30,
        "retries": 0,
        "max_output_tokens": 256,
    }
    cfg_path = tmp_path / "real.json"
    cfg_path.write_text(_json.dumps(real_cfg), encoding="utf-8")

    class A:
        pass
    args = A()
    args.root = str(root)
    args.dataset = str(ds.dataset_dir)
    args.runs = "A=1,B=1,C=1,D=1"
    args.model_mode = "real"
    args.iterations = 1
    args.real_config = str(cfg_path)
    args.test_budget = _json.dumps({
        "max_model_calls": 80, "max_seconds": 3600, "max_tool_calls": 80})
    n0 = len(Handler.calls)
    rc = evo_cli.cmd_exp_batch_create(args)
    assert rc == 0
    assert len(Handler.calls) == n0
    status = _json.loads((root / "plan" / "batch-status.json").read_text(encoding="utf-8"))
    fp = status.get("config_fingerprint") or (status.get("frozen") or {}).get("fingerprint")
    assert fp
    test_fp = status.get("test_confirm_fingerprint")
    assert test_fp
    blob = _json.dumps(status)
    assert "stub-key" not in blob
    assert "FK_BATCH" in blob or "credential" in blob.lower()

    bad = dict(real_cfg, credential_ref="WRONG_ENV")
    with pytest.raises(ex7.BatchError):
        ex7.create_batch(tmp_path / "bad-cred", ds, model_mode="real",
                         runs={"A": 1, "B": 1, "C": 1, "D": 1},
                         real_config=bad)
    assert len(Handler.calls) == n0

    args.confirm_fingerprint = fp
    args.confirm_dataset = ds.dataset_version
    args.confirm_budget = True
    args.resume = False
    args.only = "C,D"
    args.simulated_forbidden = True
    rc = evo_cli.cmd_exp_batch_run(args)
    assert rc == 0
    assert boom["sim"] == 0 and boom["maint"] == 0 and boom["pol"] == 0
    st = ex7.batch_status(root, ds)
    by_proto = {r["protocol"]: r for r in st["runs"]}
    assert by_proto["C"]["frozen"] is True
    assert by_proto["D"]["frozen"] is True
    roles_hit = {c["model"] for c in Handler.calls}
    assert "frozen-stub-model" in roles_hit or all(
        c["model"] == "frozen-stub-model" for c in Handler.calls if c["model"])
    assert any(c["auth"] and "stub-key" in c["auth"] for c in Handler.calls)
    assert all(c.get("max_tokens") in (256, None) or c.get("max_tokens") == 256
               for c in Handler.calls)
    blobs = [c.get("blob") or "" for c in Handler.calls]
    assert any("wiki-maintain" in b or "create_patterns" in b or "授权训练执行采样" in b
               for b in blobs)
    assert any("wiki-propose" in b or "read_trace" in b or '"tool"' in b
               for b in blobs)
    assert len(Handler.calls) >= 3

    args_test = A()
    args_test.root = str(root)
    args_test.dataset = str(ds.dataset_dir)
    args_test.confirm_fingerprint = fp
    args_test.consume = False
    with pytest.raises(ex7.BatchError, match="未全部冻结"):
        evo_cli.cmd_exp_batch_test(args_test)

    args.confirm_fingerprint = "deadbeef"
    with pytest.raises(Exception):
        evo_cli.cmd_exp_batch_run(args)
    args.confirm_fingerprint = fp
    args.only = None
    args.resume = True
    evo_cli.cmd_exp_batch_run(args)

    args.only = None
    args.resume = False
    evo_cli.cmd_exp_batch_run(args)
    man = _json.loads((root / "plan" / "batch-manifest.json").read_text(encoding="utf-8"))
    args_test.confirm_test_fingerprint = test_fp
    args_test.confirm_plan_hash = man["plan_hash"]
    args_test.confirm_dataset = ds.dataset_version
    args_test.confirm_budget = True
    rc = evo_cli.cmd_exp_batch_test(args_test)
    assert rc == 0
    server.shutdown()
    server.server_close()
    assert boom["sim"] == 0 and boom["maint"] == 0 and boom["pol"] == 0
    assert boom.get("prop", 0) == 0


def test_real_v2_reviewer_frozen_and_http(tmp_path, monkeypatch):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from app.config import settings
    from app.core.skill_evolution import config_freeze
    from app.core.skill_evolution.runner import SimulatedModel

    class Handler(BaseHTTPRequestHandler):
        calls: list = []

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = _json.loads(self.rfile.read(n) or b"{}")
            Handler.calls.append({
                "auth": self.headers.get("Authorization"),
                "model": body.get("model"),
                "max_tokens": body.get("max_tokens"),
            })
            content = _json.dumps({
                "identity": "review:stub",
                "items": [{"check_id": "c1", "verdict": "pass",
                           "reason": "与来源一致"}],
            }, ensure_ascii=False)
            resp = _json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    monkeypatch.setattr(settings, "llm_api_url", url)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", "frozen-stub-model")
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_require_provider_binding", True)
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        _json.dumps({"stubp": {"credential_env": "FK_BATCH",
                                               "endpoints": [url],
                                               "allow_insecure": True}}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", "stubp")
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "stubp")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", url)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "frozen-stub-reviewer")
    monkeypatch.setattr(settings, "wikiskill_reviewer_prompt_version",
                        "wiki-default-review/prompt-v1")
    monkeypatch.setenv("FK_BATCH", "stub-key")
    boom = {"sim": 0}

    def _b1(self, *a, **k):
        boom["sim"] += 1
        raise AssertionError("SimulatedModel")

    monkeypatch.setattr(SimulatedModel, "__call__", _b1)
    ds = _mini_ds(tmp_path / "v2", version="wiki-offline-batch-v2", grader=GRADER_V2)
    n0 = len(Handler.calls)
    status = ex7.create_batch(
        tmp_path / "v2-batch", ds, runs={"A": 1, "B": 1, "C": 1, "D": 1},
        model_mode="real", iterations=1, real_config={
            "provider": "stubp", "credential_ref": "FK_BATCH",
            "model": "frozen-stub-model", "api_url": url,
            "timeout": 30, "retries": 0, "max_output_tokens": 256,
        },
        test_budget={"max_model_calls": 20, "max_seconds": 3600,
                     "max_tool_calls": 20})
    assert len(Handler.calls) == n0
    frozen = status.get("frozen") or {}
    assert frozen.get("frozen_reviewer")
    assert frozen["frozen_reviewer"].get("model_id") == "frozen-stub-reviewer"
    assert "stub-key" not in _json.dumps(status)
    reviewer = config_freeze.build_reviewer_frozen(frozen)
    out = reviewer.review(
        task_id="t01", sources=["电压 60V"],
        candidate_output={"sections": []},
        checks=[{"id": "c1", "check": "claim", "detail": "电压"}])
    assert out.get("items")
    assert len(Handler.calls) > n0
    assert any(c.get("model") == "frozen-stub-reviewer" for c in Handler.calls)
    assert any(c.get("auth") and "stub-key" in c["auth"] for c in Handler.calls)
    assert boom["sim"] == 0
    server.shutdown()
    server.server_close()


def test_last_frozen_set_tamper_preflight_zero_requests(tmp_path):
    root, ds = _freeze_all(tmp_path)
    plan = ex7.load_batch_plan(root)
    last = plan["runs"][-1]
    path = Path(last["dir"]) / ex7.FROZEN_SET_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["members_hash"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    exec_n, rev_n = [], []

    def exec_hit(messages, context="", timeout=120.0):
        exec_n.append(1)
        return _stub(messages, context=context, timeout=timeout)

    class Rev:
        identity = "r"
        config_fingerprint = "f"
        def review(self, **kw):
            rev_n.append(1)
            raise AssertionError("reviewer")

    with pytest.raises(ex7.BatchError):
        ex7.evaluate_batch_test(root, ds, executor_runner=exec_hit, reviewer=Rev())
    assert exec_n == [] and rev_n == []
    te = root / "test-eval"
    if te.exists():
        assert list(te.rglob("runs/*")) == []
    assert not (root / f"test-consumed-{ds.dataset_version}.marker").is_file()


def test_batch_status_rejects_source_or_reference_tamper(tmp_path):
    root, ds, _ = _create(tmp_path)
    src = Path(ds.dataset_dir) / "sources" / "a.md"
    original = src.read_text(encoding="utf-8")
    src.write_text(original + "\nSTATUS-TAMPER", encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.batch_status(root, ds)
    src.write_text(original, encoding="utf-8")
    ref = Path(ds.dataset_dir) / "references" / "ref.json"
    original_ref = ref.read_text(encoding="utf-8")
    ref.write_text(original_ref.replace("适用条件", "STATUS-REF"), encoding="utf-8")
    with pytest.raises(ex7.BatchError):
        ex7.batch_status(root, ds)
    ref.write_text(original_ref, encoding="utf-8")
    from app.core.skill_evolution import cli as evo_cli
    class A:
        pass
    args = A()
    args.root = str(root)
    with pytest.raises((SystemExit, TypeError, ex7.BatchError)):
        evo_cli.cmd_exp_batch_status(args)
    args.dataset = str(ds.dataset_dir)
    st = evo_cli.cmd_exp_batch_status(args)
    assert st == 0


def _real_http_stub(monkeypatch, settings):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        calls: list = []

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = _json.loads(self.rfile.read(n) or b"{}")
            blob = _json.dumps(body.get("messages") or [], ensure_ascii=False)
            Handler.calls.append({
                "auth": self.headers.get("Authorization"),
                "model": body.get("model"),
                "blob": blob[:160],
            })
            if "wiki-maintain" in blob or "create_patterns" in blob or "授权训练执行采样" in blob:
                inner = _json.dumps({
                    "create_patterns": [{
                        "title": "stub-pattern", "phenomenon": "p",
                        "cause_hypothesis": "（待验证）h", "suggestion": "s",
                        "applicability": "a", "supporting_execution_ids": [],
                        "conflicting_execution_ids": [],
                    }],
                    "append_log": ["ok"], "update_index": True,
                }, ensure_ascii=False)
            elif ("wiki-propose" in blob or "read_trace" in blob
                  or "read_index" in blob or "read_pattern" in blob):
                inner = _json.dumps({
                    "action": {"type": "no_action", "reason": "stub",
                               "evidence_execution_ids": [],
                               "pattern_ids": [], "pattern_revision_ids": []},
                }, ensure_ascii=False)
            else:
                inner = _json.dumps({
                    "summary": "s",
                    "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。关闭电源。",
                }, ensure_ascii=False)
            resp = _json.dumps({"choices": [{"message": {"content": inner}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    monkeypatch.setattr(settings, "llm_api_url", url)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "llm_model", "frozen-stub-model")
    monkeypatch.setattr(settings, "wikiskill_evolution_admin_real_enabled", True)
    monkeypatch.setattr(settings, "wikiskill_require_provider_binding", True)
    monkeypatch.setattr(settings, "wikiskill_credential_providers",
                        _json.dumps({"stubp": {"credential_env": "FK_BATCH",
                                               "endpoints": [url],
                                               "allow_insecure": True}}))
    monkeypatch.setattr(settings, "wikiskill_default_provider", "stubp")
    monkeypatch.setattr(settings, "wikiskill_reviewer_provider", "stubp")
    monkeypatch.setattr(settings, "wikiskill_reviewer_api_url", url)
    monkeypatch.setattr(settings, "wikiskill_reviewer_model_id", "frozen-stub-model")
    monkeypatch.setenv("FK_BATCH", "stub-key")
    return Handler, server, url


def test_real_test_budget_blocks_and_confirm_before_http(tmp_path, monkeypatch):
    from app.config import settings
    from app.core.skill_evolution import cli as evo_cli
    Handler, server, url = _real_http_stub(monkeypatch, settings)
    try:
        ds = _mini_ds(tmp_path / "ds")
        root = tmp_path / "rb"
        real_cfg = {
            "provider": "stubp", "credential_ref": "FK_BATCH",
            "model": "frozen-stub-model", "api_url": url,
            "timeout": 30, "retries": 0, "max_output_tokens": 256,
        }
        n0 = len(Handler.calls)
        status = ex7.create_batch(
            root, ds, runs={"A": 1, "B": 1, "C": 1, "D": 1},
            model_mode="real", iterations=1, real_config=real_cfg,
            test_budget={"max_model_calls": 1, "max_seconds": 3600,
                         "max_tool_calls": 10})
        assert len(Handler.calls) == n0
        fp = status["config_fingerprint"]
        test_fp = status.get("test_confirm_fingerprint") or (
            (status.get("fingerprints") or {}).get("test_confirm_fingerprint"))
        assert test_fp
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(root, ds)
        assert len(Handler.calls) == n0
        with pytest.raises(ex7.BatchError):
            ex7.evaluate_batch_test(
                root, ds, confirm={"test_fingerprint": "deadbeef",
                                   "dataset_version": ds.dataset_version,
                                   "plan_hash": "x", "budget_ok": True})
        assert len(Handler.calls) == n0
        class A:
            pass
        args = A()
        args.root = str(root)
        args.dataset = str(ds.dataset_dir)
        args.confirm_fingerprint = fp
        args.confirm_dataset = ds.dataset_version
        args.confirm_budget = True
        args.resume = False
        args.only = None
        args.simulated_forbidden = True
        assert evo_cli.cmd_exp_batch_run(args) == 0
        n_after_run = len(Handler.calls)
        args_test = A()
        args_test.root = str(root)
        args_test.dataset = str(ds.dataset_dir)
        args_test.confirm_fingerprint = fp
        args_test.consume = False
        with pytest.raises(ex7.BatchError):
            evo_cli.cmd_exp_batch_test(args_test)
        assert len(Handler.calls) == n_after_run
        man = json.loads((root / "plan" / "batch-manifest.json").read_text(
            encoding="utf-8"))
        args_test.confirm_test_fingerprint = test_fp
        args_test.confirm_plan_hash = man["plan_hash"]
        args_test.confirm_dataset = ds.dataset_version
        args_test.confirm_budget = True
        args_test.confirm_fingerprint = fp
        with pytest.raises(ex7.BatchError):
            evo_cli.cmd_exp_batch_test(args_test)
        n_after_first_test = len(Handler.calls)
        assert n_after_first_test > n_after_run
        with pytest.raises(ex7.BatchError):
            evo_cli.cmd_exp_batch_test(args_test)
        assert len(Handler.calls) == n_after_first_test
        state = json.loads(
            (root / "plan" / "batch-test-state.json").read_text(encoding="utf-8"))
        assert int(state.get("used_model_calls") or 0) >= 1
        assert state.get("budget", {}).get("max_model_calls") == 1
    finally:
        server.shutdown()
        server.server_close()

def test_real_test_interrupt_resume_keeps_budget_and_skips_done(tmp_path, monkeypatch):
    from app.config import settings
    Handler, server, url = _real_http_stub(monkeypatch, settings)
    try:
        ds = _mini_ds(tmp_path / "ds")
        root = tmp_path / "ri"
        real_cfg = {
            "provider": "stubp", "credential_ref": "FK_BATCH",
            "model": "frozen-stub-model", "api_url": url,
            "timeout": 30, "retries": 0, "max_output_tokens": 256,
        }
        status = ex7.create_batch(
            root, ds, runs={"A": 1, "B": 1, "C": 1, "D": 1},
            model_mode="real", iterations=1, real_config=real_cfg,
            test_budget={"max_model_calls": 20, "max_seconds": 3600,
                         "max_tool_calls": 20})
        confirm = {
            "test_fingerprint": status["test_confirm_fingerprint"],
            "dataset_version": ds.dataset_version,
            "plan_hash": json.loads(
                (root / "plan" / "batch-manifest.json").read_text(
                    encoding="utf-8"))["plan_hash"],
            "budget_ok": True,
            "config_fingerprint": status["config_fingerprint"],
        }
        ex7.run_batch(root, ds, confirm={
            "config_fingerprint": status["config_fingerprint"],
            "dataset_version": ds.dataset_version, "budget_ok": True})
        real_eval = ex7.evaluate_frozen_test
        seen = []

        def once(*a, **k):
            if seen:
                raise RuntimeError("interrupt-after-first")
            seen.append(1)
            return real_eval(*a, **k)

        monkeypatch.setattr(ex7, "evaluate_frozen_test", once)
        with pytest.raises(RuntimeError, match="interrupt"):
            ex7.evaluate_batch_test(root, ds, confirm=confirm)
        state = json.loads(
            (root / "plan" / "batch-test-state.json").read_text(encoding="utf-8"))
        used = int(state.get("used_model_calls") or 0)
        done = list(state.get("completed") or [])
        assert used >= 1
        assert done
        n_mid = len(Handler.calls)
        monkeypatch.setattr(ex7, "evaluate_frozen_test", real_eval)
        out = ex7.evaluate_batch_test(root, ds, confirm=confirm)
        state2 = json.loads(
            (root / "plan" / "batch-test-state.json").read_text(encoding="utf-8"))
        assert int(state2.get("used_model_calls") or 0) >= used
        assert set(done).issubset(set(state2.get("completed") or []))
        assert len(out["reports"]) == 4
        assert n_mid <= len(Handler.calls)
    finally:
        server.shutdown()
        server.server_close()
