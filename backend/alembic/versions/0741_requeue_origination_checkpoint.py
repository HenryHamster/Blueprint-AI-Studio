"""Checkpoint requeue clone origination preservation.

The requeue fix is application behavior only: ``Task.origination`` already
exists since migration 0735.  This reserved, no-op revision records the
schema checkpoint required by the task reservation without introducing a
second migration for an unchanged table.
"""

revision = "0741"
down_revision = "0740"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
