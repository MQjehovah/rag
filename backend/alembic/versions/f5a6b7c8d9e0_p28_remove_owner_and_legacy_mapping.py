"""P28：删除 owner 字段、no_owner 债务与完成使命的旧 KO 映射表。

- knowledge_cards.owner / knowledge_debts.owner：已无业务意义，删除。
- knowledge_debts 中 debt_type='no_owner'：不再是业务问题，清除。
- legacy_ko_mappings：P22 迁移已完成，删除（保留历史迁移文件与审计）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from migration_compat import drop_columns_compat

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # no_owner 已不再是业务问题，不能继续出现在待处理/已处理页面
    if inspector.has_table("knowledge_debts"):
        bind.execute(sa.text(
            "DELETE FROM knowledge_debts WHERE debt_type='no_owner'"
        ))
        columns = {
            column["name"]
            for column in inspector.get_columns("knowledge_debts")
        }
        if "owner" in columns:
            drop_columns_compat(op, "knowledge_debts", ["owner"])

    inspector = sa.inspect(bind)
    if inspector.has_table("knowledge_cards"):
        columns = {
            column["name"]
            for column in inspector.get_columns("knowledge_cards")
        }
        if "owner" in columns:
            drop_columns_compat(op, "knowledge_cards", ["owner"])

    # 旧 KO 主表已经在 P22 删除，映射完成后可一并清理
    inspector = sa.inspect(bind)
    if inspector.has_table("legacy_ko_mappings"):
        op.drop_table("legacy_ko_mappings")


def downgrade() -> None:
    # 仅恢复 owner 列（no_owner 数据与旧映射表无法可靠恢复）
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("knowledge_cards"):
        columns = {
            column["name"]
            for column in inspector.get_columns("knowledge_cards")
        }
        if "owner" not in columns:
            with op.batch_alter_table(
                "knowledge_cards",
                recreate="always",
            ) as batch:
                batch.add_column(sa.Column("owner", sa.String(255), nullable=True))

    inspector = sa.inspect(bind)
    if inspector.has_table("knowledge_debts"):
        columns = {
            column["name"]
            for column in inspector.get_columns("knowledge_debts")
        }
        if "owner" not in columns:
            with op.batch_alter_table(
                "knowledge_debts",
                recreate="always",
            ) as batch:
                batch.add_column(sa.Column("owner", sa.String(255), nullable=True))
