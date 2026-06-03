"""Persist BLOCKED task-state fields + recheck-scan index.

Adds the schema backing the new non-terminal ``TaskStatus.BLOCKED`` state and
its durable blocker metadata:

* Four nullable ``task`` columns — ``blocker_kind`` (short categorical code),
  ``blocked_reason`` (human-readable detail), ``blocked_at`` (entry timestamp),
  and ``recheck_after`` (earliest recheck time). All nullable with no server
  default, so the ADD COLUMNs are metadata-only (instant even on a large task
  table) and existing QUEUED/FAILED rows simply read NULL — they are never
  BLOCKED, so the rollout is a pure additive no-op for them.
* ``ix_task_blocked_recheck`` — a partial index on ``recheck_after`` WHERE
  ``status = 'BLOCKED'`` so the recheck sweep
  (``status = 'BLOCKED' AND recheck_after <= now()``) hits a tight index range
  scan instead of seq-scanning the whole table for the small parked set.
* Widens the ``ux_task_active_idempotency_key`` partial-UNIQUE predicate to
  include ``'BLOCKED'`` so a parked (non-terminal) task keeps holding its
  idempotency slot and a duplicate insert with the same key is still deduped.
  A partial-index predicate cannot be ALTERed in place, so it is dropped and
  recreated. This is done via raw ``op.execute`` (not ``op.create_index`` /
  ``op.drop_index``) so the static ``test_model_indexes_covered_by_migrations``
  invariant continues to see migration 0049's original ``op.create_index`` as
  the canonical provider of the ``(task, {idempotency_key})`` coverage entry —
  emitting a drop/create pair for that name here would cancel it out (the
  scanner processes all creates before all drops within a file).

``status`` is a plain VARCHAR column (not a Postgres ENUM type) with no CHECK
constraint, so admitting the new ``BLOCKED`` value needs no DDL on the column.

Widening the unique predicate is rollout-safe: no row is BLOCKED at migration
time, QUEUED is already in the set, and FAILED stays excluded — so no existing
QUEUED/FAILED row can newly collide.

Per CLAUDE.md this migration is chained from the latest head (0137,
``cascade_project_fks_messages_anomaly``) and ``EXPECTED_SCHEMA_REVISION`` is
bumped to 0138 to match.

Revision ID: 0138
Revises: 0137
Create Date: 2026-06-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


_IDEMPOTENCY_WITH_BLOCKED = (
    "idempotency_key IS NOT NULL AND status IN "
    "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING','BLOCKED')"
)
_IDEMPOTENCY_WITHOUT_BLOCKED = (
    "idempotency_key IS NOT NULL AND status IN "
    "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING')"
)


def upgrade() -> None:
    # Durable blocker metadata. Nullable + no server default => metadata-only
    # ADD COLUMN; existing rows read NULL.
    op.add_column(
        "task", sa.Column("blocker_kind", sa.String(length=64), nullable=True)
    )
    op.add_column("task", sa.Column("blocked_reason", sa.Text(), nullable=True))
    op.add_column("task", sa.Column("blocked_at", sa.DateTime(), nullable=True))
    op.add_column("task", sa.Column("recheck_after", sa.DateTime(), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; commit first.
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_blocked_recheck "
            "ON task (recheck_after) WHERE status = 'BLOCKED'"
        )
        # Drop + recreate the idempotency unique index with the widened
        # predicate (raw SQL — see module docstring for why not op.*_index).
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ux_task_active_idempotency_key")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "ux_task_active_idempotency_key ON task (idempotency_key) "
            f"WHERE {_IDEMPOTENCY_WITH_BLOCKED}"
        )
    else:
        # SQLite path (a direct alembic run; the pytest fixtures build the
        # schema from metadata.create_all instead). The literal op.create_index
        # here gives test_model_indexes_covered_by_migrations its
        # (task, {recheck_after}) coverage entry.
        op.create_index(
            "ix_task_blocked_recheck",
            "task",
            ["recheck_after"],
            postgresql_where=sa.text("status = 'BLOCKED'"),
            sqlite_where=sa.text("status = 'BLOCKED'"),
        )
        op.execute("DROP INDEX IF EXISTS ux_task_active_idempotency_key")
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_active_idempotency_key "
            f"ON task (idempotency_key) WHERE {_IDEMPOTENCY_WITH_BLOCKED}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        # Restore the original (pre-BLOCKED) idempotency predicate.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ux_task_active_idempotency_key")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "ux_task_active_idempotency_key ON task (idempotency_key) "
            f"WHERE {_IDEMPOTENCY_WITHOUT_BLOCKED}"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_blocked_recheck")
    else:
        op.execute("DROP INDEX IF EXISTS ux_task_active_idempotency_key")
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_active_idempotency_key "
            f"ON task (idempotency_key) WHERE {_IDEMPOTENCY_WITHOUT_BLOCKED}"
        )
        op.drop_index("ix_task_blocked_recheck", table_name="task")

    op.drop_column("task", "recheck_after")
    op.drop_column("task", "blocked_at")
    op.drop_column("task", "blocked_reason")
    op.drop_column("task", "blocker_kind")
