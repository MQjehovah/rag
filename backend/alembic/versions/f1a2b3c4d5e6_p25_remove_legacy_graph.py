"""P25：删除旧 KO 图谱表及冲突表 KO 兼容列。

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("graph_relations"):
        op.drop_table("graph_relations")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("graph_entities"):
        op.drop_table("graph_entities")

    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("conflict_tasks"):
        columns = {column["name"] for column in inspector.get_columns("conflict_tasks")}
        remove = [name for name in ("ko_ids", "resolved_ko_id") if name in columns]
        if remove:
            with op.batch_alter_table("conflict_tasks", recreate="always") as batch:
                for name in remove:
                    batch.drop_column(name)


def downgrade() -> None:
    raise NotImplementedError("P25 不恢复旧 KO 图谱；请从 P22 数据库备份恢复")
