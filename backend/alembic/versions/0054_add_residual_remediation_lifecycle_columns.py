"""Add residual remediation lifecycle columns to residualremediationcase.

Builds on 0051 (which created the table with passive tracking fields) to
add the active-lifecycle fields needed for symmetric notifications and
auto-suspension after repeated failures, mirroring the CI auto-remediation
pattern in notification_dispatch.post_auto_fix_*_message.

Adds:
  - last_spawned_task_id (FK task.id ON DELETE SET NULL, nullable):
      most recent residual remediation task this fingerprint spawned;
      enables the spawn-dedup check to read from the table instead of the
      in-memory _residual_remediation_cooldowns dict that gets wiped on
      every orchestrator restart.
  - last_spawned_pr_number (Integer, nullable): the PR opened by the
      remediation task, surfaced in admin UI and merge webhooks.
  - last_spawned_at (DateTime, nullable): clock used by the cooldown check.
  - consecutive_failure_count (Integer NOT NULL default 0): increments on
      each failed remediation; resets to 0 on merge.
  - suspended_at (DateTime, nullable): set when consecutive_failure_count
      crosses _AUTO_REMEDIATION_ESCALATION_THRESHOLD; suppresses further
      auto-spawn until manually cleared.
  - sample_messages, sample_statuses, task_ids (JSONB arrays NOT NULL
      default '[]'): replace the single sample_message field with capped
      rolling lists, matching how _residual_store accumulated samples
      pre-persistence.

Status CHECK constraint additions (REMEDIATING/SUSPENDED/MERGED/FAILED)
extend the existing enum (ACTIVE/IGNORED/CLOSED_ORGANICALLY/RESOLVED)
without rewriting any existing rows.

UNIQUE (project_id, fingerprint) dedupes-before-applying so existing rows
with duplicate (project_id, fingerprint) collapse into the most-recently-
seen row with summed hit_count. NULL project_id rows are treated as
distinct under Postgres UNIQUE semantics, matching how the orphan-task
path writes them today.

Revision ID: 0054
Revises: 0053
Create Date: 2026-04-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = '0054'
down_revision = '0053'
branch_labels = None
depends_on = None


_VALID_STATUSES = (
    'ACTIVE', 'IGNORED', 'CLOSED_ORGANICALLY', 'RESOLVED',
    'REMEDIATING', 'SUSPENDED', 'MERGED', 'FAILED',
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Add lifecycle columns. server_default keeps the schema applicable
    #    to existing rows without a Python-side backfill loop.
    op.add_column(
        'residualremediationcase',
        sa.Column('last_spawned_task_id', UUID(as_uuid=True),
                  sa.ForeignKey('task.id', ondelete='SET NULL'), nullable=True),
    )
    op.add_column(
        'residualremediationcase',
        sa.Column('last_spawned_pr_number', sa.Integer(), nullable=True),
    )
    op.add_column(
        'residualremediationcase',
        sa.Column('last_spawned_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'residualremediationcase',
        sa.Column('consecutive_failure_count', sa.Integer(),
                  nullable=False, server_default='0'),
    )
    op.add_column(
        'residualremediationcase',
        sa.Column('suspended_at', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'residualremediationcase',
        sa.Column('sample_messages', JSONB(),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        'residualremediationcase',
        sa.Column('sample_statuses', JSONB(),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column(
        'residualremediationcase',
        sa.Column('task_ids', JSONB(),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
    )

    # 2. Dedupe duplicate (project_id, fingerprint) rows before applying
    #    the UNIQUE. Keep the row with the latest last_seen_at; sum
    #    hit_count from all duplicates onto the keeper. Postgres-only
    #    DELETE USING with a window function. SQLite test path is covered
    #    by an upstream guard in test-backend.sh that runs on Postgres.
    bind.execute(sa.text("""
        WITH ranked AS (
            SELECT id,
                   project_id,
                   fingerprint,
                   hit_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY project_id, fingerprint
                       ORDER BY last_seen_at DESC, created_at DESC, id
                   ) AS rn,
                   SUM(hit_count) OVER (
                       PARTITION BY project_id, fingerprint
                   ) AS total_hits
            FROM residualremediationcase
        ),
        keepers AS (
            SELECT id, total_hits FROM ranked WHERE rn = 1
        )
        UPDATE residualremediationcase r
        SET hit_count = k.total_hits
        FROM keepers k
        WHERE r.id = k.id AND r.hit_count <> k.total_hits
    """))
    bind.execute(sa.text("""
        DELETE FROM residualremediationcase
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY project_id, fingerprint
                           ORDER BY last_seen_at DESC, created_at DESC, id
                       ) AS rn
                FROM residualremediationcase
            ) ranked
            WHERE rn > 1
        )
    """))

    # 3. UNIQUE constraint on (project_id, fingerprint). Postgres treats
    #    NULL project_id rows as distinct, so orphan-task entries don't
    #    collide with each other.
    op.create_unique_constraint(
        'ux_residualremediationcase_project_fingerprint',
        'residualremediationcase',
        ['project_id', 'fingerprint'],
    )

    # 4. Replace the implicit any-string column with an explicit CHECK
    #    that gates the new lifecycle states. Existing rows already use
    #    a subset of these values.
    op.create_check_constraint(
        'ck_residualremediationcase_status',
        'residualremediationcase',
        "status IN (" + ", ".join(f"'{s}'" for s in _VALID_STATUSES) + ")",
    )

    # 5. Compound index for the spawn-dedup lookup
    #    (project_id, status, last_spawned_at)
    op.create_index(
        'ix_residualremediationcase_project_status_spawned',
        'residualremediationcase',
        ['project_id', 'status', 'last_spawned_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_residualremediationcase_project_status_spawned',
        table_name='residualremediationcase',
    )
    op.drop_constraint(
        'ck_residualremediationcase_status',
        'residualremediationcase',
        type_='check',
    )
    op.drop_constraint(
        'ux_residualremediationcase_project_fingerprint',
        'residualremediationcase',
        type_='unique',
    )
    op.drop_column('residualremediationcase', 'task_ids')
    op.drop_column('residualremediationcase', 'sample_statuses')
    op.drop_column('residualremediationcase', 'sample_messages')
    op.drop_column('residualremediationcase', 'suspended_at')
    op.drop_column('residualremediationcase', 'consecutive_failure_count')
    op.drop_column('residualremediationcase', 'last_spawned_at')
    op.drop_column('residualremediationcase', 'last_spawned_pr_number')
    op.drop_column('residualremediationcase', 'last_spawned_task_id')
