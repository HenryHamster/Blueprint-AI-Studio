"""Bounded, idempotent failure reconciliation for Resident turn workers."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Project, StrategicResident, Task, TaskStatus
from app.strategic_resident_agent_binding import (
    ResidentAgentAuthorityError,
    ResidentAgentBinding,
    ResidentAgentTaskNotFound,
    validate_resident_agent_binding,
)
from app.strategic_resident_failure_recovery import (
    apply_locked_resident_turn_failure,
    load_unsettled_turn_receipts_locked,
)
from app.strategic_resident_failure_state import (
    reset_resident_failure_recovery,
    stored_promoted_task_id,
)
from app.strategic_resident_terminal import (
    commit_and_release_failure_replay,
    commit_release_and_broadcast_failure,
)


@dataclass(frozen=True)
class ResidentTurnFailureOutcome:
    task_id: uuid.UUID
    idempotent_replay: bool
    promoted_task_id: uuid.UUID | None = None


async def fail_authoritative_resident_turn(
    session: AsyncSession,
    *,
    binding: ResidentAgentBinding,
    failure_id: str,
    reason: str,
    failure_category: str,
) -> ResidentTurnFailureOutcome:
    """Fence the live turn, persist one failure, and clear its active pointer."""

    project = (
        await session.execute(
            select(Project)
            .where(Project.id == binding.project_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == binding.project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    task = (
        await session.execute(
            select(Task)
            .where(Task.id == binding.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None or resident is None:
        raise ResidentAgentAuthorityError("Resident authority is stale")
    if task is None:
        raise ResidentAgentTaskNotFound("Resident turn not found")
    payload = task.first_payload if isinstance(task.first_payload, dict) else {}
    failure = (task.context or {}).get("_resident_failure")
    if (
        task.project_id != binding.project_id
        or resident.engine_owner != "strategic_resident"
        or int(payload.get("authority_generation") or -1)
        != int(resident.authority_generation)
    ):
        raise ResidentAgentAuthorityError("Resident turn authority is stale")
    if (
        task.status == TaskStatus.FAILED
        and isinstance(failure, dict)
        and failure.get("failure_id") == failure_id
        and resident.active_turn_task_id != task.id
        and task.id == binding.task_id
        and task.project_id == binding.project_id
        and task.vm_instance_id == binding.vm_instance_id
        and binding.task_type == "strategic_resident_turn"
        and bool(binding.claim_nonce)
        and hmac.compare_digest(
            hashlib.sha256(binding.claim_nonce.encode()).hexdigest(),
            str(failure.get("claim_nonce_hash") or ""),
        )
    ):
        await commit_and_release_failure_replay(
            session,
            task_id=task.id,
            project_id=task.project_id,
        )
        return ResidentTurnFailureOutcome(
            task.id,
            True,
            stored_promoted_task_id(failure),
        )
    validate_resident_agent_binding(
        task,
        binding,
        allowed_statuses=frozenset({TaskStatus.IN_PROGRESS}),
    )
    if (
        task.status != TaskStatus.IN_PROGRESS
        or resident.active_turn_task_id != task.id
    ):
        raise ResidentAgentAuthorityError("Resident turn authority is stale")

    pointer_ids = {
        pointer
        for pointer in (
            resident.interactive_successor_task_id,
            resident.background_successor_task_id,
        )
        if pointer is not None
    }
    successors = {}
    if pointer_ids:
        successor_rows = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.id.in_(pointer_ids))
                    .order_by(Task.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        successors = {row.id: row for row in successor_rows}

    receipts = await load_unsettled_turn_receipts_locked(
        session,
        project_id=binding.project_id,
        turn_task_id=binding.task_id,
        authority_generation=int(resident.authority_generation),
    )
    mutation = await apply_locked_resident_turn_failure(
        session,
        resident=resident,
        task=task,
        successors=successors,
        unsettled_receipts=receipts,
        failure_id=failure_id,
        reason=reason,
        failure_category=failure_category,
    )
    task_id = task.id
    project_id = task.project_id
    await commit_release_and_broadcast_failure(
        session,
        task_id=task_id,
        project_id=project_id,
        suppress_broadcast_error=False,
    )
    return ResidentTurnFailureOutcome(
        task_id,
        False,
        mutation.promoted_task_id,
    )


__all__ = [
    "ResidentTurnFailureOutcome",
    "fail_authoritative_resident_turn",
    "reset_resident_failure_recovery",
]
