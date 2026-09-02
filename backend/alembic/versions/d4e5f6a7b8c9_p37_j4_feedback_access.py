"""P37 J-4：回答反馈、知识缺口与访问申请。

Revision ID: d4e5f6a7b8c9
Revises: b2c4d5e6f7a8 (P36 V4 图谱表)
Create Date: Phase J-4

新增表：
- answer_snapshots：回答快照（answer_id 持久化 + 产生时的权限快照）。
- answer_feedback：回答反馈（同一 answer 同一 user 唯一；feedback_cluster_key 精确关联 cluster）。
- feedback_clusters：负面反馈聚类（阈值达后只触发一次分类）。
- feedback_cluster_users：聚类去重用户。
- answer_needed：「我仍需要这个答案」幂等去重（answer_id 唯一）。
- access_requests：访问申请（cluster_key 唯一）。
- access_request_users：访问申请受影响用户去重。

KnowledgeDebt 增加必要 nullable 字段（不重建债务系统）：
- version_label / version_status / debt_reason。

不修改任何历史 migration。downgrade 删除新表并回退 KnowledgeDebt 新字段。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "b2c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "answer_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("original_query", sa.Text(), nullable=False),
        sa.Column("normalized_query", sa.String(255), nullable=False),
        sa.Column("version_label", sa.String(64), nullable=True),
        sa.Column("version_status", sa.String(32), nullable=True),
        sa.Column("response_mode", sa.String(32), nullable=True),
        sa.Column("retrieval_completed", sa.Boolean(), nullable=True),
        sa.Column("answer_eligible", sa.Boolean(), nullable=True),
        sa.Column("service_degraded", sa.Boolean(), nullable=True),
        sa.Column("has_visible_sufficient_evidence", sa.Boolean(), nullable=True),
        sa.Column("business_groups", sa.Text(), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_answer_snapshots_user_id", "answer_snapshots", ["user_id"])
    op.create_index("ix_answer_snapshots_normalized_query", "answer_snapshots", ["normalized_query"])

    # feedback_clusters 必须先于 answer_feedback 创建（answer_feedback 引用其 cluster_key）。
    op.create_table(
        "feedback_clusters",
        sa.Column("cluster_key", sa.String(255), primary_key=True),
        sa.Column("normalized_query", sa.String(255), nullable=False),
        sa.Column("version_label", sa.String(64), nullable=True),
        sa.Column("version_status", sa.String(32), nullable=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("classification", sa.String(32), nullable=True),
        sa.Column("classified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_feedback_clusters_normalized_query", "feedback_clusters", ["normalized_query"])

    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("answer_id", sa.String(36), sa.ForeignKey("answer_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("feedback_cluster_key", sa.String(255), sa.ForeignKey("feedback_clusters.cluster_key", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_answer_feedback_answer_id", "answer_feedback", ["answer_id"])
    op.create_index("ix_answer_feedback_user_id", "answer_feedback", ["user_id"])
    op.create_index("ix_answer_feedback_feedback_cluster_key", "answer_feedback", ["feedback_cluster_key"])
    op.create_index(
        "ux_answer_feedback_answer_user",
        "answer_feedback",
        ["answer_id", "user_id"],
        unique=True,
    )

    op.create_table(
        "feedback_cluster_users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_key", sa.String(255), sa.ForeignKey("feedback_clusters.cluster_key", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_feedback_cluster_users_cluster_key", "feedback_cluster_users", ["cluster_key"])
    op.create_index("ix_feedback_cluster_users_user_id", "feedback_cluster_users", ["user_id"])
    op.create_index(
        "ux_fcu_cluster_user",
        "feedback_cluster_users",
        ["cluster_key", "user_id"],
        unique=True,
    )

    op.create_table(
        "answer_needed",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("answer_id", sa.String(36), sa.ForeignKey("answer_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_answer_needed_answer_id", "answer_needed", ["answer_id"])
    op.create_index("ix_answer_needed_user_id", "answer_needed", ["user_id"])
    op.create_index("ux_answer_needed_answer", "answer_needed", ["answer_id"], unique=True)

    op.create_table(
        "access_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_key", sa.String(255), nullable=False),
        sa.Column("normalized_query", sa.String(255), nullable=False),
        sa.Column("original_query", sa.Text(), nullable=True),
        sa.Column("version_label", sa.String(64), nullable=True),
        sa.Column("version_status", sa.String(32), nullable=True),
        sa.Column("requesting_groups", sa.Text(), nullable=True),
        sa.Column("target_notebook_id", sa.String(36), nullable=True),
        sa.Column("target_wiki_page_id", sa.String(36), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("affected_user_count", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("status", sa.String(32), nullable=True, server_default="open"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_access_requests_normalized_query", "access_requests", ["normalized_query"])
    op.create_index("ix_access_requests_status", "access_requests", ["status"])
    op.create_index("ux_access_requests_cluster_key", "access_requests", ["cluster_key"], unique=True)

    op.create_table(
        "access_request_users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), sa.ForeignKey("access_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_access_request_users_request_id", "access_request_users", ["request_id"])
    op.create_index("ix_access_request_users_user_id", "access_request_users", ["user_id"])
    op.create_index(
        "ux_aru_request_user",
        "access_request_users",
        ["request_id", "user_id"],
        unique=True,
    )

    # KnowledgeDebt 增加必要 nullable 字段（不重建债务系统，不改历史数据）。
    op.add_column("knowledge_debts", sa.Column("version_label", sa.String(64), nullable=True))
    op.add_column("knowledge_debts", sa.Column("version_status", sa.String(32), nullable=True))
    op.add_column("knowledge_debts", sa.Column("debt_reason", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_debts", "debt_reason")
    op.drop_column("knowledge_debts", "version_status")
    op.drop_column("knowledge_debts", "version_label")

    op.drop_table("access_request_users")
    op.drop_table("access_requests")
    op.drop_table("answer_needed")
    op.drop_table("feedback_cluster_users")
    op.drop_table("answer_feedback")
    op.drop_table("feedback_clusters")
    op.drop_table("answer_snapshots")
