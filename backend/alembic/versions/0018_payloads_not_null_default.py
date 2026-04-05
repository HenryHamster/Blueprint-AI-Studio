"""payloads_not_null_default

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-04 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfill any NULL payloads before adding NOT NULL constraint
    op.execute("UPDATE task SET payloads = '[]'::jsonb WHERE payloads IS NULL")
    op.alter_column('task', 'payloads',
                     existing_type=sa.dialects.postgresql.JSONB(),
                     nullable=False,
                     server_default=sa.text("'[]'"))


def downgrade() -> None:
    op.alter_column('task', 'payloads',
                     existing_type=sa.dialects.postgresql.JSONB(),
                     nullable=True,
                     server_default=None)
