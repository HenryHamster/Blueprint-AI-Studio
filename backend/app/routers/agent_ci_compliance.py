"""Agent-facing CI-compliance fetch endpoint."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.ci_compliance import compact_ci_compliance, get_or_compute_ci_compliance
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
    """Return CI-compliance status for the project's current integration branch HEAD.

    Checks module-size guardrails (capped files in module_size_limits.json)
    and branch-literal guardrails (agent handler TS files) via GitHub snapshot
    reads.  Results are SHA-keyed and cached in-memory so a stable HEAD costs
    only one snapshot round-trip per backend restart.

    Returns ``{"status": "success", "data": {"source": "unavailable"}}`` when
    GitHub credentials are unavailable (e.g. project has no GitHub repo
    configured); otherwise returns the full compliance dict with
    ``module_size``, ``branch_literal``, ``overall``, and ``sha`` fields.
    """
    project = await _resolve_agent_project(project_id, token, session)
    envelope = await get_or_compute_ci_compliance(project, session)
    if not isinstance(envelope, dict) or envelope.get("status") != "success":
        return {"status": "success", "data": {"source": "unavailable"}}
    compact = compact_ci_compliance(envelope.get("data"))
    if compact is None:
        return {"status": "success", "data": {"source": "unavailable"}}
    if envelope.get("computed"):
        await session.commit()
    return {"status": "success", "data": compact}
