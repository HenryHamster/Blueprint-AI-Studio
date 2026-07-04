"""Direction management endpoints for project autonomous improvement steering."""

import uuid
import logging
from math import ceil
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import get_session
from app.broadcast import broadcast_to_project
from app.models import (
    Task,
    TaskStatus,
    TaskHistory,
    ProjectMessage,
    RECOMMENDED_MODE_META_KEY,
    _utcnow,
)
from app.auth import get_current_user
from app.admin import _get_user_project, _get_user_project_with_role
from app.payload_schemas import validate_internal_payload
from app.task_locking import (
    acquire_direction_research_spawn_lock,
    lock_project_or_429,
    with_directions_locked,
)
from app.direction_lifecycle import _reset_outcome_detection_state
from app.strategic_events import STRATEGIC_KIND_DIRECTIONS, emit_strategic_updated
from app.capability_proof import enrich_problem_model_capability_proofs
from app.direction_refractory import get_direction_refractory_info
from app.project_message_projection import legacy_direction_message_dict
from app.project_message_creation import (
    create_operator_project_message,
    parse_operator_message_type,
)
from app.problem_model import check_gap_id_write, compute_model_size_signal, ProblemModel

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin", tags=["directions"], dependencies=[Depends(get_current_user)]
)


DIRECTION_TEXT_MAX = 2000
COMPLETION_CRITERIA_MAX = 500
DIRECTION_CONTEXT_FIELD_MAX = 500


def _compute_direction_merges_per_cycle(d: dict) -> Any:
    """Return merged-PRs-per-cycle for a direction. Returns None if never researched."""
    history: list[dict[str, Any]] = list(d.get("research_history") or [])
    agg: dict[str, Any] = dict(d.get("history_aggregate") or {})
    total_cycles = float(len(history) + int(agg.get("total_cycles") or 0))
    total_merged = float(
        sum(float(e.get("prs_merged", 0) or 0) for e in history)
        + float(agg.get("total_prs_merged") or 0)
    )
    if total_cycles == 0:
        return None
    return total_merged / total_cycles


VALID_CATEGORIES = {"product", "structural", "mixed"}


def _clear_direction_reactivation_state(direction: dict) -> None:
    """Drop transient status residue so active/staged transitions start clean."""
    for key in (
        "paused_reason",
        "paused_at",
        "auto_paused",
        "expired_at",
        "needs_manual_intervention",
        "needs_manual_intervention_at",
        "escalation_notification_pending",
        "escalation_notification_retries",
        "last_researched_at",
        "staged_activated_at",
        "staged_for_version",
    ):
        direction.pop(key, None)
    direction["remediation_attempts"] = 0


class _DirectionPayload(BaseModel):
    text: str = Field(..., min_length=1, max_length=DIRECTION_TEXT_MAX)
    priority: str = Field(default="medium", pattern=r"^(high|medium|low)$")
    paths: List[str] | None = None
    completion_criteria: str | None = Field(
        default=None, max_length=COMPLETION_CRITERIA_MAX
    )
    conflicts_with: List[str] | None = Field(
        default=None,
        description="Direction IDs that shouldn't run in parallel with this one",
    )
    category: str | None = Field(
        default=None,
        description="Direction category: 'product', 'structural', or 'mixed'",
    )
    cost_budget_usd: float | None = Field(
        default=None,
        ge=0,
        description="Optional cost budget ceiling in USD for this direction",
    )
    staged: bool = Field(
        default=False,
        description=(
            "Create the direction directly in the 'staged' state, gated to the "
            "current open release version, so it never passes through a "
            "research-eligible active state. Requires an open version (404 if none)."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Client-generated idempotency key. When supplied, a repeat add "
            "(double-click / network retry) carrying the same key returns the "
            "existing direction instead of creating a duplicate."
        ),
    )
    success_evidence: Optional[str] = Field(
        default=None,
        max_length=DIRECTION_CONTEXT_FIELD_MAX,
        description=(
            "Acceptance evidence: the observable signal that proves this "
            "direction succeeded. Stored under context['success_evidence'], "
            "matching the PATCH/update convention."
        ),
    )
    retirement_condition: Optional[str] = Field(
        default=None,
        max_length=DIRECTION_CONTEXT_FIELD_MAX,
        description=(
            "Retirement condition: the explicit state under which this "
            "direction may be retired. Stored under "
            "context['retirement_condition'], matching the PATCH/update "
            "convention."
        ),
    )
    gap_id: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Problem model gap ID this direction is created to address.",
    )


