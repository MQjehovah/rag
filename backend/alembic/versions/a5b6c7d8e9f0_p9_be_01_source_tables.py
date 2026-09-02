"""P9_BE_01 source tables

Revision ID: a5b6c7d8e9f0
Revises: e4f5a6b7c8d9
Create Date: 2026-08-19 15:00:00.000000

P9（V3 计划 4.5）数据源四表：
- source_connections / source_items / source_sync_runs / source_sync_errors
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a5b6c7d8e9f0'
down_revision: Union[str, Sequence[str], None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('source_connections',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('connector_key', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=True),
        sa.Column('config_json', sa.Text(), nullable=True),
        sa.Column('secret_ref', sa.String(length=255), nullable=True),
        sa.Column('target_notebook_id', sa.String(length=36), nullable=True),
        sa.Column('default_acl_json', sa.Text(), nullable=True),
        sa.Column('cursor_json', sa.Text(), nullable=True),
        sa.Column('last_success_at', sa.DateTime(), nullable=True),
        sa.Column('last_error_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('source_connections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_source_connections_connector_key'), ['connector_key'], unique=False)
        batch_op.create_index('ux_source_connection_key_name', ['connector_key', 'name'], unique=True)

    op.create_table('source_items',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('connection_id', sa.String(length=36), nullable=False),
        sa.Column('external_id', sa.String(length=512), nullable=False),
        sa.Column('external_version', sa.String(length=255), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column('metadata_hash', sa.String(length=64), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('source_path', sa.Text(), nullable=True),
        sa.Column('page_id', sa.String(length=36), nullable=True),
        sa.Column('state', sa.String(length=32), nullable=True),
        sa.Column('acl_json', sa.Text(), nullable=True),
        sa.Column('source_updated_at', sa.DateTime(), nullable=True),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['connection_id'], ['source_connections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('source_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_source_items_connection_id'), ['connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_source_items_state'), ['state'], unique=False)
        batch_op.create_index('ux_source_item_conn_external', ['connection_id', 'external_id'], unique=True)

    op.create_table('source_sync_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('connection_id', sa.String(length=36), nullable=False),
        sa.Column('mode', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('cursor_before_json', sa.Text(), nullable=True),
        sa.Column('cursor_after_json', sa.Text(), nullable=True),
        sa.Column('discovered_count', sa.Integer(), nullable=True),
        sa.Column('created_count', sa.Integer(), nullable=True),
        sa.Column('updated_count', sa.Integer(), nullable=True),
        sa.Column('unchanged_count', sa.Integer(), nullable=True),
        sa.Column('deleted_count', sa.Integer(), nullable=True),
        sa.Column('failed_count', sa.Integer(), nullable=True),
        sa.Column('cancel_requested', sa.Boolean(), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('error_summary', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['connection_id'], ['source_connections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('source_sync_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_source_sync_runs_connection_id'), ['connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_source_sync_runs_status'), ['status'], unique=False)

    op.create_table('source_sync_errors',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('external_id', sa.String(length=512), nullable=True),
        sa.Column('stage', sa.String(length=32), nullable=True),
        sa.Column('error_code', sa.String(length=64), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('retryable', sa.Boolean(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['source_sync_runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('source_sync_errors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_source_sync_errors_run_id'), ['run_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('source_sync_errors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_source_sync_errors_run_id'))
    op.drop_table('source_sync_errors')

    with op.batch_alter_table('source_sync_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_source_sync_runs_status'))
        batch_op.drop_index(batch_op.f('ix_source_sync_runs_connection_id'))
    op.drop_table('source_sync_runs')

    with op.batch_alter_table('source_items', schema=None) as batch_op:
        batch_op.drop_index('ux_source_item_conn_external')
        batch_op.drop_index(batch_op.f('ix_source_items_state'))
        batch_op.drop_index(batch_op.f('ix_source_items_connection_id'))
    op.drop_table('source_items')

    with op.batch_alter_table('source_connections', schema=None) as batch_op:
        batch_op.drop_index('ux_source_connection_key_name')
        batch_op.drop_index(batch_op.f('ix_source_connections_connector_key'))
    op.drop_table('source_connections')
