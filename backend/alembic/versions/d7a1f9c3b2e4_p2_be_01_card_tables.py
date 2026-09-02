"""P2_BE_01 card tables

Revision ID: d7a1f9c3b2e4
Revises: c4e8b2a1d5f3
Create Date: 2026-08-19 10:00:00.000000

新增 P2 Knowledge Card V3 五张表：
- knowledge_cards
- knowledge_card_revisions
- knowledge_card_blocks
- knowledge_claims
- knowledge_card_sources

current_revision_id 不声明 FK（card/revision 循环引用，见模型注释）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7a1f9c3b2e4'
down_revision: Union[str, Sequence[str], None] = 'c4e8b2a1d5f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('knowledge_cards',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('card_type', sa.String(length=32), nullable=False),
        sa.Column('canonical_title', sa.String(length=512), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('scope_json', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('risk_level', sa.String(length=16), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('owner', sa.String(length=255), nullable=True),
        sa.Column('current_revision_id', sa.String(length=36), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('knowledge_cards', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_knowledge_cards_card_type'), ['card_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_cards_content_hash'), ['content_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_cards_status'), ['status'], unique=False)

    op.create_table('knowledge_card_revisions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('card_id', sa.String(length=36), nullable=False),
        sa.Column('parent_revision_id', sa.String(length=36), nullable=True),
        sa.Column('structured_json', sa.Text(), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('change_type', sa.String(length=32), nullable=False),
        sa.Column('change_summary', sa.Text(), nullable=True),
        sa.Column('source_hash', sa.String(length=64), nullable=True),
        sa.Column('generation_method', sa.String(length=32), nullable=True),
        sa.Column('model_name', sa.String(length=127), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.Column('reviewed_by', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['card_id'], ['knowledge_cards.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('knowledge_card_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_knowledge_card_revisions_card_id'), ['card_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_card_revisions_status'), ['status'], unique=False)

    op.create_table('knowledge_card_blocks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('revision_id', sa.String(length=36), nullable=False),
        sa.Column('card_id', sa.String(length=36), nullable=False),
        sa.Column('block_type', sa.String(length=50), nullable=False),
        sa.Column('heading', sa.String(length=512), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=True),
        sa.Column('embedding', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['card_id'], ['knowledge_cards.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['revision_id'], ['knowledge_card_revisions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('knowledge_card_blocks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_knowledge_card_blocks_card_id'), ['card_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_card_blocks_revision_id'), ['revision_id'], unique=False)

    op.create_table('knowledge_claims',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('card_id', sa.String(length=36), nullable=False),
        sa.Column('revision_id', sa.String(length=36), nullable=True),
        sa.Column('claim_type', sa.String(length=50), nullable=False),
        sa.Column('statement', sa.Text(), nullable=False),
        sa.Column('normalized_statement', sa.Text(), nullable=True),
        sa.Column('scope_json', sa.Text(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['card_id'], ['knowledge_cards.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['revision_id'], ['knowledge_card_revisions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('knowledge_claims', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_knowledge_claims_card_id'), ['card_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_claims_revision_id'), ['revision_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_claims_status'), ['status'], unique=False)

    op.create_table('knowledge_card_sources',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('card_id', sa.String(length=36), nullable=False),
        sa.Column('page_id', sa.String(length=36), nullable=True),
        sa.Column('evidence_id', sa.String(length=36), nullable=True),
        sa.Column('contribution_type', sa.String(length=32), nullable=True),
        sa.Column('source_version', sa.String(length=127), nullable=True),
        sa.ForeignKeyConstraint(['card_id'], ['knowledge_cards.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['evidence_id'], ['evidence_items.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('knowledge_card_sources', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_knowledge_card_sources_card_id'), ['card_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_card_sources_evidence_id'), ['evidence_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_knowledge_card_sources_page_id'), ['page_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('knowledge_card_sources', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_knowledge_card_sources_page_id'))
        batch_op.drop_index(batch_op.f('ix_knowledge_card_sources_evidence_id'))
        batch_op.drop_index(batch_op.f('ix_knowledge_card_sources_card_id'))
    op.drop_table('knowledge_card_sources')

    with op.batch_alter_table('knowledge_claims', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_knowledge_claims_status'))
        batch_op.drop_index(batch_op.f('ix_knowledge_claims_revision_id'))
        batch_op.drop_index(batch_op.f('ix_knowledge_claims_card_id'))
    op.drop_table('knowledge_claims')

    with op.batch_alter_table('knowledge_card_blocks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_knowledge_card_blocks_revision_id'))
        batch_op.drop_index(batch_op.f('ix_knowledge_card_blocks_card_id'))
    op.drop_table('knowledge_card_blocks')

    with op.batch_alter_table('knowledge_card_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_knowledge_card_revisions_status'))
        batch_op.drop_index(batch_op.f('ix_knowledge_card_revisions_card_id'))
    op.drop_table('knowledge_card_revisions')

    with op.batch_alter_table('knowledge_cards', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_knowledge_cards_status'))
        batch_op.drop_index(batch_op.f('ix_knowledge_cards_content_hash'))
        batch_op.drop_index(batch_op.f('ix_knowledge_cards_card_type'))
    op.drop_table('knowledge_cards')
