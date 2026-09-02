"""P14 rollout event audit table.

Revision ID: f0a1b2c3d4e5
Revises: a5b6c7d8e9f0
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 本项目历史上 startup 会调用 create_all；迁移必须兼容表已被模型提前创建的环境。
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("p14_rollout_events"):
        op.create_table(
            "p14_rollout_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("previous_flags_json", sa.Text(), nullable=False),
            sa.Column("applied_flags_json", sa.Text(), nullable=False),
            sa.Column("readiness_json", sa.Text(), nullable=True),
            sa.Column("affected_runs_json", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_p14_rollout_events_action", "p14_rollout_events", ["action"])
        op.create_index("ix_p14_rollout_events_created_at", "p14_rollout_events", ["created_at"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("p14_rollout_events"):
        op.drop_table("p14_rollout_events")
