"""Durable foreground conversation admission for Strategic Residents.

The message row is the canonical operator input. ``conversation_claim_id``
groups the exact rows admitted to one foreground turn, while the short lease
and worker-generation fence make that turn reclaimable after a provider,
process, task, or runner failure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.message_audience import STRATEGIST_INTERNAL, stamp_message_audience
from app.models import (
    MessageHandlingState,
    MessageSurface,
    MessageType,
    ProjectMessage,
)
from app.project_message_creation import (
    build_project_message,
    lock_project_and_next_message_sequence,
)
from app.resident_conversation_scope import (
    resident_broad_message_scope,
    resident_conversation_scope,
)
from app.strategic_conversation_query import strategic_conversation_predicate
from app.time_utils import _utcnow


@dataclass(frozen=True)
class ConversationClaim:
    claim_id: uuid.UUID
    rows: tuple[ProjectMessage, ...]
    lease_expires_at: datetime
    attempt: int


class ConversationPublicReplyOwner(StrEnum):
    """The only lanes which may produce a public answer for a turn."""

    FAST = "fast"
    STANDARD = "standard"


@dataclass(frozen=True)
class WorkdayConsultationPacket:
    """Durable, internal hand-off for background investigation.

    The packet is represented by a normal ``ProjectMessage`` so it survives
    process replacement without introducing a second queue or transcript.
    Its internal audience keeps it out of the operator conversation.
    """

    message: ProjectMessage
    duplicate: bool


@dataclass(frozen=True)
class PublicConversationReply:
    """The one durable public reply created for a claimed turn."""

    message: ProjectMessage
    duplicate: bool
    consultation_packet: ProjectMessage | None = None


@dataclass(frozen=True)
class WorkdayConsultationClaim:
    """One exact, lease-bound delivery of an internal consultation packet."""

    message: ProjectMessage
    workday_claim_id: uuid.UUID
    lease_expires_at: datetime
    attempt: int


@dataclass(frozen=True)
class WorkdayConsultationContinuationClaim:
    """A server-created Fast receipt for one completed private investigation."""

    message: ProjectMessage
    continuation_claim_id: uuid.UUID
    lease_expires_at: datetime
    attempt: int


_PUBLIC_REPLY_OWNER_CONTEXT_KEY = "_conversation_public_reply_owner"
_CONSULTATION_PACKET_CONTEXT_KEY = "_conversation_consultation_packet"
_CONSULTATION_CONTINUATION_CONTEXT_KEY = "_conversation_consultation_continuation"
_WORKDAY_CLAIM_ID_CONTEXT_KEY = "_workday_consultation_claim_id"
_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY = "_workday_consultation_claim_generation"
_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY = "_workday_consultation_claim_incarnation"
_WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY = "_workday_consultation_claim_expires_at"
_WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY = "_workday_consultation_claim_attempt"
_WORKDAY_ACK_CLAIM_ID_CONTEXT_KEY = "_workday_consultation_ack_claim_id"
_WORKDAY_RESULT_SUMMARY_CONTEXT_KEY = "_workday_consultation_result_summary"
_CONSULTATION_RESULT_CONTEXT_KEY = "_workday_consultation_result"
_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY = "_workday_consultation_atomic_reply_id"
_CONTINUATION_CLAIM_ID_CONTEXT_KEY = "_workday_continuation_claim_id"
_CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY = "_workday_continuation_claim_generation"
_CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY = "_workday_continuation_claim_incarnation"
_CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY = "_workday_continuation_claim_expires_at"
_CONTINUATION_CLAIM_ATTEMPT_CONTEXT_KEY = "_workday_continuation_claim_attempt"
_CONVERSATION_CLAIM_IDEMPOTENCY_CONTEXT_KEY = (
    "_conversation_claim_idempotency_key"
)


def public_reply_owner_for_task_context(
    task_context: dict[str, Any] | None,
) -> ConversationPublicReplyOwner | None:
    """Read the server-owned lane marker without accepting caller input."""
    value = (task_context or {}).get("_conversation_public_reply_owner")
    try:
        return ConversationPublicReplyOwner(str(value))
    except ValueError:
        return None


def native_resident_reply_may_complete_claim(row: ProjectMessage) -> bool:
    """Whether the legacy/native reply channel may finish this input.

    Fast ownership is deliberately a property of each durable input, not of a
    task.  A process can host both the restricted Fast child and broad Workday
    execution, so task/generation/incarnation equality is insufficient here.
    """
    context = row.context if isinstance(row.context, dict) else {}
    return (
        context.get(_PUBLIC_REPLY_OWNER_CONTEXT_KEY)
        != ConversationPublicReplyOwner.FAST.value
    )


def workday_consultation_packet_idempotency_key(
    claim_id: uuid.UUID,
    delivery_key: str,
    continuation_key: str,
) -> str:
    """Return the one server-defined packet identity for a Fast claim request."""
    return f"workday-consultation:{claim_id}:{delivery_key}:{continuation_key}"


def standard_fallback_is_admissible(
    *,
    fast_capacity_available: bool,
    explicitly_requested: bool,
) -> bool:
    """Standard Strategic is a per-turn fallback, never a standing owner."""
    return explicitly_requested and not fast_capacity_available


def legacy_conversation_claim_incarnation(
    task_id: uuid.UUID,
    worker_generation: int,
) -> uuid.UUID:
    """Stable rollout identity for pre-incarnation Resident agents."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        (
            "blueprint:resident-conversation-claim:"
            f"{task_id}:{worker_generation}"
        ),
    )


def _session_scope(
    session_id: str,
    *,
    conversation_floor_sequence: int | None,
    canonical_conversation_only: bool,
) -> object:
    scope = (
        resident_conversation_scope
        if canonical_conversation_only
        else resident_broad_message_scope
    )
    return scope(
        session_id,
        conversation_floor_sequence=conversation_floor_sequence,
    )


