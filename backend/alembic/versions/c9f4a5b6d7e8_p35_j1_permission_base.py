"""P35 J-1：权限底座与钉钉文件归属（Notebook 多组 + 文件夹映射）。

Revision ID: c9f4a5b6d7e8
Revises: e5f6a7b8c9d0 (P34 版本感知 Wiki)
Create Date: Phase J-1

新增表：
- notebook_groups：Notebook 可访问业务组关联表（多组授权）。
    notebook_id → notebooks.id（CASCADE）；group_name 唯一于 (notebook_id, group_name)。
- dingtalk_folder_mappings：钉钉文件夹 → 目标 Notebook 映射。
    space_id+folder_path 唯一；notebook_id → notebooks.id（CASCADE）。

不修改任何历史 migration。真实库停在 P33，本迁移基于 P34，临时升级链必须
包含 P34 → P35。本迁移只对临时库/真实库只读副本验证，不对真实库执行。

downgrade：删除两张新表（不触碰其他表）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9f4a5b6d7e8"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notebook_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("notebook_id", sa.String(36), sa.ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("notebook_id", "group_name", name="ux_notebook_group"),
    )
    op.create_index("ix_notebook_groups_notebook_id", "notebook_groups", ["notebook_id"])
    op.create_index("ix_notebook_groups_group_name", "notebook_groups", ["group_name"])

    op.create_table(
        "dingtalk_folder_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        # space_id 用空串表示「任意知识库」：SQLite 唯一约束对 NULL 不生效，
        # 空串才能拦截同一 folder_path 的重复映射。
        sa.Column("space_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("folder_path", sa.String(1024), nullable=False),
        sa.Column("notebook_id", sa.String(36), sa.ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("space_id", "folder_path", name="ux_dingtalk_folder_mapping"),
    )
    op.create_index("ix_dingtalk_folder_mappings_space_id", "dingtalk_folder_mappings", ["space_id"])
    op.create_index("ix_dingtalk_folder_mappings_notebook_id", "dingtalk_folder_mappings", ["notebook_id"])


def downgrade() -> None:
    op.drop_table("dingtalk_folder_mappings")
    op.drop_table("notebook_groups")
