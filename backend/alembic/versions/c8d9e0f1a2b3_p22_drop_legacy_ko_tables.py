"""P22：删除仅服务旧 KnowledgeObject 的表与外键。

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _assert_ko_migration_ready(bind) -> None:
    """删除旧表前的不可绕过门禁。"""
    inspector = sa.inspect(bind)
    if not (
        inspector.has_table("knowledge_objects")
        and inspector.has_table("legacy_ko_mappings")
    ):
        return

    ko_total = bind.execute(sa.text("SELECT COUNT(*) FROM knowledge_objects")).scalar() or 0
    terminal_total = bind.execute(sa.text(
        "SELECT COUNT(DISTINCT legacy_ko_id) FROM legacy_ko_mappings "
        "WHERE migration_status IN "
        "('migrated', 'needs_recompile', 'recompiled', 'rejected_with_reason')"
    )).scalar() or 0
    if terminal_total != ko_total:
        raise RuntimeError(
            f"P22 门禁失败：旧 KO={ko_total}，具备终态映射={terminal_total}"
        )

    invalid_migrated = bind.execute(sa.text(
        "SELECT COUNT(*) FROM legacy_ko_mappings m "
        "LEFT JOIN knowledge_cards c ON c.id=m.new_card_id "
        "WHERE m.migration_status='migrated' AND "
        "(c.id IS NULL OR c.status!='published' OR NOT EXISTS ("
        "SELECT 1 FROM evidence_links e WHERE e.card_id=c.id))"
    )).scalar() or 0
    if invalid_migrated:
        raise RuntimeError(
            f"P22 门禁失败：{invalid_migrated} 条 migrated 映射没有 Published Card + Evidence"
        )

    incorrectly_bound = bind.execute(sa.text(
        "SELECT COUNT(*) FROM legacy_ko_mappings m "
        "JOIN knowledge_objects k ON k.id=m.legacy_ko_id "
        "WHERE k.status!='published' AND "
        "(m.new_card_id IS NOT NULL OR m.new_claim_id IS NOT NULL)"
    )).scalar() or 0
    if incorrectly_bound:
        raise RuntimeError(
            f"P22 门禁失败：{incorrectly_bound} 条非 Published KO 仍绑定 Card/Claim"
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _assert_ko_migration_ready(bind)

    if inspector.has_table("ko_chunks"):
        indexes = {idx["name"] for idx in inspector.get_indexes("ko_chunks")}
        if "ix_ko_chunks_embedding_hnsw" in indexes:
            op.drop_index("ix_ko_chunks_embedding_hnsw", table_name="ko_chunks")
        op.drop_table("ko_chunks")

    if inspector.has_table("evidence_links"):
        # SQLite 的旧 legacy_ko_id 外键通常没有名称，不能依靠
        # drop_constraint(name) 清除。强制重建表并删除整列，才能保证
        # knowledge_objects 删除后 INSERT 不再触发 “no such table”。
        columns = {column["name"] for column in inspector.get_columns("evidence_links")}
        indexes = {index["name"] for index in inspector.get_indexes("evidence_links")}
        if "legacy_ko_id" in columns:
            with op.batch_alter_table("evidence_links", recreate="always") as batch:
                if "ix_elink_ko_evidence" in indexes:
                    batch.drop_index("ix_elink_ko_evidence")
                if "ix_evidence_links_legacy_ko_id" in indexes:
                    batch.drop_index("ix_evidence_links_legacy_ko_id")
                batch.drop_column("legacy_ko_id")
                if "ix_elink_card_evidence" not in indexes:
                    batch.create_index(
                        "ix_elink_card_evidence", ["card_id", "evidence_id"]
                    )

    if inspector.has_table("knowledge_objects"):
        op.drop_table("knowledge_objects")

    # 在迁移内部做最后一道完整性检查，避免带着悬空外键继续升级。
    if bind.dialect.name == "sqlite":
        violations = bind.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
        if violations:
            raise RuntimeError(f"P22 外键完整性检查失败: {violations[:10]}")


def downgrade() -> None:
    """Schema 删除后只允许数据库级恢复，不重建旧 KO 产品表。"""
    raise NotImplementedError("P22 不回滚旧 KnowledgeObject 表；请从备份恢复数据库")
