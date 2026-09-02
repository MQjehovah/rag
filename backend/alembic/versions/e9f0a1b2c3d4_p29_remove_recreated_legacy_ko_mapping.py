"""P29：删除被 init_db 重新创建的 legacy_ko_mappings 表。

P28 已删除 legacy_ko_mappings，但 ORM 曾保留 LegacyKoMapping 模型，
init_db() 执行 Base.metadata.create_all() 会把它重新创建（当前真实库出现空表）。
本迁移幂等删除该表：存在才删，不存在直接跳过。

注意：LegacyKoMapping 已从正式 Base.metadata 移除（见 database.py），
后续 init_db() 不会再创建此表。正式运行代码不得再依赖此表。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 幂等：legacy_ko_mappings 存在才删除（可能是 P28 后 init_db 重建的空表）
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("legacy_ko_mappings"):
        op.drop_table("legacy_ko_mappings")


def downgrade() -> None:
    # 不重建：该表已从正式运行态移除，回滚不恢复（历史迁移审计由独立 legacy metadata 承担）
    pass
