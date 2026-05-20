"""Add releaseversion table for automain LTS release lifecycle tracking.

Stores the phase (build | stabilize) and status (open | promoted | abandoned)
of each release version so the strategic agent can drive build→stabilize
transitions and the qualifier (Phase 3) can gate automain→main promotion.

Only one ``status="open"`` row per project at a time.  version_number is
monotonically increasing per project (1-based).

Seeds a version-1 row for the self-repo project so the strategic agent
has a live row to work with immediately after the migration runs.

Revision ID: 0100
Revises: 0099
Create Date: 2026-05-19 00:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "releaseversion",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column(
            "version_number",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "phase",
            sa.String(length=32),
            nullable=False,
            server_default="build",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("phase_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        # SHA columns are nullable per migration linter rules.
        sa.Column("base_sha", sa.String(length=40), nullable=True),
        sa.Column("promoted_sha", sa.String(length=40), nullable=True),
        sa.Column("last_qualification", JSONB(), nullable=True),
        sa.Column(
            "qualification_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("phase_reasoning", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "version_number",
            name="uq_releaseversion_project_version",
        ),
    )
    op.create_index(
        op.f("ix_releaseversion_project_id"),
        "releaseversion",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_releaseversion_project_status",
        "releaseversion",
        ["project_id", "status"],
        unique=False,
    )

    # Seed a version-1 row for the self-repo project.  The self-repo project is
    # identified by ``is_self_repo = true``; on a fresh install with no self-repo
    # project this is a no-op.
    op.execute(
        """
        INSERT INTO releaseversion (
            id,
            project_id,
            version_number,
            phase,
            status,
            opened_at,
            qualification_attempts,
            created_at,
            updated_at
        )
        SELECT
            gen_random_uuid(),
            id,
            1,
            'build',
            'open',
            NOW(),
            0,
            NOW(),
            NOW()
        FROM project
        WHERE is_self_repo = true
        LIMIT 1
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_releaseversion_project_status",
        table_name="releaseversion",
    )
    op.drop_index(
        op.f("ix_releaseversion_project_id"),
        table_name="releaseversion",
    )
    op.drop_table("releaseversion")
