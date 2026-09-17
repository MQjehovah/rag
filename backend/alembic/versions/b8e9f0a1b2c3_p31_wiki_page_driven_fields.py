"""P31：Wiki 页面驱动构建所需的字段（V4 Phase C）。

Wiki 从 Card/Community 派生改为 Page 驱动正式知识层，需要：
- wiki_pages.source_page_ids：直接来源 Page ID（JSON 数组），用于 dirty 检测与增量刷新。
- wiki_pages.dirty：来源有更新待刷新标记。
- wiki_pages.category：分类。
- wiki_revisions.edit_type：auto=自动构建，manual=人工编辑。
- wiki_revisions.updated_by：编辑者。
- pages.wiki_dirty：Page 级 Wiki 待处理状态（与 index_dirty 独立）。
- pages.wiki_compiled_content_hash：上次成功构建的输入内容哈希。
- pages.wiki_last_error：上次失败的可用化错误。

重要说明（迁移托管）：
- 这些字段**只允许 Alembic 显式升级**，init_db 的 _migrate_schema 不会自动
  添加迁移托管字段（见 app/models/database.py 的 MANAGED_MIGRATION_COLUMNS）。
- 未执行本迁移的数据库在 init_db 时会抛 SchemaNotReadyError（schema_not_ready）。
- 本轮不得执行真实库 P30/P31；测试库由 Base.metadata.create_all() 创建完整结构。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "a7c3f9e2b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column(table: str, column_name: str, column_type, inspector) -> None:
    columns = {col["name"] for col in inspector.get_columns(table)}
    if column_name not in columns:
        op.add_column(table, sa.Column(column_name, column_type, nullable=True))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    _add_column("wiki_pages", "source_page_ids", sa.Text(), inspector)
    _add_column("wiki_pages", "dirty", sa.Boolean(), inspector)
    _add_column("wiki_pages", "category", sa.String(128), inspector)
    _add_column("wiki_revisions", "edit_type", sa.String(32), inspector)
    _add_column("wiki_revisions", "updated_by", sa.String(255), inspector)
    _add_column("pages", "wiki_dirty", sa.Boolean(), inspector)
    _add_column("pages", "wiki_compiled_content_hash", sa.String(64), inspector)
    _add_column("pages", "wiki_last_error", sa.Text(), inspector)

    # 显式回填（V4 Phase C 最后一次补漏第 7 点）：
    # 现有 Page 必须进入 Page 驱动 Wiki 初始构建范围。
    # TRUE/FALSE 同时兼容 SQLite 与 PostgreSQL；整数 1/0 在 PG boolean 列会
    # DatatypeMismatch（SQLite 把 Boolean 存成 INTEGER，因此此前未暴露）。
    op.execute("UPDATE pages SET wiki_dirty = TRUE WHERE wiki_dirty IS NULL")
    # 旧 Wiki 默认不脏（等来源更新再触发）。
    op.execute("UPDATE wiki_pages SET dirty = FALSE WHERE dirty IS NULL")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, column_name in (
        ("pages", "wiki_last_error"),
        ("pages", "wiki_compiled_content_hash"),
        ("pages", "wiki_dirty"),
        ("wiki_revisions", "updated_by"),
        ("wiki_revisions", "edit_type"),
        ("wiki_pages", "category"),
        ("wiki_pages", "dirty"),
        ("wiki_pages", "source_page_ids"),
    ):
        columns = {col["name"] for col in inspector.get_columns(table)}
        if column_name in columns:
            op.drop_column(table, column_name)
