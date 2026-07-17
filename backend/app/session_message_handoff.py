"""Session-end message hand-off: resolve open operator messages from a concluded session.

When a resident session ends (explicit stop, unattended-cap lapse, or lease
expiry) its open conversation messages would otherwise remain in the
``status='open'`` feed and re-appear in every later strategic payload as fresh
inbox items, inflating the backlog and re-triggering attention.

``handoff_session_messages`` resolves those messages (``status='resolved'``,
``resolution='session_handoff'``) so the open-message feed used by later
spawning cycles (``_fetch_open_messages`` in ``strategic_spawning.py``) excludes
them automatically — messages with ``status!='open'`` are already filtered by
the index-backed query.

The function is intentionally narrowly scoped: it only touches messages whose
``resident_session_id`` matches the ending session, so unrelated project-wide
open messages (standing BUG_REPORTs, unanswered QUESTION messages from other
sessions, messages without a session stamp) are NEVER touched.

Dependency note: ``resident_session_id`` is added to ``ProjectMessage`` by the
session-scope stamping sibling PR. Until that column is present on the model
this function returns 0 (a safe no-op), so the call sites in
``strategic_resident_session`` can deploy independently.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import MessageHandlingState, ProjectMessage

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def handoff_session_messages(
    session: AsyncSession,
    project_id: uuid.UUID,
    resident_session_id: str | None,
) -> int:
    """Resolve open messages that belong to the ending resident session.

    Queries ``ProjectMessage`` rows where ``status='open'`` and
    ``resident_session_id`` matches the ending session, then marks each one
    ``status='resolved'``, ``handling_state='responded'``,
    ``resolution='session_handoff'``, ``resolved_at=now``.

    Returns the number of messages resolved (0 when the column is absent or no
    messages match). Does NOT commit — callers commit after verifying the count
    and/or deciding whether the best-effort operation changed anything.

    Best-effort contract: callers should catch all exceptions, rollback if needed,
    and continue — the session end is already committed before this is called.
    """
    if not resident_session_id:
        return 0

    # Forward-compatibility guard: the resident_session_id column is added by
    # the session-scope stamping sibling PR. Until it lands, this function is
    # safely a no-op — no messages will match and the caller can deploy without
    # waiting for the dependency.
    if not hasattr(ProjectMessage, "resident_session_id"):
        return 0

    result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project_id,
            ProjectMessage.resident_session_id == resident_session_id,  # type: ignore[attr-defined]
            ProjectMessage.status == "open",
        )
    )
    messages = result.scalars().all()
    if not messages:
        return 0

    now = _utcnow_naive()
    for msg in messages:
        msg.status = "resolved"
        msg.handling_state = MessageHandlingState.RESPONDED
        msg.resolution = "session_handoff"
        msg.resolved_at = now
        session.add(msg)

    logger.info(
        "[SESSION_HANDOFF] resolved %d open message(s) for session %s (project %s)",
        len(messages),
        resident_session_id,
        project_id,
    )
    return len(messages)
