"""Persist caller-scoped GitHub OAuth credentials for onboarding authorization.

Revision ID: 0566
Revises: 0565
Create Date: 2026-07-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0566"
down_revision = "0565"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("userintegration", sa.Column("github_oauth_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("userintegration", "github_oauth_token")
