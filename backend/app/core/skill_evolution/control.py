"""阶段 8C：演化运行控制服务（创建/启动/暂停/恢复/取消）。

边界落实：
- 所有状态来自实验库持久记录（evolution_runs）；刷新/服务重启后仍可查询；
- API 调用绝不长时间同步等待整轮演化：执行交给独立 worker（默认独立子进程，
  测试注入线程执行器），执行端复用 orchestrator 的 claim/租约/预算/检查点；
- 重复点击/并发操作受状态 CAS 与 orchestrator 租约双重保护：重复 start/resume
  先检查状态与活跃租约，再由 worker claim 兜底（LeaseConflict → 明确冲突）；
- 恢复不重置预算、不自动新建替代 run（execute(resume=True) 续用原 run 记录）；
- 取消只置终态/请求位，不删除任何迭代/评估/门控/技能记录；技能接受不回退；
- 创建/启动/恢复均由服务端校验作用域、数据集与参数；
- 真实模式：创建可排队，启动需显式授权开关且配置可解析（本轮默认关闭）；
- 模拟模式执行不产生任何网络模型调用（复用既有离线编排）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Callable

from app.core.skill_evolution import (
    config_freeze, gating, orchestrator as orch, runenv, skill_store)
from app.core.skill_evolution.contracts import DatasetError, load_dataset
from app.core.skill_evolution.errors import SkillEvolutionError

# 数据集根（仓库内受控任务集）；仅允许该目录下的版本目录。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
DATASETS_ROOT = _BACKEND_DIR / "eval" / "wiki_evolution" / "datasets"

# 评分器注册表/校准状态统一来自 grader_registry（v2 工程可运行；未校准不可自动晋升）。
from app.core.skill_evolution.grader_registry import (  # noqa: E402
    grader_state as _grader_state, require_known_grader,
    require_business_promotable, GRADER_V2)


class ControlError(SkillEvolutionError):
    """控制操作失败（HTTP 层映射为 409/503）。"""

    def __init__(self, message: str, code: str = "control_error",
                 *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


# ---------------------------------------------------------------------------
# 数据集 / 评分器可用性
# ---------------------------------------------------------------------------


def list_datasets() -> list[str]:
    if not DATASETS_ROOT.is_dir():
        return []
    return sorted(d.name for d in DATASETS_ROOT.iterdir()
                  if d.is_dir() and not d.name.startswith(".")
                  and any((d / n).is_file()
                          for n in ("dataset.json", "manifest.json")))


def dataset_dir_for(dataset_version: str) -> Path:
    d = (DATASETS_ROOT / dataset_version).resolve()
    if not d.is_dir() or not d.is_relative_to(DATASETS_ROOT.resolve()):
        raise ControlError(f"数据集不存在或不受控: {dataset_version!r}",
                           code="dataset_unknown", status_code=404)
    return d


def load_dataset_spec(dataset_version: str):
    try:
        return load_dataset(dataset_dir_for(dataset_version))
    except DatasetError as exc:
        raise ControlError(str(exc), code="dataset_invalid",
                           status_code=422) from exc


def grader_ready(grader_version: str) -> dict:
    return dict(_grader_state(grader_version))


def check_dataset_grader(dataset_version: str) -> dict:
    """数据集声明的评分器必须 known+ready，否则创建/启动一律拒绝。"""
    ds = load_dataset_spec(dataset_version)
    info = dict(_grader_state(ds.grader_version))
    if info["role"] == "unknown" or not info["ready"]:
        raise ControlError(
            f"数据集 {dataset_version} 声明的评分器 {ds.grader_version} 未就绪/未注册"
            f"：{info['description']}（禁止用于自动评分，不静默回退）",
            code="grader_not_ready")
    return info


# ---------------------------------------------------------------------------
# 创建（实验 + 种子 run；模拟/真实同路径，仅 runner 配置不同）
# ---------------------------------------------------------------------------

DEFAULT_BUDGETS = {"max_model_calls": 90, "max_tool_calls": 40,
                   "max_seconds": 3600}


def _new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:20]


def create_experiment_and_run(
    root: Path, *, dataset_version: str, init_mode: str = "business",
    max_iterations: int = 3, budget: dict | None = None,
    model_mode: str = "simulated", experience: str = "full",
    notes: str = "", review: str | None = None,
) -> dict:
    """创建最小实验 + 排队 run（不执行任何步骤/模型调用）。

    model_mode=simulated → runner 配置 mode=simulated/real=False；
    model_mode=real → 允许排队，但 start 前仍须通过预检与真实授权开关。
    """
    if model_mode not in ("simulated", "real"):
        raise ControlError(f"model_mode 非法: {model_mode!r}",
                           code="invalid_mode", status_code=422)
    if experience not in ("full", "none"):
        raise ControlError(f"experience 非法: {experience!r}",
                           code="invalid_experience", status_code=422)
    if not 1 <= int(max_iterations) <= 20:
        raise ControlError("max_iterations 必须在 1..20", code="invalid_iterations",
                           status_code=422)
    budget = {**DEFAULT_BUDGETS, **(budget or {})}
    if int(budget["max_model_calls"]) < 2 or int(budget["max_tool_calls"]) <= 0 \
            or int(budget["max_seconds"]) <= 0:
        raise ControlError("预算必须为正（model_calls>=2）",
                           code="invalid_budget", status_code=422)
    check_dataset_grader(dataset_version)
    ds = load_dataset_spec(dataset_version)
    if review is not None and review != "v2":
        raise ControlError(f"review 模式不支持: {review!r}（仅 v2）",
                           code="invalid_review", status_code=422)
    from app.core.skill_evolution.grader_registry import GRADER_V2
    if ds.grader_version == GRADER_V2 and review != "v2":
        raise ControlError(
            f"数据集 {dataset_version} 评分器为 {GRADER_V2}：创建必须显式携带 "
            "review='v2'（独立语义评审契约）；禁止创建 runner 无 reviewer 的 "
            "v2 记录", code="review_required_for_v2", status_code=422)
    if review == "v2":
        if ds.grader_version != GRADER_V2:
            raise ControlError(
                f"数据集 {dataset_version} 评分器 {ds.grader_version} 与 review=v2 "
                "不一致（不同 grader_version 分数不可直接比较）",
                code="grader_review_mismatch", status_code=422)
    train = [t for t in ds.tasks if t.split == "train"]
    val = [t for t in ds.tasks if t.split == "val"]
    if not train or not val:
        raise ControlError("数据集缺少 train/val 拆分", code="dataset_invalid",
                           status_code=422)
    from app.core.skill_evolution import trace_sampling as smp
    ws = smp.group_workspace_id([t.group_id for t in train][0])
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        seed = skill_store.import_seed(
            db, skill_store.load_package(
                _BACKEND_DIR / "eval" / "wiki_evolution" /
                "skills" / "seed-default-v1", source_type="builtin_seed"))
        v = skill_store.get_version(db, seed)
        exp_cfg = {"profile": "faithful",
                   "dataset_version": ds.dataset_version,
                   "grader_version": ds.grader_version,
                   "pipeline": ["wiki.default", "3"]}
        if review:
            exp_cfg["review"] = review
        exp = gating.create_experiment(
            db, workspace_id=ws, domain="wiki_compile.default", dataset=ds,
            grader_version=ds.grader_version,
            runner_config=exp_cfg,
            pipeline_key="wiki.default", pipeline_version="3",
            runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=[t.task_id for t in val],
            initial_members=[{"skill_id": v.skill_id,
                              "version_id": v.version_id,
                              "content_hash": v.content_hash, "seq": v.seq}])
        rconfig = {
            "mode": model_mode,
            "real": model_mode == "real",
            "dataset_dir": str(ds.dataset_dir),
            "dataset_version": dataset_version,
            "freeze_eligible": True,
        }
        if review:
            rconfig["review"] = review
        run = orch.create_run(
            db, experiment_id=exp.experiment_id, workspace_id=ws,
            domain="wiki_compile.default", dataset=ds,
            init_mode=init_mode, max_iterations=int(max_iterations),
            budget={k: int(v) for k, v in budget.items()},
            runner_config=rconfig,
            train_task_ids=[t.task_id for t in train],
            experience=experience)
        return {
            "experiment_id": str(exp.experiment_id),
            "run_id": str(run.run_id),
            "workspace_id": ws,
            "dataset_version": dataset_version,
            "grader_version": ds.grader_version,
            "model_mode": model_mode,
            "max_iterations": int(max_iterations),
            "status": "queued",
            "notes": notes,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 状态读取（只读；不经控制写路径）
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = ("queued", "running", "paused")


def run_state(root: Path, run_id: str) -> dict:
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        try:
            return orch.get_run(db, run_id)
        except orch.OrchestratorError as exc:
            raise ControlError(str(exc), code="run_not_found",
                               status_code=404) from exc
    finally:
        db.close()


def _read_run(root: Path, run_id: str):
    db = skill_store.session_for(root)
    try:
        from app.models.evolution import EvolutionRun
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        return db, row
    except Exception:
        db.close()
        raise


def freeze_run_runtime_config(root: Path, run_id: str, *, actor: str = "console-api") -> dict:
    """把 real 运行的非秘密角色/评审配置冻结进 run 记录（首次授权启动时执行）。

    之后 worker/resume 一律读取冻结配置，settings 漂移不影响该 run。
    """
    from sqlalchemy import update
    from app.models.evolution import EvolutionRun
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        cfg = json.loads(row.config_json)
        r = cfg.get("runner") or {}
        mode = r.get("mode") or "simulated"
        if mode != "real":
            return {"frozen": False, "reason": "simulated 不冻结"}
        require_real_provider_binding(cfg)
        if r.get("frozen"):
            return {"frozen": True,
                    "config_fingerprint": r.get("config_fingerprint"),
                    "reviewer_fingerprint": r.get("reviewer_fingerprint")}
        block = config_freeze.build_frozen_block(
            override=r.get("override"), review=r.get("review"))
        r["frozen"] = block
        r["config_fingerprint"] = config_freeze.fingerprint_of(block)
        rv = config_freeze.reviewer_fingerprint_of(block)
        if rv:
            r["reviewer_fingerprint"] = rv
        cfg["runner"] = r
        db.execute(update(EvolutionRun).where(EvolutionRun.run_id == run_id)
                   .values(config_json=json.dumps(cfg, ensure_ascii=False,
                                                  sort_keys=True)))
        db.commit()
        return {"frozen": True,
                "config_fingerprint": r["config_fingerprint"],
                "reviewer_fingerprint": r.get("reviewer_fingerprint")}
    finally:
        db.close()


def runtime_fingerprints_of(root: Path, run_id: str) -> dict:
    """已冻结配置指纹（UI 确认展示用）；未冻结返回 frozen=False。"""
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        from app.models.evolution import EvolutionRun
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        r = (json.loads(row.config_json).get("runner") or {})
        frozen = bool(r.get("frozen"))
        return {"frozen": frozen,
                "config_fingerprint": r.get("config_fingerprint"),
                "reviewer_fingerprint": r.get("reviewer_fingerprint")}
    finally:
        db.close()


def model_mode_of(root: Path, run_id: str) -> str:
    """run 配置声明的模型模式（simulated/real），读取持久记录。"""
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        from app.models.evolution import EvolutionRun
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        cfg = json.loads(row.config_json or "{}")
        return str((cfg.get("runner") or {}).get("mode") or "simulated")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# worker 启动（默认独立子进程；测试可替换为线程执行）
# ---------------------------------------------------------------------------


def _spawn_worker(root: Path, run_id: str, worker_id: str) -> dict:
    """独立子进程执行 evolution-run（进程隔离；不触碰生产全局 Registry/runner）。"""
    root = root.resolve()
    db, row = _read_run(root, run_id)
    try:
        cfg = json.loads(row.config_json)
        r = cfg.get("runner") or {}
        dataset_dir = str(r.get("dataset_dir") or cfg.get("dataset_version") or "")
        if not dataset_dir or not Path(dataset_dir).is_dir():
            raise ControlError("run 缺少可用数据集目录（无法启动 worker）",
                               code="dataset_unresolvable", status_code=422)
        cmd = [sys.executable, "-m", "app.core.skill_evolution.cli",
               "evolution-run", "--root", str(root),
               "--dataset", dataset_dir, "--run", run_id,
               "--worker", worker_id]
        log_dir = root / "runs" / "_console"
        log_dir.mkdir(parents=True, exist_ok=True)
        logf = (log_dir / f"{run_id}.log").open("ab")
        proc = subprocess.Popen(
            cmd, cwd=str(_BACKEND_DIR),
            stdout=logf, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"pid": proc.pid, "worker": worker_id, "log": str(logf.name)}
    finally:
        db.close()


def _thread_executor(root: Path, run_id: str, worker_id: str) -> dict:
    """测试用线程执行器：同一进程内执行 orchestrator（等价 worker 语义）。

    仅用于离线测试；生产/管理面默认使用子进程 _spawn_worker。
    真实模式同样走 build_run_actors（stub 后端可离线验证整条管理入口路径）。
    """
    def _body():
        try:
            ds_dir = None
            db, row = _read_run(root, run_id)
            try:
                cfg = json.loads(row.config_json)
                r = cfg.get("runner") or {}
                ds_dir = r.get("dataset_dir")
            finally:
                db.close()
            ds = load_dataset(Path(ds_dir))
            actors, _mode = build_run_actors(root, run_id)
            orch.execute(root, ds, run_id, actors=actors,
                         worker_id=worker_id, resume=True)
        except Exception as exc:  # noqa: BLE001
            _thread_executor.last_error = exc

    _thread_executor.last_error = None
    t = threading.Thread(target=_body, daemon=True,
                         name=f"console-worker-{run_id}")
    t.start()
    return {"pid": None, "worker": worker_id, "thread": t}


def build_run_actors(root: Path, run_id: str):
    """按 run 持久配置构造 worker 执行者（worker 侧权威，绝不静默降级）。

    - mode=simulated（或旧 run 无 mode）→ 默认模拟 Actors（faithful）；
    - mode=real → 真实适配器（executor/maintainer/proposer）经 run 记录的
      override/角色构造；未授权或缺配置 → 抛 ControlError（绝不回退模拟）。
    """
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        from app.models.evolution import EvolutionRun
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        cfg = json.loads(row.config_json)
        r = cfg.get("runner") or {}
        mode = r.get("mode") or "simulated"
        if mode != "real":
            return orch.Actors(execution_profile="faithful",
                               maintainer_profile=None), "simulated"
        from app.config import settings
        if not settings.wikiskill_evolution_admin_real_enabled:
            raise ControlError(
                "真实模式 worker 未授权（wikiskill_evolution_admin_real_enabled"
                "=false）：不得静默以模拟执行", code="real_not_authorized")
        require_real_provider_binding(cfg)
        override = r.get("override") or {}
        frozen = r.get("frozen")
        if not frozen:
            raise ControlError(
                "run 缺少冻结运行配置：旧记录需迁移/不可恢复（禁止用当前默认值补齐）",
                code="frozen_missing", status_code=409)
        from app.core.skill_evolution.config_freeze import FrozenConfigError
        try:
            actors = orch.Actors(
                execution_profile="faithful",
                executor_runner=config_freeze.build_actor_runner_frozen(
                    "executor", frozen),
                maintainer_runner=config_freeze.build_actor_runner_frozen(
                    "maintainer", frozen),
                proposer_factory=lambda: config_freeze.build_actor_runner_frozen(
                    "proposer", frozen))
        except FrozenConfigError as exc:
            raise ControlError(exc.message, code=exc.code,
                               status_code=exc.status_code) from exc
        # review=v2：真实模式必须装配独立语义评审器（评审模型独立显式配置）
        if r.get("review") == "v2":
            if not (frozen.get(config_freeze.FROZEN_REVIEWER_KEY)):
                raise ControlError(
                    "run 冻结配置缺少评审配置（review=v2）", code="frozen_missing",
                    status_code=409)
            from app.core.skill_evolution.config_freeze import FrozenConfigError
            try:
                reviewer = config_freeze.build_reviewer_frozen(frozen)
            except FrozenConfigError as exc:
                raise ControlError(exc.message, code=exc.code,
                                   status_code=exc.status_code) from exc
            actors = orch.Actors(
                execution_profile="faithful",
                executor_runner=actors.executor_runner,
                maintainer_runner=actors.maintainer_runner,
                proposer_factory=actors.proposer_factory,
                reviewer_factory=lambda rv=reviewer: rv)
        return actors, "real"
    finally:
        db.close()


def _confirm_from_record(row, cfg: dict) -> dict:
    budget = cfg.get("budget") or {}
    return {"dataset_version": row.dataset_version,
            "max_iterations": row.max_iterations,
            "max_model_calls": int(budget.get("max_model_calls") or 0),
            "max_tool_calls": int(budget.get("max_tool_calls") or 0),
            "max_seconds": int(budget.get("max_seconds") or 0)}


def _require_real_confirm(row, cfg: dict, confirm: dict | None, *,
                          resume: bool) -> None:
    """真实模式每次启动/恢复的显式确认：模型/数据范围/预算绑定运行记录。

    - confirm 必须携带 explicit=true；
    - confirm 中出现的参数必须与 run 记录一致（防以恢复名义扩大授权/切换数据）；
    - resume 不接受任何参数变更（预算/模型/范围一律沿用 run 记录）。
    """
    if not isinstance(confirm, dict) or not confirm.get("explicit"):
        raise ControlError(
            "真实模式启动需要显式确认（confirm.explicit=true）：模型、数据范围与"
            "预算绑定该运行记录", code="real_confirm_required")
    rec = _confirm_from_record(row, cfg)
    for key in ("dataset_version", "max_iterations", "max_model_calls",
                "max_tool_calls", "max_seconds"):
        if key in confirm and confirm[key] != rec[key]:
            raise ControlError(
                f"确认参数与运行记录不一致（{key}: 记录={rec[key]} 请求="
                f"{confirm[key]}）；恢复不切换模型/数据/预算", code="confirm_mismatch")
    if resume:
        extra = (set(confirm) - {"explicit"} - set(rec)
                 - {"config_fingerprint", "reviewer_fingerprint"})
        if extra:
            raise ControlError(
                f"恢复不接受额外参数变更: {sorted(extra)}",
                code="resume_no_param_change")


def start_preview(root: Path, run_id: str) -> dict:
    """无网络配置预览：展示模型/端点 host/提示词版本/输出限制/重试/数据范围/预算
    与完整配置指纹。不展示密钥、不发任何探测请求、不落盘冻结。

    - simulated → 内置模拟 runner（faithful），无外部端点；
    - real → 由当前受控配置现算“预期冻结块”并返回组合指纹（若随后配置漂移，
      start 将拒绝且不静默冻结新配置）；端点仅展示 host + api_key_present 布尔。
    """
    from app.config import settings
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        from app.models.evolution import EvolutionRun
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        cfg = json.loads(row.config_json or "{}")
        r = cfg.get("runner") or {}
        budget = cfg.get("budget") or {}
        base = {
            "run_id": run_id, "model_mode": r.get("mode") or "simulated",
            "dataset_version": row.dataset_version,
            "max_iterations": row.max_iterations,
            "budget": {k: int(budget.get(k) or 0)
                       for k in ("max_model_calls", "max_tool_calls",
                                 "max_seconds")},
            "status": row.status,
            "frozen": bool(r.get("frozen")),
            "real_authorized": bool(
                settings.wikiskill_evolution_admin_real_enabled),
        }
        if base["model_mode"] != "real":
            base.update({
                "preview_note": "simulated：内置 faithful 模拟 runner（不触网络、"
                                "不构造真实凭据）；测试模式独立标记，不会自动升级"
                                "为 real。",
                "roles": [], "config_fingerprint": None,
                "reviewer_fingerprint": None})
            return base
        try:
            prospective = config_freeze.build_frozen_block(
                override=r.get("override"), review=r.get("review"))
        except config_freeze.FrozenConfigError as exc:
            raise ControlError(
                f"real 配置不可用（预览失败，未触网络）: {exc.message}",
                code=exc.code or "real_preflight_failed", status_code=409) from exc
        roles = []
        for role in ("executor", "maintainer", "proposer"):
            fd = (prospective.get("roles") or {}).get(role)
            if not fd:
                continue
            roles.append({
                "role": role, "model": fd.get("model"),
                "endpoint_host": fd.get("endpoint_host"),
                "timeout": fd.get("timeout"), "retries": fd.get("retries"),
                "max_output_tokens": fd.get("max_output_tokens"),
                "provider_id": fd.get("provider_id"),
                "api_key_present": bool(fd.get("api_key_present")),
                "secret_bound": "仅受控引用（api_key_present 布尔，无密钥值）",
            })
        rv = prospective.get(config_freeze.FROZEN_REVIEWER_KEY)
        reviewer = None
        if rv:
            reviewer = {
                "model_id": rv.get("model_id"),
                "prompt_version": rv.get("prompt_version"),
                "template_version": rv.get("template_version"),
                "endpoint_host": rv.get("endpoint_host"),
                "timeout": rv.get("timeout"), "retries": rv.get("retries"),
                "max_output_tokens": rv.get("max_output_tokens"),
                "provider_id": rv.get("provider_id"),
                "credential_env_bound": bool(rv.get("credential_env")),
                "api_key_present": bool(rv.get("api_key_present")),
            }
        base.update({
            "preview_note": "real：以下配置为确认目标；确认后 start 绑定该指纹，"
                            "settings 漂移将被拒绝（不会静默冻结新配置）。密钥不"
                            "展示、不发探测请求。",
            "roles": roles, "reviewer": reviewer,
            "config_fingerprint": config_freeze.fingerprint_of(prospective),
            "reviewer_fingerprint": config_freeze.reviewer_fingerprint_of(
                prospective),
        })
        return base
    finally:
        db.close()


def require_real_provider_binding(cfg: dict) -> None:
    """正式 real 路径 provider 门禁：新真实运行必须使用受控 provider 映射。

    - providers 未配置（wikiskill_credential_providers 为空）→ 拒绝（不自动落入
      旧全局 key 模式）；
    - 默认 provider 未注册 → 拒绝（executor/maintainer/proposer 绑定）；
    - review=v2：评审 provider 未注册/未绑定 → 拒绝（评审角色同契约）。
    旧记录仍可读/展示；不满足契约的真实启动在首个 HTTP 请求前拒绝。
    """
    from app.config import settings as _s
    if not _s.wikiskill_require_provider_binding:
        return
    if (cfg.get("runner") or {}).get("mode") != "real":
        return
    from app.core.skill_evolution.real_adapters import provider_map
    pmap = provider_map()
    if not pmap:
        raise ControlError(
            "正式 real 运行要求受控 provider 配置（wikiskill_credential_providers"
            " 缺失）：禁止回落到旧全局 key 模式；请配置后创建新运行",
            code="real_provider_binding_required", status_code=409)
    pid = _s.wikiskill_default_provider
    if not pid or pid not in pmap:
        raise ControlError(
            "正式 real 运行缺少默认 provider 绑定（executor/maintainer/proposer"
            " 须受控注册）", code="real_provider_binding_required",
            status_code=409)
    for role in ("executor", "maintainer", "proposer"):
        prov = pmap.get(pid) or {}
        if not (prov.get("credential_env") and prov.get("endpoints")):
            raise ControlError(
                f"provider {pid} 未绑定凭据引用/端点（角色 {role}）：拒绝启动",
                code="real_provider_binding_required", status_code=409)
    if ((cfg.get("runner") or {}).get("review") == "v2"):
        rp = _s.wikiskill_reviewer_provider
        rprov = pmap.get(rp) if rp else None
        if not rprov or not (rprov.get("credential_env")
                             and rprov.get("endpoints")):
            raise ControlError(
                "review=v2 需要评审角色受控 provider 绑定"
                "（wikiskill_reviewer_provider 未注册/缺引用）：拒绝启动",
                code="real_provider_binding_required", status_code=409)


def worker_start_gate(root: Path, run_id: str) -> dict:
    """CLI/worker 共享的启动资格门（服务端同一校验逻辑，不弹交互确认）。

    - real：必须 已授权开关 + 受控创建（freeze_eligible）+ 持久化冻结配置
      （首次授权 start 时写入）——缺任一即拒绝，且不得以当前默认补齐；
    - simulated：独立显式标记，绝不因环境/参数自动升级为 real；
    - 返回 {"model_mode", "authorized": bool, "frozen": bool}。
    """
    from app.config import settings
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        from app.models.evolution import EvolutionRun
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        cfg = json.loads(row.config_json or "{}")
        r = cfg.get("runner") or {}
        mode = r.get("mode") or "simulated"
        if mode != "real":
            return {"model_mode": "simulated", "authorized": True,
                    "frozen": False,
                    "note": "simulated：显式模拟（不升级 real）"}
        if not settings.wikiskill_evolution_admin_real_enabled:
            raise ControlError(
                "真实模式 worker 未授权（wikiskill_evolution_admin_real_enabled"
                "=false）", code="real_not_authorized", status_code=409)
        require_real_provider_binding(cfg)
        if not r.get("freeze_eligible"):
            raise ControlError(
                "run 非受控创建（缺 freeze_eligible）：拒绝执行",
                code="frozen_missing", status_code=409)
        if not r.get("frozen"):
            raise ControlError(
                "run 缺持久化启动授权（runner.frozen）：worker 拒绝执行（须先经"
                "管理端 预览→确认→start 冻结）", code="frozen_missing",
                status_code=409)
        return {"model_mode": "real", "authorized": True, "frozen": True}
    finally:
        db.close()


def _real_preflight(root: Path, run_id: str) -> list[str]:
    """（保留：非冻结路径早期预检；冻结后走 _real_preflight_frozen）"""
    errors: list[str] = []
    from app.core.skill_evolution.real_adapters import (
        resolve_real_config, ModelBackendError)
    for role in ("executor", "maintainer", "proposer"):
        try:
            resolve_real_config(role)
        except ModelBackendError as exc:
            errors.append(f"{role}: {exc}")
    return errors


def _real_preflight_frozen(frozen_info: dict) -> list[str]:
    """冻结快照已完整生成 → 预检通过（角色/评审已在 build_frozen_block 校验）。"""
    errors: list[str] = []
    if not frozen_info.get("frozen"):
        errors.append("未冻结")
    return errors


def start(root: Path, run_id: str, *, actor: str = "console-api",
          confirm: dict | None = None) -> dict:
    """启动排队 run（仅 queued；paused 走 resume）。

    - 预检评分器可用；真实模式需显式授权开关 + 每次启动的显式确认（参数绑定记录）；
    - CAS（queued 且无在途 spawn 租约）抢占“启动租约”，重复 start/并发 start 直接冲突；
    - spawn worker；worker claim 时接管启动租约；worker 侧 build_run_actors 二次把关。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import update
    from app.models.evolution import EvolutionRun
    from app.config import settings
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        if row.status != "queued":
            raise ControlError(f"run 状态不可启动: {row.status}（仅 queued）",
                               code="invalid_state")
        cfg = json.loads(row.config_json)
        r = cfg.get("runner") or {}
        mode = r.get("mode") or "simulated"
        if mode == "real":
            if not settings.wikiskill_evolution_admin_real_enabled:
                raise ControlError(
                    "真实模式运行未经授权（wikiskill_evolution_admin_real_enabled"
                    "=false）；本轮仅模拟模式可执行", code="real_not_authorized")
            if not r.get("freeze_eligible"):
                raise ControlError(
                    "run 非受控创建（缺 freeze_eligible）：拒绝以当前默认配置运行",
                    code="frozen_missing", status_code=409)
            require_real_provider_binding(cfg)
            _require_real_confirm(row, cfg, confirm, resume=False)
            # 预览→确认绑定：确认指纹必须等于“当前配置现算的预期冻结指纹”。
            # 配置在预览后漂移 → 在此拒绝，绝不先静默冻结新配置再报错。
            cfp = (confirm or {}).get("config_fingerprint")
            if cfp is not None:
                try:
                    prospective = config_freeze.build_frozen_block(
                        override=r.get("override"), review=r.get("review"))
                except config_freeze.FrozenConfigError as exc:
                    raise ControlError(
                        f"real 配置当前不可用（无法绑定预览指纹）: {exc.message}",
                        code=exc.code or "real_preflight_failed",
                        status_code=409) from exc
                if cfp != config_freeze.fingerprint_of(prospective):
                    raise ControlError(
                        "确认配置指纹与当前配置不一致（预览后 settings 漂移）："
                        "拒绝启动，不会静默冻结新配置；请重新预览",
                        code="frozen_mismatch")
            frozen_info = freeze_run_runtime_config(root, run_id)
            if cfp is not None and cfp != frozen_info.get("config_fingerprint"):
                raise ControlError(
                    "冻结记录与确认指纹不一致（settings 漂移/确认过期）",
                    code="frozen_mismatch")
            if confirm and confirm.get("reviewer_fingerprint") is not None and \
                    frozen_info.get("reviewer_fingerprint") is not None and \
                    confirm["reviewer_fingerprint"] != frozen_info[
                        "reviewer_fingerprint"]:
                raise ControlError(
                    "确认评审配置指纹与冻结记录不一致",
                    code="frozen_mismatch")
            errs = _real_preflight_frozen(frozen_info)
            if errs:
                raise ControlError("真实模式冻结配置不可用：" + "; ".join(errs),
                                   code="real_preflight_failed")
        worker_id = f"console-{actor}-{uuid.uuid4().hex[:6]}"
        token = uuid.uuid4().hex
        res = db.execute(
            update(EvolutionRun)
            .where(EvolutionRun.run_id == run_id,
                   EvolutionRun.status == "queued",
                   EvolutionRun.lease_token.is_(None))
            .values(lease_owner=worker_id, lease_token=token,
                    lease_expires_at=datetime.now() + timedelta(seconds=45)))
        db.commit()
        if res.rowcount != 1:
            raise ControlError("run 正在启动或已被并发操作占用，请刷新后重试",
                               code="conflict")
        try:
            launched = _spawn_worker(root, run_id, worker_id)
        except Exception:
            db.execute(
                update(EvolutionRun)
                .where(EvolutionRun.run_id == run_id,
                       EvolutionRun.lease_token == token)
                .values(lease_owner=None, lease_token=None,
                        lease_expires_at=None))
            db.commit()
            raise
        return {"run_id": run_id, "status": "queued",
                "worker": worker_id, "pid": launched.get("pid"),
                "log": launched.get("log"), "model_mode": mode,
                "note": "已请求启动；实际进入 running 由 worker 领取（租约保护）"}
    finally:
        db.close()


