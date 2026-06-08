"""task_park_wake

Schema backing the task park-and-wake primitive (new non-terminal
``TaskStatus.PARKED`` state + durable external wake registration/delivery).

Adds:

* Two nullable ``task`` columns — ``parked_at`` (entry timestamp) and
  ``park_reason`` (operator-visible note). Both nullable with no server default,
  so the ADD COLUMNs are metadata-only and existing rows read NULL (they are
  never PARKED) — a pure additive no-op for them.
* ``ix_task_parked_at`` — a partial index on ``parked_at`` WHERE
  ``status = 'PARKED'`` so the reaper's PARKED-strand sweep hits a tight index
  range scan instead of seq-scanning the whole task table.
* ``taskwakeregistration`` — the durable wake registration + delivery-state
  table. ``task_id`` CASCADE-deletes with its task (a wake registration is
  meaningless once its task is gone), unlike ComputeJob/MigrationReservation
  which detach (SET NULL) as durable history.
* Widens the ``ux_task_active_idempotency_key`` partial-UNIQUE predicate to
  include ``'PARKED'`` so a parked (non-terminal) task keeps holding its
  idempotency slot and a duplicate insert with the same key is still deduped.
  A partial-index predicate cannot be ALTERed in place, so it is dropped and
  recreated via raw ``op.execute`` (not ``op.*_index``) — mirroring alembic 0138
  so the static ``test_model_indexes_covered_by_migrations`` invariant keeps
  seeing migration 0049's original ``op.create_index`` as the canonical provider
  of the ``(task, {idempotency_key})`` coverage entry.

``status`` is a plain VARCHAR (no Postgres ENUM / CHECK constraint), so admitting
the new ``PARKED`` value needs no DDL on the column. Widening the unique
predicate is rollout-safe: no row is PARKED at migration time, the other active
values are already in the set, and FAILED stays excluded.

Per CLAUDE.md this migration is chained from the latest head (0198,
``artifact_consumption``) and ``EXPECTED_SCHEMA_REVISION`` is bumped to
0199 (linearized against production at merge time) to match.

Revision ID: 0199
Revises: 0198
Create Date: 2026-06-07 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
import sqlmodel


# revision identifiers, used by Alembic.
revision = "0199"
down_revision = "0198"
branch_labels = None
depends_on = None


_IDEMPOTENCY_WITH_PARKED = (
    "idempotency_key IS NOT NULL AND status IN "
    "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING','BLOCKED','PARKED')"
)
_IDEMPOTENCY_WITHOUT_PARKED = (
    "idempotency_key IS NOT NULL AND status IN "
    "('QUEUED','PROVISIONING','IN_PROGRESS','EVALUATING','BLOCKED')"
)


def upgrade() -> None:
    # Durable operator-visible PARKED metadata. Nullable + no server default =>
    # metadata-only ADD COLUMN; existing rows read NULL.
    op.add_column(
        "task", sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("task", sa.Column("park_reason", sa.Text(), nullable=True))

    # Durable external wake registration + delivery state.
    op.create_table(
        "taskwakeregistration",
        sa.Column("id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("task_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("project_id", sqlmodel.sql.sqltypes.GUID(), nullable=False),
        sa.Column("wake_source_kind", sa.String(length=64), nullable=False),
        sa.Column("wake_source_ref", sa.String(length=255), nullable=True),
        sa.Column(
            "source_metadata",
            JSONB(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("wake_action", sa.String(), server_default="resume", nullable=False),
        sa.Column(
            "delivery_state", sa.String(), server_default="PENDING", nullable=False
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("result_state", sa.String(length=32), nullable=True),
        sa.Column("result_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # CASCADE (not SET NULL): a wake registration is meaningless once its task
        # is gone, so it dies with the task rather than living on as history.
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_taskwakeregistration_task_id"),
        "taskwakeregistration",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_taskwakeregistration_state_deadline",
        "taskwakeregistration",
        ["delivery_state", "deadline_at"],
        unique=False,
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction; commit first.
        op.execute("COMMIT")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_task_parked_at "
            "ON task (parked_at) WHERE status = 'PARKED'"
        )
        # Drop + recreate the idempotency unique index with the widened predicate
        # (raw SQL — see module docstring for why not op.*_index).
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ux_task_active_idempotency_key")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "ux_task_active_idempotency_key ON task (idempotency_key) "
            f"WHERE {_IDEMPOTENCY_WITH_PARKED}"
        )
    else:
        # SQLite path (a direct alembic run; the pytest fixtures build the schema
        # from metadata.create_all instead). The literal op.create_index here
        # gives test_model_indexes_covered_by_migrations its (task, {parked_at})
        # coverage entry.
        op.create_index(
            "ix_task_parked_at",
            "task",
            ["parked_at"],
            postgresql_where=sa.text("status = 'PARKED'"),
            sqlite_where=sa.text("status = 'PARKED'"),
        )
        op.execute("DROP INDEX IF EXISTS ux_task_active_idempotency_key")
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_active_idempotency_key "
            f"ON task (idempotency_key) WHERE {_IDEMPOTENCY_WITH_PARKED}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        # Restore the original (pre-PARKED) idempotency predicate.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ux_task_active_idempotency_key")
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "ux_task_active_idempotency_key ON task (idempotency_key) "
            f"WHERE {_IDEMPOTENCY_WITHOUT_PARKED}"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_task_parked_at")
    else:
        op.execute("DROP INDEX IF EXISTS ux_task_active_idempotency_key")
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_active_idempotency_key "
            f"ON task (idempotency_key) WHERE {_IDEMPOTENCY_WITHOUT_PARKED}"
        )
        op.drop_index("ix_task_parked_at", table_name="task")

    op.drop_index(
        "ix_taskwakeregistration_state_deadline", table_name="taskwakeregistration"
    )
    op.drop_index(
        op.f("ix_taskwakeregistration_task_id"), table_name="taskwakeregistration"
    )
    op.drop_table("taskwakeregistration")

    op.drop_column("task", "park_reason")
    op.drop_column("task", "parked_at")
