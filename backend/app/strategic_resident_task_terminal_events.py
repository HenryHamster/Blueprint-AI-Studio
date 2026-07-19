"""Append-only terminal outcomes for Resident-derived managed Tasks."""

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


MANAGED_TASK_CONTEXT_KEY = "_resident_managed_task"
TERMINAL_EVENT_KINDS = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_SQLITE_DATABASE_TRIGGER = "trg_resident_managed_task_terminal_event_insert"
_REQUIRED_LINEAGE_KEYS = frozenset(
    {"receipt_id", "capability_id", "source_turn_task_id", "context_hash"}
)
_MANAGED_CREATE_CAPABILITIES = frozenset(
    {
        "task.ci_repair.create",
        "task.code_change.create",
        "task.debug.create",
        "task.large_review.create",
    }
)


class StrategicResidentTaskTerminalEvent(SQLModel, table=True):
    """One durable event for every managed Task transition into a terminal state."""

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

    __table_args__ = (
        CheckConstraint(
            "event_kind IN ('COMPLETED','FAILED','CANCELLED')",
            name="chk_resident_managed_task_terminal_event_kind",
        ),
        Index(
            "ix_resident_managed_task_terminal_project_sequence",
            "project_id",
            text("sequence DESC"),
        ),
    )


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _has_server_managed_lineage(context: Any, receipt_id: Any) -> bool:
    if receipt_id is None:
        return False
    if not isinstance(context, dict):
        return False
    lineage = context.get(MANAGED_TASK_CONTEXT_KEY)
    return (
        isinstance(lineage, dict)
        and all(
            isinstance(lineage.get(key), str) and bool(lineage[key].strip())
            for key in _REQUIRED_LINEAGE_KEYS
        )
        and lineage["receipt_id"] == str(receipt_id)
        and lineage["capability_id"] in _MANAGED_CREATE_CAPABILITIES
    )


def register_sqlite_terminal_event_listener(
    task_model: type[Any], receipt_model: type[Any]
) -> None:
    """Mirror the production PostgreSQL trigger for metadata-built test DBs."""

    @event.listens_for(Session, "before_flush")
    def _emit_managed_task_terminal_events(session, flush_context, instances):
        from app.database import resolve_dialect_name  # noqa: PLC0415 — local import avoids load cycle

        dialect_name = resolve_dialect_name(session)
        if dialect_name == "postgresql":
            return
        candidate_tasks = [
            task
            for task in (*session.new, *session.dirty)
            if isinstance(task, task_model)
            and _status_value(task.__dict__.get("status"))
            in TERMINAL_EVENT_KINDS
        ]
        if not candidate_tasks:
            return
        if dialect_name == "sqlite":
            installed = session.connection().exec_driver_sql(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'trigger' AND name = ? LIMIT 1",
                (_SQLITE_DATABASE_TRIGGER,),
            ).scalar_one_or_none()
            if installed is not None:
                return
        for task in candidate_tasks:
            if not _has_server_managed_lineage(
                task.__dict__.get("context"),
                task.__dict__.get("strategic_operation_receipt_id"),
            ):
                continue
            lineage = task.__dict__["context"][MANAGED_TASK_CONTEXT_KEY]
            receipt_id = task.__dict__["strategic_operation_receipt_id"]
            receipt = next(
                (
                    row
                    for row in session.new
                    if isinstance(row, receipt_model) and row.id == receipt_id
                ),
                None,
            )
            if receipt is None:
                receipt = session.get(receipt_model, receipt_id)
            if (
                receipt is None
                or receipt.project_id != task.project_id
                or str(receipt.turn_task_id) != lineage["source_turn_task_id"]
                or receipt.capability != lineage["capability_id"]
                or receipt.capability not in _MANAGED_CREATE_CAPABILITIES
                or not isinstance(receipt.expected_revisions, dict)
                or receipt.expected_revisions.get("context_hash")
                != lineage["context_hash"]
                or _status_value(receipt.status)
                not in {"applying", "applied", "reconciled"}
            ):
                continue
            status = _status_value(task.__dict__.get("status"))
            if status not in TERMINAL_EVENT_KINDS:
                continue
            if task in session.dirty:
                history = inspect(task).attrs.status.history
                if not history.added or _status_value(history.added[0]) != status:
                    continue
                if history.deleted and _status_value(history.deleted[0]) == status:
                    continue
            session.add(
                StrategicResidentTaskTerminalEvent(
                    project_id=task.project_id,
                    task_id=task.id,
                    event_kind=status,
                )
            )


__all__ = [
    "MANAGED_TASK_CONTEXT_KEY",
    "StrategicResidentTaskTerminalEvent",
    "register_sqlite_terminal_event_listener",
]
