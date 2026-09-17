"""P34：版本感知 Wiki（V4 Phase I）。

Revision ID: e5f6a7b8c9d0
Revises: d1e2f3a4b5c6 (P33 drop Card/KO)
Create Date: Phase I

新增/改动：
- wiki_pages.latest_version：确定依据的当前最新版本（可空，未标明时为空）。
- wiki_sections 增加版本感知字段：version_label / version_sort_key / is_common /
  content_origin / merge_policy / version_confidence / version_status / diff_notice。
- 新增 wiki_version_sources 关联表：后台 version → Page/Chunk 的来源映射
  （前端与普通 Wiki API 不展示）。

回填策略（V4 Phase I 四，不猜测版本）：
- 既有 summary Section → is_common=True，version_label='common'，content_origin 保持
  原样（历史 summary 不区分 auto/manual，统一 auto）。
- 既有 facts/body Section → version_label='unversioned'，version_status='unversioned'，
  content_origin='auto'，merge_policy 仅对 locked=True 的历史 Section 设为 'protected'。
  locked 内容继续保留，但只保护该 unversioned 块，不再阻止未来 3.0 等版本块创建。
- wiki_pages.latest_version 不猜测，统一置 NULL（unversioned 不可比，不得声明最新）。

downgrade：删除新增列与 wiki_version_sources 表；版本字段数据随列删除丢失，
version_label/merge_policy 信息无法恢复，故 downgrade 前应已备份。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column(table: str, column_name: str, column_type, inspector) -> None:
    columns = {col["name"] for col in inspector.get_columns(table)}
    if column_name not in columns:
        op.add_column(table, sa.Column(column_name, column_type, nullable=True))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # ---- wiki_pages.latest_version ----
    _add_column("wiki_pages", "latest_version", sa.String(64), inspector)

    # ---- wiki_sections 版本字段 ----
    _add_column("wiki_sections", "version_label", sa.String(64), inspector)
    _add_column("wiki_sections", "version_sort_key", sa.String(255), inspector)
    _add_column("wiki_sections", "is_common", sa.Boolean(), inspector)
    _add_column("wiki_sections", "content_origin", sa.String(32), inspector)
    _add_column("wiki_sections", "merge_policy", sa.String(32), inspector)
    _add_column("wiki_sections", "version_confidence", sa.Float(), inspector)
    _add_column("wiki_sections", "version_status", sa.String(32), inspector)
    _add_column("wiki_sections", "diff_notice", sa.Text(), inspector)

    # 回填：summary → common；facts/body → unversioned；locked → protected。
    # Boolean 字面量用 TRUE/FALSE（PG 严格；SQLite 同样接受）。
    op.execute(
        "UPDATE wiki_sections SET "
        "version_label = 'common', is_common = TRUE, "
        "content_origin = COALESCE(content_origin, 'auto'), "
        "merge_policy = COALESCE(merge_policy, 'auto'), "
        "version_status = 'confirmed', version_confidence = 1.0 "
        "WHERE section_type = 'summary'"
    )
    op.execute(
        "UPDATE wiki_sections SET "
        "version_label = 'unversioned', is_common = FALSE, "
        "content_origin = COALESCE(content_origin, 'auto'), "
        "merge_policy = CASE WHEN locked = TRUE THEN 'protected' ELSE 'auto' END, "
        "version_status = 'unversioned', version_confidence = 0.0 "
        "WHERE (section_type IN ('facts', 'body')) AND version_label IS NULL"
    )
    # 其余历史 Section（entities/evidence/gaps 等）也归入 unversioned，不猜测版本。
    op.execute(
        "UPDATE wiki_sections SET "
        "version_label = 'unversioned', is_common = FALSE, "
        "content_origin = COALESCE(content_origin, 'auto'), "
        "merge_policy = CASE WHEN locked = TRUE THEN 'protected' ELSE 'auto' END, "
        "version_status = 'unversioned', version_confidence = 0.0 "
        "WHERE version_label IS NULL"
    )

    # ---- 新索引：revision + version_label ----
    index_names = {ix["name"] for ix in inspector.get_indexes("wiki_sections")}
    if "ix_wiki_sections_revision_version" not in index_names:
        op.create_index(
            "ix_wiki_sections_revision_version",
            "wiki_sections",
            ["revision_id", "version_label"],
            unique=False,
        )

    # ---- wiki_version_sources 关联表（幂等：历史 init_db 的 create_all 可能已建表，
    #      导致真实库存在 schema 漂移，表已存在但版本列未加）。----
    if not inspector.has_table("wiki_version_sources"):
        op.create_table(
            "wiki_version_sources",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("wiki_page_id", sa.String(36), nullable=False),
            sa.Column("version_label", sa.String(64), nullable=False),
            sa.Column("page_id", sa.String(36), nullable=True),
            sa.Column("chunk_id", sa.String(36), nullable=True),
            sa.Column("source_item_id", sa.String(36), nullable=True),
            sa.Column("acl_scope", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["wiki_page_id"], ["wiki_pages.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    vs_index_names = {ix["name"] for ix in inspector.get_indexes("wiki_version_sources")}
    if "ix_wiki_version_sources_wiki_page_id" not in vs_index_names:
        op.create_index("ix_wiki_version_sources_wiki_page_id", "wiki_version_sources", ["wiki_page_id"], unique=False)
    if "ix_wiki_version_sources_version_label" not in vs_index_names:
        op.create_index("ix_wiki_version_sources_version_label", "wiki_version_sources", ["version_label"], unique=False)
    if "ux_wiki_version_sources" not in vs_index_names:
        op.create_index(
            "ux_wiki_version_sources",
            "wiki_version_sources",
            ["wiki_page_id", "version_label", "page_id", "chunk_id"],
            unique=True,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # 先删引用 version_label 的索引，避免 drop column 报错。
    index_names = {ix["name"] for ix in inspector.get_indexes("wiki_sections")}
    if "ix_wiki_sections_revision_version" in index_names:
        op.drop_index("ix_wiki_sections_revision_version", table_name="wiki_sections")

    # 删除关联表。
    if inspector.has_table("wiki_version_sources"):
        op.drop_table("wiki_version_sources")

    for table, column_name in (
        ("wiki_sections", "diff_notice"),
        ("wiki_sections", "version_status"),
        ("wiki_sections", "version_confidence"),
        ("wiki_sections", "merge_policy"),
        ("wiki_sections", "content_origin"),
        ("wiki_sections", "is_common"),
        ("wiki_sections", "version_sort_key"),
        ("wiki_sections", "version_label"),
        ("wiki_pages", "latest_version"),
    ):
        columns = {col["name"] for col in inspector.get_columns(table)}
        if column_name in columns:
            op.drop_column(table, column_name)
