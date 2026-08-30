"""Durable foreground conversation admission for Strategic Residents.

The message row is the canonical operator input. ``conversation_claim_id``
groups the exact rows admitted to one foreground turn, while the short lease
and worker-generation fence make that turn reclaimable after a provider,
process, task, or runner failure.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from fastapi import HTTPException
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

logger = logging.getLogger(__name__)


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
    prior_attempts: tuple[dict[str, Any], ...] = ()
    prior_receipts: tuple[dict[str, Any], ...] = ()


class WorkdayConsultationPacketProvenanceInvalid(ValueError):
    """A private packet cannot be projected without trusting corrupt state."""

    def __init__(self, packet_id: uuid.UUID):
        super().__init__("Workday consultation packet provenance is invalid")
        self.packet_id = packet_id


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
_WORKDAY_FAILED_RESULT_SUMMARY = (
    "The private Workday consultation could not produce a usable result after "
    "two attempts. This failure notice itself does not verify whether any managed "
    "task or state change occurred; only separately listed durable receipts and "
    "current canonical state do."
)
_CONSULTATION_RESULT_CONTEXT_KEY = "_workday_consultation_result"
_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY = "_workday_consultation_atomic_reply_id"
_CONSULTATION_REQUESTED_CONTEXT_KEY = "_workday_consultation_requested"
# Last renewal/ack gate failure (409) recorded against this packet, so an
# incident carries its own diagnosis instead of a bare, unexplained 409. See
# record_workday_consultation_gate_failure.
_WORKDAY_LAST_FAILURE_CONTEXT_KEY = "_workday_consultation_last_failure"
_WORKDAY_ATTEMPTS_CONTEXT_KEY = "_workday_consultation_attempts"
_WORKDAY_ATTEMPT_LIMIT = 8
_WORKDAY_CLAIM_AUTHORITIES_CONTEXT_KEY = "_workday_consultation_claim_authorities"
_WORKDAY_CLAIM_STARTED_CONTEXT_KEY = "_workday_consultation_claim_started_at"
_WORKDAY_SERVER_MISSING_PREDICATES = frozenset(
    {
        "claim_lease_expired_without_ack",
        "resident_session_handoff_without_ack",
    }
)
# A packet reclaimed this many times without a terminal ack is parked
# (finalized as 'failed') at claim time instead of being requeued forever. A
# live worker is expected to self-escalate to a 'failed' ack by attempt 2
# (see the agent's scheduleTypedConsultation); this is the server-side
# backstop for a worker that dies before it can deliver that ack.
_WORKDAY_CONSULTATION_MAX_UNACKED_CLAIMS = 3
_CONTINUATION_CLAIM_ID_CONTEXT_KEY = "_workday_continuation_claim_id"
_CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY = "_workday_continuation_claim_generation"
_CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY = "_workday_continuation_claim_incarnation"
_CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY = "_workday_continuation_claim_expires_at"
_CONTINUATION_CLAIM_ATTEMPT_CONTEXT_KEY = "_workday_continuation_claim_attempt"
_CONVERSATION_CLAIM_IDEMPOTENCY_CONTEXT_KEY = (
    "_conversation_claim_idempotency_key"
)
# An operator turn whose claim has DIED this many times without ever producing
# a public reply is answered-and-closed instead of being handed out again. The
# private Workday consultation lane has had this backstop
# (_WORKDAY_CONSULTATION_MAX_UNACKED_CLAIMS); the operator lane -- the one a
# *human* is waiting on -- had none, so a turn whose worker kept dying (or kept
# moving on without replying) churned without bound and the operator was never
# told anything at all. Higher than the consultation cap because this bound is
# the last resort behind ordinary reclaim and redelivery, not the first.
_OPERATOR_TURN_MAX_DEAD_CLAIMS = 5
# Durable per-turn count of claims that ended in a dead lease. Incremented by
# claim_operator_messages when it reclaims over a corpse, and by the reaper's
# expired-claim release sweep. Deliberately NOT conversation_claim_attempt:
# that field counts every handout, including benign Resident session rebinds,
# and is routinely in double digits on a healthy long-lived operator message.
_OPERATOR_TURN_DEAD_CLAIMS_CONTEXT_KEY = "_conversation_turn_dead_claims"
_OPERATOR_TURN_PARKED_CONTEXT_KEY = "_conversation_turn_parked"
_OPERATOR_TURN_PARKED_REASON_CONTEXT_KEY = "_conversation_turn_parked_reason"
_OPERATOR_TURN_PARKED_DEAD_CLAIMS_CONTEXT_KEY = "_conversation_turn_parked_deaths"


class OperatorTurnParked(RuntimeError):
    """A hopeless operator turn was answered-and-closed, not re-claimed.

    Raised by :func:`claim_operator_messages` instead of returning ``None`` so
    the caller cannot mistake a terminal park for an ordinary "someone else
    holds it" conflict, and so the reason reaches the operator-facing error
    instead of being flattened into a bare 409. The park itself is already
    written to the session; the caller must COMMIT, not roll back.
    """

    def __init__(
        self,
        reason: str,
        *,
        dead_claims: int,
        message_ids: tuple[uuid.UUID, ...],
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.dead_claims = dead_claims
        self.message_ids = tuple(message_ids)


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


def _operator_turn_parked_reason(dead_claims: int) -> str:
    """Explain WHY the turn was closed, in words the operator can act on."""
    return (
        "I could not process this message. The conversation turn was handed to a "
        f"strategist worker and its claim died {dead_claims} times without ever "
        "producing a reply -- each lease expired with its worker gone or moved "
        "on. Rather than hold your message open indefinitely with no answer and "
        "no explanation, it is being closed with this one. Nothing was decided "
        "or changed on your behalf. Send it again to retry; if this repeats, "
        "the Strategic Resident session itself is unhealthy."
    )


async def _park_operator_turn(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    rows: tuple[ProjectMessage, ...],
    claim_id: uuid.UUID,
    task_id: uuid.UUID,
    dead_claims: int,
    reason: str,
    resident_session_id: str | None,
    now: datetime,
) -> ProjectMessage:
    """Answer and close a hopeless operator turn instead of stranding it.

    Degrade-or-decide: the operator gets a real conversation reply saying the
    system failed and why, and the input rows reach a terminal state, so no
    message is left silently open forever waiting on a worker that will never
    come back. Caller owns the transaction and every ownership check.
    """
    park_key = f"operator-turn-parked:{claim_id}"
    reply = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.idempotency_key == park_key,
            )
        )
    ).scalar_one_or_none()
    if reply is None:
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
            content=reason,
            context=stamp_message_audience(
                {
                    "message_role": "operator_reply",
                    "_conversation_claim_id": str(claim_id),
                    _OPERATOR_TURN_PARKED_CONTEXT_KEY: True,
                    _OPERATOR_TURN_PARKED_DEAD_CLAIMS_CONTEXT_KEY: dead_claims,
                    "_resident_reply_covered_message_ids": sorted(
                        str(row.id) for row in rows
                    ),
                },
                "question_answer",
                message_type=MessageType.NOTE.value,
            ),
            idempotency_key=park_key,
            resident_session_id=resident_session_id,
        )
        session.add(reply)
    for row in rows:
        row.context = {
            **dict(row.context or {}),
            _OPERATOR_TURN_PARKED_CONTEXT_KEY: True,
            _OPERATOR_TURN_PARKED_REASON_CONTEXT_KEY: reason,
            _OPERATOR_TURN_PARKED_DEAD_CLAIMS_CONTEXT_KEY: dead_claims,
        }
        row.status = "handled"
        row.handling_state = MessageHandlingState.RESPONDED
        row.handled_by_task_id = task_id
        row.conversation_claim_expires_at = None
        row.updated_at = now
        session.add(row)
    logger.warning(
        "parked operator conversation turn after %d dead claims: "
        "project=%s claim=%s messages=%s",
        dead_claims,
        project_id,
        claim_id,
        ",".join(str(row.id) for row in rows),
    )
    return reply


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
    # Count only the handouts that provably ENDED BADLY: a reclaim over a
    # corpse lease here, or a release by the reaper's expired-claim sweep. A
    # benign rebind (Resident session handoff, which clears the claim id) is
    # NOT a death and must never push a still-answerable turn toward the cap --
    # that is why this is its own counter and not ``conversation_claim_attempt``,
    # which carries every handout forward across sessions and is routinely in
    # double digits on a healthy long-lived operator message.
    dead_claims = max(
        (
            int((row.context or {}).get(_OPERATOR_TURN_DEAD_CLAIMS_CONTEXT_KEY) or 0)
            for row in ordered
        ),
        default=0,
    )
    if reclaimed and lease_expired:
        dead_claims += 1
    if dead_claims >= _OPERATOR_TURN_MAX_DEAD_CLAIMS:
        # This turn's claim has died _OPERATOR_TURN_MAX_DEAD_CLAIMS times
        # without ever producing a public reply. Handing it out again repeats
        # the same failure while the operator keeps waiting on silence, so
        # decide instead: answer the turn with the failure and close it.
        reason = _operator_turn_parked_reason(dead_claims)
        await _park_operator_turn(
            session,
            project_id=project_id,
            rows=ordered,
            claim_id=existing_claim_id or claim_id,
            task_id=task_id,
            dead_claims=dead_claims,
            reason=reason,
            resident_session_id=session_id,
            now=now,
        )
        await session.flush()
        raise OperatorTurnParked(
            reason,
            dead_claims=dead_claims,
            message_ids=tuple(row.id for row in ordered),
        )

    for row in ordered:
        row.handling_state = MessageHandlingState.CLAIMED
        row.handled_by_task_id = task_id
        row.conversation_claim_id = claim_id
        row.conversation_claim_generation = worker_generation
        row.conversation_claim_incarnation = incarnation_id
        row.conversation_claim_expires_at = expires_at
        row.conversation_claim_attempt = attempt
        context_updates: dict[str, Any] = {}
        if dead_claims:
            # Durable so the count survives process replacement -- the whole
            # point is to bound a turn whose workers keep disappearing.
            context_updates[_OPERATOR_TURN_DEAD_CLAIMS_CONTEXT_KEY] = dead_claims
        if public_reply_owner is not None:
            context_updates[_PUBLIC_REPLY_OWNER_CONTEXT_KEY] = public_reply_owner.value
            if normalized_claim_key is not None:
                context_updates[_CONVERSATION_CLAIM_IDEMPOTENCY_CONTEXT_KEY] = (
                    normalized_claim_key
                )
        if context_updates:
            row.context = {**dict(row.context or {}), **context_updates}
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
            or (
                atomic_public_reply_id is not None
                and existing.reply_to_message_id != atomic_public_reply_id
            )
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
        reply_to_message_id=atomic_public_reply_id,
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
    resident_session_id: str | None,
) -> bool:
    context = row.context if isinstance(row.context, dict) else {}
    return bool(
        row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.status == "open"
        and (
            resident_session_id is None
            or row.resident_session_id == resident_session_id
        )
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
    resident_session_id: str | None,
) -> bool:
    context = row.context if isinstance(row.context, dict) else {}
    return bool(
        row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and row.status == "open"
        and (
            resident_session_id is None
            or row.resident_session_id == resident_session_id
        )
        and context.get(_CONSULTATION_RESULT_CONTEXT_KEY) is True
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("message_role") == "workday_consultation_result"
        and context.get("public_reply_owner") == ConversationPublicReplyOwner.FAST.value
    )


def _workday_consultation_parked_reason(attempt: int, context: dict[str, Any]) -> str:
    """Explain WHY a packet was parked, not just that it was — attempt count
    plus the last recorded gate-failure detail (if any; see
    record_workday_consultation_gate_failure) so the incident carries its own
    diagnosis instead of a silent terminal state."""
    last_failure = context.get(_WORKDAY_LAST_FAILURE_CONTEXT_KEY)
    detail_suffix = ""
    if isinstance(last_failure, dict) and last_failure.get("detail"):
        gate = last_failure.get("gate") or "unknown"
        detail_suffix = f" Last recorded gate failure ({gate}): {last_failure['detail']}"
    return (
        "The private Workday consultation was parked after "
        f"{attempt} claim attempts without a terminal acknowledgement (each prior "
        "claim's lease expired, or its worker exited before it could ack) rather "
        f"than being requeued indefinitely.{detail_suffix}"
    )


def _normalize_workday_attempt_diagnostic(value: object) -> dict[str, Any]:
    """Validate the bounded durable evidence for one exact private attempt."""

    if not isinstance(value, dict):
        raise ValueError("consultation attempt diagnostic is invalid")
    attempt = value.get("attempt")
    claim_id = value.get("workday_claim_id")
    outcome = value.get("outcome")
    predicate = value.get("terminal_predicate")
    try:
        normalized_claim_id = str(uuid.UUID(str(claim_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("consultation attempt diagnostic claim is invalid") from None
    if (
        not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or not 1 <= attempt <= _WORKDAY_ATTEMPT_LIMIT
        or outcome not in {"completed", "abandoned", "failed"}
        or not isinstance(predicate, str)
        or not predicate.strip()
        or len(predicate) > 80
    ):
        raise ValueError("consultation attempt diagnostic identity is invalid")
    started_at = _workday_claim_expiry(value.get("started_at"))
    ended_at = _workday_claim_expiry(value.get("ended_at"))
    if started_at is None or ended_at is None or ended_at < started_at:
        raise ValueError("consultation attempt diagnostic timing is invalid")

    def _bounded_optional_text(key: str, limit: int) -> str | None:
        raw = value.get(key)
        if raw is None:
            return None
        if not isinstance(raw, str) or len(raw) > limit:
            raise ValueError(f"consultation attempt diagnostic {key} is invalid")
        return raw

    def _nonnegative_int(key: str, maximum: int) -> int:
        raw = value.get(key)
        if (
            not isinstance(raw, int)
            or isinstance(raw, bool)
            or raw < 0
            or raw > maximum
        ):
            raise ValueError(f"consultation attempt diagnostic {key} is invalid")
        return raw

    ttft_ms = value.get("ttft_ms")
    if ttft_ms is not None and (
        not isinstance(ttft_ms, int)
        or isinstance(ttft_ms, bool)
        or not 0 <= ttft_ms <= 86_400_000
    ):
        raise ValueError("consultation attempt diagnostic ttft_ms is invalid")
    raw_trace = value.get("trace")
    if not isinstance(raw_trace, list) or len(raw_trace) > 128:
        raise ValueError("consultation attempt diagnostic trace is invalid")
    trace: list[dict[str, Any]] = []
    for raw_event in raw_trace:
        if not isinstance(raw_event, dict):
            raise ValueError("consultation attempt trace event is invalid")
        elapsed_ms = raw_event.get("elapsed_ms")
        kind = raw_event.get("kind")
        label = raw_event.get("label")
        is_error = raw_event.get("is_error")
        if (
            not isinstance(elapsed_ms, int)
            or isinstance(elapsed_ms, bool)
            or not 0 <= elapsed_ms <= 86_400_000
            or kind not in {"text", "tool", "tool_result", "error", "result"}
            or (label is not None and (not isinstance(label, str) or len(label) > 80))
            or (is_error is not None and not isinstance(is_error, bool))
        ):
            raise ValueError("consultation attempt trace event is invalid")
        trace.append(
            {
                "elapsed_ms": elapsed_ms,
                "kind": kind,
                **({"label": label} if label else {}),
                **({"is_error": is_error} if is_error is not None else {}),
            }
        )
    trace_truncated = value.get("trace_truncated", False)
    if not isinstance(trace_truncated, bool):
        raise ValueError("consultation attempt diagnostic trace_truncated is invalid")
    return {
        "attempt": attempt,
        "workday_claim_id": normalized_claim_id,
        "outcome": outcome,
        "terminal_predicate": predicate.strip(),
        "provider_terminal_reason": _bounded_optional_text(
            "provider_terminal_reason", 255
        ),
        "provider_output_tail": _bounded_optional_text("provider_output_tail", 2_000),
        "started_at": started_at.replace(tzinfo=timezone.utc).isoformat(),
        "ended_at": ended_at.replace(tzinfo=timezone.utc).isoformat(),
        "turn_ms": _nonnegative_int("turn_ms", 86_400_000),
        "ttft_ms": ttft_ms,
        "tool_call_count": _nonnegative_int("tool_call_count", 100_000),
        "tool_call_duration_ms": _nonnegative_int(
            "tool_call_duration_ms", 86_400_000
        ),
        "result_chars": _nonnegative_int("result_chars", 20_000),
        "trace": trace,
        "trace_truncated": trace_truncated,
    }


def _append_workday_attempt(
    context: dict[str, Any], diagnostic: dict[str, Any]
) -> None:
    raw_attempts = context.get(_WORKDAY_ATTEMPTS_CONTEXT_KEY)
    attempts = list(raw_attempts) if isinstance(raw_attempts, list) else []
    if any(not isinstance(item, dict) for item in attempts):
        raise ValueError("consultation attempt history is invalid")
    prior = next(
        (
            item
            for item in attempts
            if item.get("workday_claim_id") == diagnostic["workday_claim_id"]
        ),
        None,
    )
    if prior is not None and prior != diagnostic:
        raise ValueError("consultation attempt diagnostic conflicts with prior acknowledgement")
    if prior is None:
        attempts.append(diagnostic)
    context[_WORKDAY_ATTEMPTS_CONTEXT_KEY] = attempts[-_WORKDAY_ATTEMPT_LIMIT:]


def _workday_claim_authorities(context: dict[str, Any]) -> list[dict[str, Any]]:
    raw = context.get(_WORKDAY_CLAIM_AUTHORITIES_CONTEXT_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > _WORKDAY_ATTEMPT_LIMIT:
        raise ValueError("consultation claim authority history is invalid")
    authorities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("consultation claim authority is invalid")
        try:
            claim_id = str(uuid.UUID(str(item.get("workday_claim_id"))))
            task_id = str(uuid.UUID(str(item.get("task_id"))))
            incarnation = str(uuid.UUID(str(item.get("worker_incarnation"))))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("consultation claim authority identity is invalid") from None
        attempt = item.get("attempt")
        generation = item.get("worker_generation")
        session_id = item.get("resident_session_id")
        issued_at = _workday_claim_expiry(item.get("issued_at"))
        diagnostic_state = item.get("diagnostic_state")
        if (
            claim_id in seen
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or not 1 <= attempt <= _WORKDAY_ATTEMPT_LIMIT
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(session_id, str)
            or not session_id
            or issued_at is None
            or diagnostic_state
            not in {"open", "server_missing", "recorded", "acknowledged"}
        ):
            raise ValueError("consultation claim authority is invalid")
        seen.add(claim_id)
        authorities.append(
            {
                "workday_claim_id": claim_id,
                "attempt": attempt,
                "task_id": task_id,
                "resident_session_id": session_id,
                "worker_generation": generation,
                "worker_incarnation": incarnation,
                "issued_at": issued_at.replace(tzinfo=timezone.utc).isoformat(),
                "diagnostic_state": diagnostic_state,
            }
        )
    return authorities


def _register_workday_claim_authority(
    context: dict[str, Any],
    *,
    workday_claim_id: uuid.UUID,
    attempt: int,
    task_id: uuid.UUID,
    resident_session_id: str,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    issued_at: datetime,
) -> dict[str, Any]:
    authorities = _workday_claim_authorities(context)
    candidate = {
        "workday_claim_id": str(workday_claim_id),
        "attempt": attempt,
        "task_id": str(task_id),
        "resident_session_id": resident_session_id,
        "worker_generation": worker_generation,
        "worker_incarnation": str(worker_incarnation),
        "issued_at": issued_at.replace(tzinfo=timezone.utc).isoformat(),
        "diagnostic_state": "open",
    }
    prior = next(
        (
            item
            for item in authorities
            if item["workday_claim_id"] == candidate["workday_claim_id"]
        ),
        None,
    )
    if prior is not None:
        if {**prior, "diagnostic_state": "open"} != candidate:
            raise ValueError("consultation claim authority conflicts")
        return prior
    authorities.append(candidate)
    context[_WORKDAY_CLAIM_AUTHORITIES_CONTEXT_KEY] = authorities[
        -_WORKDAY_ATTEMPT_LIMIT:
    ]
    return candidate


def _set_workday_claim_diagnostic_state(
    context: dict[str, Any], workday_claim_id: uuid.UUID, state: str
) -> dict[str, Any]:
    authorities = _workday_claim_authorities(context)
    matched = next(
        (
            item
            for item in authorities
            if item["workday_claim_id"] == str(workday_claim_id)
        ),
        None,
    )
    if matched is None:
        raise ValueError("consultation claim diagnostic authority is absent")
    matched["diagnostic_state"] = state
    context[_WORKDAY_CLAIM_AUTHORITIES_CONTEXT_KEY] = authorities
    return matched


def _is_server_missing_workday_attempt(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("terminal_predicate") in _WORKDAY_SERVER_MISSING_PREDICATES
        and value.get("provider_terminal_reason") is None
        and value.get("provider_output_tail") is None
        and value.get("tool_call_count") == 0
        and value.get("tool_call_duration_ms") == 0
        and value.get("result_chars") == 0
        and isinstance(value.get("trace"), list)
        and len(value["trace"]) <= 1
        and value.get("trace_truncated") is True
    )


def retain_workday_consultation_attempt_for_session_handoff(
    context: object,
    *,
    task_id: uuid.UUID | None,
    resident_session_id: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Freeze the displaced private attempt before session ownership clears."""

    normalized = dict(context) if isinstance(context, dict) else {}
    if normalized.get(_CONSULTATION_PACKET_CONTEXT_KEY) is not True:
        return normalized
    claim_id_raw = normalized.get(_WORKDAY_CLAIM_ID_CONTEXT_KEY)
    if claim_id_raw is None:
        return normalized
    try:
        claim_id = uuid.UUID(str(claim_id_raw))
        attempt = int(normalized.get(_WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY))
        generation = int(normalized.get(_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY))
        incarnation = uuid.UUID(
            str(normalized.get(_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY))
        )
    except (TypeError, ValueError, AttributeError):
        raise ValueError("consultation handoff claim identity is invalid") from None
    started_at = _workday_claim_expiry(
        normalized.get(_WORKDAY_CLAIM_STARTED_CONTEXT_KEY)
    ) or now
    if (
        task_id is None
        or not resident_session_id
        or not 1 <= attempt <= _WORKDAY_ATTEMPT_LIMIT
        or generation < 1
    ):
        raise ValueError("consultation handoff claim owner is invalid")
    authority = _register_workday_claim_authority(
        normalized,
        workday_claim_id=claim_id,
        attempt=attempt,
        task_id=task_id,
        resident_session_id=resident_session_id,
        worker_generation=generation,
        worker_incarnation=incarnation,
        issued_at=started_at,
    )
    attempts = bounded_workday_consultation_attempts(normalized)
    prior = next(
        (
            item
            for item in attempts
            if item["workday_claim_id"] == str(claim_id)
        ),
        None,
    )
    if prior is None:
        elapsed_ms = min(
            86_400_000,
            max(0, int((now - started_at).total_seconds() * 1_000)),
        )
        _append_workday_attempt(
            normalized,
            _normalize_workday_attempt_diagnostic(
                {
                    "attempt": attempt,
                    "workday_claim_id": str(claim_id),
                    "outcome": "abandoned",
                    "terminal_predicate": "resident_session_handoff_without_ack",
                    "provider_terminal_reason": None,
                    "provider_output_tail": None,
                    "started_at": started_at.isoformat(),
                    "ended_at": now.isoformat(),
                    "turn_ms": elapsed_ms,
                    "ttft_ms": None,
                    "tool_call_count": 0,
                    "tool_call_duration_ms": 0,
                    "result_chars": 0,
                    "trace": [
                        {
                            "elapsed_ms": elapsed_ms,
                            "kind": "error",
                            "label": "provider_diagnostic_missing",
                            "is_error": True,
                        }
                    ],
                    "trace_truncated": True,
                }
            ),
        )
        _set_workday_claim_diagnostic_state(normalized, claim_id, "server_missing")
    elif authority["diagnostic_state"] in {"recorded", "acknowledged"}:
        pass
    elif (
        authority["diagnostic_state"] == "server_missing"
        and _is_server_missing_workday_attempt(prior)
    ):
        pass
    else:
        raise ValueError("consultation handoff attempt history conflicts")
    # The authority row and attempt diagnostic above are the durable record of
    # the displaced claim.  Its mutable lease fields must not survive after
    # session_message_handoff clears the row owner: a replacement Resident
    # would otherwise be forced to reconstruct the predecessor from owner
    # columns that are intentionally NULL at the handoff boundary.  Historical
    # receipts remain bound to the immutable authority row; only the next live
    # claimant may mint new lease authority.
    for key in (
        _WORKDAY_CLAIM_ID_CONTEXT_KEY,
        _WORKDAY_CLAIM_GENERATION_CONTEXT_KEY,
        _WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY,
        _WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY,
        _WORKDAY_CLAIM_STARTED_CONTEXT_KEY,
    ):
        normalized.pop(key, None)
    return normalized


