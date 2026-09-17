"""P48：实验状态、评估记录与门控事件（阶段 5）。

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0 (P47)
Create Date: 2026-09-07

新增 3 表（模型见 app/models/evolution.py，独立 evolution metadata）：

- evolution_experiments：最小实验状态
  experiment_id、workspace/domain、dataset_version、grader_version、
  runner_config_json、pipeline_key/pipeline_version、runtime_ref、
  initial/current/best 技能集合 JSON、best_score（整数通过数/总数，避免浮点）、
  baseline_evaluation_id / best_evaluation_id、status_rev（并发乐观比较）、
  status(active/closed)。experiment_id 区分同 Workspace 不同实验；实验各自维护
  活动集合，互不覆盖；业务绑定不受实验影响。

- evolution_evaluations：一次评估（baseline/candidate）
  evaluation_id、idempotency_key（唯一，重复评估幂等）、experiment_id、kind、
  proposal_id、base/candidate 集合快照、task_ids_json（有序验证任务）、
  per_task_results_json（execution_id+结果+评分）、config_json、
  main_passed/main_total（整数主分数）、valid+invalid_reason
  （基础设施/存储/配置问题 → invalid，不参与晋升）、usage_json。
  评估执行不持有长事务：先隔离执行，再单事务写入本行。

- evolution_gate_events：门控事件
  event_id、experiment_id、candidate_evaluation_id/baseline_evaluation_id/
  best_evaluation_id、decision(accepted/rejected/invalid)、reason、
  candidate_version_ids_json、candidate/best 分数、previous/next 集合、
  status_rev_before/after。唯一 (experiment_id, candidate_evaluation_id) 保证
  重复门控请求幂等（不重复晋升）；接受事件与实验指针更新在同一事务。

无 FK（与 ORM 一致）：一致性由 gating 服务单事务保证。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXP = "evolution_experiments"
_EVAL = "evolution_evaluations"
_GATE = "evolution_gate_events"


def upgrade() -> None:
    op.create_table(
        _EXP,
        sa.Column("experiment_id", sa.String(length=64), primary_key=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("dataset_version", sa.String(length=64), nullable=False),
        sa.Column("grader_version", sa.String(length=64), nullable=False),
        sa.Column("runner_config_json", sa.Text(), nullable=False),
        sa.Column("pipeline_key", sa.String(length=64), nullable=False),
        sa.Column("pipeline_version", sa.String(length=64), nullable=False),
        sa.Column("runtime_ref", sa.String(length=64), nullable=False),
        sa.Column("initial_skill_set_json", sa.Text(), nullable=False),
        sa.Column("current_skill_set_json", sa.Text(), nullable=False),
        sa.Column("best_skill_set_json", sa.Text(), nullable=True),
        sa.Column("best_score_passed", sa.Integer(), nullable=True),
        sa.Column("best_score_total", sa.Integer(), nullable=True),
        sa.Column("best_evaluation_id", sa.String(length=64), nullable=True),
        sa.Column("baseline_evaluation_id", sa.String(length=64), nullable=True),
        sa.Column("status_rev", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('active', 'closed')",
                           name="ck_evolution_experiment_status"),
        sa.CheckConstraint("status_rev >= 1", name="ck_evolution_experiment_rev"),
    )
    op.create_index("ix_evolution_experiments_workspace_id", _EXP, ["workspace_id"])

    op.create_table(
        _EVAL,
        sa.Column("evaluation_id", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=True),
        sa.Column("base_set_json", sa.Text(), nullable=False),
        sa.Column("candidate_set_json", sa.Text(), nullable=False),
        sa.Column("task_ids_json", sa.Text(), nullable=False),
        sa.Column("per_task_results_json", sa.Text(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("main_passed", sa.Integer(), nullable=False),
        sa.Column("main_total", sa.Integer(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("invalid_reason", sa.Text(), nullable=True),
        sa.Column("usage_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="ux_evolution_eval_idem"),
        sa.CheckConstraint("kind IN ('baseline', 'candidate')",
                           name="ck_evolution_evaluation_kind"),
        sa.CheckConstraint("main_total >= 0 AND main_passed >= 0 "
                           "AND main_passed <= main_total",
                           name="ck_evolution_evaluation_score"),
    )
    op.create_index("ix_evolution_evaluations_experiment_id", _EVAL,
                    ["experiment_id"])

    op.create_table(
        _GATE,
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("experiment_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_evaluation_id", sa.String(length=64), nullable=False),
        sa.Column("baseline_evaluation_id", sa.String(length=64), nullable=True),
        sa.Column("best_evaluation_id", sa.String(length=64), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("candidate_version_ids_json", sa.Text(), nullable=False),
        sa.Column("candidate_score", sa.Text(), nullable=True),
        sa.Column("best_score", sa.Text(), nullable=True),
        sa.Column("previous_set_json", sa.Text(), nullable=False),
        sa.Column("next_set_json", sa.Text(), nullable=True),
        sa.Column("status_rev_before", sa.Integer(), nullable=False),
        sa.Column("status_rev_after", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("experiment_id", "candidate_evaluation_id",
                            name="ux_evolution_gate_once"),
        sa.CheckConstraint("decision IN ('accepted', 'rejected', 'invalid')",
                           name="ck_evolution_gate_decision"),
    )
    op.create_index("ix_evolution_gate_events_experiment_id", _GATE,
                    ["experiment_id"])


def downgrade() -> None:
    op.drop_table(_GATE)
    op.drop_table(_EVAL)
    op.drop_table(_EXP)
