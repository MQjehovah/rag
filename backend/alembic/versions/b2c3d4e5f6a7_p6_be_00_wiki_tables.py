"""P6_BE_00 wiki tables

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-19 13:00:00.000000

P6（V3 计划 11.2）Wiki 五表：
- wiki_pages / wiki_revisions / wiki_sections / wiki_citations / wiki_links
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('wiki_pages',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('community_id', sa.String(length=36), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('acl_scope', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('current_revision_id', sa.String(length=36), nullable=True),
        sa.Column('source_hash', sa.String(length=64), nullable=True),
        sa.Column('locked', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('wiki_pages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_wiki_pages_community_id'), ['community_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_wiki_pages_status'), ['status'], unique=False)

    op.create_table('wiki_revisions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('wiki_page_id', sa.String(length=36), nullable=False),
        sa.Column('parent_revision_id', sa.String(length=36), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('source_hash', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['wiki_page_id'], ['wiki_pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('wiki_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_wiki_revisions_wiki_page_id'), ['wiki_page_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_wiki_revisions_status'), ['status'], unique=False)

    op.create_table('wiki_sections',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('revision_id', sa.String(length=36), nullable=False),
        sa.Column('section_type', sa.String(length=50), nullable=False),
        sa.Column('heading', sa.String(length=255), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('order_index', sa.Integer(), nullable=True),
        sa.Column('locked', sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(['revision_id'], ['wiki_revisions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('wiki_sections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_wiki_sections_revision_id'), ['revision_id'], unique=False)

    op.create_table('wiki_citations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('section_id', sa.String(length=36), nullable=False),
        sa.Column('card_id', sa.String(length=36), nullable=True),
        sa.Column('evidence_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['section_id'], ['wiki_sections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('wiki_citations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_wiki_citations_section_id'), ['section_id'], unique=False)

    op.create_table('wiki_links',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('source_page_id', sa.String(length=36), nullable=False),
        sa.Column('target_page_id', sa.String(length=36), nullable=False),
        sa.Column('link_type', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['source_page_id'], ['wiki_pages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_page_id'], ['wiki_pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('wiki_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_wiki_links_source_page_id'), ['source_page_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_wiki_links_target_page_id'), ['target_page_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('wiki_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wiki_links_target_page_id'))
        batch_op.drop_index(batch_op.f('ix_wiki_links_source_page_id'))
    op.drop_table('wiki_links')

    with op.batch_alter_table('wiki_citations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wiki_citations_section_id'))
    op.drop_table('wiki_citations')

    with op.batch_alter_table('wiki_sections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wiki_sections_revision_id'))
    op.drop_table('wiki_sections')

    with op.batch_alter_table('wiki_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wiki_revisions_status'))
        batch_op.drop_index(batch_op.f('ix_wiki_revisions_wiki_page_id'))
    op.drop_table('wiki_revisions')

    with op.batch_alter_table('wiki_pages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wiki_pages_status'))
        batch_op.drop_index(batch_op.f('ix_wiki_pages_community_id'))
    op.drop_table('wiki_pages')
