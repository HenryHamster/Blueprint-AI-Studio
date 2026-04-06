"""fix_admin_api_key_backfill

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-06 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The 0023 migration backfilled project.user_id with 'admin-api-key' because
    # the AllowedUser table was empty at migration time (no firebase_uid yet).
    # Now that the admin has signed in, fix the references to use the real UID.
    op.execute("""
        DO $$ DECLARE real_uid TEXT; BEGIN
            SELECT firebase_uid INTO real_uid
            FROM alloweduser WHERE role = 'admin' AND firebase_uid IS NOT NULL
            ORDER BY created_at ASC LIMIT 1;

            IF real_uid IS NOT NULL THEN
                UPDATE project SET user_id = real_uid WHERE user_id = 'admin-api-key';
                UPDATE projectmember SET user_id = real_uid WHERE user_id = 'admin-api-key';
                UPDATE maxplanaccount SET user_id = real_uid WHERE user_id = 'admin-api-key';
            END IF;
        END $$;
    """)


def downgrade() -> None:
    pass  # One-way data fix
