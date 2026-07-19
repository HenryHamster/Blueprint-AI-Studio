"""Add append-only managed Task terminal events.

Revision ID: 0636
Revises: 0635
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0636"
down_revision = "0635"
branch_labels = None
depends_on = None

_TABLE = "strategicresidenttaskterminalevent"
_INDEX = "ix_resident_managed_task_terminal_project_sequence"
_TRIGGER = "trg_resident_managed_task_terminal_event"
_FUNCTION = "emit_resident_managed_task_terminal_event"
_SQLITE_INSERT_TRIGGER = "trg_resident_managed_task_terminal_event_insert"
_SQLITE_UPDATE_TRIGGER = "trg_resident_managed_task_terminal_event_update"
_TASK_RECEIPT_INDEX = "ix_task_strategic_operation_receipt_id"
_TASK_RECEIPT_FK = "fk_task_strategic_operation_receipt"


def _create_sqlite_terminal_trigger(*, name: str, event: str, transition: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER {name}
        AFTER {event} ON task
        FOR EACH ROW
        WHEN NEW.status IN ('COMPLETED','FAILED','CANCELLED')
          AND NEW.strategic_operation_receipt_id IS NOT NULL
          AND {transition}
          AND json_type(
              COALESCE(NEW.context, '{{}}'),
              '$._resident_managed_task'
          ) = 'object'
          AND NULLIF(TRIM(CAST(json_extract(
              NEW.context,
              '$._resident_managed_task.receipt_id'
          ) AS TEXT)), '') IS NOT NULL
          AND NULLIF(TRIM(CAST(json_extract(
              NEW.context,
              '$._resident_managed_task.capability_id'
          ) AS TEXT)), '') IS NOT NULL
          AND NULLIF(TRIM(CAST(json_extract(
              NEW.context,
              '$._resident_managed_task.source_turn_task_id'
          ) AS TEXT)), '') IS NOT NULL
          AND NULLIF(TRIM(CAST(json_extract(
              NEW.context,
              '$._resident_managed_task.context_hash'
          ) AS TEXT)), '') IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM strategicoperationreceipt receipt
              WHERE receipt.id = NEW.strategic_operation_receipt_id
                AND receipt.project_id = NEW.project_id
                AND replace(CAST(receipt.id AS TEXT), '-', '') =
                    replace(CAST(json_extract(
                        NEW.context,
                        '$._resident_managed_task.receipt_id'
                    ) AS TEXT), '-', '')
                AND replace(CAST(receipt.turn_task_id AS TEXT), '-', '') =
                    replace(CAST(json_extract(
                        NEW.context,
                        '$._resident_managed_task.source_turn_task_id'
                    ) AS TEXT), '-', '')
                AND receipt.capability = json_extract(
                    NEW.context,
                    '$._resident_managed_task.capability_id'
                )
                AND json_extract(
                    receipt.expected_revisions,
                    '$.context_hash'
                ) = json_extract(
                    NEW.context,
                    '$._resident_managed_task.context_hash'
                )
                AND receipt.capability IN (
                    'task.code_change.create',
                    'task.debug.create',
                    'task.large_review.create',
                    'task.ci_repair.create'
                )
                AND receipt.status IN ('applying','applied','reconciled')
          )
        BEGIN
            INSERT INTO {_TABLE} (project_id, task_id, event_kind)
            VALUES (NEW.project_id, NEW.id, NEW.status);
        END
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    sequence_type = sa.Integer() if bind.dialect.name == "sqlite" else sa.BigInteger()
    if bind.dialect.name == "sqlite":
        # SQLite supports a nullable REFERENCES column through direct ALTER.
        # Avoid batch table recreation here: it silently drops the task-link
        # authorization triggers installed by revisions 0608 and 0611.
        op.execute(
            "ALTER TABLE task ADD COLUMN strategic_operation_receipt_id "
            "CHAR(32) CONSTRAINT fk_task_strategic_operation_receipt "
            "REFERENCES strategicoperationreceipt(id) ON DELETE SET NULL"
        )
    else:
        op.add_column(
            "task",
            sa.Column(
                "strategic_operation_receipt_id", sa.Uuid(), nullable=True
            ),
        )
        op.create_foreign_key(
            _TASK_RECEIPT_FK,
            "task",
            "strategicoperationreceipt",
            ["strategic_operation_receipt_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        _TASK_RECEIPT_INDEX,
        "task",
        ["strategic_operation_receipt_id"],
        postgresql_where=sa.text("strategic_operation_receipt_id IS NOT NULL"),
        sqlite_where=sa.text("strategic_operation_receipt_id IS NOT NULL"),
    )
    op.create_table(
        _TABLE,
        sa.Column("sequence", sequence_type, autoincrement=True, primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("event_kind", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "event_kind IN ('COMPLETED','FAILED','CANCELLED')",
            name="chk_resident_managed_task_terminal_event_kind",
        ),
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["project_id", sa.text("sequence DESC")],
    )
    if bind.dialect.name == "sqlite":
        _create_sqlite_terminal_trigger(
            name=_SQLITE_INSERT_TRIGGER,
            event="INSERT",
            transition="1 = 1",
        )
        _create_sqlite_terminal_trigger(
            name=_SQLITE_UPDATE_TRIGGER,
            event="UPDATE OF status",
            transition="OLD.status IS NOT NEW.status",
        )
        return
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF NEW.status IN ('COMPLETED','FAILED','CANCELLED')
               AND NEW.strategic_operation_receipt_id IS NOT NULL
               AND jsonb_typeof(
                   COALESCE(NEW.context, '{{}}'::jsonb)->'_resident_managed_task'
               ) = 'object'
               AND (COALESCE(NEW.context, '{{}}'::jsonb)->'_resident_managed_task')
                   ?& ARRAY['receipt_id','capability_id','source_turn_task_id','context_hash']
               AND NULLIF(NEW.context->'_resident_managed_task'->>'receipt_id', '')
                   IS NOT NULL
               AND NULLIF(NEW.context->'_resident_managed_task'->>'capability_id', '')
                   IS NOT NULL
               AND NULLIF(NEW.context->'_resident_managed_task'->>'source_turn_task_id', '')
                   IS NOT NULL
               AND NULLIF(NEW.context->'_resident_managed_task'->>'context_hash', '')
                   IS NOT NULL
               AND EXISTS (
                   SELECT 1
                   FROM strategicoperationreceipt receipt
                   WHERE receipt.id = NEW.strategic_operation_receipt_id
                     AND receipt.project_id = NEW.project_id
                     AND receipt.id::text =
                         NEW.context->'_resident_managed_task'->>'receipt_id'
                     AND receipt.turn_task_id::text =
                         NEW.context->'_resident_managed_task'->>'source_turn_task_id'
                     AND receipt.capability =
                         NEW.context->'_resident_managed_task'->>'capability_id'
                     AND receipt.expected_revisions->>'context_hash' =
                         NEW.context->'_resident_managed_task'->>'context_hash'
                     AND receipt.capability IN (
                         'task.code_change.create',
                         'task.debug.create',
                         'task.large_review.create',
                         'task.ci_repair.create'
                     )
                     AND receipt.status IN ('applying','applied','reconciled')
               )
               AND (
                   TG_OP = 'INSERT'
                   OR (TG_OP = 'UPDATE' AND OLD.status IS DISTINCT FROM NEW.status)
               )
            THEN
                INSERT INTO {_TABLE} (project_id, task_id, event_kind)
                VALUES (NEW.project_id, NEW.id, NEW.status);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        AFTER INSERT OR UPDATE OF status ON task
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()
        """
    )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON task")
        op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
    elif dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_SQLITE_UPDATE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {_SQLITE_INSERT_TRIGGER}")
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
    op.drop_index(_TASK_RECEIPT_INDEX, table_name="task")
    if dialect == "sqlite":
        # Modern supported SQLite can drop this nullable column directly,
        # preserving the pre-existing task authorization triggers on rollback.
        op.execute(
            "ALTER TABLE task DROP COLUMN strategic_operation_receipt_id"
        )
    else:
        op.drop_constraint(_TASK_RECEIPT_FK, "task", type_="foreignkey")
        op.drop_column("task", "strategic_operation_receipt_id")
