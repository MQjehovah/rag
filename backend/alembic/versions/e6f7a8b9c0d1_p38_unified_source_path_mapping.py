"""P38：统一数据源路径权限映射。

Revision ID: e6f7a8b9c0d1
Revises: d4e5f6a7b8c9 (P37)
Create Date: Phase J-5

新增 source_path_mappings 表（各数据源共用），将现有 dingtalk_folder_mappings
的 4 条真实语义迁移到新表，随后删除旧表。

迁移规则：
- 每条旧映射必须找到唯一对应的钉钉 SourceConnection；
- 找不到连接 / 存在多个无法确定的连接 → fail closed（不猜测、不删除、不生成错误映射）；
- 保留所有 notebook_id、space_id（→ path_namespace）、folder_path；
- 4 条迁移后仍为 1 条根映射 + 3 条顶级目录映射。

downgrade：重建旧表结构并回迁数据（结构恢复；业务数据若已在新表增改，不保证完整逆转）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _unique_dingtalk_connection(conn):
    """从 SQL 连接中找出唯一钉钉 SourceConnection.id；0 或 >1 返回 None（fail closed）。"""
    rows = conn.execute(
        sa.text("SELECT id FROM source_connections WHERE connector_key = 'dingtalk'")
    ).fetchall()
    if len(rows) != 1:
        return None
    return rows[0][0]


def upgrade() -> None:
    op.create_table(
        "source_path_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("source_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path_namespace", sa.String(255), nullable=False, server_default=""),
        sa.Column("folder_path", sa.String(1024), nullable=False),
        sa.Column("notebook_id", sa.String(36), sa.ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_source_path_mappings_connection_id", "source_path_mappings", ["connection_id"])
    op.create_index("ix_source_path_mappings_path_namespace", "source_path_mappings", ["path_namespace"])
    op.create_index("ix_source_path_mappings_notebook_id", "source_path_mappings", ["notebook_id"])
    op.create_index(
        "ux_source_path_mapping",
        "source_path_mappings",
        ["connection_id", "path_namespace", "folder_path"],
        unique=True,
    )

    # 迁移旧钉钉映射（仅当旧表存在时；全新空库无旧表）。
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("dingtalk_folder_mappings"):
        conn_id = _unique_dingtalk_connection(bind)
        old_rows = bind.execute(
            sa.text(
                "SELECT id, space_id, folder_path, notebook_id, created_by, created_at, updated_at "
                "FROM dingtalk_folder_mappings ORDER BY folder_path"
            )
        ).fetchall()
        if old_rows:
            if conn_id is None:
                raise RuntimeError(
                    "P38 迁移失败：无法唯一确定钉钉 SourceConnection，拒绝猜测/迁移/删除旧映射"
                )
            for row in old_rows:
                # 保留原 id，path_namespace = space_id
                bind.execute(
                    sa.text(
                        "INSERT INTO source_path_mappings "
                        "(id, connection_id, path_namespace, folder_path, notebook_id, created_by, created_at, updated_at) "
                        "VALUES (:id, :connection_id, :path_namespace, :folder_path, :notebook_id, :created_by, :created_at, :updated_at)"
                    ),
                    {
                        "id": row[0],
                        "connection_id": conn_id,
                        "path_namespace": row[1] or "",
                        "folder_path": row[2] or "",
                        "notebook_id": row[3],
                        "created_by": row[4],
                        "created_at": row[5],
                        "updated_at": row[6],
                    },
                )

        # 新表数据无误后再删旧表。
        op.drop_table("dingtalk_folder_mappings")


def downgrade() -> None:
    # 恢复结构：重建旧表并把新表数据回迁（path_namespace → space_id）。
    # 但在任何 DDL 写入之前做完整预检（fail closed），仅当满足以下全部条件才允许：
    #   1. source_path_mappings 所有行都属于钉钉 Connector；
    #   2. 所有映射只属于同一个钉钉 connection；
    #   3. 转换成 (space_id, folder_path) 后无重复；
    #   4. Notebook FK 均有效。
    # 否则立即抛 RuntimeError，版本和 schema 保持 P38 状态，不创建半成品旧表，不删除新表。
    bind = op.get_bind()

    # 预检：connection 归属 + 唯一钉钉连接
    rows = bind.execute(
        sa.text(
            "SELECT spm.id, spm.connection_id, spm.path_namespace, spm.folder_path, spm.notebook_id, "
            "       sc.connector_key "
            "FROM source_path_mappings spm "
            "LEFT JOIN source_connections sc ON sc.id = spm.connection_id "
            "ORDER BY spm.folder_path"
        )
    ).fetchall()

    conn_ids: set = set()
    for row in rows:
        connector_key = row[5]
        if connector_key != "dingtalk":
            raise RuntimeError(
                "P38 downgrade 失败：存在非钉钉 Connector 映射（connector_key=%s），"
                "无法安全降级，schema 保持 P38。" % (connector_key,)
            )
        conn_ids.add(row[1])

    if len(conn_ids) > 1:
        raise RuntimeError(
            "P38 downgrade 失败：映射属于多个钉钉 connection（%d 个），"
            "connection_id 语义无法保留，schema 保持 P38。" % len(conn_ids)
        )

    # 预检：(space_id, folder_path) 唯一键冲突
    seen: set = set()
    notebook_ids: set = set()
    for row in rows:
        key = (row[2] or "", row[3] or "")
        if key in seen:
            raise RuntimeError(
                "P38 downgrade 失败：(space_id, folder_path) 存在重复，"
                "降级后唯一键冲突，schema 保持 P38。"
            )
        seen.add(key)
        notebook_ids.add(row[4])

    # 预检：Notebook FK 有效
    if notebook_ids:
        placeholders = ",".join([":nb%d" % i for i in range(len(notebook_ids))])
        params = {"nb%d" % i: nid for i, nid in enumerate(sorted(notebook_ids))}
        existing = {
            r[0]
            for r in bind.execute(
                sa.text("SELECT id FROM notebooks WHERE id IN (%s)" % placeholders), params
            ).fetchall()
        }
        missing = notebook_ids - existing
        if missing:
            raise RuntimeError(
                "P38 downgrade 失败：映射指向不存在的 Notebook（%s），schema 保持 P38。" % sorted(missing)
            )

    # 预检通过后才执行 DDL。
    op.create_table(
        "dingtalk_folder_mappings",
        sa.Column("id", sa.String(36), primary_key=True),
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

    for row in rows:
        bind.execute(
            sa.text(
                "INSERT INTO dingtalk_folder_mappings "
                "(id, space_id, folder_path, notebook_id, created_by, created_at, updated_at) "
                "VALUES (:id, :space_id, :folder_path, :notebook_id, :created_by, :created_at, :updated_at)"
            ),
            {
                "id": row[0],
                "space_id": row[2] or "",
                "folder_path": row[3] or "",
                "notebook_id": row[4],
                "created_by": None,
                "created_at": None,
                "updated_at": None,
            },
        )
    op.drop_table("source_path_mappings")
