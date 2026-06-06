"""Add durable ``poolvm`` fleet image-identity columns.

Adds five durable image-identity columns to ``poolvm`` so the warm-pool fleet
can be reasoned about across orchestrator restarts (the in-memory heartbeat
caches are intentionally lossy): ``agent_image_name``, ``agent_image_build_ts``,
``agent_runtime_fingerprint``, ``agent_source_sha`` and ``last_heartbeat_at``.
These complement the pre-existing ``agent_image_fingerprint`` (0133) consumed by
``compute.record_vm_image`` / ``compute.get_vm_image``.

On SQLite (local test fixture) the columns are built from
``SQLModel.metadata.create_all``; this migration is the Postgres path. Per
CLAUDE.md it chains from the latest head (0155 — the migration reservation
table) and ``EXPECTED_SCHEMA_REVISION`` is bumped to 0156 to match. Every
ADD COLUMN is idempotently guarded so a re-applied / back-merged revision still
advances ``alembic_version`` instead of raising on a duplicate.

Revision ID: 0156
Revises: 0155
Create Date: 2026-06-06 13:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


# Durable image-identity columns added to ``poolvm`` (mirror app.models.PoolVm).
# Names only — the ADD COLUMNs below are spelled out inline (not generated from a
# factory) so the schema-sync invariant (test_model_columns_covered_by_migrations)
# can statically detect each ``add_column("poolvm", sa.Column("<name>", ...))``.
_POOLVM_NEW_COLUMN_NAMES = (
    "agent_image_name",
    "agent_image_build_ts",
    "agent_runtime_fingerprint",
    "agent_source_sha",
    "last_heartbeat_at",
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Each ADD COLUMN is guarded on the live column set so a re-applied /
    # back-merged revision is a no-op rather than raising on a duplicate.
    if insp.has_table("poolvm"):
        existing = {c["name"] for c in insp.get_columns("poolvm")}
        if "agent_image_name" not in existing:
            op.add_column("poolvm", sa.Column("agent_image_name", sa.String(), nullable=True))
        if "agent_image_build_ts" not in existing:
            op.add_column("poolvm", sa.Column("agent_image_build_ts", sa.Integer(), nullable=True))
        if "agent_runtime_fingerprint" not in existing:
            op.add_column(
                "poolvm",
                sa.Column("agent_runtime_fingerprint", sa.String(length=128), nullable=True),
            )
        if "agent_source_sha" not in existing:
            op.add_column("poolvm", sa.Column("agent_source_sha", sa.String(length=64), nullable=True))
        if "last_heartbeat_at" not in existing:
            op.add_column(
                "poolvm",
                sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    for name in reversed(_POOLVM_NEW_COLUMN_NAMES):
        op.drop_column("poolvm", name)
