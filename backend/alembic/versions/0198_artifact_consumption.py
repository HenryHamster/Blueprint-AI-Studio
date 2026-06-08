"""Add ``artifact.logical_key`` and ``artifactconsumption`` table.

Introduces two schema additions that together form the durable consumption seam
for downstream artifact evidence:

1. ``artifact.logical_key`` — nullable ``VARCHAR(256)`` with a plain btree index
   (``ix_artifact_logical_key``). Legacy rows and artifact types that do not
   participate in the consumption seam are left NULL.

2. ``artifactconsumption`` table — records that a consumer task claimed a
   specific artifact.  Columns:
   - ``id``               UUID primary key
   - ``artifact_id``      FK → ``artifact.id`` SET NULL on delete
   - ``consumer_task_id`` FK → ``task.id`` SET NULL on delete
   - ``producer_task_id`` FK → ``task.id`` SET NULL on delete (nullable)
   - ``logical_key``      ``VARCHAR(256)`` snapshot of the slug at claim time
   - ``created_at``       TIMESTAMPTZ server-defaulted to now()

   Indexes: ``ix_artifactconsumption_artifact_id``,
   ``ix_artifactconsumption_consumer_task_id``,
   ``ix_artifactconsumption_producer_task_id``.

   Unique constraint: ``uq_artifactconsumption_consumer_artifact`` on
   ``(consumer_task_id, artifact_id)`` prevents double-recording.

Both FK links use ``ON DELETE SET NULL`` (not CASCADE) so consumption history
survives bulk task cleanup, mirroring ``ComputeJob.task_id``.

Backwards-compatible: the new column is nullable, legacy rows are unaffected.
Per CLAUDE.md the revision id (0198) is the reserved id for this task; it
chains from the latest head (0159 — compute job accelerator) and
``EXPECTED_SCHEMA_REVISION`` is bumped to 0198 to match.

Revision ID: 0198
Revises: 0159
Create Date: 2026-06-07 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID


revision = "0198"
down_revision = "0159"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1. Add logical_key to artifact (idempotently guarded)
    if insp.has_table("artifact"):
        existing = {c["name"] for c in insp.get_columns("artifact")}
        if "logical_key" not in existing:
            op.add_column(
                "artifact",
                sa.Column("logical_key", sa.String(length=256), nullable=True),
            )
            op.create_index("ix_artifact_logical_key", "artifact", ["logical_key"])

    # 2. Create artifactconsumption table (idempotently guarded)
    if not insp.has_table("artifactconsumption"):
        op.create_table(
            "artifactconsumption",
            sa.Column("id", PGUUID(as_uuid=True), nullable=False),
            sa.Column("artifact_id", PGUUID(as_uuid=True), nullable=True),
            sa.Column("consumer_task_id", PGUUID(as_uuid=True), nullable=True),
            sa.Column("producer_task_id", PGUUID(as_uuid=True), nullable=True),
            sa.Column("logical_key", sa.String(length=256), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["artifact_id"], ["artifact.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["consumer_task_id"], ["task.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["producer_task_id"], ["task.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "consumer_task_id",
                "artifact_id",
                name="uq_artifactconsumption_consumer_artifact",
            ),
        )
        op.create_index(
            "ix_artifactconsumption_artifact_id",
            "artifactconsumption",
            ["artifact_id"],
        )
        op.create_index(
            "ix_artifactconsumption_consumer_task_id",
            "artifactconsumption",
            ["consumer_task_id"],
        )
        op.create_index(
            "ix_artifactconsumption_producer_task_id",
            "artifactconsumption",
            ["producer_task_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if insp.has_table("artifactconsumption"):
        op.drop_index(
            "ix_artifactconsumption_producer_task_id", table_name="artifactconsumption"
        )
        op.drop_index(
            "ix_artifactconsumption_consumer_task_id", table_name="artifactconsumption"
        )
        op.drop_index(
            "ix_artifactconsumption_artifact_id", table_name="artifactconsumption"
        )
        op.drop_table("artifactconsumption")

    if insp.has_table("artifact"):
        existing = {c["name"] for c in insp.get_columns("artifact")}
        if "logical_key" in existing:
            op.drop_index("ix_artifact_logical_key", table_name="artifact")
            op.drop_column("artifact", "logical_key")
