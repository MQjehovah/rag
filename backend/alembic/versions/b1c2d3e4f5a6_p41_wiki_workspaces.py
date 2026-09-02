"""P41：独立 WikiWorkspace 与确定性 Notebook 路由（Phase 3）。

Revision ID: b1c2d3e4f5a6
Revises: f2a3b4c5d6e7 (P40)
Create Date: Phase 3

新增两张表 + wiki_pages.workspace_id：
- wiki_workspaces：独立 Wiki 工作区（key 确定性稳定键，scope_id 规范化权限域）。
- notebook_workspace_bindings：Notebook → workspace 绑定（active/disabled，
  唯一约束 ux_nb_ws_binding(notebook_id, workspace_id)）。
- wiki_pages.workspace_id：FK → wiki_workspaces.id ondelete=SET NULL nullable index。

迁移期 nullable；自动编译产生的 Wiki 由写入端强制非空。

SQLite 使用 batch_alter_table（本迁移同时兼容 PostgreSQL）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.create_table(
        "wiki_workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("acl_scope", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_workspace_status"),
    )
    op.create_index("ux_wiki_workspaces_key", "wiki_workspaces", ["key"], unique=True)
    op.create_index("ix_wiki_workspaces_scope_id", "wiki_workspaces", ["scope_id"])
    op.create_index("ix_wiki_workspaces_status", "wiki_workspaces", ["status"])

    op.create_table(
        "notebook_workspace_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("notebook_id", sa.String(36), sa.ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("wiki_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_binding_status"),
    )
    op.create_index("ix_notebook_workspace_bindings_notebook_id", "notebook_workspace_bindings", ["notebook_id"])
    op.create_index("ix_notebook_workspace_bindings_workspace_id", "notebook_workspace_bindings", ["workspace_id"])
    op.create_index(
        "ux_nb_ws_binding",
        "notebook_workspace_bindings",
        ["notebook_id", "workspace_id"],
        unique=True,
    )
    # Phase 3.1：数据库部分唯一索引 —— 一个 Notebook 同时最多一个 active binding。
    # SQLite 与 PostgreSQL 均支持 sqlite_where/postgresql_where 谓词索引。
    op.create_index(
        "ux_nb_ws_binding_active",
        "notebook_workspace_bindings",
        ["notebook_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )

    if dialect == "sqlite":
        with op.batch_alter_table("wiki_pages") as batch_op:
            batch_op.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
            batch_op.create_foreign_key(
                "fk_wiki_pages_workspace_id",
                "wiki_workspaces",
                ["workspace_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        op.add_column("wiki_pages", sa.Column("workspace_id", sa.String(36), nullable=True))
        op.create_foreign_key(
            "fk_wiki_pages_workspace_id",
            "wiki_pages",
            "wiki_workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_wiki_pages_workspace_id", "wiki_pages", ["workspace_id"])


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_index("ix_wiki_pages_workspace_id", table_name="wiki_pages")
    if dialect == "sqlite":
        # SQLite batch 重建表：drop_column 会同时丢弃引用该列的 FK 约束。
        with op.batch_alter_table("wiki_pages") as batch_op:
            batch_op.drop_column("workspace_id")
    else:
        op.drop_constraint("fk_wiki_pages_workspace_id", "wiki_pages", type_="foreignkey")
        op.drop_column("wiki_pages", "workspace_id")

    op.drop_index("ux_nb_ws_binding", table_name="notebook_workspace_bindings")
    op.drop_index("ux_nb_ws_binding_active", table_name="notebook_workspace_bindings")
    op.drop_index("ix_notebook_workspace_bindings_workspace_id", table_name="notebook_workspace_bindings")
    op.drop_index("ix_notebook_workspace_bindings_notebook_id", table_name="notebook_workspace_bindings")
    op.drop_table("notebook_workspace_bindings")

    op.drop_index("ix_wiki_workspaces_status", table_name="wiki_workspaces")
    op.drop_index("ix_wiki_workspaces_scope_id", table_name="wiki_workspaces")
    op.drop_index("ux_wiki_workspaces_key", table_name="wiki_workspaces")
    op.drop_table("wiki_workspaces")
