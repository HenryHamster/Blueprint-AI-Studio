"""Privacy-safe terminal recovery for orphaned typed Workday requests."""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, or_, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.message_audience import PIPELINE_LOG, STRATEGIST_INTERNAL, stamp_message_audience
from app.database import resolve_dialect_name
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    Project,
    ProjectMessage,
    ProjectMessageSequence,
    StrategicMemorySource,
    Task,
)
from app.project_message_creation import (
    build_project_message,
    lock_project_and_next_message_sequence,
)
from app.session_message_handoff import (
    RESIDENT_HANDOFF_AT_CONTEXT_KEY,
    RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY,
)
from app.strategic_conversation_query import strategic_conversation_predicate
from app.strategic_autonomous_handoff_lineage import (
    autonomous_workday_result_lineage_valid,
)
from app.time_utils import _utcnow

_ATOMIC_REPLY_ID = "_workday_consultation_atomic_reply_id"
_PACKET_MARKER = "_conversation_consultation_packet"
_RESULT_MARKER = "_workday_consultation_result"
_REQUESTED_MARKER = "_workday_consultation_requested"
_RECOVERY_CURSOR = "_workday_consultation_recovery_before_sequence"
_RECOVERY_SCAN_CEILING = "_workday_consultation_recovery_scan_ceiling"
_RECOVERY_HIGH_WATERMARK = "_workday_consultation_recovery_high_watermark"
_RECOVERY_RETRY_SOURCE_IDS = "_workday_consultation_recovery_retry_source_ids"
_RECOVERY_LIMIT = 32
_RECOVERY_RETRY_LIMIT = 32
PRIVATE_HANDOFF_RETIREMENT_LIMIT = 8
_PRIVATE_HANDOFF_RETIREMENT_CONTEXT_KEY = "_private_handoff_retirement"
_PRIVATE_HANDOFF_RETIREMENT_REASON = "context_envelope_unavailable"
_PRIVATE_HANDOFF_PROVENANCE_INVALID_REASON = "continuation_provenance_invalid"
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


@dataclass(frozen=True)
class WorkdayResultServerLineage:
    """Canonical public source and receipt authority selected by server keys."""

    public_source_id: uuid.UUID | None
    authority_row_id: uuid.UUID
    kind: str
    projections_valid: bool = True


def _recovery_retry_source_ids(context: dict[str, object]) -> list[uuid.UUID]:
    raw = context.get(_RECOVERY_RETRY_SOURCE_IDS)
    if not isinstance(raw, list):
        return []
    recovered: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for item in raw[:_RECOVERY_RETRY_LIMIT]:
        try:
            source_id = uuid.UUID(str(item))
        except (TypeError, ValueError, AttributeError):
            continue
        if source_id not in seen:
            seen.add(source_id)
            recovered.append(source_id)
    return recovered


def _store_recovery_retry_source_ids(
    task: Task,
    context: dict[str, object],
    source_ids: list[uuid.UUID],
) -> None:
    if source_ids:
        context[_RECOVERY_RETRY_SOURCE_IDS] = [str(item) for item in source_ids]
    else:
        context.pop(_RECOVERY_RETRY_SOURCE_IDS, None)
    task.context = context
    flag_modified(task, "context")


def _source_shape_is_canonical(source: ProjectMessage) -> bool:
    context = source.context if isinstance(source.context, dict) else {}
    source_session_id = str(source.resident_session_id or "").strip()
    handoff_session_id = str(
        context.get(RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY) or ""
    ).strip()
    return bool(
        source.sequence is not None
        and source.sequence > 0
        and source.surface == MessageSurface.CONVERSATION
        and source.from_type == "strategic"
        and source.message_type == MessageType.NOTE
        and source.status == "resolved"
        and source.handling_state == MessageHandlingState.RESPONDED
        and bool(source_session_id or handoff_session_id)
        and context.get("message_role") == "operator_reply"
        and context.get(_REQUESTED_MARKER) is True
        and context.get("_conversation_public_reply_owner") == "fast"
        and context.get("audience") not in {STRATEGIST_INTERNAL, PIPELINE_LOG}
        and context.get("privacy_state") not in {"redacted", "deleted"}
    )


def _source_receipt_is_current(
    source: ProjectMessage,
    receipt: StrategicMemorySource | None,
) -> bool:
    context = source.context if isinstance(source.context, dict) else {}
    source_session_id = str(source.resident_session_id or "").strip()
    handoff_session_id = str(
        context.get(RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY) or ""
    ).strip()
    receipt_session_id = str(
        receipt.resident_session_id if receipt is not None else ""
    ).strip()
    return bool(
        receipt is not None
        and receipt.payload_state == "active"
        and receipt.tombstoned_at is None
        and receipt.source_revision == str(source.sequence)
        and receipt.native_sequence == source.sequence
        and bool(receipt_session_id)
        and (
            receipt_session_id == source_session_id
            or (
                not source_session_id
                and receipt_session_id == handoff_session_id
            )
        )
        and receipt.actor_kind == "strategic"
        and (receipt.source_metadata or {}).get("surface") == "conversation"
        and receipt.content_hash
        == hashlib.sha256(source.content.encode("utf-8")).hexdigest()
    )


