"""P1_BE_08 evidence source_doc_hash

Revision ID: c4e8b2a1d5f3
Revises: 9e5ddaba4e2c
Create Date: 2026-08-18 17:45:00.000000

给 evidence_items 增加 source_doc_hash：Evidence 生成时来源文档（page）
的 content_hash 快照，供 P1-BE-08 判定「文档 Hash 变化 → 标记 stale」。

回填（现有 Evidence 的 source_doc_hash = 其 page 当前 content_hash）由
scripts/v3_evidence_stale.py 幂等完成，不在此迁移中执行——避免迁移内
依赖业务数据，保证 DDL 纯净可回滚。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e8b2a1d5f3'
down_revision: Union[str, Sequence[str], None] = '9e5ddaba4e2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'evidence_items',
        sa.Column('source_doc_hash', sa.String(length=64), nullable=True),
    )
    op.create_index(
        'ix_evidence_items_source_doc_hash',
        'evidence_items',
        ['source_doc_hash'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_evidence_items_source_doc_hash', table_name='evidence_items')
    with op.batch_alter_table('evidence_items', schema=None) as batch_op:
        batch_op.drop_column('source_doc_hash')
