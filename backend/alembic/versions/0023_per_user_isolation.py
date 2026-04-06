"""per_user_isolation

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-06 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add user_id to maxplanaccount (nullable first for backfill)
    op.add_column('maxplanaccount', sa.Column('user_id', sa.String(), nullable=True))

    # 2. Backfill: assign existing accounts + NULL-user projects to first admin
    op.execute("""
        DO $$ DECLARE admin_uid TEXT; BEGIN
            SELECT firebase_uid INTO admin_uid
            FROM alloweduser WHERE role = 'admin' ORDER BY created_at ASC LIMIT 1;
            IF admin_uid IS NULL THEN admin_uid := 'admin-api-key'; END IF;

            UPDATE maxplanaccount SET user_id = admin_uid WHERE user_id IS NULL;
            UPDATE project SET user_id = admin_uid WHERE user_id IS NULL;
        END $$;
    """)

    # 3. Set NOT NULL constraints
    op.alter_column('maxplanaccount', 'user_id', nullable=False)
    op.alter_column('project', 'user_id', existing_type=sa.String(), nullable=False)

    # 4. Add index
    op.create_index('ix_maxplanaccount_user_id', 'maxplanaccount', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_maxplanaccount_user_id', table_name='maxplanaccount')
    op.alter_column('project', 'user_id', existing_type=sa.String(), nullable=True)
    op.drop_column('maxplanaccount', 'user_id')
