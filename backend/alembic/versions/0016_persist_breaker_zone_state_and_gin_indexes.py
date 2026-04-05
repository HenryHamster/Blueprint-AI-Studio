"""persist_breaker_zone_state_and_gin_indexes

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-04 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade():
    # Circuit breaker and zone health persistence on DeploymentState
    op.add_column('deploymentstate', sa.Column('circuit_breaker_states', JSONB, nullable=True))
    op.add_column('deploymentstate', sa.Column('zone_health_state', JSONB, nullable=True))

    # GIN indexes for JSONB query performance on Task
    op.create_index('ix_task_context_gin', 'task', ['context'], postgresql_using='gin')
    op.create_index('ix_task_depends_on_gin', 'task', ['depends_on'], postgresql_using='gin')


def downgrade():
    op.drop_index('ix_task_depends_on_gin', table_name='task')
    op.drop_index('ix_task_context_gin', table_name='task')
    op.drop_column('deploymentstate', 'zone_health_state')
    op.drop_column('deploymentstate', 'circuit_breaker_states')
