"""P26：查询日志与 Feature Flag 移除旧入口兼容字段。

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("query_logs"):
        columns = {column["name"] for column in inspector.get_columns("query_logs")}
        if "ko_citations" in columns:
            if "card_citations" in columns:
                bind.execute(sa.text(
                    "UPDATE query_logs SET card_citations=ko_citations "
                    "WHERE card_citations IS NULL AND ko_citations IS NOT NULL"
                ))
            with op.batch_alter_table("query_logs", recreate="always") as batch:
                batch.drop_column("ko_citations")
    if inspector.has_table("runtime_feature_flags"):
        bind.execute(sa.text(
            "DELETE FROM runtime_feature_flags WHERE name='legacy_search_visible'"
        ))


def downgrade() -> None:
    raise NotImplementedError("P26 不恢复已下线的旧入口字段")
