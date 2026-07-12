"""Add (project_id, created_at) index on Alert for ORDER BY performance.

The ``ix_alert_project_created_at`` index is created by migration 0458
(``add_perf_indexes``), which was merged concurrently. This migration
is a structural head marker to keep the Alembic chain linear.

Revision ID: 0459
Revises: 0458
Create Date: 2026-07-10 00:00:00.000000

"""

revision = "0459"
down_revision = "0458b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ix_alert_project_created_at is created by migration 0458 (add_perf_indexes).
    # This migration is a structural head marker for the linear chain.
    pass


def downgrade() -> None:
    # ix_alert_project_created_at is owned by migration 0458; its downgrade
    # handles the drop. This migration is intentionally irreversible — it is
    # a structural head marker with no DDL of its own.
    raise NotImplementedError(
        "No DDL to undo: ix_alert_project_created_at is owned by "
        "migration 0458 (add_perf_indexes)"
    )
