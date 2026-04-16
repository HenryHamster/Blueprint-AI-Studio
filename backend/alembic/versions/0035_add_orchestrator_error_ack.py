"""add_orchestrator_error_ack_table

Creates orchestratorerrorack for persisting error-widget dismissals.
Previously stored in a process-global dict, which lost state on every
backend restart and diverged across replicas.

Revision ID: 0035
Revises: 0034
Create Date: 2026-04-16 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'orchestratorerrorack',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=False),
        sa.Column('acknowledged_by', sa.String(), nullable=False, server_default=''),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('orchestratorerrorack')
