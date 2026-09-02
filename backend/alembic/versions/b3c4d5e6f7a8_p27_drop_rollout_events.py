"""P27：最终切换完成后移除空的 P14 回滚事件表。

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("p14_rollout_events"):
        op.drop_table("p14_rollout_events")


def downgrade() -> None:
    raise NotImplementedError("最终切换完成后不恢复旧回滚入口")
