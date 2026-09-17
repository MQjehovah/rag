"""P54: evolution_runs 累计 active 时间（整次 run 预算，跨暂停/崩溃/恢复）。

- used_active_seconds: 已消耗的 active 执行秒数（暂停等待不计入）；
- active_segment_started_at: 当前 active 段起点；无段则为 NULL。
SQLite 与 PostgreSQL 语义兼容（Float / DateTime）。不修改 P25–P53。

Revision ID: 5a94d0e3b2c8
Revises: 4f83c9e2a1d7 (P53)
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "5a94d0e3b2c8"
down_revision: Union[str, Sequence[str], None] = "4f83c9e2a1d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evolution_runs",
        sa.Column("used_active_seconds", sa.Float(), nullable=False,
                  server_default="0"))
    op.add_column(
        "evolution_runs",
        sa.Column("active_segment_started_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("evolution_runs", "active_segment_started_at")
    op.drop_column("evolution_runs", "used_active_seconds")
