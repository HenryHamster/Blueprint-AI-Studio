"""Task-JWT API for the bounded no-workspace memory consolidation worker.

The worker receives only the receipt-bound source bytes for its own leased job.
It never receives a GitHub credential, repository URL, or generic project fetch
capability.  Every operation re-derives project, job and provider authority
from the authenticated task rather than trusting values supplied by the agent.
"""

import base64
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import StrategicMemoryConsolidationJob, Task
from app.payload_schemas import MemoryConsolidationPayload
from app.routers.agent import _load_active_agent_task, verify_agent_token
from app.shared_state import limiter
from app.strategic_memory_control import SourceJobLeaseError
from app.strategic_memory_runtime import (
    ConsolidationBudget,
    ConsolidationClaim,
    MemoryRuntimeError,
    ProviderUsage,
    claim_memory_consolidation,
    renew_memory_consolidation,
    submit_memory_consolidation,
)
from app.strategic_memory_service import LedgerAdmissionError, LedgerProposal

router = APIRouter(prefix="/api/agent/memory-consolidation", tags=["agent"])


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    lease_generation: int = Field(ge=1)
    proposal: dict[str, Any]
    usage: dict[str, Any]


class RenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    lease_generation: int = Field(ge=1)


def _task_budget(task: Task) -> ConsolidationBudget:
    try:
        payload = MemoryConsolidationPayload.model_validate(task.first_payload)
    except Exception as exc:  # task was validated before dispatch; keep endpoint fail-closed
        raise HTTPException(status_code=409, detail="invalid_memory_consolidation_payload") from exc
    from app.max_plan_pool import get_task_llm_provider
    from app.task_capabilities import get_task_capability

    provider = str(get_task_llm_provider(task) or "").strip().lower()
    if not provider:
        raise HTTPException(status_code=409, detail="memory_consolidation_has_no_pooled_provider")
    capability = get_task_capability("memory_consolidation")
    allowed_providers = (
        capability.allowed_providers if capability is not None else None
    )
    if not allowed_providers or provider not in allowed_providers:
        # The task record may retain a stale assignment from before a provider
        # was removed from the verified text-only runner set.  Do not give the
        # worker receipt bytes or accept its proposal in that state.
        raise HTTPException(
            status_code=409,
            detail="memory_consolidation_provider_not_qualified",
        )
    return ConsolidationBudget(
        allowed_providers=allowed_providers,
        max_prompt_tokens=payload.max_prompt_tokens,
        max_completion_tokens=payload.max_completion_tokens,
        max_total_tokens=payload.max_total_tokens,
        max_input_bytes=payload.max_input_bytes,
    )


async def _memory_task(session: AsyncSession, token: dict) -> tuple[Task, MemoryConsolidationPayload, ConsolidationBudget]:
    task = await _load_active_agent_task(session, token)
    if (task.first_payload or {}).get("type") != "memory_consolidation":
        raise HTTPException(status_code=403, detail="memory_consolidation_capability_required")
    try:
        payload = MemoryConsolidationPayload.model_validate(task.first_payload)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="invalid_memory_consolidation_payload") from exc
    return task, payload, _task_budget(task)


def _serialize_claim(claim: ConsolidationClaim, budget: ConsolidationBudget) -> dict[str, Any]:
    return {
        "job_id": str(claim.job_id),
        "lease_generation": claim.lease_generation,
        "base_ledger_revision": claim.base_ledger_revision,
        "workspace_requirement": claim.workspace_requirement,
        "capability": claim.capability,
        "max_prompt_tokens": budget.max_prompt_tokens,
        "max_completion_tokens": budget.max_completion_tokens,
        "max_total_tokens": budget.max_total_tokens,
        "sources": [
            {
                "source_id": str(source.source_id),
                "source_sequence": source.source_sequence,
                "source_type": source.source_type,
                "content_hash": source.content_hash,
                "canonical_payload_b64": base64.b64encode(source.canonical_payload).decode("ascii"),
            }
            for source in claim.sources
        ],
    }


@router.post("/claim")
@limiter.limit("20/minute")
async def claim_memory_job(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    task, payload, budget = await _memory_task(session, token)
    try:
        claim = await claim_memory_consolidation(
            session,
            job_id=payload.job_id,
            lease_owner=str(task.id),
            budget=budget,
            expected_project_id=task.project_id,
        )
        await session.commit()
    except (MemoryRuntimeError, SourceJobLeaseError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize_claim(claim, budget)


@router.post("/renew")
@limiter.limit("60/minute")
async def renew_memory_job(
    request: Request,
    body: RenewRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    task, payload, _budget = await _memory_task(session, token)
    if body.job_id != payload.job_id:
        raise HTTPException(status_code=403, detail="job_is_not_bound_to_authenticated_task")
    job = await session.scalar(
        select(StrategicMemoryConsolidationJob).where(
            StrategicMemoryConsolidationJob.id == body.job_id,
            StrategicMemoryConsolidationJob.project_id == task.project_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="memory_consolidation_job_not_found")
    claim = ConsolidationClaim(
        job_id=job.id,
        project_id=job.project_id,
        lease_owner=str(task.id),
        lease_generation=body.lease_generation,
        base_ledger_revision=int(job.base_ledger_revision),
        context_authority_generation=int(job.context_authority_generation),
        privacy_generation=int(job.privacy_generation),
        workspace_requirement="none",
        capability="MEMORY_CONSOLIDATION",
        sources=(),
    )
    try:
        await renew_memory_consolidation(session, claim=claim)
        await session.commit()
    except SourceJobLeaseError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": str(job.id), "lease_generation": body.lease_generation}


@router.post("/submit")
@limiter.limit("20/minute")
async def submit_memory_job(
    request: Request,
    body: SubmitRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
) -> dict[str, Any]:
    task, payload, budget = await _memory_task(session, token)
    if body.job_id != payload.job_id:
        raise HTTPException(status_code=403, detail="job_is_not_bound_to_authenticated_task")
    # This is intentionally an unlocked scope read.  ``submit_memory_*``
    # acquires Project -> state -> job inside ledger admission; locking job
    # here would invert that global order.
    job = await session.scalar(
        select(StrategicMemoryConsolidationJob)
        .where(
            StrategicMemoryConsolidationJob.id == body.job_id,
            StrategicMemoryConsolidationJob.project_id == task.project_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="memory_consolidation_job_not_found")
    try:
        proposal = LedgerProposal.model_validate(body.proposal)
        usage = ProviderUsage(**body.usage)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid_installed_memory_proposal") from exc
    claim = ConsolidationClaim(
        job_id=job.id,
        project_id=job.project_id,
        lease_owner=str(task.id),
        lease_generation=body.lease_generation,
        base_ledger_revision=int(job.base_ledger_revision),
        context_authority_generation=int(job.context_authority_generation),
        privacy_generation=int(job.privacy_generation),
        workspace_requirement="none",
        capability="MEMORY_CONSOLIDATION",
        sources=(),
    )
    try:
        admitted = await submit_memory_consolidation(
            session,
            claim=claim,
            proposal=proposal,
            usage=usage,
            budget=budget,
        )
        await session.commit()
    except (MemoryRuntimeError, SourceJobLeaseError, LedgerAdmissionError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": str(admitted.job_id),
        "ledger_revision": admitted.ledger_revision,
        "replayed": admitted.replayed,
    }
