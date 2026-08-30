"""Bounded operator-readable detail for a private Workday consultation.

The consultation packet and its result are ``STRATEGIST_INTERNAL`` on purpose:
pushing either row into the operator transcript is exactly the coordination
noise that audience exists to suppress.  But leaving them *unreadable* is what
makes a Resident-invoked sub-session feel like a middle-man -- the operator can
see that a consultation was requested and, much later, that something came
back, with nothing legible in between.

This module is the read path that closes that gap without moving a single row
into the operator audience.  Nothing here changes what the conversation poll
delivers; it answers an explicit "what is that sub-session doing?" request with
a bounded projection of durable queue state:

* the question the Resident handed down,
* where the packet is in its claim/lease lifecycle and why,
* the durable operation ledger the worker appended while it ran, and
* the result summary plus its bounded mutation receipts.

The consultation id is the *public* reply message id
(``_workday_consultation_atomic_reply_id``).  Requiring that public row to
exist, belong to the project, and still be operator-visible is the access
boundary: this path can only ever expand something the operator's own thread
already refers to.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.conversation_coordinator import (
    _workday_claim_expiry,
    bounded_workday_consultation_attempts,
)
from app.message_audience import PIPELINE_LOG, STRATEGIST_INTERNAL
from app.models import (
    MessageHandlingState,
    MessageSurface,
    ProjectMessage,
)
from app.strategic_workday_mutation_rebind import (
    WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY,
    WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY,
    WORKDAY_OPERATION_LEDGER_CONTEXT_KEY,
    WORKDAY_OPERATION_LEDGER_LIMIT,
)
from app.time_utils import _utcnow, utc_iso


# Bounds. These are deliberately literal rather than derived from the writer
# side: a projection that silently grows with whatever the worker wrote is a
# payload-size incident waiting to happen on an operator read path.
CONSULTATION_QUESTION_MAX_CHARS = 4000
CONSULTATION_SUMMARY_MAX_CHARS = 8000
CONSULTATION_PREVIEW_MAX_CHARS = 240
CONSULTATION_STEP_LIMIT = WORKDAY_OPERATION_LEDGER_LIMIT
CONSULTATION_RECEIPT_LIMIT = 16
CONSULTATION_INDEX_LIMIT = 20
CONSULTATION_INDEX_MAX_LIMIT = 50
# How many internal rows one consultation may contribute before the projection
# stops reading. A consultation has one packet, one result and one
# continuation; anything past that is corruption, not a bigger conversation.
CONSULTATION_ROW_SCAN_LIMIT = 32

_ATOMIC_REPLY_ID = "_workday_consultation_atomic_reply_id"
_PACKET_MARKER = "_conversation_consultation_packet"
_RESULT_MARKER = "_workday_consultation_result"
_CONTINUATION_RESULT_PACKET_ID = "_workday_result_packet_id"
_CONSULTATION_REQUESTED = "_workday_consultation_requested"
_CONTINUATION_KEY = "_conversation_consultation_continuation"
_CLAIM_ID = "_workday_consultation_claim_id"
_CLAIM_GENERATION = "_workday_consultation_claim_generation"
_CLAIM_INCARNATION = "_workday_consultation_claim_incarnation"
_CLAIM_EXPIRES = "_workday_consultation_claim_expires_at"
_CLAIM_ATTEMPT = "_workday_consultation_claim_attempt"
_LAST_FAILURE = "_workday_consultation_last_failure"
_RESULT_STATE = "result_state"
_PRIVATE_HANDOFF_RETIREMENT = "_private_handoff_retirement"
_HIDDEN_PRIVACY_STATES = {"redacted", "deleted"}

# The operator-facing view of one durable operation. The identity fields a
# receipt carries for rebind safety (claim ids, incarnations, generation
# counters, fingerprints) are not projected: they prove nothing to a human
# reading a progress line and only widen the surface.
_STEP_FIELDS = (
    "op",
    "entity_kind",
    "entity_id",
    "effect",
    "target_kind",
    "target_revision",
    "accepted_at",
    "recorded_at",
)


def _context(row: ProjectMessage | None) -> dict[str, Any]:
    context = row.context if row is not None else None
    return dict(context) if isinstance(context, Mapping) else {}


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return utc_iso(value)
    return value.isoformat() if hasattr(value, "isoformat") else None  # utc-iso-ok: non-datetime (date) value -- no zone applies


def _bounded_text(value: Any, limit: int) -> dict[str, Any]:
    """Project text with an explicit truncation flag and its true length.

    Silently clipping is how a reader ends up trusting half a sentence. The
    caller always learns the real size and whether it saw all of it.
    """
    text = value if isinstance(value, str) else ""
    return {
        "text": text[:limit],
        "truncated": len(text) > limit,
        "chars": len(text),
    }


def _bounded_step(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return None
    op = value.get("op")
    if not isinstance(op, str) or not op or len(op) > 64:
        return None
    step: dict[str, Any] = {"op": op}
    for key in _STEP_FIELDS[1:]:
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            step[key] = raw[:255]
        elif isinstance(raw, int) and not isinstance(raw, bool):
            step[key] = raw
    return step


def _bounded_steps(
    context: Mapping[str, Any], key: str, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    raw = context.get(key)
    if not isinstance(raw, list):
        return [], False
    steps = [step for step in (_bounded_step(item) for item in raw) if step is not None]
    return steps[:limit], len(steps) > limit


def _public_request_is_visible(row: ProjectMessage | None) -> bool:
    """The access boundary: only expand what the operator thread refers to."""
    if row is None:
        return False
    context = _context(row)
    return bool(
        row.surface == MessageSurface.CONVERSATION
        and row.from_type in {"strategic", "strategist"}
        and context.get("message_role") == "operator_reply"
        and context.get(_CONSULTATION_REQUESTED) is True
        and context.get("audience") not in {STRATEGIST_INTERNAL, PIPELINE_LOG}
        and context.get("privacy_state") not in _HIDDEN_PRIVACY_STATES
    )


def _classify_related(row: ProjectMessage) -> str | None:
    context = _context(row)
    # Quarantine preserves the incident row for audit, but it is no longer a
    # readable consultation packet/result. Selecting it here would expose the
    # exact corrupt content/receipts that delivery rejected and, because rows
    # are ordered oldest-first, hide the later neutral replacement.
    if isinstance(context.get(_PRIVATE_HANDOFF_RETIREMENT), Mapping) or (
        row.handling_state == MessageHandlingState.SUPERSEDED
        and str(row.resolution or "").startswith("private_handoff_")
    ):
        return None
    role = context.get("message_role")
    if (
        role == "workday_consultation"
        and context.get(_PACKET_MARKER) is True
        and context.get("audience") == STRATEGIST_INTERNAL
    ):
        return "packet"
    if (
        role == "workday_consultation_result"
        and context.get(_RESULT_MARKER) is True
        and context.get("audience") == STRATEGIST_INTERNAL
    ):
        return "result"
    if role == "workday_consultation_continuation" and isinstance(
        context.get(_CONTINUATION_RESULT_PACKET_ID), str
    ):
        return "continuation"
    return None


def _claim_view(
    row: ProjectMessage, context: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Project the lease, including an explicit expired reading.

    A stalled consultation and a running one look identical to a caller that
    only sees ``CLAIMED``; the whole difference is the lease clock, so the
    comparison happens here instead of in every reader.
    """
    if row.handling_state != MessageHandlingState.CLAIMED:
        return None
    expires_at = _workday_claim_expiry(context.get(_CLAIM_EXPIRES))
    attempt = context.get(_CLAIM_ATTEMPT)
    generation = context.get(_CLAIM_GENERATION)
    return {
        "claimed_by_task_id": (
            str(row.handled_by_task_id) if row.handled_by_task_id else None
        ),
        "claim_id": str(context.get(_CLAIM_ID)) if context.get(_CLAIM_ID) else None,
        "incarnation": (
            str(context.get(_CLAIM_INCARNATION))
            if context.get(_CLAIM_INCARNATION)
            else None
        ),
        "generation": (
            generation
            if isinstance(generation, int) and not isinstance(generation, bool)
            else None
        ),
        "attempt": (
            attempt
            if isinstance(attempt, int) and not isinstance(attempt, bool)
            else None
        ),
        "expires_at": _iso(expires_at),
        "expired": expires_at is not None and expires_at <= _utcnow(),
    }


