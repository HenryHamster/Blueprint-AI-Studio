"""Add missing columns: task.started_at, userintegration.github_pat

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('task', sa.Column('started_at', sa.DateTime(), nullable=True))
    op.add_column('userintegration', sa.Column('github_pat', sqlmodel.sql.sqltypes.AutoString(), nullable=True))

def downgrade() -> None:
    op.drop_column('userintegration', 'github_pat')
    op.drop_column('task', 'started_at')