def _retired_terminal_recovery_is_restorable(
    row: ProjectMessage,
    *,
    context: dict[str, object],
) -> bool:
    """Accept only a pristine recovery row retired for envelope loss alone."""

    from app.strategic_workday_mutation_rebind import (
        WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY,
        WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY,
        WORKDAY_OPERATION_LEDGER_CONTEXT_KEY,
    )

    retirement = context.get(_PRIVATE_HANDOFF_RETIREMENT_CONTEXT_KEY)
    retired_at = retirement.get("retired_at") if isinstance(retirement, dict) else None
    try:
        parsed_retired_at = datetime.fromisoformat(str(retired_at))
    except (TypeError, ValueError):
        return False
    return bool(
        parsed_retired_at
        and row.message_type == MessageType.NOTE
        and row.status == "resolved"
        and row.handling_state == MessageHandlingState.SUPERSEDED
        and row.handled_by_task_id is None
        and row.resolved_at is not None
        and row.resolution == "private_handoff_context_envelope_unavailable"
        and row.conversation_claim_id is None
        and row.conversation_claim_generation is None
        and row.conversation_claim_incarnation is None
        and row.conversation_claim_expires_at is None
        and isinstance(retirement, dict)
        and retirement.get("reason") == _PRIVATE_HANDOFF_RETIREMENT_REASON
        and isinstance(retired_at, str)
        and bool(retired_at.strip())
        and "_workday_continuation_completion_idempotency_key" not in context
        and WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY not in context
        and WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY not in context
        and WORKDAY_OPERATION_LEDGER_CONTEXT_KEY not in context
        and not any(key in context for key in _PRIVATE_HANDOFF_CLAIM_CONTEXT_KEYS)
    )


def _retired_invalid_terminal_recovery_is_rebuildable(
    row: ProjectMessage,
    *,
    context: dict[str, object],
) -> bool:
    """Accept a quarantined terminal row only when replacement is safe.

    Unlike the envelope-loss path, provenance quarantine means the old context
    and content are deliberately untrusted. No payload field is retained: a
    caller that independently re-established the canonical public source may
    create a fresh constant-summary row while preserving this incident row.
    """

    retirement = context.get(_PRIVATE_HANDOFF_RETIREMENT_CONTEXT_KEY)
    retired_at = retirement.get("retired_at") if isinstance(retirement, dict) else None
    try:
        parsed_retired_at = datetime.fromisoformat(str(retired_at))
    except (TypeError, ValueError):
        return False
    return bool(
        parsed_retired_at
        and row.message_type == MessageType.NOTE
        and row.status == "resolved"
        and row.handling_state == MessageHandlingState.SUPERSEDED
        and row.handled_by_task_id is None
        and row.resolved_at is not None
        and row.resolution == "private_handoff_continuation_provenance_invalid"
        and row.conversation_claim_id is None
        and row.conversation_claim_generation is None
        and row.conversation_claim_incarnation is None
        and row.conversation_claim_expires_at is None
        and isinstance(retirement, dict)
        and retirement.get("reason") == _PRIVATE_HANDOFF_PROVENANCE_INVALID_REASON
        and isinstance(retired_at, str)
        and bool(retired_at.strip())
        and "_workday_continuation_completion_idempotency_key" not in context
        and not any(key in context for key in _PRIVATE_HANDOFF_CLAIM_CONTEXT_KEYS)
    )


async def _canonical_source_receipt(
    session: AsyncSession,
    *,
    source: ProjectMessage,
) -> StrategicMemorySource | None:
    receipt = (
        await session.execute(
            select(StrategicMemorySource).where(
                StrategicMemorySource.project_id == source.project_id,
                StrategicMemorySource.source_type == "project_message",
                StrategicMemorySource.source_key == str(source.id),
                StrategicMemorySource.source_revision == str(source.sequence),
                StrategicMemorySource.native_sequence == source.sequence,
            )
        )
    ).scalar_one_or_none()
    return receipt if _source_receipt_is_current(source, receipt) else None


async def _canonical_public_workday_source(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    claim_id: uuid.UUID,
) -> ProjectMessage | None:
    source = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == source_id,
                strategic_conversation_predicate(),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    source_context = (
        source.context if source is not None and isinstance(source.context, dict) else {}
    )
    if (
        source is None
        or not _source_shape_is_canonical(source)
        or source_context.get("_conversation_claim_id") != str(claim_id)
        or await _canonical_source_receipt(session, source=source) is None
    ):
        return None
    return source


async def _source_is_settled_or_live(
    session: AsyncSession,
    *,
    source: ProjectMessage,
) -> bool:
    settled_by_source = await _relational_settlement_by_source(
        session,
        project_id=source.project_id,
        source_ids={source.id},
    )
    return settled_by_source.get(source.id, False)


def _json_boolean_true(row: object, key: str, *, dialect: str) -> object:
    context = row.context  # type: ignore[attr-defined]
    if dialect == "postgresql":
        value = context[key]
        return and_(
            func.jsonb_typeof(value) == "boolean",
            value.as_boolean().is_(True),
        )
    if dialect == "sqlite":
        return func.json_type(context, f"$.{key}") == "true"
    raise RuntimeError(f"Unsupported handoff recovery SQL dialect: {dialect}")


