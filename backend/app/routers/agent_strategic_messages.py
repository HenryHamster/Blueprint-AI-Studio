"""Agent-facing inbound strategic-message poll endpoint.

The strategic agent (a provider session pinned to the operator's local runner)
polls this to pick up new operator chat messages in
near-real time. The ``since`` cursor plus ascending order let it drain only what
arrived after its last poll — no re-reading the whole thread and no cold
context rebuild — which is what makes the interactive latency target
(§0.2: first token ≤ ~5 s) reachable.

The cursor filter is ``created_at >= since`` (INCLUSIVE), not ``>``: the agent
dedupes by message ``id`` and advances its cursor to the MAX returned
``created_at``, then re-polls a full page from there. An inclusive boundary
means the message(s) sitting exactly on the cursor are re-delivered (harmless —
the agent drops them by id) rather than skipped. This closes two skip races a
strict ``>`` opens: (1) the ">limit truncation skip" — when more than ``limit``
messages share (or straddle) a single ``created_at`` and the page is capped, the
next ``>`` poll would jump past the un-returned remainder; (2) the "commit-race
skip" — a message committed with a ``created_at`` at-or-before the cursor after
the previous query snapshotted. Both are recovered because the agent re-scans
the boundary instant inclusively and dedupes by id.

Read-only, agent-token auth, project-scoped exactly like the strategic-context
fetch surface it sits beside (``agent_strategic_fetch.py``): the same
``_resolve_agent_project`` cross-project 404 semantics and the same 60/min rate
limit. Mutations stay on the ``/api/agent/strategic`` POST surface.

Registered under the same ``/api/agent`` prefix in ``app.main`` and OWNS the
bare ``GET /projects/{project_id}/messages`` path the agent poll helper expects.
Two sibling routes in ``agent_strategic_fetch.py`` deliberately live on DISTINCT
paths so nothing shadows this poll: the ``/messages/{ref}`` single-message
resolver and the ``/messages/open`` compact recovery listing. (Historical note:
the recovery listing briefly collided on bare ``/messages`` and, being included
first in ``app.main``, silently shadowed this poll — resolved 2026-07-05 by
moving the listing to ``/messages/open``.)
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import ProjectMessage
from app.project_message_projection import project_message_base
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.shared_state import limiter

router = APIRouter(prefix="/api/agent", tags=["agent"])

logger = logging.getLogger(__name__)

_MESSAGES_POLL_DEFAULT_LIMIT = 50
_MESSAGES_POLL_MAX_LIMIT = 200


def _parse_since(value: str | None) -> datetime | None:
    """Parse the ``since`` cursor into naive-UTC for comparison.

    ``ProjectMessage.created_at`` is stored via ``NaiveUTCDateTime`` (tz stripped,
    values are UTC), so a tz-aware ISO cursor is converted to UTC and made naive
    before comparison; a naive cursor is used as-is (already assumed UTC). An
    unparseable value is a client error (400), not a silent no-op, so a broken
    cursor can't quietly re-stream the whole thread every poll.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid 'since' timestamp (expected ISO-8601)",
        ) from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _poll_message_dict(message: ProjectMessage) -> dict[str, Any]:
    """Project one message to the exact inbound-poll shape the contract fixes.

    Reuses ``project_message_base`` for enum/timestamp/empty-context
    normalization, then narrows to the seven contract fields so no extra state
    (project_id, from_task_id, resolution, ...) leaks onto the agent surface.
    """
    base = project_message_base(message)
    return {
        "id": base["id"],
        "created_at": base["created_at"],
        "from_type": base["from_type"],
        "status": base["status"],
        "message_type": base["message_type"],
        "content": base["content"],
        "context": base["context"],
    }


@router.get("/projects/{project_id}/messages")
@limiter.limit("60/minute")
async def agent_poll_messages(
    request: Request,
    project_id: uuid.UUID,
    since: str | None = Query(default=None),
    from_type: str = Query(default="user"),
    status: str = Query(default="open"),
    session_id: str | None = Query(default=None),
    limit: int = Query(
        default=_MESSAGES_POLL_DEFAULT_LIMIT,
        ge=1,
        le=_MESSAGES_POLL_MAX_LIMIT,
    ),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return operator messages at-or-after ``since`` for the strategic agent.

    Filters on ``project_id`` + ``from_type`` (default ``user``) + ``status``
    (default ``open``) and, when a cursor is supplied, ``created_at >= since``
    (INCLUSIVE); orders ``created_at`` ascending and caps at ``limit`` — served
    by the ``ix_projectmessage_project_created_at`` index. The agent dedupes by
    message ``id`` and advances its cursor to the MAX returned ``created_at``, so
    the inclusive boundary re-delivers only the message(s) on the cursor instant
    (dropped by id) instead of skipping the un-returned remainder of a truncated
    page or a message committed at-or-before the cursor. ``server_time`` is
    captured BEFORE the query and returned so a cold client with no messages yet
    still has a monotonic cursor to start from.

    Cross-project access returns 404 via ``_resolve_agent_project`` (probing
    can't enumerate other projects' ids).
    """
    await _resolve_agent_project(project_id, token, session)
    server_time = datetime.now(timezone.utc)
    since_dt = _parse_since(since)

    conditions = [
        ProjectMessage.project_id == project_id,
        ProjectMessage.from_type == from_type,
        ProjectMessage.status == status,
    ]
    if since_dt is not None:
        # INCLUSIVE boundary (>=, not >): the agent dedupes by id and advances
        # its cursor to the max returned created_at, so re-scanning the cursor
        # instant closes both the >limit truncation skip and the commit-race
        # skip a strict > would open (see module docstring).
        conditions.append(ProjectMessage.created_at >= since_dt)
    # ``session_id`` is accepted for backward compatibility with older agents but
    # no longer scopes the poll: messages are returned project-wide (per-session
    # scoping was retired), so every agent sees the full project message stream.

    rows = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(*conditions)
                .order_by(ProjectMessage.created_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return {
        "messages": [_poll_message_dict(row) for row in rows],
        "server_time": server_time.isoformat(),
    }
