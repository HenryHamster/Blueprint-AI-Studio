"""Make context column NOT NULL with server_default '{}'

Existing NULL context values are backfilled to '{}' before adding the constraint.

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfill existing NULLs
    op.execute("UPDATE task SET context = '{}' WHERE context IS NULL")
    # Set server default and make non-nullable
    op.alter_column('task', 'context',
                     server_default=sa.text("'{}'"),
                     nullable=False,
                     existing_type=sa.dialects.postgresql.JSONB())


def downgrade() -> None:
    op.alter_column('task', 'context',
                     server_default=None,
                     nullable=True,
                     existing_type=sa.dialects.postgresql.JSONB())
