from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, delete
from typing import Optional, Dict, Any, List
import uuid
from datetime import timedelta
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.shared_state import limiter
from app.models import Task, TaskStatus, Project, ProjectMember, _utcnow
from app.auth import get_current_user
from app.admin import _get_user_project, _get_user_project_with_role, _user_project_ids

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-projects"],
    dependencies=[Depends(get_current_user)]
)


@router.get("/projects")
async def get_all_projects(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    # Join projects with membership to get user's role on each project
    query = (
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == current_user["user_id"])
        .order_by(Project.created_at.desc())
    )
    rows = (await session.execute(query)).all()
    result = []
    for project, role in rows:
        # Serialize project and add my_role
        data = {
            "id": str(project.id),
            "name": project.name,
            "user_id": project.user_id,
            "status": project.status,
            "github_owner": project.github_owner,
            "github_repo": project.github_repo,
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "directions": project.directions,
            "vision": project.vision or {},
            "design_doc": project.design_doc or {},
            "build_config": project.build_config or {},
            "autonomous_enabled": project.autonomous_enabled,
            "is_self_repo": project.is_self_repo,
            "my_role": role,
        }
        result.append(data)
    return result


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    directions: Optional[List[Dict[str, Any]]] = None
    vision: Optional[Dict[str, Any]] = None
    design_doc: Optional[Dict[str, Any]] = None
    build_config: Optional[Dict[str, Any]] = None
    autonomous_enabled: Optional[bool] = None


@router.patch("/projects/{project_id}")
@limiter.limit("30/minute")
async def update_project(
    request: Request,
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "member")

    if payload.name is not None:
        project.name = payload.name
    if payload.autonomous_enabled is not None:
        project.autonomous_enabled = payload.autonomous_enabled
    if payload.directions is not None:
        # Auto-generate ids for any directions missing one
        normalized = []
        for d in payload.directions:
            entry = dict(d)
            if not entry.get("id"):
                entry["id"] = f"d-{uuid.uuid4().hex[:8]}"
            if entry.get("priority") not in ("high", "medium", "low"):
                raise HTTPException(status_code=400, detail=f"Invalid priority: {entry.get('priority')}")
            if not isinstance(entry.get("text"), str) or not entry["text"].strip():
                raise HTTPException(status_code=400, detail="Each direction must have non-empty text")
            normalized.append(entry)
        project.directions = normalized
        flag_modified(project, "directions")
    if payload.vision is not None:
        vision = dict(payload.vision)
        current_rev = (project.vision or {}).get("revision", 0)
        vision["revision"] = (current_rev or 0) + 1
        vision["last_updated"] = _utcnow().isoformat()
        project.vision = vision
        flag_modified(project, "vision")
    if payload.design_doc is not None:
        doc = dict(payload.design_doc)
        current_rev = (project.design_doc or {}).get("revision", 0)
        doc["revision"] = (current_rev or 0) + 1
        doc["last_updated"] = _utcnow().isoformat()
        doc["updated_by"] = "user"
        project.design_doc = doc
        flag_modified(project, "design_doc")
    if payload.build_config is not None:
        project.build_config = payload.build_config
        flag_modified(project, "build_config")

    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


