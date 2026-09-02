"""P2_BE_12 compile reports

Revision ID: e8b3c4d5f6a7
Revises: d7a1f9c3b2e4
Create Date: 2026-08-19 10:30:00.000000

新增 card_compile_reports 表：每次编译保存报告和失败原因。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8b3c4d5f6a7'
down_revision: Union[str, Sequence[str], None] = 'd7a1f9c3b2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('card_compile_reports',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('page_id', sa.String(length=36), nullable=True),
        sa.Column('card_id', sa.String(length=36), nullable=True),
        sa.Column('report_json', sa.Text(), nullable=True),
        sa.Column('failures_json', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('card_compile_reports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_card_compile_reports_page_id'), ['page_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_card_compile_reports_card_id'), ['card_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_card_compile_reports_status'), ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('card_compile_reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_card_compile_reports_status'))
        batch_op.drop_index(batch_op.f('ix_card_compile_reports_card_id'))
        batch_op.drop_index(batch_op.f('ix_card_compile_reports_page_id'))
    op.drop_table('card_compile_reports')
