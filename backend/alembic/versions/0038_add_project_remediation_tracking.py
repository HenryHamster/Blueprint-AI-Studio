"""add_project_remediation_tracking

Adds a nullable JSONB column to project for tracking CI auto-remediation
outcomes that could not be linked to a specific direction (novel failures
whose fingerprint matched no direction). Written by
_record_project_remediation_outcome in task_completion.py.

Revision ID: 0038
Revises: 0037
Create Date: 2026-04-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0038'
down_revision = '0037'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('project', sa.Column('remediation_tracking', JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column('project', 'remediation_tracking')