@router.get("/projects/{project_id}/directions")
async def get_directions(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
    lite: bool = Query(
        False,
        description=(
            "When true, omit heavy stable project-level fields (design_doc, "
            "problem_model, self_reflection_log, vision, running_update_reconciliation). "
            "Used by the 10-second poll path to reduce payload size; the initial "
            "full load always uses lite=false."
        ),
    ),
):
    from app.shared_state import get_direction_hold_info as _get_direction_hold_info
    from app.shared_state import runtime_config as _runtime_config
    from app.direction_metrics import (
        _compute_direction_quality_score,
        _compute_lifetime_generalized_outcome_state,
    )
    from app.strategic_completion import (
        _compute_churn_metrics,
        _compute_lifetime_burn_metrics,
    )
    if not lite:
        from app.running_update_merge import (
            summarize_running_update_reconciliation,
        )

    project = await _get_user_project(project_id, current_user["user_id"], session)
    directions = project.directions or []

    # Per-direction count of durable knowledge documents accumulated by
    # web_research tasks. One grouped query, scoped to this project; keyed by
    # the string direction id stamped on KnowledgeDocument.direction_id.
    from app.models import KnowledgeDocument
    from sqlalchemy import func as _sa_func

    knowledge_counts: dict[str, int] = {}
    count_rows = (
        await session.execute(
            select(KnowledgeDocument.direction_id, _sa_func.count())
            .where(
                KnowledgeDocument.project_id == project_id,
                KnowledgeDocument.direction_id.is_not(None),
            )
            .group_by(KnowledgeDocument.direction_id)
        )
    ).all()
    for direction_id, count in count_rows:
        if direction_id is not None:
            knowledge_counts[str(direction_id)] = int(count)
    churn_threshold = int(
        _runtime_config.get("direction_churn_non_merge_threshold", 15) or 15
    )
    churn_min_rate = float(
        _runtime_config.get("direction_churn_min_merge_rate", 0.10) or 0.10
    )
    lifetime_threshold = int(
        _runtime_config.get("direction_lifetime_non_merge_threshold", 150) or 150
    )
    lifetime_min_rate = float(
        _runtime_config.get("direction_lifetime_min_merge_rate", 0.10) or 0.10
    )
    enriched = []
    for d in directions:
        hold_info = _get_direction_hold_info(d.get("id", ""))
        held_at = hold_info["held_at"]
        held_until = hold_info["held_until"]
        refractory_info = get_direction_refractory_info(d)
        refractory_until = refractory_info["refractory_until"]
        refractory_reason = refractory_info["refractory_reason"]
        refractory_remaining_minutes = refractory_info["refractory_remaining_minutes"]
        generalized_state = _compute_lifetime_generalized_outcome_state(d)
        raw_churn = _compute_churn_metrics(d)
        churn_metrics = (
            {
                **raw_churn,
                "threshold": churn_threshold,
                "min_merge_rate": churn_min_rate,
            }
            if raw_churn is not None
            else None
        )
        raw_lifetime = _compute_lifetime_burn_metrics(d)
        lifetime_burn_metrics = (
            {
                **raw_lifetime,
                "threshold": lifetime_threshold,
                "min_merge_rate": lifetime_min_rate,
            }
            if raw_lifetime is not None
            else None
        )
        enriched.append(
            {
                **d,
                "merges_per_cycle": _compute_direction_merges_per_cycle(d),
                "quality_score": round(_compute_direction_quality_score(d), 3),
                "generalized_successes": generalized_state["generalized_successes"],
                "generalized_health_rate": generalized_state["generalized_health_rate"],
                "outcome_kind_breakdown": generalized_state["outcome_kind_breakdown"],
                "held": bool(hold_info["held"]),
                "held_reason": hold_info["reason"] if hold_info["held"] else None,
                "held_at": held_at.isoformat()
                if isinstance(held_at, datetime)
                else None,
                "held_until": held_until.isoformat()
                if isinstance(held_until, datetime)
                else None,
                "refractory_until": refractory_until.isoformat()
                if isinstance(refractory_until, datetime)
                else None,
                "refractory_reason": refractory_reason,
                "refractory_remaining_minutes": (
                    max(1, ceil(float(refractory_remaining_minutes)))
                    if isinstance(refractory_remaining_minutes, (int, float))
                    and refractory_remaining_minutes is not None
                    else None
                ),
                "churn_metrics": churn_metrics,
                "lifetime_burn_metrics": lifetime_burn_metrics,
                "knowledge_document_count": knowledge_counts.get(d.get("id", ""), 0),
            }
        )
    resp: dict = {
        "directions": enriched,
        "autonomous_enabled": project.autonomous_enabled,
        "running_update": project.running_update or None,
        "running_update_acknowledged_at": (
            project.running_update_acknowledged_at.isoformat()
            if project.running_update_acknowledged_at
            else None
        ),
        "is_self_repo": bool(project.is_self_repo),
        "enable_self_improvement": project.enable_self_improvement,
    }

    if not lite:
        problem_model_dict = project.problem_model or {}
        proof_problem_model = enrich_problem_model_capability_proofs(
            problem_model_dict,
            getattr(project, "capability_proof_records", None),
        )
        problem_model_size_health = None
        if problem_model_dict:
            try:
                pm = ProblemModel(**problem_model_dict)
                problem_model_size_health = compute_model_size_signal(pm)
            except Exception:  # noqa: BLE001 — best-effort enrichment; a malformed stored problem_model must not 500 the directions listing
                logger.debug(
                    "[DIRECTIONS] problem_model size-health computation skipped "
                    "(malformed stored problem_model)",
                    exc_info=True,
                )
        resp.update(
            {
                "vision": project.vision or {},
                "design_doc": project.design_doc or {},
                "problem_model": proof_problem_model,
                "problem_model_size_health": problem_model_size_health,
                "running_update_reconciliation": summarize_running_update_reconciliation(
                    project.vision
                ),
                "self_reflection_log": project.self_reflection_log or [],
            }
        )

    return resp


