"""P47：技能提议者持久化（阶段 4，proposal run / proposal）。

Revision ID: c5d6e7f8a9b0
Revises: a4b5c6d7e8f9 (P46)
Create Date: 2026-09-07

新增 2 表（模型见 app/models/evolution.py，独立 evolution metadata）：

- evolution_proposal_runs：一次提议运行
  run_id、kind='proposer'、idempotency_key（唯一）、workspace/domain/dataset_version、
  status（generating/candidate_saved/no_action/prerequisites_insufficient/
  output_invalid/failed/budget_exhausted）、scope_snapshot_json（固定基础经验与技能
  快照）、authorized_execution_ids_json、proposer_config_json、model_calls_json
  （模型与工具调用记录；usage=null 不伪造成本）、read_execution_ids_json（累计去重）、
  tool_events_json（读取审计：execution_id/返回长度/截断）、proposal_id、错误诊断、
  时间戳。budget_exhausted 独立状态，不等同 no_action 或效果拒绝。

- evolution_proposals：最终提案（create/patch/no_action）
  proposal_id、run_id、scope、action、skill_id、parent_version_id /
  parent_content_hash、patch_json（受控补丁操作）、reason、pattern_ids_json /
  pattern_revision_ids_json（Pattern 引用）、evidence_execution_ids_json、
  candidate_version_id / candidate_content_hash、duplicate_of_version_id
  （同内容历史候选识别）、status。
  候选从未被标记 accepted/rejected：门控结论属于阶段 5，本阶段只有
  candidate_saved / no_action / output_invalid / prerequisites_insufficient /
  failed / budget_exhausted。

无 FK（与 ORM 一致）：一致性由 proposer 服务在单事务内保证（版本、提案、运行行原子
保存，避免孤儿候选）。SQLite/PG 兼容。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RUNS = "evolution_proposal_runs"
_PROPOSALS = "evolution_proposals"


def upgrade() -> None:
    op.create_table(
        _RUNS,
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("dataset_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scope_snapshot_json", sa.Text(), nullable=True),
        sa.Column("authorized_execution_ids_json", sa.Text(), nullable=False),
        sa.Column("proposer_config_json", sa.Text(), nullable=False),
        sa.Column("model_calls_json", sa.Text(), nullable=True),
        sa.Column("read_execution_ids_json", sa.Text(), nullable=False),
        sa.Column("tool_events_json", sa.Text(), nullable=True),
        sa.Column("proposal_id", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="ux_evolution_proposal_run_idem"),
        sa.CheckConstraint(
            "status IN ('generating', 'candidate_saved', 'no_action', "
            "'prerequisites_insufficient', 'output_invalid', 'failed', "
            "'budget_exhausted')",
            name="ck_evolution_proposal_run_status",
        ),
    )
    op.create_index("ix_evolution_proposal_runs_idempotency_key", _RUNS,
                    ["idempotency_key"])

    op.create_table(
        _PROPOSALS,
        sa.Column("proposal_id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("dataset_version", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("skill_id", sa.String(length=64), nullable=False),
        sa.Column("parent_version_id", sa.String(length=64), nullable=True),
        sa.Column("parent_content_hash", sa.String(length=64), nullable=True),
        sa.Column("patch_json", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("pattern_ids_json", sa.Text(), nullable=False),
        sa.Column("pattern_revision_ids_json", sa.Text(), nullable=False),
        sa.Column("evidence_execution_ids_json", sa.Text(), nullable=False),
        sa.Column("candidate_version_id", sa.String(length=64), nullable=True),
        sa.Column("candidate_content_hash", sa.String(length=64), nullable=True),
        sa.Column("duplicate_of_version_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("action IN ('create', 'patch', 'no_action')",
                           name="ck_evolution_proposal_action"),
        sa.CheckConstraint(
            "status IN ('candidate_saved', 'no_action', "
            "'prerequisites_insufficient', 'output_invalid', 'failed', "
            "'budget_exhausted')",
            name="ck_evolution_proposal_status",
        ),
    )
    op.create_index("ix_evolution_proposals_run_id", _PROPOSALS, ["run_id"])


def downgrade() -> None:
    op.drop_table(_PROPOSALS)
    op.drop_table(_RUNS)
