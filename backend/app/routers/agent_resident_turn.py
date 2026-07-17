"""Credential-free managed-agent endpoints for authoritative Resident turns."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_agent_jwt
from app.database import AsyncSessionLocal, get_session
from app.models import TaskStatus
from app.routers.agent import verify_agent_token
from app.strategic_operation_authority_validation import StrategicAuthorityError
from app.strategic_resident_agent_binding import (
    ResidentAgentAuthorityError,
    ResidentAgentBinding,
    ResidentAgentBindingError,
    ResidentAgentTaskNotFound,
    load_live_resident_agent_task_locked,
)
from app.strategic_resident_agent_contracts import (
    ResidentTurnCompleteRequest,
    ResidentTurnFailRequest,
)
from app.strategic_resident_context import reconstruct_authoritative_turn_input
from app.strategic_resident_failure import fail_authoritative_resident_turn
from app.strategic_resident_reconciliation import (
    StrategicResidentReconcileRequest,
    reconcile_strategic_resident_turn,
)
from app.strategic_resident_realtime import broadcast_resident_reconciliation
router = APIRouter(prefix="/api/agent/resident-turn", tags=["agent"])


def _require_task_token(token: dict[str, Any], task_id: uuid.UUID) -> None:
    try:
        token_task_id = uuid.UUID(str(token.get("sub")))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=403,
            detail="task-scoped token required",
        ) from None
    if token_task_id != task_id:
        raise HTTPException(status_code=403, detail="agent token task mismatch")


def _agent_binding(token: dict[str, Any]) -> ResidentAgentBinding:
    try:
        return ResidentAgentBinding(
            task_id=uuid.UUID(str(token.get("sub"))),
            project_id=uuid.UUID(str(token.get("project_id"))),
            vm_instance_id=str(token.get("vm_instance_id") or ""),
            task_type=str(token.get("task_type") or ""),
            claim_nonce=str(token.get("claim_nonce") or ""),
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=403,
            detail="complete Resident task token required",
        ) from None


def _binding_for_request(
    token: dict[str, Any],
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> ResidentAgentBinding:
    binding = _agent_binding(token)
    if binding.task_id != task_id or (
        project_id is not None and binding.project_id != project_id
    ):
        raise HTTPException(status_code=403, detail="agent token scope mismatch")
    return binding


@router.get("/context")
async def get_resident_turn_context(
    request: Request,
    task_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    del request
    binding = _binding_for_request(token, task_id=task_id)
    try:
        task = await load_live_resident_agent_task_locked(
            session,
            binding,
            allowed_statuses=frozenset({TaskStatus.IN_PROGRESS}),
        )
    except ResidentAgentTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResidentAgentAuthorityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResidentAgentBindingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        provider_input = await reconstruct_authoritative_turn_input(
            session,
            project_id=task.project_id,
            turn_task_id=task.id,
        )
    except StrategicAuthorityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Reconstruction narrows ArtifactRef authority to the exact post-fit
    # provider projection. Persist that freeze before returning context so the
    # agent's next, separately transacted retrieval request observes it.
    await session.commit()
    turn_task_version = int(task.version)
    return {
        "provider_input": provider_input,
        "turn_task_version": turn_task_version,
    }


@router.post("/complete")
async def complete_resident_turn(
    request: Request,
    body: ResidentTurnCompleteRequest,
    x_agent_token: str = Header(
        ...,
        description="Secret token for agent authentication",
    ),
) -> dict[str, Any]:
    del request
    token = verify_agent_jwt(x_agent_token)
    binding = _binding_for_request(
        token,
        task_id=body.task_id,
        project_id=body.project_id,
    )
    try:
        outcome = await reconcile_strategic_resident_turn(
            AsyncSessionLocal,
            StrategicResidentReconcileRequest(
                project_id=body.project_id,
                turn_task_id=body.task_id,
                authority_generation=body.authority_generation,
                reconciliation_id=body.reconciliation_id,
                expected_turn_task_version=body.expected_turn_task_version,
                expected_checkpoint_id=body.expected_checkpoint_id,
                expected_checkpoint_version=body.expected_checkpoint_version,
                expected_checkpoint_hash=body.expected_checkpoint_hash,
                context_hash=body.context_hash,
                agent_binding=binding,
                turn_result=body.result,
                accepted_memory_proposal_ids=tuple(
                    body.accepted_memory_proposal_ids
                ),
                accepted_implication_ids=tuple(body.accepted_implication_ids),
                accepted_open_loop_ids=tuple(body.accepted_open_loop_ids),
                resolved_open_loop_ids=tuple(body.resolved_open_loop_ids),
                evidence_inspections=tuple(body.evidence_inspections),
            ),
        )
    except HTTPException:
        raise
    except ResidentAgentBindingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StrategicAuthorityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await broadcast_resident_reconciliation(
        AsyncSessionLocal,
        project_id=body.project_id,
        turn_task_id=body.task_id,
        outcome=outcome,
    )
    return {
        "status": "completed",
        "checkpoint_id": str(outcome.checkpoint_id),
        "checkpoint_version": outcome.checkpoint_version,
        "checkpoint_hash": outcome.checkpoint_hash,
        "reply_message_id": (
            str(outcome.reply_message_id) if outcome.reply_message_id else None
        ),
        "promoted_task_id": (
            str(outcome.promoted_task_id) if outcome.promoted_task_id else None
        ),
        "idempotent_replay": outcome.idempotent_replay,
    }


@router.post("/fail")
async def fail_resident_turn(
    request: Request,
    body: ResidentTurnFailRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    del request
    binding = _binding_for_request(
        token,
        task_id=body.task_id,
        project_id=body.project_id,
    )
    try:
        outcome = await fail_authoritative_resident_turn(
            session,
            binding=binding,
            failure_id=body.failure_id,
            reason=body.reason,
            failure_category=body.failure_category,
        )
    except ResidentAgentTaskNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResidentAgentAuthorityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ResidentAgentBindingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "status": "failed",
        "task_id": str(outcome.task_id),
        "idempotent_replay": outcome.idempotent_replay,
        "promoted_task_id": (
            str(outcome.promoted_task_id) if outcome.promoted_task_id else None
        ),
    }


__all__ = ["ResidentTurnCompleteRequest", "ResidentTurnFailRequest", "router"]
