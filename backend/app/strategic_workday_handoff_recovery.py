"""Privacy-safe terminal recovery for orphaned typed Workday requests."""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.message_audience import PIPELINE_LOG, STRATEGIST_INTERNAL, stamp_message_audience
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    StrategicMemorySource,
    Task,
)
from app.project_message_creation import (
    build_project_message,
    lock_project_and_next_message_sequence,
)
from app.strategic_conversation_query import strategic_conversation_predicate
from app.time_utils import _utcnow

_ATOMIC_REPLY_ID = "_workday_consultation_atomic_reply_id"
_PACKET_MARKER = "_conversation_consultation_packet"
_RESULT_MARKER = "_workday_consultation_result"
_REQUESTED_MARKER = "_workday_consultation_requested"
_RECOVERY_CURSOR = "_workday_consultation_recovery_before_sequence"
_RECOVERY_LIMIT = 32
PRIVATE_HANDOFF_RETIREMENT_LIMIT = 8
_PRIVATE_HANDOFF_RETIREMENT_CONTEXT_KEY = "_private_handoff_retirement"
_PRIVATE_HANDOFF_RETIREMENT_REASON = "context_envelope_unavailable"
_PRIVATE_HANDOFF_CLAIM_CONTEXT_KEYS = (
    "_workday_consultation_claim_id",
    "_workday_consultation_claim_generation",
    "_workday_consultation_claim_incarnation",
    "_workday_consultation_claim_expires_at",
    "_workday_consultation_claim_attempt",
    "_workday_continuation_claim_id",
    "_workday_continuation_claim_generation",
    "_workday_continuation_claim_incarnation",
    "_workday_continuation_claim_expires_at",
    "_workday_continuation_claim_attempt",
)
_RECOVERY_SUMMARY = (
    "The private Workday consultation became unavailable before a usable result "
    "could be delivered. This failure notice itself does not verify whether any "
    "managed task or state change occurred; only separately listed durable "
    "receipts and current canonical state do."
)

logger = logging.getLogger(__name__)


def _source_shape_is_canonical(source: ProjectMessage, *, session_id: str) -> bool:
    context = source.context if isinstance(source.context, dict) else {}
    return bool(
        source.sequence is not None
        and source.sequence > 0
        and source.surface == MessageSurface.CONVERSATION
        and source.from_type == "strategic"
        and source.resident_session_id == session_id
        and context.get("message_role") == "operator_reply"
        and context.get(_REQUESTED_MARKER) is True
        and context.get("_conversation_public_reply_owner") == "fast"
        and context.get("audience") not in {STRATEGIST_INTERNAL, PIPELINE_LOG}
        and context.get("privacy_state") not in {"redacted", "deleted"}
    )


def _source_receipt_is_current(
    source: ProjectMessage,
    receipt: StrategicMemorySource | None,
    *,
    session_id: str,
) -> bool:
    return bool(
        receipt is not None
        and receipt.payload_state == "active"
        and receipt.tombstoned_at is None
        and receipt.source_revision == str(source.sequence)
        and receipt.native_sequence == source.sequence
        and receipt.resident_session_id == session_id
        and receipt.actor_kind == "strategic"
        and (receipt.source_metadata or {}).get("surface") == "conversation"
        and receipt.content_hash
        == hashlib.sha256(source.content.encode("utf-8")).hexdigest()
    )


def _internal_handoff_is_live(row: ProjectMessage, *, session_id: str) -> bool:
    context = row.context if isinstance(row.context, dict) else {}
    return bool(
        row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.status == "open"
        and row.resident_session_id == session_id
        and row.handling_state
        in {MessageHandlingState.PENDING, MessageHandlingState.CLAIMED}
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("public_reply_owner") == "fast"
        and (
            context.get(_PACKET_MARKER) is True
            or context.get(_RESULT_MARKER) is True
        )
    )


