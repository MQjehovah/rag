"""P5_BE_01 canonical entity + community tables

Revision ID: a1b2c3d4e5f6
Revises: f9c4d5e6a7b8
Create Date: 2026-08-19 12:00:00.000000

P5（V3 计划 10.2）六表：
- canonical_entities / entity_aliases / card_entity_links
- card_graph_relations（可追溯，与旧 graph_relations 区分）
- knowledge_communities / community_members
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f9c4d5e6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('canonical_entities',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('entity_type', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('normalized', sa.String(length=255), nullable=False),
        sa.Column('disambiguation_status', sa.String(length=32), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('acl_scope', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('canonical_entities', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_canonical_entities_entity_type'), ['entity_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_canonical_entities_normalized'), ['normalized'], unique=False)
        batch_op.create_index(batch_op.f('ix_canonical_entities_disambiguation_status'), ['disambiguation_status'], unique=False)
        batch_op.create_index('ix_ce_type_normalized', ['entity_type', 'normalized'], unique=True)

    op.create_table('entity_aliases',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('canonical_entity_id', sa.String(length=36), nullable=False),
        sa.Column('alias', sa.String(length=255), nullable=False),
        sa.Column('normalized', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['canonical_entity_id'], ['canonical_entities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('entity_aliases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_entity_aliases_canonical_entity_id'), ['canonical_entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_entity_aliases_alias'), ['alias'], unique=False)
        batch_op.create_index('ix_ea_canonical_alias', ['canonical_entity_id', 'alias'], unique=False)

    op.create_table('card_entity_links',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('card_id', sa.String(length=36), nullable=False),
        sa.Column('entity_id', sa.String(length=36), nullable=False),
        sa.Column('claim_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['entity_id'], ['canonical_entities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('card_entity_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_card_entity_links_card_id'), ['card_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_card_entity_links_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index('ix_cel_card_entity', ['card_id', 'entity_id'], unique=False)

    op.create_table('card_graph_relations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('source_entity_id', sa.String(length=36), nullable=False),
        sa.Column('target_entity_id', sa.String(length=36), nullable=False),
        sa.Column('relation_type', sa.String(length=50), nullable=False),
        sa.Column('card_id', sa.String(length=36), nullable=False),
        sa.Column('claim_id', sa.String(length=36), nullable=True),
        sa.Column('evidence_id', sa.String(length=36), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('valid_from', sa.DateTime(), nullable=True),
        sa.Column('valid_to', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_entity_id'], ['canonical_entities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_entity_id'], ['canonical_entities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('card_graph_relations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_card_graph_relations_source_entity_id'), ['source_entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_card_graph_relations_target_entity_id'), ['target_entity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_card_graph_relations_relation_type'), ['relation_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_card_graph_relations_card_id'), ['card_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_card_graph_relations_status'), ['status'], unique=False)
        batch_op.create_index('ix_cgr_source_target', ['source_entity_id', 'target_entity_id'], unique=False)

    op.create_table('knowledge_communities',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('acl_scope', sa.Text(), nullable=True),
        sa.Column('entity_count', sa.Integer(), nullable=True),
        sa.Column('card_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table('community_members',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('community_id', sa.String(length=36), nullable=False),
        sa.Column('entity_id', sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(['community_id'], ['knowledge_communities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['entity_id'], ['canonical_entities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('community_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_community_members_community_id'), ['community_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_community_members_entity_id'), ['entity_id'], unique=False)
        batch_op.create_index('ix_cm_community_entity', ['community_id', 'entity_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('community_members', schema=None) as batch_op:
        batch_op.drop_index('ix_cm_community_entity')
        batch_op.drop_index(batch_op.f('ix_community_members_entity_id'))
        batch_op.drop_index(batch_op.f('ix_community_members_community_id'))
    op.drop_table('community_members')

    op.drop_table('knowledge_communities')

    with op.batch_alter_table('card_graph_relations', schema=None) as batch_op:
        batch_op.drop_index('ix_cgr_source_target')
        batch_op.drop_index(batch_op.f('ix_card_graph_relations_status'))
        batch_op.drop_index(batch_op.f('ix_card_graph_relations_card_id'))
        batch_op.drop_index(batch_op.f('ix_card_graph_relations_relation_type'))
        batch_op.drop_index(batch_op.f('ix_card_graph_relations_target_entity_id'))
        batch_op.drop_index(batch_op.f('ix_card_graph_relations_source_entity_id'))
    op.drop_table('card_graph_relations')

    with op.batch_alter_table('card_entity_links', schema=None) as batch_op:
        batch_op.drop_index('ix_cel_card_entity')
        batch_op.drop_index(batch_op.f('ix_card_entity_links_entity_id'))
        batch_op.drop_index(batch_op.f('ix_card_entity_links_card_id'))
    op.drop_table('card_entity_links')

    with op.batch_alter_table('entity_aliases', schema=None) as batch_op:
        batch_op.drop_index('ix_ea_canonical_alias')
        batch_op.drop_index(batch_op.f('ix_entity_aliases_alias'))
        batch_op.drop_index(batch_op.f('ix_entity_aliases_canonical_entity_id'))
    op.drop_table('entity_aliases')

    with op.batch_alter_table('canonical_entities', schema=None) as batch_op:
        batch_op.drop_index('ix_ce_type_normalized')
        batch_op.drop_index(batch_op.f('ix_canonical_entities_disambiguation_status'))
        batch_op.drop_index(batch_op.f('ix_canonical_entities_normalized'))
        batch_op.drop_index(batch_op.f('ix_canonical_entities_entity_type'))
    op.drop_table('canonical_entities')
