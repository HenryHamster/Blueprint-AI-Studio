"""Fence versioned guard-suppression escalation publication.

Revision ID: 0766
Revises: 0765
Create Date: 2026-08-28
"""

from alembic import op
import sqlalchemy as sa


revision = "0766"
down_revision = "0765"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "modulefindingchain",
        sa.Column(
            "escalation_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "modulefindingchain",
        sa.Column("escalation_state_fingerprint", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("modulefindingchain", "escalation_state_fingerprint")
    op.drop_column("modulefindingchain", "escalation_version")
