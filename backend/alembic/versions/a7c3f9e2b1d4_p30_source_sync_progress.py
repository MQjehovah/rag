"""P30：source_sync_runs 增加同步阶段/进度/降级字段。

统一数据源 Worker 需要持久化：
- stage：当前阶段（remote/refresh/discover/persist/index/compile）
- progress：人类可读进度描述
- llm_degraded / degraded_reason：GLM/LLM 不可用时 Card 编译降级标记

幂等：仅当列不存在时添加（运行时 init_db 的 _migrate_schema 也会自动补列，
本迁移作为正式 schema 记录，两者互不冲突）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a7c3f9e2b1d4"
down_revision: Union[str, Sequence[str], None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column(column_name: str, column_type, inspector) -> None:
    columns = {col["name"] for col in inspector.get_columns("source_sync_runs")}
    if column_name not in columns:
        op.add_column("source_sync_runs", sa.Column(column_name, column_type, nullable=True))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    _add_column("stage", sa.String(32), inspector)
    _add_column("progress", sa.Text(), inspector)
    _add_column("llm_degraded", sa.Boolean(), inspector)
    _add_column("degraded_reason", sa.Text(), inspector)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns("source_sync_runs")}
    for column_name in ("degraded_reason", "llm_degraded", "progress", "stage"):
        if column_name in columns:
            op.drop_column("source_sync_runs", column_name)
