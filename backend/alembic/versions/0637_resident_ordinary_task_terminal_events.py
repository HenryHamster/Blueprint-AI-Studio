"""Add append-only admitted ordinary Task terminal events.

Revision ID: 0637
Revises: 0636
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0637"
down_revision = "0636"
branch_labels = None
depends_on = None

_TABLE = "strategicresidentordinarytaskterminalevent"
_INDEX = "ix_resident_ordinary_task_terminal_project_sequence"
_TRIGGER = "trg_resident_ordinary_task_terminal_event"
_FUNCTION = "emit_resident_ordinary_task_terminal_event"
_SQLITE_INSERT_TRIGGER = "trg_resident_ordinary_task_terminal_event_insert"
_SQLITE_UPDATE_TRIGGER = "trg_resident_ordinary_task_terminal_event_update"


def _create_sqlite_terminal_trigger(*, name: str, event: str, transition: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER {name}
        AFTER {event} ON task
        FOR EACH ROW
        WHEN NEW.status IN ('COMPLETED','FAILED','CANCELLED')
          AND {transition}
          AND NEW.created_by = 'USER'
          AND NEW.parent_task_id IS NULL
          AND NEW.strategic_operation_receipt_id IS NULL
        BEGIN
            INSERT INTO {_TABLE} (
                project_id, task_id, event_kind, task_created_by
            ) VALUES (
                NEW.project_id, NEW.id, NEW.status, NEW.created_by
            );
        END
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    sequence_type = sa.Integer() if bind.dialect.name == "sqlite" else sa.BigInteger()
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
        sa.Column("task_created_by", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "event_kind IN ('COMPLETED','FAILED','CANCELLED')",
            name="chk_resident_ordinary_task_terminal_event_kind",
        ),
        sa.CheckConstraint(
            "task_created_by = 'USER'",
            name="chk_resident_ordinary_task_terminal_created_by",
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
               AND NEW.created_by = 'USER'
               AND NEW.parent_task_id IS NULL
               AND NEW.strategic_operation_receipt_id IS NULL
               AND (
                   TG_OP = 'INSERT'
                   OR (TG_OP = 'UPDATE' AND OLD.status IS DISTINCT FROM NEW.status)
               )
            THEN
                INSERT INTO {_TABLE} (
                    project_id, task_id, event_kind, task_created_by
                ) VALUES (
                    NEW.project_id, NEW.id, NEW.status, NEW.created_by
                );
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
