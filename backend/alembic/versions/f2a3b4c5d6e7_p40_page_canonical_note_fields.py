"""P40：Page 新增 CanonicalNote 契约字段（Phase 2）。

Revision ID: f2a3b4c5d6e7
Revises: e6f7a8b9c0d1 (P38)
Create Date: Phase 2

新增 7 个 nullable 字段到 pages 表，记录 CanonicalNote 转换契约信息：
- note_schema_version     canonical-note/v1 / legacy-note/v0
- content_format          markdown
- content_kind            markdown/text/code/csv/office/pdf
- converter_key
- converter_version
- conversion_status       converted/partial/failed/blocked
- conversion_warnings_json JSON 数组

历史 Page 不强制回填（读取时缺失视为 legacy-note/v0）。

SQLite 使用 batch_alter_table（本迁移同时兼容 PostgreSQL）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from migration_compat import ensure_columns_sqlite


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    new_columns = [
        sa.Column("note_schema_version", sa.String(64), nullable=True),
        sa.Column("content_format", sa.String(64), nullable=True),
        sa.Column("content_kind", sa.String(64), nullable=True),
        sa.Column("converter_key", sa.String(127), nullable=True),
        sa.Column("converter_version", sa.String(127), nullable=True),
        sa.Column("conversion_status", sa.String(32), nullable=True),
        sa.Column("conversion_warnings_json", sa.Text(), nullable=True),
    ]

    if dialect == "sqlite":
        # 兼容：若部分列已物理存在且等价 → 保留并只补缺失列；不等价 → 受控失败。
        ensure_columns_sqlite(op, bind, "pages", new_columns)
    else:
        # PostgreSQL：直接 ALTER TABLE ADD COLUMN（nullable 无默认值，安全）。
        for col in new_columns:
            op.add_column("pages", col)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    col_names = [
        "note_schema_version", "content_format", "content_kind",
        "converter_key", "converter_version", "conversion_status",
        "conversion_warnings_json",
    ]

    if dialect == "sqlite":
        with op.batch_alter_table("pages") as batch_op:
            for name in col_names:
                batch_op.drop_column(name)
    else:
        for name in col_names:
            op.drop_column("pages", name)
