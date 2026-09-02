"""P42：KnowledgeCompileRun 持久任务（Phase 4）。

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6 (P41)
Create Date: Phase 4

新增三张表（DB queued 行即持久队列，重启不丢）：
- knowledge_compile_runs：Wiki 编译 run（pipeline/trigger/状态/幂等键/心跳/attempt）。
- knowledge_compile_stage_runs：run 内阶段执行记录（attempt 递增，parent_stage_run_id
  指向上一 attempt 的行，保留历史错误）。
- knowledge_compile_artifacts：编译产物摘要（只存元数据 + payload_json 内部 JSON，
  API 永不返回 payload_json 原文）。

FK/ondelete：
- knowledge_compile_runs.source_sync_run_id → source_sync_runs.id ON DELETE SET NULL
- knowledge_compile_runs.workspace_id → wiki_workspaces.id ON DELETE SET NULL
- knowledge_compile_stage_runs.run_id → knowledge_compile_runs.id ON DELETE CASCADE
- knowledge_compile_artifacts.run_id → knowledge_compile_runs.id ON DELETE CASCADE
- knowledge_compile_artifacts.stage_run_id → knowledge_compile_stage_runs.id ON DELETE CASCADE
- knowledge_compile_artifacts.reused_from_artifact_id → knowledge_compile_artifacts.id
  ON DELETE SET NULL（自引用，Phase 4.2 缓存复用链）

CHECK 约束：
- knowledge_compile_runs.status IN (queued,running,succeeded,failed,cancelled,superseded)
  （ck_compile_run_status）
- knowledge_compile_runs.attempt >= 0（ck_compile_run_attempt_nonneg）、
  max_attempts >= 1（ck_compile_run_max_attempts_min）、attempt <= max_attempts
  （ck_compile_run_attempt_le_max）
- knowledge_compile_stage_runs.status IN (queued,running,succeeded,failed,skipped,
  cancelled)（ck_compile_stage_status）

索引与唯一约束：
- 普通索引：pipeline_key/source_sync_run_id/workspace_id/wiki_page_id/status（run）
  run_id/stage_key/status（stage）、run_id/stage_run_id/reused_from（artifact）。
- 唯一索引：ux_knowledge_compile_runs_idempotency_key(idempotency_key)（幂等去重）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_indexes_for_tables() -> None:
    op.create_index("ix_knowledge_compile_runs_pipeline_key", "knowledge_compile_runs", ["pipeline_key"])
    op.create_index("ix_knowledge_compile_runs_status", "knowledge_compile_runs", ["status"])
    op.create_index("ix_knowledge_compile_runs_source_sync_run_id", "knowledge_compile_runs", ["source_sync_run_id"])
    op.create_index("ix_knowledge_compile_runs_workspace_id", "knowledge_compile_runs", ["workspace_id"])
    op.create_index("ix_knowledge_compile_runs_wiki_page_id", "knowledge_compile_runs", ["wiki_page_id"])
    op.create_index("ix_knowledge_compile_runs_request_fingerprint", "knowledge_compile_runs", ["request_fingerprint"])
    op.create_index(
        "ux_knowledge_compile_runs_idempotency_key",
        "knowledge_compile_runs",
        ["idempotency_key"],
        unique=True,
    )

    op.create_index("ix_knowledge_compile_stage_runs_run_id", "knowledge_compile_stage_runs", ["run_id"])
    op.create_index("ix_knowledge_compile_stage_runs_stage_key", "knowledge_compile_stage_runs", ["stage_key"])
    op.create_index("ix_knowledge_compile_stage_runs_status", "knowledge_compile_stage_runs", ["status"])

    op.create_index("ix_knowledge_compile_artifacts_run_id", "knowledge_compile_artifacts", ["run_id"])
    op.create_index("ix_knowledge_compile_artifacts_stage_run_id", "knowledge_compile_artifacts", ["stage_run_id"])
    op.create_index("ix_knowledge_compile_artifacts_reused_from", "knowledge_compile_artifacts", ["reused_from_artifact_id"])


def _drop_indexes_for_tables() -> None:
    op.drop_index("ix_knowledge_compile_runs_pipeline_key", table_name="knowledge_compile_runs")
    op.drop_index("ix_knowledge_compile_runs_status", table_name="knowledge_compile_runs")
    op.drop_index("ix_knowledge_compile_runs_source_sync_run_id", table_name="knowledge_compile_runs")
    op.drop_index("ix_knowledge_compile_runs_workspace_id", table_name="knowledge_compile_runs")
    op.drop_index("ix_knowledge_compile_runs_wiki_page_id", table_name="knowledge_compile_runs")
    op.drop_index("ix_knowledge_compile_runs_request_fingerprint", table_name="knowledge_compile_runs")
    op.drop_index("ux_knowledge_compile_runs_idempotency_key", table_name="knowledge_compile_runs")

    op.drop_index("ix_knowledge_compile_stage_runs_run_id", table_name="knowledge_compile_stage_runs")
    op.drop_index("ix_knowledge_compile_stage_runs_stage_key", table_name="knowledge_compile_stage_runs")
    op.drop_index("ix_knowledge_compile_stage_runs_status", table_name="knowledge_compile_stage_runs")

    op.drop_index("ix_knowledge_compile_artifacts_run_id", table_name="knowledge_compile_artifacts")
    op.drop_index("ix_knowledge_compile_artifacts_stage_run_id", table_name="knowledge_compile_artifacts")
    op.drop_index("ix_knowledge_compile_artifacts_reused_from", table_name="knowledge_compile_artifacts")


def upgrade() -> None:
    op.create_table(
        "knowledge_compile_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pipeline_key", sa.String(64), nullable=False),
        sa.Column("pipeline_version", sa.String(64), nullable=False),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("trigger_object_id", sa.String(36), nullable=True),
        sa.Column("source_sync_run_id", sa.String(36), sa.ForeignKey("source_sync_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("wiki_workspaces.id", ondelete="SET NULL"), nullable=True),
        sa.Column("wiki_page_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output_revision_id", sa.String(36), nullable=True),
        sa.Column("current_stage", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        # Phase 4.1：原子领取 lease + safe 错误分层。
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("safe_error_code", sa.String(64), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        # Phase 4.1：请求幂等指纹（idempotency_key 冲突判定，非空则唯一键指纹对比）。
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=True, server_default=sa.sql.expression.false()),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'superseded')",
            name="ck_compile_run_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_compile_run_attempt_nonneg"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_compile_run_max_attempts_min"),
        sa.CheckConstraint("attempt <= max_attempts", name="ck_compile_run_attempt_le_max"),
    )
    op.create_table(
        "knowledge_compile_stage_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("knowledge_compile_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage_key", sa.String(64), nullable=False),
        sa.Column("stage_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("output_hash", sa.String(64), nullable=True),
        sa.Column("component_key", sa.String(64), nullable=True),
        sa.Column("component_version", sa.String(64), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True, server_default=sa.sql.expression.true()),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Phase 4.1：stage 输入哈希（缓存键）+ safe 错误分层。
        sa.Column("stage_input_hash", sa.String(64), nullable=True),
        sa.Column("safe_error_code", sa.String(64), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column("parent_stage_run_id", sa.String(36), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'skipped', 'cancelled')",
            name="ck_compile_stage_status",
        ),
    )
    op.create_table(
        "knowledge_compile_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("knowledge_compile_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage_run_id", sa.String(36), sa.ForeignKey("knowledge_compile_stage_runs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=True),
        sa.Column("object_type", sa.String(32), nullable=True),
        sa.Column("object_id", sa.String(36), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column(
            "reused_from_artifact_id",
            sa.String(36),
            sa.ForeignKey("knowledge_compile_artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    _create_indexes_for_tables()


def downgrade() -> None:
    _drop_indexes_for_tables()
    op.drop_table("knowledge_compile_artifacts")
    op.drop_table("knowledge_compile_stage_runs")
    op.drop_table("knowledge_compile_runs")
