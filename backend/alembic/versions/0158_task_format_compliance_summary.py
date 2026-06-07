"""Add Task ``format_compliance_summary`` column.

Adds one nullable JSONB column to the ``task`` table, sibling to
``api_call_summary`` (0095), holding cumulative per-task structured-output
format-compliance telemetry posted by the agent on every heartbeat. Shape:
``{"format_miss_by_reason": {<reason>: int, ...}, "format_error_total": int,
"format_repair_attempts": int, "format_repair_successes": int,
"last_updated_at": iso8601}``.

Backwards-compatible: older agents that don't ship the new heartbeat fields
leave this column NULL. Per CLAUDE.md the revision id (0158) is the reserved
id for this task; it chains from the latest head (0156) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0158 to match. The ADD COLUMN is
idempotently guarded so a re-applied / back-merged revision advances
``alembic_version`` instead of raising on a duplicate.

This migration adds no ``nullable=False`` columns, so no ``server_default``
backfill is required.

Revision ID: 0158
Revises: 0156
Create Date: 2026-06-07 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0158"
down_revision = "0156"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Guarded on the live column set so a re-applied / back-merged revision is a
    # no-op rather than raising on a duplicate column.
    if insp.has_table("task"):
        existing = {c["name"] for c in insp.get_columns("task")}
        if "format_compliance_summary" not in existing:
            op.add_column(
                "task",
                sa.Column("format_compliance_summary", JSONB(), nullable=True),
            )


def downgrade() -> None:
    op.drop_column("task", "format_compliance_summary")