def _last_failure(
    context: Mapping[str, Any], attempts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Project the newest gate/attempt failure without erasing either ledger."""
    raw = context.get(_LAST_FAILURE)
    gate_failure: dict[str, Any] | None = None
    gate_at: datetime | None = None
    if isinstance(raw, Mapping):
        gate = raw.get("gate")
        detail = raw.get("detail")
        recorded_at = raw.get("recorded_at")
        gate_failure = {
            "gate": gate[:64] if isinstance(gate, str) else None,
            "detail": detail[:2000] if isinstance(detail, str) else None,
            "recorded_at": recorded_at[:64] if isinstance(recorded_at, str) else None,
        }
        gate_at = _workday_claim_expiry(recorded_at)
    latest = next(
        (
            attempt
            for attempt in reversed(attempts)
            if attempt.get("outcome") != "completed"
        ),
        None,
    )
    if latest is None:
        return gate_failure
    predicate = str(latest.get("terminal_predicate") or "attempt_failed")
    reason = latest.get("provider_terminal_reason")
    output_tail = latest.get("provider_output_tail")
    detail = "; ".join(
        part
        for part in (
            f"attempt {latest.get('attempt')}: {predicate}",
            f"provider_terminal_reason={reason}" if reason else "",
            f"provider_output_tail={output_tail}" if output_tail else "",
        )
        if part
    )
    attempt_failure = {
        "gate": f"attempt:{predicate}"[:64],
        "detail": detail[:2000],
        "recorded_at": str(latest.get("ended_at") or "")[:64] or None,
    }
    attempt_at = _workday_claim_expiry(latest.get("ended_at"))
    if gate_failure is None:
        return attempt_failure
    if gate_at is None:
        return attempt_failure
    if attempt_at is None or gate_at > attempt_at:
        return gate_failure
    return attempt_failure


def _bounded_receipts(context: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    receipts, overflowed = _bounded_steps(
        context, WORKDAY_MUTATION_RECEIPTS_CONTEXT_KEY, CONSULTATION_RECEIPT_LIMIT
    )
    have_more = (
        overflowed
        or context.get(WORKDAY_MUTATION_RECEIPTS_HAS_MORE_CONTEXT_KEY) is True
    )
    return receipts, have_more


def _consultation_state(
    *,
    packet: ProjectMessage | None,
    result: ProjectMessage | None,
    continuation: ProjectMessage | None,
    claim: Mapping[str, Any] | None,
) -> str:
    """Where the sub-session actually is, named the way an operator asks it."""
    if continuation is not None:
        return "delivered"
    if result is not None:
        return "delivering"
    if packet is None:
        return "requested"
    if claim is not None:
        return "stalled" if claim.get("expired") else "running"
    if packet.handling_state == MessageHandlingState.PENDING:
        return "queued"
    # Terminal packet with no result row: the worker gave up or the packet was
    # parked. Say that, rather than reporting a phase it is not in.
    return "closed_without_result"


def _detail_payload(
    *,
    consultation_id: uuid.UUID,
    request: ProjectMessage,
    packet: ProjectMessage | None,
    result: ProjectMessage | None,
    continuation: ProjectMessage | None,
) -> dict[str, Any]:
    packet_context = _context(packet)
    result_context = _context(result)
    claim = _claim_view(packet, packet_context) if packet is not None else None
    steps, steps_truncated = _bounded_steps(
        packet_context, WORKDAY_OPERATION_LEDGER_CONTEXT_KEY, CONSULTATION_STEP_LIMIT
    )
    receipts, receipts_have_more = _bounded_receipts(result_context)
    attempts = bounded_workday_consultation_attempts(packet_context)
    if not attempts:
        attempts = bounded_workday_consultation_attempts(result_context)
    outcome = result_context.get(_RESULT_STATE)
    continuation_key = packet_context.get(_CONTINUATION_KEY)
    return {
        "consultation_id": str(consultation_id),
        "state": _consultation_state(
            packet=packet, result=result, continuation=continuation, claim=claim
        ),
        "outcome": outcome if isinstance(outcome, str) else None,
        "requested_at": _iso(request.created_at),
        "continuation_key": (
            continuation_key if isinstance(continuation_key, str) else None
        ),
        "question": (
            _bounded_text(packet.content, CONSULTATION_QUESTION_MAX_CHARS)
            if packet is not None
            else None
        ),
        "packet": (
            {
                "id": str(packet.id),
                "created_at": _iso(packet.created_at),
                "updated_at": _iso(packet.updated_at),
                "handling_state": getattr(
                    packet.handling_state, "value", packet.handling_state
                ),
            }
            if packet is not None
            else None
        ),
        "claim": claim,
        "steps": steps,
        "steps_truncated": steps_truncated,
        "attempts": attempts,
        "last_failure": _last_failure(packet_context, attempts),
        "result": (
            {
                "id": str(result.id),
                "created_at": _iso(result.created_at),
                "summary": _bounded_text(
                    result.content, CONSULTATION_SUMMARY_MAX_CHARS
                ),
                "receipts": receipts,
                "receipts_have_more": receipts_have_more,
            }
            if result is not None
            else None
        ),
        "delivered_at": (
            _iso(continuation.created_at) if continuation is not None else None
        ),
    }


def _empty_bucket() -> dict[str, ProjectMessage | None]:
    return {"packet": None, "result": None, "continuation": None}


async def _related_rows(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    consultation_id: uuid.UUID,
) -> dict[str, ProjectMessage | None]:
    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.context[_ATOMIC_REPLY_ID].as_string()
                    == str(consultation_id),
                )
                .order_by(ProjectMessage.sequence, ProjectMessage.id)
                .limit(CONSULTATION_ROW_SCAN_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    found = _empty_bucket()
    for row in rows:
        kind = _classify_related(row)
        # First row of a kind wins: there is exactly one of each, and taking
        # the earliest keeps the reading stable if a duplicate ever lands.
        if kind is not None and found[kind] is None:
            found[kind] = row
    return found


async def project_consultation_detail(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    consultation_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return the bounded detail for one consultation, or ``None``.

    ``None`` means "not an operator-visible consultation in this project" --
    an unknown id, another project's id, or a request the operator can no
    longer see.  Callers must translate that to a 404 rather than leaking
    which of those it was.
    """
    request = (
        await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project_id,
                ProjectMessage.id == consultation_id,
            )
        )
    ).scalar_one_or_none()
    if not _public_request_is_visible(request):
        return None
    related = await _related_rows(
        session, project_id=project_id, consultation_id=consultation_id
    )
    return _detail_payload(
        consultation_id=consultation_id,
        request=request,
        packet=related["packet"],
        result=related["result"],
        continuation=related["continuation"],
    )


