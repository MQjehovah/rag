"""P45：WikiSkill 演化存储表（阶段 2，skill_evolution 不可变指令版本 + 绑定）。

Revision ID: b5c6d7e8f9a0
Revises: a9b8c7d6e5f4 (P44)
Create Date: 2026-09-07

新增两表（模型定义见 app/models/evolution.py，与 MANAGED_EVOLUTION_TABLES 一致）：

1. evolution_skill_versions —— 不可变技能指令版本：
   - version_id（PK）、skill_id、seq（每 skill 单调递增，排序不依赖字符串大小）、
     schema_version、domain、runtime_ref（兼容 Runtime 标识）、parent_version_id
     （自引用 FK）、skill_md / purpose_md（SKILL.md 与 PURPOSE.md 全文）、
     content_hash、source_type（builtin_seed/manual_seed，未来扩 evolved/proposed）、
     created_by、created_at；
   - 唯一约束 ux_evolution_skill_seq(skill_id, seq)；
   - CHECK：seq>=1；source_type 白名单；schema_version 白名单；正文非空。
   不提供原地修改路径：新内容 = 新 seq 新行（不可变由应用层保证，无 UPDATE API）。

2. evolution_skill_bindings —— Workspace+domain 生效绑定：
   - kind（experiment/business，业务绑定默认不启用）、workspace_id、domain、
     set_kind（skill=精确版本 / empty=显式空技能集合，论文无技能基线）、
     skill_id / version_id（FK evolution_skill_versions RESTRICT，空集合时两者为 NULL）、
     created_by / created_at / updated_at；
   - 唯一 ux_evolution_binding_scope(kind, workspace_id, domain)；
   - CHECK：set_kind 与 skill_id/version_id 成对（skill 必有、empty 必空）。
   无绑定行 = 功能关闭（旧编译行为保持）。

FK 全部在 create_table 内联声明（SQLite 不支持 ALTER 加约束；本迁移同时兼容 PG）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VERSION_TABLE = "evolution_skill_versions"
_BINDING_TABLE = "evolution_skill_bindings"


def upgrade() -> None:
    op.create_table(
        _VERSION_TABLE,
        sa.Column("version_id", sa.String(length=64), primary_key=True),
        sa.Column("skill_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("runtime_ref", sa.String(length=64), nullable=False),
        sa.Column("parent_version_id", sa.String(length=64),
                 sa.ForeignKey("evolution_skill_versions.version_id",
                               name="fk_evolution_skill_parent"),
                 nullable=True),
        sa.Column("skill_md", sa.Text(), nullable=False),
        sa.Column("purpose_md", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("skill_id", "seq", name="ux_evolution_skill_seq"),
        sa.CheckConstraint("seq >= 1", name="ck_evolution_skill_seq_positive"),
        sa.CheckConstraint(
            "source_type IN ('builtin_seed', 'manual_seed')",
            name="ck_evolution_skill_source_type",
        ),
        sa.CheckConstraint(
            "schema_version IN ('skill-evolution/v1')",
            name="ck_evolution_skill_schema_version",
        ),
        sa.CheckConstraint(
            "length(trim(skill_md)) > 0 AND length(trim(purpose_md)) > 0",
            name="ck_evolution_skill_content_nonempty",
        ),
    )
    op.create_index("ix_evolution_skill_versions_skill_id", _VERSION_TABLE, ["skill_id"])
    op.create_index("ix_evolution_skill_versions_content_hash",
                    _VERSION_TABLE, ["content_hash"])

    op.create_table(
        _BINDING_TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("set_kind", sa.String(length=16), nullable=False),
        sa.Column("skill_id", sa.String(length=64), nullable=True),
        sa.Column("version_id", sa.String(length=64),
                 sa.ForeignKey("evolution_skill_versions.version_id",
                               ondelete="RESTRICT",
                               name="fk_evolution_binding_version"),
                 nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("kind", "workspace_id", "domain",
                            name="ux_evolution_binding_scope"),
        sa.CheckConstraint("kind IN ('experiment', 'business')",
                           name="ck_evolution_binding_kind"),
        sa.CheckConstraint("set_kind IN ('skill', 'empty')",
                           name="ck_evolution_binding_set_kind"),
        sa.CheckConstraint(
            "(set_kind = 'skill' AND skill_id IS NOT NULL AND version_id IS NOT NULL) OR "
            "(set_kind = 'empty' AND skill_id IS NULL AND version_id IS NULL)",
            name="ck_evolution_binding_pair",
        ),
    )


def downgrade() -> None:
    op.drop_table(_BINDING_TABLE)
    op.drop_table(_VERSION_TABLE)
