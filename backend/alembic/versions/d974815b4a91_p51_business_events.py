"""P51: 业务晋升/回退审计事件表（阶段 8D）。

Revision ID: d974815b4a91
Revises: 7a7a0000c0de (P50)
"""
from __future__ import annotations

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "d974815b4a91"
down_revision: Union[str, Sequence[str], None] = "7a7a0000c0de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evolution_business_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("from_version_id", sa.String(length=64), nullable=True),
        sa.Column("to_version_id", sa.String(length=64), nullable=True),
        sa.Column("experiment_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("idempotency_key",
                            name="ux_evolution_business_event_idem"),
        sa.CheckConstraint("action IN ('promote', 'rollback')",
                           name="ck_evolution_business_action"),
    )


def downgrade() -> None:
    op.drop_table("evolution_business_events")
