"""add_project_design_doc

Living architectural description maintained by the design doc update agent.
Shared between all agents as a knowledge layer.

Revision ID: 0031
Revises: 0030
Create Date: 2026-04-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('project', sa.Column('design_doc', JSONB, nullable=True, server_default='{}'))


def downgrade() -> None:
    op.drop_column('project', 'design_doc')
