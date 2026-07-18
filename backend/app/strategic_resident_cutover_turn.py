"""Seed pending conversation work inside Resident ownership cutover."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StrategicResident, Task
from app.payload_schemas import validate_internal_payload
from app.strategic_resident_runtime import (
    AuthoritativeResidentOwnershipError,
    _create_authoritative_turn,
    _json_safe,
)
from app.strategic_resident_turns import _task_payload
from app.time_utils import _utcnow


async def seed_cutover_conversation_turn(
    session: AsyncSession,
    resident: StrategicResident,
    *,
    message_sequence_start: int,
    message_sequence_end: int,
) -> Task:
    """Seed the first authoritative interactive turn during ownership cutover."""

    if resident.engine_owner != "strategic_resident":
        raise AuthoritativeResidentOwnershipError(
            "cutover turn requires Strategic Resident ownership"
        )
    if message_sequence_start < 1 or message_sequence_end < message_sequence_start:
        raise ValueError("cutover conversation range is malformed")
    if any(
        pointer is not None
        for pointer in (
            resident.active_turn_task_id,
            resident.interactive_successor_task_id,
            resident.background_successor_task_id,
        )
    ):
        raise RuntimeError("cutover conversation turn requires empty pointers")
    task = await _create_authoritative_turn(
        session,
        resident,
        lane="interactive",
        trigger_kind="operator_message",
        message_sequence=message_sequence_end,
        project_change_cursor=None,
        input_frozen=True,
        # A cutover conversation turn is the first authoritative turn after
        # ownership flips. Context reconstruction replaces this bootstrap set
        # with only the exact ArtifactRefs represented after bounded fitting;
        # starting empty avoids granting refs from an omitted conversation tail.
        artifact_ref_grants=frozenset(),
    )
    payload = dict(_task_payload(task))
    payload["message_sequence_start"] = int(message_sequence_start)
    payload["message_sequence_end"] = int(message_sequence_end)
    task.payloads = [
        _json_safe(
            validate_internal_payload(
                payload,
                "strategic_resident_turn:cutover_conversation",
            )
        )
    ]
    resident.active_turn_task_id = task.id
    resident.active_turn_lane = "interactive"
    resident.last_wakeup_at = _utcnow()
    session.add(task)
    session.add(resident)
    return task


__all__ = ["seed_cutover_conversation_turn"]
