"""Ephemeral canonical projections for typed Fast reply streaming.

Typed Fast writes its final answer through the receipt-bound coordinator.  While
that provider turn is running, this module builds the same bounded
``ProjectMessage`` shape for a chunked preview.  The preview is deliberately
not persisted; its deterministic id and idempotency key let the durable reply
replace it in the existing conversation merge path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.sql import Select
from sqlmodel import select

from app.message_audience import stamp_message_audience
from app.models import MessageHandlingState, MessageSurface, MessageType, ProjectMessage
from app.project_message_projection import message_event_payload

STREAMING_PREVIEW_CONTEXT_KEY = "_conversation_streaming_preview"
STREAMING_PREVIEW_DELTA_CONTEXT_KEY = "_conversation_streaming_delta"
STREAMING_PREVIEW_SEQUENCE_CONTEXT_KEY = "_conversation_streaming_seq"
STREAMING_PREVIEW_TERMINAL_CONTEXT_KEY = "_conversation_streaming_terminal"
STREAMING_PREVIEW_NAMESPACE = uuid.UUID("2dd4d06f-70a6-4ccf-9eb8-6f7e17f2e4df")


def streaming_reply_message_id(
    project_id: uuid.UUID, idempotency_key: str, attempt: int = 0
) -> uuid.UUID:
    """Return the stable preview id shared by every delta of ONE reply attempt.

    ``attempt`` participates in the identity because a provider crash re-runs
    the same receipt — and therefore the same ``idempotency_key`` — from the
    top.  Its text is a new answer, not a continuation, so it must not share a
    preview row with the abandoned partial.  The ``idempotency_key`` FIELD is
    deliberately left un-namespaced: that is what the durable reply matches on
    when it replaces the preview.
    """

    suffix = "" if attempt == 0 else f":{attempt}"
    return uuid.uuid5(
        STREAMING_PREVIEW_NAMESPACE,
        f"{project_id}:{idempotency_key}{suffix}",
    )


def select_live_fast_claim(
    *,
    project_id: uuid.UUID,
    claim_id: uuid.UUID,
    task_id: uuid.UUID,
    now: datetime,
) -> Select:
    """Build the relay's liveness fence for one preview delta.

    Extracted so a DB-backed test can COMPILE AND EXECUTE it against Postgres.
    A mocked session proves nothing here: a wrong JSON path, a wrong column, or
    a wrong enum comparison would leave streaming permanently dead in
    production with every unit test green.

    Mirrors the canonical claim fence in
    ``routers/agent_strategic_messages.py`` — same ``from_type``/``status``/
    ``handling_state``/owner predicates, and the same requirement that the
    claim lease has not expired.  Generation and incarnation are the two
    canonical predicates deliberately NOT checked: they are not carried on the
    frame, and the socket is already bound to this exact task.
    """

    return (
        select(ProjectMessage)
        .where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.conversation_claim_id == claim_id,
            ProjectMessage.from_type == "user",
            ProjectMessage.status == "open",
            ProjectMessage.handling_state == MessageHandlingState.CLAIMED,
            ProjectMessage.handled_by_task_id == task_id,
            ProjectMessage.conversation_claim_expires_at.is_not(None),
            ProjectMessage.conversation_claim_expires_at > now,
            ProjectMessage.context["_conversation_public_reply_owner"].as_string()
            == "fast",
        )
        .limit(1)
    )


def select_max_conversation_sequence(project_id: uuid.UUID) -> Select:
    """Highest durable conversation sequence, for placing the preview last."""

    return select(func.max(ProjectMessage.sequence)).where(
        ProjectMessage.project_id == project_id,
        ProjectMessage.surface == MessageSurface.CONVERSATION,
    )


def build_streaming_reply_message(
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    session_id: str,
    claim_id: uuid.UUID,
    idempotency_key: str,
    content: str,
    sequence: int,
    attempt: int = 0,
    now: datetime | None = None,
) -> ProjectMessage:
    """Build a client-safe cumulative reply preview without inserting it."""

    timestamp = now or datetime.now(timezone.utc)
    context = stamp_message_audience(
        {
            STREAMING_PREVIEW_CONTEXT_KEY: True,
            "message_role": "operator_reply",
            "_conversation_claim_id": str(claim_id),
            "_conversation_public_reply_owner": "fast",
        },
        "question_answer",
        message_type=MessageType.NOTE.value,
    )
    return ProjectMessage(
        id=streaming_reply_message_id(project_id, idempotency_key, attempt),
        project_id=project_id,
        sequence=sequence,
        surface=MessageSurface.CONVERSATION,
        handling_state=MessageHandlingState.RESPONDED,
        message_type=MessageType.NOTE,
        from_type="strategic",
        from_task_id=task_id,
        content=content,
        context=context,
        status="resolved",
        created_at=timestamp,
        updated_at=timestamp,
        idempotency_key=idempotency_key,
        resident_session_id=session_id,
    )


def streaming_reply_event_payload(
    message: ProjectMessage,
    *,
    stream_seq: int,
    terminal: str | None = None,
) -> dict[str, Any]:
    """Project one bounded preview delta or terminal tombstone.

    The normal message projection is refetchable, but these messages are
    deliberately ephemeral. Marking the transport projection in its context
    lets the UI append the bounded chunk locally and prevents it from trying to
    fetch the non-persisted preview when a long answer crosses the event cap.
    """

    payload = message_event_payload(message)
    projected_message = payload["message"]
    context = projected_message.setdefault("context", {})
    context[STREAMING_PREVIEW_DELTA_CONTEXT_KEY] = terminal is None
    context[STREAMING_PREVIEW_SEQUENCE_CONTEXT_KEY] = stream_seq
    if terminal is not None:
        context[STREAMING_PREVIEW_TERMINAL_CONTEXT_KEY] = terminal
    payload["streaming"] = True
    return payload
