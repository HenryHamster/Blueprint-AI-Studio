"""add_build_config_to_project

Build configuration for agent lint/test/build commands (auto-detected or
user-provided). Stored as JSONB so agents can read structured fields like
test_command, lint_command, build_command, etc.

Revision ID: 0032
Revises: 0031
Create Date: 2026-04-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('project', sa.Column('build_config', JSONB, nullable=True, server_default='{}'))


def downgrade() -> None:
    op.drop_column('project', 'build_config')
