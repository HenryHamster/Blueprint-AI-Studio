"""Admin resolution and read endpoints for evidence-bearing discovery cycles."""

from datetime import datetime
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import _get_user_project_with_role
from app.auth import get_current_user
from app.database import get_session
from app.discovery_review import DiscoveryCycleBusy, resolve_discovery_cycle
from app.models import DiscoveryCycle, Task
from app.task_locking import lock_project_or_429


router = APIRouter()

_DISCOVERY_CYCLES_DEFAULT_LIMIT = 5
_DISCOVERY_CYCLES_MAX_LIMIT = 20


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


class DiscoveryCycleSummary(BaseModel):
    id: uuid.UUID
    state: str
    revision: int
    generation: int
    verdict: str | None
    operator_question: str | None
    review_summary: str | None
    review_result: dict[str, Any] | None
    binding_decision_allowed: bool | None
    bet_snapshot: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None


class DiscoveryCyclesResponse(BaseModel):
    cycles: list[DiscoveryCycleSummary]


def _binding_decision_allowed_for_cycle(
    cycle: DiscoveryCycle, tasks_by_id: dict[uuid.UUID, Task]
) -> bool | None:
    """Read the last-built packet's binding authority off its review task.

    This mirrors the exact check ``complete_discovery_review`` applies at
    decision time (see ``discovery_review.py``): a receipt whose packet
    manifest marks either ``binding_decision_allowed`` false or
    ``required_evidence_omitted`` true only ever permits ``ask_operator``.
    It isn't persisted on the cycle itself, so this is derived here purely
    for operator visibility -- it is not re-validated or enforced.
    """
    task = tasks_by_id.get(cycle.review_task_id) if cycle.review_task_id else None
    if task is None:
        return None
    manifest = task.first_payload.get("evidence_packet_manifest")
    if not isinstance(manifest, dict):
        return None
    if manifest.get("binding_decision_allowed") is False:
        return False
    if manifest.get("required_evidence_omitted") is True:
        return False
    return True


def _discovery_cycle_summary(
    cycle: DiscoveryCycle, tasks_by_id: dict[uuid.UUID, Task]
) -> DiscoveryCycleSummary:
    review_result = cycle.review_result if isinstance(cycle.review_result, dict) else None
    return DiscoveryCycleSummary(
        id=cycle.id,
        state=cycle.state,
        revision=cycle.revision,
        generation=cycle.generation,
        verdict=cycle.verdict,
        operator_question=(
            review_result.get("operator_question") if review_result else None
        ),
        review_summary=review_result.get("summary") if review_result else None,
        review_result=review_result,
        binding_decision_allowed=_binding_decision_allowed_for_cycle(
            cycle, tasks_by_id
        ),
        bet_snapshot=cycle.bet_snapshot or {},
        created_at=cycle.created_at,
        updated_at=cycle.updated_at,
        reviewed_at=cycle.reviewed_at,
    )


@router.get("/projects/{project_id}/directions/{direction_id}/discovery-cycles")
async def list_direction_discovery_cycles(
    project_id: uuid.UUID,
    direction_id: str,
    limit: int = Query(
        default=_DISCOVERY_CYCLES_DEFAULT_LIMIT,
        ge=1,
        le=_DISCOVERY_CYCLES_MAX_LIMIT,
    ),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> DiscoveryCyclesResponse:
    """Read-only history so an operator can see WHY a cycle landed where it
    did -- the operator_question, review summary, and binding-decision
    authority behind the state -- without needing DB/SSH access.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    cycles = (
        (
            await session.execute(
                select(DiscoveryCycle)
                .where(
                    DiscoveryCycle.project_id == project_id,
                    DiscoveryCycle.direction_key == direction_id,
                )
                .order_by(DiscoveryCycle.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    review_task_ids = {
        cycle.review_task_id for cycle in cycles if cycle.review_task_id is not None
    }
    tasks_by_id: dict[uuid.UUID, Task] = {}
    if review_task_ids:
        tasks = (
            (
                await session.execute(
                    select(Task).where(Task.id.in_(review_task_ids))
                )
            )
            .scalars()
            .all()
        )
        tasks_by_id = {task.id: task for task in tasks}

    return DiscoveryCyclesResponse(
        cycles=[_discovery_cycle_summary(cycle, tasks_by_id) for cycle in cycles]
    )
