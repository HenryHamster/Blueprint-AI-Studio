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
from app.models import Task, TaskStatus, TaskHistory, Project, _utcnow
from app.auth import get_current_user
from app.admin import _get_user_project, _get_user_project_with_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["directions"],
    dependencies=[Depends(get_current_user)]
)


class DirectionPayload(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    priority: str = Field(default="medium", pattern=r"^(high|medium|low)$")
    paths: Optional[List[str]] = None


@router.get("/projects/{project_id}/directions")
async def get_directions(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project(project_id, current_user["user_id"], session)
    return {"directions": project.directions or [], "autonomous_enabled": project.autonomous_enabled}


@router.post("/projects/{project_id}/directions")
async def add_direction(
    project_id: uuid.UUID,
    payload: DirectionPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    direction = {
        "id": f"d-{uuid.uuid4().hex[:8]}",
        "text": payload.text,
        "priority": payload.priority,
        "paths": payload.paths or [],
        "added_at": _utcnow().isoformat(),
        "status": "active",
    }
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
        if len(entry["text"]) > 500:
            raise HTTPException(status_code=400, detail="Direction text must not exceed 500 characters")
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
    if "status" in payload and payload["status"] not in ("active", "completed", "paused", "proposed"):
        raise HTTPException(status_code=400, detail="status must be active, completed, paused, or proposed")
    if "text" in payload and (not isinstance(payload["text"], str) or len(payload["text"]) > 500 or len(payload["text"]) < 1):
        raise HTTPException(status_code=400, detail="text must be 1-500 characters")
    if "paths" in payload and (not isinstance(payload["paths"], list) or not all(isinstance(p, str) for p in payload["paths"])):
        raise HTTPException(status_code=400, detail="paths must be a list of strings")

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
            if "research_history" in payload:
                if payload["research_history"] is None or (isinstance(payload["research_history"], list) and len(payload["research_history"]) == 0):
                    d["research_history"] = []
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
    task_payload = {
        "type": "research",
        "directions": [direction],
        "base_branch": "main",
    }
    new_task = Task(
        project_id=project.id,
        title=f"Research: {direction_text[:60]}",
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
