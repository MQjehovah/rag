"""阶段 6：多轮进化调度、预算、暂停与恢复（orchestrator）。

真实子模块串联（不复制评估/维护/提议）：训练（adapter）→ 维护（maintainer）→
提议（proposer）→ 评估+门控（gating），本模块只做轮次状态机、检查点指针、
租约、预算与恢复，关联 id 持久化到 EvolutionRun/EvolutionIteration（P49）。

- 恢复策略：步骤完成后持久化指针；中断后从当前步骤重跑——子模块幂等键保证
  不重复应用经验补丁/创建候选/晋升/追加 gate 事件；
- 租约：claim 原子领取 + 心跳；关键写入 _fence 校验 token 与过期时间，
  过期旧 worker 不得回写/晋升；
- 预算：max_iterations / max_model_calls / max_tool_calls / max_seconds；
  所有角色（维护+提议）调用计数；超预算 → budget_exhausted 独立终态；
  未知 token usage=null（估算另记，不虚构精确费用上限）；
- 外部模型调用不做 exactly-once 声明：只记录窗口并把重试计入预算；
- 初始化模式：paper=空技能+空经验；business=显式种子+显式初始经验快照。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.core.skill_evolution import (
    experience_store as exp,
    gating as gate,
    maintainer as maint,
    proposer as prop,
    skill_store,
)
from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import DatasetSpec
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.injector import FrozenSkillSet
from app.core.skill_evolution.train_grading import (
    TrainGradeError,
    ensure_train_tasks_graded,
)
from app.models.evolution import (
    ITER_DONE,
    ITER_FAILED,
    ITER_PAUSED,
    ITER_RUNNING,
    ITER_STEP_DONE,
    ITER_STEP_EVAL,
    ITER_STEP_MAINTAIN,
    ITER_STEP_PROPOSE,
    ITER_STEP_TRAIN,
    RUN_BUDGET_EXHAUSTED,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_PAUSED,
    RUN_QUEUED,
    RUN_RUNNING,
    STOP_MAX_ITERATIONS,
    STOP_PERFECT_SCORE,
    EvolutionIteration,
    EvolutionRun,
)

DEFAULT_LEASE_SECONDS = 600
STEP_ORDER = (ITER_STEP_TRAIN, ITER_STEP_MAINTAIN, ITER_STEP_PROPOSE,
              ITER_STEP_EVAL)


class OrchestratorError(SkillEvolutionError):
    pass


class LeaseConflict(OrchestratorError):
    pass


class BudgetExceeded(OrchestratorError):
    """预算前置检查失败：请求未发出即被阻止（预估值/上限分开记录）。"""


class LongCallRenewer(threading.Thread):
    """长模型调用期间的租约续租守护线程（正常 worker 持续续租）。

    停止续租（stop）后租约自然过期，允许其它 worker 接管；stop 释放线程资源。
    """

    def __init__(self, root, run_id: str, token: str,
                 interval: float = 5.0):
        super().__init__(daemon=True)
        self._root = Path(root)
        self._run_id = run_id
        self._token = token
        self._interval = float(interval) if interval is not None else 5.0
        if self._interval <= 0:
            self._interval = 1.0
        self._stop_evt = threading.Event()
        self.lost = False

    def run(self) -> None:
        while not self._stop_evt.wait(self._interval):
            db = skill_store.session_for(self._root)
            try:
                ok = heartbeat(db, self._run_id, self._token)
            finally:
                db.close()
            if not ok:
                self.lost = True
                return

    def stop(self) -> None:
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=5.0)


class Clock:
    """可注入时钟（测试用确定性 fake clock；生产默认 datetime.now）。"""

    def now(self) -> datetime:
        return datetime.now()


CLOCK = Clock()


def _now() -> datetime:
    return CLOCK.now()


def _start_active_segment(row) -> None:
    """开始新的 active 段。须在已结算旧段之后调用，禁止直接覆盖旧起点。"""
    row.active_segment_started_at = _now()


def _settle_active_segment(row, *, until) -> None:
    """把当前 active 段累计到 until（崩溃接管用 min(now, lease_expires)）。"""
    started = getattr(row, "active_segment_started_at", None)
    if started is None:
        return
    elapsed = max(0.0, (until - started).total_seconds())
    row.used_active_seconds = float(row.used_active_seconds or 0) + elapsed
    row.active_segment_started_at = None


def _stop_active_segment(row) -> None:
    """结束当前 active 段，把经过秒数累加到 used_active_seconds。"""
    _settle_active_segment(row, until=_now())


def _active_elapsed_seconds(row) -> float:
    used = float(getattr(row, "used_active_seconds", 0) or 0)
    started = getattr(row, "active_segment_started_at", None)
    if started is None:
        return used
    return used + max(0.0, (_now() - started).total_seconds())


class BudgetGuard:
    """发送前预算守卫（DB 持久在途标记 + 整次 run 累计时间/工具次数）。

    - reserve_model：发送前检查 上限 - used - in_flight > 0 并落库在途标记；
    - finish_model：收到已知结果（成功/明确失败）→ 清除标记；无法确定是否已发送
      （异常/崩溃）→ 标记保留，保守记为“已预留/发送状态未知”；
    - reserve_tool / finish_tool：Proposer 每次实际工具调用前预留并在尝试后消费；
      失败也计为已尝试；崩溃后 init 把遗留 tool reservation 转为已消费；
    - max_seconds = 同一 run 的累计 active 秒数；暂停不计入；resume 不重置；
    - 恢复时从 DB 读取 used_active_seconds / in_flight，崩溃不丢计数。
    """

    def __init__(self, db, run_row, *, invocation_max_seconds: float | None = None):
        self._db = db
        self._run_id = run_row.run_id
        self._token = run_row.lease_token
        self.budget_exhausted = False
        cfg = json.loads(run_row.config_json)
        self._budget = cfg.get("budget") or {}
        self._max_model = int(self._budget.get("max_model_calls") or 0)
        self._max_tool = int(self._budget.get("max_tool_calls") or 0)
        self._max_seconds = int(self._budget.get("max_seconds") or 0)
        self._invocation_max = (
            float(invocation_max_seconds)
            if invocation_max_seconds is not None and invocation_max_seconds > 0
            else None)
        self._invocation_started = _now() if self._invocation_max else None
        try:
            self._inflight = json.loads(run_row.reserved_in_flight_json or "[]")
        except ValueError:
            self._inflight = []
        self._marks: dict[str, str] = {}
        self.external_counted = False   # 真实执行 runner 已由 envelope 计数
        self._recover_tool_reservations()
        row = self._fresh()
        if row.status == RUN_RUNNING and getattr(
                row, "active_segment_started_at", None) is None:
            _start_active_segment(row)
            self._db.commit()

    def _fresh(self):
        return self._db.get(EvolutionRun, self._run_id)

    def _persist_inflight(self) -> None:
        row = self._fresh()
        row.reserved_in_flight_json = json.dumps(self._inflight)
        self._db.commit()

    def _used(self) -> tuple[int, int]:
        row = self._fresh()
        return (int(row.used_model_calls or 0),
                int(row.used_tool_calls or 0))

    def _tool_inflight_count(self) -> int:
        return sum(1 for m in self._inflight
                   if isinstance(m, dict) and str(m.get("kind") or "").startswith("tool"))

    def _recover_tool_reservations(self) -> None:
        """崩溃后把未完成的 tool reservation 转为已消费，避免遗留或丢失。"""
        leftover = [m for m in self._inflight
                    if isinstance(m, dict) and str(m.get("kind") or "").startswith("tool")]
        if not leftover:
            return
        row = self._fresh()
        row.used_tool_calls = int(row.used_tool_calls or 0) + len(leftover)
        self._inflight = [m for m in self._inflight
                          if not (isinstance(m, dict)
                                  and str(m.get("kind") or "").startswith("tool"))]
        row.reserved_in_flight_json = json.dumps(self._inflight)
        self._db.commit()

    # -- 查询 --------------------------------------------------------------
    def model_available(self) -> int:
        if self._max_model <= 0:
            return 1  # 未配置上限视为不限
        used, _ = self._used()
        model_inflight = sum(1 for m in self._inflight
                             if not (isinstance(m, dict)
                                     and str(m.get("kind") or "").startswith("tool")))
        return self._max_model - used - model_inflight

    def tool_available(self) -> int:
        if self._max_tool <= 0:
            return 10 ** 9
        _, used_tools = self._used()
        return max(0, self._max_tool - used_tools - self._tool_inflight_count())

    def seconds_remaining(self) -> float:
        row = self._fresh()
        run_rem = float("inf")
        if self._max_seconds > 0:
            run_rem = max(0.0, float(self._max_seconds) - _active_elapsed_seconds(row))
        if self._invocation_max is None:
            return run_rem
        used_inv = max(0.0, (_now() - self._invocation_started).total_seconds())
        inv_rem = max(0.0, self._invocation_max - used_inv)
        return min(run_rem, inv_rem)

    def active_elapsed(self) -> float:
        return _active_elapsed_seconds(self._fresh())

    # -- 发送前预留/完成 ---------------------------------------------------
    def _check_lease(self) -> None:
        """租约丢失后旧 worker 不得继续发起新请求（token 已被接管即拒绝）。"""
        row = self._fresh()
        if row is None or row.lease_token != self._token:
            raise LeaseConflict("租约丢失：旧 worker 不得继续发起新请求")
        if not row.lease_expires_at or row.lease_expires_at <= _now():
            raise LeaseConflict("租约过期：旧 worker 不得继续发起新请求")

    def _check_time(self) -> None:
        if self.seconds_remaining() <= 0:
            self.budget_exhausted = True
            raise BudgetExceeded("运行时间预算已满（发送前阻止）")

    def reserve_model(self) -> str:
        self._check_lease()
        if self._max_model > 0 and self.model_available() <= 0:
            self.budget_exhausted = True
            raise BudgetExceeded("model 调用预算已满（发送前阻止）")
        self._check_time()
        mark = _uuid.uuid4().hex
        self._inflight.append({"id": mark, "kind": "model", "ts": _now().timestamp()})
        self._persist_inflight()
        return mark

    def finish_model(self, mark: str, *, sent_known: bool) -> None:
        self._inflight = [m for m in self._inflight if m.get("id") != mark]
        if sent_known:
            row = self._fresh()
            row.used_model_calls = int(row.used_model_calls or 0) + 1
            self._db.commit()
        else:
            self._inflight.append({"id": mark, "kind": "model-unknown",
                                   "ts": _now().timestamp()})
        self._persist_inflight()

    def reserve_tool(self) -> str:
        """每次实际工具调用前检查并预留 1 次（失败也必须 finish 消费）。"""
        self._check_lease()
        self._check_time()
        if self._max_tool > 0 and self.tool_available() <= 0:
            self.budget_exhausted = True
            raise BudgetExceeded("tool 调用预算已满（执行前阻止）")
        mark = _uuid.uuid4().hex
        self._inflight.append({"id": mark, "kind": "tool", "ts": _now().timestamp()})
        self._persist_inflight()
        return mark

    def finish_tool(self, mark: str, *, attempted: bool = True) -> None:
        self._inflight = [m for m in self._inflight if m.get("id") != mark]
        if attempted:
            row = self._fresh()
            row.used_tool_calls = int(row.used_tool_calls or 0) + 1
            self._db.commit()
        self._persist_inflight()

    def check_steps_ok(self) -> bool:
        return not (self._max_model > 0 and self.model_available() <= 0) and             not (self._max_tool > 0 and self.tool_available() <= 0) and             not (self.seconds_remaining() <= 0)

    def check_steps(self) -> None:
        """每步/每次循环开始前检查（预估值上限；费用上限不可控则跳过）。"""
        if not self.check_steps_ok():
            self.budget_exhausted = True
            raise BudgetExceeded("预算/时间已满（发送前阻止）")

    def wrap(self, runner: Callable) -> Callable:
        """包住真实 runner：发送前预留 + 超时截断；不重复计数（事件为计数源）。"""
        def _wrapped(messages, context="", timeout=120.0):
            mark = self.reserve_model()
            remaining = self.seconds_remaining()
            eff = timeout if remaining == float("inf") else min(timeout, remaining)
            try:
                result = runner(messages, context=context, timeout=eff)
                self.finish_model(mark, sent_known=True)
                return result
            except Exception:  # noqa: BLE001
                self.finish_model(mark, sent_known=False)
                raise
        return _wrapped


# ---------------------------------------------------------------------------
# 运行创建 / 控制
# ---------------------------------------------------------------------------


def create_run(
    db: Session, *, experiment_id: str, workspace_id: str, domain: str,
    dataset: DatasetSpec, init_mode: str, max_iterations: int,
    budget: dict, runner_config: dict, train_task_ids: list[str],
    initial_experience_snapshot: dict | None = None,
    experience: str = "full",
    evolve: bool = True,
) -> EvolutionRun:
    if init_mode not in ("paper", "business"):
        raise OrchestratorError(f"init_mode 非法: {init_mode}")
    if max_iterations < 1:
        raise OrchestratorError("max_iterations 必须 >=1")
    exp_info = gate.get_experiment(db, experiment_id)
    if exp_info.workspace_id != workspace_id or exp_info.domain != domain:
        raise OrchestratorError("experiment 与运行作用域不一致")
    if exp_info.dataset_version != dataset.dataset_version:
        raise OrchestratorError("experiment 与数据集版本不一致")
    if not train_task_ids:
        raise OrchestratorError("train 任务清单为空")
    cfg = {
        "runner": dict(runner_config),
        "budget": dict(budget),
        "train_task_ids": list(train_task_ids),
        "val_task_ids": list(exp_info.runner_config.get("val_task_ids") or []),
        "dataset_version": dataset.dataset_version,
        "init_mode": init_mode,
        "experience": experience,
        "evolve": bool(evolve),
    }
    initial_set = gate.build_set_from_members(
        db, (exp_info.initial_skill_set or {}).get("members") or [])
    run_id = "run_" + _uuid.uuid4().hex[:20]
    row = EvolutionRun(
        run_id=run_id, experiment_id=experiment_id,
        workspace_id=workspace_id, domain=domain,
        dataset_version=dataset.dataset_version, init_mode=init_mode,
        config_json=json.dumps(cfg, ensure_ascii=False, sort_keys=True),
        initial_skill_set_json=json.dumps(initial_set.to_dict(), ensure_ascii=False),
        initial_experience_snapshot_json=json.dumps(
            initial_experience_snapshot or {}, ensure_ascii=False, default=str),
        max_iterations=max_iterations, current_iteration=0,
        status=RUN_QUEUED, used_model_calls=0, used_tool_calls=0,
        used_estimated_chars=0, pause_requested=False, cancel_requested=False,
    )
    db.add(row)
    db.commit()
    return db.get(EvolutionRun, run_id)


def claim(db: Session, run_id: str, worker_id: str = "worker",
           resume_clear: bool = False) -> EvolutionRun:
    row = db.get(EvolutionRun, run_id)
    if row is None:
        raise OrchestratorError(f"run 不存在: {run_id}")
    active_lease = (row.status == RUN_RUNNING and row.lease_expires_at
                    and row.lease_expires_at > _now())
    if active_lease:
        raise LeaseConflict("run 正在被其他 worker 运行")
    if row.status not in (RUN_QUEUED, RUN_PAUSED, RUN_RUNNING):
        raise OrchestratorError(f"run 状态不可运行: {row.status}")
    previous_lease_expires = row.lease_expires_at
    started = getattr(row, "active_segment_started_at", None)
    if started is not None:
        end = _now()
        if previous_lease_expires is not None:
            end = min(end, previous_lease_expires)
        _settle_active_segment(row, until=end)
    token = _uuid.uuid4().hex
    row.status = RUN_RUNNING
    row.lease_owner = worker_id
    row.lease_token = token
    row.lease_expires_at = _now() + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    if resume_clear:
        row.pause_requested = False
        row.cancel_requested = False
    row.started_at = row.started_at or _now()
    _start_active_segment(row)
    db.commit()
    return db.get(EvolutionRun, run_id)


def heartbeat(db: Session, run_id: str, token: str) -> bool:
    """续租（长模型调用期间由持有 token 的 worker 定期调用）。

    租约已过期 → 返回 False（旧 worker 不得续租续命；由新 worker 接管）。
    """
    row = db.get(EvolutionRun, run_id)
    if row is None or row.status != RUN_RUNNING or row.lease_token != token:
        return False
    if not row.lease_expires_at or row.lease_expires_at <= _now():
        return False
    row.lease_expires_at = _now() + timedelta(seconds=DEFAULT_LEASE_SECONDS)
    started = getattr(row, "active_segment_started_at", None)
    if started is not None:
        elapsed = max(0.0, (_now() - started).total_seconds())
        row.used_active_seconds = float(row.used_active_seconds or 0) + elapsed
        row.active_segment_started_at = _now()
    db.commit()
    return True


def pause_request(db: Session, run_id: str) -> None:
    row = db.get(EvolutionRun, run_id)
    if row is None:
        raise OrchestratorError("run 不存在")
    row.pause_requested = True
    db.commit()


def cancel_request(db: Session, run_id: str) -> None:
    row = db.get(EvolutionRun, run_id)
    if row is None:
        raise OrchestratorError("run 不存在")
    row.cancel_requested = True
    db.commit()


def get_run(db: Session, run_id: str) -> dict:
    row = db.get(EvolutionRun, run_id)
    if row is None:
        raise OrchestratorError("run 不存在")
    iters = (db.query(EvolutionIteration)
             .filter(EvolutionIteration.run_id == run_id)
             .order_by(EvolutionIteration.number.asc()).all())
    return {
        "run_id": row.run_id, "experiment_id": row.experiment_id,
        "workspace_id": row.workspace_id, "dataset_version": row.dataset_version,
        "init_mode": row.init_mode, "status": row.status,
        "stop_reason": row.stop_reason,
        "current_iteration": row.current_iteration,
        "max_iterations": row.max_iterations,
        "used": {"model_calls": row.used_model_calls,
                 "tool_calls": row.used_tool_calls,
                 "estimated_chars": row.used_estimated_chars,
                 "active_seconds": float(getattr(row, "used_active_seconds", 0) or 0)},
        "lease_owner": row.lease_owner, "lease_token_present": bool(row.lease_token),
        "pause_requested": bool(row.pause_requested),
        "cancel_requested": bool(row.cancel_requested),
        "error_code": row.error_code, "error_message": row.error_message,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "iterations": [_iter_view(i) for i in iters],
    }


def _iter_view(it: EvolutionIteration) -> dict:
    from app.core.skill_evolution.train_grading import (
        execution_ids_from_state, parse_train_task_state)
    records = parse_train_task_state(it.train_execution_ids_json)
    return {
        "iteration_id": it.iteration_id, "number": it.number,
        "status": it.status, "step": it.step,
        "freeze_set": json.loads(it.freeze_set_json),
        "train_execution_ids": execution_ids_from_state(records),
        "maintenance_run_id": it.maintenance_run_id,
        "proposal_run_id": it.proposal_run_id, "proposal_id": it.proposal_id,
        "evaluation_id": it.evaluation_id, "gate_event_id": it.gate_event_id,
        "no_action": bool(it.no_action), "attempts": it.attempts,
        "error_code": it.error_code, "error_message": it.error_message,
    }


# ---------------------------------------------------------------------------
# 内部：租约与指针
# ---------------------------------------------------------------------------


def _fence(db: Session, run_row: EvolutionRun, token: str) -> bool:
    row = db.get(EvolutionRun, run_row.run_id)
    if row is None or row.status != RUN_RUNNING or row.lease_token != token:
        return False
    if not row.lease_expires_at or row.lease_expires_at <= datetime.now():
        return False
    return True


def _commit_guarded(db: Session, run_row, token: str) -> None:
    if not _fence(db, run_row, token):
        db.rollback()
        raise LeaseConflict("租约丢失/过期：旧 worker 不得回写")
    db.commit()


def _next_step(current: str) -> str:
    idx = STEP_ORDER.index(current)
    return STEP_ORDER[idx + 1] if idx + 1 < len(STEP_ORDER) else ITER_STEP_DONE


# ---------------------------------------------------------------------------
# 脚本化策略提议者（测试/演示；真实模型经同一接口接入）
# ---------------------------------------------------------------------------


class PolicyProposer:
    """确定性轮次策略（演示设施；走真实工具/补丁/存储，非捷径）：

    iteration 1 → patch STRICT-V1（相对基线改善）；2 → patch STRICT-V1-ALT
    （与最佳持平）；>=3 → 先读技能门控影响历史，再 no_action。
    自管理多轮读取状态（不依赖内层 proposer 的 finish 状态机）。
    """

    def __init__(self, iteration: int, parent_version_id: str | None,
                 parent_skill_md: str):
        self._iteration = iteration
        self._parent = parent_version_id
        self._marker = {1: "STRICT-V1", 2: "STRICT-V1-ALT"}.get(iteration)
        self._line = next((ln for ln in parent_skill_md.splitlines()
                           if ln.startswith("8. ")), None)
        self._pending: list[str] = []
        self._history_read = False
        self._done = False
        self._parsed = False
        self._directive = {
            "STRICT-V1": "8. 参数与结论必须与来源一致；输出 STRICT-V1 核对清单。",
            "STRICT-V1-ALT":
                "8. 参数与来源一致；结尾附 STRICT-V1 核对清单（逐条覆盖条件要点）。",
        }

    def __call__(self, messages, context="", timeout=120.0):
        if self._done:
            raise prop.ProposerError("重复 finish")
        prompt = "\n".join((m.get("content") or "") if isinstance(m, dict) else ""
                            for m in (messages or []))
        if not self._parsed:
            section = prompt.split("--- 授权训练摘要 ---", 1)
            section = section[1].split("--- 当前技能集合", 1)[0] if len(section) > 1 else prompt
            ids = re.findall(r"- ([0-9a-f]{32}) .*kind=\w+", section)
            self._pending = list(dict.fromkeys(ids))
            self._parsed = True
        if self._pending:
            eid = self._pending.pop(0)
            return {"tool": "read_trace", "args": {"execution_id": eid}}
        evidence = list(self._pending_read(messages))
        if not self._marker or not self._parent or not self._line:
            if not self._history_read:
                self._history_read = True
                return {"tool": "read_skill_history", "args": {"skill_id": "default"}}
            self._done = True
            return {"action": {"type": "no_action",
                               "reason": f"策略：第 {self._iteration} 轮无修改"
                                         "（已读门控影响历史）",
                               "evidence_execution_ids": evidence,
                               "pattern_ids": [], "pattern_revision_ids": []}}
        self._done = True
        return {"action": {"type": "patch", "skill_id": "default",
                           "parent_version_id": self._parent,
                           "parent_content_hash": None,
                           "ops": [{"file": "SKILL.md", "type": "replace",
                                    "anchor": self._line,
                                    "replacement": self._directive[self._marker]}],
                           "reason": f"策略候选（第 {self._iteration} 轮，"
                                     f"{self._marker}；未评估）",
                           "evidence_execution_ids": evidence,
                           "pattern_ids": [], "pattern_revision_ids": []}}

    @staticmethod
    def _pending_read(messages) -> list[str]:
        import re as _re
        prompt = "\n".join((m.get("content") or "") if isinstance(m, dict) else ""
                            for m in (messages or []))
        return _re.findall(r"\b[0-9a-f]{32}\b", prompt)[:4]


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


@dataclass
class Actors:
    execution_profile: str = "faithful"
    maintainer_profile: str | None = None   # None → 有 pattern 则 update，否则 create
    proposer_factory: Callable | None = None
    executor_runner: Callable | None = None  # 真实/外部执行 runner（None=模拟 faithful）
    maintainer_runner: Callable | None = None  # 真实 Maintainer runner（None=模拟）
    reviewer_factory: Callable | None = None  # 语义评审器工厂（v2 评审；None=无）



def _reviewer_for(actors: Actors, guard: "BudgetGuard | None"):
    """由 actors.reviewer_factory 构造评审器；真实评审器发送经 guard 包裹（预算约束）。"""
    reviewer = None
    if actors.reviewer_factory is not None:
        reviewer = actors.reviewer_factory()
    if reviewer is not None and guard is not None and hasattr(reviewer, "attach_sender"):
        reviewer.attach_sender(guard.wrap(reviewer._send_http))
    return reviewer


def _count_review_calls(run_row, view: dict, guard) -> None:
    n = int(view.get("review_model_calls") or 0)
    if n and not (guard is not None and guard.external_counted):
        run_row.used_model_calls = run_row.used_model_calls + n


def _parent_skill_text(root: Path, version_id: str) -> str:
    db = skill_store.session_for(root)
    try:
        return skill_store.get_version(db, version_id).skill_md
    finally:
        db.close()


def execute(root, dataset: DatasetSpec, run_id: str, *,
            worker_id: str = "worker", actors: Actors | None = None,
            max_wall_seconds: int | None = None, resume: bool = False) -> dict:
    """运行/恢复一次演化运行（claim→循环→终态）。resume=True 清除暂停/取消请求。"""
    actors = actors or Actors()
    root = Path(root)
    db = skill_store.session_for(root)
    run_row = claim(db, run_id, worker_id=worker_id, resume_clear=resume)
    token = run_row.lease_token
    budget = json.loads(run_row.config_json)["budget"]
    budget_max = int(budget.get("max_seconds") or 0)
    guard = BudgetGuard(db, run_row, invocation_max_seconds=max_wall_seconds)
    interval_base = budget_max if budget_max > 0 else DEFAULT_LEASE_SECONDS
    renewer = LongCallRenewer(root, run_id, token,
                              interval=min(5.0, max(1.0, interval_base / 4.0)))
    if actors.executor_runner is not None:
        actors.executor_runner = guard.wrap(actors.executor_runner)
        guard.external_counted = True   # 真实执行由 envelope 计数

    try:
        renewer.start()
        while True:
            run_row = db.get(EvolutionRun, run_id)
            guard.check_steps()
            if run_row.cancel_requested:
                _set_terminal(db, run_row, token, RUN_CANCELLED,
                              "cancel_requested")
                return get_run(db, run_id)
            if run_row.pause_requested:
                _pause(db, run_row, token, reason="user_pause")
                return get_run(db, run_id)
            if not guard.check_steps_ok():
                _set_terminal(db, run_row, token, RUN_BUDGET_EXHAUSTED,
                              "budget_exhausted")
                return get_run(db, run_id)

            exp_info = gate.get_experiment(db, run_row.experiment_id)
            if exp_info.best_score is not None and \
                    exp_info.best_score["passed"] == exp_info.best_score["total"]:
                _set_terminal(db, run_row, token, RUN_COMPLETED, STOP_PERFECT_SCORE)
                return get_run(db, run_id)
            if exp_info.best_score is None:
                reviewer = _reviewer_for(actors, guard)
                base = gate.run_baseline(
                    root, dataset, run_row.experiment_id,
                    profile=actors.execution_profile,
                    executor_runner=actors.executor_runner, reviewer=reviewer)
                _count_review_calls(run_row, base, guard)
                _commit_guarded(db, run_row, run_row.lease_token)
                if not base["valid"]:
                    if guard.budget_exhausted:
                        raise BudgetExceeded(
                            "预算已满（baseline 无效前的发送已被拦截）")
                    _pause(db, run_row, token,
                           reason=f"baseline_invalid:{base.get('invalid_reason')}")
                    return get_run(db, run_id)
                if base["score"]["passed"] == base["score"]["total"]:
                    _set_terminal(db, run_row, token, RUN_COMPLETED,
                                  STOP_PERFECT_SCORE)
                    return get_run(db, run_id)

            cfg = json.loads(run_row.config_json)
            if not cfg.get("evolve", True):
                _set_terminal(db, run_row, token, RUN_COMPLETED,
                              STOP_MAX_ITERATIONS)
                return get_run(db, run_id)

            if run_row.current_iteration >= run_row.max_iterations:
                _set_terminal(db, run_row, token, RUN_COMPLETED,
                              STOP_MAX_ITERATIONS)
                return get_run(db, run_id)

            n = run_row.current_iteration + 1
            existing = (db.query(EvolutionIteration)
                        .filter(EvolutionIteration.run_id == run_id,
                                EvolutionIteration.number == n).first())
            if existing is not None and existing.status in ("running", "paused"):
                it = existing
            else:
                it = _new_iteration(db, run_row, n)
                _commit_guarded(db, run_row, token)

            outcome = _run_iteration(root, dataset, run_row, it, token,
                                     actors, db, guard=guard)
            if outcome == "paused":
                # 轮次步骤机在迭代/步骤边界检测到暂停并已把该轮置 ITER_PAUSED；
                # 若 run 仍 running，这里把 run 级状态持久化为 paused 并释放租约
                # （否则 run 停在 running、租约悬挂、resume 无法 CAS）。
                # 内部分支（eval_invalid/失败降级等）已自行 _pause 落盘时不再二次写。
                run_row = db.get(EvolutionRun, run_id)
                if run_row is not None and run_row.status == RUN_RUNNING:
                    _pause(db, run_row, token, reason="user_pause")
                return get_run(db, run_id)
            if outcome == "cancelled":
                run_row = db.get(EvolutionRun, run_id)
                _set_terminal(db, run_row, token, RUN_CANCELLED,
                              "cancel_requested")
                return get_run(db, run_id)
            # done → 提交轮次号并继续下一轮
            run_row = db.get(EvolutionRun, run_id)
            run_row.current_iteration = n
            _commit_guarded(db, run_row, token)
    except BudgetExceeded:
        run_row = db.get(EvolutionRun, run_id)
        _set_terminal(db, run_row, token, RUN_BUDGET_EXHAUSTED,
                      "budget_exhausted_preflight")
        return get_run(db, run_id)
    finally:
        renewer.stop()
        db.close()


def _set_terminal(db, run_row, token, status, reason) -> None:
    if not _fence(db, run_row, token):
        db.rollback()
        raise LeaseConflict("租约丢失：不能写终态")
    _stop_active_segment(run_row)
    run_row.status = status
    run_row.stop_reason = reason
    run_row.finished_at = _now()
    run_row.lease_token = None
    run_row.lease_expires_at = None
    db.commit()


def _pause(db, run_row, token, reason: str) -> None:
    if not _fence(db, run_row, token):
        db.rollback()
        raise LeaseConflict("租约丢失：不能暂停")
    _stop_active_segment(run_row)
    run_row.status = RUN_PAUSED
    run_row.stop_reason = reason
    run_row.lease_token = None
    run_row.lease_expires_at = None
    db.commit()


def _new_iteration(db, run_row, number) -> EvolutionIteration:
    exp_info = gate.get_experiment(db, run_row.experiment_id)
    current = gate.build_set_from_members(
        db, (exp_info.current_skill_set or {}).get("members") or [])
    it = EvolutionIteration(
        iteration_id="iter_" + _uuid.uuid4().hex[:20],
        run_id=run_row.run_id, number=number,
        status=ITER_RUNNING, step=ITER_STEP_TRAIN,
        freeze_set_json=json.dumps(current.to_dict(), ensure_ascii=False),
        experiment_status_rev=exp_info.status_rev,
        train_execution_ids_json="[]", no_action=False, attempts=1,
    )
    db.add(it)
    db.flush()
    return it


def _run_iteration(root, dataset, run_row, it: EvolutionIteration, token,
                   actors: Actors, db: Session,
                   guard: BudgetGuard | None = None) -> str:
    """单轮步骤机；返回 'done' | 'paused' | 'cancelled'。"""
    from app.core.skill_evolution.train_grading import (
        dump_train_task_state, execution_ids_from_state, parse_train_task_state)
    # 恢复：invalid 评估轮次（事件已落库）不再重跑，直接推进下一轮。
    if it.error_code == "EVAL_INVALID" and it.evaluation_id and it.gate_event_id:
        it.status = ITER_DONE
        it.step = ITER_STEP_DONE
        db.commit()
        return "done"
    while True:
        run_row = db.get(EvolutionRun, run_row.run_id)
        if run_row.cancel_requested:
            it.status = ITER_PAUSED
            _commit_guarded(db, run_row, token)
            return "cancelled"
        if run_row.pause_requested:
            it.status = ITER_PAUSED
            _commit_guarded(db, run_row, token)
            return "paused"
        step = it.step
        if step == ITER_STEP_TRAIN:
            cfg = json.loads(run_row.config_json)
            if cfg.get("evolve") is False:
                it.step = ITER_STEP_DONE
                it.status = ITER_DONE
                _commit_guarded(db, run_row, token)
                return "done"
            prior_records = parse_train_task_state(it.train_execution_ids_json)
            prior_ids = execution_ids_from_state(prior_records)
            counted = set(prior_ids)
            freeze_set = gate.build_set_from_members(
                db, (json.loads(it.freeze_set_json).get("members") or []))
            exp_info = gate.get_experiment(db, run_row.experiment_id)
            reviewer = _reviewer_for(actors, guard)

            def _on_train_progress(records, ids=None) -> None:
                ids = ids if ids is not None else execution_ids_from_state(records)
                fresh = [i for i in ids if i not in counted]
                if fresh and not (guard is not None and guard.external_counted):
                    run_row.used_model_calls += _count_model_calls(root, fresh)
                counted.update(ids)
                it.train_execution_ids_json = dump_train_task_state(records)
                _commit_guarded(db, run_row, token)

            try:
                train_ids = _run_train_tasks(
                    root, dataset, cfg["train_task_ids"], freeze_set,
                    actors.execution_profile,
                    executor_runner=actors.executor_runner,
                    existing_ids=prior_ids,
                    existing_state=prior_records,
                    grader_version=exp_info.grader_version,
                    reviewer=reviewer,
                    on_progress=_on_train_progress,
                    run_id=run_row.run_id, iteration=it.number)
            except TrainGradeError as exc:
                it.status = ITER_PAUSED
                it.error_code = "TRAIN_GRADE_FAILED"
                it.error_message = str(exc)[:2000]
                _pause(db, run_row, token, reason="train_grade_failed")
                return "paused"
            it.step = ITER_STEP_MAINTAIN
            _commit_guarded(db, run_row, token)
            continue
        if step == ITER_STEP_MAINTAIN:
            # C 组（experience=none）：跳过维护、不写/不读持久 Pattern。
            if json.loads(run_row.config_json).get("experience") == "none":
                it.step = ITER_STEP_PROPOSE
                _commit_guarded(db, run_row, token)
                continue
            if it.maintenance_run_id:
                it.step = ITER_STEP_PROPOSE
                _commit_guarded(db, run_row, token)
                continue
            ws = run_row.workspace_id
            domain = run_row.domain
            patterns = exp.list_patterns(db, ws, domain)
            profile = "update" if patterns else "create"
            target = patterns[0]["pattern_id"] if patterns else None
            mwrapped = False
            mrunner = actors.maintainer_runner
            if mrunner is not None:
                if guard is not None:
                    mrunner = guard.wrap(mrunner)   # 真实 maintainer：发送前预留
                mwrapped = True
            try:
                summary = maint.run_maintenance(
                    root, dataset, ws,
                    execution_ids=execution_ids_from_state(
                        parse_train_task_state(it.train_execution_ids_json)),
                    runner=mrunner,
                    profile=profile, target_pattern_id=target,
                    idempotency_extra=f"{run_row.run_id}:{it.number}:maintain")
                it.maintenance_run_id = summary.run_id
                if not mwrapped:
                    run_row.used_model_calls += summary.model_calls
                it.step = ITER_STEP_PROPOSE
                _commit_guarded(db, run_row, token)
                continue
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                if guard is not None and guard.budget_exhausted:
                    raise BudgetExceeded(
                        "预算已满（maintain 失败由预算拦截，立即终态）")
                it.attempts += 1
                if it.attempts > 2:
                    it.status = ITER_FAILED
                    it.error_code = "MAINTAIN_FAILED"
                    it.error_message = str(exc)[:2000]
                    _pause(db, run_row, token, reason="maintain_failed")
                    return "paused"
                it.error_message = f"retry maintain: {exc}"[:1000]
                _commit_guarded(db, run_row, token)
                continue
        if step == ITER_STEP_PROPOSE:
            if it.proposal_id:
                it.step = ITER_STEP_EVAL
                _commit_guarded(db, run_row, token)
                continue
            exp_info = gate.get_experiment(db, run_row.experiment_id)
            members = (exp_info.current_skill_set or {}).get("members") or []
            parent = members[0]["version_id"] if members else None
            parent_text = _parent_skill_text(root, parent) if parent else ""
            runner = (actors.proposer_factory() if actors.proposer_factory
                      else PolicyProposer(it.number, parent, parent_text))
            wrapped = False
            if guard is not None and not isinstance(runner, PolicyProposer):
                runner = guard.wrap(runner)   # 真实提议 runner：发送前预留
                wrapped = True
            try:
                summary = prop.run_proposer(
                    root, dataset, run_row.workspace_id,
                    parent_version_id=parent,
                    execution_ids=execution_ids_from_state(
                        parse_train_task_state(it.train_execution_ids_json)),
                    runner=runner,
                    idempotency_extra=f"{run_row.run_id}:{it.number}:propose",
                    budget_guard=guard,
                    max_tool_calls=(min(prop.DEFAULT_MAX_TOOL_CALLS,
                                        guard.tool_available())
                                    if guard is not None
                                    else prop.DEFAULT_MAX_TOOL_CALLS))
                if not wrapped:
                    run_row.used_model_calls += summary.model_calls
                if guard is None:
                    run_row.used_tool_calls += summary.tool_calls
                if (guard is not None and guard.budget_exhausted) or \
                        summary.status == prop.PROPOSAL_BUDGET_EXHAUSTED or \
                        summary.error_code == "BUDGET_EXHAUSTED":
                    raise BudgetExceeded("预算已满（propose 被预算拦截）")
                if summary.action == "no_action" or summary.status == "no_action":
                    it.no_action = True
                    it.proposal_run_id = summary.run_id
                    it.proposal_id = summary.proposal_id
                    it.step = ITER_STEP_DONE
                    it.status = ITER_DONE
                    _commit_guarded(db, run_row, token)
                    return "done"
                if summary.proposal_id:
                    it.proposal_run_id = summary.run_id
                    it.proposal_id = summary.proposal_id
                    it.step = ITER_STEP_EVAL
                    _commit_guarded(db, run_row, token)
                    continue
                # 其它（invalid/prereq/budget/failed）→ 有限重试，不可静默 done。
                it.attempts += 1
                if it.attempts > 2:
                    if guard is not None and guard.budget_exhausted:
                        raise BudgetExceeded("预算已满（propose 失败由预算拦截）")
                    it.status = ITER_FAILED
                    it.error_code = summary.status or "PROPOSE_FAILED"
                    it.error_message = (summary.error_message or "propose failed")[:2000]
                    _pause(db, run_row, token, reason="propose_failed")
                    return "paused"
                it.error_message = (f"retry propose({summary.status}): "
                                    f"{summary.error_message or ''}")[:1000]
                _commit_guarded(db, run_row, token)
                continue
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                if guard is not None and guard.budget_exhausted:
                    raise BudgetExceeded("预算已满（propose 异常由预算拦截）")
                it.attempts += 1
                if it.attempts > 2:
                    it.status = ITER_FAILED
                    it.error_code = "PROPOSE_FAILED"
                    it.error_message = str(exc)[:2000]
                    _pause(db, run_row, token, reason="propose_failed")
                    return "paused"
                it.error_message = f"retry propose: {exc}"[:1000]
                _commit_guarded(db, run_row, token)
                continue
        if step == ITER_STEP_EVAL:
            if it.gate_event_id:
                it.step = ITER_STEP_DONE
                it.status = ITER_DONE
                _commit_guarded(db, run_row, token)
                return "done"
            try:
                reviewer = _reviewer_for(actors, guard)
                res = gate.evaluate_and_gate(
                    root, dataset, run_row.experiment_id, it.proposal_id,
                    profile=actors.execution_profile,
                    executor_runner=actors.executor_runner, reviewer=reviewer)
                _count_review_calls(run_row, res, guard)
                _commit_guarded(db, run_row, run_row.lease_token)
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                if guard is not None and guard.budget_exhausted:
                    raise BudgetExceeded("预算已满（评估异常由预算拦截）")
                it.status = ITER_PAUSED
                it.error_code = type(exc).__name__
                it.error_message = str(exc)[:2000]
                _pause(db, run_row, token, reason="eval_conflict_or_error")
                return "paused"
            it.evaluation_id = res.get("evaluation_id")
            g = res.get("gate") or {}
            it.gate_event_id = g.get("event_id")
            if it.evaluation_id:
                db2 = skill_store.session_for(root)
                try:
                    ev_row = db2.query(__import__(
                        "app.models.evolution",
                        fromlist=["EvolutionEvaluation"]).EvolutionEvaluation
                    ).filter_by(evaluation_id=it.evaluation_id).first()
                    if ev_row is not None:
                        exec_ids = [t.get("execution_id") for t in
                                    json.loads(ev_row.per_task_results_json or "[]")]
                        if not (guard is not None and guard.external_counted):
                            run_row.used_model_calls += _count_model_calls(
                                root, exec_ids)
                finally:
                    db2.close()
            if g.get("decision") == "invalid":
                if guard is not None and guard.budget_exhausted:
                    raise BudgetExceeded(
                        "预算耗尽：EVAL_INVALID 由发送前拦截导致 → 显式预算终态")
                it.status = ITER_PAUSED
                it.error_code = "EVAL_INVALID"
                it.error_message = g.get("reason") or "evaluation invalid"
                _pause(db, run_row, token, reason="eval_invalid")
                return "paused"
            it.step = ITER_STEP_DONE
            it.status = ITER_DONE
            _commit_guarded(db, run_row, token)
            return "done"
        if step == ITER_STEP_DONE:
            it.status = ITER_DONE
            _commit_guarded(db, run_row, token)
            return "done"
        raise OrchestratorError(f"未知迭代步骤: {step}")


def _count_model_calls(root: Path, execution_ids: list[str]) -> int:
    """统计已封存执行中的模型调用次数（执行/验证预算；事件里记录了调用）。"""
    from app.core.skill_evolution.trace import iter_events
    total = 0
    for eid in execution_ids:
        run_dir = Path(root) / "runs" / eid
        if not run_dir.is_dir():
            continue
        for ev in iter_events(run_dir):
            if ev.get("kind") == "model_call":
                total += 1
    return total


def _run_train_tasks(root: Path, dataset: DatasetSpec, task_ids: list[str],
                     skill_set: FrozenSkillSet, profile: str,
                     executor_runner=None, *, existing_ids: list[str] | None = None,
                     grader_version: str | None = None, reviewer=None,
                     on_progress=None, run_id: str | None = None,
                     iteration: int | None = None, existing_state=None) -> list[str]:
    if not grader_version:
        raise TrainGradeError("训练评分缺少冻结 grader_version")
    return ensure_train_tasks_graded(
        root, dataset, task_ids, skill_set, profile,
        executor_runner=executor_runner, existing_ids=existing_ids or [],
        grader_version=grader_version, reviewer=reviewer,
        on_progress=on_progress, run_id=run_id, iteration=iteration,
        existing_state=existing_state)
