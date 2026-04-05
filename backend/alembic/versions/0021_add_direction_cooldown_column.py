"""add_direction_cooldown_column

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-05 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0020 was deployed before this column was added to it.
    # Add it if missing (idempotent via IF NOT EXISTS workaround).
    op.execute("""
        DO $$ BEGIN
            ALTER TABLE deploymentstate
            ADD COLUMN runtime_autonomous_direction_cooldown_minutes INTEGER;
        EXCEPTION WHEN duplicate_column THEN
            NULL;
        END $$;
    """)


def downgrade() -> None:
    op.drop_column('deploymentstate', 'runtime_autonomous_direction_cooldown_minutes')
