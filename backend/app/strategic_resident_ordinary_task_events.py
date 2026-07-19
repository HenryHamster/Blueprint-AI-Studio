"""Append-only high-signal outcomes for operator-authored root Tasks."""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Session
from sqlmodel import Field, SQLModel
from sqlmodel.sql.sqltypes import GUID


TERMINAL_EVENT_KINDS = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
OPERATOR_TASK_PROVENANCE = frozenset({"USER"})
_SQLITE_DATABASE_TRIGGER = "trg_resident_ordinary_task_terminal_event_insert"


class StrategicResidentOrdinaryTaskTerminalEvent(SQLModel, table=True):
    """One durable event for an admitted ordinary Task terminal transition.

    The event intentionally carries only immutable routing/evidence identity. A
    Resident that needs details must inspect the exact Task; raw context, logs,
    descriptions, and status messages never enter this cadence ledger.
    """

    sequence: int | None = Field(
        default=None,
        sa_column=Column(
            BigInteger().with_variant(Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
    )
    project_id: uuid.UUID = Field(
        sa_column=Column(
            GUID(), ForeignKey("project.id", ondelete="CASCADE"), nullable=False
        )
    )
    task_id: uuid.UUID = Field(sa_column=Column(GUID(), nullable=False))
    event_kind: str = Field(sa_column=Column(String(16), nullable=False))
    task_created_by: str = Field(sa_column=Column(String(16), nullable=False))

    __table_args__ = (
        CheckConstraint(
            "event_kind IN ('COMPLETED','FAILED','CANCELLED')",
            name="chk_resident_ordinary_task_terminal_event_kind",
        ),
        CheckConstraint(
            "task_created_by = 'USER'",
            name="chk_resident_ordinary_task_terminal_created_by",
        ),
        Index(
            "ix_resident_ordinary_task_terminal_project_sequence",
            "project_id",
            text("sequence DESC"),
        ),
    )


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _is_admitted_ordinary_task(task: Any) -> bool:
    """Admit only operator-owned root work, never pipeline/Resident children."""

    return (
        str(task.__dict__.get("created_by") or "") in OPERATOR_TASK_PROVENANCE
        and task.__dict__.get("parent_task_id") is None
        and task.__dict__.get("strategic_operation_receipt_id") is None
    )


def register_sqlite_ordinary_task_terminal_listener(task_model: type[Any]) -> None:
    """Mirror the production trigger for metadata-created SQLite test DBs."""

    @event.listens_for(Session, "before_flush")
    def _emit_ordinary_task_terminal_events(session, flush_context, instances):
        from app.database import resolve_dialect_name  # noqa: PLC0415 — local import avoids load cycle

        dialect_name = resolve_dialect_name(session)
        if dialect_name == "postgresql":
            return
        candidates = [
            task
            for task in (*session.new, *session.dirty)
            if isinstance(task, task_model)
            and _status_value(task.__dict__.get("status")) in TERMINAL_EVENT_KINDS
            and _is_admitted_ordinary_task(task)
        ]
        if not candidates:
            return
        if dialect_name == "sqlite":
            installed = (
                session.connection()
                .exec_driver_sql(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'trigger' AND name = ? LIMIT 1",
                    (_SQLITE_DATABASE_TRIGGER,),
                )
                .scalar_one_or_none()
            )
            if installed is not None:
                return
        for task in candidates:
            status = _status_value(task.__dict__.get("status"))
            if task in session.dirty:
                history = inspect(task).attrs.status.history
                if not history.added or _status_value(history.added[0]) != status:
                    continue
                if history.deleted and _status_value(history.deleted[0]) == status:
                    continue
            session.add(
                StrategicResidentOrdinaryTaskTerminalEvent(
                    project_id=task.project_id,
                    task_id=task.id,
                    event_kind=status,
                    task_created_by=str(task.created_by),
                )
            )


__all__ = [
    "StrategicResidentOrdinaryTaskTerminalEvent",
    "register_sqlite_ordinary_task_terminal_listener",
]