def _json_nonempty_string(row: object, key: str, *, dialect: str) -> object:
    context = row.context  # type: ignore[attr-defined]
    if dialect == "postgresql":
        value = context[key]
        return and_(
            func.jsonb_typeof(value) == "string",
            value.as_string() != "",
        )
    if dialect == "sqlite":
        return and_(
            func.json_type(context, f"$.{key}") == "text",
            func.json_extract(context, f"$.{key}") != "",
        )
    raise RuntimeError(f"Unsupported handoff recovery SQL dialect: {dialect}")


def _json_string_equals(
    row: object,
    key: str,
    expected: str,
    *,
    dialect: str,
) -> object:
    context = row.context  # type: ignore[attr-defined]
    if dialect == "postgresql":
        value = context[key]
        return and_(
            func.jsonb_typeof(value) == "string",
            value.as_string() == expected,
        )
    if dialect == "sqlite":
        return and_(
            func.json_type(context, f"$.{key}") == "text",
            func.json_extract(context, f"$.{key}") == expected,
        )
    raise RuntimeError(f"Unsupported handoff recovery SQL dialect: {dialect}")


def _privacy_state_is_public(row: object, *, dialect: str) -> object:
    context = row.context  # type: ignore[attr-defined]
    if dialect == "postgresql":
        value = context["privacy_state"]
        value_type = func.jsonb_typeof(value)
        return or_(
            value_type.is_(None),
            value_type == "null",
            and_(
                value_type == "string",
                value.as_string().not_in(("redacted", "deleted")),
            ),
        )
    if dialect == "sqlite":
        value_type = func.json_type(context, "$.privacy_state")
        return or_(
            value_type.is_(None),
            value_type == "null",
            and_(
                value_type == "text",
                func.json_extract(context, "$.privacy_state").not_in(
                    ("redacted", "deleted")
                ),
            ),
        )
    raise RuntimeError(f"Unsupported handoff recovery SQL dialect: {dialect}")


def _settled_or_live_predicate(row: object, *, dialect: str) -> object:
    public_continuation = and_(
        row.from_type == "strategic",  # type: ignore[attr-defined]
        row.surface == MessageSurface.CONVERSATION,  # type: ignore[attr-defined]
        _json_string_equals(
            row,
            "message_role",
            "workday_consultation_continuation",
            dialect=dialect,
        ),
        _privacy_state_is_public(row, dialect=dialect),
    )
    completed_result = and_(
        row.from_type == "system",  # type: ignore[attr-defined]
        row.surface == MessageSurface.CONVERSATION,  # type: ignore[attr-defined]
        row.status == "handled",  # type: ignore[attr-defined]
        row.handling_state == MessageHandlingState.RESPONDED,  # type: ignore[attr-defined]
        _json_boolean_true(row, _RESULT_MARKER, dialect=dialect),
        _json_nonempty_string(
            row,
            "_workday_continuation_completion_idempotency_key",
            dialect=dialect,
        ),
    )
    live_private_handoff = and_(
        row.from_type == "system",  # type: ignore[attr-defined]
        row.surface == MessageSurface.CONVERSATION,  # type: ignore[attr-defined]
        row.status == "open",  # type: ignore[attr-defined]
        row.handling_state.in_(  # type: ignore[attr-defined]
            (MessageHandlingState.PENDING, MessageHandlingState.CLAIMED)
        ),
        _json_string_equals(
            row,
            "audience",
            STRATEGIST_INTERNAL,
            dialect=dialect,
        ),
        _json_string_equals(
            row,
            "public_reply_owner",
            "fast",
            dialect=dialect,
        ),
        or_(
            _json_boolean_true(row, _PACKET_MARKER, dialect=dialect),
            _json_boolean_true(row, _RESULT_MARKER, dialect=dialect),
        ),
    )
    return or_(public_continuation, completed_result, live_private_handoff)


