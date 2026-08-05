"""Add the general service-credential (machine token) table.

Revision ID: 0723
Revises: 0722
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0723"
down_revision = "0722"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "servicecredential",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("principal_user_id", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_path", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "length(name) > 0",
            name="chk_servicecredential_name_nonempty",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_servicecredential_name"),
        sa.UniqueConstraint("token_hash", name="uq_servicecredential_token_hash"),
    )
    op.create_index(
        "ix_servicecredential_principal_user_id",
        "servicecredential",
        ["principal_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_servicecredential_principal_user_id",
        table_name="servicecredential",
    )
    op.drop_table("servicecredential")
