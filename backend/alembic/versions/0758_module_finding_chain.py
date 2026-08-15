"""Persist project-scoped recurring review concerns."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0758"
down_revision = "0757"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "modulefindingchain",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("finding_key", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "verified_occurrences", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("pr_numbers", JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("first_seen_pr", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_seen_pr", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column(
            "file_provenance", sa.String(length=16), nullable=False, server_default="unverified"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "finding_key", name="uq_modulefindingchain_project_key"),
    )
    op.create_index(
        "ix_modulefindingchain_project_id", "modulefindingchain", ["project_id"], unique=False
    )
    op.create_index(
        "ix_modulefindingchain_project_file",
        "modulefindingchain",
        ["project_id", "file_path"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_modulefindingchain_project_file", table_name="modulefindingchain")
    op.drop_index("ix_modulefindingchain_project_id", table_name="modulefindingchain")
    op.drop_table("modulefindingchain")
