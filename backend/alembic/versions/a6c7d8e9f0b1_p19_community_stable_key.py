"""P19 Community 稳定标识字段与重建任务表。

Revision ID: a6c7d8e9f0b1
Revises: f0a1b2c3d4e5
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6c7d8e9f0b1"
down_revision: Union[str, Sequence[str], None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return
    existing = {col["name"] for col in inspector.get_columns(table)}
    if column.name not in existing:
        with op.batch_alter_table(table) as batch:
            batch.add_column(column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _add_column_if_missing("knowledge_communities", sa.Column("community_key", sa.String(length=64), nullable=True))
    _add_column_if_missing("knowledge_communities", sa.Column("source_hash", sa.String(length=64), nullable=True))
    _add_column_if_missing("knowledge_communities", sa.Column("algorithm", sa.String(length=32), nullable=True))
    _add_column_if_missing("knowledge_communities", sa.Column("algorithm_version", sa.String(length=32), nullable=True))
    _add_column_if_missing("knowledge_communities", sa.Column("build_version", sa.String(length=32), nullable=True))
    _add_column_if_missing("knowledge_communities", sa.Column("dirty", sa.Boolean(), nullable=True))
    _add_column_if_missing("knowledge_communities", sa.Column("status", sa.String(length=32), nullable=True))

    inspector = sa.inspect(bind)
    if inspector.has_table("knowledge_communities"):
        indexes = {idx["name"] for idx in inspector.get_indexes("knowledge_communities")}
        if "ix_knowledge_communities_community_key" not in indexes:
            op.create_index(
                "ix_knowledge_communities_community_key",
                "knowledge_communities",
                ["community_key"],
                unique=True,
            )

    if not inspector.has_table("community_rebuild_jobs"):
        op.create_table(
            "community_rebuild_jobs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=True),
            sa.Column("trigger", sa.String(length=32), nullable=True),
            sa.Column("card_id", sa.String(length=36), nullable=True),
            sa.Column("acl_scopes_json", sa.Text(), nullable=True),
            sa.Column("idempotency_key", sa.String(length=128), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_community_rebuild_jobs_status", "community_rebuild_jobs", ["status"])
        op.create_index("ix_community_rebuild_jobs_card_id", "community_rebuild_jobs", ["card_id"])
        op.create_index("ix_community_rebuild_jobs_idempotency_key", "community_rebuild_jobs", ["idempotency_key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("community_rebuild_jobs"):
        op.drop_table("community_rebuild_jobs")
