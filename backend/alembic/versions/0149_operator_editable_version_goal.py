"""Add operator-editable version goal tracking.

The ``ReleaseVersion`` table gained two columns to support operator-editable goals:
- ``goal_updated_at``: timestamp of the last goal edit by an operator
- ``goal_editor_info``: provenance info ("[operator:email]" format) for who edited it

Both columns are nullable so pre-existing rows backfill naturally (no goal editor info).
This allows operators to set/refine the version's goal from the lifecycle surface,
providing intentional-control over the version's objective beyond auto-derived goals.

Per CLAUDE.md this chains from the latest head (0148) and ``EXPECTED_SCHEMA_REVISION``
is bumped to 0149 to match. Purely additive and reversible.

Revision ID: 0149
Revises: 0148
Create Date: 2026-06-05 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "goal_updated_at",
            sa.DateTime(),
            nullable=True,
        ),
    )
    op.add_column(
        "releaseversion",
        sa.Column(
            "goal_editor_info",
            sa.String(length=255),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "goal_editor_info")
    op.drop_column("releaseversion", "goal_updated_at")