@router.post("/projects/{project_id}/directions")
async def add_direction(
    project_id: uuid.UUID,
    payload: _DirectionPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    if payload.category is not None and payload.category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {sorted(VALID_CATEGORIES)}",
        )

    if payload.gap_id:
        _gap_err, _open_ids = check_gap_id_write(payload.gap_id, project.problem_model)
        if _gap_err:
            raise HTTPException(
                status_code=400,
                detail={"error": "gap_link_invalid", "reason": _gap_err, "open_gap_ids": _open_ids},
            )

    from app.shared_state import runtime_config as _runtime_config

    max_active = _runtime_config.get("max_active_directions", 5)

    # Normalize idempotency key: strip whitespace, treat empty as None
    idem_key = (payload.idempotency_key or "").strip() or None

    # Atomic staged create: resolve and stamp the open version exactly the way
    # the staged PATCH branch (update_direction) does, so a next-version idea is
    # born staged in a single commit and never passes through a research-eligible
    # active state. No open version → no target to auto-activate against, so 404
    # rather than silently creating active.
    staged_for_version: int | None = None
    if payload.staged:
        from app.release_version import current_open_version

        open_version = await current_open_version(session, project.id)
        if open_version is None:
            raise HTTPException(
                status_code=404,
                detail="No open release version found for this project.",
            )
        staged_for_version = open_version.version_number

    direction = {
        "id": f"d-{uuid.uuid4().hex[:8]}",
        "text": payload.text,
        "priority": payload.priority,
        "paths": payload.paths or [],
        "added_at": _utcnow().isoformat(),
        "status": "staged" if payload.staged else "active",
    }
    if payload.staged:
        direction["staged_for_version"] = staged_for_version
        direction["staged_at"] = _utcnow().isoformat()
    if payload.category:
        direction["category"] = payload.category
    if payload.completion_criteria:
        direction["completion_criteria"] = payload.completion_criteria
    else:
        logger.warning(
            "Direction created without completion_criteria: %s", direction["id"]
        )
    if payload.cost_budget_usd is not None:
        direction["cost_budget_usd"] = float(payload.cost_budget_usd)
    if idem_key:
        direction["idempotency_key"] = idem_key
    # Acceptance-evidence contract: stamp success_evidence / retirement_condition
    # into the nested context dict exactly the way update_direction (PATCH) does,
    # so create and patch produce identical shapes. Only set keys that were
    # provided, and only attach `context` at all when at least one is present —
    # an omitted field introduces no empty context entry. Pydantic's max_length
    # already enforces the 500-char cap that PATCH checks manually.
    context: Dict[str, Any] = {}
    if payload.success_evidence is not None:
        context["success_evidence"] = payload.success_evidence
    if payload.retirement_condition is not None:
        context["retirement_condition"] = payload.retirement_condition
    if context:
        direction["context"] = context
    if payload.gap_id:
        direction["gap_id"] = payload.gap_id

    existing_direction: list = []  # Will be populated if we find a duplicate

    def _mutate_add(directions: list) -> None:
        # Idempotency: if this add carries an idempotency key and a direction
        # with that key already exists, return the existing direction instead of
        # creating a duplicate.
        if idem_key:
            for d in directions:
                if d.get("idempotency_key") == idem_key:
                    existing_direction.append(d)
                    return
        # The active-direction cap counts active directions only; a staged
        # create is inert until its version promotes, so it does not consume a
        # slot (mirrors update_direction, where the cap fires only on → active).
        if not payload.staged:
            active_count = sum(1 for d in directions if d.get("status") == "active")
            if active_count >= max_active:
                raise HTTPException(
                    status_code=409,
                    detail=f"Direction cap reached: max {max_active} active directions allowed",
                )
        if payload.conflicts_with:
            existing_ids = {d.get("id") for d in directions}
            invalid = [cid for cid in payload.conflicts_with if cid not in existing_ids]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown direction IDs in conflicts_with: {invalid}",
                )
            direction["conflicts_with"] = payload.conflicts_with
        directions.append(direction)

    applied = await with_directions_locked(
        session, project.id, _mutate_add, skip_locked=True
    )
    if not applied:
        raise HTTPException(
            status_code=429,
            detail="Project row locked by concurrent write, retry",
            headers={"Retry-After": "2"},
        )
    await session.commit()
    await emit_strategic_updated(project.id, STRATEGIC_KIND_DIRECTIONS)

    # Return existing direction if we found one (idempotent duplicate)
    if existing_direction:
        return {"direction": existing_direction[0], "status": "duplicate"}
    return {"direction": direction}


@router.put("/projects/{project_id}/directions")
async def replace_directions(
    project_id: uuid.UUID,
    payload: List[Dict[str, Any]],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Replace the entire directions array for a project."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    normalized = []
    for d in payload:
        entry = dict(d)
        if not entry.get("id"):
            entry["id"] = f"d-{uuid.uuid4().hex[:8]}"
        if not entry.get("added_at"):
            entry["added_at"] = _utcnow().isoformat()
        if entry.get("priority") not in ("high", "medium", "low"):
            raise HTTPException(
                status_code=400, detail=f"Invalid priority: {entry.get('priority')}"
            )
        if not isinstance(entry.get("text"), str) or not entry["text"].strip():
            raise HTTPException(
                status_code=400, detail="Each direction must have non-empty text"
            )
        if len(entry["text"]) > DIRECTION_TEXT_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"Direction text must not exceed {DIRECTION_TEXT_MAX} characters",
            )
        if "completion_criteria" in entry:
            cc = entry["completion_criteria"]
            if cc is not None and not isinstance(cc, str):
                raise HTTPException(
                    status_code=400,
                    detail="completion_criteria must be a string or null",
                )
            if cc is not None and len(cc) > COMPLETION_CRITERIA_MAX:
                raise HTTPException(
                    status_code=400,
                    detail=f"completion_criteria must not exceed {COMPLETION_CRITERIA_MAX} characters",
                )
        if (
            "category" in entry
            and entry["category"] is not None
            and entry["category"] not in VALID_CATEGORIES
        ):
            raise HTTPException(
                status_code=400,
                detail=f"category must be one of: {sorted(VALID_CATEGORIES)}",
            )
        if "cost_budget_usd" in entry and entry["cost_budget_usd"] is not None:
            try:
                entry["cost_budget_usd"] = float(entry["cost_budget_usd"])
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="cost_budget_usd must be a number or null"
                ) from None
            if entry["cost_budget_usd"] < 0:
                raise HTTPException(
                    status_code=400, detail="cost_budget_usd must be non-negative"
                )
        if "status" not in entry:
            entry["status"] = "active"
        normalized.append(entry)

    applied = await with_directions_locked(
        session, project.id, lambda _: normalized, skip_locked=True
    )
    if not applied:
        raise HTTPException(
            status_code=429,
            detail="Project row locked by concurrent write, retry",
            headers={"Retry-After": "2"},
        )
    await session.commit()
    await emit_strategic_updated(project.id, STRATEGIC_KIND_DIRECTIONS)
    return {"directions": normalized}


