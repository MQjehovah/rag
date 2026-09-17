"""阶段 1/2 最小 CLI（可脚本化入口）。

技能/绑定命令（阶段 2）：
  seed           --root <dir> [--skill-dir <dir>] [--source builtin_seed|manual_seed]
  add-version    --root <dir> --skill-dir <dir> [--parent-version <id>] [--source manual_seed]
  versions       --root <dir> [--skill-id <id>]
  show-version   --root <dir> --version-id <id>
  bind           --root <dir> --workspace <ws> [--version-id <id> | --empty]
                 [--domain wiki_compile.default] [--kind experiment|business]
  unbind         --root <dir> --workspace <ws> [--domain ...] [--kind experiment|business]
  bindings       --root <dir> [--kind ...]

执行命令（技能选择显式、优先级高 → 低）：
  run  ... [--skills-version <id>]... | [--empty-skills] | [--binding-workspace <ws>]
       （都不给 → 无指令，legacy 语义）
  rerun ... （默认按原执行记录技能集合重放：原版本/哈希，绝不自动用最新）

示例（backend 目录、已激活 .venv）：
  python -m app.core.skill_evolution.cli seed --root eval/wiki_evolution/runtime
  python -m app.core.skill_evolution.cli bind --root eval/wiki_evolution/runtime \
      --workspace ws_demo --version-id default:0001
  python -m app.core.skill_evolution.cli run --root eval/wiki_evolution/runtime \
      --dataset eval/wiki_evolution/datasets/wiki-default-v1 --task wiki-default-001 \
      --binding-workspace ws_demo
  python -m app.core.skill_evolution.cli versions --root eval/wiki_evolution/runtime
  python -m app.core.skill_evolution.cli rerun --root eval/wiki_evolution/runtime \
      --dataset eval/wiki_evolution/datasets/wiki-default-v1 --execution-id <id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.skill_evolution import runenv, skill_store
from app.core.skill_evolution.adapter import build_snapshot_for_task, run_one
from app.core.skill_evolution.contracts import DatasetError, load_dataset
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.grader import GRADER_VERSION, grade
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution import experience_store as exp
from app.core.skill_evolution import trace_sampling as sampling
from app.core.skill_evolution import maintainer as maint
from app.core.skill_evolution import proposer as prop
from app.core.skill_evolution import gating as gate
from app.core.skill_evolution import orchestrator as orch
from app.core.skill_evolution import experiment7 as ex7
from app.core.skill_evolution.runner import KNOWN_PROFILES
from app.core.skill_evolution.snapshot import SnapshotStore
from app.core.skill_evolution.trace import TraceError, iter_events, list_sealed_runs, load_meta


def _dataset(path: str) -> Path:
    p = Path(path).resolve()
    if not p.is_dir():
        raise DatasetError(f"数据集目录不存在: {p}")
    return p


def _store_session(root: Path):
    root = runenv.ensure_experiment_root(root)
    return skill_store.session_for(root)


# ---------------------------------------------------------------------------
# validate-dataset（阶段 1）
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    dataset_dir = _dataset(args.dataset)
    spec = load_dataset(dataset_dir)
    summary = spec.split_summary()
    print(f"dataset_version: {spec.dataset_version}")
    print(f"domain: {spec.domain}   grader_version: {spec.grader_version}")
    print(f"tasks: {len(spec.tasks)}")
    for split, info in summary["splits"].items():
        print(f"  {split}: {info['task_count']} tasks, groups={sorted(info['group_ids'])}")
    label_hashes: dict[str, str] = {}
    for t in spec.tasks:
        payload = build_snapshot_for_task(dataset_dir, t)
        prev = label_hashes.get(t.input_snapshot_id)
        if prev is not None and prev != payload.content_hash():
            raise DatasetError(
                f"snapshot_id {t.input_snapshot_id!r} 被不同内容复用"
                f"（{prev[:12]} vs {payload.content_hash()[:12]}）")
        label_hashes[t.input_snapshot_id] = payload.content_hash()
    print(f"snapshot labels: {len(label_hashes)}（内容哈希一致）")
    refs = {t.reference_ref for t in spec.tasks}
    for ref in sorted(refs):
        from app.core.skill_evolution.contracts import load_reference
        data = load_reference(dataset_dir, ref)
        missing = [t.task_id for t in spec.tasks
                   if t.reference_ref == ref and t.task_id not in data]
        if missing:
            raise DatasetError(f"reference {ref} 缺少任务: {missing}")
    print("OK")
    return 0


# ---------------------------------------------------------------------------
# 技能版本 / 绑定
# ---------------------------------------------------------------------------


def cmd_seed(args: argparse.Namespace) -> int:
    db = _store_session(Path(args.root))
    try:
        skill_dir = Path(args.skill_dir).resolve()
        pkg = skill_store.load_package(skill_dir, source_type=args.source)
        version_id = skill_store.import_seed(db, pkg)
        print(f"seed ok: skill={pkg.skill_id} version={version_id} "
              f"hash={pkg.content_hash()[:16]} source={pkg.source_type}")
    finally:
        db.close()
    return 0


def cmd_add_version(args: argparse.Namespace) -> int:
    db = _store_session(Path(args.root))
    try:
        pkg = skill_store.load_package(Path(args.skill_dir).resolve(),
                                       source_type=args.source)
        parent = args.parent_version
        if parent:
            skill_store.get_version(db, parent)  # 存在性/损坏校验
        version_id = skill_store.add_version(db, pkg, parent_version_id=parent)
        print(f"new version: {version_id} (hash={pkg.content_hash()[:16]})")
    finally:
        db.close()
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    db = _store_session(Path(args.root))
    try:
        rows = skill_store.list_versions(db, skill_id=args.skill_id)
        for r in rows:
            print(f"{r.version_id}  skill={r.skill_id} seq={r.seq} "
                  f"domain={r.domain} runtime={r.runtime_ref} "
                  f"source={r.source_type} parent={r.parent_version_id or '-'} "
                  f"hash={r.content_hash[:16]}")
        print(f"total={len(rows)}")
    finally:
        db.close()
    return 0


def cmd_show_version(args: argparse.Namespace) -> int:
    db = _store_session(Path(args.root))
    try:
        r = skill_store.get_version(db, args.version_id)
        print(f"version_id={r.version_id} skill_id={r.skill_id} seq={r.seq}")
        print(f"domain={r.domain} runtime_ref={r.runtime_ref}")
        print(f"parent={r.parent_version_id or '-'} source={r.source_type}")
        print(f"content_hash={r.content_hash}")
        print("---- SKILL.md ----")
        print(r.skill_md)
    finally:
        db.close()
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    db = _store_session(Path(args.root))
    try:
        if args.empty:
            skill_store.bind(db, kind=args.kind, workspace_id=args.workspace,
                             domain=args.domain, empty=True)
            print(f"bound empty (explicit no-skill): {args.kind}/{args.workspace}/{args.domain}")
        else:
            skill_store.bind(db, kind=args.kind, workspace_id=args.workspace,
                             domain=args.domain, version_id=args.version_id)
            print(f"bound {args.kind}/{args.workspace}/{args.domain} -> {args.version_id}")
    finally:
        db.close()
    return 0


def cmd_unbind(args: argparse.Namespace) -> int:
    db = _store_session(Path(args.root))
    try:
        removed = skill_store.unbind(db, kind=args.kind, workspace_id=args.workspace,
                                     domain=args.domain)
        print("unbound" if removed else "no binding to remove")
    finally:
        db.close()
    return 0


def cmd_bindings(args: argparse.Namespace) -> int:
    db = _store_session(Path(args.root))
    try:
        rows = skill_store.list_bindings(db, kind=args.kind)
        if not rows:
            print("(none)")
        for r in rows:
            target = r["version_id"] or ("<empty>" if r["set_kind"] == "empty" else "-")
            print(f"{r['kind']}  {r['workspace_id']}  {r['domain']}  -> {target}")
    finally:
        db.close()
    return 0


# ---------------------------------------------------------------------------
# run / rerun
# ---------------------------------------------------------------------------


def _select_tasks(spec, args) -> list:
    if args.task:
        return [spec.task(args.task)]
    if args.split:
        picked = [t for t in spec.tasks if t.split == args.split]
        if not picked:
            raise DatasetError(f"split {args.split} 无任务")
        return picked
    return list(spec.tasks)


def _resolve_skills(root: Path, args) -> FrozenSkillSet:
    """技能选择优先级：--skills-version > --empty-skills > --binding-workspace > none。"""
    if getattr(args, "skills_version", None):
        db = skill_store.session_for(root)
        try:
            return skill_store.freeze_versions(db, list(args.skills_version))
        finally:
            db.close()
    if getattr(args, "empty_skills", False):
        return FrozenSkillSet.empty()
    binding_ws = getattr(args, "binding_workspace", None)
    if binding_ws:
        db = skill_store.session_for(root)
        try:
            resolved = skill_store.resolve_binding(
                db, kind="experiment", workspace_id=binding_ws,
                domain=skill_store.DEFAULT_DOMAIN)
        finally:
            db.close()
        if resolved["mode"] == "none":
            raise DatasetError(f"workspace {binding_ws} 无实验绑定（先 bind 或显式选技能）")
        if resolved["mode"] == "empty":
            return FrozenSkillSet.empty()
        return FrozenSkillSet.from_versions((resolved["version"],))
    return FrozenSkillSet.disabled()


def _run_and_grade(root: Path, dataset_dir: Path, task, profile: str,
                   plan: FrozenSkillSet, snapshot=None) -> dict:
    spec = load_dataset(dataset_dir)
    res = run_one(root, spec, task, profile=profile, snapshot=snapshot, skills=plan)
    grading = grade(
        outcome=res["meta"]["outcome"],
        candidate=res["candidate"],
        reference_ref=task.reference_ref,
        dataset_dir=dataset_dir,
        task_id=task.task_id,
    )
    grade_path = Path(res["run_dir"]) / "grade.json"
    grade_path.write_text(json.dumps(grading, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    res["grade"] = grading
    return res


def cmd_run(args: argparse.Namespace) -> int:
    root = runenv.ensure_experiment_root(Path(args.root))
    dataset_dir = _dataset(args.dataset)
    spec = load_dataset(dataset_dir)
    tasks = _select_tasks(spec, args)
    plan = _resolve_skills(root, args)
    failures = 0
    for task in tasks:
        try:
            res = _run_and_grade(root, dataset_dir, task, args.profile, plan)
        except (SkillEvolutionError, DatasetError) as exc:
            failures += 1
            print(f"FAIL {task.task_id}: {exc}")
            continue
        out = res["meta"]["outcome"]
        verdict = res["grade"]["verdict"]
        passed = sum(1 for r in res["grade"]["item_results"] if r["passed"])
        total = len(res["grade"]["item_results"])
        stats = res["meta"].get("skill_stats") or {}
        print(
            f"exec {res['execution_id']} task={task.task_id} "
            f"run={out['run_status']} published={out['published']} "
            f"verdict={verdict} checks={passed}/{total} "
            f"skills={plan.summary()} inj_events={stats.get('events', 0)} "
            f"inj_true={stats.get('injected', 0)} profile={args.profile}"
        )
    return 1 if failures else 0


def cmd_rerun(args: argparse.Namespace) -> int:
    root = runenv.ensure_experiment_root(Path(args.root))
    dataset_dir = _dataset(args.dataset)
    spec = load_dataset(dataset_dir)
    old = load_meta(Path(args.root) / "runs" / args.execution_id)
    task = spec.task(old["task_id"])
    snapshot = SnapshotStore(root).load(old["input_snapshot_id"])
    skills = old.get("skills") or {}
    mode = skills.get("mode", "none")
    if mode == "versions":
        ids = tuple(skills.get("version_ids") or [])
        hashes = tuple(skills.get("content_hashes") or [])
        db = skill_store.session_for(root)
        try:
            # 重放默认用原执行记录的精确技能版本与内容哈希；缺失/失配明确失败。
            plan = skill_store.freeze_versions(db, ids, expected_hashes=hashes)
        finally:
            db.close()
        print(f"rerun skills: frozen original versions {ids} (hash 校验通过)")
    elif mode == "empty":
        plan = FrozenSkillSet.empty()
        print("rerun skills: explicit empty set（原记录无注入）")
    else:
        plan = FrozenSkillSet.disabled()
        print("rerun skills: legacy no-skill 语义（原执行未注入指令；不自动补入种子）")
    res = _run_and_grade(root, dataset_dir, task, args.profile, plan, snapshot=snapshot)
    print(f"rerun exec {res['execution_id']} task={task.task_id} "
          f"(原 execution {args.execution_id} 未改动)")
    return 0


# ---------------------------------------------------------------------------
# show / report
# ---------------------------------------------------------------------------


def cmd_show(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    runs = list_sealed_runs(root)
    if args.execution_id:
        runs = [r for r in runs if r["execution_id"] == args.execution_id]
        if not runs:
            print(f"未找到 execution: {args.execution_id}")
            return 1
    for r in runs:
        meta = r["meta"]
        skills = meta.get("skills") or {"mode": meta.get("skill_instruction_version") and "versions" or "none"}
        print(f"== {r['execution_id']} ==")
        print(f"  task={meta.get('task_id')} split={meta.get('split')} "
              f"profile={meta.get('model', {}).get('profile')}")
        print(f"  skills={json.dumps(skills, ensure_ascii=False)}")
        print(f"  run_status={meta.get('run_status')} "
              f"outcome_kind={meta.get('outcome', {}).get('failure_kind')} "
              f"published={meta.get('candidate', {}).get('revision_id') is not None}")
        print(f"  model_calls={meta.get('usage', {}).get('model_calls')} "
              f"inject_events={meta.get('skill_stats', {}).get('events')} "
              f"inject_true={meta.get('skill_stats', {}).get('injected')}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    rows = list_sealed_runs(root)
    lines = [
        "# WikiSkill 阶段 1/2 执行报告（离线模拟基线）",
        "",
        f"- grader_version: `{GRADER_VERSION}`",
        f"- runs: {len(rows)}",
        "",
        "| execution | task | split | profile | skills | run | published | verdict | checks | model_calls |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        meta = r["meta"]
        exec_id = r["execution_id"]
        run_dir = Path(root) / "runs" / exec_id
        grade_data = None
        gp = run_dir / "grade.json"
        if gp.is_file():
            try:
                grade_data = json.loads(gp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                grade_data = None
        verdict = grade_data.get("verdict") if grade_data else "-"
        checks = ""
        if grade_data:
            items = grade_data.get("item_results") or []
            checks = f"{sum(1 for i in items if i['passed'])}/{len(items)}"
        skills = (meta.get("skills") or {}).get("mode", "none")
        if (meta.get("skills") or {}).get("version_ids"):
            skills += ":" + ",".join((meta.get("skills") or {}).get("version_ids"))
        lines.append(
            f"| {exec_id[:12]} | {meta.get('task_id')} | {meta.get('split')} | "
            f"{meta.get('model', {}).get('profile')} | {skills} | "
            f"{meta.get('run_status')} | "
            f"{meta.get('candidate', {}).get('revision_id') is not None} | "
            f"{verdict} | {checks} | {meta.get('usage', {}).get('model_calls')} |"
        )
    text = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"报告已写入: {args.output}")
    else:
        print(text)
    return 0


# ---------------------------------------------------------------------------
# 技能提议（阶段 4）
# ---------------------------------------------------------------------------


def cmd_propose_check(args: argparse.Namespace) -> int:
    dataset_dir = _dataset(args.dataset)
    spec = load_dataset(dataset_dir)
    root = Path(args.root).resolve()
    ws = sampling.group_workspace_id(args.workspace or spec.tasks[0].group_id)
    metas = sampling.list_authorized_train_meta(root, spec, ws)
    task_ids = {m["task_id"] for m in metas}
    print(f"workspace={ws} dataset={spec.dataset_version} "
          f"authorized_executions={len(metas)} distinct_tasks={len(task_ids)}")
    for m in metas:
        print(f"  {m['execution_id']} task={m.get('task_id')}")
    ok = len(metas) >= 4 and len(task_ids) >= 4
    print("PRECHECK_" + ("OK（≥4 条不同训练轨迹可提交修改）" if ok
                         else "INSUFFICIENT（不足 4 条，不能生成技能修改）"))
    return 0 if ok else 3


def cmd_propose_run(args: argparse.Namespace) -> int:
    dataset_dir = _dataset(args.dataset)
    spec = load_dataset(dataset_dir)
    root = Path(args.root).resolve()
    ws = sampling.group_workspace_id(args.workspace or spec.tasks[0].group_id)
    summary = prop.run_proposer(
        root, spec, ws,
        profile=args.profile,
        parent_version_id=args.parent_version,
        execution_ids=args.execution_ids,
        idempotency_extra=args.idem_extra or "")
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_propose_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        q = db.query(prop.EvolutionProposalRun)
        if args.run_id:
            row = q.filter(prop.EvolutionProposalRun.run_id == args.run_id).first()
        else:
            row = q.order_by(prop.EvolutionProposalRun.created_at.desc()).first()
        if row is None:
            print("(none)")
            return 1
        p_row = None
        if row.proposal_id:
            p_row = db.get(prop.EvolutionProposal, row.proposal_id)
        print(json.dumps({
            "run_id": row.run_id, "status": row.status,
            "workspace": row.workspace_id, "domain": row.domain,
            "dataset_version": row.dataset_version,
            "read_execution_ids": json.loads(row.read_execution_ids_json or "[]"),
            "tool_events": json.loads(row.tool_events_json or "[]"),
            "error_code": row.error_code, "error_message": row.error_message,
            "proposal": ({
                "proposal_id": p_row.proposal_id, "action": p_row.action,
                "skill_id": p_row.skill_id,
                "parent_version_id": p_row.parent_version_id,
                "candidate_version_id": p_row.candidate_version_id,
                "candidate_content_hash": p_row.candidate_content_hash,
                "reason": p_row.reason,
                "evidence": json.loads(p_row.evidence_execution_ids_json or "[]"),
                "patterns": json.loads(p_row.pattern_ids_json or "[]"),
            } if p_row else None),
        }, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_propose_tools(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        row = (db.query(prop.EvolutionProposalRun)
               .filter(prop.EvolutionProposalRun.run_id == args.run_id).first())
        if row is None:
            print("not found")
            return 1
        for ev in json.loads(row.tool_events_json or "[]"):
            print(ev)
    finally:
        db.close()
    return 0


def _text_diff(a: str, b: str, limit: int = 200) -> str:
    import difflib
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(),
                                      lineterm="", n=1))
    return "\n".join(lines[:limit])


def cmd_propose_diff(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        p_row = db.get(prop.EvolutionProposal, args.proposal_id)
        if p_row is None:
            print("proposal not found")
            return 1
        if not p_row.candidate_version_id:
            print("该提案未生成候选版本（action=" + str(p_row.action) + ")")
            return 0
        if p_row.parent_version_id:
            parent = skill_store.get_version(db, p_row.parent_version_id)
            cand = skill_store.get_version(db, p_row.candidate_version_id)
            print("parent_version:", parent.version_id,
                  "candidate:", cand.version_id)
            print("--- SKILL.md diff ---")
            print(_text_diff(parent.skill_md, cand.skill_md))
            print("--- PURPOSE.md diff ---")
            print(_text_diff(parent.purpose_md, cand.purpose_md))
        else:
            cand = skill_store.get_version(db, p_row.candidate_version_id)
            print("create candidate:", cand.version_id, "skill:", cand.skill_id)
            print(cand.skill_md[:1500])
    finally:
        db.close()
    return 0


def cmd_propose_export(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        p_row = db.get(prop.EvolutionProposal, args.proposal_id)
        if p_row is None or not p_row.candidate_version_id:
            print("无候选版本可导出")
            return 1
        cand = skill_store.get_version(db, p_row.candidate_version_id)
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        skill_path = out / "SKILL.md"
        purpose_path = out / "PURPOSE.md"
        if skill_path.exists() or purpose_path.exists():
            print("error: 目标目录已存在 SKILL.md/PURPOSE.md（不覆盖已有版本）")
            return 2
        skill_path.write_text(cand.skill_md, encoding="utf-8")
        purpose_path.write_text(cand.purpose_md, encoding="utf-8")
        print(f"exported candidate {cand.version_id} -> {out} "
              f"（状态：{p_row.status}，未评估）")
    finally:
        db.close()
    return 0


# ---------------------------------------------------------------------------
# 评估与门控（阶段 5）
# ---------------------------------------------------------------------------


def cmd_gate_experiment(args: argparse.Namespace) -> int:
    dataset_dir = _dataset(args.dataset)
    ds = load_dataset(dataset_dir)
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        members = []
        for vid in args.versions.split(","):
            v = skill_store.get_version(db, vid)
            members.append({"skill_id": v.skill_id, "version_id": v.version_id,
                            "content_hash": v.content_hash, "seq": v.seq})
        exp = gate.create_experiment(
            db, workspace_id=args.workspace, domain=gate.DOMAIN_DEFAULT if False else "wiki_compile.default",
            dataset=ds, grader_version=ds.grader_version,
            runner_config={"profile": args.profile},
            pipeline_key="wiki.default", pipeline_version="3",
            runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=[t for t in args.val_tasks.split(",") if t],
            initial_members=members)
        print(json.dumps({k: getattr(exp, k) for k in (
            "experiment_id", "workspace_id", "dataset_version", "current_skill_set",
            "best_score", "status_rev")}, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_gate_baseline(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    res = gate.run_baseline(Path(args.root).resolve(), ds, args.experiment,
                            profile=args.profile)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_gate_candidate(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    res = gate.evaluate_and_gate(Path(args.root).resolve(), ds, args.experiment,
                                 args.proposal, profile=args.profile)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_gate_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        exps = gate.list_experiments(db, workspace_id=args.workspace)
        for e in exps:
            print(f"{e.experiment_id} ws={e.workspace_id} ds={e.dataset_version} "
                  f"best={e.best_score} rev={e.status_rev}")
        if args.experiment:
            info = gate.get_experiment(db, args.experiment)
            print(json.dumps({
                "current_skill_set": info.current_skill_set,
                "best_score": info.best_score,
                "best_skill_set": info.best_skill_set,
                "baseline_evaluation_id": info.baseline_evaluation_id,
                "status_rev": info.status_rev,
            }, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_gate_history(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        if args.experiment:
            for g in gate.gate_history(db, args.experiment):
                print(json.dumps(g, ensure_ascii=False, indent=2))
        else:
            for e in gate.list_experiments(db):
                print(f"== {e.experiment_id} ==")
                for g in gate.gate_history(db, e.experiment_id):
                    print(f"  {g['decision']} {g['reason']} score={g['candidate_score']}")
    finally:
        db.close()
    return 0


def cmd_gate_export(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        files = gate.export_report_markdown(db, args.experiment)
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (out / name).write_text(text, encoding="utf-8")
        print(f"exported {sorted(files)} -> {out}")
    finally:
        db.close()
    return 0


# ---------------------------------------------------------------------------
# 多轮进化（阶段 6）
# ---------------------------------------------------------------------------


def cmd_evo_create(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        exp_info = gate.get_experiment(db, args.experiment)
        row = orch.create_run(
            db, experiment_id=args.experiment,
            workspace_id=exp_info.workspace_id, domain=exp_info.domain,
            dataset=ds, init_mode=args.init_mode,
            max_iterations=args.iterations,
            budget={"max_model_calls": args.max_model_calls,
                    "max_tool_calls": args.max_tool_calls,
                    "max_seconds": args.max_seconds},
            runner_config={"profile": args.profile},
            train_task_ids=[t for t in args.train_tasks.split(",") if t])
        print(f"created run {row.run_id} status={row.status}")
    finally:
        db.close()
    return 0


def cmd_evo_run(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    from app.core.skill_evolution import control as _ctl
    # CLI/worker 共享启动资格门（real 需持久化启动授权冻结；simulated 不升级）
    try:
        gate = _ctl.worker_start_gate(Path(args.root).resolve(), args.run)
    except _ctl.ControlError as exc:
        print(json.dumps({"error": exc.message, "code": exc.code},
                         ensure_ascii=False))
        return 2
    actors, mode = _ctl.build_run_actors(Path(args.root).resolve(), args.run)
    view = orch.execute(Path(args.root).resolve(), ds, args.run, actors=actors,
                        worker_id=args.worker, resume=True)
    out = dict(view)
    out["model_mode"] = mode
    out["worker_gate"] = gate
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_evo_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        view = orch.get_run(db, args.run)
        print(json.dumps(view, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_evo_pause(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        orch.pause_request(db, args.run)
        print("pause requested")
    finally:
        db.close()
    return 0


def cmd_evo_cancel(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        orch.cancel_request(db, args.run)
        print("cancel requested")
    finally:
        db.close()
    return 0


def cmd_evo_report(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        view = orch.get_run(db, args.run)
        lines = [
            "# WikiSkill 多轮进化运行报告", "",
            f"- run: `{view['run_id']}`  experiment: `{view['experiment_id']}`",
            f"- status: {view['status']}  stop_reason: {view['stop_reason']}",
            f"- init_mode: {view['init_mode']}  dataset: {view['dataset_version']}",
            f"- iterations: {view['current_iteration']}/{view['max_iterations']}",
            f"- used(model/tool): {view['used']}",
            "", "## 轮次串联", "",
            "| iter | step | 状态 | train | maintain | propose | proposal | eval | gate |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for it in view["iterations"]:
            lines.append(
                f"| {it['number']} | {it['step']} | {it['status']} | "
                f"{len(it['train_execution_ids'])} | "
                f"{it['maintenance_run_id'] or '-'} | "
                f"{it['proposal_run_id'] or '-'} | {it['proposal_id'] or '-'} | "
                f"{it['evaluation_id'] or '-'} | {it['gate_event_id'] or '-'} |")
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "evolution-report.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        print(f"report -> {out / 'evolution-report.md'}")
    finally:
        db.close()
    return 0



# ---------------------------------------------------------------------------
# 真实链路试运行 trial-real-d（阶段 7A/试运行入口；不调用网络除非 run）
# ---------------------------------------------------------------------------

TRIAL_SEED_DIR = "eval/wiki_evolution/skills/seed-default-v1"
_DEFAULT_TRIAL_BUDGET = {"max_requests": 90, "max_tools": 40,
                         "max_seconds": 3600, "timeout": 60.0,
                         "max_iterations": 3}


def _trial_override(args) -> dict:
    ov = {"mode": "real", "timeout": float(args.timeout)}
    if args.max_output_tokens is not None:
        ov["max_output_tokens"] = int(args.max_output_tokens)
    if args.model:
        ov["model"] = args.model
    return ov


def _trial_resolve_errors(override: dict) -> list[str]:
    """角色接线 fail-closed：executor/maintainer/proposer 缺一即报错（不发请求）。"""
    from app.core.skill_evolution.real_adapters import (
        ModelBackendError, resolve_real_config)
    errors = []
    for role in ("executor", "maintainer", "proposer"):
        try:
            resolve_real_config(role, override=override)
        except ModelBackendError as exc:
            errors.append(f"[{role}] {exc}")
    return errors


def _trial_summary(args, ds, errors, *, created=False, run_id=None) -> dict:
    from app.core.skill_evolution.real_adapters import resolve_real_config
    roles = {}
    ov = _trial_override(args)
    for role in ("executor", "maintainer", "proposer"):
        try:
            rec = resolve_real_config(role, override=ov).to_record()
        except Exception as exc:  # noqa: BLE001
            rec = {"mode": "error", "error": str(exc)}
        roles[role] = {"mode": rec.get("mode"), "model": rec.get("model"),
                       "api_key_present": rec.get("api_key_present", False),
                       "timeout": rec.get("timeout"),
                       "max_output_tokens": rec.get("max_output_tokens")}
    roles["eval"] = {"mode": roles["executor"]["mode"],
                     "uses_same_as": "executor",
                     "model": roles["executor"]["model"]}
    return {
        "mode": "real", "dataset": ds.dataset_version,
        "dataset_dir": str(ds.dataset_dir),
        "train_tasks": sum(1 for t in ds.tasks if t.split == "train"),
        "val_tasks": sum(1 for t in ds.tasks if t.split == "val"),
        "test_never_run": True, "max_iterations": args.iterations,
        "budget": {"max_model_calls": args.max_requests,
                   "max_tool_calls": args.max_tools,
                   "max_seconds": args.max_seconds},
        "timeout_s": float(args.timeout),
        "max_output_tokens": args.max_output_tokens,
        "roles": roles, "errors": errors,
        "created": created, "run_id": run_id,
        "warnings": ["估算≠硬上限；一次编译可能产生多次请求",
                     "费用无法可靠估算；货币金额不可严格自动停止"],
    }


def _trial_common_args(ap) -> None:
    ap.add_argument("--root", required=True, help="独立实验根目录")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--iterations", type=int,
                    default=_DEFAULT_TRIAL_BUDGET["max_iterations"])
    ap.add_argument("--max-requests", dest="max_requests", type=int,
                    default=_DEFAULT_TRIAL_BUDGET["max_requests"])
    ap.add_argument("--max-tools", dest="max_tools", type=int,
                    default=_DEFAULT_TRIAL_BUDGET["max_tools"])
    ap.add_argument("--max-seconds", dest="max_seconds", type=int,
                    default=_DEFAULT_TRIAL_BUDGET["max_seconds"])
    ap.add_argument("--timeout", type=float,
                    default=_DEFAULT_TRIAL_BUDGET["timeout"])
    ap.add_argument("--max-output-tokens", dest="max_output_tokens",
                    type=int, default=None)
    ap.add_argument("--model", default=None,
                    help="覆盖模型标识（缺省沿用 .env llm_model；不猜默认）")


def cmd_trial_real_d(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    ds_path = _dataset(args.dataset)
    ds = load_dataset(ds_path)
    errors: list[str] = []
    if not 1 <= int(args.iterations) <= 3:
        errors.append("iterations 必须在 1..3（试运行最多 3 轮）")
    if int(args.max_requests) < 2:
        errors.append("max_requests 至少 2")
    for label, v in (("max_tools", args.max_tools),
                     ("max_seconds", args.max_seconds)):
        if int(v) <= 0:
            errors.append(f"{label} 必须 > 0")
    if args.max_output_tokens is not None and int(args.max_output_tokens) <= 0:
        errors.append("max_output_tokens 必须 > 0")
    if not any(t.split == "val" for t in ds.tasks):
        errors.append("数据集缺少 val（真实门控需要）")
    if args.model is None:
        from app.config import settings
        if not settings.llm_model:
            errors.append("未配置 llm_model 也未提供 --model（不猜测 model ID）")
    override = _trial_override(args)
    errors += _trial_resolve_errors(override)

    if args.mode == "preflight":
        print(json.dumps(_trial_summary(args, ds, errors),
                         ensure_ascii=False, indent=2))
        return 1 if errors else 0
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 2

    if args.mode == "create":
        db = _store_session(root)
        try:
            from app.core.skill_evolution import trace_sampling as smp
            from app.core.skill_evolution.real_adapters import (
                EXEC_ROLE, MAINTAIN_ROLE, PROPOSE_ROLE)
            ws = smp.group_workspace_id(
                [t.group_id for t in ds.tasks if t.split == "train"][0])
            seed = skill_store.import_seed(db, skill_store.load_package(
                TRIAL_SEED_DIR, source_type="builtin_seed"))
            v = skill_store.get_version(db, seed)
            exp = gate.create_experiment(
                db, workspace_id=ws, domain="wiki_compile.default", dataset=ds,
                grader_version=ds.grader_version,
                runner_config={"profile": "faithful"},
                pipeline_key="wiki.default", pipeline_version="3",
                runtime_ref=skill_store.RUNTIME_REF,
                val_task_ids=[t.task_id for t in ds.tasks if t.split == "val"],
                initial_members=[{"skill_id": v.skill_id,
                                  "version_id": v.version_id,
                                  "content_hash": v.content_hash,
                                  "seq": v.seq}])
            rconfig = {
                "real": True,
                "dataset_dir": str(ds.dataset_dir),
                "mode": "real",
                "roles": {"executor": EXEC_ROLE, "maintainer": MAINTAIN_ROLE,
                          "proposer": PROPOSE_ROLE},
                "override": override,
            }
            run = orch.create_run(
                db, experiment_id=exp.experiment_id, workspace_id=ws,
                domain="wiki_compile.default", dataset=ds,
                init_mode="business", max_iterations=int(args.iterations),
                budget={"max_model_calls": int(args.max_requests),
                        "max_tool_calls": int(args.max_tools),
                        "max_seconds": int(args.max_seconds)},
                runner_config=rconfig,
                train_task_ids=[t.task_id for t in ds.tasks
                                if t.split == "train"],
                experience="full")
        finally:
            db.close()
        print(json.dumps(_trial_summary(args, ds, [], created=True,
                                        run_id=run.run_id),
                         ensure_ascii=False, indent=2))
        print(f"run_id: {run.run_id}")
        return 0

    if args.mode == "run":
        db = _store_session(root)
        try:
            row = db.get(__import__("app.models.evolution",
                                    fromlist=["EvolutionRun"]).EvolutionRun,
                         args.run_id)
            if row is None:
                print(f"error: run 不存在: {args.run_id}", file=sys.stderr)
                return 2
            cfg = json.loads(row.config_json)
            r = cfg["runner"]
            ds2 = load_dataset(Path(r["dataset_dir"]))
            from app.core.skill_evolution.real_adapters import (
                build_executor_runner, build_maintainer_runner,
                build_proposer_runner, resolve_real_config)
            errs = _trial_resolve_errors(r["override"])
            if errs:
                for e in errs:
                    print(f"error: {e}", file=sys.stderr)
                return 2
            exec_runner = build_executor_runner(
                resolve_real_config("executor", override=r["override"]))
            maint_runner = build_maintainer_runner(
                resolve_real_config("maintainer", override=r["override"]))
            actors = orch.Actors(
                execution_profile="faithful",
                executor_runner=exec_runner,
                maintainer_runner=maint_runner,
                proposer_factory=lambda: build_proposer_runner(
                    resolve_real_config("proposer", override=r["override"])))
        finally:
            db.close()
        view = orch.execute(root, ds2, args.run_id, actors=actors,
                            resume=True)
        out = {k: view.get(k) for k in ("run_id", "status", "stop_reason",
                                        "current_iteration",
                                        "used_model_calls", "used_tool_calls")}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if view.get("status") == "completed" else 1




def cmd_exp_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    ds = load_dataset(_dataset(args.dataset))
    plan = ex7.build_plan(root, ds.dataset_version)
    print(json.dumps({"dataset_version": ds.dataset_version,
                      "runs": len(plan)}, ensure_ascii=False, indent=2))
    return 0


def cmd_exp_check(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    cfg = ex7.ProtocolConfig.from_dict(json.loads(
        Path(args.protocol_json).read_text(encoding="utf-8")))
    errors = ex7.check_plan(ds, cfg)
    if errors:
        print("errors:"); [print(" -", e) for e in errors]
        return 2
    print("OK protocol", cfg.protocol)
    return 0


def cmd_exp_dry(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    root = Path(args.root).resolve()
    plan_file = root / "plan/plan.json"
    if not plan_file.is_file():
        ex7.build_plan(root, ds.dataset_version)
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    n_train = sum(1 for t in ds.tasks if t.split == "train")
    n_val = sum(1 for t in ds.tasks if t.split == "val")
    n_test = sum(1 for t in ds.tasks if t.split == "test")
    out = []
    for run in plan["runs"]:
        cfg = ex7.ProtocolConfig.from_dict(run["config"])
        errors = ex7.check_plan(ds, cfg)
        est = ex7.estimate_budget(cfg, n_train, n_val, n_test)
        out.append({"protocol": cfg.protocol, "replicate": cfg.replicate,
                    "dir": run["dir"], "config_ok": not errors,
                    "estimate": est})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_exp_batch_create(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    runs = None
    if args.runs:
        runs = {}
        for part in args.runs.split(","):
            proto, n = part.split("=", 1)
            runs[proto.strip()] = int(n)
    real_config = None
    if getattr(args, "real_config", None):
        payload = json.loads(Path(args.real_config).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("real-config 必须是 JSON 对象")
        real_config = payload
    test_budget = None
    raw_tb = getattr(args, "test_budget", None)
    if raw_tb:
        text = raw_tb.strip()
        if text.startswith("{"):
            test_budget = json.loads(text)
        else:
            test_budget = json.loads(Path(text).read_text(encoding="utf-8"))
        if not isinstance(test_budget, dict):
            raise SystemExit("test-budget 必须是 JSON 对象")
    out = ex7.create_batch(
        Path(args.root).resolve(), ds, runs=runs,
        model_mode=args.model_mode,
        iterations=(int(args.iterations) if args.iterations else None),
        real_config=real_config,
        test_budget=test_budget)
    print(json.dumps({
        "schema": out["schema"],
        "dataset_version": out["dataset_version"],
        "grader_version": out["grader_version"],
        "created": True,
        "runs": len(out["runs"]),
        "config_fingerprint": out.get("config_fingerprint"),
        "test_confirm_fingerprint": out.get("test_confirm_fingerprint"),
        "test_budget": out.get("test_budget"),
        "model_mode": out.get("model_mode"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_exp_batch_run(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    only = None
    if args.only:
        only = tuple(x.strip() for x in args.only.split(",") if x.strip())
    confirm = None
    if getattr(args, "confirm_fingerprint", None) or getattr(args, "confirm_dataset", None) \
            or getattr(args, "confirm_budget", False):
        confirm = {
            "config_fingerprint": getattr(args, "confirm_fingerprint", None),
            "dataset_version": getattr(args, "confirm_dataset", None) or ds.dataset_version,
            "budget_ok": bool(getattr(args, "confirm_budget", False)),
        }
    out = ex7.run_batch(
        Path(args.root).resolve(), ds, resume=bool(args.resume), only=only,
        simulated_forbidden=bool(args.simulated_forbidden), confirm=confirm)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_exp_batch_resume(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    out = ex7.resume_batch(Path(args.root).resolve(), ds)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_exp_batch_status(args: argparse.Namespace) -> int:
    if not getattr(args, "dataset", None):
        raise ex7.BatchError(
            "experiment-batch-status 需要 --dataset 做完整校验")
    ds = load_dataset(_dataset(args.dataset))
    out = ex7.batch_status(Path(args.root).resolve(), ds)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_exp_batch_test(args: argparse.Namespace) -> int:
    ds = load_dataset(_dataset(args.dataset))
    root = Path(args.root).resolve()
    confirm = None
    status_path = root / "plan" / "batch-status.json"
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("model_mode") == "real":
            confirm = {
                "test_fingerprint": getattr(args, "confirm_test_fingerprint", None),
                "config_fingerprint": getattr(args, "confirm_fingerprint", None),
                "dataset_version": getattr(args, "confirm_dataset", None),
                "plan_hash": getattr(args, "confirm_plan_hash", None),
                "budget_ok": bool(getattr(args, "confirm_budget", False)),
            }
    out = ex7.evaluate_batch_test(
        root, ds, consume=bool(getattr(args, "consume", False)),
        confirm=confirm)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_exp_report(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    plan_file = root / "plan/plan.json"
    if not plan_file.is_file():
        print("plan 不存在：先 experiment-plan")
        return 1
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    lines = ["# 阶段 7A 实验批次计划", "",
             f"- dataset: {plan['dataset_version']}",
             f"- runs: {len(plan['runs'])}", "",
             "| protocol | replicate | dir |",
             "|---|---|---|"]
    for run in plan["runs"]:
        lines.append(f"| {run['protocol']} | {run['replicate']} | {run['dir']} |")
    out_dir = args.out_dir and Path(args.out_dir) or root / "plan"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "experiment-plan.md").write_text("\n".join(lines) + "\n",
                                                encoding="utf-8")
    print(f"report -> {out_dir / 'experiment-plan.md'}")
    return 0


# ---------------------------------------------------------------------------
# 经验维护 / 导出（阶段 3）
# ---------------------------------------------------------------------------


def cmd_maintain_list(args: argparse.Namespace) -> int:
    dataset_dir = _dataset(args.dataset)
    spec = load_dataset(dataset_dir)
    root = Path(args.root).resolve()
    ws = sampling.group_workspace_id(args.workspace or spec.tasks[0].group_id)
    metas = sampling.list_authorized_train_meta(root, spec, ws)
    print(f"workspace={ws} authorized train executions={len(metas)}")
    for m in metas:
        kind = sampling._candidate_kind(root, m)
        skills = (m.get("skills") or {}).get("mode", "none")
        print(f"  {m['execution_id']} task={m.get('task_id')} kind={kind} "
              f"run={m.get('run_status')} skills={skills}")
    return 0


def cmd_maintain_preview(args: argparse.Namespace) -> int:
    dataset_dir = _dataset(args.dataset)
    spec = load_dataset(dataset_dir)
    root = Path(args.root).resolve()
    ws = sampling.group_workspace_id(args.workspace or spec.tasks[0].group_id)
    metas = sampling.list_authorized_train_meta(root, spec, ws)
    if args.execution_ids:
        chosen = [m for m in metas if m["execution_id"] in args.execution_ids]
    else:
        chosen = sampling.sample_executions(
            root, metas, max_failures=args.max_failures,
            max_successes=args.max_successes, include_infra=args.include_infra)
    print(f"workspace={ws} chosen={len(chosen)} "
          f"fail_required={args.max_failures} success_required={args.max_successes}")
    logs = sampling.build_maintainer_context(
        root, chosen, log_char_cap=args.log_cap)
    total = 0
    for lg in logs:
        total += lg["original_chars"]
        print(f"  {lg['execution_id']} chars={lg['original_chars']} "
              f"truncated={str(lg['truncated']).lower()}")
    print(f"log_total_chars={total} (cap/条={args.log_cap})")
    return 0


def cmd_maintain_run(args: argparse.Namespace) -> int:
    dataset_dir = _dataset(args.dataset)
    spec = load_dataset(dataset_dir)
    root = Path(args.root).resolve()
    ws = sampling.group_workspace_id(args.workspace or spec.tasks[0].group_id)
    feedback = []
    if args.feedback:
        f = Path(args.feedback)
        feedback = (f.read_text(encoding="utf-8").splitlines()
                    if f.is_file() else [args.feedback])
    summary = maint.run_maintenance(
        root, spec, ws,
        execution_ids=args.execution_ids,
        runner=maint.SimulatedMaintainer(profile=args.model_profile,
                                         target_pattern_id=args.pattern),
        profile=args.model_profile,
        target_pattern_id=args.pattern,
        feedback=feedback,
        max_failures=args.max_failures, max_successes=args.max_successes,
        include_infra=args.include_infra,
        log_char_cap=args.log_cap,
        idempotency_extra=args.idem_extra or "")
    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_maintain_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        if args.run_id:
            row = exp.get_run(db, args.run_id)
            print(json.dumps(row, ensure_ascii=False, indent=2) if row else "not found")
            return 0 if row else 1
        ws = args.workspace or ""
        domain = args.domain or exp.DOMAIN_DEFAULT
        index = exp.get_index(db, ws, domain) if ws else None
        print("runs（本 scope 最近维护，来自模型行不在 scope 过滤；列出索引状态）:")
        print(json.dumps({"index": index, "patterns": len(exp.list_patterns(db, ws, domain)) if ws else 0},
                         ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


def cmd_patterns(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        if args.pattern:
            row = exp.get_pattern(db, args.workspace, args.domain, args.pattern)
            print(json.dumps(row, ensure_ascii=False, indent=2))
            print("history:")
            print(json.dumps(exp.list_revisions(db, args.pattern), ensure_ascii=False, indent=2))
            return 0
        rows = exp.list_patterns(db, args.workspace, args.domain)
        for r in rows:
            print(f"{r['pattern_id']} [{r['status']}] {r['title']} "
                  f"rev={r['current_revision_id']}")
        print(f"total={len(rows)}")
    finally:
        db.close()
    return 0


def cmd_experience_export(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = skill_store.session_for(root)
    try:
        export = exp.export_scope(db, args.workspace, args.domain)
        files = exp.render_markdown(export)
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern_dir = out_dir / "patterns"
        pattern_dir.mkdir(exist_ok=True)
        for rel, text in files.items():
            target = out_dir / rel
            target.write_text(text, encoding="utf-8")
        print(f"exported {len(files)} files to {out_dir} "
              f"(patterns={len(export['patterns'])})")
    finally:
        db.close()
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _add_common_run_args(p) -> None:
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--task", default=None)
    p.add_argument("--split", default=None)
    p.add_argument("--profile", default="faithful", choices=KNOWN_PROFILES)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-evolution-cli",
                                     description="WikiSkill 离线编译评测与技能版本")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate-dataset", help="校验任务集")
    p.add_argument("--dataset", required=True)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="执行任务/任务集（可指定技能版本/空集合/绑定）")
    _add_common_run_args(p)
    p.add_argument("--skills-version", action="append", default=None)
    p.add_argument("--empty-skills", action="store_true", default=False)
    p.add_argument("--binding-workspace", default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("rerun", help="按原执行技能版本/内容哈希重放（不自动用最新）")
    _add_common_run_args(p)
    p.add_argument("--execution-id", required=True)
    p.set_defaults(func=cmd_rerun)

    p = sub.add_parser("seed", help="导入并校验种子技能包（同内容幂等）")
    p.add_argument("--root", required=True)
    p.add_argument("--skill-dir",
                   default=str(skill_store.SEED_DIR))
    p.add_argument("--source", default="builtin_seed",
                   choices=["builtin_seed", "manual_seed"])
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("add-version", help="从技能包目录新增版本（不可变：新 seq）")
    p.add_argument("--root", required=True)
    p.add_argument("--skill-dir", required=True)
    p.add_argument("--parent-version", default=None)
    p.add_argument("--source", default="manual_seed",
                   choices=["builtin_seed", "manual_seed"])
    p.set_defaults(func=cmd_add_version)

    p = sub.add_parser("versions", help="列出技能版本（按 seq 排序）")
    p.add_argument("--root", required=True)
    p.add_argument("--skill-id", default=None)
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("show-version", help="查看单个技能版本全文与校验")
    p.add_argument("--root", required=True)
    p.add_argument("--version-id", required=True)
    p.set_defaults(func=cmd_show_version)

    p = sub.add_parser("bind", help="写入/切换绑定（技能版本或显式空集合）")
    p.add_argument("--root", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--domain", default=skill_store.DEFAULT_DOMAIN)
    p.add_argument("--kind", default="experiment", choices=["experiment", "business"])
    p.add_argument("--version-id", default=None)
    p.add_argument("--empty", action="store_true", default=False)
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("unbind", help="移除绑定（回退关闭态）")
    p.add_argument("--root", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--domain", default=skill_store.DEFAULT_DOMAIN)
    p.add_argument("--kind", default="experiment", choices=["experiment", "business"])
    p.set_defaults(func=cmd_unbind)

    p = sub.add_parser("bindings", help="列出绑定")
    p.add_argument("--root", required=True)
    p.add_argument("--kind", default=None, choices=["experiment", "business"])
    p.set_defaults(func=cmd_bindings)

    p = sub.add_parser("show", help="查询执行结果")
    p.add_argument("--root", required=True)
    p.add_argument("--execution-id", default=None)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("report", help="导出可读报告")
    p.add_argument("--root", required=True)
    p.add_argument("--output", default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("maintain-list", help="查看可用于维护的 train 执行")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--workspace", default=None)
    p.set_defaults(func=cmd_maintain_list)

    p = sub.add_parser("maintain-preview", help="预览采样结果（不落库/不调模型）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--execution-ids", action="append", default=None)
    p.add_argument("--max-failures", type=int, default=5)
    p.add_argument("--max-successes", type=int, default=3)
    p.add_argument("--include-infra", action="store_true", default=False)
    p.add_argument("--log-cap", type=int, default=15000)
    p.set_defaults(func=cmd_maintain_preview)

    p = sub.add_parser("maintain-run", help="执行一次维护（模拟维护者；可注入 runner）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--execution-ids", action="append", default=None)
    p.add_argument("--model-profile", default="create", choices=["create", "update"])
    p.add_argument("--pattern", default=None)
    p.add_argument("--feedback", default=None)
    p.add_argument("--max-failures", type=int, default=5)
    p.add_argument("--max-successes", type=int, default=3)
    p.add_argument("--include-infra", action="store_true", default=False)
    p.add_argument("--log-cap", type=int, default=15000)
    p.add_argument("--idem-extra", default=None)
    p.set_defaults(func=cmd_maintain_run)

    p = sub.add_parser("maintain-status", help="查看维护状态")
    p.add_argument("--root", required=True)
    p.add_argument("--run-id", default=None)
    p.add_argument("--workspace", default=None)
    p.add_argument("--domain", default=None)
    p.set_defaults(func=cmd_maintain_status)

    p = sub.add_parser("patterns", help="查看模式及历史修订")
    p.add_argument("--root", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--domain", default=exp.DOMAIN_DEFAULT)
    p.add_argument("--pattern", default=None)
    p.set_defaults(func=cmd_patterns)

    p = sub.add_parser("experience-export", help="导出一致快照（index/logs/patterns）")
    p.add_argument("--root", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--domain", default=exp.DOMAIN_DEFAULT)
    p.add_argument("--out-dir", required=True)
    p.set_defaults(func=cmd_experience_export)

    p = sub.add_parser("propose-check", help="提议前置检查（≥4 条不同训练轨迹）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--workspace", default=None)
    p.set_defaults(func=cmd_propose_check)

    p = sub.add_parser("propose-run", help="启动一次提议（多轮工具循环）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--profile", default="patch",
                   choices=["create", "patch", "no_action"])
    p.add_argument("--parent-version", default="default:0001")
    p.add_argument("--execution-ids", action="append", default=None)
    p.add_argument("--idem-extra", default=None)
    p.set_defaults(func=cmd_propose_run)

    p = sub.add_parser("propose-status", help="查看最近/指定提议运行")
    p.add_argument("--root", required=True)
    p.add_argument("--run-id", default=None)
    p.set_defaults(func=cmd_propose_status)

    p = sub.add_parser("propose-tools", help="查看工具读取记录（审计）")
    p.add_argument("--root", required=True)
    p.add_argument("--run-id", required=True)
    p.set_defaults(func=cmd_propose_tools)

    p = sub.add_parser("propose-diff", help="查看提案父版本与候选差异")
    p.add_argument("--root", required=True)
    p.add_argument("--proposal-id", required=True)
    p.set_defaults(func=cmd_propose_diff)

    p = sub.add_parser("propose-export",
                       help="导出候选 SKILL.md/PURPOSE.md（不覆盖已有文件）")
    p.add_argument("--root", required=True)
    p.add_argument("--proposal-id", required=True)
    p.add_argument("--out-dir", required=True)
    p.set_defaults(func=cmd_propose_export)

    p = sub.add_parser("gate-experiment", help="创建最小实验（含冻结 val 任务清单）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--versions", required=True, help="初始集合版本逗号列表")
    p.add_argument("--val-tasks", required=True, help="val 任务逗号列表")
    p.add_argument("--profile", default="faithful")
    p.set_defaults(func=cmd_gate_experiment)

    p = sub.add_parser("gate-baseline", help="运行初始基线并初始化 best_score")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--profile", default="faithful")
    p.set_defaults(func=cmd_gate_baseline)

    p = sub.add_parser("gate-candidate", help="评估指定候选并执行严格门控")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--proposal", required=True)
    p.add_argument("--profile", default="faithful")
    p.set_defaults(func=cmd_gate_candidate)

    p = sub.add_parser("gate-status", help="查看实验集合与 best_score")
    p.add_argument("--root", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--experiment", default=None)
    p.set_defaults(func=cmd_gate_status)

    p = sub.add_parser("gate-history", help="查看门控历史")
    p.add_argument("--root", required=True)
    p.add_argument("--experiment", default=None)
    p.set_defaults(func=cmd_gate_history)

    p = sub.add_parser("gate-export", help="导出评估报告 + skill-impact.md")
    p.add_argument("--root", required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--out-dir", required=True)
    p.set_defaults(func=cmd_gate_export)

    p = sub.add_parser("evolution-create", help="创建多轮进化运行")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--experiment", required=True)
    p.add_argument("--train-tasks", required=True)
    p.add_argument("--iterations", type=int, required=True)
    p.add_argument("--init-mode", default="business", choices=["paper", "business"])
    p.add_argument("--profile", default="faithful")
    p.add_argument("--max-model-calls", type=int, default=120)
    p.add_argument("--max-tool-calls", type=int, default=120)
    p.add_argument("--max-seconds", type=int, default=0)
    p.set_defaults(func=cmd_evo_create)

    p = sub.add_parser("evolution-run", help="领取并执行（可恢复）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--worker", default="worker")
    p.set_defaults(func=cmd_evo_run)

    p = sub.add_parser("evolution-status", help="查看运行/轮次/预算")
    p.add_argument("--root", required=True)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_evo_status)

    p = sub.add_parser("evolution-pause", help="请求暂停")
    p.add_argument("--root", required=True)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_evo_pause)

    p = sub.add_parser("evolution-cancel", help="请求取消")
    p.add_argument("--root", required=True)
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_evo_cancel)

    p = sub.add_parser("evolution-report", help="导出完整运行报告")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--out-dir", required=True)
    p.set_defaults(func=cmd_evo_report)

    p = sub.add_parser("experiment-plan", help="生成四组批次计划与隔离目录")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.set_defaults(func=cmd_exp_plan)

    p = sub.add_parser("experiment-check", help="校验单次协议配置")
    p.add_argument("--dataset", required=True)
    p.add_argument("--protocol-json", required=True)
    p.set_defaults(func=cmd_exp_check)

    p = sub.add_parser("experiment-dry", help="离线 dry-run（估算调用/成本=null）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.set_defaults(func=cmd_exp_dry)

    p = sub.add_parser("experiment-report", help="批次计划报告")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--out-dir", default=None)
    p.set_defaults(func=cmd_exp_report)

    p = sub.add_parser("experiment-batch-create",
                       help="创建正式 A/B/C/D 批次（校验并写入计划）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--runs", default=None,
                   help="例如 A=1,B=1,C=1,D=1")
    p.add_argument("--model-mode", default="simulated",
                   choices=["simulated", "real"])
    p.add_argument("--iterations", default=None, type=int)
    p.add_argument("--real-config", default=None,
                   help="非秘密 JSON：provider/credential_ref/model/api_url（禁止含密钥）")
    p.add_argument("--test-budget", default=None,
                   help="real test 独立预算 JSON：max_model_calls/max_seconds")
    p.set_defaults(func=cmd_exp_batch_create)

    p = sub.add_parser("experiment-batch-run",
                       help="执行正式批次（每组独立实验根/库/run）")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--resume", action="store_true", default=False)
    p.add_argument("--only", default=None, help="仅运行指定协议，逗号分隔")
    p.add_argument("--simulated-forbidden", action="store_true", default=False)
    p.add_argument("--confirm-fingerprint", default=None)
    p.add_argument("--confirm-dataset", default=None)
    p.add_argument("--confirm-budget", action="store_true", default=False)
    p.set_defaults(func=cmd_exp_batch_run)

    p = sub.add_parser("experiment-batch-resume",
                       help="从同一批次/同一 run 恢复未完成实验")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.set_defaults(func=cmd_exp_batch_resume)

    p = sub.add_parser("experiment-batch-status", help="查看批次冻结与 run 状态")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True,
                   help="完整校验必须提供数据集（source/reference 漂移将失败）")
    p.set_defaults(func=cmd_exp_batch_status)

    p = sub.add_parser("experiment-batch-test",
                       help="全部冻结后统一执行 test split")
    p.add_argument("--root", required=True)
    p.add_argument("--dataset", required=True)
    p.add_argument("--confirm-fingerprint", default=None)
    p.add_argument("--confirm-test-fingerprint", default=None)
    p.add_argument("--confirm-plan-hash", default=None)
    p.add_argument("--confirm-dataset", default=None)
    p.add_argument("--confirm-budget", action="store_true", default=False)
    p.add_argument("--consume", action="store_true", default=False)
    p.set_defaults(func=cmd_exp_batch_test)

    p = sub.add_parser("trial-real-d",
                      help="真实链路试运行入口：preflight|create|run")
    p.add_argument("mode", choices=["preflight", "create", "run"])
    p.add_argument("--run-id", default=None, help="run 模式：复用已创建 run")
    _trial_common_args(p)
    p.set_defaults(func=cmd_trial_real_d)

    sub.add_parser("list-profiles", help="列出 fake runner 行为")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list-profiles":
        for name in KNOWN_PROFILES:
            print(name)
        return 0
    try:
        return int(args.func(args) or 0)
    except (DatasetError, SkillEvolutionError, TraceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
