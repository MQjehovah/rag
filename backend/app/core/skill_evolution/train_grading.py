"""训练执行质量判定：复用冻结 v1/v2 评分，原子写入 grade.json。

- 训练成功/失败分类来自冻结 grader 的 verdict，而不是编译成功猜测；
- v1 走确定性 grader.grade；v2 走 review_eval.grade_task_v2，缺 reviewer /
  配置漂移 / 非法响应 fail-closed，不回退 v1；
- 真实 reviewer 仍由调用方经 BudgetGuard.wrap 注入；本模块不另建发送通道；
- grade.json 只含非秘密指纹与安全摘要，不含参考答案全文 / 密钥 / Authorization。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from app.core.skill_evolution.contracts import DatasetSpec, TaskSpec, canonical_json
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.grader import grade as grade_v1
from app.core.skill_evolution.grader_registry import GRADER_V2, grader_state
from app.core.skill_evolution.trace import TraceError, load_meta

GRADE_SCHEMA = "train-grade/v1"
GRADE_FILENAME = "grade.json"
GRADE_HASH_FILENAME = "grade.sha256"
_INFRA_KINDS = ("infra_or_model",)
_PASS_VERDICTS = ("pass",)
_FAIL_VERDICTS = ("fail",)
_UNKNOWN_VERDICTS = ("needs_review", "invalid")

_FORBIDDEN_KEY_SUBSTR = (
    "authorization", "api_key", "apikey", "secret", "password", "credential",
    "bearer", "reference_text", "expected_points", "forbidden",
)


class TrainGradeError(SkillEvolutionError):
    """训练评分失败（缺 reviewer、配置漂移、无法评分）。"""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def task_spec_fingerprint(task: TaskSpec) -> str:
    """任务契约指纹（不含参考答案正文）。"""
    payload = {
        "task_id": task.task_id,
        "dataset_version": task.dataset_version,
        "domain": task.domain,
        "split": task.split,
        "group_id": task.group_id,
        "grader_version": task.grader_version,
        "reference_ref": task.reference_ref,
        "instruction_hash": hashlib.sha256(
            (task.instruction or "").encode("utf-8")).hexdigest(),
        "sources": [s.to_dict() for s in (task.sources or ())],
    }
    return _sha256_bytes(canonical_json(payload).encode("utf-8"))


def _sha256_canonical(obj) -> str:
    return _sha256_bytes(canonical_json(obj).encode("utf-8"))


def stable_execution_id(run_id: str, iteration: int, task_id: str) -> str:
    """同一 evolution run + iteration + task 的稳定 execution 身份（32 hex）。"""
    return hashlib.sha256(canonical_json({
        "run_id": str(run_id), "iteration": int(iteration),
        "task_id": str(task_id),
    }).encode("utf-8")).hexdigest()[:32]


def read_trace_seal(run_dir: Path) -> str | None:
    path = Path(run_dir) / "seal.sha256"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip().removeprefix("sha256:")


def _attr_value(obj, name, default=None):
    val = getattr(obj, name, default)
    if callable(val) and not isinstance(val, type):
        try:
            val = val()
        except TypeError:
            pass
    return val


def reviewer_binding(reviewer) -> dict:
    if reviewer is None:
        return {
            "reviewer_identity": None,
            "reviewer_config_fingerprint": None,
            "prompt_version": None,
        }
    identity = _attr_value(reviewer, "identity")
    cfp = _attr_value(reviewer, "config_fingerprint")
    pv = (_attr_value(reviewer, "prompt_version")
          or getattr(reviewer, "_pv", None))
    if not identity:
        identity = f"reviewer:{type(reviewer).__qualname__}"
    if not cfp:
        cfp = _sha256_canonical({
            "type": type(reviewer).__qualname__,
            "prompt_version": pv,
            "identity": identity,
        })
    return {
        "reviewer_identity": identity,
        "reviewer_config_fingerprint": cfp,
        "prompt_version": pv,
    }


def grade_fingerprints(dataset: DatasetSpec, task: TaskSpec,
                       grader_version: str, *,
                       candidate=None, outcome=None,
                       execution_id: str | None = None,
                       run_dir: Path | None = None,
                       reviewer=None) -> dict:
    """数据集 / 参考文件 / 任务契约 / 评分器 / 输出证据 / reviewer 的非秘密指纹。"""
    ds_dir = Path(dataset.dataset_dir)
    ds_json = ds_dir / "dataset.json"
    ref_path = (ds_dir / "references" / task.reference_ref).resolve()
    if not ds_json.is_file():
        raise TrainGradeError(f"数据集文件缺失: {ds_json}")
    if not ref_path.is_file() or not ref_path.is_relative_to(ds_dir.resolve()):
        raise TrainGradeError(f"reference 文件缺失或越界: {task.reference_ref}")
    fps = {
        "dataset_json": _sha256_file(ds_json),
        "reference_file": _sha256_file(ref_path),
        "task_spec": task_spec_fingerprint(task),
        "grader_version": grader_version,
        "dataset_version": dataset.dataset_version,
    }
    if candidate is not None:
        fps["candidate_sha256"] = _sha256_canonical(candidate)
    if outcome is not None:
        fps["outcome_sha256"] = _sha256_canonical(outcome)
    if run_dir is not None:
        outp = Path(run_dir) / "output.json"
        if outp.is_file():
            fps["output_sha256"] = _sha256_file(outp)
        seal = read_trace_seal(run_dir)
        if seal:
            fps["trace_seal"] = seal
        meta_p = Path(run_dir) / "meta.json"
        if meta_p.is_file():
            fps["meta_sha256"] = _sha256_file(meta_p)
    elif candidate is not None and outcome is not None:
        fps["output_sha256"] = _sha256_canonical(
            {"candidate": candidate, "outcome": outcome})
    if execution_id:
        fps["execution_id"] = execution_id
    bind = reviewer_binding(reviewer)
    fps["reviewer_identity"] = bind["reviewer_identity"]
    fps["reviewer_config_fingerprint"] = bind["reviewer_config_fingerprint"]
    if bind["prompt_version"] is not None:
        fps["prompt_version"] = bind["prompt_version"]
    return fps


def _strip_secrets(obj):
    """递归去掉密钥/参考全文等字段；不改变判定结构。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower().replace("-", "_")
            if any(s in lk for s in _FORBIDDEN_KEY_SUBSTR):
                continue
            out[k] = _strip_secrets(v)
        return out
    if isinstance(obj, list):
        return [_strip_secrets(x) for x in obj]
    if isinstance(obj, str) and obj.lower().startswith("bearer "):
        return "bearer ***"
    return obj