@router.delete("/projects/{project_id}/directions/{direction_id}")
async def remove_direction(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    def _mutate_remove(directions: list) -> list:
        removed = [d for d in directions if d.get("id") == direction_id]
        remaining = [d for d in directions if d.get("id") != direction_id]
        if len(remaining) == len(directions):
            raise HTTPException(status_code=404, detail="Direction not found")

        # If this was a proposed or staged direction, keep a tombstone to prevent
        # re-proposal / re-staging.
        if removed and removed[0].get("status") in {"proposed", "staged"}:
            from datetime import datetime, timezone

            rejected_text = (removed[0].get("text") or "")[:200]
            if rejected_text.strip():
                tombstone = {
                    "id": removed[0]["id"],
                    "text": rejected_text,
                    "status": "rejected",
                    "rejected_at": datetime.now(timezone.utc)
                    .replace(tzinfo=None)
                    .isoformat(),
                }
                rejected = [d for d in remaining if d.get("status") == "rejected"]
                non_rejected = [d for d in remaining if d.get("status") != "rejected"]
                rejected.append(tombstone)
                rejected = rejected[-20:]
                remaining = non_rejected + rejected
        return remaining

    applied = await with_directions_locked(
        session, project.id, _mutate_remove, skip_locked=True
    )
    if not applied:
        raise HTTPException(
            status_code=429,
            detail="Project row locked by concurrent write, retry",
            headers={"Retry-After": "2"},
        )

    # Cancel QUEUED tasks spawned for this direction (same transaction, lock held)
    cancelled_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status == TaskStatus.QUEUED,
            Task.context["direction_id"].as_string() == direction_id,
        )
    )
    cancelled_tasks = cancelled_result.scalars().all()
    vm_ids_to_delete = []
    for t in cancelled_tasks:
        t.status = TaskStatus.CANCELLED
        t.latest_status_message = f"Cancelled: direction {direction_id} was removed"
        session.add(t)
        if t.vm_instance_id:
            vm_ids_to_delete.append(t.vm_instance_id)

    await session.commit()
    await emit_strategic_updated(project.id, STRATEGIC_KIND_DIRECTIONS)

    # Delete VMs for cancelled tasks (after commit)
    for vm_id in vm_ids_to_delete:
        from app.vm_lifecycle import _ensure_vm_deleted

        await _ensure_vm_deleted(vm_id, reason="direction_removed")

    return {"message": "Direction removed", "cancelled_tasks": len(cancelled_tasks)}


