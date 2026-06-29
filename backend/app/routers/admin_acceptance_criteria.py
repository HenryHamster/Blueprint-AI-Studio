"""Admin endpoints for version-scoped acceptance criteria management.

Provides:
  GET  /projects/{project_id}/acceptance-criteria          — list criteria for open version
  POST /projects/{project_id}/acceptance-criteria/link-direction — link criteria keys to a direction
"""

import logging
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app import acceptance_criteria as ac
from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.models import Direction

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/projects/{project_id}/acceptance-criteria")
async def list_acceptance_criteria(
    project_id: uuid.UUID,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return criteria and coverage summary for the project's open release version."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        return {"release_version_id": None, "criteria": [], "total": 0}
    summary = await ac.get_version_coverage_summary(session, open_version.id)
    return {
        "release_version_id": str(open_version.id),
        "version_number": open_version.version_number,
        **summary,
    }


class LinkDirectionRequest(BaseModel):
    direction_key: str = Field(..., max_length=128)
    criterion_keys: List[str] = Field(..., max_length=50)


@router.post("/projects/{project_id}/acceptance-criteria/link-direction")
async def link_criteria_to_direction(
    project_id: uuid.UUID,
    body: LinkDirectionRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Link a set of criterion keys to a direction row (sets Direction.criterion_keys).

    Validates that every supplied criterion_key exists in the current open
    release version.  Keys from closed or older versions are rejected.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.release_version import current_open_version

    open_version = await current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(status_code=409, detail="No open release version")

    # Validate that all supplied keys exist in the current open version.
    # criterion_key is unique only within release_version_id, so we must scope
    # the lookup to open_version.id to reject stale keys from closed versions.
    if body.criterion_keys:
        existing = await ac.get_criteria_by_keys(
            session,
            project_id,
            body.criterion_keys,
            release_version_id=open_version.id,
        )
        existing_keys = {r.criterion_key for r in existing}
        unknown = [k for k in body.criterion_keys if k not in existing_keys]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown criterion keys for this version: {unknown}",
            )

    row = (
        await session.execute(
            select(Direction).where(
                Direction.project_id == project_id,
                Direction.direction_key == body.direction_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Direction not found")

    row.criterion_keys = list(body.criterion_keys)
    session.add(row)
    await session.commit()
    logger.info(
        "[AC] Linked %d criterion key(s) to direction %s for project %s",
        len(body.criterion_keys),
        body.direction_key,
        project_id,
    )
    return {
        "status": "ok",
        "direction_key": body.direction_key,
        "criterion_keys": list(body.criterion_keys),
    }