def _safe_checks(item_results: list) -> list[dict]:
    out = []
    for it in item_results or []:
        if not isinstance(it, dict):
            continue
        out.append({
            "id": it.get("id") or it.get("check_id"),
            "kind": it.get("kind") or it.get("check"),
            "passed": bool(it.get("passed") or it.get("status") == "pass"),
            "status": it.get("status") or (
                "pass" if it.get("passed") else "fail"),
        })
    return out


def write_grade_integrity(run_dir: Path, digest: str) -> Path:
    """独立完整性记录：仅 SHA-256，不含密钥/Authorization/reference 全文。"""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / GRADE_HASH_FILENAME
    fd, tmp = tempfile.mkstemp(prefix="gradehash.", suffix=".tmp", dir=str(run_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"sha256:{digest}\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_grade_integrity(run_dir: Path) -> str | None:
    path = Path(run_dir) / GRADE_HASH_FILENAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text.removeprefix("sha256:") or None


def verify_grade_integrity(run_dir: Path) -> bool:
    expected = read_grade_integrity(run_dir)
    grade = Path(run_dir) / GRADE_FILENAME
    if not expected or not grade.is_file():
        return False
    try:
        return expected == _sha256_file(grade)
    except (OSError, UnicodeDecodeError):
        return False


def atomic_write_grade(run_dir: Path, payload: dict) -> Path:
    """同目录临时文件 + os.replace，保证 grade.json 只以完整文件出现。"""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / GRADE_FILENAME
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix="grade.", suffix=".tmp", dir=str(run_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    write_grade_integrity(run_dir, _sha256_file(target))
    return target


def load_grade_document(run_dir: Path) -> dict | None:
    path = Path(run_dir) / GRADE_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


_EVIDENCE_KEYS = (
    "candidate_sha256", "outcome_sha256", "output_sha256",
    "execution_id", "trace_seal",
)


def grade_document_structure_ok(existing: dict | None, *, task_id: str,
                                grader_version: str, fingerprints: dict,
                                candidate=None, outcome=None,
                                reviewer=None) -> bool:
    """纯内存结构校验（不含 seal）。生产复用不得只依赖本函数。"""
    if not existing:
        return False
    if existing.get("schema") != GRADE_SCHEMA:
        return False
    if existing.get("task_id") != task_id:
        return False
    if existing.get("split") != "train":
        return False
    if existing.get("grader_version") != grader_version:
        return False
    fp = existing.get("fingerprints") or {}
    if not isinstance(fp, dict):
        return False
    for key in _EVIDENCE_KEYS:
        if not fp.get(key):
            return False
        if key in fingerprints and fp.get(key) != fingerprints.get(key):
            return False
    for key, expected in fingerprints.items():
        if fp.get(key) != expected:
            return False
    use_v2 = grader_version == GRADER_V2
    stored_id = existing.get("reviewer_identity", fp.get("reviewer_identity"))
    stored_cfp = existing.get("reviewer_config_fingerprint",
                              fp.get("reviewer_config_fingerprint"))
    if not use_v2:
        if stored_id not in (None, "n/a", "not_applicable"):
            return False
        if stored_cfp not in (None, "", "n/a", "not_applicable"):
            return False
        if reviewer is not None:
            return False
    else:
        if reviewer is None:
            return False
        if stored_id != getattr(reviewer, "identity", None):
            return False
        if stored_cfp != getattr(reviewer, "config_fingerprint", None):
            return False
        if not stored_id or not stored_cfp:
            return False
    if candidate is not None and fp.get("candidate_sha256") != _sha256_canonical(candidate):
        return False
    if outcome is not None and fp.get("outcome_sha256") != _sha256_canonical(outcome):
        return False
    if existing.get("verdict") not in (
            "pass", "fail", "needs_review", "invalid", "infra"):
        return False
    return True


def grade_is_reusable(existing: dict | None, *, task_id: str,
                      grader_version: str, fingerprints: dict,
                      candidate=None, outcome=None, reviewer=None,
                      run_dir: Path | None = None) -> bool:
    """正式复用入口：无 run_dir / 完整性失败则 fail-closed。"""
    if run_dir is None:
        return False
    if not grade_document_structure_ok(
            existing, task_id=task_id, grader_version=grader_version,
            fingerprints=fingerprints, candidate=candidate, outcome=outcome,
            reviewer=reviewer):
        return False
    if not verify_grade_integrity(run_dir):
        return False
    disk = load_grade_document(run_dir)
    if not disk:
        return False
    if _sha256_canonical(disk) != _sha256_canonical(existing):
        return False
    expected_hash = fingerprints.get("grade_sha256")
    live_hash = read_grade_integrity(run_dir)
    if expected_hash and live_hash != expected_hash:
        return False
    return True


def _reviewer_meta(reviewer, *, grader_version: str) -> dict:
    bind = reviewer_binding(reviewer)
    if grader_version != GRADER_V2:
        return {
            "reviewer_identity": None,
            "reviewer_config_fingerprint": None,
            "reviewer_applicable": False,
        }
    return {
        "reviewer_identity": bind["reviewer_identity"],
        "reviewer_config_fingerprint": bind["reviewer_config_fingerprint"],
        "reviewer_applicable": True,
        "prompt_version": bind["prompt_version"],
    }


def dispatch_grade(*, dataset: DatasetSpec, task: TaskSpec, candidate: dict,
                   outcome: dict, grader_version: str, reviewer=None) -> dict:
    """按冻结 grader_version 分派 v1/v2；v2 缺 reviewer 不回退 v1。"""
    state = grader_state(grader_version)
    if state.get("role") == "unknown" or not state.get("ready"):
        raise TrainGradeError(
            f"冻结评分器未注册或未就绪: {grader_version}")
    if task.grader_version != grader_version:
        raise TrainGradeError(
            f"任务 grader_version={task.grader_version} 与 run 冻结 "
            f"{grader_version} 不一致")
    use_v2 = grader_version == GRADER_V2 or state.get("role") == "semantic"
    if use_v2:
        if grader_version != GRADER_V2 and state.get("role") != "semantic":
            raise TrainGradeError(f"无法识别的 v2 评分器: {grader_version}")
        if reviewer is None:
            raise TrainGradeError(
                "v2 训练评分缺少冻结 reviewer（不回退 v1）")
        from app.core.skill_evolution import review_eval
        raw = review_eval.grade_task_v2(
            dataset_dir=dataset.dataset_dir, task=task,
            candidate=candidate or {}, reviewer=reviewer)
        return {
            "verdict": raw.get("verdict") or "invalid",
            "checks": _safe_checks(raw.get("items") or []),
            "pending": {
                "unresolved_ids": list(raw.get("unresolved_ids") or []),
                "evaluation_invalid": raw.get("evaluation_invalid"),
                "promotable": bool(raw.get("promotable")),
            },
            "compile_ok": bool((outcome or {}).get("run_ok")) and bool(
                (outcome or {}).get("published")),
            "review_requests": int(raw.get("review_requests") or 0),
        }
    raw = grade_v1(
        outcome=outcome or {}, candidate=candidate or {},
        reference_ref=task.reference_ref, dataset_dir=dataset.dataset_dir,
        task_id=task.task_id)
    return {
        "verdict": raw.get("verdict") or "fail",
        "checks": _safe_checks(raw.get("item_results") or []),
        "pending": {"unresolved_ids": [], "evaluation_invalid": None,
                    "promotable": raw.get("verdict") == "pass"},
        "compile_ok": bool(raw.get("compile_ok")),
        "review_requests": 0,
    }


def build_grade_document(*, task: TaskSpec, grader_version: str,
                         fingerprints: dict, grading: dict,
                         reviewer=None) -> dict:
    pending = grading.get("pending") or {}
    doc = {
        "schema": GRADE_SCHEMA,
        "version": GRADE_SCHEMA,
        "task_id": task.task_id,
        "split": "train",
        "grader_version": grader_version,
        "fingerprints": dict(fingerprints),
        "verdict": grading.get("verdict"),
        "checks": grading.get("checks") or [],
        "pending": {
            "unresolved_ids": list(pending.get("unresolved_ids") or []),
            "evaluation_invalid": pending.get("evaluation_invalid"),
            "promotable": bool(pending.get("promotable")),
        },
        "compile_ok": bool(grading.get("compile_ok")),
        "created_at": _now_iso(),
    }
    doc.update(_reviewer_meta(reviewer, grader_version=grader_version))
    return _strip_secrets(doc)


def grade_train_execution(
    root: Path, dataset: DatasetSpec, task: TaskSpec, execution_id: str, *,
    grader_version: str, candidate: dict, outcome: dict, reviewer=None,
) -> dict:
    """对一条训练执行评分并原子写入 grade.json；指纹一致则幂等复用。"""
    if task.split != "train":
        raise TrainGradeError(
            f"训练评分只接受 split=train，收到 {task.split}")
    run_dir = Path(root) / "runs" / execution_id
    run_dir.mkdir(parents=True, exist_ok=True)
    outp = run_dir / "output.json"
    payload = {"candidate": candidate, "outcome": outcome}
    rewrite = True
    if outp.is_file():
        try:
            old = json.loads(outp.read_text(encoding="utf-8"))
            rewrite = (
                _sha256_canonical(old.get("candidate")) != _sha256_canonical(candidate)
                or _sha256_canonical(old.get("outcome")) != _sha256_canonical(outcome))
        except (OSError, ValueError, TypeError):
            rewrite = True
    if rewrite:
        outp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    fps = grade_fingerprints(
        dataset, task, grader_version, candidate=candidate, outcome=outcome,
        execution_id=execution_id, run_dir=run_dir, reviewer=reviewer)
    existing = load_grade_document(run_dir)
    if grade_is_reusable(existing, task_id=task.task_id,
                         grader_version=grader_version, fingerprints=fps,
                         candidate=candidate, outcome=outcome,
                         reviewer=reviewer, run_dir=run_dir):
        return existing
    if (outcome or {}).get("failure_kind") in _INFRA_KINDS:
        grading = {
            "verdict": "infra",
            "checks": [],
            "pending": {"unresolved_ids": [], "evaluation_invalid": None,
                        "promotable": False},
            "compile_ok": False,
            "review_requests": 0,
        }
    else:
        grading = dispatch_grade(
            dataset=dataset, task=task, candidate=candidate,
            outcome=outcome, grader_version=grader_version, reviewer=reviewer)
    doc = build_grade_document(
        task=task, grader_version=grader_version, fingerprints=fps,
        grading=grading, reviewer=reviewer)
    atomic_write_grade(run_dir, doc)
    return doc


def _candidate_from_output(run_dir: Path) -> tuple[dict, dict]:
    payload = json.loads((run_dir / "output.json").read_text(encoding="utf-8"))
    return payload.get("candidate") or {}, payload.get("outcome") or {}


TRAIN_STATE_SCHEMA = "train-task-state/v1"
STATUS_ASSIGNED = "assigned"
STATUS_EXECUTED = "executed"
STATUS_GRADED = "graded"


def parse_train_task_state(raw) -> list[dict]:
    """兼容旧 list[str] 与结构化任务状态。禁止扫描任意历史猜测复用。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if isinstance(raw, dict) and raw.get("schema") == TRAIN_STATE_SCHEMA:
        return list(raw.get("tasks") or [])
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, str):
                out.append({"task_id": None, "execution_id": item,
                            "status": STATUS_GRADED})
            elif isinstance(item, dict) and item.get("execution_id"):
                out.append({
                    "task_id": item.get("task_id"),
                    "execution_id": item["execution_id"],
                    "status": item.get("status") or STATUS_GRADED,
                    "grade_sha256": item.get("grade_sha256"),
                })
        return out
    return []


def dump_train_task_state(records: list[dict]) -> str:
    return json.dumps({
        "schema": TRAIN_STATE_SCHEMA,
        "tasks": records,
    }, ensure_ascii=False)


def execution_ids_from_state(records: list[dict]) -> list[str]:
    return [r["execution_id"] for r in records if r.get("execution_id")]


def _execution_complete(run_dir: Path) -> bool:
    if not (run_dir / "output.json").is_file():
        return False
    try:
        from app.core.skill_evolution.trace import verify_sealed
        verify_sealed(run_dir)
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_train_tasks_graded(
    root: Path, dataset: DatasetSpec, task_ids: list[str], skill_set,
    profile: str, *, executor_runner=None, existing_ids: list[str] | None = None,
    grader_version: str, reviewer=None,
    on_progress=None,
    run_id: str | None = None, iteration: int | None = None,
    existing_state=None,
) -> list[str]:
    """执行缺失的训练任务并保证每条都有可复用 grade。

    崩溃恢复：先持久化 task→execution_id（assigned），再执行模型；执行完成后
    持久化 executed 再评分；grade 落盘后持久化 graded。禁止扫描其它 run 的历史。
    """
    from app.core.skill_evolution.adapter import run_one

    root = Path(root)
    records = parse_train_task_state(existing_state)
    if not records and existing_ids:
        records = parse_train_task_state(existing_ids)

    by_task: dict[str, dict] = {}
    orphans: list[dict] = []
    for rec in records:
        tid = rec.get("task_id")
        if tid:
            by_task[str(tid)] = rec
        elif rec.get("execution_id"):
            orphans.append(rec)

    def _persist() -> None:
        if on_progress is not None:
            on_progress(list(records), execution_ids_from_state(records))

    ordered: list[str] = []
    for tid in task_ids:
        task = dataset.task(tid)
        rec = by_task.get(tid)
        if rec is None and orphans:
            rec = orphans.pop(0)
            rec["task_id"] = tid
            by_task[tid] = rec
        if rec is None:
            if not run_id or iteration is None:
                from app.core.skill_evolution.runenv import new_execution_dir
                eid, _ = new_execution_dir(root)
            else:
                eid = stable_execution_id(run_id, iteration, tid)
            rec = {"task_id": tid, "execution_id": eid,
                   "status": STATUS_ASSIGNED}
            records.append(rec)
            by_task[tid] = rec
            _persist()
        eid = rec["execution_id"]
        run_dir = root / "runs" / eid
        if rec.get("status") == STATUS_GRADED and load_grade_document(run_dir):
            try:
                candidate, outcome = _candidate_from_output(run_dir)
            except (OSError, ValueError, TypeError):
                candidate, outcome = {}, {}
            existing = load_grade_document(run_dir)
            fps = None
            try:
                fps = grade_fingerprints(
                    dataset, task, grader_version, candidate=candidate,
                    outcome=outcome, execution_id=eid, run_dir=run_dir,
                    reviewer=reviewer)
            except TrainGradeError:
                fps = None
            if fps and grade_is_reusable(
                    existing, task_id=task.task_id, grader_version=grader_version,
                    fingerprints=fps, candidate=candidate, outcome=outcome,
                    reviewer=reviewer, run_dir=run_dir):
                if rec.get("grade_sha256") and rec.get("grade_sha256") != \
                        read_grade_integrity(run_dir):
                    pass
                else:
                    if not rec.get("grade_sha256"):
                        rec["grade_sha256"] = read_grade_integrity(run_dir)
                        _persist()
                    ordered.append(eid)
                    continue
        if _execution_complete(run_dir):
            rec["status"] = STATUS_EXECUTED
            _persist()
            try:
                candidate, outcome = _candidate_from_output(run_dir)
            except (OSError, ValueError, TypeError) as exc:
                raise TrainGradeError(
                    f"训练执行 {eid} 输出损坏，无法评分: {exc}") from exc
            meta = load_meta(run_dir)
            outcome = outcome or (meta.get("outcome") or {})
            grade_train_execution(
                root, dataset, task, eid, grader_version=grader_version,
                candidate=candidate, outcome=outcome, reviewer=reviewer)
            rec["status"] = STATUS_GRADED
            rec["grade_sha256"] = read_grade_integrity(run_dir)
            _persist()
            ordered.append(eid)
            continue
        res = run_one(root, dataset, task, profile=profile, skills=skill_set,
                      llm_runner_override=executor_runner, execution_id=eid)
        rec["status"] = STATUS_EXECUTED
        _persist()
        grade_train_execution(
            root, dataset, task, eid, grader_version=grader_version,
            candidate=res.get("candidate") or {},
            outcome=(res.get("meta") or {}).get("outcome") or {},
            reviewer=reviewer)
        rec["status"] = STATUS_GRADED
        rec["grade_sha256"] = read_grade_integrity(run_dir)
        _persist()
        ordered.append(eid)
    return ordered


def interpret_grade_kind(root: Path, meta: dict) -> str:
    """success / quality_failure / infra / unknown。grade 缺失不再等于 success。"""
    outcome = meta.get("outcome") or {}
    if outcome.get("failure_kind") in _INFRA_KINDS:
        return "infra"
    execution_id = meta.get("execution_id")
    if not execution_id:
        return "unknown"
    run_dir = Path(root) / "runs" / execution_id
    doc = load_grade_document(run_dir)
    if doc is None:
        return "unknown"
    if not verify_grade_integrity(run_dir):
        return "unknown"
    if doc.get("schema") != GRADE_SCHEMA:
        return "unknown"
    if doc.get("task_id") and meta.get("task_id") and \
            doc.get("task_id") != meta.get("task_id"):
        return "unknown"
    if doc.get("split") not in (None, "train") and doc.get("split") != meta.get("split"):
        return "unknown"
    meta_grader = meta.get("grader_version")
    if meta_grader and doc.get("grader_version") and \
            doc.get("grader_version") != meta_grader:
        return "unknown"
    fp = doc.get("fingerprints")
    if not isinstance(fp, dict):
        return "unknown"
    for key in _EVIDENCE_KEYS:
        if not fp.get(key):
            return "unknown"
    if fp.get("execution_id") != execution_id:
        return "unknown"
    outp = run_dir / "output.json"
    if not outp.is_file():
        return "unknown"
    try:
        payload = json.loads(outp.read_text(encoding="utf-8"))
        candidate = payload.get("candidate") or {}
        outc = payload.get("outcome") or {}
    except (OSError, ValueError, TypeError):
        return "unknown"
    if fp.get("output_sha256") != _sha256_file(outp):
        return "unknown"
    if fp.get("candidate_sha256") != _sha256_canonical(candidate):
        return "unknown"
    if fp.get("outcome_sha256") != _sha256_canonical(outc):
        return "unknown"
    try:
        from app.core.skill_evolution.trace import TraceError, verify_sealed
        seal = verify_sealed(run_dir)
    except (TraceError, OSError, ValueError):
        return "unknown"
    if fp.get("trace_seal") != seal:
        return "unknown"
    if meta.get("dataset_version") and fp.get("dataset_version") and \
            fp.get("dataset_version") != meta.get("dataset_version"):
        return "unknown"
    if meta_grader and fp.get("grader_version") and \
            fp.get("grader_version") != meta_grader:
        return "unknown"
    verdict = doc.get("verdict")
    if verdict in _PASS_VERDICTS:
        return "success"
    if verdict in _FAIL_VERDICTS:
        return "quality_failure"
    if verdict == "infra":
        return "infra"
    if verdict in _UNKNOWN_VERDICTS:
        return "unknown"
    return "unknown"


def _closed_grade_summary() -> dict:
    return {"kind": "unknown", "verdict": None, "checks_failed": [],
            "pending": True}


def safe_grade_summary(root: Path, meta: dict) -> dict:
    """维护/提议可见的安全摘要（无参考答案全文）。

    seal 缺失/失配或 interpret=unknown 时 fail-closed，不回传 grade.json
    中可能被篡改的 verdict/checks/pending。
    """
    execution_id = meta.get("execution_id")
    if not execution_id:
        return _closed_grade_summary()
    run_dir = Path(root) / "runs" / execution_id
    if not verify_grade_integrity(run_dir):
        return _closed_grade_summary()
    kind = interpret_grade_kind(root, meta)
    if kind == "unknown":
        return _closed_grade_summary()
    doc = load_grade_document(run_dir)
    if not doc:
        return _closed_grade_summary()
    failed = [c.get("id") for c in (doc.get("checks") or [])
              if not c.get("passed")]
    pending = doc.get("pending") or {}
    return {
        "kind": kind,
        "verdict": doc.get("verdict"),
        "grader_version": doc.get("grader_version"),
        "checks_failed": failed,
        "unresolved_ids": list(pending.get("unresolved_ids") or []),
        "pending": bool(pending.get("unresolved_ids") or
                        pending.get("evaluation_invalid") or
                        doc.get("verdict") in _UNKNOWN_VERDICTS),
    }
