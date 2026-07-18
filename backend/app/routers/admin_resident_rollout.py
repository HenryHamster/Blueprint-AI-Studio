"""Owner-facing Strategic Resident rollout health surface."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.shared_state import limiter
from app.strategic_resident_rollout_health import build_resident_rollout_health


router = APIRouter(prefix="/api/admin", tags=["strategic-resident-rollout"])


@router.get("/projects/{project_id}/resident-engine/health")
@limiter.limit("60/minute")
async def get_project_resident_rollout_health(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return durable rollout/readiness evidence without waking the Resident."""

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    return await build_resident_rollout_health(session, project_id)


__all__ = ["router"]
