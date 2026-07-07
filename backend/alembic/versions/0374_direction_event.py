"""direction_event

Append-only event trail for direction lifecycle transitions (created,
status_changed, pruned).

Revision ID: 0374
Revises: 0368
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "0374"
down_revision = "0368"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "direction_event",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("direction_key", sa.String(length=128), nullable=False),
        sa.Column("event_kind", sa.String(length=32), nullable=False),
        sa.Column("prior_status", sa.String(length=32), nullable=True),
        sa.Column("new_status", sa.String(length=32), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("snapshot_data", JSONB(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_direction_event_project_key",
        "direction_event",
        ["project_id", "direction_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_direction_event_project_key", table_name="direction_event")
    op.drop_table("direction_event")
