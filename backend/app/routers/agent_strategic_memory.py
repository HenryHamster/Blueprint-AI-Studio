"""Read-only agent fetches for Strategic Memory V2.

The resident may inspect the same scoped memory heads, immutable provenance,
and content-free retrieval trace that an operator can inspect.  It cannot
perform governed corrections or delete canonical messages: those actions are
intentionally limited to the authenticated admin surface.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.shared_state import limiter
from app.strategic_memory_governance import (
    MemoryObjectNotFound,
    get_project_memory_detail,
    get_project_memory_fetch_trace,
    list_project_memory,
    memory_v2_rollout,
)


router = APIRouter(prefix="/api/agent", tags=["agent"])


def _require_read_enabled(project_id: uuid.UUID) -> None:
    if not memory_v2_rollout(project_id).read_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.get("/projects/{project_id}/memory")
@limiter.limit("60/minute")
async def agent_list_strategic_memory(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=250),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """List current heads for the project backing this agent task."""

    del request
    await _resolve_agent_project(project_id, token, session)
    _require_read_enabled(project_id)
    return {"items": await list_project_memory(session, project_id=project_id, limit=limit, offset=offset)}


@router.get("/projects/{project_id}/memory/fetch-traces/{snapshot_id}")
@limiter.limit("60/minute")
async def agent_get_strategic_memory_fetch_trace(
    request: Request,
    project_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return trace metadata only; canonical envelope bytes never leave storage here."""

    del request
    await _resolve_agent_project(project_id, token, session)
    _require_read_enabled(project_id)
    try:
        return await get_project_memory_fetch_trace(session, project_id=project_id, snapshot_id=snapshot_id)
    except MemoryObjectNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from error


@router.get("/projects/{project_id}/memory/{item_id}")
@limiter.limit("60/minute")
async def agent_get_strategic_memory_detail(
    request: Request,
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return one immutable item history and its scoped provenance receipt IDs."""

    del request
    await _resolve_agent_project(project_id, token, session)
    _require_read_enabled(project_id)
    try:
        return await get_project_memory_detail(session, project_id=project_id, item_id=item_id)
    except MemoryObjectNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from error
