"""add_project_vision

Add a JSONB vision column to the project table for strategic product-level
understanding maintained by the strategic research agent.

Revision ID: 0029
Revises: 0028
Create Date: 2026-04-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('project', sa.Column('vision', JSONB, nullable=True, server_default='{}'))


def downgrade() -> None:
    op.drop_column('project', 'vision')