async def _relational_settlement_by_source(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    source_ids: set[uuid.UUID],
) -> dict[uuid.UUID, bool]:
    """Project bounded packet/result settlement from immutable reply lineage."""

    settled: dict[uuid.UUID, bool] = {
        source_id: False for source_id in source_ids
    }
    if not source_ids:
        return settled
    dialect = resolve_dialect_name(session)
    source = aliased(ProjectMessage)
    direct = aliased(ProjectMessage)
    child = aliased(ProjectMessage)
    child_matches = (
        select(child.id)
        .where(
            child.project_id == project_id,
            child.reply_to_message_id == direct.id,
            _settled_or_live_predicate(child, dialect=dialect),
        )
        .correlate(direct)
        .exists()
    )
    direct_matches = (
        select(direct.id)
        .where(
            direct.project_id == project_id,
            direct.reply_to_message_id == source.id,
            or_(
                _settled_or_live_predicate(direct, dialect=dialect),
                child_matches,
            ),
        )
        .correlate(source)
        .exists()
    )
    rows = (
        await session.execute(
            select(
                source.id,
                direct_matches.label("settled_or_live"),
            )
            .where(
                source.project_id == project_id,
                source.id.in_(source_ids),
            )
        )
    ).all()
    for source_id, is_settled in rows:
        if source_id in settled:
            settled[source_id] = bool(is_settled)
    return settled


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
    restore_existing: ProjectMessage | None = None
    for _replacement_depth in range(PRIVATE_HANDOFF_RETIREMENT_LIMIT + 1):
        existing = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key == idempotency_key,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if existing is None:
            break
        existing_context = (
            existing.context if isinstance(existing.context, dict) else {}
        )
        immutable_existing = bool(
            existing.from_type == "system"
            and existing.surface == MessageSurface.CONVERSATION
            and existing.message_type == MessageType.NOTE
            and existing.idempotency_key == idempotency_key
        )
        canonical_existing = bool(
            immutable_existing
            and existing.content == _RECOVERY_SUMMARY
            and existing_context.get(_RESULT_MARKER) is True
            and existing_context.get("source_packet_id") == str(source.id)
            and existing_context.get("claim_id") == str(claim_id)
            and existing_context.get("delivery_key") == "terminal-recovery"
            and existing_context.get(
                "_conversation_consultation_continuation"
            )
            == "terminal-recovery"
            and existing_context.get("result_state") == "failed"
            and existing_context.get("message_role")
            == "workday_consultation_result"
            and existing_context.get("public_reply_owner") == "fast"
            and existing_context.get("audience") == STRATEGIST_INTERNAL
            and existing_context.get(_ATOMIC_REPLY_ID) == str(source.id)
            and existing_context.get("_workday_terminal_recovery") is True
            and existing_context.get("privacy_state")
            not in {"redacted", "deleted"}
        )
        if canonical_existing and (
            existing.status == "open"
            and existing.handling_state
            in {MessageHandlingState.PENDING, MessageHandlingState.CLAIMED}
        ):
            if existing.reply_to_message_id is None:
                existing.reply_to_message_id = source.id
                session.add(existing)
                await session.flush()
            elif existing.reply_to_message_id != source.id:
                raise ValueError("terminal Workday recovery relational lineage conflicts")
            return existing
        if canonical_existing and _retired_terminal_recovery_is_restorable(
            existing,
            context=existing_context,
        ):
            restore_existing = existing
            break
        if immutable_existing and _retired_invalid_terminal_recovery_is_rebuildable(
            existing,
            context=existing_context,
        ):
            # Preserve the quarantined incident row. A deterministic successor
            # key gives the independently re-established source one fresh,
            # neutral delivery without copying any untrusted provenance.
            idempotency_key = (
                "workday-consultation-terminal-recovery-replacement:"
                f"{source.id}:{existing.id}"
            )
            continue
        if canonical_existing:
            raise ValueError("terminal Workday recovery is already settled")
        raise ValueError("terminal Workday recovery idempotency key conflicts")
    else:
        raise ValueError("terminal Workday recovery replacement limit reached")

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
    if restore_existing is not None:
        restored_context = dict(restore_existing.context or {})
        for key in _PRIVATE_HANDOFF_CLAIM_CONTEXT_KEYS:
            restored_context.pop(key, None)
        restored_context.pop(_PRIVATE_HANDOFF_RETIREMENT_CONTEXT_KEY, None)
        restored_context.pop(RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY, None)
        restored_context.pop(RESIDENT_HANDOFF_AT_CONTEXT_KEY, None)
        restored_context["envelope"] = context_envelope_reference(pinned)
        restore_existing.context = restored_context
        restore_existing.status = "open"
        restore_existing.handling_state = MessageHandlingState.PENDING
        restore_existing.handled_by_task_id = None
        restore_existing.resident_session_id = session_id
        restore_existing.resolution = None
        restore_existing.resolved_at = None
        restore_existing.updated_at = _utcnow()
        if restore_existing.reply_to_message_id not in {None, source.id}:
            raise ValueError("terminal Workday recovery relational lineage conflicts")
        restore_existing.reply_to_message_id = source.id
        session.add(restore_existing)
        await session.flush()
        return restore_existing
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
        reply_to_message_id=source.id,
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
        or not _source_shape_is_canonical(source)
        or await _canonical_source_receipt(session, source=source) is None
        or await _source_is_settled_or_live(session, source=source)
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
    reason: str = _PRIVATE_HANDOFF_RETIREMENT_REASON,
) -> None:
    """Terminalize a private row that cannot be delivered safely."""
    if reason not in {
        _PRIVATE_HANDOFF_RETIREMENT_REASON,
        _PRIVATE_HANDOFF_PROVENANCE_INVALID_REASON,
    }:
        raise ValueError("unsupported private handoff retirement reason")
    now = _utcnow()
    context = dict(row.context or {})
    for key in _PRIVATE_HANDOFF_CLAIM_CONTEXT_KEYS:
        context.pop(key, None)
    context[_PRIVATE_HANDOFF_RETIREMENT_CONTEXT_KEY] = {
        "reason": reason,
        "retired_at": now.isoformat(),
    }
    row.context = context
    row.status = "resolved"
    row.handling_state = MessageHandlingState.SUPERSEDED
    row.handled_by_task_id = None
    row.resolution = f"private_handoff_{reason}"
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
    trust_row_context: bool = True,
    server_source_id: uuid.UUID | None = None,
) -> bool:
    """Create the neutral Fast-visible recovery for one retired private row."""
    source_id = server_source_id
    if source_id is None:
        source_id = (
            workday_handoff_source_id(row)
            if trust_row_context
            else await workday_result_source_id_from_server_lineage(session, row=row)
        )
    if source_id is None:
        return False
    try:
        # Retirement is flushed before this savepoint.  Roll back every
        # partial snapshot/sequence/result write if neutral recovery fails,
        # while keeping the retired private row and durable retry identity.
        async with session.begin_nested():
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
        task_context = dict(task.context or {})
        retry_source_ids = _recovery_retry_source_ids(task_context)
        if source_id not in retry_source_ids:
            if len(retry_source_ids) >= _RECOVERY_RETRY_LIMIT:
                # Roll back the caller's retirement instead of silently losing
                # a source when the bounded durable retry queue is full.
                raise RuntimeError(
                    "Workday terminal recovery retry queue is full"
                ) from None
            retry_source_ids.append(source_id)
        _store_recovery_retry_source_ids(task, task_context, retry_source_ids)
        session.add(task)
        await session.flush()
        logger.warning(
            "[STRATEGIC] Could not yet create neutral terminal recovery for "
            "Workday request %s",
            source_id,
            exc_info=True,
        )
        return False
    return recovered is not None


