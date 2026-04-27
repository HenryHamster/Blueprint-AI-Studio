"""Partial UNIQUE index on Artifact (task_id, artifact_type='PULL_REQUEST')

Audit 2026-04-25 A14: PR artifact creation in ``agent_status.update_agent_status``
checks for duplicates with a non-locking SELECT before INSERT. Two concurrent
status POSTs can both pass the dedup check and INSERT — the task ends up
with two PR artifacts pointing at the same PR, and downstream review logic
(``review_pipeline``) picks one nondeterministically.

Other artifact types (GIT_COMMIT, ERROR_LOG, SCREENSHOT, ...) legitimately
have multiple rows per task (one per commit, one per error, iteration-bumped
re-runs), so a full UNIQUE(task_id, artifact_type) would over-constrain.
A partial unique index limited to PULL_REQUEST closes the race without
touching the other types.

Revision ID: 0050
Revises: 0049
Create Date: 2026-04-26 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0050'
down_revision = '0049'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ux_artifact_task_pull_request',
        'artifact',
        ['task_id'],
        unique=True,
        postgresql_where=sa.text("artifact_type = 'PULL_REQUEST'"),
    )


def downgrade() -> None:
    op.drop_index('ux_artifact_task_pull_request', table_name='artifact')
