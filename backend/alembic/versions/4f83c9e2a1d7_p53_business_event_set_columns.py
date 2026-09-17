"""P53: evolution_business_events 增加完整集合审计列（B2 写路径 / 后续回退材料）。

- from_members_json / to_members_json: 完整业务集合成员（规范顺序，含 seq），
  绝不只留首版本；单成员集合由应用层同时镜像 from_version_id/to_version_id；
- from_set_hash / to_set_hash: 各自规范集合哈希（business-binding-set/v1）；
- from_rev / to_rev: 切换前后绑定修订号（与 bindings.rev 一致，回退可复核）。
全列 nullable，既有 P51 事件行不受影响（旧语义列仍可读）。

Revision ID: 4f83c9e2a1d7
Revises: 653bbcf9847b (P52)
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "4f83c9e2a1d7"
down_revision: Union[str, Sequence[str], None] = "653bbcf9847b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EVENT_TABLE = "evolution_business_events"


def upgrade() -> None:
    op.add_column(_EVENT_TABLE,
                  sa.Column("from_members_json", sa.Text(), nullable=True))
    op.add_column(_EVENT_TABLE,
                  sa.Column("to_members_json", sa.Text(), nullable=True))
    op.add_column(_EVENT_TABLE,
                  sa.Column("from_set_hash", sa.String(length=64),
                            nullable=True))
    op.add_column(_EVENT_TABLE,
                  sa.Column("to_set_hash", sa.String(length=64),
                            nullable=True))
    op.add_column(_EVENT_TABLE,
                  sa.Column("from_rev", sa.Integer(), nullable=True))
    op.add_column(_EVENT_TABLE,
                  sa.Column("to_rev", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_EVENT_TABLE, "to_rev")
    op.drop_column(_EVENT_TABLE, "from_rev")
    op.drop_column(_EVENT_TABLE, "to_set_hash")
    op.drop_column(_EVENT_TABLE, "from_set_hash")
    op.drop_column(_EVENT_TABLE, "to_members_json")
    op.drop_column(_EVENT_TABLE, "from_members_json")