def _legacy_workday_attempt_diagnostic(
    context: dict[str, Any],
    *,
    workday_claim_id: uuid.UUID,
    outcome: str,
    result_chars: int,
    now: datetime,
) -> dict[str, Any]:
    """Project an explicit diagnostic for an ack from a pre-upgrade runner.

    The first API release that required ``attempt_diagnostic`` can overlap an
    Agent already inside a provider turn.  Such an Agent cannot manufacture the
    new field, and rejecting its ack prevents it from draining into the runner
    auto-update.  Preserve the facts the server actually knows (claim identity,
    outcome, claim-to-ack timing), mark the unavailable trace as truncated, and
    deliberately leave provider-only fields null rather than inventing them.

    A lost-response retry after the first ack must reuse the byte-identical
    stored projection.  Look up that exact claim before synthesizing a new row.
    """

    for prior in reversed(bounded_workday_consultation_attempts(context)):
        if (
            prior.get("workday_claim_id") == str(workday_claim_id)
            and prior.get("outcome") == outcome
        ):
            return prior

    attempt = int(context.get(_WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY) or 0)
    started_at = _workday_claim_expiry(
        context.get(_WORKDAY_CLAIM_STARTED_CONTEXT_KEY)
    ) or now
    elapsed_ms = min(
        86_400_000,
        max(0, int((now - started_at).total_seconds() * 1_000)),
    )
    return _normalize_workday_attempt_diagnostic(
        {
            "attempt": attempt,
            "workday_claim_id": str(workday_claim_id),
            "outcome": outcome,
            "terminal_predicate": "legacy_runner_ack_without_diagnostic",
            "provider_terminal_reason": None,
            "provider_output_tail": None,
            "started_at": started_at.isoformat(),
            "ended_at": now.isoformat(),
            "turn_ms": elapsed_ms,
            "ttft_ms": None,
            "tool_call_count": 0,
            "tool_call_duration_ms": 0,
            "result_chars": result_chars,
            "trace": [
                {
                    "elapsed_ms": elapsed_ms,
                    "kind": "error",
                    "label": "legacy_diagnostic_unavailable",
                    "is_error": True,
                }
            ],
            "trace_truncated": True,
        }
    )


