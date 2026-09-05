"""P44：WikiSection API Reference 结构字段 + WikiSectionEvidenceBinding（Phase 7C）。

Revision ID: a9b8c7d6e5f4
Revises: d3e4f5a6b7c8 (P43)
Create Date: Phase 7C.1

为 wiki_sections 增加 6 个 nullable 兼容字段（历史 Section 保持 NULL，不虚假回填）：
- section_key        String(255)  API Reference section 稳定 key
- skill_key          String(64)   生成本 Section 的 Skill key
- skill_version      String(64)   生成本 Section 的 Skill 版本
- content_hash       String(64)   Section 内容 hash
- validation_status  String(32)   NULL/pass/fail（CHECK）
- structure_json     Text         只保存 JSON-safe 结构（不存 Prompt/ACL/Secret/正文）

约束：
- ck_wiki_sections_validation_status：NULL 或 ('pass','fail')
- 部分唯一索引 ux_wiki_sections_revision_section_key：(revision_id, section_key)
  WHERE section_key IS NOT NULL

新增 wiki_section_evidence_bindings：
- id / section_id(FK wiki_sections.id CASCADE) / evidence_id(FK evidence_items.id
  CASCADE) / field_path(非空) / usage_type(support|conflict CHECK) /
  evidence_content_hash / created_at
- 唯一索引 ux_wiki_section_evidence_section_field_evidence_usage
  (section_id, field_path, evidence_id, usage_type)
- ix_..._section_id / ix_..._evidence_id 两个普通索引

不新增 WikiRevision→CompileRun 外键：KnowledgeCompileRun.output_revision_id 与
Artifact 链已表达关系，避免重复事实来源。

SQLite 使用 batch_alter_table（本迁移同时兼容 PostgreSQL）。
"""
from __future__ import annotations

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

from migration_compat import (
    ensure_checks_sqlite,
    ensure_columns_sqlite,
    ensure_index,
)


revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SECTION_COLUMNS = [
    sa.Column("section_key", sa.String(255), nullable=True),
    sa.Column("skill_key", sa.String(64), nullable=True),
    sa.Column("skill_version", sa.String(64), nullable=True),
    sa.Column("content_hash", sa.String(64), nullable=True),
    sa.Column("validation_status", sa.String(32), nullable=True),
    sa.Column("structure_json", sa.Text(), nullable=True),
]

_BINDING_TABLE = "wiki_section_evidence_bindings"


def _create_binding_indexes() -> None:
    op.create_index(
        "ix_wiki_section_evidence_bindings_section_id",
        _BINDING_TABLE, ["section_id"], unique=False,
    )
    op.create_index(
        "ix_wiki_section_evidence_bindings_evidence_id",
        _BINDING_TABLE, ["evidence_id"], unique=False,
    )
    op.create_index(
        "ux_wiki_section_evidence_section_field_evidence_usage",
        _BINDING_TABLE,
        ["section_id", "field_path", "evidence_id", "usage_type"],
        unique=True,
    )


def _drop_binding_indexes() -> None:
    op.drop_index("ux_wiki_section_evidence_section_field_evidence_usage",
                  table_name=_BINDING_TABLE)
    op.drop_index("ix_wiki_section_evidence_bindings_evidence_id",
                  table_name=_BINDING_TABLE)
    op.drop_index("ix_wiki_section_evidence_bindings_section_id",
                  table_name=_BINDING_TABLE)


def _create_section_partial_unique() -> None:
    ensure_index(
        op, op.get_bind(), "wiki_sections",
        "ux_wiki_sections_revision_section_key",
        ["revision_id", "section_key"],
        unique=True,
        sqlite_where=sa.text("section_key IS NOT NULL"),
        postgresql_where=sa.text("section_key IS NOT NULL"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    checks = [
        (
            "ck_wiki_sections_validation_status",
            "validation_status IS NULL OR validation_status IN ('pass','fail')",
        ),
        (
            "ck_wiki_sections_section_key_nonempty",
            "section_key IS NULL OR length(trim(section_key)) > 0",
        ),
    ]
    if dialect == "sqlite":
        # 兼容：部分列/约束/部分唯一索引可能已物理存在 → 补齐缺失结构；不等价受控失败。
        ensure_columns_sqlite(op, bind, "wiki_sections", _SECTION_COLUMNS)
        ensure_checks_sqlite(op, bind, "wiki_sections", checks)
        _create_section_partial_unique()
    else:
        for column in _SECTION_COLUMNS:
            op.add_column("wiki_sections", column)
        op.create_check_constraint(
            "ck_wiki_sections_validation_status",
            "wiki_sections",
            "validation_status IS NULL OR validation_status IN ('pass','fail')",
        )
        op.create_check_constraint(
            "ck_wiki_sections_section_key_nonempty",
            "wiki_sections",
            "section_key IS NULL OR length(trim(section_key)) > 0",
        )
        _create_section_partial_unique()

    op.create_table(
        _BINDING_TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "section_id",
            sa.String(36),
            sa.ForeignKey("wiki_sections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            sa.String(36),
            sa.ForeignKey("evidence_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_path", sa.String(255), nullable=False),
        sa.Column("usage_type", sa.String(32), nullable=False),
        sa.Column("evidence_content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "usage_type IN ('support','conflict')",
            name="ck_wiki_section_evidence_bindings_usage_type",
        ),
        sa.CheckConstraint(
            "length(trim(field_path)) > 0",
            name="ck_wiki_section_evidence_bindings_field_path_nonempty",
        ),
        sa.CheckConstraint(
            "length(evidence_content_hash) = 64",
            name="ck_wiki_section_evidence_bindings_evidence_hash_len",
        ),
    )
    _create_binding_indexes()


def downgrade() -> None:
    _drop_binding_indexes()
    op.drop_table(_BINDING_TABLE)

    bind = op.get_bind()
    dialect = bind.dialect.name
    op.drop_index("ux_wiki_sections_revision_section_key",
                  table_name="wiki_sections")
    if dialect == "sqlite":
        with op.batch_alter_table("wiki_sections") as batch_op:
            batch_op.drop_constraint(
                "ck_wiki_sections_validation_status", type_="check"
            )
            batch_op.drop_constraint(
                "ck_wiki_sections_section_key_nonempty", type_="check"
            )
            for column in reversed(_SECTION_COLUMNS):
                batch_op.drop_column(column.name)
    else:
        op.drop_constraint(
            "ck_wiki_sections_validation_status", "wiki_sections", type_="check"
        )
        op.drop_constraint(
            "ck_wiki_sections_section_key_nonempty", "wiki_sections", type_="check"
        )
        for column in reversed(_SECTION_COLUMNS):
            op.drop_column("wiki_sections", column.name)