def _workday_packet_claim_id_from_server_key(
    row: ProjectMessage,
) -> uuid.UUID | None:
    """Parse the claim identity from the server-owned packet key prefix."""

    key_parts = str(row.idempotency_key or "").split(":", 2)
    if len(key_parts) != 3 or key_parts[0] != "workday-consultation":
        return None
    try:
        return uuid.UUID(key_parts[1])
    except (TypeError, ValueError, AttributeError):
        return None


async def workday_packet_immutable_source_id(
    session: AsyncSession,
    *,
    row: ProjectMessage,
) -> uuid.UUID | None:
    """Resolve neutral-recovery authority from server key plus relational FK."""

    claim_id = _workday_packet_claim_id_from_server_key(row)
    source_id = row.reply_to_message_id
    if (
        claim_id is None
        or row.from_type != "system"
        or row.surface != MessageSurface.CONVERSATION
        or row.message_type != MessageType.NOTE
    ):
        return None
    if not isinstance(source_id, uuid.UUID):
        return None
    if await _canonical_public_workday_source(
        session,
        project_id=row.project_id,
        source_id=source_id,
        claim_id=claim_id,
    ) is None:
        return None
    return source_id


async def workday_packet_source_id_from_server_lineage(
    session: AsyncSession,
    *,
    row: ProjectMessage,
) -> uuid.UUID | None:
    """Validate packet projections against immutable relational authority."""

    context = row.context if isinstance(row.context, dict) else {}
    from app.conversation_coordinator import (
        workday_consultation_packet_idempotency_key,
    )

    claim_id = _workday_packet_claim_id_from_server_key(row)
    source_id = await workday_packet_immutable_source_id(session, row=row)
    if claim_id is None or source_id is None:
        return None
    delivery_key = context.get("delivery_key")
    continuation_key = context.get("_conversation_consultation_continuation")
    if not (
        isinstance(delivery_key, str)
        and bool(delivery_key)
        and isinstance(continuation_key, str)
        and bool(continuation_key)
        and context.get("claim_id") == str(claim_id)
        and context.get(_ATOMIC_REPLY_ID) == str(source_id)
        and row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.message_type == MessageType.NOTE
        and row.status == "open"
        and row.handling_state
        in {MessageHandlingState.PENDING, MessageHandlingState.CLAIMED}
        and row.idempotency_key
        == workday_consultation_packet_idempotency_key(
            claim_id,
            delivery_key,
            continuation_key,
        )
        and row.content == context.get("question")
        and context.get(_PACKET_MARKER) is True
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("message_role") == "workday_consultation"
        and context.get("public_reply_owner") == "fast"
    ):
        return None
    return source_id