def reset_conversation_claim_for_session_handoff(row: ProjectMessage) -> None:
    """Sever a terminal predecessor's turn identity before narrowing membership.

    A pre-boundary Fast claim may span both legacy backlog and a current operator
    row. Once the durable session floor excludes the legacy member, preserving
    that shared claim id would make the current row permanently unclaimable: the
    coordinator correctly rejects a subset of immutable membership. The ended or
    superseded predecessor can no longer complete, so the intentionally rebound
    row starts a fresh exact turn while excluded members retain their old claim
    evidence for diagnosis.
    """

    row.conversation_claim_id = None
    row.conversation_claim_generation = None
    row.conversation_claim_incarnation = None
    row.conversation_claim_expires_at = None
    context = dict(row.context or {})
    context.pop(_PUBLIC_REPLY_OWNER_CONTEXT_KEY, None)
    context.pop(_CONVERSATION_CLAIM_IDEMPOTENCY_CONTEXT_KEY, None)
    row.context = context


def _message_ordering() -> tuple[object, ...]:
    return (
        ProjectMessage.sequence.asc().nulls_last(),
        ProjectMessage.created_at.asc(),
        ProjectMessage.id.asc(),
    )


async def claim_operator_messages(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    session_id: str,
    task_id: uuid.UUID,
    worker_generation: int,
    incarnation_id: uuid.UUID,
    message_ids: list[uuid.UUID],
    lease_seconds: int,
    public_reply_owner: ConversationPublicReplyOwner | None = None,
    canonical_conversation_only: bool = False,
    client_claim_id: uuid.UUID | None = None,
    claim_idempotency_key: str | None = None,
    conversation_floor_sequence: int | None = None,
) -> ConversationClaim | None:
    """Claim exactly one canonical operator batch under the caller's Project lock.

    The caller supplies rows from the read-only poll. The lock plus exact ID
    validation closes the poll/claim race. A live claim owned by another worker
    is not stealable; an expired claim is reclaimed as the same logical turn.
    """
    if not message_ids:
        return None
    now = _utcnow()
    lease_seconds = min(300, max(15, int(lease_seconds)))
    requested = list(dict.fromkeys(message_ids))
    normalized_claim_key = (
        claim_idempotency_key.strip() if claim_idempotency_key is not None else None
    )
    if client_claim_id is not None and (
        not normalized_claim_key or len(normalized_claim_key) > 255
    ):
        raise ValueError(
            "client_claim_id requires a non-empty <=255-character idempotency key"
        )
    if client_claim_id is None and normalized_claim_key is not None:
        raise ValueError("claim idempotency key requires client_claim_id")
    row_conditions: list[object] = [
        ProjectMessage.project_id == project_id,
        ProjectMessage.id.in_(requested),
        ProjectMessage.from_type == "user",
        ProjectMessage.status == "open",
        _session_scope(
            session_id,
            conversation_floor_sequence=conversation_floor_sequence,
            canonical_conversation_only=canonical_conversation_only,
        ),
    ]
    if canonical_conversation_only:
        row_conditions.append(strategic_conversation_predicate())
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(*row_conditions)
                .order_by(*_message_ordering())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(requested):
        return None
    ordered = tuple(by_id[value] for value in requested)

    existing_ids = {
        row.conversation_claim_id
        for row in ordered
        if row.conversation_claim_id is not None
    }
    if len(existing_ids) > 1:
        return None
    existing_claim_id = next(iter(existing_ids), None)
    if existing_claim_id is not None:
        # A retry must name the exact immutable membership of the existing
        # logical turn. Otherwise a caller could silently append a newer input
        # to a live claim, or omit a covered input and receive an incomplete
        # prompt while completion acknowledges both.
        all_claim_rows = (
            (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.conversation_claim_id == existing_claim_id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if {row.id for row in all_claim_rows} != set(requested):
            return None
    claimed_rows = tuple(
        row for row in ordered if row.conversation_claim_id is not None
    )
    legacy_incarnation = legacy_conversation_claim_incarnation(
        task_id,
        worker_generation,
    )
    same_owner = bool(claimed_rows) and all(
        row.handling_state == MessageHandlingState.CLAIMED
        and row.handled_by_task_id == task_id
        and row.conversation_claim_generation == worker_generation
        and (
            row.conversation_claim_incarnation == incarnation_id
            or (
                row.conversation_claim_incarnation is None
                and incarnation_id == legacy_incarnation
            )
        )
        for row in claimed_rows
    )
    # A durable claim can be retried by its exact process, but it must never
    # change lanes.  In particular a background Workday recovery cannot turn a
    # Fast-owned input into its own public reply after a restart.
    if public_reply_owner is not None and any(
        (row.context or {}).get(_PUBLIC_REPLY_OWNER_CONTEXT_KEY)
        not in (None, public_reply_owner.value)
        for row in claimed_rows
    ):
        return None
    # A rolling-deploy legacy worker can keep completing claims it created, but
    # must never reclaim a turn that the typed foreground has made Fast-owned.
    # This keeps a mixed fleet live without opening a second public owner.
    if public_reply_owner is None and any(
        (row.context or {}).get(_PUBLIC_REPLY_OWNER_CONTEXT_KEY) is not None
        for row in claimed_rows
    ):
        return None
    lease_live = any(
        row.conversation_claim_expires_at is not None
        and row.conversation_claim_expires_at > now
        for row in claimed_rows
    )
    lease_expired = bool(claimed_rows) and all(
        row.handling_state
        in (MessageHandlingState.PENDING, MessageHandlingState.CLAIMED)
        and row.conversation_claim_expires_at is not None
        and row.conversation_claim_expires_at <= now
        for row in claimed_rows
    )
    if existing_claim_id is not None and lease_live and not same_owner:
        return None

    client_identity_matches = existing_claim_id is None or (
        (client_claim_id is None or existing_claim_id == client_claim_id)
        and (
            normalized_claim_key is None
            or all(
                (row.context or {}).get(
                    _CONVERSATION_CLAIM_IDEMPOTENCY_CONTEXT_KEY
                )
                == normalized_claim_key
                for row in claimed_rows
            )
        )
    )
    # A live same-process replay must retain its stable client identity.  Once
    # the lease expires, however, a replacement process has necessarily lost
    # those in-memory identifiers.  Exact immutable membership is already
    # locked and verified above, so allow the replacement to bind a fresh
    # client identity instead of leaving the operator turn unrecoverable.
    if existing_claim_id is not None and lease_live and not client_identity_matches:
        return None

    rebind_expired_client_identity = (
        existing_claim_id is not None
        and lease_expired
        and not client_identity_matches
    )
    if (
        existing_claim_id is not None
        and not client_identity_matches
        and not rebind_expired_client_identity
    ):
        return None

    if client_claim_id is not None and (
        existing_claim_id is None or rebind_expired_client_identity
    ):
        rows_using_client_claim = (
            (
                await session.execute(
                    select(ProjectMessage)
                    .where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.conversation_claim_id == client_claim_id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if rows_using_client_claim:
            return None
    claim_id = (
        client_claim_id
        if rebind_expired_client_identity
        else existing_claim_id or client_claim_id or uuid.uuid4()
    )
    reclaimed = existing_claim_id is not None and (
        not same_owner or rebind_expired_client_identity
    )
    expires_at = now + timedelta(seconds=lease_seconds)
    attempt = max((row.conversation_claim_attempt for row in ordered), default=0)
    if existing_claim_id is None or reclaimed:
        attempt += 1
    attempt = max(1, attempt)

    for row in ordered:
        row.handling_state = MessageHandlingState.CLAIMED
        row.handled_by_task_id = task_id
        row.conversation_claim_id = claim_id
        row.conversation_claim_generation = worker_generation
        row.conversation_claim_incarnation = incarnation_id
        row.conversation_claim_expires_at = expires_at
        row.conversation_claim_attempt = attempt
        if public_reply_owner is not None:
            row.context = {
                **dict(row.context or {}),
                _PUBLIC_REPLY_OWNER_CONTEXT_KEY: public_reply_owner.value,
                **(
                    {
                        _CONVERSATION_CLAIM_IDEMPOTENCY_CONTEXT_KEY:
                        normalized_claim_key
                    }
                    if normalized_claim_key is not None
                    else {}
                ),
            }
        row.updated_at = now
        session.add(row)
    await session.flush()
    return ConversationClaim(
        claim_id=claim_id,
        rows=ordered,
        lease_expires_at=expires_at,
        attempt=attempt,
    )


async def renew_conversation_claim(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    claim_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    incarnation_id: uuid.UUID,
    lease_seconds: int = 45,
) -> bool:
    """Renew only an exact live claim owner; stale generations fail closed."""
    now = _utcnow()
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.conversation_claim_id == claim_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    legacy_incarnation = legacy_conversation_claim_incarnation(
        task_id,
        worker_generation,
    )
    if not rows or any(
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or row.conversation_claim_generation != worker_generation
        or (
            row.conversation_claim_incarnation != incarnation_id
            and not (
                row.conversation_claim_incarnation is None
                and incarnation_id == legacy_incarnation
            )
        )
        or row.conversation_claim_expires_at is None
        or row.conversation_claim_expires_at <= now
        for row in rows
    ):
        return False
    expires_at = now + timedelta(seconds=min(300, max(15, int(lease_seconds))))
    for row in rows:
        row.conversation_claim_incarnation = incarnation_id
        row.conversation_claim_expires_at = expires_at
        row.updated_at = now
        session.add(row)
    await session.flush()
    return True


def _envelope_transport_fields(envelope: Any) -> dict[str, Any]:
    """Persist only a revocable pointer to Phase 2A's content-bearing snapshot."""
    if hasattr(envelope, "model_dump"):
        value = envelope.model_dump(mode="json")
    elif isinstance(envelope, dict):
        value = dict(envelope)
    else:
        value = {
            name: getattr(envelope, name)
            for name in (
                "snapshot_id",
                "canonical_envelope_b64",
                "canonical_sha256",
                "context_authority_generation",
                "privacy_generation",
                "transcript_sequence",
                "ledger_revision",
                "expires_at",
            )
            if hasattr(envelope, name)
        }
    from app.strategic_memory_envelope import context_envelope_reference

    return context_envelope_reference(value)


async def _pin_current_context_envelope_reference(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    reference: dict[str, Any],
):
    """Pin a current snapshot without making an earlier expiry projection toxic.

    Pinning extends only the cache lifetime; it does not change the immutable
    bytes, authority/privacy generations, transcript cursor, or ledger revision.
    A Fast turn may create more than one private packet from the one claim
    envelope it received. After the first packet extends ``expires_at``, accept
    that earlier projection only when the current snapshot still exists and all
    immutable fences match. Revoked, expired, substituted, or generation-stale
    snapshots still fail closed.
    """
    from app.strategic_memory_envelope import (
        context_envelope_reference,
        get_fenced_context_envelope_snapshot,
        pin_fenced_context_envelope_for_packet,
    )

    pinned = await pin_fenced_context_envelope_for_packet(
        session,
        project_id=project_id,
        reference=reference,
    )
    if pinned is not None:
        return pinned
    try:
        snapshot_id = uuid.UUID(str(reference["snapshot_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    current = await get_fenced_context_envelope_snapshot(
        session,
        project_id=project_id,
        snapshot_id=snapshot_id,
    )
    if current is None:
        return None
    current_reference = context_envelope_reference(current)
    immutable_fields = (
        "snapshot_id",
        "canonical_sha256",
        "context_authority_generation",
        "privacy_generation",
        "transcript_sequence",
        "ledger_revision",
    )
    if any(
        str(current_reference[field]) != str(reference.get(field))
        for field in immutable_fields
    ):
        return None
    return await pin_fenced_context_envelope_for_packet(
        session,
        project_id=project_id,
        reference=current_reference,
    )


async def create_or_get_workday_consultation_packet(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    claim: ConversationClaim,
    envelope: Any,
    delivery_key: str,
    question: str,
    continuation_key: str | None = None,
    resident_session_id: str | None = None,
    atomic_public_reply_id: uuid.UUID | None = None,
) -> WorkdayConsultationPacket:
    """Persist an internal Workday hand-off exactly once.

    This routine intentionally creates no public response and does not mark
    the input handled.  The foreground owner remains responsible for that
    acknowledgement; Workday can restart, re-fetch the packet, and append an
    idempotent continuation without acquiring public reply authority.
    """
    normalized_delivery_key = delivery_key.strip()
    normalized_continuation_key = (continuation_key or "initial").strip()
    normalized_question = question.strip()
    if not normalized_delivery_key or len(normalized_delivery_key) > 128:
        raise ValueError("delivery_key must be a non-empty <=128-character value")
    if not normalized_continuation_key or len(normalized_continuation_key) > 128:
        raise ValueError("continuation_key must be a non-empty <=128-character value")
    if not normalized_question or len(normalized_question) > 20_000:
        raise ValueError("question must be a non-empty <=20000-character value")
    transport = _envelope_transport_fields(envelope)
    idempotency_key = workday_consultation_packet_idempotency_key(
        claim.claim_id,
        normalized_delivery_key,
        normalized_continuation_key,
    )
    existing = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    expected_context = {
        _CONSULTATION_PACKET_CONTEXT_KEY: True,
        "claim_id": str(claim.claim_id),
        "claim_attempt": claim.attempt,
        "delivery_key": normalized_delivery_key,
        "question": normalized_question,
        _CONSULTATION_CONTINUATION_CONTEXT_KEY: normalized_continuation_key,
        "public_reply_owner": ConversationPublicReplyOwner.FAST.value,
        **(
            {
                _CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY: str(
                    atomic_public_reply_id
                )
            }
            if atomic_public_reply_id is not None
            else {}
        ),
    }
    if existing is not None:
        existing_context = existing.context if isinstance(existing.context, dict) else {}
        if (
            existing.from_type != "system"
            or existing.surface != MessageSurface.CONVERSATION
            or {key: existing_context.get(key) for key in expected_context}
            != expected_context
        ):
            raise ValueError("consultation idempotency key belongs to a different packet")
        from app.strategic_memory_envelope import context_envelope_reference

        pinned = await _pin_current_context_envelope_reference(
            session,
            project_id=project_id,
            reference=existing_context.get("envelope") or {},
        )
        if pinned is None:
            raise ValueError("consultation envelope is stale, expired, or revoked")
        existing.context = {
            **existing_context,
            "envelope": context_envelope_reference(pinned),
        }
        session.add(existing)
        await session.flush()
        return WorkdayConsultationPacket(message=existing, duplicate=True)

    from app.strategic_memory_envelope import context_envelope_reference

    pinned = await _pin_current_context_envelope_reference(
        session,
        project_id=project_id,
        reference=transport,
    )
    if pinned is None:
        raise ValueError("consultation envelope is stale, expired, or revoked")
    expected_context["envelope"] = context_envelope_reference(pinned)
    sequence = await lock_project_and_next_message_sequence(session, project_id)
    packet = await build_project_message(
        session,
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.PENDING,
        message_type=MessageType.NOTE,
        from_type="system",
        content=normalized_question,
        context=stamp_message_audience(
            {
                **expected_context,
                "message_role": "workday_consultation",
            },
            "workday_consultation",
            message_type=MessageType.NOTE.value,
        ),
        idempotency_key=idempotency_key,
        resident_session_id=resident_session_id,
    )
    session.add(packet)
    await session.flush()
    return WorkdayConsultationPacket(message=packet, duplicate=False)


def _workday_claim_expiry(value: Any) -> datetime | None:
    """Parse the JSON-stored lease timestamp without accepting malformed state."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _is_workday_consultation_packet(
    row: ProjectMessage,
    *,
    resident_session_id: str,
) -> bool:
    context = row.context if isinstance(row.context, dict) else {}
    return bool(
        row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.status == "open"
        and row.resident_session_id == resident_session_id
        and context.get(_CONSULTATION_PACKET_CONTEXT_KEY) is True
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("message_role") == "workday_consultation"
        and context.get("public_reply_owner") == ConversationPublicReplyOwner.FAST.value
        # A typed packet becomes deliverable only as part of the transaction
        # that created its source Fast reply.  The older pre-completion helper
        # may still create a packet for compatibility, but it is deliberately
        # not claimable by broad Workday execution.
        and isinstance(context.get(_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY), str)
        and bool(context.get(_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY))
    )


def _is_workday_consultation_result(
    row: ProjectMessage,
    *,
    resident_session_id: str,
) -> bool:
    context = row.context if isinstance(row.context, dict) else {}
    return bool(
        row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.status == "open"
        and row.resident_session_id == resident_session_id
        and context.get(_CONSULTATION_RESULT_CONTEXT_KEY) is True
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("message_role") == "workday_consultation_result"
        and context.get("public_reply_owner") == ConversationPublicReplyOwner.FAST.value
    )


async def claim_next_workday_consultation_packet(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    resident_session_id: str,
    lease_seconds: int = 45,
) -> WorkdayConsultationClaim | None:
    """Lease the oldest pending internal packet to the exact Resident process.

    The caller already holds the Project authority lock.  Packet state lives in
    server-created context because Phase 2 deliberately adds no second queue or
    mutable client capability.  A crashed worker's lease is reclaimable; a live
    worker receives the same packet on retry and must ack it before another
    packet is delivered.
    """
    now = _utcnow()
    lease_seconds = min(300, max(15, int(lease_seconds)))
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "system",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.status == "open",
                    ProjectMessage.handling_state.in_(
                        (MessageHandlingState.PENDING, MessageHandlingState.CLAIMED)
                    ),
                    ProjectMessage.resident_session_id == resident_session_id,
                )
                .order_by(*_message_ordering())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if not _is_workday_consultation_packet(
            row, resident_session_id=resident_session_id
        ):
            continue
        context = dict(row.context or {})
        prior_claim_id_raw = context.get(_WORKDAY_CLAIM_ID_CONTEXT_KEY)
        prior_expires_at = _workday_claim_expiry(
            context.get(_WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY)
        )
        prior_generation = context.get(_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY)
        prior_incarnation = context.get(_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY)
        same_owner = (
            row.handling_state == MessageHandlingState.CLAIMED
            and row.handled_by_task_id == task_id
            and prior_generation == worker_generation
            and str(prior_incarnation) == str(worker_incarnation)
            and prior_claim_id_raw is not None
            and prior_expires_at is not None
            and prior_expires_at > now
        )
        if row.handling_state == MessageHandlingState.CLAIMED and not same_owner:
            if prior_expires_at is not None and prior_expires_at > now:
                continue
        try:
            workday_claim_id = (
                uuid.UUID(str(prior_claim_id_raw)) if same_owner else uuid.uuid4()
            )
        except (TypeError, ValueError, AttributeError):
            workday_claim_id = uuid.uuid4()
            same_owner = False
        attempt = int(context.get(_WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY) or 0)
        if not same_owner:
            attempt += 1
        attempt = max(1, attempt)
        expires_at = now + timedelta(seconds=lease_seconds)
        context.update(
            {
                _WORKDAY_CLAIM_ID_CONTEXT_KEY: str(workday_claim_id),
                _WORKDAY_CLAIM_GENERATION_CONTEXT_KEY: worker_generation,
                _WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY: str(worker_incarnation),
                _WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY: expires_at.isoformat(),
                _WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY: attempt,
            }
        )
        row.context = context
        row.handling_state = MessageHandlingState.CLAIMED
        row.handled_by_task_id = task_id
        row.updated_at = now
        session.add(row)
        await session.flush()
        return WorkdayConsultationClaim(
            message=row,
            workday_claim_id=workday_claim_id,
            lease_expires_at=expires_at,
            attempt=attempt,
        )
    return None


async def renew_workday_consultation_packet(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    packet_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    resident_session_id: str,
    workday_claim_id: uuid.UUID,
    lease_seconds: int = 45,
) -> WorkdayConsultationClaim:
    """Renew an exact, still-live Workday delivery lease.

    Unlike an acknowledgement this refuses an expired lease.  That is the
    fence which prevents an old broad-tool Workday turn from retaining delivery
    authority after another process is eligible to reclaim the packet.
    """
    row = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == packet_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or not _is_workday_consultation_packet(
        row, resident_session_id=resident_session_id
    ):
        raise ValueError("consultation packet is unavailable")
    now = _utcnow()
    context = dict(row.context or {})
    expires_at = _workday_claim_expiry(
        context.get(_WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY)
    )
    if (
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or context.get(_WORKDAY_CLAIM_ID_CONTEXT_KEY) != str(workday_claim_id)
        or context.get(_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY) != worker_generation
        or str(context.get(_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY))
        != str(worker_incarnation)
        or expires_at is None
        or expires_at <= now
    ):
        raise ValueError("consultation packet lease is expired or owned by another worker")
    renewed_until = now + timedelta(seconds=min(300, max(15, int(lease_seconds))))
    context[_WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY] = renewed_until.isoformat()
    row.context = context
    row.updated_at = now
    session.add(row)
    await session.flush()
    return WorkdayConsultationClaim(
        message=row,
        workday_claim_id=workday_claim_id,
        lease_expires_at=renewed_until,
        attempt=max(1, int(context.get(_WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY) or 1)),
    )


async def acknowledge_workday_consultation_packet(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    packet_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    resident_session_id: str,
    workday_claim_id: uuid.UUID,
    outcome: str,
    result_summary: str | None = None,
) -> bool:
    """Ack one exact delivery; ``True`` means a retry of an already-completed ack."""
    row = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == packet_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("consultation packet does not exist")
    context = dict(row.context or {})
    normalized_result_summary = (result_summary or "").strip()
    if len(normalized_result_summary) > 20_000:
        raise ValueError("consultation result_summary exceeds 20000 characters")
    if not _is_workday_consultation_packet(
        row, resident_session_id=resident_session_id
    ):
        # A completed packet changes its status but retains the immutable marker
        # solely to make an exact retry harmless.
        if (
            row.from_type == "system"
            and row.surface == MessageSurface.CONVERSATION
            and context.get(_CONSULTATION_PACKET_CONTEXT_KEY) is True
            and row.handled_by_task_id == task_id
            and context.get(_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY)
            == worker_generation
            and str(context.get(_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY))
            == str(worker_incarnation)
            and context.get(_WORKDAY_ACK_CLAIM_ID_CONTEXT_KEY) == str(workday_claim_id)
            and context.get("_workday_consultation_ack_outcome") == "completed"
            and outcome == "completed"
        ):
            if (
                normalized_result_summary
                and context.get(_WORKDAY_RESULT_SUMMARY_CONTEXT_KEY)
                != normalized_result_summary
            ):
                raise ValueError("completed consultation result_summary conflicts")
            return True
        raise ValueError("consultation packet is unavailable")
    if outcome not in {"completed", "abandoned"}:
        raise ValueError("consultation outcome is invalid")
    if outcome == "completed" and not normalized_result_summary:
        raise ValueError("completed consultation requires a result_summary")
    if (
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or context.get(_WORKDAY_CLAIM_ID_CONTEXT_KEY) != str(workday_claim_id)
        or context.get(_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY) != worker_generation
        or str(context.get(_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY))
        != str(worker_incarnation)
    ):
        raise ValueError("consultation packet is owned by another worker")
    now = _utcnow()
    expires_at = _workday_claim_expiry(
        context.get(_WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY)
    )
    if expires_at is None or expires_at <= now:
        raise ValueError("consultation packet lease is expired")
    if outcome == "completed":
        result_key = (
            f"workday-consultation-result:{packet_id}:{workday_claim_id}"
        )
        existing_result = (
            await session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.idempotency_key == result_key,
                )
            )
        ).scalar_one_or_none()
        result_context = stamp_message_audience(
            {
                _CONSULTATION_RESULT_CONTEXT_KEY: True,
                "source_packet_id": str(packet_id),
                "claim_id": context.get("claim_id"),
                "delivery_key": context.get("delivery_key"),
                _CONSULTATION_CONTINUATION_CONTEXT_KEY: context.get(
                    _CONSULTATION_CONTINUATION_CONTEXT_KEY
                ),
                _WORKDAY_ACK_CLAIM_ID_CONTEXT_KEY: str(workday_claim_id),
                "message_role": "workday_consultation_result",
                "public_reply_owner": ConversationPublicReplyOwner.FAST.value,
                "envelope": context.get("envelope"),
            },
            "workday_consultation_result",
            message_type=MessageType.NOTE.value,
        )
        if existing_result is not None:
            existing_context = (
                existing_result.context
                if isinstance(existing_result.context, dict)
                else {}
            )
            if (
                existing_result.from_type != "system"
                or existing_result.content != normalized_result_summary
                or {key: existing_context.get(key) for key in result_context}
                != result_context
            ):
                raise ValueError("consultation result idempotency key conflicts")
        else:
            sequence = await lock_project_and_next_message_sequence(session, project_id)
            result = await build_project_message(
                session,
                project_id=project_id,
                sequence=sequence,
                surface=MessageSurface.CONVERSATION,
                handling_state=MessageHandlingState.PENDING,
                message_type=MessageType.NOTE,
                from_type="system",
                content=normalized_result_summary,
                context=result_context,
                idempotency_key=result_key,
                resident_session_id=resident_session_id,
            )
            session.add(result)
        context[_WORKDAY_ACK_CLAIM_ID_CONTEXT_KEY] = str(workday_claim_id)
        context["_workday_consultation_ack_outcome"] = "completed"
        context[_WORKDAY_RESULT_SUMMARY_CONTEXT_KEY] = normalized_result_summary
        row.context = context
        row.status = "handled"
        row.handling_state = MessageHandlingState.RESPONDED
        row.updated_at = now
        session.add(row)
        await session.flush()
        return False
    for key in (
        _WORKDAY_CLAIM_ID_CONTEXT_KEY,
        _WORKDAY_CLAIM_GENERATION_CONTEXT_KEY,
        _WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY,
        _WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY,
        _WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY,
    ):
        context.pop(key, None)
    row.context = context
    row.handling_state = MessageHandlingState.PENDING
    row.handled_by_task_id = None
    row.updated_at = now
    session.add(row)
    await session.flush()
    return False


async def claim_next_workday_consultation_continuation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    resident_session_id: str,
    lease_seconds: int = 45,
) -> WorkdayConsultationContinuationClaim | None:
    """Lease one completed Workday result to Fast for an eventual public follow-up.

    The result itself is server-created and private.  This is a new claim, not
    a reopening of the already-answered user turn, so one original input and
    one later investigation update cannot contend for the same receipt.
    """
    now = _utcnow()
    lease_seconds = min(300, max(15, int(lease_seconds)))
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.from_type == "system",
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.status == "open",
                    ProjectMessage.handling_state.in_(
                        (MessageHandlingState.PENDING, MessageHandlingState.CLAIMED)
                    ),
                    ProjectMessage.resident_session_id == resident_session_id,
                )
                .order_by(*_message_ordering())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if not _is_workday_consultation_result(
            row, resident_session_id=resident_session_id
        ):
            continue
        context = dict(row.context or {})
        prior_claim_id = context.get(_CONTINUATION_CLAIM_ID_CONTEXT_KEY)
        prior_expires_at = _workday_claim_expiry(
            context.get(_CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY)
        )
        same_owner = (
            row.handling_state == MessageHandlingState.CLAIMED
            and row.handled_by_task_id == task_id
            and context.get(_CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY)
            == worker_generation
            and str(context.get(_CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY))
            == str(worker_incarnation)
            and prior_claim_id is not None
            and prior_expires_at is not None
            and prior_expires_at > now
        )
        if row.handling_state == MessageHandlingState.CLAIMED and not same_owner:
            if prior_expires_at is not None and prior_expires_at > now:
                continue
        try:
            continuation_claim_id = (
                uuid.UUID(str(prior_claim_id)) if same_owner else uuid.uuid4()
            )
        except (TypeError, ValueError, AttributeError):
            continuation_claim_id = uuid.uuid4()
            same_owner = False
        attempt = int(context.get(_CONTINUATION_CLAIM_ATTEMPT_CONTEXT_KEY) or 0)
        if not same_owner:
            attempt += 1
        attempt = max(1, attempt)
        expires_at = now + timedelta(seconds=lease_seconds)
        context.update(
            {
                _CONTINUATION_CLAIM_ID_CONTEXT_KEY: str(continuation_claim_id),
                _CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY: worker_generation,
                _CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY: str(worker_incarnation),
                _CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY: expires_at.isoformat(),
                _CONTINUATION_CLAIM_ATTEMPT_CONTEXT_KEY: attempt,
            }
        )
        row.context = context
        row.handling_state = MessageHandlingState.CLAIMED
        row.handled_by_task_id = task_id
        row.updated_at = now
        session.add(row)
        await session.flush()
        return WorkdayConsultationContinuationClaim(
            message=row,
            continuation_claim_id=continuation_claim_id,
            lease_expires_at=expires_at,
            attempt=attempt,
        )
    return None


async def renew_workday_consultation_continuation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    result_packet_id: uuid.UUID,
    continuation_claim_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    resident_session_id: str,
    lease_seconds: int = 45,
) -> WorkdayConsultationContinuationClaim:
    """Renew one exact, still-live Fast continuation receipt."""
    row = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == result_packet_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or not _is_workday_consultation_result(
        row, resident_session_id=resident_session_id
    ):
        raise ValueError("consultation continuation is unavailable")
    now = _utcnow()
    context = dict(row.context or {})
    expires_at = _workday_claim_expiry(
        context.get(_CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY)
    )
    if (
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or context.get(_CONTINUATION_CLAIM_ID_CONTEXT_KEY)
        != str(continuation_claim_id)
        or context.get(_CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY)
        != worker_generation
        or str(context.get(_CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY))
        != str(worker_incarnation)
        or expires_at is None
        or expires_at <= now
    ):
        raise ValueError(
            "consultation continuation is expired or owned by another worker"
        )
    renewed_until = now + timedelta(
        seconds=min(300, max(15, int(lease_seconds)))
    )
    context[_CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY] = renewed_until.isoformat()
    row.context = context
    row.updated_at = now
    session.add(row)
    await session.flush()
    return WorkdayConsultationContinuationClaim(
        message=row,
        continuation_claim_id=continuation_claim_id,
        lease_expires_at=renewed_until,
        attempt=max(
            1, int(context.get(_CONTINUATION_CLAIM_ATTEMPT_CONTEXT_KEY) or 1)
        ),
    )


async def release_workday_consultation_continuation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    result_packet_id: uuid.UUID,
    continuation_claim_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    resident_session_id: str,
) -> str:
    """Release a failed Fast continuation without reopening completed output."""
    row = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == result_packet_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("consultation continuation does not exist")
    context = dict(row.context or {})
    if (
        row.status != "open"
        or row.handling_state == MessageHandlingState.RESPONDED
        or context.get("_workday_continuation_completion_idempotency_key")
    ):
        return "already_completed"
    if not _is_workday_consultation_result(
        row, resident_session_id=resident_session_id
    ):
        raise ValueError("consultation continuation is unavailable")
    existing_claim_id = context.get(_CONTINUATION_CLAIM_ID_CONTEXT_KEY)
    if (
        row.handling_state == MessageHandlingState.PENDING
        and existing_claim_id is None
    ):
        return "already_released"
    if (
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or existing_claim_id != str(continuation_claim_id)
        or context.get(_CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY)
        != worker_generation
        or str(context.get(_CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY))
        != str(worker_incarnation)
    ):
        raise ValueError("consultation continuation is owned by another worker")
    for key in (
        _CONTINUATION_CLAIM_ID_CONTEXT_KEY,
        _CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY,
        _CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY,
        _CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY,
        _CONTINUATION_CLAIM_ATTEMPT_CONTEXT_KEY,
    ):
        context.pop(key, None)
    row.context = context
    row.handling_state = MessageHandlingState.PENDING
    row.handled_by_task_id = None
    row.updated_at = _utcnow()
    session.add(row)
    await session.flush()
    return "released"


async def complete_workday_consultation_continuation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    result_packet_id: uuid.UUID,
    continuation_claim_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    resident_session_id: str,
    content: str,
    idempotency_key: str,
) -> PublicConversationReply:
    """Commit exactly one Fast-owned public continuation from a Workday result."""
    normalized_content = content.strip()
    normalized_key = idempotency_key.strip()
    if not normalized_content:
        raise ValueError("continuation content is required")
    if not normalized_key or len(normalized_key) > 255:
        raise ValueError("idempotency_key must be a non-empty <=255-character value")
    existing = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == normalized_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        context = existing.context if isinstance(existing.context, dict) else {}
        if (
            existing.from_type != "strategic"
            or existing.from_task_id != task_id
            or existing.content != normalized_content
            or context.get("_workday_continuation_claim_id")
            != str(continuation_claim_id)
        ):
            raise ValueError("idempotency_key already belongs to a different continuation")
        return PublicConversationReply(message=existing, duplicate=True)
    row = (
        await session.execute(
            select(ProjectMessage)
            .where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == result_packet_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or not _is_workday_consultation_result(
        row, resident_session_id=resident_session_id
    ):
        raise ValueError("consultation continuation is unavailable")
    context = dict(row.context or {})
    now = _utcnow()
    expires_at = _workday_claim_expiry(
        context.get(_CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY)
    )
    if (
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or context.get(_CONTINUATION_CLAIM_ID_CONTEXT_KEY)
        != str(continuation_claim_id)
        or context.get(_CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY)
        != worker_generation
        or str(context.get(_CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY))
        != str(worker_incarnation)
        or expires_at is None
        or expires_at <= now
    ):
        raise ValueError("consultation continuation is expired or owned by another worker")
    sequence = await lock_project_and_next_message_sequence(session, project_id)
    reply = await build_project_message(
        session,
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        message_type=MessageType.NOTE,
        from_type="strategic",
        from_task_id=task_id,
        content=normalized_content,
        context=stamp_message_audience(
            {
                "message_role": "workday_consultation_continuation",
                "_workday_continuation_claim_id": str(continuation_claim_id),
                "_workday_result_packet_id": str(result_packet_id),
                _PUBLIC_REPLY_OWNER_CONTEXT_KEY: ConversationPublicReplyOwner.FAST.value,
                "_resident_reply_covered_message_ids": [],
            },
            "question_answer",
            message_type=MessageType.NOTE.value,
        ),
        idempotency_key=normalized_key,
        resident_session_id=resident_session_id,
    )
    session.add(reply)
    context["_workday_continuation_completion_idempotency_key"] = normalized_key
    row.context = context
    row.status = "handled"
    row.handling_state = MessageHandlingState.RESPONDED
    row.updated_at = now
    session.add(row)
    await session.flush()
    return PublicConversationReply(message=reply, duplicate=False)


async def abandon_fast_public_turn(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    claim_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    incarnation_id: uuid.UUID,
) -> str:
    """Release an exact failed Fast turn without reopening a completed one.

    Returns ``abandoned``, ``already_abandoned``, or ``already_completed``.
    The caller has already proved current Fast process authority at the Project
    boundary; this function locks the rows and makes the response decision from
    durable state, so a completion/timeout race never reopens user input.
    """
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.conversation_claim_id == claim_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    replies = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.from_type == "strategic",
            )
        )
    ).scalars().all()
    reply_exists = any(
        isinstance(reply.context, dict)
        and reply.context.get("_conversation_claim_id") == str(claim_id)
        for reply in replies
    )
    if reply_exists or any(
        row.handling_state == MessageHandlingState.RESPONDED
        or row.status != "open"
        for row in rows
    ):
        return "already_completed"
    if not rows:
        return "already_abandoned"
    if any(
        row.from_type != "user"
        or row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or row.conversation_claim_generation != worker_generation
        or row.conversation_claim_incarnation != incarnation_id
        or (row.context or {}).get(_PUBLIC_REPLY_OWNER_CONTEXT_KEY)
        != ConversationPublicReplyOwner.FAST.value
        for row in rows
    ):
        raise ValueError("conversation claim is owned by another worker")
    now = _utcnow()
    for row in rows:
        context = dict(row.context or {})
        context.pop(_PUBLIC_REPLY_OWNER_CONTEXT_KEY, None)
        row.context = context
        row.status = "open"
        row.handling_state = MessageHandlingState.PENDING
        row.handled_by_task_id = None
        row.conversation_claim_id = None
        row.conversation_claim_generation = None
        row.conversation_claim_incarnation = None
        row.conversation_claim_expires_at = None
        row.updated_at = now
        session.add(row)
    await session.flush()
    return "abandoned"


async def complete_claimed_public_turn(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    claim_id: uuid.UUID,
    task_id: uuid.UUID,
    worker_generation: int,
    incarnation_id: uuid.UUID,
    owner: ConversationPublicReplyOwner,
    content: str,
    idempotency_key: str,
    resident_session_id: str | None = None,
    workday_request: dict[str, Any] | None = None,
    envelope: Any | None = None,
) -> PublicConversationReply:
    """Commit exactly one owner-fenced public reply and acknowledge its input.

    Workday intentionally has no value in ``ConversationPublicReplyOwner``;
    its packet APIs cannot reach this mutation.
    """
    normalized_content = content.strip()
    normalized_key = idempotency_key.strip()
    if not normalized_content:
        raise ValueError("public reply content is required")
    if not normalized_key or len(normalized_key) > 255:
        raise ValueError("idempotency_key must be a non-empty <=255-character value")
    normalized_workday_request: tuple[str, str, str] | None = None
    if workday_request is not None:
        if not isinstance(workday_request, dict):
            raise ValueError("workday_request must be an object")
        delivery_key = str(workday_request.get("delivery_key") or "").strip()
        question = str(workday_request.get("question") or "").strip()
        continuation_key = str(
            workday_request.get("continuation_key") or "initial"
        ).strip()
        if not delivery_key or len(delivery_key) > 128:
            raise ValueError("workday delivery_key must be a non-empty <=128-character value")
        if not question or len(question) > 20_000:
            raise ValueError("workday question must be a non-empty <=20000-character value")
        if not continuation_key or len(continuation_key) > 128:
            raise ValueError("workday continuation_key must be a non-empty <=128-character value")
        if envelope is None:
            raise ValueError("workday_request requires the exact context envelope")
        normalized_workday_request = (delivery_key, question, continuation_key)
    existing = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == normalized_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        context = existing.context if isinstance(existing.context, dict) else {}
        if (
            existing.from_type != "strategic"
            or existing.from_task_id != task_id
            or existing.content != normalized_content
            or context.get("_conversation_claim_id") != str(claim_id)
            or context.get(_PUBLIC_REPLY_OWNER_CONTEXT_KEY) != owner.value
        ):
            raise ValueError("idempotency_key already belongs to a different public reply")
        if normalized_workday_request is None:
            associated_packets = (
                await session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project_id,
                        ProjectMessage.from_type == "system",
                    )
                )
            ).scalars().all()
            if any(
                isinstance(packet.context, dict)
                and packet.context.get(_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY)
                == str(existing.id)
                for packet in associated_packets
            ):
                raise ValueError("idempotency_key already completed with a Workday request")
            return PublicConversationReply(message=existing, duplicate=True)
        delivery_key, question, continuation_key = normalized_workday_request
        expected_packet_key = workday_consultation_packet_idempotency_key(
            claim_id, delivery_key, continuation_key
        )
        packet = (
            await session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.idempotency_key == expected_packet_key,
                )
            )
        ).scalar_one_or_none()
        packet_context = packet.context if packet is not None and isinstance(packet.context, dict) else {}
        if (
            packet is None
            or packet.content != question
            or packet_context.get(_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY)
            != str(existing.id)
        ):
            raise ValueError("idempotency_key Workday request conflicts with existing reply")
        return PublicConversationReply(
            message=existing,
            duplicate=True,
            consultation_packet=packet,
        )

    now = _utcnow()
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.conversation_claim_id == claim_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not rows or any(
        row.from_type != "user"
        or row.status != "open"
        or row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or row.conversation_claim_generation != worker_generation
        or row.conversation_claim_incarnation != incarnation_id
        or row.conversation_claim_expires_at is None
        or row.conversation_claim_expires_at <= now
        or (row.context or {}).get(_PUBLIC_REPLY_OWNER_CONTEXT_KEY) != owner.value
        for row in rows
    ):
        raise ValueError("conversation claim is expired, superseded, or owned by another lane")

    sequence = await lock_project_and_next_message_sequence(session, project_id)
    reply = await build_project_message(
        session,
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        message_type=MessageType.NOTE,
        from_type="strategic",
        from_task_id=task_id,
        content=normalized_content,
        context=stamp_message_audience(
            {
                "message_role": "operator_reply",
                "_conversation_claim_id": str(claim_id),
                _PUBLIC_REPLY_OWNER_CONTEXT_KEY: owner.value,
                "_resident_reply_covered_message_ids": sorted(str(row.id) for row in rows),
            },
            "question_answer",
            message_type=MessageType.NOTE.value,
        ),
        idempotency_key=normalized_key,
        resident_session_id=resident_session_id,
    )
    session.add(reply)
    consultation_packet: ProjectMessage | None = None
    if normalized_workday_request is not None:
        delivery_key, question, continuation_key = normalized_workday_request
        claim = ConversationClaim(
            claim_id=claim_id,
            rows=tuple(rows),
            lease_expires_at=max(
                row.conversation_claim_expires_at
                for row in rows
                if row.conversation_claim_expires_at is not None
            ),
            attempt=max(row.conversation_claim_attempt for row in rows),
        )
        packet = await create_or_get_workday_consultation_packet(
            session,
            project_id=project_id,
            claim=claim,
            envelope=envelope,
            delivery_key=delivery_key,
            question=question,
            continuation_key=continuation_key,
            resident_session_id=resident_session_id,
            atomic_public_reply_id=reply.id,
        )
        consultation_packet = packet.message
    for row in rows:
        row.status = "handled"
        row.handling_state = MessageHandlingState.RESPONDED
        row.conversation_claim_expires_at = None
        row.updated_at = now
        session.add(row)
    await session.flush()
    return PublicConversationReply(
        message=reply,
        duplicate=False,
        consultation_packet=consultation_packet,
    )
