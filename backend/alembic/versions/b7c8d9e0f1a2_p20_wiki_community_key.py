"""P20 Wiki 按 community_key upsert。

Revision ID: b7c8d9e0f1a2
Revises: a6c7d8e9f0b1
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a6c7d8e9f0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("wiki_pages"):
        return
    existing = {col["name"] for col in inspector.get_columns("wiki_pages")}
    if "community_key" not in existing:
        with op.batch_alter_table("wiki_pages") as batch:
            batch.add_column(sa.Column("community_key", sa.String(length=64), nullable=True))
    indexes = {idx["name"] for idx in inspector.get_indexes("wiki_pages")}
    if "ix_wiki_pages_community_key" not in indexes:
        op.create_index("ix_wiki_pages_community_key", "wiki_pages", ["community_key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("wiki_pages"):
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("wiki_pages")}
    if "ix_wiki_pages_community_key" in indexes:
        op.drop_index("ix_wiki_pages_community_key", table_name="wiki_pages")
    existing = {col["name"] for col in inspector.get_columns("wiki_pages")}
    if "community_key" in existing:
        with op.batch_alter_table("wiki_pages") as batch:
            batch.drop_column("community_key")
