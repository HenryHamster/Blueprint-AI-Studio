"""Crash-safe recovery mechanics for failed authoritative Resident turns."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.artifact_refs import merge_resident_artifact_ref_grants
from app.broadcast import _sanitize_log
from app.models import (
    Project,
    StrategicOperationReceipt,
    StrategicResident,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.max_plan_pool import clear_task_llm_runtime_context
from app.strategic_resident_agent_binding import (
    RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY,
    RESIDENT_CLAIM_NONCE_CONTEXT_KEY,
    ResidentAgentAuthorityError,
)
from app.strategic_resident_failure_state import failure_backoff_seconds
from app.strategic_resident_promotion import promote_resident_successor
from app.strategic_resident_runtime import is_authoritative_resident_turn
from app.strategic_resident_slot_release import (
    clear_resident_turn_slot_release,
    stage_resident_turn_slot_release,
)
from app.strategic_resident_terminal import commit_release_and_broadcast_failure
from app.strategic_resident_turns import INPUT_FROZEN_CONTEXT_KEY, _task_payload
from app.time_utils import _utcnow, set_retry_after


UNSETTLED_RECEIPT_STATUSES = frozenset(
    {"pending", "applying", "reconciling", "unknown_effect"}
)
@dataclass(frozen=True)
class ResidentFailureMutation:
    promoted_task_id: uuid.UUID | None
    unsettled_receipt_ids: tuple[str, ...]


def _merge_interactive_range(successor: Task, failed: Task) -> None:
    payload = dict(_task_payload(successor))
    failed_payload = _task_payload(failed)
    starts = [
        value
        for value in (
            payload.get("message_sequence_start"),
            failed_payload.get("message_sequence_start"),
        )
        if isinstance(value, int)
    ]
    ends = [
        value
        for value in (
            payload.get("message_sequence_end"),
            failed_payload.get("message_sequence_end"),
        )
        if isinstance(value, int)
    ]
    if starts:
        payload["message_sequence_start"] = min(starts)
    if ends:
        payload["message_sequence_end"] = max(ends)
    payload["trigger_kind"] = "operator_message"
    payload["recovery_source_turn_task_id"] = str(failed.id)
    payload["trigger_count"] = min(
        10_000, int(payload.get("trigger_count") or 1) + 1
    )
    successor.payloads = [payload]
    merge_resident_artifact_ref_grants(successor, failed)
    successor.updated_at = _utcnow()


async def _ensure_interactive_retry(
    session: AsyncSession,
    resident: StrategicResident,
    failed: Task,
    successors: dict[uuid.UUID, Task],
) -> Task:
    successor = successors.get(resident.interactive_successor_task_id)
    created = False
    if not is_authoritative_resident_turn(
        successor,
        project_id=resident.project_id,
        mutable_successor=True,
    ):
        payload = dict(_task_payload(failed))
        payload.update(
            {
                "lane": "interactive",
                "trigger_kind": "operator_message",
                "recovery_source_turn_task_id": str(failed.id),
                "trigger_count": min(
                    10_000, int(payload.get("trigger_count") or 1) + 1
                ),
            }
        )
        context = clear_task_llm_runtime_context(failed.context)
        for key in (
            RESIDENT_CLAIM_NONCE_CONTEXT_KEY,
            RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY,
            "_resident_failure",
        ):
            context.pop(key, None)
        clear_resident_turn_slot_release(context)
        context[INPUT_FROZEN_CONTEXT_KEY] = False
        successor = Task(
            project_id=failed.project_id,
            title="Resident operator turn (retry)",
            description="Bounded retry of an interrupted Resident operator turn",
            payloads=[payload],
            context=context,
            status=TaskStatus.QUEUED,
            priority=max(95, int(failed.priority or 0)),
            created_by="ORCHESTRATOR",
            requires_approval=False,
            archived=True,
        )
        session.add(successor)
        await session.flush()
        session.add(
            TaskHistory(
                task_id=successor.id,
                action="SPAWNED",
                details={
                    "spawned_by": "STRATEGIC_RESIDENT_RECOVERY",
                    "source_turn_task_id": str(failed.id),
                    "lane": "interactive",
                },
            )
        )
        resident.interactive_successor_task_id = successor.id
        successors[successor.id] = successor
        created = True
    if not created:
        _merge_interactive_range(successor, failed)
    session.add(successor)
    return successor


async def load_unsettled_turn_receipts_locked(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    turn_task_id: uuid.UUID,
    authority_generation: int,
) -> tuple[StrategicOperationReceipt, ...]:
    """Lock the bounded per-turn ledger after Project/Resident/Task locks."""
    ledger = (
        (
            await session.execute(
                select(StrategicOperationReceipt)
                .where(
                    StrategicOperationReceipt.project_id == project_id,
                    StrategicOperationReceipt.turn_task_id == turn_task_id,
                    StrategicOperationReceipt.authority_generation
                    == int(authority_generation),
                )
                .order_by(StrategicOperationReceipt.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        row for row in ledger if row.status in UNSETTLED_RECEIPT_STATUSES
    )


async def apply_locked_resident_turn_failure(
    session: AsyncSession,
    *,
    resident: StrategicResident,
    task: Task,
    successors: dict[uuid.UUID, Task],
    unsettled_receipts: tuple[StrategicOperationReceipt, ...],
    failure_id: str,
    reason: str,
    failure_category: str,
) -> ResidentFailureMutation:
    """Mutate one already-fenced failure without external I/O or hidden retries."""
    now = _utcnow()
    payload = _task_payload(task)
    lane = str(payload.get("lane") or resident.active_turn_lane or "background")
    safe_reason = _sanitize_log(reason)[:5_000]
    unsettled_ids = tuple(str(row.id) for row in unsettled_receipts)
    context = dict(task.context or {})
    claim_nonce = str(context.get(RESIDENT_CLAIM_NONCE_CONTEXT_KEY) or "")
    context.pop(RESIDENT_CLAIM_NONCE_CONTEXT_KEY, None)
    context.pop(RESIDENT_CLAIM_ATTEMPT_CONTEXT_KEY, None)
    context["_resident_failure"] = {
        "failure_id": failure_id,
        "reason": safe_reason,
        "category": failure_category,
        "failed_at": now.isoformat(),
        "unsettled_receipt_ids": list(unsettled_ids),
        "claim_nonce_hash": (
            hashlib.sha256(claim_nonce.encode()).hexdigest() if claim_nonce else None
        ),
    }
    task.context = context
    task.status = TaskStatus.FAILED
    task.completed_at = now
    task.updated_at = now
    task.last_heartbeat = None
    if task.vm_provisioned_at:
        task.vm_stopped_at = now
    task.latest_status_message = f"Resident turn failed: {safe_reason[:500]}"
    session.add(task)
    session.add(
        TaskHistory(
            task_id=task.id,
            action="RESIDENT_TURN_FAILED",
            details={
                "failure_id": failure_id,
                "reason": safe_reason[:1_000],
                "category": failure_category,
                "unsettled_receipt_ids": list(unsettled_ids),
            },
        )
    )
    resident.active_turn_task_id = None
    resident.active_turn_lane = None
    health = dict(resident.health_state or {})
    failure_count = min(int(health.get("consecutive_turn_failures") or 0) + 1, 100)
    retry_limit = max(
        1,
        min(int((resident.provider_policy or {}).get("failure_retry_limit") or 5), 20),
    )
    health.update(
        {
            "last_turn_failure_at": now.isoformat(),
            "last_turn_failure_task_id": str(task.id),
            "last_turn_failure_id": failure_id,
            "last_turn_failure_category": failure_category,
            "consecutive_turn_failures": failure_count,
        }
    )
    resident.health_state = health
    if lane == "interactive":
        await _ensure_interactive_retry(session, resident, task, successors)

    retry_allowed = not unsettled_ids and failure_count < retry_limit
    interactive = successors.get(resident.interactive_successor_task_id)
    may_promote = is_authoritative_resident_turn(
        interactive,
        project_id=resident.project_id,
        mutable_successor=True,
    ) or resident.background_policy == "active"
    promoted = (
        promote_resident_successor(
            resident,
            successors,
            mutation_authorized=True,
        )
        if retry_allowed and may_promote
        else None
    )
    recovery = dict(resident.recovery_state or {})
    recovery.update(
        {
            "status": (
                "unknown_effect"
                if unsettled_ids
                else "retrying"
                if retry_allowed
                else "attention"
            ),
            "failure_category": failure_category,
            "failure_count": failure_count,
            "retry_limit": retry_limit,
            "source_turn_task_id": str(task.id),
            "unknown_operation_receipt_ids": list(unsettled_ids),
            "updated_at": now.isoformat(),
        }
    )
    resident.recovery_state = recovery
    delay_seconds = failure_backoff_seconds(failure_category, failure_count)
    if promoted is not None:
        set_retry_after(promoted, delay_seconds / 60)
        resident.next_wakeup_at = None
        resident.health_status = "recovering"
    elif (
        lane != "interactive"
        and retry_allowed
        and resident.background_policy == "active"
    ):
        resident.next_wakeup_at = now + timedelta(seconds=delay_seconds)
        resident.health_status = "recovering"
    else:
        resident.next_wakeup_at = None
        resident.health_status = "blocked"
    failure_marker = dict(context["_resident_failure"])
    failure_marker["promoted_task_id"] = (
        str(promoted.id) if promoted is not None else None
    )
    context["_resident_failure"] = failure_marker
    task.context = context
    stage_resident_turn_slot_release(task)
    resident.updated_at = now
    session.add(resident)
    return ResidentFailureMutation(
        promoted_task_id=promoted.id if promoted is not None else None,
        unsettled_receipt_ids=unsettled_ids,
    )


async def reap_authoritative_resident_turn(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    reap_reason: str,
    reap_reason_code: str,
) -> ResidentFailureMutation:
    """Fence and recover a post-claim Resident orphan in canonical lock order."""
    project = (
        await session.execute(
            select(Project).where(Project.id == project_id).with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    resident = (
        await session.execute(
            select(StrategicResident)
            .where(StrategicResident.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    task = (
        await session.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None or resident is None or task is None:
        raise ResidentAgentAuthorityError("Resident authority is stale")
    payload = _task_payload(task)
    if (
        resident.engine_owner != "strategic_resident"
        or resident.active_turn_task_id != task.id
        or task.status != TaskStatus.IN_PROGRESS
        or not is_authoritative_resident_turn(task, project_id=project_id)
        or int(payload.get("authority_generation") or -1)
        != int(resident.authority_generation)
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
    successors: dict[uuid.UUID, Task] = {}
    if pointer_ids:
        rows = (
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
        successors = {row.id: row for row in rows}
    receipts = await load_unsettled_turn_receipts_locked(
        session,
        project_id=project_id,
        turn_task_id=task_id,
        authority_generation=int(resident.authority_generation),
    )
    mutation = await apply_locked_resident_turn_failure(
        session,
        resident=resident,
        task=task,
        successors=successors,
        unsettled_receipts=receipts,
        failure_id=f"reaper:{task_id}:{reap_reason_code}",
        reason=reap_reason,
        failure_category=reap_reason_code,
    )
    await commit_release_and_broadcast_failure(
        session,
        task_id=task_id,
        project_id=project_id,
        suppress_broadcast_error=True,
    )
    return mutation


__all__ = [
    "UNSETTLED_RECEIPT_STATUSES",
    "ResidentFailureMutation",
    "apply_locked_resident_turn_failure",
    "load_unsettled_turn_receipts_locked",
    "reap_authoritative_resident_turn",
]
