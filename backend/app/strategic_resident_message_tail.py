"""Preserve unrepresented conversation tails across bounded Resident turns."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.artifact_refs import (
    artifact_ref_grants_from_message_context,
    union_resident_artifact_ref_grants,
)
from app.models import MessageSurface, ProjectMessage, StrategicResident, Task
from app.payload_schemas import validate_internal_payload
from app.strategic_operation_authority_validation import (
    StrategicAuthorityError,
    _validated_mutation_mapping,
)
from app.strategic_resident_runtime import (
    coalesce_authoritative_resident_turn,
    is_authoritative_resident_turn,
)


class ReconciliationCursorError(StrategicAuthorityError):
    code = "reconciliation_cursor_error"


def represented_cursors(
    task: Task,
    resident: StrategicResident,
    *,
    represented_message_end: int | None,
) -> tuple[int, dict[str, Any]]:
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    message_end = payload.get("message_sequence_end")
    try:
        message_sequence = (
            int(represented_message_end)
            if message_end is not None and represented_message_end is not None
            else int(resident.handled_message_sequence)
        )
        if message_end is not None and represented_message_end is None:
            raise ReconciliationCursorError(
                "interactive turn represented no unhandled message"
            )
        if message_end is not None and message_sequence > int(message_end):
            raise ReconciliationCursorError("represented message cursor exceeds turn")
    except (TypeError, ValueError):
        raise ReconciliationCursorError("turn message cursor is malformed") from None
    if message_sequence < int(resident.handled_message_sequence):
        raise ReconciliationCursorError("turn message cursor regressed")
    project_cursor = payload.get("project_change_cursor")
    if project_cursor is None:
        project_cursor = resident.handled_project_change_cursor or {}
    if not isinstance(project_cursor, Mapping):
        raise ReconciliationCursorError("turn project-change cursor is malformed")
    return message_sequence, _validated_mutation_mapping(
        "handled_project_change_cursor", project_cursor
    )


async def ensure_message_tail_successor(
    session: AsyncSession,
    resident: StrategicResident,
    tasks: dict[uuid.UUID, Task],
    *,
    represented_end: int,
    requested_end: int,
) -> Task | None:
    """Fold an omitted contiguous tail into the interactive successor."""

    if represented_end >= requested_end:
        return None
    tail_start = represented_end + 1
    successor = tasks.get(resident.interactive_successor_task_id)
    if not is_authoritative_resident_turn(
        successor,
        project_id=resident.project_id,
        mutable_successor=True,
    ):
        wake = await coalesce_authoritative_resident_turn(
            session,
            project_id=resident.project_id,
            lane="interactive",
            trigger_kind="operator_message",
            message_sequence=requested_end,
            locked_resident=resident,
            prelocked_tasks=tasks,
        )
        successor = await session.get(Task, wake.task_id)
        if successor is None:
            raise RuntimeError("Resident conversation-tail successor was not created")
        tasks[successor.id] = successor
    payload = dict(successor.first_payload or {})
    current_start = int(payload.get("message_sequence_start") or requested_end)
    current_end = int(payload.get("message_sequence_end") or requested_end)
    payload["message_sequence_start"] = min(current_start, tail_start)
    payload["message_sequence_end"] = max(current_end, requested_end)
    successor.payloads = [
        validate_internal_payload(payload, "strategic_resident_turn:message_tail")
    ]
    tail_contexts = (
        await session.execute(
            select(ProjectMessage.context).where(
                ProjectMessage.project_id == resident.project_id,
                ProjectMessage.surface == MessageSurface.CONVERSATION,
                ProjectMessage.sequence >= tail_start,
                ProjectMessage.sequence <= requested_end,
            )
        )
    ).scalars()
    tail_grants: set[uuid.UUID] = set()
    for context in tail_contexts:
        tail_grants.update(
            artifact_ref_grants_from_message_context(
                context if isinstance(context, Mapping) else None
            )
        )
    # Preserve only exact refs visible in the omitted tail. Copying every grant
    # from the active turn would let the successor retrieve already-handled chips.
    union_resident_artifact_ref_grants(successor, frozenset(tail_grants))
    session.add(successor)
    return successor


__all__ = [
    "ReconciliationCursorError",
    "ensure_message_tail_successor",
    "represented_cursors",
]
