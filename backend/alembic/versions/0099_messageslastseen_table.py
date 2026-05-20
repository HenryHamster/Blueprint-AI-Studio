"""Add messageslastseen table for per-operator cross-device divider persistence.

Stores the last-seen timestamp per user per project so the since-last-visit
divider in the messages panel follows an operator across devices. localStorage
remains the offline fallback when the backend is unreachable.

Revision ID: 0099
Revises: 0098
Create Date: 2026-05-09 00:00:00.000000

"""

import sqlmodel.sql.sqltypes
from alembic import op
import sqlalchemy as sa

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "messageslastseen",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "project_id", name="uq_messageslastseen_user_project"
        ),
    )
    op.create_index(
        op.f("ix_messageslastseen_user_id"),
        "messageslastseen",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_messageslastseen_project_id"),
        "messageslastseen",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_messageslastseen_project_id"), table_name="messageslastseen")
    op.drop_index(op.f("ix_messageslastseen_user_id"), table_name="messageslastseen")
    op.drop_table("messageslastseen")