async def workday_result_server_lineage(
    session: AsyncSession,
    *,
    row: ProjectMessage,
) -> WorkdayResultServerLineage | None:
    """Resolve result authority exclusively from immutable server lineage."""

    key_parts = str(row.idempotency_key or "").split(":")
    terminal_source_id: uuid.UUID | None = None
    if len(key_parts) == 2 and key_parts[0] == "workday-consultation-terminal-recovery":
        try:
            terminal_source_id = uuid.UUID(key_parts[1])
        except (TypeError, ValueError, AttributeError):
            return None
    elif (
        len(key_parts) == 3
        and key_parts[0] == "workday-consultation-terminal-recovery-replacement"
    ):
        try:
            terminal_source_id = uuid.UUID(key_parts[1])
            predecessor_id = uuid.UUID(key_parts[2])
        except (TypeError, ValueError, AttributeError):
            return None
        seen_ids = {row.id}
        for _depth in range(PRIVATE_HANDOFF_RETIREMENT_LIMIT + 1):
            if predecessor_id in seen_ids:
                return None
            seen_ids.add(predecessor_id)
            predecessor = (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == row.project_id,
                        ProjectMessage.id == predecessor_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            predecessor_context = (
                predecessor.context
                if predecessor is not None
                and isinstance(predecessor.context, dict)
                else {}
            )
            if not (
                predecessor is not None
                and predecessor.from_type == "system"
                and predecessor.surface == MessageSurface.CONVERSATION
                and predecessor.message_type == MessageType.NOTE
                and _retired_invalid_terminal_recovery_is_rebuildable(
                    predecessor,
                    context=predecessor_context,
                )
            ):
                return None
            predecessor_parts = str(predecessor.idempotency_key or "").split(":")
            if (
                len(predecessor_parts) == 2
                and predecessor_parts[0]
                == "workday-consultation-terminal-recovery"
                and predecessor_parts[1] == str(terminal_source_id)
            ):
                break
            if not (
                len(predecessor_parts) == 3
                and predecessor_parts[0]
                == "workday-consultation-terminal-recovery-replacement"
                and predecessor_parts[1] == str(terminal_source_id)
            ):
                return None
            try:
                predecessor_id = uuid.UUID(predecessor_parts[2])
            except (TypeError, ValueError, AttributeError):
                return None
        else:
            return None
    if terminal_source_id is not None:
        row_context = row.context if isinstance(row.context, dict) else {}
        if not (
            row.from_type == "system"
            and row.surface == MessageSurface.CONVERSATION
            and row.message_type == MessageType.NOTE
            and row.reply_to_message_id == terminal_source_id
            and row_context.get(_RESULT_MARKER) is True
            and row_context.get("audience") == STRATEGIST_INTERNAL
            and row_context.get("message_role") == "workday_consultation_result"
            and row_context.get("public_reply_owner") == "fast"
            and row_context.get("_workday_terminal_recovery") is True
        ):
            return None
        source = (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == row.project_id,
                    ProjectMessage.id == terminal_source_id,
                    strategic_conversation_predicate(),
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            source is None
            or not _source_shape_is_canonical(source)
            or await _canonical_source_receipt(session, source=source) is None
        ):
            return None
        return WorkdayResultServerLineage(
            public_source_id=terminal_source_id,
            authority_row_id=terminal_source_id,
            kind="terminal_recovery",
        )
    autonomous_lineage = await autonomous_workday_result_lineage_valid(
        session,
        row=row,
        key_parts=key_parts,
    )
    if autonomous_lineage is not None:
        return WorkdayResultServerLineage(
            public_source_id=None,
            authority_row_id=row.id,
            kind="autonomous_handoff",
            projections_valid=autonomous_lineage,
        )
    if len(key_parts) != 3 or key_parts[0] != "workday-consultation-result":
        return None
    try:
        packet_id = uuid.UUID(key_parts[1])
        result_claim_id = uuid.UUID(key_parts[2])
    except (TypeError, ValueError, AttributeError):
        return None
    packet = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == row.project_id,
                ProjectMessage.id == packet_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if packet is None:
        return None
    context = packet.context if isinstance(packet.context, dict) else {}
    row_context = row.context if isinstance(row.context, dict) else {}
    if row.reply_to_message_id != packet_id:
        return None
    from app.conversation_coordinator import (
        workday_consultation_packet_idempotency_key,
    )

    packet_claim_id = _workday_packet_claim_id_from_server_key(packet)
    immutable_source_id = await workday_packet_immutable_source_id(
        session,
        row=packet,
    )
    if packet_claim_id is None or immutable_source_id is None:
        return None
    try:
        ack_claim_id = uuid.UUID(
            str(context.get("_workday_consultation_ack_claim_id"))
        )
    except (TypeError, ValueError, AttributeError):
        ack_claim_id = None
    delivery_key = context.get("delivery_key")
    continuation_key = context.get("_conversation_consultation_continuation")
    expected_packet_key = (
        workday_consultation_packet_idempotency_key(
            packet_claim_id,
            delivery_key,
            continuation_key,
        )
        if isinstance(delivery_key, str)
        and bool(delivery_key)
        and isinstance(continuation_key, str)
        and bool(continuation_key)
        else None
    )
    projections_valid = bool(
        packet.from_type == "system"
        and packet.surface == MessageSurface.CONVERSATION
        and packet.message_type == MessageType.NOTE
        and packet.status == "handled"
        and packet.handling_state == MessageHandlingState.RESPONDED
        and context.get("claim_id") == str(packet_claim_id)
        and context.get(_ATOMIC_REPLY_ID) == str(immutable_source_id)
        and packet.idempotency_key == expected_packet_key
        and packet.content == context.get("question")
        and context.get(_PACKET_MARKER) is True
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("message_role") == "workday_consultation"
        and context.get("public_reply_owner") == "fast"
        and result_claim_id == ack_claim_id
        and context.get("_workday_consultation_ack_outcome")
        in {"completed", "failed"}
        and row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.message_type == MessageType.NOTE
        and row_context.get(_RESULT_MARKER) is True
        and row_context.get("audience") == STRATEGIST_INTERNAL
        and row_context.get("message_role") == "workday_consultation_result"
        and row_context.get("public_reply_owner") == "fast"
        and row_context.get("source_packet_id") == str(packet_id)
    )
    return WorkdayResultServerLineage(
        public_source_id=immutable_source_id,
        authority_row_id=packet_id,
        kind="consultation_packet",
        projections_valid=projections_valid,
    )


async def terminal_recovery_source_candidate_from_server_key(
    session: AsyncSession,
    *,
    row: ProjectMessage,
) -> uuid.UUID | None:
    """Re-establish only a neutral-recovery source from a terminal server key.

    This is deliberately weaker than result lineage and must never authorize
    delivery, receipts, content, context, or an envelope. It exists solely so
    a quarantined corrupt replacement chain can still produce one new constant
    failure result from an independently validated canonical public source.
    """

    key_parts = str(row.idempotency_key or "").split(":")
    if not (
        (len(key_parts) == 2 and key_parts[0] == "workday-consultation-terminal-recovery")
        or (
            len(key_parts) == 3
            and key_parts[0]
            == "workday-consultation-terminal-recovery-replacement"
        )
    ):
        return None
    try:
        source_id = uuid.UUID(key_parts[1])
        if len(key_parts) == 3:
            # The predecessor is not an authority input here, but requiring the
            # complete server-key grammar prevents a malformed lookalike from
            # selecting even the weaker neutral-recovery path.
            uuid.UUID(key_parts[2])
    except (TypeError, ValueError, AttributeError):
        return None
    context = row.context if isinstance(row.context, dict) else {}
    if not (
        row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.message_type == MessageType.NOTE
        and row.status == "open"
        and row.handling_state == MessageHandlingState.CLAIMED
        and context.get(_RESULT_MARKER) is True
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("message_role") == "workday_consultation_result"
        and context.get("public_reply_owner") == "fast"
        and context.get("_workday_terminal_recovery") is True
        # These result-owned links only corroborate the independently parsed
        # server key. They can never choose a source or authorize delivery.
        and context.get("source_packet_id") == str(source_id)
        and context.get(_ATOMIC_REPLY_ID) == str(source_id)
    ):
        return None
    source = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == row.project_id,
                ProjectMessage.id == source_id,
                strategic_conversation_predicate(),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        source is None
        or not _source_shape_is_canonical(source)
        or await _canonical_source_receipt(session, source=source) is None
    ):
        return None
    return source_id


