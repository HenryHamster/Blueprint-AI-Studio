"""add_project_self_reflection_log

Adds the ``self_reflection_log`` JSONB column to ``project`` — an
append-only list of the strategic agent's own observations about what was
hard each cycle, used to drive self-improvement on self-repo projects.
Empty list on non-self-repo projects (zero cost when unused).

Revision ID: 0042
Revises: 0041
Create Date: 2026-04-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0042'
down_revision = '0041'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'project',
        sa.Column(
            'self_reflection_log',
            JSONB,
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column('project', 'self_reflection_log')
