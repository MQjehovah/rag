"""任务执行适配（阶段 1 核心）：真实 wiki.default v3 编译 + 隔离记录。

边界落实：
- 独立进程/独立 SQLite（runenv）；显式 pipeline_version="3"（executor.create_run）；
- 不启动 worker、不调用 scheduler/API；外部模型与图谱调用全部替换为注入 runner
  （SimulatedModel + GraphRecorder）。任何未注入的默认真实路径一旦被触发，会因
  settings.llm_api_url 为空抛 LLMServiceUnavailable（graph 则根本不会执行），
  运行失败 → fail loud，绝不静默外呼；
- 发布产物只落在实验库；评分参考在本模块完全不接触（grader 单独加载）；
- 执行中追加事件 → 结束后封存（TraceStore.seal）；封存失败抛 TraceError。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.skill_evolution import runenv
from app.core.skill_evolution.contracts import DatasetSpec, TaskSpec
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.runner import (
    GraphRecorder,
    RecordingRunner,
    SimDoc,
    SimulatedModel,
)
from app.core.skill_evolution.snapshot import SnapshotContent, SnapshotDoc, SnapshotStore
from app.core.skill_evolution.injector import FrozenSkillSet, SkillInjectingRunner
from app.core.skill_evolution.trace import TraceStore, load_meta as _trace_load_meta


def build_snapshot_for_task(dataset_dir: Path, task: TaskSpec) -> SnapshotContent:
    """从 dataset 来源文件构建快照内容（资料全文进入快照）。"""
    sources_root = (dataset_dir / "sources").resolve()
    docs: list[SnapshotDoc] = []
    for src in task.sources:
        p = (sources_root / src.file).resolve()
        if not p.is_file() or not p.is_relative_to(sources_root):
            raise SkillEvolutionError(f"来源文件缺失或越界: {src.file!r} for {task.task_id}")
        content = p.read_text(encoding="utf-8").strip()
        docs.append(SnapshotDoc(
            doc_id=src.doc_id, title=src.title, content=content,
            file=src.file, product_version=src.product_version,
        ))
    return SnapshotContent(
        label=task.input_snapshot_id,
        wiki_title=task.wiki_title,
        wiki_category=task.wiki_category,
        docs=tuple(docs),
        meta={
            "dataset_version": task.dataset_version,
            "domain": task.domain,
            "task_id": task.task_id,
        },
    )


def _group_short(group_id: str) -> str:
    return hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:12]


def materialize_experiment(db: Session, snapshot: SnapshotContent, task: TaskSpec) -> dict:
    """在实验库中物化任务输入（Notebook/Workspace/Binding/Pages/Wiki）。"""
    from app.models.database import (
        Notebook,
        NotebookWorkspaceBinding,
        Page,
        WikiPage,
        WikiWorkspace,
    )

    ws_suffix = _group_short(task.group_id)
    ws_id = f"ws_{ws_suffix}"
    nb_id = f"nb_{ws_suffix}"
    wiki_id = f"w_{task.task_id}"

    ws = db.get(WikiWorkspace, ws_id)
    if ws is None:
        db.add(WikiWorkspace(
            id=ws_id, key=f"key_{ws_suffix}", name=f"eval-ws-{task.group_id}",
            acl_scope=runenv.PUBLIC_ACL, scope_id=runenv.PUBLIC_SCOPE, status="active",
        ))
    nb = db.get(Notebook, nb_id)
    if nb is None:
        db.add(Notebook(id=nb_id, name=f"eval-nb-{task.task_id}", group_id="__public__"))
    db.flush()
    binding = (
        db.query(NotebookWorkspaceBinding)
        .filter(NotebookWorkspaceBinding.notebook_id == nb_id,
                NotebookWorkspaceBinding.workspace_id == ws_id)
        .first()
    )
    if binding is None:
        db.add(NotebookWorkspaceBinding(
            notebook_id=nb_id, workspace_id=ws_id, status="active"))

    page_ids: list[str] = []
    for doc in snapshot.docs:
        if db.get(Page, doc.doc_id) is None:
            db.add(Page(
                id=doc.doc_id, notebook_id=nb_id, title=doc.title,
                content=doc.content, source_path=doc.file or None,
            ))
        page_ids.append(doc.doc_id)
    wiki = db.get(WikiPage, wiki_id)
    if wiki is None:
        db.add(WikiPage(
            id=wiki_id, title=snapshot.wiki_title, summary="",
            category=snapshot.wiki_category or "资料",
            acl_scope=runenv.PUBLIC_ACL, status="draft",
            source_page_ids=json.dumps(page_ids, ensure_ascii=False),
            dirty=True, locked=False, workspace_id=ws_id,
        ))
    else:
        wiki.source_page_ids = json.dumps(page_ids, ensure_ascii=False)
        wiki.dirty = True
        wiki.status = "draft"
        wiki.workspace_id = ws_id
    db.commit()
    return {"workspace_id": ws_id, "wiki_id": wiki_id, "page_ids": page_ids}


def classify_failure(code: str | None) -> str:
    """失败代码 → 分类（确定性；infra/model 与 content/compile 分开）。"""
    code = (code or "").upper()
    infra_markers = (
        "SERVICE_UNAVAILABLE", "GRAPH_BUILD_FAILED", "WORKER_LOST",
        "RETRY_EXHAUSTED", "STAGE_EXCEPTION", "STAGE_CONTRACT_VIOLATION",
        "PIPELINE_NOT_REGISTERED", "LLM", "LEASE", "CANCEL", "SUPERSEDED",
    )
    content_markers = (
        "INVALID_RESPONSE", "VALIDATION_FAILED", "STALE_INPUT", "EMPTY",
        "PARTIAL_SYNTHESIS", "NOT_WORTHY", "SKILL_NOT_SUPPORTED", "SCOPE",
        "WORKSPACE_MISMATCH", "PAGE_STALE", "EVIDENCE_STALE",
    )
    if any(m in code for m in infra_markers):
        return "infra_or_model"
    if any(m in code for m in content_markers):
        return "content_compile"
    return "unknown"


def classify_run(*, run_status: str, code: str | None,
                 published: bool, revision_count: int) -> dict:
    """执行结果分类：成功 / 无输出 / 内容失败 / 基础设施-模型失败。"""
    if run_status != "succeeded":
        return {
            "run_ok": False,
            "published": published,
            "revision_count": revision_count,
            "failure_kind": classify_failure(code),
            "run_status": run_status,
            "code": code,
        }
    if not published or revision_count == 0:
        return {
            "run_ok": True,
            "published": False,
            "revision_count": revision_count,
            "failure_kind": "compile_no_output",
            "run_status": run_status,
            "code": code,
        }
    return {
        "run_ok": True,
        "published": True,
        "revision_count": revision_count,
        "failure_kind": None,
        "run_status": run_status,
        "code": code,
    }


def read_candidate(db: Session, run_id: str) -> dict:
    """从实验库读取候选输出（published revision + sections + wiki 状态）。"""
    from app.models.database import (
        KnowledgeCompileRun as CompileRun,
        WikiPage,
        WikiRevision,
        WikiSection,
    )

    run = db.query(CompileRun).filter(CompileRun.id == run_id).first()
    if run is None:
        raise SkillEvolutionError(f"run 缺失: {run_id}")
    revision_id = run.output_revision_id
    sections: list[dict] = []
    summary = ""
    wiki_status = None
    if revision_id:
        rev = db.get(WikiRevision, revision_id)
        if rev is not None:
            summary = rev.summary or ""
            rows = (
                db.query(WikiSection)
                .filter(WikiSection.revision_id == revision_id)
                .order_by(WikiSection.order_index, WikiSection.version_label)
                .all()
            )
            sections = [
                {
                    "section_type": s.section_type,
                    "version_label": s.version_label,
                    "is_common": bool(s.is_common),
                    "diff_notice": s.diff_notice or "",
                    "content": s.content or "",
                }
                for s in rows
            ]
    if run.wiki_page_id:
        wp = db.get(WikiPage, run.wiki_page_id)
        if wp is not None:
            wiki_status = wp.status
    return {
        "revision_id": revision_id,
        "summary": summary,
        "sections": sections,
        "wiki_status": wiki_status,
    }


def render_candidate_body(candidate: dict) -> str:
    """可读正文（供输出/报告）。"""
    parts: list[str] = []
    for sec in candidate["sections"]:
        label = sec.get("version_label") or "unversioned"
        if sec.get("section_type") == "summary":
            parts.append(f"# 摘要（{label}）\n{sec['content']}")
            continue
        head = f"# [{label}]"
        if sec.get("diff_notice"):
            head += "（差异提示）"
        parts.append(f"{head}\n{sec['content']}")
    if not parts and candidate.get("summary"):
        parts.append(candidate["summary"])
    return "\n\n".join(parts)


def run_one(
    root: Path,
    dataset: DatasetSpec,
    task: TaskSpec,
    *,
    profile: str,
    snapshot: SnapshotContent | None = None,
    skills: "FrozenSkillSet | str | None" = None,
    llm_runner_override: "Callable | None" = None,
    execution_id: str | None = None,
) -> dict:
    """执行单个任务并封存记录，返回 execution 汇总（含 candidate 供 grader）。"""
    from app.core.wiki_pipeline import executor
    from app.models.database import (
        KnowledgeCompileArtifact as Artifact,
        KnowledgeCompileRun as CompileRun,
        KnowledgeCompileStageRun as StageRun,
        init_db,
    )

    runenv.register_compile_stack()
    root = runenv.ensure_experiment_root(root)
    if isinstance(skills, str):
        plan = FrozenSkillSet.empty() if skills == "empty" else FrozenSkillSet.disabled()
    else:
        plan = skills if isinstance(skills, FrozenSkillSet) else FrozenSkillSet.disabled()
    store = SnapshotStore(root)
    if snapshot is None:
        snapshot = build_snapshot_for_task(dataset.dataset_dir, task)
    store.store(snapshot)  # 不可覆盖 + 一致性校验

    execution_id, run_dir = runenv.new_execution_dir(root, execution_id)
    db_path = runenv.execution_db_path(root, execution_id)
    runenv.reject_unsafe_db_path(db_path, root)
    engine = runenv.make_experiment_engine(db_path)
    trace: TraceStore | None = None
    try:
        init_db(engine)
        db = runenv.make_session(engine)
        try:
            ids = materialize_experiment(db, snapshot, task)

            collector: dict = {
                "model_calls": 0,
                "estimated_input_chars": 0,
                "estimated_output_chars": 0,
                "estimated_tokens": 0,
            }
            model_errors: list[dict] = []
            graph_targets = 0
            injection_stats = {"events": 0, "injected": 0, "reasons": {}}

            def emit_collect(event: dict) -> None:
                kind = event.get("kind")
                if kind == "model_call":
                    collector["model_calls"] += 1
                    collector["estimated_input_chars"] += int(
                        event.get("estimated_input_chars") or 0)
                    collector["estimated_output_chars"] += int(
                        event.get("estimated_output_chars") or 0)
                    collector["estimated_tokens"] += int(
                        event.get("estimated_tokens") or 0)
                    if event.get("error"):
                        model_errors.append(event["error"])
                elif kind == "graph_isolated":
                    nonlocal graph_targets
                    graph_targets += 1
                elif kind == "skill_injection":
                    injection_stats["events"] += 1
                    if event.get("injected"):
                        injection_stats["injected"] += 1
                    reason = event.get("reason") or "unknown"
                    injection_stats["reasons"][reason] = (
                        injection_stats["reasons"].get(reason, 0) + 1)

            def emit_event(event: dict) -> None:
                trace.emit(event)
                emit_collect(event)

            meta_base = {
                "execution_id": execution_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "task_id": task.task_id,
                "dataset_version": task.dataset_version,
                "domain": task.domain,
                "split": task.split,
                "group_id": task.group_id,
                "input_snapshot_id": snapshot.label,
                "snapshot_content_hash": snapshot.content_hash(),
                "instruction": task.instruction,
                "instruction_hash": hashlib.sha256(
                    task.instruction.encode("utf-8")).hexdigest(),
                "grader_version": task.grader_version,
                "reference_ref": task.reference_ref,
                "trigger": task.trigger,
                "pipeline_key": "wiki.default",
                "pipeline_version": "3",
                "skills": plan.to_dict(),
                "skill_instruction_version": (
                    ",".join(plan.version_ids) if plan.version_ids else None),
                "skill_instruction_injected": False,
                "model": {
                    "provider": "offline-simulated",
                    "profile": profile,
                    "api_ref": None,
                },
                "runtime": runenv.runtime_summary(),
            }
            trace = TraceStore(run_dir, meta_base)
            docs = tuple(SimDoc(
                doc_id=d.doc_id, title=d.title, content=d.content,
                product_version=d.product_version) for d in snapshot.docs)
            if llm_runner_override is None:
                sim = SimulatedModel(docs=docs, profile=profile)
                recording = RecordingRunner(sim, emit_event)
            else:
                # 真实/外部 runner 显式覆盖：不使用模拟模型，也不回退。
                recording = RecordingRunner(llm_runner_override, emit_event)
            # 唯一注入点：SkillInjectingRunner 在内容生成调用前插入冻结指令；
            # RecordingRunner 记录注入后的最终消息（验收：每次调用只注入一次）。
            llm = SkillInjectingRunner(plan, recording, emit_event)
            graph = GraphRecorder(emit_event)
            executor.reset_external_runners()
            executor.configure_external_runners(llm_runner=llm, graph_runner=graph)

            run = executor.create_run(
                db,
                pipeline_key="wiki.default",
                pipeline_version="3",  # 显式固定（阶段 1 契约）
                trigger_type=task.trigger,
                wiki_page_id=ids["wiki_id"],
                workspace_id=ids["workspace_id"],
            )
            db.commit()
            executed = executor.execute_run(db, run.id)
            db.refresh(executed)
            run_id = executed.id
            run_status = executed.status
            run_code = executed.safe_error_code
            run_attempt = executed.attempt

            # 阶段结果（真实 StageRun 行）。
            stage_rows = (
                db.query(StageRun)
                .filter(StageRun.run_id == run_id)
                .order_by(StageRun.stage_order, StageRun.attempt)
                .all()
            )
            stages = [
                {
                    "stage_key": s.stage_key,
                    "status": s.status,
                    "attempt": s.attempt,
                    "retryable": s.retryable,
                    "error_code": s.error_code,
                    "safe_error_code": s.safe_error_code,
                    "output_hash": s.output_hash,
                }
                for s in stage_rows
            ]
            artifact_types = [
                a.artifact_type
                for a in (db.query(Artifact).filter(Artifact.run_id == run_id).all())
            ]
            candidate = read_candidate(db, run_id)
        finally:
            db.close()
    except Exception:
        if trace is not None:
            trace.abort()
        raise
    finally:
        engine.dispose()

    outcome = classify_run(
        run_status=run_status, code=run_code,
        published=candidate["revision_id"] is not None,
        revision_count=1 if candidate["revision_id"] else 0,
    )
    for st in stages:
        trace.emit({"kind": "stage_result", "stage": st})
    trace.emit({
        "kind": "compile_result",
        "run_id": run_id,
        "run_status": run_status,
        "run_code": run_code,
        "attempt": run_attempt,
        "artifact_types": artifact_types,
        "outcome": outcome,
    })
    final_meta = {
        "run_id": run_id,
        "run_status": run_status,
        "run_code": run_code,
        "run_attempt": run_attempt,
        "artifact_types": artifact_types,
        "stages": stages,
        "outcome": outcome,
        "candidate": {
            "revision_id": candidate["revision_id"],
            "section_count": len(candidate["sections"]),
            "wiki_status": candidate["wiki_status"],
        },
        "skill_stats": injection_stats,
        "skills": plan.to_dict(),
        "skill_instruction_injected": injection_stats["injected"] > 0,
        "usage": {
            **collector,
            "model_error_calls": len(model_errors),
            "graph_targets": graph_targets,
            "usage_tokens": None,  # 阶段 1 无供应商用量 → null，非 0
            "usage_measured": False,
            "usage_estimated": True,
        },
    }
    try:
        seal = trace.seal(final_meta)
    except Exception:
        trace.abort()
        raise
    # 输出物（候选正文 + 摘要 JSON）随 run 目录保存。
    body = render_candidate_body(candidate)
    (run_dir / "output.md").write_text(body, encoding="utf-8")
    (run_dir / "output.json").write_text(
        json.dumps({"candidate": candidate, "outcome": outcome},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if task.split == "train":
        from app.core.skill_evolution.train_grading import (
            TrainGradeError, grade_train_execution)
        try:
            grade_train_execution(
                Path(root) if not isinstance(root, Path) else root,
                dataset, task, execution_id,
                grader_version=task.grader_version,
                candidate=candidate, outcome=outcome)
        except TrainGradeError:
            # v2 无 reviewer：保持无 grade（unknown）；编排器随后用冻结 reviewer 补评。
            pass
    # 返回完整封存 meta（trace 文件中的 base+final 合并内容），并附 seal 值。
    meta_out = _trace_load_meta(run_dir)
    meta_out["seal_sha256"] = seal
    return {
        "execution_id": execution_id,
        "run_dir": str(run_dir),
        "meta": meta_out,
        "candidate": candidate,
    }
