"""add_alloweduser_table

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-05 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'alloweduser',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('firebase_uid', sa.String(), nullable=True),
        sa.Column('role', sa.String(), nullable=False, server_default='user'),
        sa.Column('display_name', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_alloweduser_email', 'alloweduser', ['email'], unique=True)
    op.create_index('ix_alloweduser_firebase_uid', 'alloweduser', ['firebase_uid'])


def downgrade() -> None:
    op.drop_table('alloweduser')
