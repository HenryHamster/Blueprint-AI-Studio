"""Add spend_approval_cleared_at to Task for operator-approved spend provenance.

Revision ID: 0331
Revises: 0330
"""

import sqlalchemy as sa
from alembic import op


revision = "0331"
down_revision = "0330"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task",
        sa.Column("spend_approval_cleared_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task", "spend_approval_cleared_at")
