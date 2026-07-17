"""Add (status, completed_at) index on computejob for viewable-media retire sweep.

Revision ID: 0635
Revises: 0628
Create Date: 2026-07-17

The reaper sweep ``sweep_retire_compute_job_viewable_files`` joins
``computejobfile`` to ``computejob`` filtering on ``status`` IN terminal values
AND ``completed_at < horizon``.  The existing ``ix_computejob_status_deadline``
covers ``(status, wall_clock_deadline)``; a dedicated index on
``(status, completed_at)`` lets the planner short-circuit the horizon scan and
avoids a seq-scan on ``computejob`` for long-running deployments where most
jobs are terminal.
"""

from __future__ import annotations

from alembic import op


revision = "0635"
down_revision = "0628"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_computejob_status_completed_at",
        "computejob",
        ["status", "completed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_computejob_status_completed_at", table_name="computejob")