@router.delete("/projects/{project_id}")
async def delete_project_admin(
    project_id: uuid.UUID,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Delete a project and cascade to tasks/artifacts/history.
    Refuses if active tasks exist unless force=true."""
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")

    ACTIVE_STATUSES = [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]
    active_tasks = (await session.execute(
        select(Task.id).where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
    )).scalars().all()

    if active_tasks and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Project has {len(active_tasks)} active task(s). Cancel them first, or pass ?force=true."
        )

    if active_tasks and force:
        # Force: cancel active tasks and delete their VMs
        from app.main import _ensure_vm_deleted
        for task_id in active_tasks:
            task = await session.get(Task, task_id)
            if task:
                task.status = TaskStatus.CANCELLED
                task.latest_status_message = "Project force-deleted"
                session.add(task)
                await _ensure_vm_deleted(task, reason="project_force_delete")
        await session.commit()

    # Delete dependent records that lack ON DELETE CASCADE
    from app.models import Alert
    await session.execute(
        delete(Alert).where(Alert.project_id == project_id)
    )
    await session.delete(project)
    await session.commit()

    # Evict from GitHub token cache
    from app.routers.agent import _github_token_cache
    _github_token_cache.pop(str(project_id), None)

    return {"message": f"Project {project_id} deleted", "active_tasks_cancelled": len(active_tasks) if active_tasks else 0}


@router.post("/autonomous/pause-all")
async def pause_all_autonomous(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Disable autonomous mode on all projects at once."""
    projects = (await session.execute(
        select(Project).where(Project.id.in_(_user_project_ids(current_user["user_id"])))
    )).scalars().all()
    for p in projects:
        p.autonomous_enabled = False
    await session.commit()
    return {"paused": len(projects)}


@router.get("/projects/{project_id}/directions/task-counts")
async def get_direction_task_counts(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project(project_id, current_user["user_id"], session)

    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.not_in([TaskStatus.CANCELLED, TaskStatus.FAILED]),
        )
    )
    tasks = result.scalars().all()

    counts: Dict[str, int] = {}
    for task in tasks:
        payload = task.first_payload
        if payload.get("type") != "research":
            continue
        # New-format tasks have current_direction_id pointing to the targeted direction
        current_did = payload.get("current_direction_id")
        if current_did:
            counts[current_did] = counts.get(current_did, 0) + 1
        else:
            # Legacy format: count all directions in the list
            for direction in payload.get("directions", []):
                d_id = direction.get("id")
                if d_id:
                    counts[d_id] = counts.get(d_id, 0) + 1

    return {"counts": counts}


@router.patch("/projects/{project_id}/autonomous")
async def toggle_autonomous(
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await _get_user_project_with_role(project_id, current_user["user_id"], session, "owner")

    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="'enabled' field required")

    project.autonomous_enabled = bool(payload["enabled"])
    # Reset failure counter when re-enabling
    if project.autonomous_enabled:
        from app.shared_state import autonomous_failure_counts
        autonomous_failure_counts.pop(str(project_id), None)
    session.add(project)
    await session.commit()
    return {"autonomous_enabled": project.autonomous_enabled}


@router.get("/autonomous-status")
async def get_autonomous_status(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.shared_state import autonomous_failure_counts, runtime_config

    projects = (await session.execute(
        select(Project).where(Project.id.in_(_user_project_ids(current_user["user_id"]))).order_by(Project.created_at.desc())
    )).scalars().all()

    one_hour_ago = _utcnow() - timedelta(hours=1)

    # Count autonomous RESEARCH tasks per project in the last hour (matches rate limit guard)
    auto_tasks = (await session.execute(
        select(Task).where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalars().all()
    tasks_per_project: dict = {}
    for t in auto_tasks:
        if (t.first_payload).get("type") == "research":
            tasks_per_project[t.project_id] = tasks_per_project.get(t.project_id, 0) + 1

    # Batch query: most recent autonomous task per project
    last_research_subq = (
        select(
            Task.project_id,
            func.max(Task.created_at).label("max_created_at"),
        ).where(Task.created_by == "ORCHESTRATOR")
        .group_by(Task.project_id)
        .subquery()
    )
    last_research_rows = (await session.execute(
        select(last_research_subq.c.project_id, last_research_subq.c.max_created_at)
    )).all()
    last_research_map = {row.project_id: row.max_created_at for row in last_research_rows}

    result = []
    for project in projects:
        project_id_str = str(project.id)
        result.append({
            "project_id": project_id_str,
            "project_name": project.name,
            "autonomous_enabled": project.autonomous_enabled,
            "consecutive_failures": autonomous_failure_counts.get(project_id_str, 0),
            "last_research_at": last_research_map[project.id].isoformat() if last_research_map.get(project.id) else None,
            "tasks_spawned_last_hour": tasks_per_project.get(project.id, 0),
            "rate_limit": runtime_config["max_autonomous_tasks_per_hour"],
        })

    return {"projects": result}
