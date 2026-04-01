"""Add status_log JSONB column to task table

Persistent log of all status messages with timestamps.

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('task', sa.Column('status_log', JSONB, nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column('task', 'status_log')
