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
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import Project, ProjectMessage, Task
from app.project_message_projection import project_message_base
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.shared_state import limiter

router = APIRouter(prefix="/api/agent", tags=["agent"])

logger = logging.getLogger(__name__)

_MESSAGES_POLL_DEFAULT_LIMIT = 20
_MESSAGES_POLL_MAX_LIMIT = 20


def _parse_persisted_worker_generation(value: Any) -> int | None:
    """Parse persisted Resident authority, defaulting only missing state to 1."""
    if value is None:
        return 1
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        generation = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            generation = int(raw)
        except ValueError:
            return None
    else:
        return None
    return generation if generation >= 1 else None


async def _lock_resident_poll_project(
    session: AsyncSession, project_id: uuid.UUID
) -> Project | None:
    """Freshen and share-lock canonical Resident authority through the poll."""
    result = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


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
    worker_generation: int | None = Query(default=None, ge=1),
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
    project = await _resolve_agent_project(project_id, token, session)
    authenticated_task_id = uuid.UUID(str(token.get("sub")))
    authenticated_task = await session.get(Task, authenticated_task_id)
    task_context = (
        authenticated_task.context if authenticated_task is not None else {}
    )
    if task_context and task_context.get("_resident_session") is True:
        # Resident replacement takes the Project row exclusively. Hold a fresh
        # shared lock through this poll so validation and message selection are
        # one authority boundary, never validate-gen1/read-after-gen2.
        project = await _lock_resident_poll_project(session, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        authenticated_task = await session.get(
            Task, authenticated_task_id, populate_existing=True
        )
        task_context = (
            authenticated_task.context if authenticated_task is not None else {}
        )
        authenticated_session_id = str(
            task_context.get("strategic_session_id") or ""
        )
        authenticated_generation = _parse_persisted_worker_generation(
            task_context.get("_resident_worker_generation")
        )
        if (
            task_context.get("_resident_session") is not True
            or not authenticated_session_id
            or authenticated_generation is None
            or (session_id is not None and session_id != authenticated_session_id)
            or (
                worker_generation is not None
                and worker_generation != authenticated_generation
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="Resident worker identity mismatch",
            )

        resident_blob = (
            (project.vision or {}).get("_resident_strategic_session") or {}
        )
        current_generation = _parse_persisted_worker_generation(
            resident_blob.get("worker_generation")
        )
        if (
            resident_blob.get("status") != "active"
            or bool(resident_blob.get("stop_requested"))
            or str(resident_blob.get("session_id") or "")
            != authenticated_session_id
            or str(resident_blob.get("task_id") or "")
            != str(authenticated_task_id)
            or current_generation is None
            or current_generation != authenticated_generation
        ):
            raise HTTPException(
                status_code=409,
                detail="Resident worker generation superseded or inactive",
            )
        # Caller-supplied scope is advisory only; authenticated task authority
        # always selects the current Resident conversation.
        session_id = authenticated_session_id
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
    if session_id:
        # Include legacy NULL-session backlog, but never leak a different
        # Workday session into this resident's conversation.
        conditions.append(
            or_(
                ProjectMessage.resident_session_id == session_id,
                ProjectMessage.resident_session_id.is_(None),
            )
        )

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