def _index_row(
    request: ProjectMessage, bucket: Mapping[str, ProjectMessage | None]
) -> dict[str, Any]:
    packet = bucket["packet"]
    result = bucket["result"]
    continuation = bucket["continuation"]
    packet_context = _context(packet)
    result_context = _context(result)
    claim = _claim_view(packet, packet_context) if packet is not None else None
    steps, steps_truncated = _bounded_steps(
        packet_context, WORKDAY_OPERATION_LEDGER_CONTEXT_KEY, CONSULTATION_STEP_LIMIT
    )
    outcome = result_context.get(_RESULT_STATE)
    return {
        "consultation_id": str(request.id),
        "state": _consultation_state(
            packet=packet, result=result, continuation=continuation, claim=claim
        ),
        "outcome": outcome if isinstance(outcome, str) else None,
        "requested_at": _iso(request.created_at),
        "question_preview": (
            _bounded_text(packet.content, CONSULTATION_PREVIEW_MAX_CHARS)
            if packet is not None
            else None
        ),
        "step_count": len(steps),
        "steps_truncated": steps_truncated,
        "claim_expired": bool(claim and claim.get("expired")),
        "delivered_at": (
            _iso(continuation.created_at) if continuation is not None else None
        ),
    }


async def project_consultation_index(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    limit: int = CONSULTATION_INDEX_LIMIT,
) -> dict[str, Any]:
    """Return the most recent consultations as collapsed progress rows.

    This is the list the conversation renders inline: one line per
    sub-session.  It carries only what a collapsed row shows; the expanded
    view is a second, explicit read of :func:`project_consultation_detail`.
    """
    bounded_limit = max(1, min(CONSULTATION_INDEX_MAX_LIMIT, int(limit)))
    requests = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.from_type.in_(("strategic", "strategist")),
                    ProjectMessage.context["message_role"].as_string()
                    == "operator_reply",
                    ProjectMessage.context[_CONSULTATION_REQUESTED]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(ProjectMessage.sequence.desc(), ProjectMessage.id.desc())
                .limit(bounded_limit)
            )
        )
        .scalars()
        .all()
    )
    visible = [row for row in requests if _public_request_is_visible(row)]
    if not visible:
        return {"consultations": [], "limit": bounded_limit}
    ids = {str(row.id) for row in visible}
    related_rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.project_id == project_id,
                    ProjectMessage.surface == MessageSurface.CONVERSATION,
                    ProjectMessage.context[_ATOMIC_REPLY_ID].as_string().in_(ids),
                )
                .order_by(ProjectMessage.sequence, ProjectMessage.id)
                .limit(bounded_limit * CONSULTATION_ROW_SCAN_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[str, dict[str, ProjectMessage | None]] = {
        key: _empty_bucket() for key in ids
    }
    for row in related_rows:
        kind = _classify_related(row)
        if kind is None:
            continue
        bucket = grouped.get(str(_context(row).get(_ATOMIC_REPLY_ID) or ""))
        if bucket is not None and bucket[kind] is None:
            bucket[kind] = row
    return {
        "consultations": [
            _index_row(request, grouped[str(request.id)]) for request in visible
        ],
        "limit": bounded_limit,
    }


__all__ = [
    "CONSULTATION_INDEX_LIMIT",
    "CONSULTATION_INDEX_MAX_LIMIT",
    "CONSULTATION_PREVIEW_MAX_CHARS",
    "CONSULTATION_QUESTION_MAX_CHARS",
    "CONSULTATION_RECEIPT_LIMIT",
    "CONSULTATION_STEP_LIMIT",
    "CONSULTATION_SUMMARY_MAX_CHARS",
    "project_consultation_detail",
    "project_consultation_index",
]
