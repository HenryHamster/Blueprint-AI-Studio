"""persist_autonomous_research_config

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('deploymentstate', sa.Column('runtime_autonomous_research_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('deploymentstate', sa.Column('runtime_autonomous_research_model', sa.String(), nullable=True))
    op.add_column('deploymentstate', sa.Column('runtime_autonomous_research_effort', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('deploymentstate', 'runtime_autonomous_research_effort')
    op.drop_column('deploymentstate', 'runtime_autonomous_research_model')
    op.drop_column('deploymentstate', 'runtime_autonomous_research_interval_minutes')
