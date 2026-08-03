"""Admin resolution endpoint for evidence-bearing discovery cycles."""

from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.discovery_review import DiscoveryCycleBusy, resolve_discovery_cycle
from app.task_locking import lock_project_or_429


router = APIRouter()


class DiscoveryResolutionPayload(BaseModel):
    cycle_id: uuid.UUID
    cycle_revision: int = Field(ge=0)
    action: str = Field(pattern=r"^(continue|park|close)$")
    next_bet: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.post("/projects/{project_id}/directions/{direction_id}/discovery-resolution")
async def resolve_direction_discovery_cycle(
    project_id: uuid.UUID,
    direction_id: str,
    payload: DiscoveryResolutionPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    # Canonical write order is Project -> Cycle. SKIP LOCKED turns a competing
    # completion into an explicit retry instead of a Cycle -> Project deadlock.
    await lock_project_or_429(session, project_id)

    try:
        resolved = await resolve_discovery_cycle(
            session,
            project_id=project_id,
            direction_key=direction_id,
            cycle_id=payload.cycle_id,
            cycle_revision=payload.cycle_revision,
            action=payload.action,
            next_bet=payload.next_bet,
            note=payload.note,
            skip_locked=True,
        )
    except DiscoveryCycleBusy as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "2"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {
        "status": "resolved",
        "action": payload.action,
        "cycle_id": str(resolved.id),
        "generation": resolved.generation,
    }
