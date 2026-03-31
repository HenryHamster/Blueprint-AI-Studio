"""Add task decomposition (depends_on, plan_output) and autonomy (directions, autonomous_enabled)

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Task decomposition fields
    op.add_column('task', sa.Column('depends_on', postgresql.JSONB(astext_type=sa.Text()),
                                    server_default='[]', nullable=False))
    op.add_column('task', sa.Column('plan_output', postgresql.JSONB(astext_type=sa.Text()),
                                    nullable=True))

    # Project autonomy fields
    op.add_column('project', sa.Column('directions', postgresql.JSONB(astext_type=sa.Text()),
                                       server_default='[]', nullable=False))
    op.add_column('project', sa.Column('autonomous_enabled', sa.Boolean(),
                                       server_default=sa.text('true'), nullable=False))

def downgrade() -> None:
    op.drop_column('project', 'autonomous_enabled')
    op.drop_column('project', 'directions')
    op.drop_column('task', 'plan_output')
    op.drop_column('task', 'depends_on')
