"""P33 V4 Phase H：删除已退出的 Card/KO/owner/review/risk/旧 Community/旧 Graph 表。

Revision ID: d1e2f3a4b5c6
Revises: a8b9c0d1e2f3 (P32.5 reconciliation)
Create Date: Phase H-1

依赖图（外键逆序，先删引用者，后删被引用者）——按 ORM/Alembic 历史外键整理：

被删表之间的外键依赖（source -> target）：
  knowledge_debt_queries.debt_id        -> knowledge_debts.id (CASCADE)
  knowledge_debt_queries.query_log_id   -> query_logs.id (CASCADE)
  knowledge_debt_cards.debt_id          -> knowledge_debts.id (CASCADE)
  community_members.community_id        -> knowledge_communities.id (CASCADE)
  community_members.entity_id           -> canonical_entities.id (CASCADE)
  entity_aliases.canonical_entity_id    -> canonical_entities.id (CASCADE)
  card_entity_links.entity_id           -> canonical_entities.id (CASCADE)
  card_graph_relations.source_entity_id -> canonical_entities.id (CASCADE)
  card_graph_relations.target_entity_id -> canonical_entities.id (CASCADE)
  knowledge_card_sources.card_id        -> knowledge_cards.id (CASCADE)
  knowledge_claims.card_id              -> knowledge_cards.id (CASCADE)
  knowledge_claims.revision_id          -> knowledge_card_revisions.id (CASCADE)
  knowledge_card_blocks.card_id         -> knowledge_cards.id (CASCADE)
  knowledge_card_blocks.revision_id     -> knowledge_card_revisions.id (CASCADE)
  knowledge_card_revisions.card_id      -> knowledge_cards.id (CASCADE)

保留表外键（不阻断删除，但不得级联删除保留表）：
  knowledge_card_sources.page_id        -> pages.id（保留，CASCADE 仅删 sources 行）
  knowledge_card_sources.evidence_id    -> evidence_items.id（保留）
  card_compile_reports.page_id          -> pages.id（保留）
  evidence_links.evidence_id            -> evidence_items.id（保留）
  wiki_citations.section_id             -> wiki_sections.id（保留）
  graph_edges.source_id/target_id       -> pages.id（保留）

因此删除顺序为：
  1. 关联/子表：knowledge_debt_cards → knowledge_debt_queries → debt_candidates
     → community_members → community_rebuild_jobs → wiki_citations →
     card_compile_reports → evidence_links → card_graph_relations →
     card_entity_links → entity_aliases → conflict_tasks → query_logs
  2. Card 主表：knowledge_card_sources → knowledge_claims → knowledge_card_blocks
     → knowledge_card_revisions → knowledge_cards
  3. 旧实体/社区主表：canonical_entities → knowledge_communities → graph_edges

保留：V4 knowledge_debts 新字段及 knowledge_debt_users；Page/Chunk/Notebook/
EvidenceItem/Wiki/Debt V4/Source/ACL 全部保留。

downgrade：本迁移**不可逆**——删除的表与历史数据无法在数据库内重建，
downgrade 必须在版本号改变前安全失败，恢复只能通过迁移前已验证备份完成。

同时删除所有已移除 Feature Flag 的 runtime_feature_flags 行（含 card_v3_enabled），
保留仍有效的 dingtalk_connector_enabled / source_hub_enabled / wiki_topic_enabled /
gitlab_connector_enabled。
"""
from __future__ import annotations

import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# 已移除的 Feature Flag（P33 删除其 runtime_feature_flags 行）。
_LEGACY_FLAGS = (
    "card_v3_enabled",
    "unified_retrieval_enabled",
    "debt_chat_enabled",
    "card_graph_enabled",
    "layered_retrieval_enabled",
    "source_card_compile_enabled",
    "legacy_debt_card_enabled",
    "legacy_readonly",
)

# 按外键依赖逆序排列：先删引用他人的子表/关联表，最后删被引用的主表。
_DROP_TABLES = [
    "knowledge_debt_cards",
    "knowledge_debt_queries",
    "debt_candidates",
    "community_members",
    "community_rebuild_jobs",
    "wiki_citations",
    "card_compile_reports",
    "evidence_links",
    "card_graph_relations",
    "card_entity_links",
    "entity_aliases",
    "conflict_tasks",
    "query_logs",
    "knowledge_card_sources",
    "knowledge_claims",
    "knowledge_card_blocks",
    "knowledge_card_revisions",
    "knowledge_cards",
    "canonical_entities",
    "knowledge_communities",
    "graph_edges",
]

# 保留表内需删除的旧字段。
_DROP_COLUMNS = {
    "knowledge_debts": ["resolution_card_id", "resolution_evidence_id"],
    "pages": ["compile_hash"],
}


def _drop_table_if_exists(name: str) -> None:
    bind = op.get_bind()
    try:
        inspector = sa.inspect(bind)
        if not inspector.has_table(name):
            return
    except Exception:
        # 离线（--sql）模式：无法 inspect，直接 emit DROP TABLE IF EXISTS。
        op.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))
        return
    op.drop_table(name)


def _drop_column_if_exists(table: str, column: str) -> None:
    bind = op.get_bind()
    try:
        inspector = sa.inspect(bind)
        if not inspector.has_table(table):
            return
        columns = {c["name"] for c in inspector.get_columns(table)}
        if column not in columns:
            return
    except Exception:
        op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
        return
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.drop_column(column)


def upgrade() -> None:
    for table in _DROP_TABLES:
        _drop_table_if_exists(table)

    for table, columns in _DROP_COLUMNS.items():
        for column in columns:
            _drop_column_if_exists(table, column)

    # 删除已移除 Feature Flag 的 runtime_feature_flags 行。
    # 这些 flag 名是受控常量（无注入风险），内联为 SQL 字面量，确保
    # 在线执行与离线 --sql 生成都正确渲染（避免 literal_binds 把绑定参数渲染成 NULL）。
    bind = op.get_bind()
    quoted = ", ".join(f"'{flag}'" for flag in _LEGACY_FLAGS)
    bind.execute(sa.text(f"DELETE FROM runtime_feature_flags WHERE name IN ({quoted})"))
    logger.info("P33: removed %d legacy feature flags", len(_LEGACY_FLAGS))


def downgrade() -> None:
    """不可逆迁移：downgrade 必须在版本号改变前安全失败。

    删除的 21 张表及历史 Card/KO/owner/review/risk 数据无法在数据库内重建；
    恢复只能通过迁移前已完成并验证的完整备份完成，本迁移不伪造空 schema 恢复。
    """
    raise RuntimeError(
        "P33 是不可逆迁移：已删除 21 张 Card/KO/owner/review/risk 表及其数据，"
        "无法在数据库内恢复。请使用迁移前已完成并验证的备份恢复。"
    )
