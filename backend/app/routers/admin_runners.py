"""Admin endpoints for managing pull-based fleet runners.

A fleet runner is a process on operator hardware that polls the cloud
orchestrator for work (see ``app.routers.runner``). These endpoints let a
project owner enroll a runner (minting its one-time secret token), list the
project's runners, and revoke one. Only the SHA256 hash of a runner's token is
ever stored — the plaintext is returned exactly once, at registration.
"""

import logging
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import create_runner_token, hash_runner_token, get_current_user
from app.database import get_session
from app.models import LocalRunner

logger = logging.getLogger("blueprint")

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-runners"],
    dependencies=[Depends(get_current_user)],
)


# Default capability shape for a freshly registered runner: handles every task
# type, no GPU, two concurrent tasks. Callers may override any subset.
_DEFAULT_CAPABILITIES: Dict[str, Any] = {
    "task_types": "all",
    "has_gpu": False,
    "max_concurrent": 2,
}


class RunnerRegisterRequest(BaseModel):
    hostname: str | None = None
    version: str | None = None
    capabilities: Dict[str, Any] | None = None


def _normalize_capabilities(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    """Merge a caller-supplied capabilities dict over the defaults."""
    caps = dict(_DEFAULT_CAPABILITIES)
    if raw:
        caps.update(raw)
    return caps


def _runner_view(runner: LocalRunner) -> Dict[str, Any]:
    return {
        "id": str(runner.id),
        "hostname": runner.hostname,
        "version": runner.version,
        "status": runner.status,
        "capabilities": runner.capabilities,
        "last_seen_at": (
            runner.last_seen_at.isoformat() if runner.last_seen_at else None
        ),
        "created_at": runner.created_at.isoformat() if runner.created_at else None,
    }


@router.post("/projects/{project_id}/runners/register")
async def register_runner(
    project_id: uuid.UUID,
    payload: RunnerRegisterRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Register a new fleet runner and return its one-time secret token.

    The plaintext token is included in the response exactly once and is never
    stored — only its SHA256 hash is persisted on the runner row.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    runner = LocalRunner(
        project_id=project_id,
        token_hash="",  # filled below once we have the runner id for the token
        hostname=payload.hostname,
        version=payload.version,
        capabilities=_normalize_capabilities(payload.capabilities),
    )
    token = create_runner_token(runner.id)
    runner.token_hash = hash_runner_token(token)
    session.add(runner)
    await session.commit()
    await session.refresh(runner)

    logger.info(
        "[RUNNERS] Registered runner %s for project %s (hostname=%s)",
        runner.id,
        project_id,
        payload.hostname,
    )
    # The token is surfaced ONCE here and never again.
    return {"runner_id": str(runner.id), "token": token}


@router.get("/projects/{project_id}/runners")
async def list_runners(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """List the project's runners (excluding deleted ones)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    result = await session.execute(
        select(LocalRunner)
        .where(
            LocalRunner.project_id == project_id,
            LocalRunner.status != "deleted",
        )
        .order_by(LocalRunner.created_at.desc())
    )
    return [_runner_view(r) for r in result.scalars().all()]


@router.delete("/projects/{project_id}/runners/{runner_id}")
async def delete_runner(
    project_id: uuid.UUID,
    runner_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Revoke a runner by marking it deleted (token can no longer authenticate)."""
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    result = await session.execute(
        select(LocalRunner).where(
            LocalRunner.id == runner_id,
            LocalRunner.project_id == project_id,
        )
    )
    runner = result.scalars().first()
    if runner is None or runner.status == "deleted":
        raise HTTPException(status_code=404, detail="Runner not found")

    runner.status = "deleted"
    session.add(runner)
    await session.commit()
    logger.info("[RUNNERS] Deleted runner %s for project %s", runner_id, project_id)
    return {"runner_id": str(runner_id), "status": "deleted"}
