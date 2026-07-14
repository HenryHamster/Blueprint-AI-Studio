"""add_project_tool

Create the ``projecttool`` table backing the per-project custom UI tools
framework. Each row is a tool/panel (e.g. a whiteboard/canvas) registered
against exactly one project; the admin-ui conditionally renders registered
tools in the owning project's workspace only. A unique constraint on
``(project_id, slug)`` enforces one tool per slug per project.

Revision ID: 0474
Revises: 0473
Create Date: 2026-07-10 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0474"
down_revision = "0473"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projecttool",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "placement",
            sa.String(),
            nullable=False,
            server_default="project_section",
        ),
        sa.Column("config", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("content", JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "content_revision", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "slug", name="uq_project_tool_project_slug"
        ),
    )
    op.create_index(
        "ix_projecttool_project_id",
        "projecttool",
        ["project_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_projecttool_project_id", table_name="projecttool")
    op.drop_table("projecttool")
