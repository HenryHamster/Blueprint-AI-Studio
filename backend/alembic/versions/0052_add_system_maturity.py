"""Add system maturity table.

Revision ID: 0052
Revises: 0051
Create Date: 2026-04-27 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '0052'
down_revision = '0051'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLModel maps ``SystemMaturity`` to ``systemmaturity`` by default.
    op.create_table(
        'systemmaturity',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column(
            'project_id',
            sa.Uuid(),
            sa.ForeignKey('project.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('system_id', sa.String(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column(
            'components',
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column('recommended_mode', sa.String(), nullable=True),
        sa.Column(
            'top_issues',
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            'evidence',
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column('last_audited_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'source_task_id',
            sa.Uuid(),
            sa.ForeignKey('task.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('project_id', 'system_id', name='uq_system_maturity_project_system'),
    )

    op.create_index('ix_systemmaturity_project_id', 'systemmaturity', ['project_id'])
    op.create_index('ix_systemmaturity_system_id', 'systemmaturity', ['system_id'])


def downgrade() -> None:
    # Destructive rollback: all SystemMaturity rows are dropped with the table.
    op.drop_index('ix_systemmaturity_system_id', table_name='systemmaturity')
    op.drop_index('ix_systemmaturity_project_id', table_name='systemmaturity')
    op.drop_table('systemmaturity')