async def workday_result_source_id_from_server_lineage(
    session: AsyncSession,
    *,
    row: ProjectMessage,
) -> uuid.UUID | None:
    """Compatibility projection of the canonical public source identity."""

    lineage = await workday_result_server_lineage(session, row=row)
    return (
        lineage.public_source_id
        if lineage is not None and lineage.projections_valid
        else None
    )


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
    retry_source_ids = _recovery_retry_source_ids(task_context)
    normalized_retry_ids = [str(item) for item in retry_source_ids]
    if (
        _RECOVERY_RETRY_SOURCE_IDS in task_context
        and task_context.get(_RECOVERY_RETRY_SOURCE_IDS) != normalized_retry_ids
    ):
        _store_recovery_retry_source_ids(task, task_context, retry_source_ids)
        session.add(task)
    for source_id in list(retry_source_ids):
        try:
            async with session.begin_nested():
                recovered = await recover_workday_request_by_source_id(
                    session,
                    project=project,
                    task=task,
                    source_id=source_id,
                    worker_generation=worker_generation,
                    worker_incarnation=worker_incarnation,
                    session_id=session_id,
                )
        except ValueError:
            # Keep the exact source ahead of the monotonic broad-scan cursor.
            # Rotate one persistent failure behind later sources so a poisoned
            # head cannot starve every other orphan in the bounded queue.
            logger.warning(
                "[STRATEGIC] Deferred terminal recovery retry for Workday "
                "request %s",
                source_id,
                exc_info=True,
            )
            retry_source_ids.remove(source_id)
            retry_source_ids.append(source_id)
            _store_recovery_retry_source_ids(task, task_context, retry_source_ids)
            session.add(task)
            continue
        retry_source_ids.remove(source_id)
        _store_recovery_retry_source_ids(task, task_context, retry_source_ids)
        session.add(task)
        if recovered is not None:
            return recovered
    before_raw = task_context.get(_RECOVERY_CURSOR)
    high_raw = task_context.get(_RECOVERY_HIGH_WATERMARK)
    high_watermark = (
        high_raw
        if isinstance(high_raw, int)
        and not isinstance(high_raw, bool)
        and high_raw >= 0
        else 0
    )
    ceiling_raw = task_context.get(_RECOVERY_SCAN_CEILING)
    cursor_pair_is_valid = bool(
        isinstance(before_raw, int)
        and not isinstance(before_raw, bool)
        and isinstance(ceiling_raw, int)
        and not isinstance(ceiling_raw, bool)
        and high_watermark < before_raw <= ceiling_raw
    )
    before = before_raw if cursor_pair_is_valid else None
    scan_ceiling = ceiling_raw if cursor_pair_is_valid else None
    if not cursor_pair_is_valid and (
        _RECOVERY_CURSOR in task_context
        or _RECOVERY_SCAN_CEILING in task_context
    ):
        task_context.pop(_RECOVERY_CURSOR, None)
        task_context.pop(_RECOVERY_SCAN_CEILING, None)
        task.context = task_context
        flag_modified(task, "context")
        session.add(task)
    if scan_ceiling is None:
        counter = await session.get(ProjectMessageSequence, project.id)
        scan_ceiling = int(counter.last_sequence) if counter is not None else 0
        if counter is None:
            latest_sequence = (
                await session.execute(
                    select(ProjectMessage.sequence)
                    .where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.sequence.is_not(None),
                    )
                    .order_by(ProjectMessage.sequence.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            scan_ceiling = int(latest_sequence or 0)
    if before is None and scan_ceiling <= high_watermark:
        return None
    postgres = session.get_bind().dialect.name == "postgresql"
    recovery_candidate_predicate = (
        text(
            "projectmessage.sequence IS NOT NULL AND "
            "projectmessage.from_type = 'strategic' AND "
            "projectmessage.surface = 'conversation' AND "
            "projectmessage.context @> "
            "'{\"_workday_consultation_requested\": true}'::jsonb"
        )
        if postgres
        else ProjectMessage.context[_REQUESTED_MARKER]
        .as_boolean()
        .is_(True)
    )
    statement = select(ProjectMessage).where(
        ProjectMessage.project_id == project.id,
        ProjectMessage.from_type == "strategic",
        recovery_candidate_predicate,
        ProjectMessage.sequence > high_watermark,
        ProjectMessage.sequence <= scan_ceiling,
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
    source_ids = {str(source.id) for source in sources}
    receipts_by_source: dict[str, list[StrategicMemorySource]] = {
        source_id: [] for source_id in source_ids
    }
    settled_by_source: dict[uuid.UUID, bool] = {}
    if source_ids:
        source_receipt_identities = [
            (str(source.id), str(source.sequence)) for source in sources
        ]
        receipts = (
            (
                await session.execute(
                    select(StrategicMemorySource).where(
                        StrategicMemorySource.project_id == project.id,
                        StrategicMemorySource.source_type == "project_message",
                        tuple_(
                            StrategicMemorySource.source_key,
                            StrategicMemorySource.source_revision,
                        ).in_(source_receipt_identities),
                    )
                )
            )
            .scalars()
            .all()
        )
        for receipt in receipts:
            if receipt.source_key in receipts_by_source:
                receipts_by_source[receipt.source_key].append(receipt)
        settled_by_source = await _relational_settlement_by_source(
            session,
            project_id=project.id,
            source_ids={source.id for source in sources},
        )
    for source in sources:
        source_id = str(source.id)
        if source.id in retry_source_ids:
            continue
        source_receipts = [
            receipt
            for receipt in receipts_by_source.get(source_id, [])
            if _source_receipt_is_current(source, receipt)
        ]
        if (
            not _source_shape_is_canonical(source)
            or len(source_receipts) != 1
            or settled_by_source.get(source.id, False)
        ):
            continue
        try:
            async with session.begin_nested():
                recovered = await _create_recovery_result(
                    session,
                    project=project,
                    task=task,
                    source=source,
                    worker_generation=worker_generation,
                    worker_incarnation=worker_incarnation,
                    session_id=session_id,
                )
        except ValueError:
            if source.id not in retry_source_ids:
                if len(retry_source_ids) >= _RECOVERY_RETRY_LIMIT:
                    raise RuntimeError(
                        "Workday terminal recovery retry queue is full"
                    ) from None
                retry_source_ids.append(source.id)
            _store_recovery_retry_source_ids(
                task,
                task_context,
                retry_source_ids,
            )
            session.add(task)
            logger.warning(
                "[STRATEGIC] Deferred broad-scan terminal recovery for "
                "Workday request %s",
                source.id,
                exc_info=True,
            )
            continue
        if recovered is not None:
            task_context[_RECOVERY_CURSOR] = int(source.sequence or 0)
            task_context[_RECOVERY_SCAN_CEILING] = scan_ceiling
            task.context = task_context
            flag_modified(task, "context")
            session.add(task)
            return recovered
    if len(sources) == _RECOVERY_LIMIT:
        task_context[_RECOVERY_CURSOR] = min(int(row.sequence or 0) for row in sources)
        task_context[_RECOVERY_SCAN_CEILING] = scan_ceiling
    else:
        task_context.pop(_RECOVERY_CURSOR, None)
        task_context.pop(_RECOVERY_SCAN_CEILING, None)
        task_context[_RECOVERY_HIGH_WATERMARK] = max(
            high_watermark,
            scan_ceiling,
        )
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
    "terminal_recovery_source_candidate_from_server_key",
    "workday_handoff_source_id",
    "workday_packet_immutable_source_id",
    "workday_packet_source_id_from_server_lineage",
    "workday_result_server_lineage",
    "workday_result_source_id_from_server_lineage",
]
