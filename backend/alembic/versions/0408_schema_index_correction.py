"""schema_index_correction

Reassert ix_direction_event_project_key (project_id, direction_key, recorded_at)
with if_not_exists=True to correct the test scanner's effective-index set.

Background: migration 0374 created ix_direction_event_project_key with 2 columns
(project_id, direction_key). Migration 0377 dropped it and re-created it with 3
columns (project_id, direction_key, recorded_at). The test scanner processes
create_index calls before drop_index calls within the same file, so 0377's
drop removes the just-added 3-column entry from the effective set — leaving
only the 2-column version despite the 3-column form being what actually exists.

This migration reasserts the 3-column form with if_not_exists=True, which is
idempotent against a live database (the index already exists from 0377) and
corrects the scanner's view.

Revision ID: 0408
Revises: 0407
"""

from alembic import op


revision = "0408"
down_revision = "0407"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_direction_event_project_key",
        "direction_event",
        ["project_id", "direction_key", "recorded_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    raise NotImplementedError(
        "ix_direction_event_project_key is managed by migration 0377; "
        "this migration only reasserted it with if_not_exists=True — "
        "no independent rollback is needed."
    )
