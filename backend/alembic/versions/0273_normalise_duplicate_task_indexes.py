"""Normalise duplicate task index definitions that break SQLite chain replay.

Two pairs of migrations each define the same index name on the non-Postgres
path without guarding, so a windowed SQLite replay that spans any of the four
affected revisions fails on the second create:

  ix_task_project_status_updated — created by both 0236 and 0237
  ix_task_post_commit_pending    — created by both 0045 and 0117

The Postgres branches in 0117, 0236, and 0237 all use
``CREATE INDEX CONCURRENTLY IF NOT EXISTS``, so production is unaffected.
This migration re-normalises both indexes on the non-Postgres path using
guarded DROP/CREATE so the replay is idempotent regardless of which prior
revisions have run. Running this migration twice is also a clean no-op.

On Postgres both operations are explicit no-ops (the CONCURRENTLY+IF NOT
EXISTS path already applied the correct definitions in prod).

Revision ID: 0273
Revises: 0269
"""

from alembic import op


revision = "0273"
down_revision = "0269"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Prod already has both indexes via IF NOT EXISTS guards in 0117/0236/0237.
        # No-op to avoid taking an AccessShareLock on task during a no-change migration.
        return

    # Normalise ix_task_project_status_updated (0236 and 0237 both create it)
    op.execute("DROP INDEX IF EXISTS ix_task_project_status_updated")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_project_status_updated"
        " ON task (project_id, status, updated_at)"
    )

    # Normalise ix_task_post_commit_pending (0045 and 0117 both create it)
    # Re-assert as the 0117 definition: composite (project_id, status) with
    # the partial predicate — more selective than the bare 0045 single-column form.
    op.execute("DROP INDEX IF EXISTS ix_task_post_commit_pending")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_post_commit_pending"
        " ON task (project_id, status) WHERE post_commit_pending = true"
    )


def downgrade() -> None:
    # Forward-only normalisation: the index definitions are owned by
    # 0045/0117/0236/0237. Dropping them here would wrongly remove those
    # revisions' indexes when downgrading, so there is no correct rollback.
    raise NotImplementedError(
        "0273 is a forward-only normalisation; teardown of these indexes belongs "
        "to migrations 0045/0117/0236/0237."
    )
