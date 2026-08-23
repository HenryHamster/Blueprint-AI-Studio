"""Operator read path for private Workday consultations.

A Resident-invoked sub-session runs against an internal packet the operator
conversation deliberately never carries.  These two endpoints are the explicit
"show me what that is doing" request: a collapsed index the conversation can
render as one progress line per sub-session, and a detail read for the
expanded view.

Neither endpoint changes any message audience.  They project bounded, durable
queue state for consultations the operator's own thread already refers to, and
they are read-only -- no claim, lease, ack or delivery can be driven from here.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.shared_state import limiter
from app.strategic_consultation_detail import (
    CONSULTATION_INDEX_LIMIT,
    CONSULTATION_INDEX_MAX_LIMIT,
    project_consultation_detail,
    project_consultation_index,
)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-projects"],
    dependencies=[Depends(get_current_user)],
)

logger = logging.getLogger(__name__)


@router.get("/projects/{project_id}/strategic-session/consultations")
@limiter.limit("60/minute")
async def list_project_consultations(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(CONSULTATION_INDEX_LIMIT, ge=1, le=CONSULTATION_INDEX_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Collapsed progress rows for the most recent Workday consultations.

    One row per sub-session: what it was asked (preview), where it is, how many
    durable operations it has recorded, and when its answer reached the thread.
    Guarded with the same viewer-safe project-access check as the sibling
    Strategic read models.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    return await project_consultation_index(session, project_id=project.id, limit=limit)


@router.get(
    "/projects/{project_id}/strategic-session/consultations/{consultation_id}"
)
@limiter.limit("60/minute")
async def get_project_consultation(
    request: Request,
    project_id: uuid.UUID,
    consultation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Expanded detail for one consultation.

    ``consultation_id`` is the public reply message id the operator already
    sees in the thread.  A 404 covers every not-yours/not-visible case without
    distinguishing them.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    detail = await project_consultation_detail(
        session, project_id=project.id, consultation_id=consultation_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Consultation not found")
    return detail
