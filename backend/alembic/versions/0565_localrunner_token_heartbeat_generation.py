"""Track which runner token generation authenticated the latest heartbeat.

Revision ID: 0565
Revises: 0564
Create Date: 2026-07-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0565"
down_revision = "0564"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "localrunner",
        sa.Column("token_generation", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "localrunner",
        sa.Column(
            "last_heartbeat_token_generation",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("localrunner", "last_heartbeat_token_generation")
    op.drop_column("localrunner", "token_generation")
