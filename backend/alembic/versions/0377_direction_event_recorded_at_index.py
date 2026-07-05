"""direction_event_recorded_at_index

Add recorded_at as a third column to ix_direction_event_project_key so that
the common query (filter by project_id + direction_key, order by recorded_at
DESC) is fully index-covered and avoids a sort pass.

Revision ID: 0377
Revises: 0376
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0377"
down_revision = "0376"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_direction_event_project_key", table_name="direction_event")
    op.create_index(
        "ix_direction_event_project_key",
        "direction_event",
        ["project_id", "direction_key", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_direction_event_project_key", table_name="direction_event")
    op.create_index(
        "ix_direction_event_project_key",
        "direction_event",
        ["project_id", "direction_key"],
    )
