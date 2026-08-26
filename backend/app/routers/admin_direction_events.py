"""Admin endpoint for querying the direction lifecycle event trail.

GET /api/admin/projects/{project_id}/directions/{direction_key}/events
    Returns the full lifecycle event trail for a direction, newest first.
    Supports optional ``limit`` (default 50, max 200) and ``offset`` query params.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.models import Direction, DirectionEvent
from app.time_utils import utc_iso

router = APIRouter(prefix="/api/admin")
logger = logging.getLogger(__name__)

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


@router.get("/projects/{project_id}/directions/{direction_key}/events")
async def list_direction_events(
    project_id: uuid.UUID,
    direction_key: str,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the lifecycle event trail for a direction, newest first.

    404 if the direction has never existed (no rows in the direction table AND
    no event trail rows).  A pruned direction that no longer has a ``direction``
    table row still returns its event trail so operators can audit the full
    lifecycle after a prune.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )

    # Accept either a live direction row or a non-empty event trail as proof
    # that the direction_key is real for this project.
    direction_row = (
        await session.execute(
            select(Direction).where(
                Direction.project_id == project_id,
                Direction.direction_key == direction_key,
            )
        )
    ).scalar_one_or_none()

    event_count = await session.scalar(
        select(func.count()).select_from(DirectionEvent).where(
            DirectionEvent.project_id == project_id,
            DirectionEvent.direction_key == direction_key,
        )
    )

    if direction_row is None and not event_count:
        raise HTTPException(status_code=404, detail=f"Direction '{direction_key}' not found")

    # Paginated fetch, newest first.
    rows = (
        (
            await session.execute(
                select(DirectionEvent)
                .where(
                    DirectionEvent.project_id == project_id,
                    DirectionEvent.direction_key == direction_key,
                )
                .order_by(DirectionEvent.recorded_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    events = [
        {
            "id": str(row.id),
            "direction_key": row.direction_key,
            "event_kind": row.event_kind,
            "prior_status": row.prior_status,
            "new_status": row.new_status,
            "actor": row.actor,
            "reason": row.reason,
            "snapshot_data": row.snapshot_data,
            "recorded_at": utc_iso(row.recorded_at),
        }
        for row in rows
    ]

    return {
        "direction_key": direction_key,
        "project_id": str(project_id),
        "total": event_count or 0,
        "offset": offset,
        "limit": limit,
        "events": events,
    }
