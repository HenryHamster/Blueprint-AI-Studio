"""Promote hot task.context fields to first-class columns

Audit 2026-04-25 found that several lifecycle flags living in JSONB
``task.context`` cause idempotency races — the only way to read fresh
values is a full row re-fetch, but mutation paths use stale in-memory
copies. Promotes the audit-cited flags to columns so reads bypass
JSONB and SELECT FOR UPDATE locks the same row that mutations see.

Also adds optimistic-concurrency ``version`` to deploymentstate (audit
F4: inconsistent FOR UPDATE semantics across mutations).

Backfills from the existing JSONB context, then strips the now-promoted
keys to prevent dual-source drift (no backward compat — Blueprint has
no external users).

Revision ID: 0045
Revises: 0044
Create Date: 2026-04-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0045'
down_revision = '0044'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- task lifecycle columns ---
    op.add_column(
        'task',
        sa.Column('post_commit_pending', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'task',
        sa.Column('post_commit_failed', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'task',
        sa.Column('requeue_in_flight', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('task', sa.Column('infra_wait_until', sa.DateTime(timezone=False), nullable=True))
    op.add_column('task', sa.Column('retry_after_until', sa.DateTime(timezone=False), nullable=True))
    op.add_column('task', sa.Column('previous_error_fingerprint', sa.Text(), nullable=True))
    op.add_column(
        'task',
        sa.Column('auto_remediation', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('task', sa.Column('strategic_cycle_id', sa.String(length=36), nullable=True))
    op.add_column(
        'task',
        sa.Column('spawn_failed', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('task', sa.Column('last_remediation_at', sa.DateTime(timezone=False), nullable=True))
    op.add_column('task', sa.Column('last_token_refresh_at', sa.DateTime(timezone=False), nullable=True))

    # --- partial indexes for hot query paths ---
    # auto_remediation cap query (auto_remediation.py)
    op.create_index(
        'ix_task_auto_remediation',
        'task',
        ['auto_remediation'],
        postgresql_where=sa.text('auto_remediation IS TRUE'),
    )
    # requeue_in_flight dedup (reaper.py)
    op.create_index(
        'ix_task_requeue_in_flight',
        'task',
        ['requeue_in_flight'],
        postgresql_where=sa.text('requeue_in_flight IS TRUE'),
    )
    # strategic_cycle lifecycle hook (task_lifecycle.py)
    op.create_index(
        'ix_task_strategic_cycle_id',
        'task',
        ['strategic_cycle_id'],
        postgresql_where=sa.text('strategic_cycle_id IS NOT NULL'),
    )
    # fingerprint cap query (auto_remediation.py)
    op.create_index(
        'ix_task_previous_error_fingerprint',
        'task',
        ['previous_error_fingerprint'],
        postgresql_where=sa.text('previous_error_fingerprint IS NOT NULL'),
    )
    # post_commit_pending guard (agent_status.py)
    op.create_index(
        'ix_task_post_commit_pending',
        'task',
        ['post_commit_pending'],
        postgresql_where=sa.text('post_commit_pending IS TRUE'),
    )

    # --- backfill from existing context JSONB ---
    # Booleans first. Some prod rows store these keys as objects (e.g.
    # auto_remediation = {"at": "..."}) instead of bools — guard the cast
    # with jsonb_typeof so non-boolean shapes degrade to FALSE rather than
    # crashing the whole migration on InvalidTextRepresentationError.
    op.execute("""
        UPDATE task SET
          post_commit_pending = CASE
            WHEN jsonb_typeof(context->'_post_commit_pending') = 'boolean'
              THEN (context->>'_post_commit_pending')::boolean
            ELSE FALSE
          END,
          requeue_in_flight = CASE
            WHEN jsonb_typeof(context->'requeue_in_flight') = 'boolean'
              THEN (context->>'requeue_in_flight')::boolean
            ELSE FALSE
          END,
          auto_remediation = CASE
            WHEN jsonb_typeof(context->'auto_remediation') = 'boolean'
              THEN (context->>'auto_remediation')::boolean
            ELSE FALSE
          END,
          spawn_failed = CASE
            WHEN jsonb_typeof(context->'_spawn_failed') = 'boolean'
              THEN (context->>'_spawn_failed')::boolean
            ELSE FALSE
          END,
          previous_error_fingerprint = CASE
            WHEN jsonb_typeof(context->'_previous_error_fingerprint') = 'string'
              THEN context->>'_previous_error_fingerprint'
            ELSE NULL
          END,
          strategic_cycle_id = CASE
            WHEN jsonb_typeof(context->'strategic_cycle_id') = 'string'
              THEN context->>'strategic_cycle_id'
            ELSE NULL
          END
        WHERE context IS NOT NULL;
    """)
    # Timestamp fields parsed best-effort; leave NULL on parse fail. Match
    # ISO 8601 with optional timezone — strip 'Z' and timezone offset since
    # the new columns are timezone-naive (matches DB convention; F8).
    op.execute(r"""
        UPDATE task SET retry_after_until = (
            SUBSTRING(context->>'retry_after' FROM '^[0-9T:\-\.]+')
        )::timestamp
        WHERE context ? 'retry_after'
          AND context->>'retry_after' ~ '^\d{4}-\d{2}-\d{2}';
    """)
    op.execute(r"""
        UPDATE task SET infra_wait_until = (
            SUBSTRING(context->>'infra_wait_until' FROM '^[0-9T:\-\.]+')
        )::timestamp
        WHERE context ? 'infra_wait_until'
          AND context->>'infra_wait_until' ~ '^\d{4}-\d{2}-\d{2}';
    """)
    op.execute(r"""
        UPDATE task SET last_remediation_at = (
            SUBSTRING(context->>'last_remediation_at' FROM '^[0-9T:\-\.]+')
        )::timestamp
        WHERE context ? 'last_remediation_at'
          AND context->>'last_remediation_at' ~ '^\d{4}-\d{2}-\d{2}';
    """)

    # --- strip promoted keys from context (no dual-source) ---
    op.execute("""
        UPDATE task SET context = context
          - '_post_commit_pending'
          - 'requeue_in_flight'
          - 'auto_remediation'
          - '_spawn_failed'
          - '_previous_error_fingerprint'
          - 'strategic_cycle_id'
          - 'retry_after'
          - 'infra_wait_until'
          - 'last_remediation_at'
        WHERE context IS NOT NULL;
    """)

    # --- deploymentstate optimistic-concurrency version ---
    op.add_column(
        'deploymentstate',
        sa.Column('version', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )


def downgrade() -> None:
    op.drop_column('deploymentstate', 'version')
    op.drop_index('ix_task_post_commit_pending', table_name='task')
    op.drop_index('ix_task_previous_error_fingerprint', table_name='task')
    op.drop_index('ix_task_strategic_cycle_id', table_name='task')
    op.drop_index('ix_task_requeue_in_flight', table_name='task')
    op.drop_index('ix_task_auto_remediation', table_name='task')
    for col in (
        'last_token_refresh_at',
        'last_remediation_at',
        'spawn_failed',
        'strategic_cycle_id',
        'auto_remediation',
        'previous_error_fingerprint',
        'retry_after_until',
        'infra_wait_until',
        'requeue_in_flight',
        'post_commit_failed',
        'post_commit_pending',
    ):
        op.drop_column('task', col)
