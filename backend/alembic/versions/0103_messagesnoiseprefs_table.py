"""Add messagesnoiseprefs table for per-operator cross-device noise-pref persistence.

Stores the operator's messages-panel noise-control preferences (per-event-type
visibility choices and saved filter views) per user per project so settings
follow an operator across devices. localStorage remains the offline fallback
when the backend is unreachable.

Revision ID: 0103
Revises: 0102
Create Date: 2026-05-21 00:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "messagesnoiseprefs",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("prefs", JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "project_id", name="uq_messagesnoiseprefs_user_project"
        ),
    )
    op.create_index(
        op.f("ix_messagesnoiseprefs_user_id"),
        "messagesnoiseprefs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_messagesnoiseprefs_project_id"),
        "messagesnoiseprefs",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_messagesnoiseprefs_project_id"), table_name="messagesnoiseprefs"
    )
    op.drop_index(
        op.f("ix_messagesnoiseprefs_user_id"), table_name="messagesnoiseprefs"
    )
    op.drop_table("messagesnoiseprefs")
