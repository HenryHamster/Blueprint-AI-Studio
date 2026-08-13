"""Durable message eligibility for one conversational Workday session."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, false, func, or_

from app.models import ProjectMessage


RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY = "conversation_floor_sequence"
RESIDENT_OPEN_MESSAGE_CURSOR_FLOOR_KEY = "open_message_cursor_floor"


def resident_conversation_floor_sequence(
    source: dict[str, Any] | None,
) -> int | None:
    """Read a persisted positive transcript floor, rejecting malformed state."""

    value = (source or {}).get(RESIDENT_CONVERSATION_FLOOR_SEQUENCE_KEY)
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def resident_conversation_scope(
    resident_session_id: str,
    *,
    conversation_floor_sequence: int | None,
) -> object:
    """Return the canonical row scope for a Resident conversation.

    Rows explicitly stamped for this session stay visible even when they predate
    the session floor (the session-handoff path intentionally rebinds unanswered
    input this way). Unstamped rows are admitted only at or after the durable
    transcript floor. ``None`` retains the mixed-revision legacy scope while a
    pre-boundary Resident is still running during rollout.
    """

    null_session_scope: object = ProjectMessage.resident_session_id.is_(None)
    if conversation_floor_sequence is not None:
        null_session_scope = and_(
            null_session_scope,
            ProjectMessage.sequence.is_not(None),
            ProjectMessage.sequence >= conversation_floor_sequence,
        )
    return or_(
        ProjectMessage.resident_session_id == resident_session_id,
        null_session_scope,
    )


def resident_broad_message_scope(
    resident_session_id: str,
    *,
    conversation_floor_sequence: int | None,
) -> object:
    """Scope a broad Resident inbox without hiding non-conversation activity.

    The durable floor is a replay boundary for unstamped operator conversation
    input only. Activity, blocker, bug-report, and other general inbox rows keep
    their legacy NULL-session visibility even when their sequence predates the
    chat boundary. ``coalesce(..., false)`` makes the negation NULL-safe on both
    SQLite and PostgreSQL if a legacy row has partially-null routing metadata.
    """

    null_session_scope: object = ProjectMessage.resident_session_id.is_(None)
    if conversation_floor_sequence is not None:
        from app.strategic_conversation_query import (
            strategic_conversation_predicate,
        )

        canonical_operator_conversation = and_(
            ProjectMessage.from_type.in_(("user", "operator")),
            strategic_conversation_predicate(),
        )
        noncanonical_message = func.coalesce(
            canonical_operator_conversation,
            false(),
        ).is_(False)
        null_session_scope = and_(
            null_session_scope,
            or_(
                noncanonical_message,
                and_(
                    ProjectMessage.sequence.is_not(None),
                    ProjectMessage.sequence >= conversation_floor_sequence,
                ),
            ),
        )
    return or_(
        ProjectMessage.resident_session_id == resident_session_id,
        null_session_scope,
    )
