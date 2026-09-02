"""P24：图片理解改为持久化可重试任务。

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("vision_analysis_jobs"):
        return
    op.create_table(
        "vision_analysis_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("page_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("trigger", sa.String(length=32), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("idempotency_key", name="uq_vision_jobs_idempotency_key"),
    )
    op.create_index("ix_vision_analysis_jobs_page_id", "vision_analysis_jobs", ["page_id"])
    op.create_index("ix_vision_analysis_jobs_status", "vision_analysis_jobs", ["status"])
    op.create_index("ix_vision_analysis_jobs_idempotency_key", "vision_analysis_jobs", ["idempotency_key"], unique=True)
    op.create_index("ix_vision_jobs_status_created", "vision_analysis_jobs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("vision_analysis_jobs")
