"""Add partial index on ImprovementLane (status, parked_at) for stale-parked sweep.

The stale-parked-lane sweep in ``warm_pool_maintainer.py`` queries::

    SELECT ... FROM improvementlane
    WHERE status = 'PARKED'
      AND parked_at < :cutoff

This is a full-table scan on every sweep cycle.  The new partial index
``ix_improvementlane_status_parked_at`` covers only PARKED rows so the
sweep can seek directly to expired lanes.

Revision ID: 0610
Revises: 0609
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0610"
down_revision = "0609"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_improvementlane_status_parked_at"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            "ON improvementlane (status, parked_at) "
            "WHERE status = 'PARKED'"
        )
    else:
        op.create_index(
            _INDEX_NAME,
            "improvementlane",
            ["status", "parked_at"],
            postgresql_where=sa.text("status = 'PARKED'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    else:
        op.drop_index(
            _INDEX_NAME,
            table_name="improvementlane",
            postgresql_where=sa.text("status = 'PARKED'"),
        )