async def _canonical_source_receipt(
    session: AsyncSession,
    *,
    source: ProjectMessage,
    session_id: str,
) -> StrategicMemorySource | None:
    receipt = (
        await session.execute(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == source.project_id,
                StrategicMemorySource.source_type == "project_message",
                StrategicMemorySource.source_key == str(source.id),
            )
        )
    ).scalar_one_or_none()
    return receipt if _source_receipt_is_current(source, receipt, session_id=session_id) else None


async def _source_is_settled_or_live(
    session: AsyncSession,
    *,
    source: ProjectMessage,
    session_id: str,
) -> bool:
    linked = (
        (
            await session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == source.project_id,
                    ProjectMessage.context[_ATOMIC_REPLY_ID].as_string()
                    == str(source.id),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in linked:
        context = row.context if isinstance(row.context, dict) else {}
        if (
            row.from_type == "strategic"
            and row.surface == MessageSurface.CONVERSATION
            and context.get("message_role") == "workday_consultation_continuation"
            and context.get("privacy_state") not in {"redacted", "deleted"}
        ):
            return True
        if _internal_handoff_is_live(row, session_id=session_id):
            return True
    return False


async def _create_recovery_result(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    source: ProjectMessage,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    session_id: str,
) -> ProjectMessage:
    context = source.context if isinstance(source.context, dict) else {}
    try:
        claim_id = uuid.UUID(str(context.get("_conversation_claim_id")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("consultation source claim receipt is invalid") from exc
    idempotency_key = f"workday-consultation-terminal-recovery:{source.id}"
    existing = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    from app.strategic_fast_live_work import _fast_live_work_authority
    from app.strategic_memory_envelope import (
        build_and_store_context_envelope_snapshot,
        context_envelope_reference,
        pin_fenced_context_envelope_for_packet,
    )

    authority = await _fast_live_work_authority(
        session, project=project, resident_task_id=task.id
    )
    envelope = await build_and_store_context_envelope_snapshot(
        session,
        project_id=project.id,
        session_id=session_id,
        claim_id=claim_id,
        worker_generation=worker_generation,
        worker_incarnation=worker_incarnation,
        input_revision=int(source.sequence or 0),
        transcript_sequence=int(source.sequence or 0),
        decision_authority=authority,
        retrieval_trace={"workday_terminal_recovery": True},
        memory_query=source.content,
    )
    pinned = await pin_fenced_context_envelope_for_packet(
        session,
        project_id=project.id,
        reference=context_envelope_reference(envelope),
    )
    if pinned is None:
        raise ValueError("terminal Workday recovery envelope could not be pinned")
    sequence = await lock_project_and_next_message_sequence(session, project.id)
    result = await build_project_message(
        session,
        project_id=project.id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        message_type=MessageType.NOTE,
        from_type="system",
        content=_RECOVERY_SUMMARY,
        context=stamp_message_audience(
            {
                _RESULT_MARKER: True,
                "source_packet_id": str(source.id),
                "claim_id": str(claim_id),
                "delivery_key": "terminal-recovery",
                "_conversation_consultation_continuation": "terminal-recovery",
                "result_state": "failed",
                "message_role": "workday_consultation_result",
                "public_reply_owner": "fast",
                "envelope": context_envelope_reference(pinned),
                _ATOMIC_REPLY_ID: str(source.id),
                "_workday_terminal_recovery": True,
            },
            "workday_consultation_result",
            message_type=MessageType.NOTE.value,
        ),
        idempotency_key=idempotency_key,
        resident_session_id=session_id,
    )
    session.add(result)
    await session.flush()
    return result


async def recover_workday_request_by_source_id(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    source_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    session_id: str,
) -> ProjectMessage | None:
    source = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.id == source_id,
                strategic_conversation_predicate(),
            )
        )
    ).scalar_one_or_none()
    if (
        source is None
        or not _source_shape_is_canonical(source, session_id=session_id)
        or await _canonical_source_receipt(
            session, source=source, session_id=session_id
        )
        is None
        or await _source_is_settled_or_live(
            session, source=source, session_id=session_id
        )
    ):
        return None
    return await _create_recovery_result(
        session,
        project=project,
        task=task,
        source=source,
        worker_generation=worker_generation,
        worker_incarnation=worker_incarnation,
        session_id=session_id,
    )


async def retire_unavailable_private_handoff(
    session: AsyncSession,
    *,
    row: ProjectMessage,
) -> None:
    """Terminalize a private queue row whose fenced prompt no longer exists."""
    now = _utcnow()
    context = dict(row.context or {})
    for key in _PRIVATE_HANDOFF_CLAIM_CONTEXT_KEYS:
        context.pop(key, None)
    context[_PRIVATE_HANDOFF_RETIREMENT_CONTEXT_KEY] = {
        "reason": _PRIVATE_HANDOFF_RETIREMENT_REASON,
        "retired_at": now.isoformat(),
    }
    row.context = context
    row.status = "resolved"
    row.handling_state = MessageHandlingState.SUPERSEDED
    row.handled_by_task_id = None
    row.resolution = "private_handoff_context_envelope_unavailable"
    row.resolved_at = now
    row.updated_at = now
    session.add(row)
    await session.flush()


async def recover_retired_private_handoff(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    row: ProjectMessage,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    resident_session_id: str,
) -> bool:
    """Create the neutral Fast-visible recovery for one retired private row."""
    source_id = workday_handoff_source_id(row)
    if source_id is None:
        return False
    try:
        recovered = await recover_workday_request_by_source_id(
            session,
            project=project,
            task=task,
            source_id=source_id,
            worker_generation=worker_generation,
            worker_incarnation=worker_incarnation,
            session_id=resident_session_id,
        )
    except ValueError:
        logger.warning(
            "[STRATEGIC] Could not yet create neutral terminal recovery for "
            "Workday request %s",
            source_id,
            exc_info=True,
        )
        return False
    return recovered is not None


async def recover_one_missing_workday_request(
    session: AsyncSession,
    *,
    project: Project,
    task: Task,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    session_id: str,
) -> ProjectMessage | None:
    task_context = dict(task.context or {})
    before_raw = task_context.get(_RECOVERY_CURSOR)
    before = before_raw if isinstance(before_raw, int) and before_raw > 0 else None
    statement = select(ProjectMessage).where(
        ProjectMessage.project_id == project.id,
        ProjectMessage.from_type == "strategic",
        ProjectMessage.resident_session_id == session_id,
        ProjectMessage.context[_REQUESTED_MARKER].as_boolean().is_(True),
        strategic_conversation_predicate(),
    )
    if before is not None:
        statement = statement.where(ProjectMessage.sequence < before)
    sources = (
        (
            await session.execute(
                statement.order_by(ProjectMessage.sequence.desc(), ProjectMessage.id)
                .limit(_RECOVERY_LIMIT)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for source in sources:
        recovered = await recover_workday_request_by_source_id(
            session,
            project=project,
            task=task,
            source_id=source.id,
            worker_generation=worker_generation,
            worker_incarnation=worker_incarnation,
            session_id=session_id,
        )
        if recovered is not None:
            task_context.pop(_RECOVERY_CURSOR, None)
            task.context = task_context
            flag_modified(task, "context")
            session.add(task)
            return recovered
    if len(sources) == _RECOVERY_LIMIT:
        task_context[_RECOVERY_CURSOR] = min(int(row.sequence or 0) for row in sources)
    else:
        task_context.pop(_RECOVERY_CURSOR, None)
    task.context = task_context
    flag_modified(task, "context")
    session.add(task)
    return None


def workday_handoff_source_id(row: ProjectMessage) -> uuid.UUID | None:
    context = row.context if isinstance(row.context, dict) else {}
    try:
        return uuid.UUID(str(context.get(_ATOMIC_REPLY_ID)))
    except (TypeError, ValueError, AttributeError):
        return None


__all__ = [
    "PRIVATE_HANDOFF_RETIREMENT_LIMIT",
    "recover_one_missing_workday_request",
    "recover_retired_private_handoff",
    "recover_workday_request_by_source_id",
    "retire_unavailable_private_handoff",
    "workday_handoff_source_id",
]
