"""P43：WikiPage Content Skill 持久化字段（Phase 6）。

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7 (P42)
Create Date: Phase 6

为 wiki_pages 增加 6 个 Content Skill 字段（全部 nullable，历史 Wiki 不自动回填）：
- content_skill       String(64)  当前内容结构 Skill key
- skill_version       String(64)  当前 Skill 精确版本
- skill_selected_by   String(32)  auto/manual/migration/default_fallback/locked/sticky
- skill_confidence    Float       NULL 或 0<=v<=1（CHECK ck_wiki_pages_skill_confidence）
- skill_locked        Boolean     NULL/False/True（人工锁定 Skill）
- skill_decision_json Text        安全结构化 SkillDecision 摘要

CHECK 约束（与 ORM WikiPage.__table_args__ 一致）：
- ck_wiki_pages_skill_confidence：skill_confidence IS NULL OR (>=0.0 AND <=1.0)
- ck_wiki_pages_skill_selected_by：skill_selected_by IS NULL OR IN (...6 值...)

SQLite 使用 batch_alter_table（本迁移同时兼容 PostgreSQL）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from migration_compat import ensure_checks_sqlite, ensure_columns_sqlite


revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SKILL_SELECTED_BY_VALUES = (
    "auto", "manual", "migration", "default_fallback", "locked", "sticky",
)

_SKILL_COLUMNS = [
    sa.Column("content_skill", sa.String(64), nullable=True),
    sa.Column("skill_version", sa.String(64), nullable=True),
    sa.Column("skill_selected_by", sa.String(32), nullable=True),
    sa.Column("skill_confidence", sa.Float(), nullable=True),
    sa.Column(
        "skill_locked",
        sa.Boolean(),
        nullable=True,
        server_default=sa.sql.expression.false(),
    ),
    sa.Column("skill_decision_json", sa.Text(), nullable=True),
]

_SELECTED_BY_SQL = ", ".join(f"'{v}'" for v in _SKILL_SELECTED_BY_VALUES)


def _selected_by_condition() -> str:
    return (
        "skill_selected_by IS NULL OR skill_selected_by IN "
        f"({_SELECTED_BY_SQL})"
    )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    checks = [
        (
            "ck_wiki_pages_skill_confidence",
            "skill_confidence IS NULL OR "
            "(skill_confidence >= 0.0 AND skill_confidence <= 1.0)",
        ),
        ("ck_wiki_pages_skill_selected_by", _selected_by_condition()),
    ]
    if dialect == "sqlite":
        # 兼容：部分列可能已物理存在且等价 → 保留并只补缺失列/缺失 CHECK；不等价受控失败。
        ensure_columns_sqlite(op, bind, "wiki_pages", _SKILL_COLUMNS)
        ensure_checks_sqlite(op, bind, "wiki_pages", checks)
    else:
        for column in _SKILL_COLUMNS:
            op.add_column("wiki_pages", column)
        op.create_check_constraint(
            "ck_wiki_pages_skill_confidence",
            "wiki_pages",
            "skill_confidence IS NULL OR "
            "(skill_confidence >= 0.0 AND skill_confidence <= 1.0)",
        )
        op.create_check_constraint(
            "ck_wiki_pages_skill_selected_by",
            "wiki_pages",
            _selected_by_condition(),
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        # SQLite batch 重建表：先删 CHECK（重建时不复制被删列的约束），再删列。
        with op.batch_alter_table("wiki_pages") as batch_op:
            batch_op.drop_constraint(
                "ck_wiki_pages_skill_confidence", type_="check"
            )
            batch_op.drop_constraint(
                "ck_wiki_pages_skill_selected_by", type_="check"
            )
            for column in reversed(_SKILL_COLUMNS):
                batch_op.drop_column(column.name)
    else:
        op.drop_constraint(
            "ck_wiki_pages_skill_confidence", "wiki_pages", type_="check"
        )
        op.drop_constraint(
            "ck_wiki_pages_skill_selected_by", "wiki_pages", type_="check"
        )
        for column in reversed(_SKILL_COLUMNS):
            op.drop_column("wiki_pages", column.name)
