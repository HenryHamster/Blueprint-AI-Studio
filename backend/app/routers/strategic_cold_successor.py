"""Owner launch/readback and proof-agent endpoints for a cold successor."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.routers.agent import verify_agent_token
from app.routers.agent_strategic_fetch import _resolve_agent_project
from app.shared_state import limiter
from app.strategic_cold_successor_lifecycle import (
    ColdSuccessorAttestationRequest,
    ColdSuccessorHeartbeatRequest,
    ColdSuccessorReceiptRequest,
    ColdSuccessorStartRequest,
    ColdSuccessorTerminalRequest,
    attest_cold_successor,
    cold_successor_status,
    heartbeat_cold_successor,
    launch_cold_successor,
    read_cold_successor_receipt,
    terminal_cold_successor,
)
from app.strategic_cold_successor_authority import COLD_SUCCESSOR_LAUNCH_MODE


router = APIRouter(tags=["strategic-cold-successor"])


@router.post("/api/admin/projects/{project_id}/strategic-session/cold-successor/start")
@limiter.limit("10/minute")
async def start_cold_successor_route(
    request: Request,
    project_id: uuid.UUID,
    payload: ColdSuccessorStartRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    return await launch_cold_successor(
        session, project_id, payload, operator_user_id=current_user["user_id"]
    )


@router.get("/api/admin/projects/{project_id}/strategic-session/cold-successor")
async def get_cold_successor_route(
    project_id: uuid.UUID,
    missing_ok: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    try:
        return await cold_successor_status(session, project_id)
    except HTTPException as exc:
        if not (missing_ok and exc.status_code == 404):
            raise
        return {
            "launch_mode": COLD_SUCCESSOR_LAUNCH_MODE,
            "proof_state": "absent",
        }


@router.post("/api/agent/projects/{project_id}/strategic-session/cold-successor/heartbeat")
@limiter.limit("60/minute")
async def heartbeat_cold_successor_route(
    request: Request,
    project_id: uuid.UUID,
    payload: ColdSuccessorHeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    await _resolve_agent_project(project_id, token, session)
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    return await heartbeat_cold_successor(session, project_id, payload)


@router.post("/api/agent/projects/{project_id}/strategic-session/cold-successor/attest")
@limiter.limit("10/minute")
async def attest_cold_successor_route(
    request: Request,
    project_id: uuid.UUID,
    payload: ColdSuccessorAttestationRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    await _resolve_agent_project(project_id, token, session)
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    return await attest_cold_successor(session, project_id, payload)


@router.get("/api/agent/projects/{project_id}/strategic-session/cold-successor/receipt")
@limiter.limit("60/minute")
async def read_cold_successor_receipt_route(
    request: Request,
    project_id: uuid.UUID,
    payload: ColdSuccessorReceiptRequest = Depends(),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    await _resolve_agent_project(project_id, token, session)
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    return await read_cold_successor_receipt(session, project_id, payload)


@router.post("/api/agent/projects/{project_id}/strategic-session/cold-successor/terminal")
@limiter.limit("20/minute")
async def terminal_cold_successor_route(
    request: Request,
    project_id: uuid.UUID,
    payload: ColdSuccessorTerminalRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    await _resolve_agent_project(project_id, token, session)
    if str(token.get("sub")) != str(payload.task_id):
        raise HTTPException(status_code=403, detail="Token task mismatch")
    return await terminal_cold_successor(session, project_id, payload)
