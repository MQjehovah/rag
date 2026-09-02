"""P23：已发布 Card 的待审 Revision 使用独立指针。

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("knowledge_cards")}
    if "draft_revision_id" not in columns:
        with op.batch_alter_table("knowledge_cards") as batch:
            batch.add_column(sa.Column("draft_revision_id", sa.String(length=36), nullable=True))
            batch.create_index("ix_knowledge_cards_draft_revision_id", ["draft_revision_id"])


def downgrade() -> None:
    with op.batch_alter_table("knowledge_cards") as batch:
        batch.drop_index("ix_knowledge_cards_draft_revision_id")
        batch.drop_column("draft_revision_id")
