"""Direction management endpoints for project autonomous improvement steering."""
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import get_session
from app.models import Task, TaskStatus, TaskHistory, Project, ProjectMessage, MessageType, _utcnow
from app.auth import get_current_user
from app.admin import _get_user_project, _get_user_project_with_role
from app.payload_schemas import validate_internal_payload

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["directions"],
    dependencies=[Depends(get_current_user)]
)


DIRECTION_TEXT_MAX = 2000
COMPLETION_CRITERIA_MAX = 500


def _compute_direction_merge_rate(d: dict) -> float:
    """Return merged-PRs-per-cycle for a direction. Returns 0.5 (neutral) if never researched."""
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
    if total_cycles == 0:
        return 0.5
    return total_merged / total_cycles


VALID_CATEGORIES = {"product", "structural", "mixed"}


class DirectionPayload(BaseModel):
    text: str = Field(..., min_length=1, max_length=DIRECTION_TEXT_MAX)
    priority: str = Field(default="medium", pattern=r"^(high|medium|low)$")
    paths: Optional[List[str]] = None
    completion_criteria: Optional[str] = Field(default=None, max_length=COMPLETION_CRITERIA_MAX)
    conflicts_with: Optional[List[str]] = Field(default=None, description="Direction IDs that shouldn't run in parallel with this one")
    category: Optional[str] = Field(default=None, description="Direction category: 'product', 'structural', or 'mixed'")
    cost_budget_usd: Optional[float] = Field(default=None, ge=0, description="Optional cost budget ceiling in USD for this direction")


