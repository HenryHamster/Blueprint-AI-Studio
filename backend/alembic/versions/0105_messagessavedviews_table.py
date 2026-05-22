"""Add messagessavedviews table for per-operator cross-device saved-views persistence.

Stores the operator's messages-panel saved views (named filter/sort combinations)
per user per project so they follow an operator across devices. localStorage remains
the offline fallback when the backend is unreachable.

Revision ID: 0105
Revises: 0104
Create Date: 2026-05-21 00:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "messagessavedviews",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("saved_views", JSONB(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "project_id", name="uq_messagessavedviews_user_project"
        ),
    )
    op.create_index(
        op.f("ix_messagessavedviews_user_id"),
        "messagessavedviews",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_messagessavedviews_project_id"),
        "messagessavedviews",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_messagessavedviews_project_id"), table_name="messagessavedviews"
    )
    op.drop_index(
        op.f("ix_messagessavedviews_user_id"), table_name="messagessavedviews"
    )
    op.drop_table("messagessavedviews")
