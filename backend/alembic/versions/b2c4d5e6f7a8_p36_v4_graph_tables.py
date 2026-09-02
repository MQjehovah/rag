"""P36 J-3：真正的实体关系知识图谱（V4 图谱持久化表）。

Revision ID: b2c4d5e6f7a8
Revises: c9f4a5b6d7e8 (P35 J-1 权限底座)
Create Date: Phase J-3

新增 5 张表（不复活 P33 已删除的旧图谱/Card/Community 表）：
- v4_graph_entities：实体节点（确定性稳定 key，scope 隔离）。
- v4_graph_relations：带关系名的边（version_family + 同 source/type/target/version 去重）。
- v4_graph_relation_evidence：关系 → Page/Chunk/Evidence/Wiki 追溯映射（内部）。
- v4_graph_entity_evidence：实体 → Page/Chunk/Evidence/Wiki 追溯映射（内部）。
- v4_graph_communities：Community 分组（只做分组/颜色/布局）。

provenance 真实外键：
- page_id → pages.id（CASCADE：Page 删除即失效图谱证据）。
- chunk_id → page_chunks.id（CASCADE）。
- evidence_id → evidence_items.id（CASCADE）。
- wiki_page_id → wiki_pages.id（CASCADE）。
- revision_id → wiki_revisions.id（CASCADE：Revision 删除即失效图谱证据；
  图谱 provenance 表不反向引用 wiki_revisions，不构成循环）。
- section_id → wiki_sections.id（CASCADE：Section 删除即失效图谱证据，不残留）。

downgrade：删除 5 张新表（不触碰其他表；图谱数据可随时重建）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "c9f4a5b6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "v4_graph_entities",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("community_key", sa.String(512), nullable=True),
        sa.Column("acl_scope", sa.Text(), nullable=True),
        sa.Column("version_label", sa.String(64), nullable=True),
        sa.Column("version_status", sa.String(32), nullable=True, server_default="unversioned"),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_v4_graph_entities_entity_type", "v4_graph_entities", ["entity_type"])
    op.create_index("ix_v4_graph_entities_community_key", "v4_graph_entities", ["community_key"])
    op.create_index("ix_v4_graph_entities_updated_at", "v4_graph_entities", ["updated_at"])

    op.create_table(
        "v4_graph_relations",
        sa.Column("id", sa.String(1024), primary_key=True),
        sa.Column("source_id", sa.String(512), sa.ForeignKey("v4_graph_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", sa.String(512), sa.ForeignKey("v4_graph_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relation_type", sa.String(64), nullable=False),
        sa.Column("version_label", sa.String(64), nullable=True),
        sa.Column("version_status", sa.String(32), nullable=True, server_default="unversioned"),
        sa.Column("version_family", sa.String(255), nullable=True),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("acl_scope", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_v4_graph_relations_source_id", "v4_graph_relations", ["source_id"])
    op.create_index("ix_v4_graph_relations_target_id", "v4_graph_relations", ["target_id"])
    op.create_index("ix_v4_graph_relations_version_family", "v4_graph_relations", ["version_family"])
    op.create_index("ix_v4_graph_relations_updated_at", "v4_graph_relations", ["updated_at"])

    op.create_table(
        "v4_graph_relation_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("relation_id", sa.String(1024), sa.ForeignKey("v4_graph_relations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("chunk_id", sa.String(36), sa.ForeignKey("page_chunks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("evidence_items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("wiki_page_id", sa.String(36), sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("wiki_revisions.id", ondelete="CASCADE"), nullable=True),
        sa.Column("section_id", sa.String(36), sa.ForeignKey("wiki_sections.id", ondelete="CASCADE"), nullable=True),
    )
    op.create_index("ix_v4_graph_relation_evidence_relation_id", "v4_graph_relation_evidence", ["relation_id"])
    op.create_index("ix_v4_graph_relation_evidence_page_id", "v4_graph_relation_evidence", ["page_id"])
    op.create_index(
        "ux_v4_graph_rel_evidence",
        "v4_graph_relation_evidence",
        ["relation_id", "page_id", "chunk_id", "evidence_id", "wiki_page_id", "revision_id", "section_id"],
        unique=True,
    )

    op.create_table(
        "v4_graph_entity_evidence",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("entity_id", sa.String(512), sa.ForeignKey("v4_graph_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("chunk_id", sa.String(36), sa.ForeignKey("page_chunks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("evidence_items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("wiki_page_id", sa.String(36), sa.ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("wiki_revisions.id", ondelete="CASCADE"), nullable=True),
        sa.Column("section_id", sa.String(36), sa.ForeignKey("wiki_sections.id", ondelete="CASCADE"), nullable=True),
    )
    op.create_index("ix_v4_graph_entity_evidence_entity_id", "v4_graph_entity_evidence", ["entity_id"])
    op.create_index("ix_v4_graph_entity_evidence_page_id", "v4_graph_entity_evidence", ["page_id"])
    op.create_index(
        "ux_v4_graph_entity_evidence",
        "v4_graph_entity_evidence",
        ["entity_id", "page_id", "chunk_id", "evidence_id", "wiki_page_id", "revision_id", "section_id"],
        unique=True,
    )

    op.create_table(
        "v4_graph_communities",
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("acl_scope", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_v4_graph_communities_acl_scope", "v4_graph_communities", ["acl_scope"])


def downgrade() -> None:
    op.drop_table("v4_graph_entity_evidence")
    op.drop_table("v4_graph_relation_evidence")
    op.drop_table("v4_graph_relations")
    op.drop_table("v4_graph_communities")
    op.drop_table("v4_graph_entities")
