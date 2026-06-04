"""Reconcile BLOCKED task fields + project.merge_gate_runtime onto the live DB.

The ``main -> automain`` back-merge renumbered the digestcache migration from
main's ``0138`` to ``0140``, which placed automain's task-blocked-state (``0138``)
and merge_gate_runtime (``0139``) migrations at revision numbers the live LTS
database had ALREADY consumed with main's own migrations. Alembic therefore
considered ``0138``/``0139`` applied and skipped them, so the live DB is missing
``task.blocker_kind`` / ``blocked_reason`` / ``blocked_at`` / ``recheck_after``
(+ the BLOCKED indexes) and ``project.merge_gate_runtime`` even though its
version counter is already past them. Every ``0138``+-expecting revision then
boot-crashes on a missing column.

This migration re-applies those objects IDEMPOTENTLY: on the live DB it adds the
missing objects; on a fresh DB (where ``0138``/``0139`` created them normally)
every step is a no-op. Forward-only and additive — it mirrors the authoritative
definitions in migrations ``0138`` / ``0139``. The index DDL is emitted via raw
``op.execute`` (not ``op.create_index``) so ``0138`` remains the single canonical
provider of the ``test_model_indexes_covered_by_migrations`` coverage entry.

Revision ID: 0141
Revises: 0140
Create Date: 2026-06-04 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

# Mirror of 0138's widened idempotency predicate (BLOCKED is non-terminal and
# keeps holding its idempotency slot).
_IDEMPOTENCY_WITH_BLOCKED = (
    "idempotency_key IS NOT NULL AND status IN "
    "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING','BLOCKED')"
)


def _columns(insp, table: str) -> set:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # --- 0139: project.merge_gate_runtime ---
    if "merge_gate_runtime" not in _columns(insp, "project"):
        op.add_column(
            "project", sa.Column("merge_gate_runtime", JSONB(), nullable=True)
        )

    # --- 0138: BLOCKED task-state fields + indexes ---
    # A missing ``blocker_kind`` means the whole of 0138 was skipped on this DB
    # (the renumber-collision case); replay it. Present => no-op on fresh DBs.
    if "blocker_kind" not in _columns(insp, "task"):
        op.add_column(
            "task", sa.Column("blocker_kind", sa.String(length=64), nullable=True)
        )
        op.add_column("task", sa.Column("blocked_reason", sa.Text(), nullable=True))
        op.add_column("task", sa.Column("blocked_at", sa.DateTime(), nullable=True))
        op.add_column("task", sa.Column("recheck_after", sa.DateTime(), nullable=True))

        if bind.dialect.name == "postgresql":
            # CONCURRENTLY cannot run inside a transaction; commit first.
            op.execute("COMMIT")
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_blocked_recheck "
                "ON task (recheck_after) WHERE status = 'BLOCKED'"
            )
            op.execute(
                "DROP INDEX CONCURRENTLY IF EXISTS ux_task_active_idempotency_key"
            )
            op.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
                "ux_task_active_idempotency_key ON task (idempotency_key) "
                f"WHERE {_IDEMPOTENCY_WITH_BLOCKED}"
            )
        else:
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_task_blocked_recheck "
                "ON task (recheck_after) WHERE status = 'BLOCKED'"
            )
            op.execute("DROP INDEX IF EXISTS ux_task_active_idempotency_key")
            op.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_active_idempotency_key "
                f"ON task (idempotency_key) WHERE {_IDEMPOTENCY_WITH_BLOCKED}"
            )


def downgrade() -> None:
    # Forward-only reconciliation: it only ensures objects exist that migrations
    # 0138 / 0139 already own. Dropping them here would wrongly remove those
    # revisions' columns when downgrading to 0140 (where they must still be
    # present), so there is no correct rollback for this revision.
    raise NotImplementedError(
        "0141 is a forward-only reconciliation; teardown of these objects belongs "
        "to migrations 0138 / 0139."
    )
