"""Add qualificationverdict table for per-verdict qualification-run history.

Stores one immutable row per finalized release-qualification verdict so the
dry-run track record is durably retained across multiple finalize cycles.
ReleaseVersion.last_qualification is overwritten on every finalize; this table
appends the full history so compute_promotion_readiness can aggregate it.

Keyed by release_version_id (FK to releaseversion) and project_id (FK to
project), both with CASCADE delete so rows are cleaned up when the parent is
removed.

Revision ID: 0107
Revises: 0106
Create Date: 2026-05-21 00:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qualificationverdict",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("release_version_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("candidate_sha", sa.String(length=40), nullable=True),
        sa.Column(
            "confidence",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("sub_scores", JSONB(), nullable=True),
        sa.Column("blocker", sa.String(length=64), nullable=True),
        sa.Column(
            "would_promote",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "promotion_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "terminal_status",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
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
    )
    op.create_index(
        op.f("ix_qualificationverdict_project_id"),
        "qualificationverdict",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_qualificationverdict_release_version_id"),
        "qualificationverdict",
        ["release_version_id"],
        unique=False,
    )
    # Composite index for efficient per-project recent-history queries.
    op.create_index(
        "ix_qualificationverdict_project_created",
        "qualificationverdict",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_qualificationverdict_project_created",
        table_name="qualificationverdict",
    )
    op.drop_index(
        op.f("ix_qualificationverdict_release_version_id"),
        table_name="qualificationverdict",
    )
    op.drop_index(
        op.f("ix_qualificationverdict_project_id"),
        table_name="qualificationverdict",
    )
    op.drop_table("qualificationverdict")
