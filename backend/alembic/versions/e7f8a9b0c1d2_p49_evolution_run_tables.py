"""P49：多轮进化运行与轮次（阶段 6）。

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1 (P48)
Create Date: 2026-09-07

新增 2 表（模型见 app/models/evolution.py，独立 evolution metadata）：

- evolution_runs：调度实体
  run_id、experiment_id、workspace/domain、dataset_version、
  init_mode(paper/business)、config_json（train/val 清单、预算、模型配置快照）、
  initial_skill_set_json、initial_experience_snapshot_json、
  max_iterations/current_iteration、status
  (queued/running/paused/completed/failed/cancelled/budget_exhausted)、
  stop_reason(max_iterations/perfect_score 等)、用量计数（调用/工具/估算字符）、
  lease_owner/lease_token/lease_expires_at（单 worker 领取与租约）、
  pause_requested/cancel_requested、错误诊断、时间戳。

- evolution_iterations：轮次
  iteration_id、run_id、number（唯一）、status(running/paused/done/failed)、
  step(train/maintain/propose/eval/gate/done，检查点指针)、
  freeze_set_json（本轮固定技能集合）、experiment_status_rev、
  train_execution_ids_json、maintenance_run_id、proposal_run_id、
  proposal_id、evaluation_id、gate_event_id、no_action、attempts、
  错误诊断、时间戳。

子模块（经验/维护/提议/评估/门控）记录不复制：仅以关联 id 引用，各自幂等键保证恢复。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RUNS = "evolution_runs"
_ITERS = "evolution_iterations"


def upgrade() -> None:
    op.create_table(
        _RUNS,
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("dataset_version", sa.String(length=64), nullable=False),
        sa.Column("init_mode", sa.String(length=16), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("initial_skill_set_json", sa.Text(), nullable=False),
        sa.Column("initial_experience_snapshot_json", sa.Text(), nullable=True),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("current_iteration", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("used_model_calls", sa.Integer(), nullable=False),
        sa.Column("used_tool_calls", sa.Integer(), nullable=False),
        sa.Column("used_estimated_chars", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("pause_requested", sa.Boolean(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','paused','completed','failed',"
            "'cancelled','budget_exhausted')",
            name="ck_evolution_run_status"),
        sa.CheckConstraint("init_mode IN ('paper','business')",
                           name="ck_evolution_run_init_mode"),
        sa.CheckConstraint("max_iterations >= 1", name="ck_evolution_run_max_iter"),
    )
    op.create_index("ix_evolution_runs_experiment_id", _RUNS, ["experiment_id"])
    op.create_index("ix_evolution_runs_workspace_id", _RUNS, ["workspace_id"])

    op.create_table(
        _ITERS,
        sa.Column("iteration_id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("step", sa.String(length=16), nullable=False),
        sa.Column("freeze_set_json", sa.Text(), nullable=False),
        sa.Column("experiment_status_rev", sa.Integer(), nullable=False),
        sa.Column("train_execution_ids_json", sa.Text(), nullable=False),
        sa.Column("maintenance_run_id", sa.String(length=64), nullable=True),
        sa.Column("proposal_run_id", sa.String(length=64), nullable=True),
        sa.Column("proposal_id", sa.String(length=64), nullable=True),
        sa.Column("evaluation_id", sa.String(length=64), nullable=True),
        sa.Column("gate_event_id", sa.String(length=64), nullable=True),
        sa.Column("no_action", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("run_id", "number", name="ux_evolution_iter_number"),
        sa.CheckConstraint("status IN ('running','paused','done','failed')",
                           name="ck_evolution_iter_status"),
        sa.CheckConstraint(
            "step IN ('train','maintain','propose','eval','gate','done')",
            name="ck_evolution_iter_step"),
        sa.CheckConstraint("number >= 1", name="ck_evolution_iter_number_pos"),
    )
    op.create_index("ix_evolution_iterations_run_id", _ITERS, ["run_id"])


def downgrade() -> None:
    op.drop_table(_ITERS)
    op.drop_table(_RUNS)
