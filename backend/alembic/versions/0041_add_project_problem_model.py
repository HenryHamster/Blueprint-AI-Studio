"""add_project_problem_model

Adds the ``problem_model`` JSONB column to ``project``. This is the new
strategic-layer source of truth that replaces the free-form ``vision``
blob + scattered strategic state. See app.problem_model for the schema.

Revision ID: 0041
Revises: 0040
Create Date: 2026-04-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0041'
down_revision = '0040'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'project',
        sa.Column('problem_model', JSONB, nullable=True, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column('project', 'problem_model')
