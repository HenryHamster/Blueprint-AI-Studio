"""Add the warm-pool idle-cost carry-forward ledger (``poolidlecostledger``).

The idle reaper hard-deletes a ``poolvm`` row on teardown, which carries the
member's accrued ``idle_cost_banked``. Because reaping (≈1800s idle-TTL) is far
more frequent than efficiency snapshots (once per UTC day), nearly every pool
member is provisioned, accrues idle cost, and is deleted between two snapshots —
its spend captured nowhere, making ``cost_per_merged_pr_incl_pool_idle``
undercount. This append-only ledger durably carries that banked cost forward:
the reaper writes one row in the same transaction that deletes the registry row,
and ``efficiency_snapshot.compute_pool_idle_cost`` sums it into the settled
total so "teardown never erases measured idle spend" holds.

Per CLAUDE.md this migration is chained from the latest head (0133) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0134 to match. On SQLite (local test
fixture) the table is created from ``SQLModel.metadata.create_all``; this
migration is the Postgres path.

Revision ID: 0134
Revises: 0133
Create Date: 2026-06-01 12:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa


revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "poolidlecostledger",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("vm_instance_id", sa.String(), nullable=False),
        sa.Column("idle_cost_usd", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column(
            "retired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Single-column project index (mirrors the model's foreign_key index=True) so
    # the snapshot's project-scoped carry-forward sum stays cheap.
    op.create_index(
        "ix_poolidlecostledger_project_id",
        "poolidlecostledger",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_poolidlecostledger_project_id", table_name="poolidlecostledger"
    )
    op.drop_table("poolidlecostledger")
