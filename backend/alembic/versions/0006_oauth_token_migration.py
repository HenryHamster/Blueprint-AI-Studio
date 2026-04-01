"""Replace credentials_json with oauth_token on MaxPlanAccount

Switch from storing full ~/.claude/credentials.json blob to a single
long-lived OAuth token (sk-ant-oat01-...) from `claude setup-token`.
Add claude_config_json for onboarding bypass and token_created_at for age tracking.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename credentials_json → oauth_token
    op.alter_column('maxplanaccount', 'credentials_json',
                     new_column_name='oauth_token', existing_type=sa.Text(), existing_nullable=False)
    # Add new columns
    op.add_column('maxplanaccount', sa.Column('claude_config_json', sa.Text(), nullable=True))
    op.add_column('maxplanaccount', sa.Column('token_created_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('maxplanaccount', 'token_created_at')
    op.drop_column('maxplanaccount', 'claude_config_json')
    op.alter_column('maxplanaccount', 'oauth_token',
                     new_column_name='credentials_json', existing_type=sa.Text(), existing_nullable=False)
