"""Phase 4：KnowledgeCompile Run/Stage 集中状态机。

所有状态写入都必须经过 apply_run_status / apply_stage_status（返回非空字符串 =
非法转换，调用方不得执行写入）。禁止业务函数自行拼接状态字符串直接赋值。

Run 状态：queued/running/succeeded/failed/cancelled/superseded
允许转换：
- queued → running（claim）｜cancelled（排队直接取消）｜superseded（新 run 抢占）
- running → succeeded/failed/queued（heartbeat 超时恢复）/cancelled/superseded
- failed → queued（retry，attempt+1）｜superseded（新 run 抢占）
非法（终态不可迁移）：succeeded/cancelled/superseded → 任何

Stage 状态：queued/running/succeeded/failed/skipped/cancelled
允许转换：
- queued → running/skipped（上游失败级联）/cancelled（run 级 cancel 级联）
- running → succeeded/failed/queued（超时）/skipped/cancelled
- failed → queued（retry：新行 attempt+1，旧行保留）
- 终态：succeeded/skipped/cancelled → 任何（非法）
"""
from __future__ import annotations

RUN_STATUSES = (
    "queued", "running", "succeeded", "failed", "cancelled", "superseded",
)
STAGE_STATUSES = (
    "queued", "running", "succeeded", "failed", "skipped", "cancelled",
)

RUN_TERMINAL = ("succeeded", "cancelled", "superseded")
STAGE_TERMINAL = ("succeeded", "skipped", "cancelled")

_TRIGGER_TYPES = (
    "page_changed", "page_deleted", "manual_rebuild",
    "skill_migration", "manual_edit",
    "batch_rebuild",
)

# 转换表：current → {allowed new}。中心唯一事实来源。
_RUN_TABLE: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "superseded"}),
    "running": frozenset({"succeeded", "failed", "queued", "cancelled", "superseded"}),
    "failed": frozenset({"queued", "superseded"}),
    "succeeded": frozenset(),
    "cancelled": frozenset(),
    "superseded": frozenset(),
}

_STAGE_TABLE: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "skipped", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "queued", "skipped", "cancelled"}),
    "failed": frozenset({"queued"}),
    "succeeded": frozenset(),
    "skipped": frozenset(),
    "cancelled": frozenset(),
}


def validate_run_transition(current: str, new: str) -> str:
    """校验 Run 状态转换；返回空串 = 允许，非空 = 拒绝原因。"""
    if new not in RUN_STATUSES:
        return f"unknown_status={new}"
    if current not in RUN_STATUSES:
        return f"unknown_current_status={current}"
    if new in _RUN_TABLE.get(current, frozenset()):
        return ""
    return f"illegal_run_transition={current}->{new}"


def validate_stage_transition(current: str, new: str) -> str:
    """校验 Stage 状态转换；返回空串 = 允许，非空 = 拒绝原因。"""
    if new not in STAGE_STATUSES:
        return f"unknown_status={new}"
    if current not in STAGE_STATUSES:
        return f"unknown_current_status={current}"
    if new in _STAGE_TABLE.get(current, frozenset()):
        return ""
    return f"illegal_stage_transition={current}->{new}"


def validate_trigger_type(trigger_type: str) -> str:
    """校验 run trigger_type；空串 = 合法。"""
    if trigger_type not in _TRIGGER_TYPES:
        return f"invalid_trigger_type={trigger_type}"
    return ""


def is_run_terminal(status: str) -> bool:
    return status in RUN_TERMINAL


def is_stage_terminal(status: str) -> bool:
    return status in STAGE_TERMINAL


def apply_run_status(run, new_status: str) -> str:
    """集中写 Run.status；非法转换不改写并返回原因。"""
    reason = validate_run_transition(run.status, new_status)
    if reason:
        return reason
    run.status = new_status
    return ""


def apply_stage_status(row, new_status: str) -> str:
    """集中写 StageRun.status；非法转换不改写并返回原因。"""
    reason = validate_stage_transition(row.status, new_status)
    if reason:
        return reason
    row.status = new_status
    return ""