@router.get("/projects/{project_id}/directions")
async def get_directions(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project(project_id, current_user["user_id"], session)
    directions = project.directions or []
    directions_with_rate = [
        {**d, "merge_rate": _compute_direction_merge_rate(d)} for d in directions
    ]
    return {
        "directions": directions_with_rate,
        "autonomous_enabled": project.autonomous_enabled,
        "vision": project.vision or {},
        "design_doc": project.design_doc or {},
        "problem_model": project.problem_model or {},
        "running_update": project.running_update or None,
        "running_update_acknowledged_at": (
            project.running_update_acknowledged_at.isoformat()
            if project.running_update_acknowledged_at else None
        ),
        "self_reflection_log": project.self_reflection_log or [],
        "is_self_repo": bool(project.is_self_repo),
    }


@router.post("/projects/{project_id}/directions")
async def add_direction(
    project_id: uuid.UUID,
    payload: DirectionPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    if payload.category is not None and payload.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of: {sorted(VALID_CATEGORIES)}")

    from app.shared_state import runtime_config as _runtime_config
    max_active = _runtime_config.get("max_active_directions", 5)
    active_count = sum(1 for d in (project.directions or []) if d.get("status") == "active")
    if active_count >= max_active:
        raise HTTPException(
            status_code=409,
            detail=f"Direction cap reached: max {max_active} active directions allowed"
        )

    direction = {
        "id": f"d-{uuid.uuid4().hex[:8]}",
        "text": payload.text,
        "priority": payload.priority,
        "paths": payload.paths or [],
        "added_at": _utcnow().isoformat(),
        "status": "active",
    }
    if payload.category:
        direction["category"] = payload.category
    if payload.completion_criteria:
        direction["completion_criteria"] = payload.completion_criteria
    else:
        logger.warning("Direction created without completion_criteria: %s", direction["id"])
    if payload.cost_budget_usd is not None:
        direction["cost_budget_usd"] = float(payload.cost_budget_usd)
    if payload.conflicts_with:
        # Validate referenced IDs exist
        existing_ids = {d.get("id") for d in (project.directions or [])}
        invalid = [cid for cid in payload.conflicts_with if cid not in existing_ids]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Unknown direction IDs in conflicts_with: {invalid}")
        direction["conflicts_with"] = payload.conflicts_with

    directions = list(project.directions or [])
    directions.append(direction)
    project.directions = directions
    session.add(project)
    await session.commit()

    return {"direction": direction}


@router.put("/projects/{project_id}/directions")
async def replace_directions(
    project_id: uuid.UUID,
    payload: List[Dict[str, Any]],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Replace the entire directions array for a project."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    normalized = []
    for d in payload:
        entry = dict(d)
        if not entry.get("id"):
            entry["id"] = f"d-{uuid.uuid4().hex[:8]}"
        if not entry.get("added_at"):
            entry["added_at"] = _utcnow().isoformat()
        if entry.get("priority") not in ("high", "medium", "low"):
            raise HTTPException(status_code=400, detail=f"Invalid priority: {entry.get('priority')}")
        if not isinstance(entry.get("text"), str) or not entry["text"].strip():
            raise HTTPException(status_code=400, detail="Each direction must have non-empty text")
        if len(entry["text"]) > DIRECTION_TEXT_MAX:
            raise HTTPException(status_code=400, detail=f"Direction text must not exceed {DIRECTION_TEXT_MAX} characters")
        if "completion_criteria" in entry:
            cc = entry["completion_criteria"]
            if cc is not None and not isinstance(cc, str):
                raise HTTPException(status_code=400, detail="completion_criteria must be a string or null")
            if cc is not None and len(cc) > COMPLETION_CRITERIA_MAX:
                raise HTTPException(status_code=400, detail=f"completion_criteria must not exceed {COMPLETION_CRITERIA_MAX} characters")
        if "category" in entry and entry["category"] is not None and entry["category"] not in VALID_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"category must be one of: {sorted(VALID_CATEGORIES)}")
        if "cost_budget_usd" in entry and entry["cost_budget_usd"] is not None:
            try:
                entry["cost_budget_usd"] = float(entry["cost_budget_usd"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="cost_budget_usd must be a number or null")
            if entry["cost_budget_usd"] < 0:
                raise HTTPException(status_code=400, detail="cost_budget_usd must be non-negative")
        if "status" not in entry:
            entry["status"] = "active"
        normalized.append(entry)

    project.directions = normalized
    flag_modified(project, "directions")
    session.add(project)
    await session.commit()
    return {"directions": normalized}


@router.delete("/projects/{project_id}/directions/{direction_id}")
async def remove_direction(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    original_directions = list(project.directions or [])
    removed = [d for d in original_directions if d.get("id") == direction_id]
    directions = [d for d in original_directions if d.get("id") != direction_id]
    if len(directions) == len(original_directions):
        raise HTTPException(status_code=404, detail="Direction not found")

    # If this was a proposed direction, keep a tombstone (status='rejected') to prevent re-proposal
    if removed and removed[0].get("status") == "proposed":
        from datetime import datetime, timezone
        rejected_text = (removed[0].get("text") or "")[:200]
        if rejected_text.strip():
            tombstone = {
                "id": removed[0]["id"],
                "text": rejected_text,
                "status": "rejected",
                "rejected_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            }
            # Append tombstone and cap to 20 most-recent rejected entries
            rejected = [d for d in directions if d.get("status") == "rejected"]
            non_rejected = [d for d in directions if d.get("status") != "rejected"]
            rejected.append(tombstone)
            rejected = rejected[-20:]
            directions = non_rejected + rejected

    # Cancel QUEUED tasks spawned for this direction
    cancelled_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status == TaskStatus.QUEUED,
            Task.context["direction_id"].as_string() == direction_id
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

    project.directions = directions
    session.add(project)
    await session.commit()

    # Delete VMs for cancelled tasks (after commit)
    for vm_id in vm_ids_to_delete:
        from app.main import _ensure_vm_deleted
        await _ensure_vm_deleted(vm_id, reason="direction_removed")

    return {"message": "Direction removed", "cancelled_tasks": len(cancelled_tasks)}


@router.patch("/projects/{project_id}/directions/reorder")
async def reorder_directions(
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    direction_ids = payload.get("direction_ids")
    if not isinstance(direction_ids, list):
        raise HTTPException(status_code=400, detail="direction_ids must be a list")

    existing_ids = {d.get("id") for d in (project.directions or [])}
    provided_ids = set(direction_ids)
    if existing_ids != provided_ids:
        raise HTTPException(status_code=400, detail="direction_ids must match existing direction IDs exactly")

    directions_by_id = {d.get("id"): d for d in (project.directions or [])}
    reordered = [directions_by_id[id] for id in direction_ids]

    project.directions = reordered
    flag_modified(project, 'directions')
    session.add(project)
    await session.commit()
    return {"directions": reordered}


@router.patch("/projects/{project_id}/directions/{direction_id}")
async def update_direction(
    project_id: uuid.UUID,
    direction_id: str,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    # Validate inputs
    if "priority" in payload and payload["priority"] not in ("high", "medium", "low"):
        raise HTTPException(status_code=400, detail="priority must be high, medium, or low")
    if "status" in payload and payload["status"] not in ("active", "completed", "paused", "proposed", "expired"):
        raise HTTPException(status_code=400, detail="status must be active, completed, paused, proposed, or expired")
    if "text" in payload and (not isinstance(payload["text"], str) or len(payload["text"]) > DIRECTION_TEXT_MAX or len(payload["text"]) < 1):
        raise HTTPException(status_code=400, detail=f"text must be 1-{DIRECTION_TEXT_MAX} characters")
    if "paths" in payload and (not isinstance(payload["paths"], list) or not all(isinstance(p, str) for p in payload["paths"])):
        raise HTTPException(status_code=400, detail="paths must be a list of strings")
    if "completion_criteria" in payload and payload["completion_criteria"] is not None:
        if not isinstance(payload["completion_criteria"], str) or len(payload["completion_criteria"]) > COMPLETION_CRITERIA_MAX:
            raise HTTPException(status_code=400, detail=f"completion_criteria must be a string of at most {COMPLETION_CRITERIA_MAX} characters")
    if "conflicts_with" in payload and payload["conflicts_with"] is not None:
        if not isinstance(payload["conflicts_with"], list) or not all(isinstance(c, str) for c in payload["conflicts_with"]):
            raise HTTPException(status_code=400, detail="conflicts_with must be a list of direction ID strings")
        existing_ids = {d.get("id") for d in (project.directions or []) if d.get("id") != direction_id}
        invalid = [cid for cid in payload["conflicts_with"] if cid not in existing_ids]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Unknown direction IDs in conflicts_with: {invalid}")
    if "category" in payload and payload["category"] is not None and payload["category"] not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of: {sorted(VALID_CATEGORIES)}")
    if "cost_budget_usd" in payload and payload["cost_budget_usd"] is not None:
        if not isinstance(payload["cost_budget_usd"], (int, float)) or isinstance(payload["cost_budget_usd"], bool):
            raise HTTPException(status_code=400, detail="cost_budget_usd must be a number or null")
        if payload["cost_budget_usd"] < 0:
            raise HTTPException(status_code=400, detail="cost_budget_usd must be non-negative")

    # Direction cap check: if reactivating, ensure we won't exceed the cap
    if payload.get("status") == "active":
        from app.shared_state import runtime_config as _runtime_config
        max_active = _runtime_config.get("max_active_directions", 5)
        # Count active directions, excluding the one being updated (it may already be active)
        active_count = sum(
            1 for d in (project.directions or [])
            if d.get("status") == "active" and d.get("id") != direction_id
        )
        if active_count >= max_active:
            raise HTTPException(
                status_code=409,
                detail=f"Direction cap reached: max {max_active} active directions allowed"
            )

    directions = list(project.directions or [])
    updated = False
    for d in directions:
        if d.get("id") == direction_id:
            if "priority" in payload:
                d["priority"] = payload["priority"]
            if "status" in payload:
                d["status"] = payload["status"]
                # When reactivating from paused, clear auto-pause fields
                if payload["status"] == "active":
                    d.pop("paused_reason", None)
                    d.pop("paused_at", None)
                    d.pop("auto_paused", None)
                    d.pop("expired_at", None)
                    # Operator reactivation acknowledges any escalation — clear
                    # manual-intervention flag, pending notification state, and
                    # reset remediation attempts so spawn calibration no longer
                    # penalizes the direction.
                    d.pop("needs_manual_intervention", None)
                    d.pop("needs_manual_intervention_at", None)
                    d.pop("escalation_notification_pending", None)
                    d.pop("escalation_notification_retries", None)
                    d.pop("last_researched_at", None)
                    d["remediation_attempts"] = 0
                # When reviving from expired back to proposed, clear expired_at
                if payload["status"] == "proposed":
                    d.pop("expired_at", None)
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
                    import app.shared_state as _ss  # noqa: PLC0415
                    _ss.clear_direction_hold(direction_id)
            if "research_history" in payload:
                if payload["research_history"] is None or (isinstance(payload["research_history"], list) and len(payload["research_history"]) == 0):
                    d["research_history"] = []
            if "notes" in payload:
                if isinstance(payload["notes"], list):
                    d["notes"] = payload["notes"]
            if "completion_criteria" in payload:
                if payload["completion_criteria"] is None:
                    d.pop("completion_criteria", None)
                elif isinstance(payload["completion_criteria"], str):
                    d["completion_criteria"] = payload["completion_criteria"]
            if "conflicts_with" in payload:
                if payload["conflicts_with"] is None or (isinstance(payload["conflicts_with"], list) and len(payload["conflicts_with"]) == 0):
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
            updated = True
            break

    if not updated:
        raise HTTPException(status_code=404, detail="Direction not found")

    project.directions = directions
    flag_modified(project, 'directions')
    session.add(project)
    await session.commit()
    return {"direction": next(d for d in directions if d.get("id") == direction_id)}


@router.post("/projects/{project_id}/directions/{direction_id}/research")
async def create_direction_research_task(
    project_id: uuid.UUID,
    direction_id: str,
    bypass_cooldown: bool = False,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    direction = next((d for d in (project.directions or []) if d.get("id") == direction_id), None)
    if not direction:
        raise HTTPException(status_code=404, detail="Direction not found")

    if not bypass_cooldown:
        from app.shared_state import runtime_config as _runtime_config
        cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
        last_researched_at = direction.get("last_researched_at")
        if last_researched_at:
            elapsed = (_utcnow() - datetime.fromisoformat(last_researched_at)).total_seconds()
            if elapsed < cooldown_minutes * 60:
                remaining_minutes = int((cooldown_minutes * 60 - elapsed) / 60)
                raise HTTPException(
                    status_code=429,
                    detail=f"Direction is in cooldown for {remaining_minutes} more minutes. Use bypass_cooldown=true to force."
                )

    direction_text = direction.get("text", "")
    description = direction_text
    if bypass_cooldown:
        description = f"[cooldown bypassed manually] {direction_text}"

    vm_name = f"agent-vm-{uuid.uuid4().hex}"
    task_payload = validate_internal_payload({
        "type": "research",
        "directions": [direction],
        "base_branch": "main",
    }, "directions:research")
    new_task = Task(
        project_id=project.id,
        title=f"Research: {direction_text[:200]}",
        description=description,
        payloads=[task_payload],
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="USER",
        requires_approval=False,
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={
            "created_by": "USER",
            "method": "direction_research",
            "direction_id": direction_id,
            "bypass_cooldown": bypass_cooldown,
        }
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
    current_user: dict = Depends(get_current_user)
):
    """Clear a direction's consecutive-failure hold so new tasks dispatch again.

    Returns was_held=True if a hold was actually in place, False if the
    direction was already healthy (idempotent).
    """
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")
    direction = next((d for d in (project.directions or []) if d.get("id") == direction_id), None)
    if not direction:
        raise HTTPException(status_code=404, detail="Direction not found")

    import app.shared_state as _ss  # noqa: PLC0415
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
    current_user: dict = Depends(get_current_user)
):
    """Manually trigger a strategic cycle. Phase: 'design_doc' (Phase 1 → chains to Phase 2) or 'strategic_review' (Phase 2 only)."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    # Guard: no strategic task already running. Lock the project row so the
    # check+insert is transactionally atomic with concurrent spawn attempts
    # (design_doc completion chain, autonomous-research tick, other manual triggers).
    await session.execute(
        select(Project.id).where(Project.id == project.id).with_for_update()
    )
    active = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    for t in active:
        t_type = (t.first_payload).get("type")
        if t_type in ("design_doc_update", "strategic_review"):
            raise HTTPException(status_code=409, detail=f"A {t_type} task is already active: {t.id}")

    from app.shared_state import runtime_config as _runtime_config

    # Strip agent-unused / operator-only keys (e.g. playbook_archive) before
    # shipping the design_doc. See _agent_view_design_doc for rationale.
    from app.strategic_spawning import _agent_view_design_doc  # noqa: PLC0415
    design_doc_for_agent = _agent_view_design_doc(project.design_doc)

    if phase == "design_doc":
        # Fetch open messages
        open_msgs_result = await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
                ProjectMessage.status == "open",
            ).order_by(ProjectMessage.created_at.asc()).limit(50)
        )
        open_msgs = [
            {"id": str(m.id), "type": m.message_type, "content": m.content,
             "context": m.context, "from": m.from_type,
             "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in open_msgs_result.scalars().all()
        ]
        doc_model = _runtime_config.get("task_type_models", {}).get("design_doc_update") or \
                    _runtime_config.get("task_type_models", {}).get("research") or None
        doc_effort = _runtime_config.get("task_type_efforts", {}).get("design_doc_update") or \
                     _runtime_config.get("task_type_efforts", {}).get("research") or None
        payload = {
            "type": "design_doc_update",
            "design_doc": design_doc_for_agent,
            "proposed_changes": (project.design_doc or {}).get("proposed_changes", []),
            "project_name": project.name,
            "vision": project.vision or {},
            "open_messages": open_msgs,
            "base_branch": "main",
            **({"model": doc_model} if doc_model else {}),
            **({"effort": doc_effort} if doc_effort else {}),
        }
        title = f"Architecture review: {project.name}"
    elif phase == "strategic_review":
        # Build direction stats
        from app.strategic_analysis import _build_outcome_evaluation
        all_active = [d for d in (project.directions or []) if d.get("status") == "active"]
        direction_stats = []
        for d in all_active:
            history = d.get("research_history", [])
            agg = d.get("history_aggregate", {})
            total_cycles = len(history) + (agg.get("total_cycles") or 0)
            total_merged = sum(h.get("prs_merged", 0) for h in history) + (agg.get("total_prs_merged") or 0)
            total_failed = sum(h.get("tasks_failed", 0) for h in history) + (agg.get("total_tasks_failed") or 0)
            stat = {
                "id": d.get("id"), "text": d.get("text"), "priority": d.get("priority"),
                "status": d.get("status"), "total_cycles": total_cycles,
                "prs_merged": total_merged, "tasks_failed": total_failed,
            }
            if d.get("completion_criteria"):
                stat["completion_criteria"] = d["completion_criteria"]
            if d.get("category"):
                stat["category"] = d["category"]
            direction_stats.append(stat)
        # Unified conversation thread — last 30 messages chronologically
        thread_result = await session.execute(
            select(ProjectMessage).where(
                ProjectMessage.project_id == project.id,
            ).order_by(ProjectMessage.created_at.desc()).limit(30)
        )
        all_thread_msgs = list(reversed(thread_result.scalars().all()))
        conversation_thread = [
            {"id": str(m.id), "type": m.message_type, "content": m.content,
             "context": m.context, "from": m.from_type, "status": m.status,
             "resolution": m.resolution,
             "created_at": m.created_at.isoformat() if m.created_at else None,
             "resolved_at": m.resolved_at.isoformat() if m.resolved_at else None}
            for m in all_thread_msgs
        ]
        open_msgs = [msg for msg in conversation_thread if msg["status"] == "open"]
        review_model = _runtime_config.get("task_type_models", {}).get("strategic_review") or \
                       _runtime_config.get("task_type_models", {}).get("research") or None
        review_effort = _runtime_config.get("task_type_efforts", {}).get("strategic_review") or \
                        _runtime_config.get("task_type_efforts", {}).get("research") or None
        payload = {
            "type": "strategic_review",
            "research_mode": "strategic",
            "design_doc": design_doc_for_agent,
            "vision": project.vision or {},
            "directions_summary": direction_stats,
            "outcome_evaluation": _build_outcome_evaluation(project),
            "project_name": project.name,
            "open_messages": open_msgs,
            "conversation_thread": conversation_thread,
            "base_branch": "main",
            **({"model": review_model} if review_model else {}),
            **({"effort": review_effort} if review_effort else {}),
        }
        title = f"Strategic review: {project.name}"
    else:
        raise HTTPException(status_code=400, detail="Phase must be 'design_doc' or 'strategic_review'")

    task_type = payload.get("type", phase)
    payload = validate_internal_payload(payload, f"directions:{task_type}")
    new_task = Task(
        project_id=project.id,
        title=title,
        description=title,
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="USER",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="CREATED",
        details={"created_by": "USER", "method": "manual_strategic_cycle", "phase": phase},
    )
    session.add(history)
    await session.commit()
    await session.refresh(new_task)

    return {
        "id": str(new_task.id),
        "project_id": str(project.id),
        "title": new_task.title,
        "status": new_task.status,
        "phase": phase,
    }


# ── Messages ─────────────────────────────────────────────────────────────


class CreateMessage(BaseModel):
    content: str
    message_type: str = "NOTE"  # NOTE, FEEDBACK, BUG_REPORT, QUESTION, BLOCKER


@router.post("/projects/{project_id}/messages")
async def create_message(
    project_id: str,
    payload: CreateMessage,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(get_current_user),
):
    """Create a message from the user to the autonomous system."""
    project = await _get_user_project(project_id, user, session)
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    if len(payload.content) > 5000:
        raise HTTPException(status_code=400, detail="Content exceeds 5000 character limit")

    try:
        msg_type = MessageType(payload.message_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid message type: {payload.message_type}")

    msg = ProjectMessage(
        project_id=project.id,
        message_type=msg_type,
        from_type="user",
        content=payload.content.strip(),
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)

    return {"id": str(msg.id), "status": "created"}


@router.get("/projects/{project_id}/messages")
async def list_messages(
    project_id: str,
    status: Optional[str] = "open",
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(get_current_user),
):
    """List messages for a project."""
    project = await _get_user_project(project_id, user, session)
    stmt = select(ProjectMessage).where(ProjectMessage.project_id == project.id)
    if status:
        stmt = stmt.where(ProjectMessage.status == status)
    stmt = stmt.order_by(ProjectMessage.created_at.desc()).limit(50)
    result = await session.execute(stmt)
    msgs = result.scalars().all()
    return [
        {
            "id": str(m.id), "type": m.message_type, "content": m.content,
            "from": m.from_type, "status": m.status, "context": m.context,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in msgs
    ]
