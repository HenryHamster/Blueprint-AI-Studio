"""Add fix_loop_history JSONB column to Task.

Per-task fix-loop telemetry foundation. Stores per-loop attempt arrays
(lint, test, check) so the orchestrator and admin UI can measure
convergence rate and divergence patterns across the agent's fix loops.

Shape stored at runtime:
    {"lint":  [FixLoopAttempt, ...],
     "test":  [FixLoopAttempt, ...],
     "check": [FixLoopAttempt, ...]}

Validated at the ingest boundary by ``app.fix_loop_telemetry.FixLoopEventIn``;
the column itself is loose JSONB so newer agents can ship additional
diagnostic fields without a migration.

Revision ID: 0072
Revises: 0071
Create Date: 2026-05-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Use JSONB on Postgres, JSON on SQLite (matches pattern from migration 0060).
    # SQLite test path relies on sa.JSON() which maps to TEXT at the dialect level.
    column_type = postgresql.JSONB() if bind.dialect.name == 'postgresql' else sa.JSON()

    op.add_column(
        "task",
        sa.Column(
            "fix_loop_history",
            column_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb") if bind.dialect.name == 'postgresql' else sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("task", "fix_loop_history")