def bounded_workday_consultation_attempts(context: object) -> list[dict[str, Any]]:
    if not isinstance(context, dict):
        return []
    raw = context.get(_WORKDAY_ATTEMPTS_CONTEXT_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("consultation attempt history is invalid")
    return [
        _normalize_workday_attempt_diagnostic(item)
        for item in raw[-_WORKDAY_ATTEMPT_LIMIT:]
    ]


async def record_workday_consultation_attempt_diagnostic(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    packet_id: uuid.UUID,
    task_id: uuid.UUID,
    resident_session_id: str,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    workday_claim_id: uuid.UUID,
    attempt_diagnostic: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Persist provider evidence without granting completion authority.

    A superseded worker may use this path only when the packet's server-issued
    claim ledger proves the exact historical owner tuple.  The write never
    changes the packet lease, owner, status, result content, receipts, or ack;
    it can only add an absent diagnostic or replace one exact server-authored
    missing-evidence placeholder.
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
    if row is None:
        raise ValueError("consultation packet does not exist")
    context = dict(row.context or {})
    if not (
        row.from_type == "system"
        and row.surface == MessageSurface.CONVERSATION
        and context.get(_CONSULTATION_PACKET_CONTEXT_KEY) is True
        and context.get("audience") == STRATEGIST_INTERNAL
        and context.get("message_role") == "workday_consultation"
        and context.get("public_reply_owner")
        == ConversationPublicReplyOwner.FAST.value
    ):
        raise ValueError("consultation packet provenance is invalid")
    diagnostic = _normalize_workday_attempt_diagnostic(attempt_diagnostic)
    if (
        diagnostic["workday_claim_id"] != str(workday_claim_id)
        or diagnostic["outcome"] == "completed"
        or diagnostic["terminal_predicate"] in _WORKDAY_SERVER_MISSING_PREDICATES
        or diagnostic["terminal_predicate"] == "server_claim_attempt_cap"
    ):
        raise ValueError("consultation attempt diagnostic is not reportable")
    authorities = _workday_claim_authorities(context)
    authority = next(
        (
            item
            for item in authorities
            if item["workday_claim_id"] == str(workday_claim_id)
        ),
        None,
    )
    if authority is None or (
        authority["attempt"] != diagnostic["attempt"]
        or authority["task_id"] != str(task_id)
        or authority["resident_session_id"] != resident_session_id
        or authority["worker_generation"] != worker_generation
        or authority["worker_incarnation"] != str(worker_incarnation)
    ):
        raise ValueError("consultation attempt diagnostic authority is absent")
    attempts = bounded_workday_consultation_attempts(context)
    prior_index = next(
        (
            index
            for index, item in enumerate(attempts)
            if item["workday_claim_id"] == str(workday_claim_id)
        ),
        None,
    )
    if prior_index is None:
        if authority["diagnostic_state"] != "open":
            raise ValueError("consultation attempt diagnostic state conflicts")
        attempts.append(diagnostic)
        disposition = "recorded"
    else:
        prior = attempts[prior_index]
        if prior == diagnostic and authority["diagnostic_state"] in {
            "recorded",
            "acknowledged",
        }:
            disposition = "duplicate"
        elif (
            authority["diagnostic_state"] == "server_missing"
            and _is_server_missing_workday_attempt(prior)
        ):
            attempts[prior_index] = diagnostic
            disposition = "upgraded"
        else:
            raise ValueError("consultation attempt diagnostic conflicts")
    context[_WORKDAY_ATTEMPTS_CONTEXT_KEY] = attempts[-_WORKDAY_ATTEMPT_LIMIT:]

    result_rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.reply_to_message_id == packet_id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    canonical_results = [
        result
        for result in result_rows
        if isinstance(result.context, dict)
        and result.context.get(_CONSULTATION_RESULT_CONTEXT_KEY) is True
        and result.context.get("source_packet_id") == str(packet_id)
    ]
    if len(canonical_results) > 1 or (row.status != "open" and not canonical_results):
        raise ValueError("consultation result diagnostic projection is invalid")
    if canonical_results:
        result = canonical_results[0]
        result_context = dict(result.context or {})
        result_attempts = bounded_workday_consultation_attempts(result_context)
        result_index = next(
            (
                index
                for index, item in enumerate(result_attempts)
                if item["workday_claim_id"] == str(workday_claim_id)
            ),
            None,
        )
        if result_index is None:
            if disposition == "duplicate":
                raise ValueError("consultation result diagnostic is missing")
            result_attempts.append(diagnostic)
        elif result_attempts[result_index] != diagnostic:
            if not _is_server_missing_workday_attempt(result_attempts[result_index]):
                raise ValueError("consultation result diagnostic conflicts")
            result_attempts[result_index] = diagnostic
        result_context[_WORKDAY_ATTEMPTS_CONTEXT_KEY] = result_attempts[
            -_WORKDAY_ATTEMPT_LIMIT:
        ]
        result.context = result_context
        session.add(result)

    state = "acknowledged" if canonical_results else "recorded"
    _set_workday_claim_diagnostic_state(context, workday_claim_id, state)
    row.context = context
    session.add(row)
    await session.flush()
    return disposition, state, diagnostic


async def read_workday_consultation_attempt_diagnostic(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    packet_id: uuid.UUID,
    task_id: uuid.UUID,
    resident_session_id: str,
    worker_generation: int,
    worker_incarnation: uuid.UUID,
    workday_claim_id: uuid.UUID,
) -> tuple[str, dict[str, Any] | None]:
    """Read the exact diagnostic receipt without changing packet state."""

    row = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == packet_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("consultation packet does not exist")
    context = dict(row.context or {})
    authority = next(
        (
            item
            for item in _workday_claim_authorities(context)
            if item["workday_claim_id"] == str(workday_claim_id)
        ),
        None,
    )
    if authority is None or (
        authority["task_id"] != str(task_id)
        or authority["resident_session_id"] != resident_session_id
        or authority["worker_generation"] != worker_generation
        or authority["worker_incarnation"] != str(worker_incarnation)
    ):
        raise ValueError("consultation attempt diagnostic authority is absent")
    diagnostic = next(
        (
            item
            for item in bounded_workday_consultation_attempts(context)
            if item["workday_claim_id"] == str(workday_claim_id)
        ),
        None,
    )
    return authority["diagnostic_state"], diagnostic


async def _finalize_workday_consultation_packet(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    packet_id: uuid.UUID,
    row: ProjectMessage,
    context: dict[str, Any],
    resident_session_id: str,
    workday_claim_id: uuid.UUID,
    outcome: str,
    result_summary: str,
    now: datetime,
) -> None:
    """Persist a terminal ('completed' or 'failed') consultation result and
    close the packet. Shared by the client-driven ack path
    (acknowledge_workday_consultation_packet) and the server-side
    claim-attempt-cap park path (claim_next_workday_consultation_packet) so
    both produce an identical result/continuation shape for Fast to consume.
    Caller owns the transaction (flush/commit) and any ownership/lease checks.
    """
    from app.strategic_workday_mutation_rebind import (
        WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY,
        WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY,
        authoritative_workday_operation_receipts,
        workday_mutation_receipts_have_more,
    )

    # A retry is a continuation of the same packet, so its result must expose
    # every durable receipt, not only writes made under the final claim id.
    mutation_receipts = authoritative_workday_operation_receipts(
        context,
        packet_id=packet_id,
    )
    mutation_receipts_have_more = workday_mutation_receipts_have_more(context)
    result_key = f"workday-consultation-result:{packet_id}:{workday_claim_id}"
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
            "result_state": outcome,
            "message_role": "workday_consultation_result",
            "public_reply_owner": ConversationPublicReplyOwner.FAST.value,
            "envelope": context.get("envelope"),
            _WORKDAY_ATTEMPTS_CONTEXT_KEY: bounded_workday_consultation_attempts(
                context
            ),
            **(
                {
                    WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY: mutation_receipts,
                    WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY: (
                        mutation_receipts_have_more
                    ),
                }
                if mutation_receipts or mutation_receipts_have_more
                else {}
            ),
            **(
                {
                    _CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY: context.get(
                        _CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY
                    )
                }
                if isinstance(
                    context.get(_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY), str
                )
                and context.get(_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY)
                else {}
            ),
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
            or existing_result.content != result_summary
            or {key: existing_context.get(key) for key in result_context}
            != result_context
        ):
            raise ValueError("consultation result idempotency key conflicts")
        if existing_result.reply_to_message_id is None:
            existing_result.reply_to_message_id = packet_id
            session.add(existing_result)
            await session.flush()
        elif existing_result.reply_to_message_id != packet_id:
            raise ValueError("consultation result relational lineage conflicts")
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
            content=result_summary,
            context=result_context,
            idempotency_key=result_key,
            resident_session_id=resident_session_id,
            reply_to_message_id=packet_id,
        )
        session.add(result)
    context[_WORKDAY_ACK_CLAIM_ID_CONTEXT_KEY] = str(workday_claim_id)
    context["_workday_consultation_ack_outcome"] = outcome
    context[_WORKDAY_RESULT_SUMMARY_CONTEXT_KEY] = result_summary
    row.context = context
    row.status = "handled"
    row.handling_state = MessageHandlingState.RESPONDED
    row.updated_at = now
    session.add(row)


