"""Drop the strategic-resident (shadow-turn) schema.

Revision ID: 0657
Revises: 0656

The strategic-resident subsystem was rolled back after failing in the field.
This migration removes every database object it created, so the migration-head
schema matches the SQLModel metadata once the resident model classes are gone.

Dropped objects (created by revisions 0611, 0627, 0631, 0632, 0635, 0636,
0637 and 0640):

* ``task.strategic_operation_receipt_id`` column, its
  ``fk_task_strategic_operation_receipt`` foreign key and
  ``ix_task_strategic_operation_receipt_id`` index (0636).
* The resident tables: ``strategicresident``, ``strategicresidentcheckpoint``,
  ``strategicoperationreceipt``, ``strategicworkermemoryproposal``,
  ``strategicoperationapproval``, ``strategicresidentsweepstate``,
  ``strategicresidenttaskterminalevent`` and
  ``strategicresidentordinarytaskterminalevent`` (0611/0631/0632/0635/0636/0637).
* The resident authority/terminal-event triggers on the surviving ``task``
  table and the standalone PL/pgSQL functions those triggers used.

The surviving ``ix_strategiccycle_project_terminal_updated`` index (added by
0635 but still declared on the ``StrategicCycle`` model) is intentionally kept.
The ``projectmessage.resident_session_id`` column and
``ix_projectmessage_project_session_created`` index (added by revision 0396)
are also intentionally kept because they carry the persistent conversational
Workday identity and are not part of the removed shadow-turn control plane.

This is a one-way drop: the resident tables carried only rolled-back subsystem
state, so ``downgrade`` re-creating them is not supported.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "0657"
down_revision = "0656"
branch_labels = None
depends_on = None


# Resident tables in child-first order so a FK-enforcing dialect can drop them
# cleanly. ``strategicresident`` and ``strategicresidentcheckpoint`` reference
# each other; Postgres uses CASCADE, SQLite does not enforce the cycle at drop
# time once the dependent rows/tables are gone.
_RESIDENT_TABLES = (
    "strategicworkermemoryproposal",
    "strategicoperationapproval",
    "strategicresidentordinarytaskterminalevent",
    "strategicresidenttaskterminalevent",
    "strategicoperationreceipt",
    "strategicresident",
    "strategicresidentcheckpoint",
    "strategicresidentsweepstate",
)

# Triggers installed on the surviving ``task`` table by the resident revisions.
_TASK_TRIGGERS = (
    "trg_task_strategicresident_active_delete",
    "trg_task_strategicresident_project_links",
    "trg_resident_managed_task_terminal_event",
    "trg_resident_ordinary_task_terminal_event",
)

# SQLite trigger names (the SQLite branch splits some project-link triggers into
# per-event insert/update variants).
_SQLITE_TRIGGERS = (
    "trg_strategicresidentcheckpoint_immutability",
    "trg_strategicresidentcheckpoint_authority",
    "trg_checkpoint_strategicresident_project_links",
    "trg_task_strategicresident_active_delete",
    "trg_task_strategicresident_project_links",
    "trg_strategicoperationreceipt_authority",
    "trg_strategicoperationreceipt_immutability",
    "trg_strategicresident_project_links_update",
    "trg_strategicresident_project_links_insert",
    "trg_strategicresident_authority_monotonic",
    "trg_strategicresident_checkpoint_rotation",
    "trg_strategicresident_checkpoint_clear",
    "trg_resident_managed_task_terminal_event_insert",
    "trg_resident_managed_task_terminal_event_update",
    "trg_resident_ordinary_task_terminal_event_insert",
    "trg_resident_ordinary_task_terminal_event_update",
)

# Standalone PL/pgSQL functions (no catalog dependency on the dropped tables, so
# CASCADE will not remove them). No-argument unless a signature is given.
_PG_FUNCTIONS = (
    "enforce_strategicresidentcheckpoint_immutability()",
    "fence_strategicresidentcheckpoint_insert()",
    "protect_strategicresident_checkpoint_projects()",
    "clear_strategicresident_active_turn_on_task_delete()",
    "protect_strategicresident_task_projects()",
    "fence_strategicoperationreceipt_insert()",
    "enforce_strategicoperationreceipt_immutability()",
    "enforce_strategicresident_project_links()",
    "enforce_strategicresident_authority_monotonic()",
    "fence_strategicresident_checkpoint_rotation()",
    "emit_resident_managed_task_terminal_event()",
    "emit_resident_ordinary_task_terminal_event()",
    "is_authorized_resident_cutover_rotation("
    "uuid,text,bigint,uuid,jsonb,uuid,text,bigint,uuid,uuid,uuid,uuid,uuid,jsonb)",
    "is_authorized_resident_cutover_checkpoint("
    "uuid,uuid,bigint,text,jsonb,bigint,uuid)",
)


def _sqlite_columns(table: str) -> set[str]:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        # 1. Drop resident triggers on the surviving ``task`` table.
        for trigger in _TASK_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON task")

        # 2. Drop the resident tables. CASCADE removes their own triggers /
        #    indexes and the ``task`` -> ``strategicoperationreceipt`` FK.
        for table in _RESIDENT_TABLES:
            op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

        # 3. Remove the resident columns/indexes on surviving tables. DROP
        #    COLUMN ... CASCADE also removes the (now orphaned) FK and index.
        op.execute("DROP INDEX IF EXISTS ix_task_strategic_operation_receipt_id")
        op.execute(
            "ALTER TABLE task DROP COLUMN IF EXISTS "
            "strategic_operation_receipt_id CASCADE"
        )
        # resident_session_id and its composite index are intentionally kept:
        # migration 0671 restores the Workday conversational session linkage that
        # this column carries, so dropping it here would silently wipe data on
        # any production DB that runs both migrations in a single `alembic upgrade head`.

        # 4. Drop the standalone resident functions.
        for function in _PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION IF EXISTS {function}")
        return

    if dialect == "sqlite":
        # 1. Drop resident triggers (task-scoped and resident-table-scoped).
        for trigger in set(_TASK_TRIGGERS) | set(_SQLITE_TRIGGERS):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")

        # 2. Remove resident columns/indexes on surviving tables before the
        #    tables they referenced. SQLite has no ``DROP COLUMN IF EXISTS``, so
        #    guard on the live schema (idempotent across partial states).
        op.execute("DROP INDEX IF EXISTS ix_task_strategic_operation_receipt_id")
        if "strategic_operation_receipt_id" in _sqlite_columns("task"):
            op.execute("ALTER TABLE task DROP COLUMN strategic_operation_receipt_id")
        # resident_session_id and its composite index are intentionally kept:
        # migration 0671 restores the Workday conversational session linkage that
        # this column carries, so dropping it here would silently wipe data on
        # any production DB that runs both migrations in a single `alembic upgrade head`.

        # 3. Drop the resident tables (child-first).
        for table in _RESIDENT_TABLES:
            op.execute(f"DROP TABLE IF EXISTS {table}")
        return

    raise NotImplementedError(f"Unsupported dialect for resident drop: {dialect}")


def downgrade() -> None:
    raise NotImplementedError(
        "The strategic-resident schema was removed as dead subsystem state and "
        "cannot be recreated by downgrade."
    )