def resume(root: Path, run_id: str, *, actor: str = "console-api",
           confirm: dict | None = None) -> dict:
    """恢复 paused run：CAS（paused 且无在途租约）→ 保持 paused 置启动租约后 spawn。

    不重置预算/已用计数、不切换模型/数据范围、不自动扩大授权；重复/并发恢复由
    CAS 拒绝；真实模式恢复同样要求显式确认（参数绑定记录）。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import update
    from app.models.evolution import EvolutionRun
    from app.config import settings
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        if row.status != "paused":
            raise ControlError(f"run 状态不可恢复: {row.status}（仅 paused）",
                               code="invalid_state")
        cfg = json.loads(row.config_json)
        r = cfg.get("runner") or {}
        mode = r.get("mode") or "simulated"
        if mode == "real":
            if not settings.wikiskill_evolution_admin_real_enabled:
                raise ControlError("真实模式未授权，无法恢复",
                                   code="real_not_authorized")
            if not r.get("freeze_eligible"):
                raise ControlError(
                    "run 非受控创建（缺 freeze_eligible）：拒绝恢复",
                    code="frozen_missing", status_code=409)
            require_real_provider_binding(cfg)
            _require_real_confirm(row, cfg, confirm, resume=True)
            frozen_info = freeze_run_runtime_config(root, run_id)
            expected = frozen_info.get("config_fingerprint")
            got = (confirm or {}).get("config_fingerprint")
            if not got or got != expected:
                raise ControlError(
                    "恢复必须确认完整配置指纹（记录="
                    f"{expected}）：settings 漂移不得影响该 run",
                    code="frozen_confirm_required")
            rvf = frozen_info.get("reviewer_fingerprint")
            if rvf and (confirm or {}).get("reviewer_fingerprint") != rvf:
                raise ControlError("恢复必须确认评审配置指纹",
                                   code="frozen_confirm_required")
            errs = _real_preflight_frozen(frozen_info)
            if errs:
                raise ControlError("真实模式冻结配置不可用：" + "; ".join(errs),
                                   code="real_preflight_failed")
        worker_id = f"console-{actor}-{uuid.uuid4().hex[:6]}"
        token = uuid.uuid4().hex
        res = db.execute(
            update(EvolutionRun)
            .where(EvolutionRun.run_id == run_id,
                   EvolutionRun.status == "paused",
                   EvolutionRun.lease_token.is_(None))
            .values(lease_owner=worker_id, lease_token=token,
                    lease_expires_at=datetime.now() + timedelta(seconds=45)))
        db.commit()
        if res.rowcount != 1:
            raise ControlError("run 正在被并发恢复/取消占用，请刷新", code="conflict")
        try:
            launched = _spawn_worker(root, run_id, worker_id)
        except Exception:
            db.execute(
                update(EvolutionRun)
                .where(EvolutionRun.run_id == run_id,
                       EvolutionRun.lease_token == token)
                .values(lease_owner=None, lease_token=None,
                        lease_expires_at=None))
            db.commit()
            raise
        return {"run_id": run_id, "status": "queued",
                "worker": worker_id, "pid": launched.get("pid"),
                "log": launched.get("log"), "model_mode": mode,
                "note": "恢复请求已发出：预算/已用计数沿用原 run，不新建替代 run"}
    finally:
        db.close()


def pause(root: Path, run_id: str, *, actor: str = "console-api") -> dict:
    """暂停：running → 置 pause_requested（worker 在检查点暂停）；
    queued → 直接终态 paused（无 worker）。paused → 幂等成功。"""
    from datetime import datetime
    from sqlalchemy import update
    from app.models.evolution import EvolutionRun
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        if row.status == "paused":
            return {"run_id": run_id, "status": "paused", "already": True}
        if row.status not in ("running", "queued"):
            raise ControlError(f"run 状态不可暂停: {row.status}",
                               code="invalid_state")
        if row.status == "queued":
            res = db.execute(
                update(EvolutionRun)
                .where(EvolutionRun.run_id == run_id,
                       EvolutionRun.status == "queued",
                       EvolutionRun.lease_token.is_(None))
                .values(status="paused", stop_reason="user_pause",
                        finished_at=datetime.now()))
            db.commit()
            if res.rowcount != 1:
                raise ControlError("run 正在启动或已并发变更", code="conflict")
            return {"run_id": run_id, "status": "paused",
                    "stop_reason": "user_pause"}
        # running → 请求位（worker 在安全检查点收敛；不直接抢终态）
        orch.pause_request(db, run_id)
        return {"run_id": run_id, "status": "running",
                "pause_requested": True,
                "note": "已请求暂停：worker 将在当前迭代/步骤边界收敛"}
    finally:
        db.close()


def cancel(root: Path, run_id: str, *, actor: str = "console-api") -> dict:
    """取消：running → cancel_requested（worker 收敛终态 cancelled）；
    queued/paused → CAS 直接终态 cancelled（无 worker 观察）。
    取消不删除任何迭代/评估/门控/技能记录，也不回退已接受版本。"""
    from datetime import datetime
    from sqlalchemy import update
    from app.models.evolution import EvolutionRun
    root = root.resolve()
    db = skill_store.session_for(root)
    try:
        row = db.get(EvolutionRun, run_id)
        if row is None:
            raise ControlError(f"run 不存在: {run_id}", code="run_not_found",
                               status_code=404)
        if row.status == "cancelled":
            return {"run_id": run_id, "status": "cancelled", "already": True}
        if row.status in ("completed", "failed", "budget_exhausted"):
            raise ControlError(f"run 已终态，无需取消: {row.status}",
                               code="invalid_state")
        if row.status == "running":
            orch.cancel_request(db, run_id)
            return {"run_id": run_id, "status": "running",
                    "cancel_requested": True,
                    "note": "已请求取消：worker 在安全边界收敛为 cancelled"}
        res = db.execute(
            update(EvolutionRun)
            .where(EvolutionRun.run_id == run_id,
                   EvolutionRun.status.in_(("queued", "paused")),
                   EvolutionRun.lease_token.is_(None))
            .values(status="cancelled", stop_reason="cancel_requested",
                    finished_at=datetime.now(), lease_token=None,
                    lease_expires_at=None))
        db.commit()
        if res.rowcount != 1:
            raise ControlError("run 状态已并发变更（可能刚被 worker 领取）",
                               code="conflict")
        return {"run_id": run_id, "status": "cancelled",
                "stop_reason": "cancel_requested"}
    finally:
        db.close()


# worker 启动可注入点（测试用线程执行器）
SUBPROCESS_SPAWNER: Callable = _spawn_worker
_spawn_worker_impl: Callable = SUBPROCESS_SPAWNER


_spawn_worker_impl: Callable = _spawn_worker


def set_worker_spawner(fn: Callable) -> None:
    global _spawn_worker_impl
    _spawn_worker_impl = fn


def _spawn_worker(root: Path, run_id: str, worker_id: str) -> dict:
    return _spawn_worker_impl(root, run_id, worker_id)
