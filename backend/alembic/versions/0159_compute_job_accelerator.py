"""Add single-GPU accelerator columns to ``computejob``.

Adds two durable, nullable columns to ``computejob`` so a compute job can record
the single-GPU accelerator it was provisioned with (mirror app.models.ComputeJob):
``accelerator_type`` (canonical short name, e.g. "T4"/"L4"/"V100") and
``accelerator_count`` (defaults to 1 at create time when an accelerator is
requested). Both are NULL for CPU-only jobs, so existing rows and the CPU create
path are unchanged.

On SQLite (local test fixture) the columns are built from
``SQLModel.metadata.create_all``; this migration is the Postgres path. Per
CLAUDE.md it chains from the latest head (0158 — task format compliance summary) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0159 to match. Every ADD COLUMN is
idempotently guarded so a re-applied / back-merged revision still advances
``alembic_version`` instead of raising on a duplicate.

Revision ID: 0159
Revises: 0158
Create Date: 2026-06-07 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None


# Accelerator columns added to ``computejob`` (mirror app.models.ComputeJob).
# Names only — the ADD COLUMNs below are spelled out inline (not generated from a
# factory) so the schema-sync invariant (test_model_columns_covered_by_migrations)
# can statically detect each ``add_column("computejob", sa.Column("<name>", ...))``.
_COMPUTEJOB_NEW_COLUMN_NAMES = (
    "accelerator_type",
    "accelerator_count",
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Each ADD COLUMN is guarded on the live column set so a re-applied /
    # back-merged revision is a no-op rather than raising on a duplicate.
    if insp.has_table("computejob"):
        existing = {c["name"] for c in insp.get_columns("computejob")}
        if "accelerator_type" not in existing:
            op.add_column(
                "computejob",
                sa.Column("accelerator_type", sa.String(length=64), nullable=True),
            )
        if "accelerator_count" not in existing:
            op.add_column(
                "computejob",
                sa.Column("accelerator_count", sa.Integer(), nullable=True),
            )


def downgrade() -> None:
    for name in reversed(_COMPUTEJOB_NEW_COLUMN_NAMES):
        op.drop_column("computejob", name)
