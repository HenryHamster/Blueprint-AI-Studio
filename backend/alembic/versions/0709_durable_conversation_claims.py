"""Add durable, generation-fenced foreground conversation claims.

Revision ID: 0709
Revises: 0708
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0709"
down_revision = "0708"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projectmessage",
        sa.Column(
            "conversation_claim_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "projectmessage",
        sa.Column("conversation_claim_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "projectmessage",
        sa.Column(
            "conversation_claim_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "projectmessage",
        sa.Column(
            "conversation_claim_attempt",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "chk_projectmessage_claim_attempt_nonnegative",
        "projectmessage",
        "conversation_claim_attempt >= 0",
    )
    op.create_index(
        "ix_projectmessage_project_claim",
        "projectmessage",
        ["project_id", "conversation_claim_id"],
    )
    op.create_index(
        "ix_projectmessage_project_claim_lease",
        "projectmessage",
        ["project_id", "handling_state", "conversation_claim_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_projectmessage_project_claim_lease",
        table_name="projectmessage",
    )
    op.drop_index(
        "ix_projectmessage_project_claim",
        table_name="projectmessage",
    )
    op.drop_constraint(
        "chk_projectmessage_claim_attempt_nonnegative",
        "projectmessage",
        type_="check",
    )
    op.drop_column("projectmessage", "conversation_claim_attempt")
    op.drop_column("projectmessage", "conversation_claim_expires_at")
    op.drop_column("projectmessage", "conversation_claim_generation")
    op.drop_column("projectmessage", "conversation_claim_id")
