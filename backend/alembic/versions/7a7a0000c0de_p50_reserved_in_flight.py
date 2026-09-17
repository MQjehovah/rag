"""P50: evolution_runs 增加在途预留调用列（阶段 7A 预算前置）。

Revision ID: 7a7a0000c0de
Revises: e7f8a9b0c1d2 (P49)
"""
from __future__ import annotations

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "7a7a0000c0de"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evolution_runs",
                  sa.Column("reserved_in_flight_json", sa.Text(),
                            nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("evolution_runs", "reserved_in_flight_json")
