"""Add AuditFindingResolution ledger table.

Records per-(release_version, finding_key) resolution state (resolved | stale)
for carried qualification audit findings, surviving last_qualification finalize
overwrites.  Supports the re-qualification readiness signal.

Revision ID: 0260
Revises: 0238
"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa


# Reserved revision id for this task (linearized against production at merge
# time).  Chained from the actual current head 0238 to keep the chain a single
# linear sequence (the reservation note named 0235, but 0236-0238 merged after
# the reservation was issued — chaining from 0235 would branch the chain).
revision = "0260"
down_revision = "0238"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auditfindingresolution",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("release_version_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("finding_key", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resolving_ref", sa.String(length=512), nullable=True),
        sa.Column("verified_against_sha", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["release_version_id"],
            ["releaseversion.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_version_id",
            "finding_key",
            name="uq_auditfindingresolution_version_key",
        ),
    )
    op.create_index(
        op.f("ix_auditfindingresolution_project_id"),
        "auditfindingresolution",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auditfindingresolution_release_version_id"),
        "auditfindingresolution",
        ["release_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auditfindingresolution_finding_key"),
        "auditfindingresolution",
        ["finding_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_auditfindingresolution_finding_key"),
        table_name="auditfindingresolution",
    )
    op.drop_index(
        op.f("ix_auditfindingresolution_release_version_id"),
        table_name="auditfindingresolution",
    )
    op.drop_index(
        op.f("ix_auditfindingresolution_project_id"),
        table_name="auditfindingresolution",
    )
    op.drop_table("auditfindingresolution")
