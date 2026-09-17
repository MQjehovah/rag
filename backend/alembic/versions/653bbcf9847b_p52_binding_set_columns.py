"""P52: evolution_skill_bindings 增加完整集合语义列（B2 检查点）。

- set_hash:    规范集合哈希（成员 skill_id/version_id/content_hash/seq，固定顺序）
- rev:         绑定修订号（每次成功切换 +1；CAS 用修订+哈希，天然防 ABA）
- members_json: 完整成员列表 JSON（skill=多成员集合；空集合=[] 显式无绑定内容）
- 兼容：既有行 members_json 为 NULL、set_hash 为 NULL、rev 默认 1 —— 读路径把
  NULL 解释为“旧单技能绑定”（取 version_id/skill_id），写入/升级时再物化新列。
  空绑定在旧语义用 set_kind='empty'；新语义 members_json='[]' 显式表示。

Revision ID: 653bbcf9847b
Revises: d974815b4a91 (P51)
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "653bbcf9847b"
down_revision: Union[str, Sequence[str], None] = "d974815b4a91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evolution_skill_bindings",
                  sa.Column("rev", sa.Integer(), nullable=False,
                            server_default="1"))
    op.add_column("evolution_skill_bindings",
                  sa.Column("set_hash", sa.String(length=64),
                            nullable=True))
    op.add_column("evolution_skill_bindings",
                  sa.Column("members_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("evolution_skill_bindings", "members_json")
    op.drop_column("evolution_skill_bindings", "set_hash")
    op.drop_column("evolution_skill_bindings", "rev")
