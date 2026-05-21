"""Add build_stall_alerted_at to releaseversion.

Stores the build-phase START reference time (phase_changed_at, falling back to
opened_at) that was active when a build-stall alert was last posted for this
version. Used by the build-stall sweep to deduplicate alerts: a new alert is
only posted when the current build-phase reference time differs from this field
(or the field is NULL, meaning no alert has been posted yet). Because a version
can move build -> stabilize -> build again (each transition re-stamps
phase_changed_at), storing the reference time lets the dedup reset naturally
when a fresh build phase begins while never re-firing for the same stall.
Storing dedup state here avoids relying solely on Alert row existence, which
can be lost when an operator clears alerts.

Nullable, no server default: the column is write-on-alert only.

Revision ID: 0108
Revises: 0107
Create Date: 2026-05-21 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "releaseversion",
        sa.Column(
            "build_stall_alerted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("releaseversion", "build_stall_alerted_at")
