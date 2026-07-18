"""Persist Resident sweep position and index terminal-cycle lookup.

Revision ID: 0635
Revises: 0634
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0635"
down_revision = "0634"
branch_labels = None
depends_on = None

_INDEX = "ix_strategiccycle_project_terminal_updated"
_PREDICATE = (
    "outcome IN ('completed','failed_phase1','failed_phase2',"
    "'failed_phase2_spawn','stalled_phase1','stalled_phase2',"
    "'thinker_artifacts_missing')"
)
_SWEEP_NAME = "durable_project_change"


def upgrade() -> None:
    op.create_table(
        "strategicresidentsweepstate",
        sa.Column("sweep_name", sa.String(length=64), primary_key=True),
        sa.Column("after_project_id", sa.Uuid(), nullable=True),
        sa.Column(
            "version", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    state = sa.table(
        "strategicresidentsweepstate",
        sa.column("sweep_name", sa.String(length=64)),
        sa.column("version", sa.BigInteger()),
    )
    op.bulk_insert(state, [{"sweep_name": _SWEEP_NAME, "version": 0}])

    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                "ON strategiccycle (project_id, updated_at, id) "
                f"WHERE {_PREDICATE}"
            )
    else:
        op.create_index(
            _INDEX,
            "strategiccycle",
            ["project_id", "updated_at", "id"],
            sqlite_where=sa.text(_PREDICATE),
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    else:
        op.drop_index(_INDEX, table_name="strategiccycle")
    op.drop_table("strategicresidentsweepstate")
