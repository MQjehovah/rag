"""P7_BE_01 legacy_ko_mapping

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-19 14:00:00.000000

P7（V3 计划 12.2）legacy_ko_mappings 表：旧 KO → 新 Card/Claim 映射。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('legacy_ko_mappings',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('legacy_ko_id', sa.String(length=36), nullable=False),
        sa.Column('new_card_id', sa.String(length=36), nullable=True),
        sa.Column('new_claim_id', sa.String(length=36), nullable=True),
        sa.Column('migration_status', sa.String(length=32), nullable=True),
        sa.Column('migration_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('legacy_ko_mappings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_legacy_ko_mappings_legacy_ko_id'), ['legacy_ko_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_legacy_ko_mappings_migration_status'), ['migration_status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('legacy_ko_mappings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_legacy_ko_mappings_migration_status'))
        batch_op.drop_index(batch_op.f('ix_legacy_ko_mappings_legacy_ko_id'))
    op.drop_table('legacy_ko_mappings')
