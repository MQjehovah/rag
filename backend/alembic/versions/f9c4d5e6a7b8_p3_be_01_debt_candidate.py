"""P3_BE_01 debt candidate + knowledge_debts extension

Revision ID: f9c4d5e6a7b8
Revises: e8b3c4d5f6a7
Create Date: 2026-08-19 11:00:00.000000

P3（V3 计划 8.3）：
- knowledge_debts 扩展 P3 字段（root_cause/scope_json/priority/owner/
  occurrence_count/resolution_card_id/resolution_evidence_id/first_seen_at/
  last_seen_at/acl_scope/title）
- 新增 debt_candidates 表（债务候选项）
- 新增 knowledge_debt_queries / knowledge_debt_cards 关联表
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9c4d5e6a7b8'
down_revision: Union[str, Sequence[str], None] = 'e8b3c4d5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # knowledge_debts 扩展字段
    with op.batch_alter_table('knowledge_debts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('title', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('root_cause', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('scope_json', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('priority', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('owner', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('occurrence_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('resolution_card_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('resolution_evidence_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('first_seen_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('last_seen_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('acl_scope', sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f('ix_knowledge_debts_root_cause'), ['root_cause'], unique=False)

    # debt_candidates
    op.create_table('debt_candidates',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('root_cause', sa.String(length=50), nullable=False),
        sa.Column('normalized_question', sa.Text(), nullable=True),
        sa.Column('intent', sa.String(length=50), nullable=True),
        sa.Column('scope_json', sa.Text(), nullable=True),
        sa.Column('cluster_key', sa.String(length=255), nullable=True),
        sa.Column('representative_query', sa.Text(), nullable=True),
        sa.Column('occurrence_count', sa.Integer(), nullable=True),
        sa.Column('unique_user_count', sa.Integer(), nullable=True),
        sa.Column('negative_feedback_count', sa.Integer(), nullable=True),
        sa.Column('priority_score', sa.Float(), nullable=True),
        sa.Column('acl_scope', sa.Text(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('debt_candidates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_debt_candidates_root_cause'), ['root_cause'], unique=False)
        batch_op.create_index(batch_op.f('ix_debt_candidates_cluster_key'), ['cluster_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_debt_candidates_status'), ['status'], unique=False)

    # knowledge_debt_queries
    op.create_table('knowledge_debt_queries',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('debt_id', sa.String(length=36), nullable=False),
        sa.Column('query_log_id', sa.String(length=36), nullable=False),
        sa.Column('relation_type', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['debt_id'], ['knowledge_debts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['query_log_id'], ['query_logs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('knowledge_debt_queries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_knowledge_debt_queries_debt_id'), ['debt_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_debt_queries_query_log_id'), ['query_log_id'], unique=False)

    # knowledge_debt_cards
    op.create_table('knowledge_debt_cards',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('debt_id', sa.String(length=36), nullable=False),
        sa.Column('card_id', sa.String(length=36), nullable=False),
        sa.Column('relation_type', sa.String(length=32), nullable=True),
        sa.Column('match_score', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['debt_id'], ['knowledge_debts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('knowledge_debt_cards', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_knowledge_debt_cards_debt_id'), ['debt_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_debt_cards_card_id'), ['card_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('knowledge_debt_cards', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_knowledge_debt_cards_card_id'))
        batch_op.drop_index(batch_op.f('ix_knowledge_debt_cards_debt_id'))
    op.drop_table('knowledge_debt_cards')

    with op.batch_alter_table('knowledge_debt_queries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_knowledge_debt_queries_query_log_id'))
        batch_op.drop_index(batch_op.f('ix_knowledge_debt_queries_debt_id'))
    op.drop_table('knowledge_debt_queries')

    with op.batch_alter_table('debt_candidates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_debt_candidates_status'))
        batch_op.drop_index(batch_op.f('ix_debt_candidates_cluster_key'))
        batch_op.drop_index(batch_op.f('ix_debt_candidates_root_cause'))
    op.drop_table('debt_candidates')

    with op.batch_alter_table('knowledge_debts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_knowledge_debts_root_cause'))
        batch_op.drop_column('acl_scope')
        batch_op.drop_column('last_seen_at')
        batch_op.drop_column('first_seen_at')
        batch_op.drop_column('resolution_evidence_id')
        batch_op.drop_column('resolution_card_id')
        batch_op.drop_column('occurrence_count')
        batch_op.drop_column('owner')
        batch_op.drop_column('priority')
        batch_op.drop_column('scope_json')
        batch_op.drop_column('root_cause')
        batch_op.drop_column('title')
