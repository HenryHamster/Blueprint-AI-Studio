"""Add goal to releaseversion.

The version's goal — a point-in-time snapshot of
ProblemModel.intent.current_focus captured when the version opened.  Frozen
for the version's lifetime so each version keeps a stable record of what it
was for, even after current_focus drifts on.

Nullable: pre-0111 rows and projects with no current_focus carry no goal.

Revision ID: 0111
Revises: 0110
Create Date: 2026-05-22 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column("goal", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "goal")
