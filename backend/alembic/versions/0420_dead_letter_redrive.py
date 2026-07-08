"""dead_letter_redrive

Reserved revision — chains the dead-letter redrive feature into the linear
migration history. The notification_dead_letter table was created in 0418; this
revision marks the administrative redrive capability (list + re-drive endpoints)
without an additional schema change.

Revision ID: 0420
Revises: 0418
"""

revision = "0420"
down_revision = "0418"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "0420 is a no-op chain marker (the notification_dead_letter table was "
        "created at 0418); there is no schema to roll back."
    )