async def record_workday_consultation_gate_failure(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    packet_id: uuid.UUID,
    task_id: uuid.UUID | None,
    resident_session_id: str | None,
    gate: str,
    detail: str,
) -> None:
    """Best-effort: log and persist WHY a consultation renewal/ack gate
    rejected a request (409), so the incident carries its own diagnosis
    instead of an opaque bare 409 with nothing server-side to inspect.

    Caller is responsible for committing (or rolling back on failure) — this
    function only flushes within the caller's transaction. Never raises: a
    diagnostic-recording failure must not mask the original error.
    """
    bounded_detail = (detail or "")[:2000]
    logger.warning(
        "workday consultation gate failure: project=%s packet=%s task=%s "
        "session=%s gate=%s detail=%s",
        project_id, packet_id, task_id, resident_session_id, gate, bounded_detail,
    )
    try:
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
            return
        now = _utcnow()
        context = dict(row.context or {})
        context[_WORKDAY_LAST_FAILURE_CONTEXT_KEY] = {
            "gate": gate,
            "detail": bounded_detail,
            "task_id": str(task_id) if task_id else None,
            "resident_session_id": resident_session_id,
            "recorded_at": now.isoformat(),
        }
        row.context = context
        row.updated_at = now
        session.add(row)
        await session.flush()
    except Exception:
        logger.exception(
            "failed to persist workday consultation gate failure detail: "
            "project=%s packet=%s gate=%s",
            project_id, packet_id, gate,
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
                    ProjectMessage.context[_CONSULTATION_PACKET_CONTEXT_KEY]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(*_message_ordering())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if not _is_workday_consultation_packet(row, resident_session_id=None):
            continue
        context = dict(row.context or {})
        from app.strategic_workday_mutation_rebind import (
            authoritative_workday_operation_receipts,
        )

        try:
            bounded_workday_consultation_attempts(context)
            prior_receipts = authoritative_workday_operation_receipts(
                context,
                packet_id=row.id,
            )
            prior_attempt = context.get(_WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY, 0)
            if (
                not isinstance(prior_attempt, int)
                or isinstance(prior_attempt, bool)
                or prior_attempt < 0
                or prior_attempt >= _WORKDAY_CONSULTATION_MAX_UNACKED_CLAIMS
            ):
                raise ValueError("consultation claim attempt is invalid")
        except (HTTPException, ValueError) as exc:
            raise WorkdayConsultationPacketProvenanceInvalid(row.id) from exc
        prior_claim_id_raw = context.get(_WORKDAY_CLAIM_ID_CONTEXT_KEY)
        prior_expires_at = _workday_claim_expiry(
            context.get(_WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY)
        )
        prior_generation = context.get(_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY)
        prior_incarnation = context.get(_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY)
        same_owner = (
            row.handling_state == MessageHandlingState.CLAIMED
            and row.resident_session_id == resident_session_id
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
        if not same_owner and prior_claim_id_raw is not None and prior_attempt >= 1:
            # A dead worker cannot send an ack. Preserve that attempt explicitly
            # at the reclaim boundary instead of letting it disappear behind the
            # next claim's generic failure summary.
            prior_started_at = _workday_claim_expiry(
                context.get(_WORKDAY_CLAIM_STARTED_CONTEXT_KEY)
            ) or now
            try:
                prior_claim_id = uuid.UUID(str(prior_claim_id_raw))
                if (
                    row.handled_by_task_id is None
                    or not isinstance(row.resident_session_id, str)
                    or not row.resident_session_id
                    or not isinstance(prior_generation, int)
                    or isinstance(prior_generation, bool)
                    or prior_generation < 1
                ):
                    raise ValueError("expired consultation claim owner is invalid")
                prior_authority = _register_workday_claim_authority(
                    context,
                    workday_claim_id=prior_claim_id,
                    attempt=prior_attempt,
                    task_id=row.handled_by_task_id,
                    resident_session_id=row.resident_session_id,
                    worker_generation=prior_generation,
                    worker_incarnation=uuid.UUID(str(prior_incarnation)),
                    issued_at=prior_started_at,
                )
            except (TypeError, ValueError, AttributeError) as exc:
                raise WorkdayConsultationPacketProvenanceInvalid(row.id) from exc
            elapsed_ms = max(0, int((now - prior_started_at).total_seconds() * 1000))
            prior_diagnostic = next(
                (
                    item
                    for item in bounded_workday_consultation_attempts(context)
                    if item.get("workday_claim_id") == str(prior_claim_id)
                ),
                None,
            )
            if prior_diagnostic is None:
                expired_diagnostic = _normalize_workday_attempt_diagnostic(
                    {
                        "attempt": prior_attempt,
                        "workday_claim_id": str(prior_claim_id),
                        "outcome": "abandoned",
                        "terminal_predicate": "claim_lease_expired_without_ack",
                        "provider_terminal_reason": None,
                        "provider_output_tail": None,
                        "started_at": prior_started_at.isoformat(),
                        "ended_at": now.isoformat(),
                        "turn_ms": min(elapsed_ms, 86_400_000),
                        "ttft_ms": None,
                        "tool_call_count": 0,
                        "tool_call_duration_ms": 0,
                        "result_chars": 0,
                        "trace": [
                            {
                                "elapsed_ms": min(elapsed_ms, 86_400_000),
                                "kind": "error",
                                "label": "provider_diagnostic_missing",
                                "is_error": True,
                            }
                        ],
                        "trace_truncated": True,
                    }
                )
                _append_workday_attempt(context, expired_diagnostic)
                _set_workday_claim_diagnostic_state(
                    context, prior_claim_id, "server_missing"
                )
            elif prior_authority["diagnostic_state"] in {"recorded", "acknowledged"}:
                pass
            elif (
                prior_authority["diagnostic_state"] == "server_missing"
                and _is_server_missing_workday_attempt(prior_diagnostic)
            ):
                pass
            else:
                raise WorkdayConsultationPacketProvenanceInvalid(row.id)
        attempt = prior_attempt
        if not same_owner:
            attempt += 1
        attempt = max(1, attempt)
        if not same_owner and attempt >= _WORKDAY_CONSULTATION_MAX_UNACKED_CLAIMS:
            # This packet has been reclaimed (previous owner's lease expired,
            # or its worker exited) _WORKDAY_CONSULTATION_MAX_UNACKED_CLAIMS
            # times in a row without a terminal ack ever arriving. Requeuing
            # it again would repeat the same failure indefinitely (the churn
            # this cap exists to break); park it as a terminal 'failed' result
            # instead so Fast sees an explicit, explained outcome.
            parked_diagnostic = _normalize_workday_attempt_diagnostic(
                {
                    "attempt": attempt,
                    "workday_claim_id": str(workday_claim_id),
                    "outcome": "failed",
                    "terminal_predicate": "server_claim_attempt_cap",
                    "provider_terminal_reason": None,
                    "provider_output_tail": None,
                    "started_at": now.isoformat(),
                    "ended_at": now.isoformat(),
                    "turn_ms": 0,
                    "ttft_ms": None,
                    "tool_call_count": 0,
                    "tool_call_duration_ms": 0,
                    "result_chars": 0,
                    "trace": [],
                    "trace_truncated": False,
                }
            )
            _append_workday_attempt(context, parked_diagnostic)
            await _finalize_workday_consultation_packet(
                session,
                project_id=project_id,
                packet_id=row.id,
                row=row,
                context=context,
                resident_session_id=resident_session_id,
                workday_claim_id=workday_claim_id,
                outcome="failed",
                result_summary=_workday_consultation_parked_reason(attempt, context),
                now=now,
            )
            await session.flush()
            continue
        expires_at = now + timedelta(seconds=lease_seconds)
        context.update(
            {
                _WORKDAY_CLAIM_ID_CONTEXT_KEY: str(workday_claim_id),
                _WORKDAY_CLAIM_GENERATION_CONTEXT_KEY: worker_generation,
                _WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY: str(worker_incarnation),
                _WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY: expires_at.isoformat(),
                _WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY: attempt,
                _WORKDAY_CLAIM_STARTED_CONTEXT_KEY: (
                    context.get(_WORKDAY_CLAIM_STARTED_CONTEXT_KEY)
                    if same_owner
                    else now.isoformat()
                ),
            }
        )
        try:
            _register_workday_claim_authority(
                context,
                workday_claim_id=workday_claim_id,
                attempt=attempt,
                task_id=task_id,
                resident_session_id=resident_session_id,
                worker_generation=worker_generation,
                worker_incarnation=worker_incarnation,
                issued_at=_workday_claim_expiry(
                    context[_WORKDAY_CLAIM_STARTED_CONTEXT_KEY]
                )
                or now,
            )
        except ValueError as exc:
            raise WorkdayConsultationPacketProvenanceInvalid(row.id) from exc
        if row.resident_session_id != resident_session_id:
            from app.session_message_handoff import (
                RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY,
            )

            prior_session_id = str(row.resident_session_id or "").strip()
            if prior_session_id:
                context[RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY] = prior_session_id
            row.resident_session_id = resident_session_id
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
            prior_attempts=tuple(bounded_workday_consultation_attempts(context)),
            prior_receipts=tuple(prior_receipts),
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
    attempt_diagnostic: dict[str, Any] | None = None,
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
    now = _utcnow()
    diagnostic = (
        _normalize_workday_attempt_diagnostic(attempt_diagnostic)
        if attempt_diagnostic is not None
        else _legacy_workday_attempt_diagnostic(
            context,
            workday_claim_id=workday_claim_id,
            outcome=outcome,
            result_chars=len(normalized_result_summary),
            now=now,
        )
    )
    if (
        diagnostic["workday_claim_id"] != str(workday_claim_id)
        or diagnostic["outcome"] != outcome
    ):
        raise ValueError("consultation attempt diagnostics do not match the claim")
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
            and context.get("_workday_consultation_ack_outcome") in {
                "completed",
                "failed",
            }
            and outcome == context.get("_workday_consultation_ack_outcome")
        ):
            if (
                outcome == "completed"
                and normalized_result_summary
                and context.get(_WORKDAY_RESULT_SUMMARY_CONTEXT_KEY)
                != normalized_result_summary
            ):
                raise ValueError("completed consultation result_summary conflicts")
            attempts = context.get(_WORKDAY_ATTEMPTS_CONTEXT_KEY)
            if not isinstance(attempts, list) or diagnostic not in attempts:
                raise ValueError("completed consultation attempt diagnostic conflicts")
            stored_result_summary = context.get(
                _WORKDAY_RESULT_SUMMARY_CONTEXT_KEY
            )
            if not isinstance(stored_result_summary, str) or not stored_result_summary:
                raise ValueError("completed consultation result receipt is invalid")
            # A pre-relational deployment may have committed the exact result
            # row before its packet FK existed.  An exact lost-response retry
            # is the only safe repair boundary: the server-derived result key,
            # immutable content/context projection, and terminal packet
            # receipt must all match before _finalize may fill a NULL link.
            await _finalize_workday_consultation_packet(
                session,
                project_id=project_id,
                packet_id=packet_id,
                row=row,
                context=context,
                resident_session_id=resident_session_id,
                workday_claim_id=workday_claim_id,
                outcome=outcome,
                result_summary=stored_result_summary,
                now=now,
            )
            await session.flush()
            return True
        raise ValueError("consultation packet is unavailable")
    if outcome not in {"completed", "abandoned", "failed"}:
        raise ValueError("consultation outcome is invalid")
    if outcome == "completed" and not normalized_result_summary:
        raise ValueError("completed consultation requires a result_summary")
    if outcome == "failed":
        if int(context.get(_WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY) or 0) < 2:
            raise ValueError("failed consultation requires at least two attempts")
        normalized_result_summary = _WORKDAY_FAILED_RESULT_SUMMARY
    if (
        row.handling_state != MessageHandlingState.CLAIMED
        or row.handled_by_task_id != task_id
        or context.get(_WORKDAY_CLAIM_ID_CONTEXT_KEY) != str(workday_claim_id)
        or context.get(_WORKDAY_CLAIM_GENERATION_CONTEXT_KEY) != worker_generation
        or str(context.get(_WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY))
        != str(worker_incarnation)
    ):
        raise ValueError("consultation packet is owned by another worker")
    expires_at = _workday_claim_expiry(
        context.get(_WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY)
    )
    if expires_at is None or expires_at <= now:
        raise ValueError("consultation packet lease is expired")
    if (
        diagnostic["attempt"]
        != int(context.get(_WORKDAY_CLAIM_ATTEMPT_CONTEXT_KEY) or 0)
    ):
        raise ValueError("consultation attempt diagnostics do not match the claim")
    _append_workday_attempt(context, diagnostic)
    if any(
        item["workday_claim_id"] == str(workday_claim_id)
        for item in _workday_claim_authorities(context)
    ):
        _set_workday_claim_diagnostic_state(
            context, workday_claim_id, "acknowledged"
        )
    if outcome in {"completed", "failed"}:
        await _finalize_workday_consultation_packet(
            session,
            project_id=project_id,
            packet_id=packet_id,
            row=row,
            context=context,
            resident_session_id=resident_session_id,
            workday_claim_id=workday_claim_id,
            outcome=outcome,
            result_summary=normalized_result_summary,
            now=now,
        )
        await session.flush()
        return False
    for key in (
        _WORKDAY_CLAIM_ID_CONTEXT_KEY,
        _WORKDAY_CLAIM_GENERATION_CONTEXT_KEY,
        _WORKDAY_CLAIM_INCARNATION_CONTEXT_KEY,
        _WORKDAY_CLAIM_EXPIRES_CONTEXT_KEY,
        _WORKDAY_CLAIM_STARTED_CONTEXT_KEY,
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
                    ProjectMessage.context[_CONSULTATION_RESULT_CONTEXT_KEY]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(*_message_ordering())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if not _is_workday_consultation_result(row, resident_session_id=None):
            continue
        context = dict(row.context or {})
        prior_claim_id = context.get(_CONTINUATION_CLAIM_ID_CONTEXT_KEY)
        prior_expires_at = _workday_claim_expiry(
            context.get(_CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY)
        )
        same_owner = (
            row.handling_state == MessageHandlingState.CLAIMED
            and row.resident_session_id == resident_session_id
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
        if row.resident_session_id != resident_session_id:
            from app.session_message_handoff import (
                RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY,
            )

            prior_session_id = str(row.resident_session_id or "").strip()
            if prior_session_id:
                context[RESIDENT_HANDOFF_FROM_SESSION_CONTEXT_KEY] = prior_session_id
            row.resident_session_id = resident_session_id
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
    # Preserve the durable attempt counter across release/reclaim. The Agent
    # uses the next attempt to terminalize a repeatedly unrenderable result
    # instead of reclaiming the same private packet forever.
    for key in (
        _CONTINUATION_CLAIM_ID_CONTEXT_KEY,
        _CONTINUATION_CLAIM_GENERATION_CONTEXT_KEY,
        _CONTINUATION_CLAIM_INCARNATION_CONTEXT_KEY,
        _CONTINUATION_CLAIM_EXPIRES_CONTEXT_KEY,
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
            or context.get("_workday_result_packet_id") != str(result_packet_id)
            or existing.reply_to_message_id not in {None, result_packet_id}
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
                **(
                    {
                        _CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY: context.get(
                            _CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY
                        )
                    }
                    if isinstance(
                        context.get(_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY), str
                    )
                    and context.get(_CONSULTATION_ATOMIC_REPLY_ID_CONTEXT_KEY)
                    else {}
                ),
            },
            "question_answer",
            message_type=MessageType.NOTE.value,
        ),
        idempotency_key=normalized_key,
        resident_session_id=resident_session_id,
        reply_to_message_id=result_packet_id,
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
        if packet.reply_to_message_id is None:
            packet.reply_to_message_id = existing.id
            session.add(packet)
            await session.flush()
        elif packet.reply_to_message_id != existing.id:
            raise ValueError("idempotency_key Workday relational lineage conflicts")
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
                _CONSULTATION_REQUESTED_CONTEXT_KEY: normalized_workday_request is not None,
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
