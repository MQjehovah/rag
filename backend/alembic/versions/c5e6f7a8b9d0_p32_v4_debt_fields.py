"""P32 V4 Phase F 知识债务字段（待执行迁移，不自动升级真实库）

Revision ID: c5e6f7a8b9d0
Revises: b8e9f0a1b2c3
Create Date: 2026-08-25

V4 Phase F：
- knowledge_debts 新增 nullable 字段：original_query / normalized_query /
  cluster_key / affected_user_count / scope_id / retrieval_reason。
- 新增 knowledge_debt_users 表（受影响用户去重，内部不对外）。

仅声明迁移，Phase F 禁止对真实 notes.db 执行 alembic upgrade；测试用内存库验证。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5e6f7a8b9d0'
down_revision: Union[str, Sequence[str], None] = 'b8e9f0a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('knowledge_debts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('original_query', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('normalized_query', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('cluster_key', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('affected_user_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('scope_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('retrieval_reason', sa.String(length=50), nullable=True))
        batch_op.create_index('ix_knowledge_debts_normalized_query', ['normalized_query'])
        batch_op.create_index('ux_knowledge_debts_cluster_key', ['cluster_key'], unique=True)
        batch_op.create_index('ix_knowledge_debts_scope_id', ['scope_id'])

    op.create_table(
        'knowledge_debt_users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('debt_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['debt_id'], ['knowledge_debts.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_knowledge_debt_users_debt_id', 'knowledge_debt_users', ['debt_id'])
    op.create_index('ix_knowledge_debt_users_user_id', 'knowledge_debt_users', ['user_id'])
    op.create_index('ux_kdu_debt_user', 'knowledge_debt_users', ['debt_id', 'user_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_kdu_debt_user', table_name='knowledge_debt_users')
    op.drop_index('ix_knowledge_debt_users_user_id', table_name='knowledge_debt_users')
    op.drop_index('ix_knowledge_debt_users_debt_id', table_name='knowledge_debt_users')
    op.drop_table('knowledge_debt_users')

    with op.batch_alter_table('knowledge_debts', schema=None) as batch_op:
        batch_op.drop_index('ix_knowledge_debts_scope_id')
        batch_op.drop_index('ux_knowledge_debts_cluster_key')
        batch_op.drop_index('ix_knowledge_debts_normalized_query')
        batch_op.drop_column('retrieval_reason')
        batch_op.drop_column('scope_id')
        batch_op.drop_column('affected_user_count')
        batch_op.drop_column('cluster_key')
        batch_op.drop_column('normalized_query')
        batch_op.drop_column('original_query')
