"""add_halted_at_to_deployment_state

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-04 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('deploymentstate', sa.Column('halted_at', sa.DateTime, nullable=True))


def downgrade():
    op.drop_column('deploymentstate', 'halted_at')
