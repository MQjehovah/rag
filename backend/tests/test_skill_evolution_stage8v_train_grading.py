"""M1：训练轨迹接入真实冻结 grader（离线；不触外部模型）。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.skill_evolution import runenv, train_grading as tg
from app.core.skill_evolution.contracts import load_dataset
from app.core.skill_evolution.grader_registry import GRADER_V2
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution.trace_sampling import (
    _candidate_kind,
    build_context_log,
    sample_executions,
)
from app.core.skill_evolution import model_review, review_eval

GRADER_V1 = "wiki-default-grader/v1"
SECRET_REF = "必须出现的秘密参考句-NEVER-LEAK-XYZ"


def _mini_ds(tmp_path: Path, *, version="wiki-offline-train-v1", grader=GRADER_V1):
    root = tmp_path / "dataset"
    src = root / "sources"
    ref = root / "references"
    src.mkdir(parents=True)
    ref.mkdir()
    (src / "a.md").write_text(
        "适用条件：室内安装。扭矩 25 N·m。电压 60V。关闭电源并断开高压回路。",
        encoding="utf-8")
    refs = {}
    for tid, extra in (
        ("t-pass", [{"id": "p1", "kind": "phrase", "text": "适用条件"}]),
        ("t-fail", [{"id": "p1", "kind": "phrase", "text": SECRET_REF}]),
        ("t-val", [{"id": "p1", "kind": "phrase", "text": "适用条件"}]),
        ("t-test", [{"id": "p1", "kind": "phrase", "text": "适用条件"}]),
    ):
        refs[tid] = {"expected_points": extra, "forbidden": ["100 N·m"]}
    (ref / "ref.json").write_text(json.dumps(refs, ensure_ascii=False), encoding="utf-8")
    tasks = []
    for tid, split, gid in (
        ("t-pass", "train", "g-train"),
        ("t-fail", "train", "g-train"),
        ("t-val", "val", "g-val"),
        ("t-test", "test", "g-test"),
    ):
        tasks.append({
            "task_id": tid, "dataset_version": version, "domain": "wiki_compile.default",
            "split": split, "group_id": gid, "input_snapshot_id": f"snap-{tid}",
            "instruction": "根据资料生成 Wiki。", "grader_version": grader,
            "reference_ref": "ref.json", "trigger": "manual_rebuild",
            "wiki_title": f"Wiki {tid}", "wiki_category": "资料",
            "sources": [{"doc_id": "d1", "title": "手册", "file": "a.md"}],
        })
    (root / "dataset.json").write_text(json.dumps({
        "dataset_version": version, "domain": "wiki_compile.default",
        "grader_version": grader, "tasks": tasks,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_dataset(root)


def _grade_doc(*, task_id, verdict, grader=GRADER_V1, fps=None, schema=tg.GRADE_SCHEMA):
    return {
        "schema": schema, "version": schema, "task_id": task_id, "split": "train",
        "grader_version": grader,
        "fingerprints": fps or {
            "dataset_json": "a" * 64, "reference_file": "b" * 64,
            "task_spec": "c" * 64, "grader_version": grader,
            "dataset_version": "wiki-offline-train-v1",
        },
        "verdict": verdict, "checks": [{"id": "p1", "passed": verdict == "pass"}],
        "pending": {"unresolved_ids": [], "evaluation_invalid": None,
                    "promotable": verdict == "pass"},
        "compile_ok": True, "created_at": "2026-09-09T00:00:00",
    }


def _meta(eid, task_id="t-fail", *, published=True, infra=False, grader=GRADER_V1):
    return {
        "execution_id": eid, "task_id": task_id, "split": "train",
        "dataset_version": "wiki-offline-train-v1",
        "grader_version": grader, "created_at": "2026-09-09T00:00:00",
        "run_status": "succeeded",
        "outcome": {
            "run_ok": True, "published": published,
            "failure_kind": "infra_or_model" if infra else None,
        },
        "candidate": {"revision_id": "rev1" if published else None},
    }


def _write_grade(root: Path, eid: str, doc: dict):
    run_dir = root / "runs" / eid
    run_dir.mkdir(parents=True, exist_ok=True)
    tg.atomic_write_grade(run_dir, doc)
    return run_dir


def _fps_for_dir(run_dir: Path, eid: str, candidate, outcome, *, grader=GRADER_V1,
                 seal: str, dataset_version="wiki-offline-train-v1"):
    from app.core.skill_evolution.contracts import canonical_json
    import hashlib
    return {
        "dataset_json": "a" * 64, "reference_file": "b" * 64,
        "task_spec": "c" * 64, "grader_version": grader,
        "dataset_version": dataset_version,
        "candidate_sha256": hashlib.sha256(
            canonical_json(candidate).encode("utf-8")).hexdigest(),
        "outcome_sha256": hashlib.sha256(
            canonical_json(outcome).encode("utf-8")).hexdigest(),
        "output_sha256": hashlib.sha256(
            (run_dir / "output.json").read_bytes()).hexdigest(),
        "execution_id": eid,
        "trace_seal": seal,
        "reviewer_identity": None,
        "reviewer_config_fingerprint": None,
    }


def _prepared_grade(root: Path, eid: str, task_id: str, verdict: str, *,
                    grader=GRADER_V1, published=True):
    cand = {"revision_id": "rev1" if published else None}
    outcome = {"run_ok": True, "published": published, "failure_kind": None}
    run_dir = root / "runs" / eid
    seal = _write_sealed_output(run_dir, eid, task_id, cand, outcome, grader=grader)
    fps = _fps_for_dir(run_dir, eid, cand, outcome, grader=grader, seal=seal)
    _write_grade(root, eid, _grade_doc(task_id=task_id, verdict=verdict,
                                       grader=grader, fps=fps))
    return _meta(eid, task_id, published=published, grader=grader)


def test_compile_ok_grade_fail_is_quality_failure(tmp_path):
    root = tmp_path / "lab"
    eid = "e-fail"
    meta = _prepared_grade(root, eid, "t-fail", "fail")
    assert tg.interpret_grade_kind(root, meta) == "quality_failure"
    assert _candidate_kind(root, meta) == "quality_failure"
    chosen = sample_executions(root, [meta])
    assert chosen and chosen[0]["execution_id"] == eid


def test_compile_ok_grade_pass_is_success(tmp_path):
    root = tmp_path / "lab"
    eid = "e-pass"
    meta = _prepared_grade(root, eid, "t-pass", "pass")
    assert tg.interpret_grade_kind(root, meta) == "success"
    assert _candidate_kind(root, meta) == "success"


def test_missing_grade_is_unknown_not_success(tmp_path):
    root = tmp_path / "lab"
    (root / "runs" / "e-none").mkdir(parents=True)
    meta = _meta("e-none", "t-pass")
    assert tg.interpret_grade_kind(root, meta) == "unknown"
    assert _candidate_kind(root, meta) != "success"
    assert sample_executions(root, [meta]) == []


def test_corrupt_or_mismatched_grade_is_unknown(tmp_path):
    root = tmp_path / "lab"
    run = root / "runs" / "e-bad"
    run.mkdir(parents=True)
    (run / "grade.json").write_text("{not-json", encoding="utf-8")
    assert tg.interpret_grade_kind(root, _meta("e-bad")) == "unknown"

    _write_grade(root, "e-wrong-task", _grade_doc(task_id="other", verdict="pass"))
    assert tg.interpret_grade_kind(root, _meta("e-wrong-task", "t-pass")) == "unknown"

    fps = _grade_doc(task_id="t-pass", verdict="pass")["fingerprints"]
    fps = dict(fps, dataset_version="tampered-version")
    _write_grade(root, "e-fp", _grade_doc(task_id="t-pass", verdict="pass", fps=fps))
    assert tg.interpret_grade_kind(root, _meta("e-fp", "t-pass")) == "unknown"

    _write_grade(root, "e-schema", _grade_doc(
        task_id="t-pass", verdict="pass", schema="not-a-schema"))
    assert tg.interpret_grade_kind(root, _meta("e-schema", "t-pass")) == "unknown"


def test_v2_needs_review_and_invalid_are_not_success(tmp_path):
    root = tmp_path / "lab"
    for verdict in ("needs_review", "invalid"):
        eid = f"e-{verdict}"
        meta = _prepared_grade(root, eid, "t-pass", verdict, grader=GRADER_V2)
        kind = tg.interpret_grade_kind(root, meta)
        assert kind == "unknown"
        assert sample_executions(root, [meta]) == []


def test_v2_missing_reviewer_does_not_fallback_v1(tmp_path, monkeypatch):
    ds = _mini_ds(tmp_path)
    called = []
    monkeypatch.setattr(tg, "grade_v1", lambda **_k: called.append("v1") or {
        "verdict": "pass", "item_results": [], "compile_ok": True})
    task = ds.task("t-pass")
    with pytest.raises(tg.TrainGradeError, match="不回退"):
        tg.dispatch_grade(
            dataset=ds, task=SimpleNamespace(**{**task.__dict__,
                                                "grader_version": GRADER_V2}),
            candidate={"sections": []}, outcome={"run_ok": True, "published": True},
            grader_version=GRADER_V2, reviewer=None)
    assert called == []


def test_train_grade_does_not_leak_reference(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    task = ds.task("t-fail")
    eid = "exec-noleak"
    run_dir = lab / "runs" / eid
    run_dir.mkdir(parents=True)
    candidate = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内。扭矩 25 N·m。"}]}
    outcome = {"run_ok": True, "published": True, "failure_kind": None}
    _write_sealed_output(run_dir, eid, "t-fail", candidate, outcome)
    doc = tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=candidate, outcome=outcome)
    blob = json.dumps(doc, ensure_ascii=False)
    assert SECRET_REF not in blob
    assert "authorization" not in blob.lower()
    assert "api_key" not in blob.lower()
    assert doc["verdict"] == "fail"
    assert doc["split"] == "train"
    assert "fingerprints" in doc and doc["fingerprints"]["reference_file"]


def test_resume_reuses_grade_and_does_not_repeat_review(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    task = ds.task("t-pass")
    candidate = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。"}]}
    outcome = {"run_ok": True, "published": True}
    _write_sealed_output(lab / "runs" / "e-resume", "e-resume", "t-pass",
                         candidate, outcome)
    first = tg.grade_train_execution(
        lab, ds, task, "e-resume", grader_version=GRADER_V1,
        candidate=candidate, outcome=outcome)
    second = tg.grade_train_execution(
        lab, ds, task, "e-resume", grader_version=GRADER_V1,
        candidate=candidate, outcome=outcome)
    assert first["created_at"] == second["created_at"]
    assert first["verdict"] == second["verdict"]
    fps = tg.grade_fingerprints(
        ds, task, GRADER_V1, candidate=candidate, outcome=outcome,
        execution_id="e-resume", run_dir=lab / "runs" / "e-resume")
    assert tg.grade_is_reusable(
        first, task_id=task.task_id, grader_version=GRADER_V1,
        fingerprints=fps, candidate=candidate, outcome=outcome, reviewer=None,
        run_dir=lab / "runs" / "e-resume")
    assert not tg.grade_is_reusable(
        first, task_id=task.task_id, grader_version=GRADER_V1,
        fingerprints=fps, candidate=candidate, outcome=outcome, reviewer=None)


def test_candidate_or_outcome_change_does_not_reuse(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    task = ds.task("t-pass")
    cand = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。"}]}
    outcome = {"run_ok": True, "published": True}
    _write_sealed_output(lab / "runs" / "e-bind", "e-bind", "t-pass", cand, outcome)
    first = tg.grade_train_execution(
        lab, ds, task, "e-bind", grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    changed = tg.grade_train_execution(
        lab, ds, task, "e-bind", grader_version=GRADER_V1,
        candidate={"revision_id": "changed"}, outcome=outcome)
    assert not tg.grade_is_reusable(
        first, task_id=task.task_id, grader_version=GRADER_V1,
        fingerprints=tg.grade_fingerprints(
            ds, task, GRADER_V1, candidate={"revision_id": "changed"},
            outcome=outcome, execution_id="e-bind",
            run_dir=lab / "runs" / "e-bind"),
        candidate={"revision_id": "changed"}, outcome=outcome, reviewer=None)
    assert changed.get("fingerprints", {}).get("candidate_sha256") != \
        first.get("fingerprints", {}).get("candidate_sha256")
    out2 = dict(outcome, published=False)
    third = tg.grade_train_execution(
        lab, ds, task, "e-bind-out", grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    fourth = tg.grade_train_execution(
        lab, ds, task, "e-bind-out", grader_version=GRADER_V1,
        candidate=cand, outcome=out2)
    assert third.get("fingerprints", {}).get("outcome_sha256") != \
        fourth.get("fingerprints", {}).get("outcome_sha256")


def _write_sealed_output(run_dir: Path, eid: str, task_id: str, candidate, outcome,
                         *, grader=GRADER_V1):
    from app.core.skill_evolution.contracts import canonical_json
    import hashlib
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"candidate": candidate, "outcome": outcome}
    (run_dir / "output.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {
        "execution_id": eid, "task_id": task_id, "split": "train",
        "dataset_version": "wiki-offline-train-v1", "grader_version": grader,
        "sealed": True, "outcome": outcome, "candidate": candidate,
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps({"seq": 1, "kind": "start"}) + "\n", encoding="utf-8")
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    seal = hashlib.sha256(
        (canonical_json(meta) + "\n" + events).encode("utf-8")).hexdigest()
    (run_dir / "seal.sha256").write_text(f"sha256:{seal}\n", encoding="utf-8")
    return seal


def test_tampered_output_or_seal_is_unknown(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    task = ds.task("t-pass")
    cand = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。"}]}
    outcome = {"run_ok": True, "published": True}
    eid = "e-tamp"
    run_dir = lab / "runs" / eid
    _write_sealed_output(run_dir, eid, "t-pass", cand, outcome)
    tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    meta = _meta(eid, "t-pass")
    assert tg.interpret_grade_kind(lab, meta) == "success"
    payload = json.loads((run_dir / "output.json").read_text(encoding="utf-8"))
    payload["candidate"]["revision_id"] = "tampered"
    (run_dir / "output.json").write_text(json.dumps(payload), encoding="utf-8")
    assert tg.interpret_grade_kind(lab, meta) == "unknown"
    _write_sealed_output(run_dir, eid, "t-pass", cand, outcome)
    tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    (run_dir / "seal.sha256").write_text("sha256:" + ("0" * 64) + "\n", encoding="utf-8")
    assert tg.interpret_grade_kind(lab, meta) == "unknown"


def test_tampered_grade_verdict_or_checks_not_reusable_unknown(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    task = ds.task("t-pass")
    cand = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。"}]}
    outcome = {"run_ok": True, "published": True}
    eid = "e-grade-tamp"
    run_dir = lab / "runs" / eid
    _write_sealed_output(run_dir, eid, "t-pass", cand, outcome)
    first = tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    meta = _meta(eid, "t-pass")
    assert tg.interpret_grade_kind(lab, meta) == "success"
    doc = json.loads((run_dir / "grade.json").read_text(encoding="utf-8"))
    blob = json.dumps(doc, ensure_ascii=False)
    assert SECRET_REF not in blob
    assert "Authorization" not in blob
    assert "stub-key" not in blob
    seal = (run_dir / "grade.sha256").read_text(encoding="utf-8")
    assert seal.startswith("sha256:")
    assert SECRET_REF not in seal
    assert "Authorization" not in seal
    doc["verdict"] = "fail"
    (run_dir / "grade.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    fps = tg.grade_fingerprints(
        ds, task, GRADER_V1, candidate=cand, outcome=outcome,
        execution_id=eid, run_dir=run_dir)
    assert not tg.grade_is_reusable(
        json.loads((run_dir / "grade.json").read_text(encoding="utf-8")),
        task_id=task.task_id, grader_version=GRADER_V1, fingerprints=fps,
        candidate=cand, outcome=outcome, reviewer=None, run_dir=run_dir)
    assert tg.interpret_grade_kind(lab, meta) == "unknown"
    _write_sealed_output(run_dir, eid, "t-pass", cand, outcome)
    tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    doc = json.loads((run_dir / "grade.json").read_text(encoding="utf-8"))
    doc["checks"] = [{"id": "injected", "passed": True, "status": "pass"}]
    (run_dir / "grade.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    assert tg.interpret_grade_kind(lab, meta) == "unknown"
    doc["pending"] = {"unresolved_ids": ["x"], "promotable": True}
    doc["reviewer_identity"] = "forged"
    (run_dir / "grade.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    assert tg.interpret_grade_kind(lab, meta) == "unknown"


def test_v2_reviewer_fingerprint_change_or_delete_not_reuse(tmp_path):
    ds = _mini_ds(tmp_path, grader=GRADER_V2)
    task = SimpleNamespace(**{**ds.task("t-pass").__dict__, "grader_version": GRADER_V2})
    cand = {"sections": [{"section_type": "facts", "version_label": "unversioned",
                          "content": "x"}]}
    outcome = {"run_ok": True, "published": True}

    class R:
        identity = "rev-a"
        config_fingerprint = "fp-a"
        prompt_version = "stub-v1"

    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    (lab / "runs" / "e-rv").mkdir(parents=True)
    (lab / "runs" / "e-rv" / "output.json").write_text(
        json.dumps({"candidate": cand, "outcome": outcome}), encoding="utf-8")
    first = tg.build_grade_document(
        task=task, grader_version=GRADER_V2,
        fingerprints={"dataset_json": "a" * 64, "reference_file": "b" * 64,
                      "task_spec": "c" * 64, "grader_version": GRADER_V2,
                      "dataset_version": ds.dataset_version,
                      "candidate_sha256": "c1", "outcome_sha256": "o1",
                      "output_sha256": "u1", "execution_id": "e-rv",
                      "trace_seal": "s1"},
        grading={"verdict": "pass", "checks": [], "pending": {},
                 "compile_ok": True},
        reviewer=R())
    other = SimpleNamespace(identity="rev-b", config_fingerprint="fp-a",
                            prompt_version="stub-v1")
    assert not tg.grade_document_structure_ok(
        first, task_id="t-pass", grader_version=GRADER_V2,
        fingerprints=first["fingerprints"], candidate=cand, outcome=outcome,
        reviewer=other)
    stripped = dict(first)
    stripped.pop("reviewer_identity", None)
    stripped.pop("reviewer_config_fingerprint", None)
    assert not tg.grade_document_structure_ok(
        stripped, task_id="t-pass", grader_version=GRADER_V2,
        fingerprints=first["fingerprints"], candidate=cand, outcome=outcome,
        reviewer=R())
    assert not tg.grade_is_reusable(
        first, task_id="t-pass", grader_version=GRADER_V2,
        fingerprints=first["fingerprints"], candidate=cand, outcome=outcome,
        reviewer=R())


def test_crash_a_executed_ungraded_only_grades(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    task = ds.task("t-pass")
    cand = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。关闭电源并断开高压回路。"}]}
    outcome = {"run_ok": True, "published": True}
    eid = tg.stable_execution_id("run_crash", 1, "t-pass")
    run_dir = lab / "runs" / eid
    _write_sealed_output(run_dir, eid, "t-pass", cand, outcome)
    exec_calls = []

    def boom_exec(messages, context="", timeout=120.0):
        exec_calls.append(1)
        raise AssertionError("不得重复执行模型")

    state = [{"task_id": "t-pass", "execution_id": eid, "status": "executed"}]
    ids = tg.ensure_train_tasks_graded(
        lab, ds, ["t-pass"], FrozenSkillSet.disabled(), "faithful",
        executor_runner=boom_exec, grader_version=GRADER_V1,
        run_id="run_crash", iteration=1, existing_state=state)
    assert exec_calls == []
    assert ids == [eid]
    assert (run_dir / "grade.json").is_file()


def test_crash_b_graded_not_advanced_zero_calls(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    cand = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。关闭电源并断开高压回路。"}]}
    outcome = {"run_ok": True, "published": True}
    eid = tg.stable_execution_id("run_crash", 1, "t-pass")
    run_dir = lab / "runs" / eid
    _write_sealed_output(run_dir, eid, "t-pass", cand, outcome)
    tg.grade_train_execution(
        lab, ds, ds.task("t-pass"), eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    exec_calls = []
    review_calls = []

    def boom_exec(messages, context="", timeout=120.0):
        exec_calls.append(1)
        raise AssertionError("executor")

    class BoomRev:
        identity = "r"
        config_fingerprint = "f"
        def review(self, **kw):
            review_calls.append(1)
            raise AssertionError("reviewer")

    state = [{"task_id": "t-pass", "execution_id": eid, "status": "graded"}]
    ids = tg.ensure_train_tasks_graded(
        lab, ds, ["t-pass"], FrozenSkillSet.disabled(), "faithful",
        executor_runner=boom_exec, grader_version=GRADER_V1,
        reviewer=BoomRev(), run_id="run_crash", iteration=1,
        existing_state=state)
    assert exec_calls == [] and review_calls == []
    assert ids == [eid]


def test_stable_id_not_shared_across_runs_or_iterations():
    a = tg.stable_execution_id("runA", 1, "t-pass")
    b = tg.stable_execution_id("runB", 1, "t-pass")
    c = tg.stable_execution_id("runA", 2, "t-pass")
    assert a != b and a != c and len(a) == 32


def test_grade_document_has_evidence_hashes_no_secrets(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    task = ds.task("t-fail")
    cand = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内。扭矩 25 N·m。"}]}
    outcome = {"run_ok": True, "published": True}
    _write_sealed_output(lab / "runs" / "e-hash", "e-hash", "t-fail", cand, outcome)
    doc = tg.grade_train_execution(
        lab, ds, task, "e-hash", grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    fp = doc["fingerprints"]
    for key in ("candidate_sha256", "outcome_sha256", "output_sha256",
                "execution_id", "trace_seal", "grader_version"):
        assert fp.get(key)
    assert doc.get("reviewer_identity") in (None, "n/a", "not_applicable")
    blob = json.dumps(doc, ensure_ascii=False)
    assert SECRET_REF not in blob
    assert "Authorization" not in blob
    assert "stub-key" not in blob



def test_v2_resume_does_not_repeat_reviewer_budget(tmp_path):
    root = tmp_path / "v2ds"
    src = root / "sources"
    ref = root / "references"
    src.mkdir(parents=True)
    ref.mkdir()
    (src / "a.md").write_text("电压 220V；温度 25°C。操作步骤：安装本体。\n内容编号 X1。",
                              encoding="utf-8")
    (ref / "ref.json").write_text(json.dumps({
        "t1": {"v2_spec": {
            "claims": [{"cid": "c1", "subject": "电压", "predicate": "为",
                        "object": "220", "unit": "V"}],
            "coverage": [{"topic": "操作步骤"}],
        }},
    }), encoding="utf-8")
    (root / "dataset.json").write_text(json.dumps({
        "dataset_version": "wiki-offline-v2",
        "domain": "wiki_compile.default",
        "grader_version": GRADER_V2,
        "tasks": [{
            "task_id": "t1", "dataset_version": "wiki-offline-v2",
            "domain": "wiki_compile.default", "split": "train",
            "group_id": "g-train", "input_snapshot_id": "s1",
            "instruction": "生成 Wiki", "grader_version": GRADER_V2,
            "reference_ref": "ref.json", "trigger": "manual_rebuild",
            "wiki_title": "T1", "wiki_category": "资料",
            "sources": [{"doc_id": "d1", "title": "a", "file": "a.md"}],
        }],
    }), encoding="utf-8")
    ds = load_dataset(root)
    task = ds.task("t1")
    cand = {"sections": [{"section_type": "facts", "version_label": "unversioned",
                          "content": "电压 220V；温度 25°C。操作步骤：安装本体。内容编号 X1。"}]}
    spec = review_eval.load_v2_spec(root, task)
    base = __import__("app.core.skill_evolution.grader_v2", fromlist=["evaluate"]).evaluate(
        cand, spec)
    ids = [it["id"] for it in base["items"] if it["status"] == "needs_review"]

    class Counting(model_review.StubSemanticReviewer):
        def __init__(self):
            script = {"t1": {"items": [
                {"check_id": i, "verdict": "pass", "reason": "与来源一致"}
                for i in ids]}}
            super().__init__(script, prompt_version="stub-v1")
            self.calls = 0

        def review(self, **kw):
            self.calls += 1
            return super().review(**kw)

    reviewer = Counting()
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    _write_sealed_output(lab / "runs" / "e-v2", "e-v2", "t1", cand,
                         {"run_ok": True, "published": True}, grader=GRADER_V2)
    tg.grade_train_execution(
        lab, ds, task, "e-v2", grader_version=GRADER_V2,
        candidate=cand, outcome={"run_ok": True, "published": True},
        reviewer=reviewer)
    assert reviewer.calls == 1
    tg.grade_train_execution(
        lab, ds, task, "e-v2", grader_version=GRADER_V2,
        candidate=cand, outcome={"run_ok": True, "published": True},
        reviewer=reviewer)
    assert reviewer.calls == 1


def test_real_compile_pass_and_fail_grades_and_maintainer_sees_failure(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")

    def stub_fail(messages, context="", timeout=120.0):
        return {"summary": "s", "content": "适用条件：室内。扭矩 25 N·m。电压 60V。"}

    def stub_pass(messages, context="", timeout=120.0):
        return {"summary": "s",
                "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。关闭电源并断开高压回路。"}

    fail_ids = tg.ensure_train_tasks_graded(
        lab, ds, ["t-fail"], FrozenSkillSet.disabled(), "faithful",
        executor_runner=stub_fail, grader_version=GRADER_V1)
    pass_ids = tg.ensure_train_tasks_graded(
        lab, ds, ["t-pass"], FrozenSkillSet.disabled(), "faithful",
        executor_runner=stub_pass, grader_version=GRADER_V1)
    fail_meta = json.loads(
        (lab / "runs" / fail_ids[0] / "meta.json").read_text(encoding="utf-8"))
    pass_meta = json.loads(
        (lab / "runs" / pass_ids[0] / "meta.json").read_text(encoding="utf-8"))
    assert fail_meta["outcome"].get("published") or fail_meta.get("run_status")
    assert _candidate_kind(lab, fail_meta) == "quality_failure"
    assert _candidate_kind(lab, pass_meta) == "success"
    log = build_context_log(lab, fail_meta)
    text = log.get("text") or "\n".join(log.get("lines") or []) if isinstance(log, dict) else str(log)
    if isinstance(log, dict) and not text.strip():
        text = json.dumps(log, ensure_ascii=False)
    assert "fail" in text.lower() or "quality_failure" in text or "grade_verdict=fail" in text
    assert SECRET_REF not in text
    sampled = sample_executions(lab, [fail_meta, pass_meta])
    kinds = [_candidate_kind(lab, m) for m in sampled]
    assert "quality_failure" in kinds


def test_infra_kind_and_safe_summary(tmp_path):
    root = tmp_path / "lab"
    _write_grade(root, "e-infra", _grade_doc(task_id="t-fail", verdict="infra"))
    meta = _meta("e-infra", infra=True)
    assert tg.interpret_grade_kind(root, meta) == "infra"
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab2")
    task = ds.task("t-fail")
    cand = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内。扭矩 25 N·m。"}]}
    outcome = {"run_ok": True, "published": True}
    _write_sealed_output(lab / "runs" / "e-fail-s", "e-fail-s", "t-fail", cand, outcome)
    tg.grade_train_execution(
        lab, ds, task, "e-fail-s", grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    summary = tg.safe_grade_summary(lab, _meta("e-fail-s", "t-fail"))
    blob = json.dumps(summary, ensure_ascii=False)
    assert SECRET_REF not in blob
    assert summary["verdict"] == "fail"
    assert summary["kind"] == "quality_failure"


def _tamper_grade_field(run_dir: Path, **fields):
    doc = json.loads((run_dir / "grade.json").read_text(encoding="utf-8"))
    doc.update(fields)
    (run_dir / "grade.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    return doc


def test_grade_train_execution_does_not_reuse_tampered_or_unsealed(tmp_path):
    ds = _mini_ds(tmp_path)
    lab = runenv.ensure_experiment_root(tmp_path / "lab")
    task = ds.task("t-pass")
    cand = {"revision_id": "r1", "sections": [
        {"section_type": "facts", "version_label": "unversioned",
         "content": "适用条件：室内安装。扭矩 25 N·m。电压 60V。"}]}
    outcome = {"run_ok": True, "published": True}
    eid = "e-reuse-tamp"
    run_dir = lab / "runs" / eid
    _write_sealed_output(run_dir, eid, "t-pass", cand, outcome)
    first = tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    assert first["verdict"] == "pass"
    meta = _meta(eid, "t-pass")
    _tamper_grade_field(run_dir, verdict="fail")
    closed = tg.safe_grade_summary(lab, meta)
    assert closed["kind"] == "unknown"
    assert closed["verdict"] is None
    assert closed["checks_failed"] == []
    assert closed["pending"] is True
    repaired = tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    assert repaired["verdict"] == "pass"
    assert repaired["verdict"] != "fail"
    assert tg.verify_grade_integrity(run_dir)
    live = json.loads((run_dir / "grade.json").read_text(encoding="utf-8"))
    assert live["verdict"] == "pass"

    _tamper_grade_field(run_dir, checks=[{"id": "injected", "passed": True}])
    assert tg.safe_grade_summary(lab, meta)["verdict"] is None
    tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    live = json.loads((run_dir / "grade.json").read_text(encoding="utf-8"))
    assert live.get("checks") != [{"id": "injected", "passed": True}]

    _tamper_grade_field(run_dir, pending={"unresolved_ids": ["x"], "promotable": True})
    assert tg.safe_grade_summary(lab, meta)["kind"] == "unknown"
    tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)

    _tamper_grade_field(run_dir, reviewer_identity="forged")
    assert tg.safe_grade_summary(lab, meta)["verdict"] is None
    tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)

    (run_dir / "grade.sha256").unlink()
    missing = tg.safe_grade_summary(lab, meta)
    assert missing["kind"] == "unknown"
    assert missing["verdict"] is None
    assert missing["pending"] is True
    tg.grade_train_execution(
        lab, ds, task, eid, grader_version=GRADER_V1,
        candidate=cand, outcome=outcome)
    assert tg.verify_grade_integrity(run_dir)
