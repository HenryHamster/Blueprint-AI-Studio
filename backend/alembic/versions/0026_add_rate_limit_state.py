"""add rate_limit_state to max_plan_accounts

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('maxplanaccount', sa.Column('rate_limit_state', JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column('maxplanaccount', 'rate_limit_state')