@router.patch("/projects/{project_id}/directions/reorder")
async def reorder_directions(
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    direction_ids = payload.get("direction_ids")
    if not isinstance(direction_ids, list):
        raise HTTPException(status_code=400, detail="direction_ids must be a list")

    reordered: list = []

    def _mutate_reorder(directions: list) -> list:
        existing_ids = {d.get("id") for d in directions}
        provided_ids = set(direction_ids)
        if existing_ids != provided_ids:
            raise HTTPException(
                status_code=400,
                detail="direction_ids must match existing direction IDs exactly",
            )
        directions_by_id = {d.get("id"): d for d in directions}
        ordered = [directions_by_id[did] for did in direction_ids]
        reordered[:] = ordered
        return ordered

    applied = await with_directions_locked(
        session, project.id, _mutate_reorder, skip_locked=True
    )
    if not applied:
        raise HTTPException(
            status_code=429,
            detail="Project row locked by concurrent write, retry",
            headers={"Retry-After": "2"},
        )
    await session.commit()
    await emit_strategic_updated(project.id, STRATEGIC_KIND_DIRECTIONS)
    return {"directions": reordered}


@router.patch("/projects/{project_id}/directions/{direction_id}")
async def update_direction(
    project_id: uuid.UUID,
    direction_id: str,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Validate inputs (pure payload checks that don't need locked state)
    if "priority" in payload and payload["priority"] not in ("high", "medium", "low"):
        raise HTTPException(
            status_code=400, detail="priority must be high, medium, or low"
        )
    if "status" in payload and payload["status"] not in (
        "active",
        "completed",
        "staged",
        "paused",
        "proposed",
        "expired",
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "status must be active, completed, staged, paused, proposed, or expired"
            ),
        )
    if "text" in payload and (
        not isinstance(payload["text"], str)
        or len(payload["text"]) > DIRECTION_TEXT_MAX
        or len(payload["text"]) < 1
    ):
        raise HTTPException(
            status_code=400, detail=f"text must be 1-{DIRECTION_TEXT_MAX} characters"
        )
    if "paths" in payload and (
        not isinstance(payload["paths"], list)
        or not all(isinstance(p, str) for p in payload["paths"])
    ):
        raise HTTPException(status_code=400, detail="paths must be a list of strings")
    if (
        "completion_criteria" in payload
        and payload["completion_criteria"] is not None
        and (
            not isinstance(payload["completion_criteria"], str)
            or len(payload["completion_criteria"]) > COMPLETION_CRITERIA_MAX
        )
    ):
        raise HTTPException(
            status_code=400,
            detail=f"completion_criteria must be a string of at most {COMPLETION_CRITERIA_MAX} characters",
        )
    if (
        "conflicts_with" in payload
        and payload["conflicts_with"] is not None
        and (
            not isinstance(payload["conflicts_with"], list)
            or not all(isinstance(c, str) for c in payload["conflicts_with"])
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="conflicts_with must be a list of direction ID strings",
        )
    if (
        "category" in payload
        and payload["category"] is not None
        and payload["category"] not in VALID_CATEGORIES
    ):
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {sorted(VALID_CATEGORIES)}",
        )
    if "cost_budget_usd" in payload and payload["cost_budget_usd"] is not None:
        if not isinstance(payload["cost_budget_usd"], (int, float)) or isinstance(
            payload["cost_budget_usd"], bool
        ):
            raise HTTPException(
                status_code=400, detail="cost_budget_usd must be a number or null"
            )
        if payload["cost_budget_usd"] < 0:
            raise HTTPException(
                status_code=400, detail="cost_budget_usd must be non-negative"
            )
    if "refractory_until" in payload and payload["refractory_until"] is not None:
        if not isinstance(payload["refractory_until"], str):
            raise HTTPException(
                status_code=400,
                detail="refractory_until must be an ISO timestamp or null",
            )
        try:
            datetime.fromisoformat(payload["refractory_until"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="refractory_until must be an ISO timestamp or null",
            ) from None
    if (
        "success_evidence" in payload
        and payload["success_evidence"] is not None
        and (
            not isinstance(payload["success_evidence"], str)
            or len(payload["success_evidence"]) > DIRECTION_CONTEXT_FIELD_MAX
        )
    ):
        raise HTTPException(
            status_code=400,
            detail=f"success_evidence must be a string of at most {DIRECTION_CONTEXT_FIELD_MAX} characters",
        )
    if (
        "retirement_condition" in payload
        and payload["retirement_condition"] is not None
        and (
            not isinstance(payload["retirement_condition"], str)
            or len(payload["retirement_condition"]) > DIRECTION_CONTEXT_FIELD_MAX
        )
    ):
        raise HTTPException(
            status_code=400,
            detail=f"retirement_condition must be a string of at most {DIRECTION_CONTEXT_FIELD_MAX} characters",
        )
    from app.strategic_completion import (
        _VALID_RECOMMENDED_MODES,
        _normalize_recommended_mode,
        _reset_scaffolding_state_on_outcome_change,
    )

    if (
        "recommended_mode" in payload
        and payload["recommended_mode"] is not None
        and _normalize_recommended_mode(payload["recommended_mode"]) is None
    ):
        raise HTTPException(
            status_code=400,
            detail=f"recommended_mode must be one of: {sorted(_VALID_RECOMMENDED_MODES)}",
        )
    if (
        "system_id" in payload
        and payload["system_id"] is not None
        and (
            not isinstance(payload["system_id"], str) or len(payload["system_id"]) > 100
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="system_id must be a string of at most 100 characters",
        )
    if (
        "gap_id" in payload
        and payload["gap_id"] is not None
        and (
            not isinstance(payload["gap_id"], str) or len(payload["gap_id"]) > 100
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="gap_id must be a string of at most 100 characters",
        )
    if "gap_id" in payload and payload["gap_id"] is not None:
        _gap_err, _open_ids = check_gap_id_write(payload["gap_id"], project.problem_model)
        if _gap_err:
            raise HTTPException(
                status_code=400,
                detail={"error": "gap_link_invalid", "reason": _gap_err, "open_gap_ids": _open_ids},
            )

    # Resolve and validate the staging TARGET version. Triggered both when a
    # direction transitions to ``staged`` AND when an already-staged direction is
    # retargeted to a different version (an explicit ``staged_for_version`` in the
    # payload without a status change). The target defaults to the current open
    # version and must be >= it: a version that has already promoted/closed would
    # auto-activate the direction on the next sweep, which is nonsensical.
    staged_for_version: int | None = None
    _wants_staging = (
        payload.get("status") == "staged" or "staged_for_version" in payload
    )
    if _wants_staging:
        from app.release_version import current_open_version

        open_version = await current_open_version(session, project.id)
        if open_version is None:
            raise HTTPException(
                status_code=404,
                detail="No open release version found for this project.",
            )
        target = payload.get("staged_for_version")
        if target is None:
            staged_for_version = open_version.version_number
        elif not isinstance(target, int) or isinstance(target, bool):
            raise HTTPException(
                status_code=400,
                detail="staged_for_version must be an integer version number",
            )
        elif target < open_version.version_number:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"staged_for_version {target} has already promoted; choose the "
                    f"current open version ({open_version.version_number}) or later"
                ),
            )
        else:
            staged_for_version = target

    updated_direction: list = []  # [d] populated by mutate_fn

    def _mutate_update(directions: list) -> None:
        # Cap check (needs locked data): if reactivating, ensure cap won't be exceeded
        if payload.get("status") == "active":
            from app.shared_state import runtime_config as _runtime_config

            max_active = _runtime_config.get("max_active_directions", 5)
            active_count = sum(
                1
                for d in directions
                if d.get("status") == "active" and d.get("id") != direction_id
            )
            if active_count >= max_active:
                raise HTTPException(
                    status_code=409,
                    detail=f"Direction cap reached: max {max_active} active directions allowed",
                )

        # conflicts_with existence check (needs locked data)
        if "conflicts_with" in payload and payload["conflicts_with"] is not None:
            existing_ids = {
                d.get("id") for d in directions if d.get("id") != direction_id
            }
            invalid = [
                cid for cid in payload["conflicts_with"] if cid not in existing_ids
            ]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown direction IDs in conflicts_with: {invalid}",
                )

        for d in directions:
            if d.get("id") == direction_id:
                # Snapshot contract fields before mutation to detect real changes.
                _pre_text = d.get("text")
                _pre_paths = list(d.get("paths") or [])
                _pre_cc = d.get("completion_criteria")
                _pre_ctx = d.get("context") or {}
                _pre_se = _pre_ctx.get("success_evidence")
                _pre_rc = _pre_ctx.get("retirement_condition")

                if "priority" in payload:
                    d["priority"] = payload["priority"]
                if "status" in payload:
                    d["status"] = payload["status"]
                    if payload["status"] in ("active", "staged"):
                        _clear_direction_reactivation_state(d)
                    if payload["status"] == "staged":
                        d["staged_for_version"] = staged_for_version
                        d["staged_at"] = _utcnow().isoformat()
                    # When reviving from expired back to proposed, clear expired_at
                    if payload["status"] == "proposed":
                        d.pop("expired_at", None)
                # Retarget an already-staged direction to a different version
                # without re-staging it (no status change). Only a currently
                # staged direction can be retargeted; the resolved/validated
                # target replaces its marker so the strategist + activation sweep
                # bind it to the new version.
                if (
                    "staged_for_version" in payload
                    and payload.get("status") != "staged"
                ):
                    if d.get("status") != "staged":
                        raise HTTPException(
                            status_code=409,
                            detail="Only a staged direction can be retargeted to a different version",
                        )
                    d["staged_for_version"] = staged_for_version
                if "text" in payload:
                    d["text"] = payload["text"]
                if "paths" in payload:
                    d["paths"] = payload["paths"]
                if "pending_update" in payload:
                    d["pending_update"] = payload["pending_update"]
                if "research_context" in payload:
                    if payload["research_context"] is None:
                        d.pop("research_context", None)
                    elif isinstance(payload["research_context"], str):
                        d["research_context"] = payload["research_context"]
                        # Re-evaluation of research context indicates the direction was reviewed —
                        # clear any existing hold so new tasks can dispatch again.
                        import app.shared_state as _ss

                        _ss.clear_direction_hold(direction_id)
                if "research_history" in payload and (
                    payload["research_history"] is None
                    or (
                        isinstance(payload["research_history"], list)
                        and len(payload["research_history"]) == 0
                    )
                ):
                    d["research_history"] = []
                if "notes" in payload and isinstance(payload["notes"], list):
                    d["notes"] = payload["notes"]
                if "completion_criteria" in payload:
                    if payload["completion_criteria"] is None:
                        d.pop("completion_criteria", None)
                    elif isinstance(payload["completion_criteria"], str):
                        d["completion_criteria"] = payload["completion_criteria"]
                if "conflicts_with" in payload:
                    if payload["conflicts_with"] is None or (
                        isinstance(payload["conflicts_with"], list)
                        and len(payload["conflicts_with"]) == 0
                    ):
                        d.pop("conflicts_with", None)
                    elif isinstance(payload["conflicts_with"], list):
                        d["conflicts_with"] = payload["conflicts_with"]
                if "category" in payload:
                    if payload["category"] is None:
                        d.pop("category", None)
                    elif payload["category"] in VALID_CATEGORIES:
                        d["category"] = payload["category"]
                if "cost_budget_usd" in payload:
                    if payload["cost_budget_usd"] is None:
                        d.pop("cost_budget_usd", None)
                    else:
                        d["cost_budget_usd"] = float(payload["cost_budget_usd"])
                if "refractory_until" in payload:
                    if payload["refractory_until"] is None:
                        d.pop("refractory_until", None)
                    else:
                        d["refractory_until"] = payload["refractory_until"]
                if "success_evidence" in payload or "retirement_condition" in payload:
                    context = d.get("context") or {}
                    _old_se = context.get("success_evidence")
                    _old_rc = context.get("retirement_condition")
                    if "success_evidence" in payload:
                        if payload["success_evidence"] is None:
                            context.pop("success_evidence", None)
                        else:
                            context["success_evidence"] = payload["success_evidence"]
                    if "retirement_condition" in payload:
                        if payload["retirement_condition"] is None:
                            context.pop("retirement_condition", None)
                        else:
                            context["retirement_condition"] = payload[
                                "retirement_condition"
                            ]
                    if (
                        context.get("success_evidence") != _old_se
                        or context.get("retirement_condition") != _old_rc
                    ):
                        _reset_scaffolding_state_on_outcome_change(d)
                    d["context"] = context
                if "recommended_mode" in payload:
                    if payload["recommended_mode"] is None:
                        d.pop("recommended_mode", None)
                        d.pop(RECOMMENDED_MODE_META_KEY, None)
                    else:
                        mode = _normalize_recommended_mode(payload["recommended_mode"])
                        d["recommended_mode"] = mode
                        d[RECOMMENDED_MODE_META_KEY] = {
                            "mode": mode,
                            "source": "operator",
                            "set_at": _utcnow().isoformat(),
                        }
                if "system_id" in payload:
                    if payload["system_id"] is None:
                        d.pop("system_id", None)
                    else:
                        d["system_id"] = payload["system_id"][:100]
                if "gap_id" in payload:
                    if payload["gap_id"] is None:
                        d.pop("gap_id", None)
                    else:
                        d["gap_id"] = payload["gap_id"]

                # Reset outcome-detection state when any contract field actually changed.
                _new_ctx = d.get("context") or {}
                if (
                    d.get("text") != _pre_text
                    or list(d.get("paths") or []) != _pre_paths
                    or d.get("completion_criteria") != _pre_cc
                    or _new_ctx.get("success_evidence") != _pre_se
                    or _new_ctx.get("retirement_condition") != _pre_rc
                ):
                    _reset_outcome_detection_state(d)

                updated_direction.append(d)
                return
        raise HTTPException(status_code=404, detail="Direction not found")

    applied = await with_directions_locked(
        session, project.id, _mutate_update, skip_locked=True
    )
    if not applied:
        raise HTTPException(
            status_code=429,
            detail="Project row locked by concurrent write, retry",
            headers={"Retry-After": "2"},
        )
    await session.commit()
    await emit_strategic_updated(project.id, STRATEGIC_KIND_DIRECTIONS)
    return {"direction": updated_direction[0]}


@router.post("/projects/{project_id}/directions/{direction_id}/research")
async def create_direction_research_task(
    project_id: uuid.UUID,
    direction_id: str,
    bypass_cooldown: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    direction = next(
        (d for d in (project.directions or []) if d.get("id") == direction_id), None
    )
    if not direction:
        raise HTTPException(status_code=404, detail="Direction not found")

    from app.shared_state import (
        get_project_integration_branch as _get_project_integration_branch,
    )
    from app.autonomous_research import research_direction_claim_key

    # Serialize same-direction spawns before the in-flight scan so a
    # concurrently-committing pipelined research task can never slip between
    # the read guard and this handler's insert under a different claim key.
    await acquire_direction_research_spawn_lock(session, project.id, direction_id)

    # In-flight guard: never spawn a second research on a direction that already
    # has one in progress. Two concurrent research cycles produce two rollup PRs
    # over the same files, guaranteed to conflict once one merges (mirrors the
    # autonomous dedup in autonomous_research.py; incident 2026-06-03 on
    # d-5d841cea). Unconditional — bypass_cooldown overrides only the recency
    # cooldown, not concurrency. Match by current_direction_id, falling back to
    # the legacy single-direction payload shape for any in-flight task spawned
    # before this endpoint started stamping current_direction_id.
    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        TaskStatus.BLOCKED,
        TaskStatus.PARKED,
    ]

    # Fast path: modern research tasks all carry the direction claim key, so a
    # direct lookup avoids scanning unrelated active tasks in the common case.
    claim_key = research_direction_claim_key(project.id, direction_id)
    existing_claim = (
        await session.execute(
            select(Task)
            .where(
                Task.project_id == project.id,
                Task.idempotency_key == claim_key,
                Task.status.in_(active_statuses),
            )
            .options(load_only(Task.id, Task.status, raiseload=True))
        )
    ).scalar_one_or_none()
    if existing_claim is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Research is already in progress on this direction "
                f"(task {str(existing_claim.id)[:8]}, status {existing_claim.status}). "
                f"Cancel it before starting another."
            ),
        )

    active_research = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project.id,
                    Task.status.in_(active_statuses),
                    Task.payloads[0]["type"].as_string() == "research",
                )
                .options(load_only(Task.id, Task.payloads, Task.status, raiseload=True))
            )
        )
        .scalars()
        .all()
    )
    for rt in active_research:
        rp = rt.first_payload
        if rp.get("type") != "research":
            continue
        rt_direction_id = rp.get("current_direction_id") or next(
            (
                d.get("id")
                for d in (rp.get("directions") or [])
                if isinstance(d, dict) and d.get("id")
            ),
            None,
        )
        if rt_direction_id == direction_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Research is already in progress on this direction "
                    f"(task {str(rt.id)[:8]}, status {rt.status}). "
                    f"Cancel it before starting another."
                ),
            )

    if not bypass_cooldown:
        refractory_info = get_direction_refractory_info(direction)
        if refractory_info["refractory_remaining_minutes"] is not None:
            remaining_minutes = max(
                1, ceil(float(refractory_info["refractory_remaining_minutes"]))
            )
            reason = refractory_info["refractory_reason"] or "standard"
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Direction is in {reason} hold for {remaining_minutes} more minutes. "
                    "Use bypass_cooldown=true to force."
                ),
            )

    direction_text = direction.get("text", "")
    description = direction_text
    if bypass_cooldown:
        description = f"[hold bypassed manually] {direction_text}"

    vm_name = f"agent-vm-{uuid.uuid4().hex}"
    task_payload = validate_internal_payload(
        {
            "type": "research",
            "directions": [direction],
            # Canonical direction key used by the autonomous dedup
            # (autonomous_research.py) and completion attribution
            # (research_completion.py). Without it, a manually-spawned research
            # was invisible to the autonomous busy-direction check, so the
            # autonomous loop could spawn a concurrent duplicate on the same
            # direction.
            "current_direction_id": direction_id,
            "base_branch": _get_project_integration_branch(project),
            "is_self_repo": bool(project.is_self_repo),
            "enable_self_improvement": project.enable_self_improvement,
        },
        "directions:research",
    )
    # Atomic per-direction claim — the SAME key the autonomous loop stamps, so
    # a manual spawn and a concurrent autonomous tick (or a double-click) can
    # never BOTH create an active research cycle on this direction. The in-app
    # scan above is only the friendly fast path; the partial unique index
    # ux_task_active_idempotency_key is the concurrency-safe backstop.
    idempotency_key = research_direction_claim_key(project.id, direction_id)
    new_task = Task(
        project_id=project.id,
        title=f"Research: {direction_text[:200]}",
        description=description,
        payloads=[task_payload],
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="USER",
        requires_approval=False,
        idempotency_key=idempotency_key,
    )
    session.add(new_task)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent spawn (another manual request or the autonomous loop) won
        # the active idempotency slot for this direction between our scan and
        # this insert. Coalesce onto the in-flight research rather than spawning
        # a second VM-backed cycle or erroring — mirrors trigger_self_audit.
        await session.rollback()
        existing = (
            (
                await session.execute(
                    select(Task).where(
                        Task.idempotency_key == idempotency_key,
                        Task.status.in_(
                            [
                                TaskStatus.QUEUED,
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                                TaskStatus.BLOCKED,
                                TaskStatus.PARKED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .first()
        )
        detail = (
            f"Research is already in progress on this direction "
            f"(task {str(existing.id)[:8]}, status {existing.status}). "
            f"Cancel it before starting another."
            if existing is not None
            else "Research is already in progress on this direction."
        )
        raise HTTPException(status_code=409, detail=detail)
    await session.refresh(new_task)

    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={
            "created_by": "USER",
            "method": "direction_research",
            "direction_id": direction_id,
            "bypass_cooldown": bypass_cooldown,
        },
    )
    session.add(history)
    await session.commit()

    return {
        "id": str(new_task.id),
        "project_id": str(project.id),
        "title": new_task.title,
        "status": new_task.status,
        "vm_instance_id": new_task.vm_instance_id,
    }


@router.post("/projects/{project_id}/directions/{direction_id}/unhold")
async def unhold_direction(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Clear a direction's consecutive-failure hold so new tasks dispatch again.

    Returns was_held=True if a hold was actually in place, False if the
    direction was already healthy (idempotent).
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    direction = next(
        (d for d in (project.directions or []) if d.get("id") == direction_id), None
    )
    if not direction:
        raise HTTPException(status_code=404, detail="Direction not found")

    import app.shared_state as _ss

    was_held, _ = _ss.is_direction_held(direction_id)
    _ss.clear_direction_hold(direction_id)
    logger.info(
        f"[DIRECTION] Hold cleared for {direction_id} by {current_user.get('email', current_user['user_id'])} "
        f"(was_held={was_held})"
    )
    return {"status": "unheld", "direction_id": direction_id, "was_held": was_held}


@router.post("/projects/{project_id}/strategic-cycle")
async def trigger_strategic_cycle(
    project_id: uuid.UUID,
    phase: str = "design_doc",
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger a strategic cycle.

    phase 'design_doc' (Phase 1, chains to Phase 2) or 'strategic_review'
    (Phase 2 only).  Delegates to the SAME spawn functions the autonomous
    research loop uses (_spawn_design_doc_update / _spawn_strategic_review), so a
    manual trigger is identical to a standard cycle -- there is no parallel
    inline payload builder that can drift out of sync (e.g. silently omitting
    the version-phase block / v1 bootstrap).
    """
    if phase not in ("design_doc", "strategic_review"):
        raise HTTPException(
            status_code=400, detail="Phase must be 'design_doc' or 'strategic_review'"
        )
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Lock the project row so the spawn function's _has_active_strategic_task
    # check + insert is atomic against concurrent spawns (design-doc completion
    # chain, autonomous tick, other manual triggers).  Phase A1: skip_locked +
    # 429 on contention so a 30s lock_timeout can't 500 -> instance quarantine.
    await lock_project_or_429(session, project.id)

    # One code path with the autonomous spawns -- no drift possible.
    from app.strategic_spawning import (
        _spawn_design_doc_update,
        _spawn_strategic_review,
    )

    if phase == "design_doc":
        spawned = await _spawn_design_doc_update(project, session)
    else:
        spawned = await _spawn_strategic_review(project, session)

    if not spawned:
        raise HTTPException(
            status_code=409,
            detail=(
                "A strategic cycle (design_doc_update or strategic_review) is "
                "already active for this project."
            ),
        )

    # The spawn function created and committed the task; surface the newest
    # task so callers (ops.py) can print its id.
    new_task = (
        (
            await session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    return {
        "id": str(new_task.id) if new_task else None,
        "task_id": str(new_task.id) if new_task else None,
        "project_id": str(project.id),
        "title": new_task.title if new_task else None,
        "status": new_task.status if new_task else None,
        "phase": phase,
    }


# ── Messages ─────────────────────────────────────────────────────────────


class _CreateMessage(BaseModel):
    content: str
    message_type: str = "NOTE"  # NOTE, FEEDBACK, BUG_REPORT, QUESTION, BLOCKER
    workbench_context: Dict[str, Any] | None = None
    # Client-generated idempotency key. When supplied, a repeat send (double
    # submit / network retry of the same steering instruction) converges to the
    # single already-stored message and a single arming, mirroring the live-chat
    # endpoint (admin_notifications.create_message).
    idempotency_key: str | None = None


@router.post("/projects/{project_id}/messages")
async def create_message(
    project_id: str,
    payload: _CreateMessage,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(get_current_user),
):
    """Create a message from the user to the autonomous system."""
    project = await _get_user_project(project_id, user["user_id"], session)
    result = await create_operator_project_message(
        session,
        project=project,
        user_id=user["user_id"],
        message_type=parse_operator_message_type(
            payload.message_type,
            default_note_for_empty=True,
        ),
        content=payload.content,
        context=None,
        workbench_context=payload.workbench_context,
        idempotency_key=payload.idempotency_key,
        enforce_raw_content_max=True,
    )
    if result.message_event is not None:
        await broadcast_to_project(
            str(project.id),
            "message_created",
            result.message_event,
        )

    return {"id": result.message_id, "status": result.status, "arming": result.arming}


@router.get("/projects/{project_id}/messages")
async def list_messages(
    project_id: str,
    status: str | None = "open",
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(get_current_user),
):
    """List messages for a project."""
    project = await _get_user_project(project_id, user["user_id"], session)
    stmt = select(ProjectMessage).where(ProjectMessage.project_id == project.id)
    if status:
        stmt = stmt.where(ProjectMessage.status == status)
    stmt = stmt.order_by(ProjectMessage.created_at.desc()).limit(50)
    result = await session.execute(stmt)
    msgs = result.scalars().all()
    return [legacy_direction_message_dict(m) for m in msgs]
