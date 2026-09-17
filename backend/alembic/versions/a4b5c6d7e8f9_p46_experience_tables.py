"""P46：经验 Wiki 表（阶段 3，experience_store 模式/修订/维护运行/日志/索引）。

Revision ID: b7c8d9e0f1a2
Revises: b5c6d7e8f9a0 (P45)
Create Date: 2026-09-07

新增 5 表（模型见 app/models/evolution.py，独立 metadata；演化 schema 由
create_evolution_schema / require_evolution_schema 管理，业务库需 Alembic 显式迁移）：

- evolution_patterns：Pattern 头（pattern_id、workspace_id、domain、title、status
  observed/supported/contradicted、current_revision_id）；同 scope 标题唯一；
  不把一次观察自动标记为稳定规律（默认 observed）。
- evolution_pattern_revisions：不可变修订（revision_id、pattern_id、seq、
  parent_revision_id、run_id 来源维护运行、payload_json 现象/原因假设/建议/适用/证据、
  payload_hash）；同 pattern seq 唯一。无覆盖 API：新证据 = 新修订。
- evolution_maintenance_runs：维护运行（run_id、idempotency_key 唯一、scope、
  status created/applied/failed、config_json、input_execution_ids_json、
  model_calls_json、error_code/error_message 安全诊断）。
- evolution_logs：本轮变化日志（追加写；run_id + seq 唯一）。
- evolution_indexes：当前生效经验索引（scope_key 主键；与已应用修订同一事务更新）。

经验按 workspace_id + domain 隔离；不进入业务 RAG/检索索引。
所有表无 FK 约束（与 ORM 定义一致）：作用域、引用与证据合法性由应用层在事务内
校验（experience_store），避免 SQLite 迁移顺序与部分成功耦合。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "b5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PATTERNS = "evolution_patterns"
_REVISIONS = "evolution_pattern_revisions"
_RUNS = "evolution_maintenance_runs"
_LOGS = "evolution_logs"
_INDEXES = "evolution_indexes"


def upgrade() -> None:
    op.create_table(
        _PATTERNS,
        sa.Column("pattern_id", sa.String(length=64), primary_key=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("current_revision_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("workspace_id", "domain", "title",
                            name="ux_evolution_pattern_title"),
        sa.CheckConstraint(
            "status IN ('observed', 'supported', 'contradicted')",
            name="ck_evolution_pattern_status",
        ),
    )
    op.create_index("ix_evolution_patterns_workspace_id", _PATTERNS,
                    ["workspace_id"])

    op.create_table(
        _REVISIONS,
        sa.Column("revision_id", sa.String(length=64), primary_key=True),
        sa.Column("pattern_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("pattern_id", "seq", name="ux_evolution_pattern_seq"),
        sa.CheckConstraint("seq >= 1", name="ck_evolution_pattern_seq_positive"),
        sa.CheckConstraint("length(trim(payload_json)) > 0",
                           name="ck_evolution_pattern_payload_nonempty"),
    )
    op.create_index("ix_evolution_pattern_revisions_pattern_id", _REVISIONS,
                    ["pattern_id"])

    op.create_table(
        _RUNS,
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("input_execution_ids_json", sa.Text(), nullable=False),
        sa.Column("model_calls_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="ux_evolution_run_idem"),
        sa.CheckConstraint(
            "status IN ('created', 'applied', 'failed')",
            name="ck_evolution_run_status",
        ),
    )

    op.create_table(
        _LOGS,
        sa.Column("log_id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("entry", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("run_id", "seq", name="ux_evolution_log_seq"),
    )
    op.create_index("ix_evolution_logs_run_id", _LOGS, ["run_id"])
    op.create_index("ix_evolution_logs_workspace_id", _LOGS, ["workspace_id"])

    op.create_table(
        _INDEXES,
        sa.Column("scope_key", sa.String(length=160), primary_key=True),
        sa.Column("index_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table(_INDEXES)
    op.drop_table(_LOGS)
    op.drop_table(_RUNS)
    op.drop_table(_REVISIONS)
    op.drop_table(_PATTERNS)
