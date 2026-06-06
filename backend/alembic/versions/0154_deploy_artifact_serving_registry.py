"""Add the per-artifact deploy serving registry table.

Foundation for tracking serving confirmation per deploy artifact instead of
overloading a single scalar pair on ``releaseversion``. This adds one additive,
nullable table:

``releasedeployartifact``
- ``version_id``              — FK to ``releaseversion.id`` (CASCADE delete)
- ``artifact_kind``           — provider key, e.g. ``backend_revision``
- ``serving_status``          — ``unconfirmed`` | ``serving_confirmed`` | ``deploy_failed``
- ``served_identifier``       — observed served SHA / fingerprint
- ``confirmation_deadline``   — bounded deadline for this artifact
- ``reason_code``             — provider failure code
- ``served_at``               — when serving was confirmed
- ``created_at`` / ``updated_at`` — additive timestamps

``alembic revision --autogenerate`` was used to derive this revision, but the
raw output also emitted many unrelated legacy type / FK / dropped-table diffs
that do not belong in this task. Those false positives were removed so the
migration stays purely additive and chain-safe.

Revision ID: 0154
Revises: 0153
Create Date: 2026-06-06 00:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa


revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "releasedeployartifact",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("version_id", sqlmodel.sql.sqltypes.GUID(), nullable=True),
        sa.Column("artifact_kind", sa.String(length=64), nullable=True),
        sa.Column("serving_status", sa.String(length=32), nullable=True),
        sa.Column("served_identifier", sa.String(length=128), nullable=True),
        sa.Column("confirmation_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(length=128), nullable=True),
        sa.Column("served_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["releaseversion.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "version_id",
            "artifact_kind",
            name="uq_releasedeployartifact_version_kind",
        ),
    )
    op.create_index(
        "ix_releasedeployartifact_version_id",
        "releasedeployartifact",
        ["version_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_releasedeployartifact_version_id", table_name="releasedeployartifact"
    )
    op.drop_table("releasedeployartifact")
