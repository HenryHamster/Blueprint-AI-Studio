"""Agent-facing CI-compliance fetch endpoint."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.ci_compliance import compact_ci_compliance, ensure_current_ci_compliance
from app.database import get_session
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.shared_state import limiter

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/projects/{project_id}/ci-compliance")
@limiter.limit("60/minute")
async def agent_fetch_ci_compliance(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return the current integration-branch HEAD CI-compliance status."""
    project = await _resolve_agent_project(project_id, token, session)
    status = await ensure_current_ci_compliance(session, project)
    if status.get("status") != "success":
        raise HTTPException(
            status_code=503,
            detail={
                "reason": status.get("reason"),
                "integration_branch": status.get("integration_branch"),
            },
        )
    await session.commit()
    data = compact_ci_compliance(status.get("data"))
    return {
        "status": "success",
        "data": {
            **(data or {}),
            "integration_branch": status.get("integration_branch"),
            "computed": bool(status.get("computed")),
        },
    }
