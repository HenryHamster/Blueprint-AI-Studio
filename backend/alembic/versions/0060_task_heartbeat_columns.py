"""Promote heartbeat-hot fields from Task.context JSONB to dedicated columns.

Audit 2026-04-29 (DB comprehensive remediation, Phase B2 / finding C3):
``/api/agent/heartbeat`` rewrites the entire ``Task.context`` JSONB blob
every 30 seconds per VM. At 200 VMs that is 6.6 full-blob writes/sec;
because PostgreSQL stores JSONB out-of-line in TOAST when above the page
threshold, every heartbeat re-allocates new TOAST pages even though the
mutation only touches a handful of subkeys. This is the single largest
write-amplification path on the hot side of the workload.

Mitigation: promote the two fields the heartbeat handler rewrites every
cycle into dedicated columns the storage layer can update in place
(no TOAST page churn, much smaller WAL records):

  - ``last_metrics_json`` (JSONB, nullable): the recent
    ``HeartbeatMetrics`` payload (rss_mb / heap / disk / cgroup snapshot).
  - ``last_tool_use_at`` (DateTime, nullable): timestamp of the most
    recent agent-side tool invocation, derived from the tail of the
    ``tool_trace`` ring buffer the agent ships with each heartbeat.

The audit-trail-ish fields (``metrics_history`` ring buffer and
``_tool_trace`` last-known-good window) keep the JSONB write — they're
only persisted every Nth heartbeat (N=5) by the handler, dropping the
JSONB write rate by 80%.

Dual-write transition: the handler also continues writing the same
fields back into ``context`` JSONB so unmigrated readers (forensics,
admin views) still see the values. The JSONB mirror writes are removed
in a Phase F follow-up once all readers have been migrated.

Backfill: existing rows with ``context.last_metrics`` / ``context
.last_tool_use_at`` are copied into the new columns so the dashboard
(and any forensic snapshot viewer) doesn't see a regression hole on the
first deploy after the migration.

Revision ID: 0058
Revises: 0057
Create Date: 2026-04-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '0058'
down_revision = '0057'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Use JSONB on Postgres (matches existing context/payloads columns).
    # SQLite tests fall through to sa.JSON which the dialect maps to TEXT.
    metrics_type = JSONB() if bind.dialect.name == 'postgresql' else sa.JSON()

    op.add_column(
        'task',
        sa.Column('last_metrics_json', metrics_type, nullable=True),
    )
    op.add_column(
        'task',
        sa.Column('last_tool_use_at', sa.DateTime(), nullable=True),
    )

    # Backfill from existing context. Postgres-only — the JSONB extract
    # operators don't exist on SQLite, and the test path always starts
    # from a fresh schema with no historical context to migrate.
    if bind.dialect.name == 'postgresql':
        op.execute("""
            UPDATE task
            SET last_metrics_json = context -> 'last_metrics'
            WHERE jsonb_typeof(context) = 'object'
              AND context ? 'last_metrics'
        """)
        op.execute("""
            UPDATE task
            SET last_tool_use_at = (context ->> 'last_tool_use_at')::timestamp
            WHERE jsonb_typeof(context) = 'object'
              AND context ? 'last_tool_use_at'
        """)


def downgrade() -> None:
    op.drop_column('task', 'last_tool_use_at')
    op.drop_column('task', 'last_metrics_json')
