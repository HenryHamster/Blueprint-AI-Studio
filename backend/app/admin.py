from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional, Dict, Any, List, TypedDict
import uuid
import json
import httpx
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, update
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.shared_state import limiter, circuit_breakers as _circuit_breakers
from app.models import Task, TaskStatus, TaskHistory, Project, Artifact, ArtifactType, UserIntegration, AllowedUser, MaxPlanAccount, DeploymentState, DeploymentStatus, Alert, _utcnow
from app.auth import get_current_user, require_admin

from app.github import generate_installation_access_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_user)]
)


def _user_project_ids(user_id: str):
    """Subquery returning project IDs owned by user_id."""
    return select(Project.id).where(Project.user_id == user_id)


async def _get_user_project(project_id: uuid.UUID, user_id: str, session: AsyncSession) -> Project:
    """Fetch a project ensuring it belongs to the given user. Raises 404 if not found."""
    project = await session.get(Project, project_id)
    if not project or project.user_id != user_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _get_user_task(task_id: uuid.UUID, user_id: str, session: AsyncSession) -> Task:
    """Fetch a task ensuring its project belongs to the given user. Raises 404 if not found."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await session.get(Project, task.project_id)
    if not project or project.user_id != user_id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/projects")
async def get_all_projects(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    query = select(Project).where(Project.user_id == current_user["user_id"]).order_by(Project.created_at.desc())
    result = await session.execute(query)
    projects = result.scalars().all()
    return projects

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    directions: Optional[List[Dict[str, Any]]] = None
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
    project = await _get_user_project(project_id, current_user["user_id"], session)

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
    project = await _get_user_project(project_id, current_user["user_id"], session)

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

    await session.delete(project)
    await session.commit()

    # Evict from GitHub token cache
    from app.main import _github_token_cache
    _github_token_cache.pop(str(project_id), None)

    return {"message": f"Project {project_id} deleted", "active_tasks_cancelled": len(active_tasks) if active_tasks else 0}


@router.post("/autonomous/pause-all")
async def pause_all_autonomous(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Disable autonomous mode on all projects at once."""
    projects = (await session.execute(select(Project).where(Project.user_id == current_user["user_id"]))).scalars().all()
    for p in projects:
        p.autonomous_enabled = False
    await session.commit()
    return {"paused": len(projects)}

TERMINAL_STATUSES = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]


@router.get("/tasks")
async def get_all_tasks(
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    status: List[TaskStatus] = Query(default=[]),
    terminal_only: bool = Query(False),
    archived: Optional[bool] = Query(None),
    search: Optional[str] = Query(None, max_length=200),
    created_after: Optional[datetime] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    # Build shared filter conditions
    filters = [Task.project_id.in_(_user_project_ids(current_user["user_id"]))]
    if status:
        filters.append(Task.status.in_(status))
    elif terminal_only:
        filters.append(Task.status.in_(TERMINAL_STATUSES))
    if archived is not None:
        filters.append(Task.archived == archived)
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        search_pattern = f"%{escaped}%"
        filters.append(
            (Task.title.ilike(search_pattern, escape="\\")) | (Task.description.ilike(search_pattern, escape="\\"))
        )
    if created_after:
        filters.append(Task.created_at >= created_after)

    count_query = select(func.count()).select_from(Task)
    for f in filters:
        count_query = count_query.where(f)
    total = (await session.execute(count_query)).scalar() or 0

    query = select(Task).order_by(Task.created_at.desc()).offset(offset).limit(limit)
    for f in filters:
        query = query.where(f)

    result = await session.execute(query)
    tasks = result.scalars().all()

    # Join artifacts to include all artifacts per task
    task_ids = [t.id for t in tasks]
    artifacts_map: Dict[str, List[Dict[str, Any]]] = {}
    pr_map: Dict[str, str] = {}
    logs_map: Dict[str, str] = {}
    logs_truncated_map: Dict[str, bool] = {}
    research_map: Dict[str, str] = {}
    commit_map: Dict[str, Dict[str, Any]] = {}
    if task_ids:
        artifact_stmt = select(Artifact).where(Artifact.task_id.in_(task_ids)).order_by(Artifact.created_at.asc())
        artifacts = (await session.execute(artifact_stmt)).scalars().all()
        for a in artifacts:
            tid = str(a.task_id)
            if tid not in artifacts_map:
                artifacts_map[tid] = []
            artifacts_map[tid].append({
                "artifact_type": a.artifact_type,
                "pr_url": a.pr_url,
                "commit_hash": a.commit_hash,
                "repo_url": a.repo_url,
                "iteration": a.iteration,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })
            if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                pr_map[tid] = a.pr_url
            if a.artifact_type == ArtifactType.ERROR_LOG and a.logs:
                logs_map[tid] = a.logs[:2000]
                logs_truncated_map[tid] = len(a.logs) > 2000
            if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                try:
                    parsed = json.loads(a.logs[:100000])
                    if isinstance(parsed, list):
                        research_map[str(a.task_id)] = parsed
                    elif isinstance(parsed, dict):
                        logs = parsed.get('logs', [])
                        if isinstance(logs, list):
                            research_map[str(a.task_id)] = logs
                except (json.JSONDecodeError, AttributeError):
                    pass
            if a.artifact_type == ArtifactType.GIT_COMMIT and a.commit_hash:
                commit_map[tid] = {"commit_hash": a.commit_hash, "repo_url": a.repo_url}

    # Fetch task history (last 10 entries per task)
    history_map: Dict[str, List[Dict[str, Any]]] = {}
    if task_ids:
        history_stmt = (
            select(TaskHistory)
            .where(TaskHistory.task_id.in_(task_ids))
            .order_by(TaskHistory.timestamp.desc())
        )
        all_history = (await session.execute(history_stmt)).scalars().all()
        for h in all_history:
            tid = str(h.task_id)
            if tid not in history_map:
                history_map[tid] = []
            if len(history_map[tid]) < 10:
                history_map[tid].append({
                    "timestamp": h.timestamp.isoformat() if h.timestamp else None,
                    "action": h.action,
                    "details": h.details,
                })

    # Build parent→children map from the fetched result set (no extra DB query)
    children_map: Dict[str, List[str]] = {}
    for t in tasks:
        if t.parent_task_id:
            parent_key = str(t.parent_task_id)
            if parent_key not in children_map:
                children_map[parent_key] = []
            children_map[parent_key].append(str(t.id))

    # Return tasks with artifact data attached
    result_list = []
    for t in tasks:
        task_type = ((t.payloads or [{}])[0]).get("type", "create")
        task_dict = {
            "id": str(t.id),
            "project_id": str(t.project_id),
            "title": t.title,
            "description": t.description,
            "status": t.status,
            "task_type": task_type,
            "vm_instance_id": t.vm_instance_id,
            "latest_status_message": t.latest_status_message,
            "status_log": t.status_log or [],
            "created_by": t.created_by,
            "requires_approval": t.requires_approval,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "depends_on": t.depends_on or [],
            "plan_output": t.plan_output,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "last_heartbeat": t.last_heartbeat.isoformat() if t.last_heartbeat else None,
            "attempt_count": t.attempt_count,
            "payloads": t.payloads,
            "context": {k: v for k, v in (t.context or {}).items() if not k.startswith("max_plan_")},
            "archived": t.archived,
            "artifacts": artifacts_map.get(str(t.id), []),
            "pr_url": pr_map.get(str(t.id)),
            "error_logs": logs_map.get(str(t.id)),
            "error_logs_truncated": logs_truncated_map.get(str(t.id), False),
            "research_findings": research_map.get(str(t.id)),
            "commit_hash": commit_map.get(str(t.id), {}).get("commit_hash"),
            "repo_url": commit_map.get(str(t.id), {}).get("repo_url"),
            "history": history_map.get(str(t.id), []),
            "child_task_ids": children_map.get(str(t.id), []),
        }
        result_list.append(task_dict)

    # Global counts (not affected by pagination/filters)
    active_count = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING])
        )
    )).scalar() or 0
    evaluating_count = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status == TaskStatus.EVALUATING)
    )).scalar() or 0
    queued_count = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED)
    )).scalar() or 0

    return {"tasks": result_list, "total": total, "active_count": active_count, "evaluating_count": evaluating_count, "queued_count": queued_count}


class BulkArchiveRequest(BaseModel):
    task_ids: Optional[List[str]] = None
    filter: Optional[str] = None  # 'completed' or 'terminal'


@router.post("/tasks/bulk-archive")
async def bulk_archive_tasks(
    request: BulkArchiveRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    user_project_ids = _user_project_ids(current_user["user_id"])
    if request.task_ids is not None:
        archived = []
        for raw_id in request.task_ids:
            try:
                task = await session.get(Task, uuid.UUID(raw_id))
            except (ValueError, AttributeError):
                continue
            if task and task.status in TERMINAL_STATUSES and not task.archived:
                # Verify task belongs to user's project
                project = await session.get(Project, task.project_id)
                if not project or project.user_id != current_user["user_id"]:
                    continue
                task.archived = True
                session.add(task)
                archived.append(task)
    elif request.filter in ('completed', 'terminal'):
        statuses = [TaskStatus.COMPLETED] if request.filter == 'completed' else TERMINAL_STATUSES
        query = select(Task).where(Task.archived.is_(False), Task.status.in_(statuses), Task.project_id.in_(user_project_ids))
        result = await session.execute(query)
        archived = result.scalars().all()
        for task in archived:
            task.archived = True
            session.add(task)
    else:
        raise HTTPException(status_code=400, detail="Must provide task_ids or a valid filter ('completed' or 'terminal')")

    await session.commit()
    return {"archived_count": len(archived)}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await _get_user_task(tid, current_user["user_id"], session)

    # Fetch all artifacts for this task (all types, not just last per type)
    artifact_stmt = select(Artifact).where(Artifact.task_id == tid).order_by(Artifact.created_at.asc())
    artifacts = (await session.execute(artifact_stmt)).scalars().all()
    artifacts_list = []
    pr_url = None
    error_logs = None
    error_logs_truncated = False
    research_findings = None
    commit_hash = None
    repo_url = None
    for a in artifacts:
        artifacts_list.append({
            "artifact_type": a.artifact_type,
            "pr_url": a.pr_url,
            "commit_hash": a.commit_hash,
            "repo_url": a.repo_url,
            "iteration": a.iteration,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
        if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
            pr_url = a.pr_url
        if a.artifact_type == ArtifactType.ERROR_LOG and a.logs:
            error_logs_truncated = len(a.logs) > 2000
            error_logs = a.logs[:2000]
        if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
            research_findings = a.logs[:100000]
        if a.artifact_type == ArtifactType.GIT_COMMIT and a.commit_hash:
            commit_hash = a.commit_hash
            repo_url = a.repo_url

    # Fetch full task history (all entries, not capped at 10)
    history_stmt = (
        select(TaskHistory)
        .where(TaskHistory.task_id == tid)
        .order_by(TaskHistory.timestamp.desc())
    )
    all_history = (await session.execute(history_stmt)).scalars().all()
    history = [
        {
            "timestamp": h.timestamp.isoformat() if h.timestamp else None,
            "action": h.action,
            "details": h.details,
        }
        for h in all_history
    ]

    # Fetch child task IDs
    child_ids_result = await session.execute(
        select(Task.id).where(Task.parent_task_id == tid)
    )
    child_task_ids = [str(row) for row in child_ids_result.scalars().all()]

    task_type = ((task.payloads or [{}])[0]).get("type", "create")
    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "task_type": task_type,
        "vm_instance_id": task.vm_instance_id,
        "latest_status_message": task.latest_status_message,
        "status_log": task.status_log or [],
        "created_by": task.created_by,
        "requires_approval": task.requires_approval,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "depends_on": task.depends_on or [],
        "plan_output": task.plan_output,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "last_heartbeat": task.last_heartbeat.isoformat() if task.last_heartbeat else None,
        "attempt_count": task.attempt_count,
        "payloads": task.payloads,
        "context": {k: v for k, v in (task.context or {}).items() if not k.startswith("max_plan_")},
        "archived": task.archived,
        "artifacts": artifacts_list,
        "pr_url": pr_url,
        "error_logs": error_logs,
        "error_logs_truncated": error_logs_truncated,
        "commit_hash": commit_hash,
        "repo_url": repo_url,
        "research_findings": research_findings,
        "history": history,
        "child_task_ids": child_task_ids,
    }


class CreateTaskPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=50000)
    base_branch: str = Field(default="main", max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$")
    task_type: str = Field(default="create", pattern=r"^(create|plan|research|review|fix|security_audit|qa|deps|docs)$")
    pr_number: Optional[int] = None
    branch: Optional[str] = Field(default=None, max_length=255, pattern=r"^[a-zA-Z0-9_./-]*[a-zA-Z0-9_.-]$")
    model: Optional[str] = Field(default=None, min_length=1)
    effort: Optional[str] = Field(default=None, pattern=r"^(low|medium|high)$")

    @field_validator("base_branch", "branch")
    @classmethod
    def validate_branch_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        for part in v.split("/"):
            if part in (".git", ".lock"):
                raise ValueError(f"Branch name contains reserved component: {part}")
            if part.startswith("."):
                raise ValueError(f"Branch component cannot start with a dot: {part}")
            if ".." in part:
                raise ValueError(f"Branch component cannot contain '..': {part}")
        return v


@router.post("/tasks")
@limiter.limit("60/minute")
async def create_task_manually(
    request: Request,
    payload: CreateTaskPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Create a Claude Code task. Auto-creates a project for the repo if one doesn't exist.
    """
    # Parse owner/repo
    github_owner, github_repo = payload.github_repo.split("/", 1)

    # Find or create project for this repo (scoped to current user)
    stmt = select(Project).where(
        Project.github_owner == github_owner,
        Project.github_repo == github_repo,
        Project.user_id == current_user["user_id"],
    )
    project = (await session.execute(stmt)).scalars().first()

    if not project:
        project = Project(
            name=payload.github_repo,
            user_id=current_user.get("user_id", "admin"),
            github_owner=github_owner,
            github_repo=github_repo,
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)

    # Auto-generate VM instance ID so orchestrator worker picks up the task
    vm_name = f"agent-vm-{uuid.uuid4().hex}"

    # Build payload based on task type
    task_payload = {
        "type": payload.task_type,
        "prompt": payload.prompt,
        "base_branch": payload.base_branch,
    }
    if payload.model is not None:
        task_payload["model"] = payload.model
    if payload.effort is not None:
        task_payload["effort"] = payload.effort
    # Review/fix tasks need pr_number and branch
    if payload.task_type in ("review", "fix") and payload.pr_number:
        task_payload["pr_number"] = payload.pr_number
        task_payload["branch"] = payload.branch or ""
        if payload.task_type == "review":
            task_payload["original_prompt"] = payload.prompt
    # Research tasks auto-include project directions
    if payload.task_type == "research":
        active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
        task_payload["directions"] = active_directions

    # Guard: cap active tasks per project to prevent queue explosion
    active_count = (await session.execute(
        select(func.count(Task.id)).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING])
        )
    )).scalar() or 0
    if active_count >= 200:
        raise HTTPException(status_code=429, detail=f"Project has {active_count} active tasks (limit: 200)")

    new_task = Task(
        project_id=project.id,
        title=payload.title,
        description=payload.prompt,
        payloads=[task_payload],
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="USER",
        requires_approval=False,
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    # Log history
    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={
            "created_by": "USER",
            "method": "admin_launcher",
            "github_repo": payload.github_repo,
            "base_branch": payload.base_branch,
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

class RequestChangesPayload(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=5000)
    action: str = Field(default="fix", pattern=r"^(fix|review)$")

@router.post("/tasks/{task_id}/request-changes")
async def request_changes(
    task_id: uuid.UUID,
    payload: RequestChangesPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Request changes on a task awaiting approval. Spawns a fix or re-review task."""
    task = await _get_user_task(task_id, current_user["user_id"], session)
    if not task.requires_approval:
        raise HTTPException(status_code=400, detail="Task is not awaiting approval")

    # Find the PR
    artifact = (await session.execute(
        select(Artifact).where(Artifact.task_id == task_id, Artifact.artifact_type == ArtifactType.PULL_REQUEST)
    )).scalars().first()
    if not artifact or not artifact.pr_url:
        raise HTTPException(status_code=400, detail="No PR found for this task")

    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
    branch = f"blueprint/task-{str(task.id).replace('-','')[:8]}"
    task_payload = (task.payloads or [{}])[0]
    base_branch = task_payload.get("base_branch", "main")

    from app.main import _spawn_pipeline_task
    if payload.action == "fix":  # noqa: SIM108 — two distinct multi-line async calls, ternary unreadable
        spawned = await _spawn_pipeline_task(
            task, "fix", session, pr_number, branch, base_branch,
            fix_prompt=f"Human reviewer requested changes:\n\n{payload.feedback}",
            original_task_id=str(task.id)
        )
    else:
        spawned = await _spawn_pipeline_task(
            task, "review", session, pr_number, branch, base_branch,
            original_prompt=task_payload.get("prompt", task.description or ""),
            original_task_id=str(task.id)
        )

    if spawned:
        task.requires_approval = False
        task.latest_status_message = f"Changes requested — spawned {payload.action} task"
        session.add(task)
        await session.commit()
        return {"spawned_task_id": str(spawned.id), "action": payload.action}

    raise HTTPException(status_code=500, detail="Failed to spawn task (iteration cap?)")


@router.post("/tasks/{task_id}/archive")
async def archive_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    task = await _get_user_task(task_id, current_user["user_id"], session)
    if task.status not in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Cannot archive task with status '{task.status}'. Only terminal tasks (COMPLETED, FAILED, CANCELLED) can be archived.")
    task.archived = True
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@router.post("/tasks/{task_id}/unarchive")
async def unarchive_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    task = await _get_user_task(task_id, current_user["user_id"], session)
    task.archived = False
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@router.patch("/tasks/{task_id}")
@limiter.limit("60/minute")
async def modify_task(
    request: Request,
    task_id: uuid.UUID,
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    force_kill: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    task = await _get_user_task(task_id, current_user["user_id"], session)

    _TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

    changes = {}
    if "status" in payload:
        try:
            new_status = TaskStatus(payload["status"])

            # State machine validation: restrict invalid transitions
            if new_status == TaskStatus.QUEUED and task.status not in _TERMINAL_STATUSES:
                raise HTTPException(status_code=400, detail=f"Cannot requeue task in {task.status.value} state (must be completed/failed/cancelled)")
            if task.status in _TERMINAL_STATUSES and new_status not in (TaskStatus.QUEUED,):
                raise HTTPException(status_code=400, detail=f"Cannot transition from terminal state {task.status.value} to {new_status.value} (only requeue is allowed)")

            task.status = new_status
            changes["status"] = new_status.value

            # Clear approval flag on terminal statuses
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                task.requires_approval = False

            # Delete VM and release account when moving to terminal state
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED) and task.vm_instance_id:
                from app.main import _ensure_vm_deleted
                if not force_kill:
                    # Graceful: commit terminal status first so agent's next heartbeat
                    # returns 4xx, triggering graceful shutdown (5s wind-down)
                    session.add(task)
                    await session.commit()
                    await asyncio.sleep(5)
                await _ensure_vm_deleted(task, reason="admin_terminal")
                acct_id = (task.context or {}).get("max_plan_account_id")
                if acct_id:
                    from app.main import release_max_plan_account
                    await release_max_plan_account(acct_id)

            # For low-priority tasks cancelled by user, store suppress_until so the
            # orchestrator can skip re-proposals for 180 days without expanding the dedup window
            if new_status == TaskStatus.CANCELLED and task.context and task.context.get("finding_hash"):
                task_priority = task.priority if task.priority is not None else 50
                if task_priority == 0:  # low priority tasks map to 0 in TASK_PRIORITY_MAP
                    from app.main import _utcnow
                    suppress_until = (_utcnow() + timedelta(days=180)).isoformat()
                    task.context = {**task.context, "suppress_until": suppress_until}
                    flag_modified(task, "context")
                    logger.info(f"[ADMIN] Set suppress_until={suppress_until} on cancelled low-priority task {task.id}")

            # Cascade cancellation to active children
            if new_status == TaskStatus.CANCELLED:
                from app.main import _ensure_vm_deleted as _cancel_vm, release_max_plan_account as _cancel_release
                active_children = (await session.execute(
                    select(Task).where(
                        Task.parent_task_id == task.id,
                        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING])
                    )
                )).scalars().all()
                for child in active_children:
                    child.status = TaskStatus.CANCELLED
                    child.requires_approval = False
                    if child.vm_instance_id:
                        await _cancel_vm(child, reason="parent_cancelled")
                    child_acct = (child.context or {}).get("max_plan_account_id")
                    if child_acct:
                        _cancel_release(child_acct)
                    session.add(child)
                if active_children:
                    logger.info(f"[ADMIN] Cascaded CANCELLED to {len(active_children)} children of task {task.id}")

            # Reset retry state when manually re-queuing a task
            if new_status == TaskStatus.QUEUED:
                # Check if this is a research/plan parent with work children — re-evaluate
                # instead of re-run. Don't treat review/fix/create tasks as parents just
                # because they have incidental debug children.
                task_type = ((task.payloads or [{}])[0]).get("type", "")
                is_parent_type = task_type in ("research", "plan")
                from app.main import _requeue_parent_task
                children = []
                if is_parent_type:
                    children = (await session.execute(
                        select(Task).where(Task.parent_task_id == task.id)
                    )).scalars().all()
                if children:
                    retried = await _requeue_parent_task(task, session)
                    changes["status"] = TaskStatus.EVALUATING
                    changes["retried_children"] = retried
                else:
                    old_vm_id = task.vm_instance_id
                    task.attempt_count = 0
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    task.last_heartbeat = None
                    changes["attempt_count"] = 0
                    changes["vm_instance_id"] = task.vm_instance_id
                    changes["_old_vm_to_delete"] = old_vm_id
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status")

    if "requires_approval" in payload:
        was_blocked = task.requires_approval
        task.requires_approval = bool(payload["requires_approval"])
        changes["requires_approval"] = task.requires_approval

        if was_blocked and not task.requires_approval:
            logger.info(f"[ADMIN] Task {task.id} manually approved (was blocked: {task.latest_status_message or 'unknown reason'})")

        # Approval flow: if unblocking a completed task with a PR, continue pipeline
        if was_blocked and not task.requires_approval and task.status in (TaskStatus.COMPLETED, TaskStatus.EVALUATING):
            artifact = (await session.execute(
                select(Artifact).where(Artifact.task_id == task.id,
                                       Artifact.artifact_type == ArtifactType.PULL_REQUEST)
            )).scalars().first()
            if artifact and artifact.pr_url:
                pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
                branch = f"blueprint/task-{str(task.id).replace('-','')[:8]}"
                task_payload = (task.payloads or [{}])[0]
                base_branch = task_payload.get("base_branch", "main")

                ctx = task.context or {}
                if ctx.get("touched_critical"):
                    # Critical file approval: review already passed, merge the PR
                    try:
                        from app.main import get_github_token_for_task
                        from app.github import merge_pull_request, create_pr_comment, delete_branch
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)
                        if merge_result.get("merged"):
                            task.status = TaskStatus.COMPLETED
                            task.context = {**ctx, "pr_merged": True}
                            task.latest_status_message = f"Human-approved merge of PR #{pr_number}"
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                "**Blueprint Review Pipeline**: Human-approved merge (security-critical files).")
                            try:
                                await delete_branch(gh_token, owner, repo, branch)
                            except Exception:
                                pass
                            changes["merged_pr"] = pr_number
                            logger.info(f"[ADMIN] Human-approved merge of PR #{pr_number} (critical files)")
                        else:
                            changes["merge_error"] = merge_result.get("message", "unknown")
                            task.latest_status_message = f"Approved but merge failed: {merge_result.get('message', 'unknown')}"
                            logger.error(f"[ADMIN] Approved merge of PR #{pr_number} failed: {merge_result.get('message')}")
                    except Exception as e:
                        changes["merge_error"] = str(e)[:200]
                        task.latest_status_message = f"Approved but merge failed: {str(e)[:200]}"
                        logger.error(f"[ADMIN] Approved merge failed: {e}", exc_info=True)
                else:
                    # Security scan approval: review hasn't happened yet, spawn it
                    from app.main import _spawn_pipeline_task
                    original_prompt = task_payload.get("prompt", task.description or "")
                    review_task = await _spawn_pipeline_task(
                        task, "review", session, pr_number, branch, base_branch,
                        original_prompt=original_prompt, original_task_id=str(task.id)
                    )
                    if review_task:
                        changes["spawned_review"] = str(review_task.id)
                    else:
                        # Iteration cap reached — revert requires_approval and return error
                        task.requires_approval = True
                        task.status = TaskStatus.FAILED
                        task.latest_status_message = "Iteration cap reached — no follow-on task spawned"
                        session.add(task)
                        await session.commit()
                        raise HTTPException(
                            status_code=422,
                            detail="Iteration cap reached — cannot spawn follow-on task. Archive this task or reset iteration count."
                        )
                # Task stays COMPLETED regardless
                if "status" in payload and TaskStatus(payload["status"]) == TaskStatus.QUEUED:
                    task.status = TaskStatus.COMPLETED
                    changes["status"] = TaskStatus.COMPLETED

    if "payloads" in payload:
        task.payloads = payload["payloads"]
        changes["payloads"] = "updated"

    if "parent_task_id" in payload:
        task.parent_task_id = uuid.UUID(payload["parent_task_id"]) if payload["parent_task_id"] else None
        changes["parent_task_id"] = str(task.parent_task_id)

    if "context" in payload and isinstance(payload["context"], dict):
        merged = {**(task.context or {}), **payload["context"]}
        if len(json.dumps(merged)) > 102400:
            raise HTTPException(status_code=400, detail="Merged context exceeds 100KB limit")
        task.context = merged
        changes["context"] = "updated"

    if "depends_on" in payload:
        new_deps = payload["depends_on"] if isinstance(payload["depends_on"], list) else []
        # Validate: all dependency UUIDs must exist and not create cycles
        if new_deps:
            try:
                dep_uuids = [uuid.UUID(d) for d in new_deps]
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="Invalid UUID in depends_on")
            # Check all referenced tasks exist
            existing = (await session.execute(
                select(Task.id).where(Task.id.in_(dep_uuids))
            )).scalars().all()
            if len(existing) != len(dep_uuids):
                raise HTTPException(status_code=400, detail="One or more depends_on task IDs do not exist")
            # Cycle detection: walk depends_on graph from each dep to see if we reach task.id
            task_id_str = str(task.id)
            async def _has_path_to(start_id: str, target_id: str, visited: set, depth: int = 0) -> bool:
                if depth > 50:
                    return False  # DAG too deep, treat as no cycle
                if start_id == target_id:
                    return True
                if start_id in visited:
                    return False
                visited.add(start_id)
                dep_task = await session.get(Task, uuid.UUID(start_id))
                if not dep_task:
                    return False
                for d in (dep_task.depends_on or []):
                    if await _has_path_to(d, target_id, visited, depth + 1):
                        return True
                return False
            for dep_id in new_deps:
                if await _has_path_to(dep_id, task_id_str, set()):
                    raise HTTPException(status_code=400, detail=f"Circular dependency detected: {dep_id} eventually depends on {task_id_str}")
        task.depends_on = new_deps
        changes["depends_on"] = task.depends_on

    if changes:
        session.add(task)

        history = TaskHistory(
            task_id=task.id,
            action="MODIFIED",
            details={"changes": changes, "modified_by": "USER"}
        )
        session.add(history)
        await session.commit()
        await session.refresh(task)

        # Delete old VM if a requeue overwrote vm_instance_id
        old_vm = changes.pop("_old_vm_to_delete", None)
        if old_vm:
            from app.main import _ensure_vm_deleted
            await _ensure_vm_deleted(old_vm, reason="admin_requeue")

        # Trigger rollup check if child status/context changed
        from app.main import _check_and_trigger_rollup, _cleanup_branches
        should_check_rollup = False
        if "status" in payload and task.parent_task_id:
            new_status = TaskStatus(payload["status"])
            if new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                should_check_rollup = True
            # Clean up staging branches when parent task is cancelled/failed
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                ctx = task.context or {}
                if ctx.get("plan_branch") or ctx.get("research_branch"):
                    await _cleanup_branches(task, session)
        if "context" in payload and task.parent_task_id and isinstance(payload["context"], dict) and payload["context"].get("pr_merged"):
            # pr_merged was set via admin → trigger rollup
            should_check_rollup = True
        if should_check_rollup:
            await _check_and_trigger_rollup(task, session)

    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "title": task.title,
        "status": task.status,
        "vm_instance_id": task.vm_instance_id,
        "attempt_count": task.attempt_count,
        "requires_approval": task.requires_approval,
    }


@router.post("/projects/{project_id}/clear-wave-deps")
async def clear_wave_deps(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Clear depends_on on all QUEUED tasks in a project to unblock wave-gated tasks."""
    await _get_user_project(project_id, current_user["user_id"], session)
    result = await session.execute(
        select(Task).where(Task.project_id == project_id, Task.status == TaskStatus.QUEUED)
    )
    tasks = result.scalars().all()
    cleared = 0
    for task in tasks:
        if task.depends_on:
            task.depends_on = []
            session.add(task)
            cleared += 1
    await session.commit()
    return {"cleared": cleared, "total_queued": len(tasks)}


@router.post("/tasks/{task_id}/rollup")
async def trigger_rollup(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Manually trigger rollup PR for an EVALUATING parent task."""
    from app.main import get_github_token_for_task, _spawn_pipeline_task
    from app.github import create_pull_request, merge_pull_request, delete_branch

    task = await _get_user_task(task_id, current_user["user_id"], session)
    if task.status != TaskStatus.EVALUATING:
        raise HTTPException(status_code=400, detail=f"Task is {task.status}, not EVALUATING")

    ctx = task.context or {}
    staging_branch = ctx.get("plan_branch") or ctx.get("research_branch")
    target_branch = ctx.get("parent_base") or ctx.get("original_base")

    if not staging_branch or not target_branch:
        raise HTTPException(status_code=400, detail="Task has no staging/target branch info in context")

    gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    # Count children
    from sqlmodel import select
    children = (await session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )).scalars().all()

    pr_title = f"[Blueprint] Rollup: {task.title}"
    pr_body = f"Rollup of {len(children)} subtasks from `{staging_branch}`\n\n---\n*Automated by Blueprint Pipeline*"

    pr_result = await create_pull_request(gh_token, owner, repo,
        title=pr_title, head=staging_branch, base=target_branch, body=pr_body)
    rollup_pr_url = pr_result.get("html_url", "")
    rollup_pr_number = pr_result.get("number")

    is_target_intermediate = target_branch.startswith("research/") or target_branch.startswith("plan/")

    if is_target_intermediate:
        merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
        if merge_result.get("merged"):
            task.status = TaskStatus.COMPLETED
            task.context = {**ctx, "pr_merged": True, "rollup_pr_url": rollup_pr_url}
            task.latest_status_message = f"Rollup PR auto-merged into {target_branch}"
            await delete_branch(gh_token, owner, repo, staging_branch)
        else:
            task.latest_status_message = f"Rollup merge failed: {merge_result.get('message')}"
    else:
        task.context = {**ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
        review_task = await _spawn_pipeline_task(
            task, "review", session, rollup_pr_number, staging_branch, target_branch,
            original_prompt=task.description or task.title,
            original_task_id=str(task.id)
        )
        if review_task:
            task.latest_status_message = f"Rollup PR #{rollup_pr_number} created, review spawned → {review_task.id}"
        else:
            task.status = TaskStatus.FAILED
            task.latest_status_message = f"Rollup PR #{rollup_pr_number} created but review not spawned (iteration cap)"

    session.add(task)
    await session.commit()

    return {
        "rollup_pr_url": rollup_pr_url,
        "rollup_pr_number": rollup_pr_number,
        "target": target_branch,
        "status": task.status,
    }


@router.get("/settings")
async def get_settings(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.shared_state import runtime_config as _runtime_config
    from app.main import _max_plan_pools
    from app.models import UserIntegration

    # Check if the user has a GitHub integration
    github_account = None
    stmt = select(UserIntegration).where(UserIntegration.user_id == current_user["user_id"])
    integration = (await session.execute(stmt)).scalars().first()
    if integration and integration.github_account_name:
        github_account = integration.github_account_name

    user_pool = _max_plan_pools.get(current_user["user_id"], [])
    return {
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "max_retries": _runtime_config["max_retries"],
        "max_autonomous_tasks_per_hour": _runtime_config["max_autonomous_tasks_per_hour"],
        "max_global_autonomous_tasks_per_hour": _runtime_config.get("max_global_autonomous_tasks_per_hour", 50),
        "autonomous_research_interval_minutes": _runtime_config.get("autonomous_research_interval_minutes", 30),
        "task_type_models": _runtime_config.get("task_type_models", {}),
        "task_type_efforts": _runtime_config.get("task_type_efforts", {}),
        "health_check_urls": _runtime_config.get("health_check_urls", []),
        "agent_prompt_suffixes": _runtime_config.get("agent_prompt_suffixes", {}),
        "autonomous_periodic_tasks": _runtime_config.get("autonomous_periodic_tasks", {}),
        "autonomous_direction_cooldown_minutes": _runtime_config.get("autonomous_direction_cooldown_minutes", 240),
        "autonomous_research_wave_size": _runtime_config.get("autonomous_research_wave_size", 3),
        "github_account": github_account,
        "using_max_plan": len([a for a in user_pool if a.get("enabled", True)]) > 0,
        "max_plan_accounts": [
            {"id": a["id"], "name": a["name"], "enabled": a.get("enabled", True),
             "active_tasks": a["active_tasks"], "total_tasks": a["total_tasks"],
             "token_created_at": a.get("token_created_at")}
            for a in user_pool
        ],
    }


@router.patch("/settings")
@limiter.limit("30/minute")
async def update_settings(
    request: Request,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    from app.shared_state import runtime_config as _runtime_config, runtime_config_lock
    async with runtime_config_lock:
        return await _apply_settings(payload, session, _runtime_config)

async def _apply_settings(payload: Dict[str, Any], session: AsyncSession, _runtime_config: Dict):
    # Snapshot config so we can restore on DB commit failure
    _config_snapshot = dict(_runtime_config)

    if "max_concurrent_vms" in payload:
        try:
            val = int(payload["max_concurrent_vms"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_concurrent_vms must be a number")
        if not (1 <= val <= 100):
            raise HTTPException(status_code=400, detail="max_concurrent_vms must be between 1 and 100")
        _runtime_config["max_concurrent_vms"] = val
        # Clear PAUSED/HALTED state if user explicitly sets VMs > 0
        # Otherwise the next cold start will override back to 0
        if val > 0:
            state = await session.get(DeploymentState, 1)
            if state and state.status in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
                old_status = state.status
                state.status = DeploymentStatus.STABLE
                state.pause_reason = None
                state.paused_at = None
                state.pre_pause_max_vms = None
                session.add(state)
                logger.warning(f"[SETTINGS] Cleared {old_status} state — user set max_vms={val}")
    if "max_retries" in payload:
        try:
            val = int(payload["max_retries"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_retries must be a number")
        if not (0 <= val <= 10):
            raise HTTPException(status_code=400, detail="max_retries must be between 0 and 10")
        _runtime_config["max_retries"] = val
    if "max_autonomous_tasks_per_hour" in payload:
        try:
            val = int(payload["max_autonomous_tasks_per_hour"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_autonomous_tasks_per_hour must be a number")
        if not (1 <= val <= 50):
            raise HTTPException(status_code=400, detail="max_autonomous_tasks_per_hour must be between 1 and 50")
        _runtime_config["max_autonomous_tasks_per_hour"] = val
    if "max_global_autonomous_tasks_per_hour" in payload:
        try:
            val = int(payload["max_global_autonomous_tasks_per_hour"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_global_autonomous_tasks_per_hour must be a number")
        if not (1 <= val <= 500):
            raise HTTPException(status_code=400, detail="max_global_autonomous_tasks_per_hour must be between 1 and 500")
        _runtime_config["max_global_autonomous_tasks_per_hour"] = val
    if "autonomous_research_interval_minutes" in payload:
        try:
            val = int(payload["autonomous_research_interval_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_research_interval_minutes must be a number")
        if not (1 <= val <= 1440):
            raise HTTPException(status_code=400, detail="autonomous_research_interval_minutes must be between 1 and 1440")
        _runtime_config["autonomous_research_interval_minutes"] = val
    VALID_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", ""}
    VALID_EFFORTS = {"low", "medium", "high", "max", ""}
    if "task_type_models" in payload:
        val = payload["task_type_models"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="task_type_models must be a dict")
        if any(v not in VALID_MODELS for v in val.values()):
            raise HTTPException(status_code=400, detail="Invalid model in task_type_models")
        _runtime_config["task_type_models"] = val
    if "task_type_efforts" in payload:
        val = payload["task_type_efforts"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="task_type_efforts must be a dict")
        if any(v not in VALID_EFFORTS for v in val.values()):
            raise HTTPException(status_code=400, detail="Invalid effort in task_type_efforts")
        _runtime_config["task_type_efforts"] = val
    if "agent_prompt_suffixes" in payload:
        val = payload["agent_prompt_suffixes"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="agent_prompt_suffixes must be a dict")
        if any(not isinstance(v, str) for v in val.values()):
            raise HTTPException(status_code=400, detail="agent_prompt_suffixes values must be strings")
        _runtime_config["agent_prompt_suffixes"] = val
    if "autonomous_periodic_tasks" in payload:
        val = payload["autonomous_periodic_tasks"]
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail="autonomous_periodic_tasks must be a dict")
        VALID_TYPES = {"security_audit", "qa", "deps"}
        for type_key, type_cfg in val.items():
            if type_key not in VALID_TYPES:
                raise HTTPException(status_code=400, detail=f"Invalid task type: {type_key}")
            if not isinstance(type_cfg, dict):
                raise HTTPException(status_code=400, detail=f"Config for {type_key} must be a dict")
            if "enabled" in type_cfg and not isinstance(type_cfg["enabled"], bool):
                raise HTTPException(status_code=400, detail=f"enabled for {type_key} must be a boolean")
            if "interval_hours" in type_cfg:
                try:
                    ih = int(type_cfg["interval_hours"])
                except (ValueError, TypeError):
                    raise HTTPException(status_code=400, detail=f"interval_hours for {type_key} must be a number")
                if not (1 <= ih <= 8760):
                    raise HTTPException(status_code=400, detail=f"interval_hours for {type_key} must be between 1 and 8760")
        # Merge into existing config (don't replace keys not in the payload)
        existing = _runtime_config.get("autonomous_periodic_tasks", {})
        for type_key, type_cfg in val.items():
            existing.setdefault(type_key, {}).update(type_cfg)
        _runtime_config["autonomous_periodic_tasks"] = existing
    # Accept both new (minutes) and old (hours) keys for backward compat
    if "autonomous_direction_cooldown_minutes" in payload:
        try:
            val = int(payload["autonomous_direction_cooldown_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_direction_cooldown_minutes must be a number")
        if not (1 <= val <= 525600):
            raise HTTPException(status_code=400, detail="autonomous_direction_cooldown_minutes must be between 1 and 525600")
        _runtime_config["autonomous_direction_cooldown_minutes"] = val
    elif "autonomous_direction_cooldown_hours" in payload:
        try:
            val = int(payload["autonomous_direction_cooldown_hours"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_direction_cooldown_hours must be a number")
        _runtime_config["autonomous_direction_cooldown_minutes"] = val * 60
    if "autonomous_research_wave_size" in payload:
        try:
            val = int(payload["autonomous_research_wave_size"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_research_wave_size must be a number")
        if not (1 <= val <= 50):
            raise HTTPException(status_code=400, detail="autonomous_research_wave_size must be between 1 and 50")
        _runtime_config["autonomous_research_wave_size"] = val
    if "health_check_urls" in payload:
        val = payload["health_check_urls"]
        if not isinstance(val, list):
            raise HTTPException(status_code=400, detail="health_check_urls must be a list")
        if any(not isinstance(u, str) for u in val):
            raise HTTPException(status_code=400, detail="health_check_urls entries must be strings")
        if any(not u.startswith(("http://", "https://")) for u in val if u):
            raise HTTPException(status_code=400, detail="health_check_urls entries must start with http:// or https://")
        _runtime_config["health_check_urls"] = [u for u in val if u]

    # Persist to DB so values survive Cloud Run restarts
    # Always persist all runtime config values (not just changed ones) to ensure consistency
    try:
        # FOR UPDATE prevents concurrent settings writes from racing (multi-instance)
        state = (await session.execute(
            select(DeploymentState).where(DeploymentState.id == 1).with_for_update()
        )).scalars().first()
        if state:
            state.runtime_max_concurrent_vms = _runtime_config["max_concurrent_vms"]
            state.runtime_max_retries = _runtime_config["max_retries"]
            state.runtime_max_autonomous_tasks_per_hour = _runtime_config["max_autonomous_tasks_per_hour"]
            state.runtime_max_global_autonomous_tasks_per_hour = _runtime_config.get("max_global_autonomous_tasks_per_hour")
            state.runtime_autonomous_research_interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
            state.runtime_autonomous_direction_cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
            session.add(state)
            await session.commit()
            logger.info(f"[SETTINGS] Persisted runtime config: max_vms={state.runtime_max_concurrent_vms}")
    except Exception as e:
        # Restore in-memory config to pre-change state on DB failure
        _runtime_config.update(_config_snapshot)
        logger.error(f"[SETTINGS] DB persist failed, rolling back in-memory config: {e}")
        await session.rollback()
        raise HTTPException(status_code=500, detail="Failed to persist settings")

    return _runtime_config


# --- Max Plan Account Pool ---

@router.post("/max-plan-accounts")
@limiter.limit("10/minute")
async def add_max_plan_account(
    request: Request,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Add a Max plan account. Provide 'name' and 'oauth_token' (sk-ant-oat01-... from `claude setup-token`).
    Optionally provide 'claude_config_json' (content of ~/.claude.json for onboarding bypass)."""
    from app.main import _max_plan_pools, _account_by_id
    name = payload.get("name")
    oauth_token = payload.get("oauth_token")
    if not name or not oauth_token:
        raise HTTPException(status_code=400, detail="'name' and 'oauth_token' are required")
    if not oauth_token.startswith("sk-ant-"):
        raise HTTPException(status_code=400, detail="oauth_token should be a token starting with 'sk-ant-' from `claude setup-token`")
    import re
    if not re.fullmatch(r'sk-ant-[A-Za-z0-9_\-]+', oauth_token):
        raise HTTPException(status_code=400, detail="oauth_token contains invalid characters")

    claude_config_json = payload.get("claude_config_json")
    if claude_config_json is not None:
        import json as _json
        try:
            _json.loads(claude_config_json)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="claude_config_json must be valid JSON")

    now = _utcnow()
    db_account = MaxPlanAccount(
        name=name,
        oauth_token=oauth_token,  # Raw token saved temporarily for ID generation
        claude_config_json=claude_config_json,
        token_created_at=now,
        user_id=current_user["user_id"],
    )
    session.add(db_account)
    await session.commit()
    await session.refresh(db_account)

    # Store token in Secret Manager, save resource name in DB
    try:
        from app.secrets_manager import store_user_secret
        secret_name = f"maxplan-{db_account.id}"
        resource_name = await store_user_secret("system", secret_name, oauth_token)
        db_account.oauth_token = resource_name
        session.add(db_account)
        await session.commit()
    except Exception as e:
        logger.warning(f"[POOL] Secret Manager unavailable, token stored in DB directly: {e}")
        # Fallback: raw token stays in DB (backward compat)

    # Sync to in-memory pool (always use the raw token for runtime)
    from app.main import _max_plan_pool_lock
    entry = {
        "id": str(db_account.id),
        "user_id": current_user["user_id"],
        "name": db_account.name,
        "oauth_token": oauth_token,  # In-memory pool always holds the resolved token
        "claude_config_json": db_account.claude_config_json,
        "enabled": db_account.enabled,
        "active_tasks": 0,
        "total_tasks": db_account.total_tasks,
        "token_created_at": now.isoformat(),
    }
    async with _max_plan_pool_lock:
        _max_plan_pools.setdefault(current_user["user_id"], []).append(entry)
        _account_by_id[str(db_account.id)] = entry
    return {"id": str(db_account.id), "name": db_account.name, "message": "Account added to pool"}


@router.delete("/max-plan-accounts/{account_id}")
@limiter.limit("10/minute")
async def remove_max_plan_account(
    request: Request,
    account_id: str,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.main import _max_plan_pools, _account_by_id

    # Delete from database
    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account ID")
    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    if db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    # Check for active tasks using this account
    active_using = (await session.execute(
        select(Task.id).where(
            Task.status == TaskStatus.IN_PROGRESS,
            Task.context["max_plan_account_id"].as_string() == account_id
        )
    )).scalars().all()
    if active_using and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Account has {len(active_using)} active task(s). "
                   f"Pass ?force=true to remove anyway (tasks will fail on next token refresh)."
        )

    await session.delete(db_account)
    await session.commit()

    # Remove from in-memory pool
    from app.main import _max_plan_pool_lock
    async with _max_plan_pool_lock:
        user_id = current_user["user_id"]
        if user_id in _max_plan_pools:
            _max_plan_pools[user_id] = [a for a in _max_plan_pools[user_id] if a["id"] != account_id]
        _account_by_id.pop(account_id, None)
    return {"message": "Account removed"}


@router.patch("/max-plan-accounts/{account_id}")
async def update_max_plan_account(
    account_id: str,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.main import _account_by_id

    # Update in database
    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account ID")
    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    if db_account.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Account not found")

    if "enabled" in payload:
        db_account.enabled = bool(payload["enabled"])
    if "name" in payload:
        db_account.name = payload["name"]
    _raw_new_token = None  # Track raw token for in-memory pool sync
    if "oauth_token" in payload:
        new_token = payload["oauth_token"]
        if not new_token.startswith("sk-ant-"):
            raise HTTPException(status_code=400, detail="oauth_token should start with 'sk-ant-'")
        import re
        if not re.fullmatch(r'sk-ant-[A-Za-z0-9_\-]+', new_token):
            raise HTTPException(status_code=400, detail="oauth_token contains invalid characters")
        _raw_new_token = new_token
        # Store in Secret Manager, save resource name in DB
        try:
            from app.secrets_manager import store_user_secret
            secret_name = f"maxplan-{account_id}"
            resource_name = await store_user_secret("system", secret_name, new_token)
            db_account.oauth_token = resource_name
        except Exception as e:
            logger.warning(f"[POOL] Secret Manager unavailable, token stored in DB directly: {e}")
            db_account.oauth_token = new_token
        db_account.token_created_at = _utcnow()
    if "claude_config_json" in payload:
        import json as _json
        try:
            _json.loads(payload["claude_config_json"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="claude_config_json must be valid JSON")
        db_account.claude_config_json = payload["claude_config_json"]
    db_account.updated_at = _utcnow()
    session.add(db_account)
    await session.commit()

    # Sync to in-memory pool
    from app.main import _max_plan_pool_lock
    async with _max_plan_pool_lock:
        mem_account = _account_by_id.get(account_id)
        if mem_account:
            if "enabled" in payload:
                mem_account["enabled"] = db_account.enabled
            if "name" in payload:
                mem_account["name"] = db_account.name
            if "oauth_token" in payload:
                mem_account["oauth_token"] = _raw_new_token  # In-memory pool holds resolved token, not resource name
                mem_account["token_created_at"] = db_account.token_created_at.isoformat() if db_account.token_created_at else None
            if "claude_config_json" in payload:
                mem_account["claude_config_json"] = db_account.claude_config_json

    return {"id": account_id, "name": db_account.name, "enabled": db_account.enabled}


# --- GitHub Repository & Branch Discovery ---

async def _get_installation_token_for_user(user_id: str, session: AsyncSession) -> str:
    """
    Retrieves a GitHub installation access token for the given user.
    Raises HTTPException if no integration or installation is found.
    """
    stmt = select(UserIntegration).where(UserIntegration.user_id == user_id)
    integration = (await session.execute(stmt)).scalars().first()

    if not integration:
        raise HTTPException(
            status_code=400,
            detail="No GitHub integration found. Please connect your GitHub account first."
        )

    if integration.github_installation_id:
        try:
            token = await generate_installation_access_token(integration.github_installation_id)
            return token
        except HTTPException:
            raise HTTPException(
                status_code=502,
                detail="Failed to generate GitHub installation token. The installation may have been removed."
            )

    raise HTTPException(
        status_code=400,
        detail="No GitHub App installation found for your account. Please install the GitHub App first."
    )


@router.get("/github/repos")
async def list_github_repos(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Lists repositories accessible via the current user's GitHub App installation.
    Returns a sorted list of {full_name, default_branch} objects.
    """
    token = await _get_installation_token_for_user(current_user["user_id"], session)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    repos = []
    url = "https://api.github.com/installation/repositories?per_page=100"

    async with httpx.AsyncClient(timeout=15.0) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.error(f"GitHub API error listing repos: {resp.status_code} {resp.text}")
                raise HTTPException(
                    status_code=502,
                    detail="Failed to fetch repositories from GitHub."
                )
            data = resp.json()
            for r in data.get("repositories", []):
                repos.append({
                    "full_name": r["full_name"],
                    "default_branch": r.get("default_branch", "main"),
                })
            # Follow pagination
            url = resp.links.get("next", {}).get("url")

    repos.sort(key=lambda r: r["full_name"].lower())
    return {"repositories": repos}


@router.get("/github/branches")
async def list_github_branches(
    repo: str = Query(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Lists branches for a specific repository accessible via the user's GitHub App installation.
    Query param: repo=owner/repo
    Returns a sorted list of branch names.
    """
    token = await _get_installation_token_for_user(current_user["user_id"], session)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    branches = []
    url = f"https://api.github.com/repos/{repo}/branches?per_page=100"

    async with httpx.AsyncClient(timeout=15.0) as client:
        while url:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                raise HTTPException(
                    status_code=404,
                    detail=f"Repository '{repo}' not found or not accessible."
                )
            if resp.status_code != 200:
                logger.error(f"GitHub API error listing branches for {repo}: {resp.status_code} {resp.text}")
                raise HTTPException(
                    status_code=502,
                    detail="Failed to fetch branches from GitHub."
                )
            data = resp.json()
            for b in data:
                branches.append(b["name"])
            # Follow pagination
            url = resp.links.get("next", {}).get("url")

    branches.sort(key=str.lower)
    return {"branches": branches}


# --- Project Directions (autonomous improvement steering) ---

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
    project = (await session.execute(
        select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"]).with_for_update()
    )).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

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
    project = (await session.execute(
        select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"]).with_for_update()
    )).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

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
    project = (await session.execute(
        select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"]).with_for_update()
    )).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    directions = [d for d in (project.directions or []) if d.get("id") != direction_id]
    if len(directions) == len(project.directions or []):
        raise HTTPException(status_code=404, detail="Direction not found")

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
    project = (await session.execute(
        select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"]).with_for_update()
    )).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

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
    project = (await session.execute(
        select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"]).with_for_update()
    )).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

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
            if "text" in payload:
                d["text"] = payload["text"]
            if "paths" in payload:
                d["paths"] = payload["paths"]
            if "pending_update" in payload:
                d["pending_update"] = payload["pending_update"]
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
    project = await _get_user_project(project_id, current_user["user_id"], session)

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
        payload = (task.payloads or [{}])[0]
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
    project = await _get_user_project(project_id, current_user["user_id"], session)

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
        select(Project).where(Project.user_id == current_user["user_id"]).order_by(Project.created_at.desc())
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
        if ((t.payloads or [{}])[0]).get("type") == "research":
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


@router.post("/restart-orchestrator")
async def restart_orchestrator(
    current_user: dict = Depends(require_admin)
):
    """Restart the orchestrator background task without restarting the entire service."""
    import asyncio
    from app.shared_state import background_tasks
    from app.main import orchestrator_worker

    old_task = background_tasks.get("orchestrator")
    if old_task and not old_task.done():
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

    background_tasks["orchestrator"] = asyncio.create_task(orchestrator_worker())
    logger.info("[ADMIN] Orchestrator restarted via admin endpoint")
    return {"status": "restarted"}


# --- Deployment State & Health ---

def _get_zone_health_summary() -> dict:
    try:
        from app.compute import get_zone_health_summary
        return get_zone_health_summary()
    except Exception:
        return {"total_zones": 0, "available": 0, "exhausted": []}


async def build_deploy_state_payload(session: AsyncSession) -> dict:
    """Assemble the deploy state payload. Used by both HTTP endpoint and WS broadcasts."""
    import os
    from app.shared_state import deploy_monitoring, external_health, runtime_config
    import app.main as _main_module

    state = await session.get(DeploymentState, 1)
    if not state:
        return {"error": "No deployment state found — service may not have initialized yet"}

    # Calculate monitoring window progress
    window_progress = None
    if deploy_monitoring["window_start"]:
        duration = deploy_monitoring["extended_window"] if deploy_monitoring["is_extended"] else deploy_monitoring["window_duration"]
        elapsed = (_utcnow() - deploy_monitoring["window_start"]).total_seconds()
        window_progress = {"elapsed_seconds": int(elapsed), "total_seconds": duration, "percent": min(100, int(elapsed / duration * 100))}

    # Calculate recent failure rate
    results = deploy_monitoring["task_results"]
    failure_rate = 0.0
    if results:
        failures = sum(1 for _, outcome, _ in results if outcome == "failure")
        failure_rate = failures / len(results)

    return {
        "current_revision": state.current_revision,
        "current_sha": state.current_sha,
        "deployed_at": state.deployed_at.isoformat() if state.deployed_at else None,
        "status": state.status,
        "last_known_good_revision": state.last_known_good_revision,
        "last_known_good_sha": state.last_known_good_sha,
        "rolled_back_from_sha": state.rolled_back_from_sha,
        "rollback_reason": state.rollback_reason,
        "rollback_count": state.rollback_count,
        "pause_reason": state.pause_reason,
        "paused_at": state.paused_at.isoformat() if state.paused_at else None,
        "reason": state.pause_reason if state.status == DeploymentStatus.PAUSED else None,
        "pre_pause_max_vms": state.pre_pause_max_vms,
        "monitoring_window": window_progress,
        "failure_rate": round(failure_rate, 3),
        "task_results_count": len(results),
        "external_health": external_health,
        "max_concurrent_vms": runtime_config["max_concurrent_vms"],
        "zone_health": _get_zone_health_summary(),
        "gcp_project_id": os.getenv("GCP_PROJECT_ID", ""),
        "health_recovery_streak": getattr(_main_module, "_health_recovery_streak", 0),
        "health_recovery_streak_required": 3,
        "health_failure_streak": getattr(_main_module, "_pause_failure_streak", 0),
        "health_failure_streak_threshold": 3,
    }


@router.get("/deploy-state")
async def get_deploy_state(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Get current deployment state including monitoring status and health."""
    return await build_deploy_state_payload(session)


@router.post("/deploy-state/pause")
async def pause_pipeline(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Manually pause the pipeline (set max_concurrent_vms=0). Used for manual intervention."""
    from app.shared_state import runtime_config

    state = await session.get(DeploymentState, 1)
    if state:
        state.status = DeploymentStatus.PAUSED
        state.pause_reason = "Manual pause via admin"
        state.paused_at = _utcnow()
        state.pre_pause_max_vms = runtime_config["max_concurrent_vms"]
        state.runtime_max_concurrent_vms = runtime_config["max_concurrent_vms"]
        session.add(state)
        await session.commit()

    old_vms = runtime_config["max_concurrent_vms"]
    runtime_config["max_concurrent_vms"] = 0
    logger.warning(f"[ADMIN] Pipeline PAUSED manually (was max_concurrent_vms={old_vms})")
    from app.main import broadcast_deploy_state
    await broadcast_deploy_state()
    return {"status": "paused", "previous_max_vms": old_vms}


@router.post("/deploy-state/resume")
async def resume_pipeline(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Resume the pipeline after a pause. Restores max_concurrent_vms to pre-pause value."""
    from app.shared_state import runtime_config

    state = await session.get(DeploymentState, 1)
    if not state:
        raise HTTPException(status_code=404, detail="No deployment state found")

    # Validate current state — only resume from PAUSED
    if state.status == DeploymentStatus.HALTED:
        raise HTTPException(status_code=400, detail="Pipeline is HALTED (3+ consecutive rollbacks). Use mark-stable to acknowledge before resuming.")
    if state.status not in (DeploymentStatus.PAUSED, DeploymentStatus.ROLLED_BACK):
        raise HTTPException(status_code=400, detail=f"Pipeline is {state.status}, not paused — nothing to resume")

    restore_vms = state.pre_pause_max_vms or state.runtime_max_concurrent_vms or 5
    state.status = DeploymentStatus.STABLE
    state.pause_reason = None
    state.paused_at = None
    state.pre_pause_max_vms = None
    session.add(state)
    await session.commit()

    runtime_config["max_concurrent_vms"] = restore_vms
    state.runtime_max_concurrent_vms = restore_vms
    session.add(state)
    await session.commit()
    logger.warning(f"[ADMIN] Pipeline RESUMED (max_concurrent_vms={restore_vms})")
    from app.main import broadcast_deploy_state
    await broadcast_deploy_state()
    return {"status": "resumed", "max_concurrent_vms": restore_vms}


@router.post("/deploy-state/reset-health")
async def reset_deploy_health(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Force-reset health state and resume the pipeline. Emergency operator escape hatch."""
    from app.shared_state import runtime_config, external_health
    import app.main as _main_module

    state = await session.get(DeploymentState, 1)
    if not state:
        raise HTTPException(status_code=404, detail="No deployment state found")

    if state.status != DeploymentStatus.PAUSED:
        raise HTTPException(status_code=400, detail=f"Pipeline is {state.status}, not PAUSED — reset-health only applies to PAUSED state")

    restore_vms = state.pre_pause_max_vms or state.runtime_max_concurrent_vms or 5

    # Clear external health failures
    external_health["failed_deps"] = []
    for key in external_health:
        if key != "failed_deps" and isinstance(external_health[key], bool):
            external_health[key] = True

    # Reset in-memory streak counters in main.py
    try:
        _main_module._pause_failure_streak = 0
        _main_module._health_recovery_streak = 0
    except Exception:
        pass

    # Restore VM count and clear pause fields
    runtime_config["max_concurrent_vms"] = restore_vms
    state.runtime_max_concurrent_vms = restore_vms
    state.status = DeploymentStatus.STABLE
    state.pause_reason = None
    state.paused_at = None
    state.pre_pause_max_vms = None
    session.add(state)
    await session.commit()

    logger.warning(f"[ADMIN] Health force-reset — pipeline RESUMED (max_concurrent_vms={restore_vms})")
    from app.main import broadcast_deploy_state
    await broadcast_deploy_state()
    return await build_deploy_state_payload(session)


@router.post("/deploy-state/mark-stable")
async def mark_deploy_stable(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Manually mark the current deploy as stable (skip monitoring window)."""
    from app.shared_state import deploy_monitoring

    state = await session.get(DeploymentState, 1)
    if not state:
        raise HTTPException(status_code=404, detail="No deployment state found")

    state.status = DeploymentStatus.STABLE
    state.last_known_good_revision = state.current_revision
    state.last_known_good_sha = state.current_sha
    state.rollback_count = 0
    state.rollback_reason = None
    session.add(state)
    await session.commit()

    deploy_monitoring["window_start"] = None
    logger.warning(f"[ADMIN] Deploy manually marked STABLE (sha={state.current_sha[:8]})")
    from app.main import broadcast_deploy_state
    await broadcast_deploy_state()
    return {"status": "stable", "sha": state.current_sha}


@router.post("/deploy-state/rollback")
async def trigger_rollback(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """One-click rollback: shift Cloud Run traffic to last_known_good_revision.
    Pauses the pipeline and records the rollback in deployment state."""
    import subprocess
    from app.shared_state import runtime_config, deploy_monitoring

    state = await session.get(DeploymentState, 1)
    if not state:
        raise HTTPException(status_code=404, detail="No deployment state found")

    if not state.last_known_good_revision or state.last_known_good_revision == state.current_revision:
        raise HTTPException(status_code=400, detail="No previous good revision to rollback to")

    # Guard against double-rollback
    if state.status == DeploymentStatus.ROLLED_BACK:
        raise HTTPException(status_code=400, detail="Already rolled back — use resume or mark-stable first")

    # Get causation analysis for the rollback reason
    from app.main import should_rollback
    _, analysis_reason, _ = should_rollback()

    # Attempt Cloud Run traffic shift via gcloud
    import os
    project_id = os.getenv("GCP_PROJECT_ID", "")
    region = os.getenv("GCP_REGION", "us-central1")
    service = "blueprint-orchestrator"

    rollback_revision = state.last_known_good_revision
    rollback_error = None

    try:
        def _run_gcloud_rollback():
            return subprocess.run(
                ["gcloud", "run", "services", "update-traffic", service,
                 "--to-revisions", f"{rollback_revision}=100",
                 "--region", region, "--project", project_id],
                capture_output=True, text=True, timeout=30
            )
        result = await asyncio.to_thread(_run_gcloud_rollback)
        if result.returncode != 0:
            rollback_error = result.stderr[:500]
            logger.error(f"[ADMIN] Cloud Run rollback failed: {rollback_error}")
    except Exception as e:
        rollback_error = str(e)[:500]
        logger.error(f"[ADMIN] Rollback command failed: {e}", exc_info=True)

    # Update deployment state regardless of gcloud result
    state.status = DeploymentStatus.ROLLED_BACK
    state.rolled_back_from_sha = state.current_sha
    state.rollback_reason = f"Manual rollback: {analysis_reason}"
    state.rollback_count += 1
    state.pre_pause_max_vms = runtime_config["max_concurrent_vms"]
    session.add(state)
    await session.commit()

    # Pause pipeline
    runtime_config["max_concurrent_vms"] = 0
    deploy_monitoring["window_start"] = None

    logger.warning(f"[ADMIN] ROLLBACK triggered: {state.current_sha[:8]} → {state.last_known_good_sha[:8]}")
    from app.main import broadcast_deploy_state
    await broadcast_deploy_state()

    return {
        "status": "rolled_back" if not rollback_error else "rollback_requested",
        "from_sha": state.current_sha,
        "to_revision": rollback_revision,
        "to_sha": state.last_known_good_sha,
        "reason": analysis_reason,
        "gcloud_error": rollback_error,
        "pipeline_paused": True,
    }


@router.post("/repair-state")
async def repair_state(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Post-rollback state audit and repair. Fixes stuck tasks, orphaned parents, stale evaluating tasks."""
    from app.models import TaskHistory
    repairs = []

    # 1. Tasks stuck in PROVISIONING/IN_PROGRESS with no heartbeat for 15+ min → FAILED
    fifteen_min_ago = _utcnow() - timedelta(minutes=15)
    stuck_tasks = (await session.execute(
        select(Task).where(
            Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
            Task.vm_instance_id.isnot(None),
        )
    )).scalars().all()

    for t in stuck_tasks:
        cutoff = t.last_heartbeat or t.started_at or t.created_at
        if cutoff and cutoff < fifteen_min_ago:
            original_status = t.status  # Capture before overwriting
            t.status = TaskStatus.FAILED
            t.latest_status_message = f"Repaired: stuck in {original_status} with no heartbeat since {cutoff.isoformat()}"
            session.add(t)
            session.add(TaskHistory(task_id=t.id, action="REPAIR", details={"reason": "stuck_no_heartbeat", "original_status": str(original_status)}))
            repairs.append(f"Failed stuck task {t.id}")

            # Clean up VM
            if t.vm_instance_id:
                from app.main import _ensure_vm_deleted
                await _ensure_vm_deleted(t, reason="admin_repair")

    # 2. EVALUATING tasks where all children are terminal but parent not updated
    evaluating_tasks = (await session.execute(
        select(Task).where(Task.status == TaskStatus.EVALUATING)
    )).scalars().all()

    for parent in evaluating_tasks:
        children = (await session.execute(
            select(Task).where(Task.parent_task_id == parent.id)
        )).scalars().all()

        if not children:
            continue

        all_terminal = all(c.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED] for c in children)
        if all_terminal:
            # Re-trigger rollup by setting back to EVALUATING (the orchestrator will pick it up)
            parent.latest_status_message = "Repaired: all children terminal, re-triggering rollup"
            session.add(parent)
            session.add(TaskHistory(task_id=parent.id, action="REPAIR", details={"reason": "stale_evaluating"}))
            repairs.append(f"Re-triggered rollup for {parent.id}")

    # 3. Tasks with parent_task_id pointing to non-existent tasks
    all_task_ids = {str(t.id) for t in (await session.execute(select(Task.id))).scalars().all()}
    orphan_children = (await session.execute(
        select(Task).where(Task.parent_task_id.isnot(None))
    )).scalars().all()

    for t in orphan_children:
        if str(t.parent_task_id) not in all_task_ids:
            t.parent_task_id = None
            t.latest_status_message = "Repaired: cleared orphaned parent reference"
            session.add(t)
            session.add(TaskHistory(task_id=t.id, action="REPAIR", details={"reason": "orphaned_parent_ref"}))
            repairs.append(f"Cleared orphaned parent ref on {t.id}")

    # 4. Circuit breakers stuck in HALF_OPEN with a deleted/non-existent test task
    from app.main import _maybe_create_circuit_breaker_alert, _utcnow as _main_utcnow
    for proj_id, breaker in list(_circuit_breakers.items()):
        if breaker.get("state") != "HALF_OPEN":
            continue
        test_id = breaker.get("half_open_task_id")
        if not test_id:
            continue
        try:
            test_task = await session.get(Task, uuid.UUID(test_id))
        except Exception:
            test_task = None
        if test_task is None:
            breaker["state"] = "OPEN"
            breaker["opened_at"] = _main_utcnow()
            breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
            breaker["half_open_task_id"] = None
            logger.warning(f"[ADMIN REPAIR] Circuit breaker for {proj_id} stuck in HALF_OPEN — test task {test_id} not found, resetting to OPEN")
            try:
                project_uuid = uuid.UUID(proj_id)
                await _maybe_create_circuit_breaker_alert(
                    project_uuid, session,
                    f"HALF_OPEN test task deleted/reaped (admin repair) — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
            except Exception:
                pass
            repairs.append(f"Reset HALF_OPEN circuit breaker for project {proj_id} (test task {test_id} not found)")

    await session.commit()
    logger.warning(f"[ADMIN] State repair completed: {len(repairs)} repairs")
    return {"repairs": repairs, "count": len(repairs)}


@router.get("/activity")
async def get_activity(
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    event_types: Optional[List[str]] = Query(None),
    since: Optional[datetime] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Return a reverse-chronological list of recent system events."""
    seven_days_ago = _utcnow() - timedelta(days=7)
    cutoff = since if since is not None else seven_days_ago
    events: List[Dict[str, Any]] = []

    # --- 1 & 2: Completed and Failed tasks in last 7 days (or since cutoff) ---
    user_project_ids = _user_project_ids(current_user["user_id"])
    task_filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        Task.created_at >= cutoff,
        Task.project_id.in_(user_project_ids),
    ]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    tasks_result = await session.execute(
        select(Task).where(*task_filters)
    )
    recent_tasks = tasks_result.scalars().all()

    task_ids = [t.id for t in recent_tasks] if recent_tasks else []

    # Fetch relevant artifacts in one query
    artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> artifact info
    research_artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> research artifact
    error_log_map: Dict[str, str] = {}  # task_id -> truncated error logs
    investigation_report_map: Dict[str, str] = {}  # task_id -> summary text

    if task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(task_ids),
                Artifact.artifact_type.in_([
                    ArtifactType.PULL_REQUEST,
                    ArtifactType.ERROR_LOG,
                    ArtifactType.RESEARCH_FINDINGS,
                    ArtifactType.INVESTIGATION_REPORT,
                ])
            )
            .order_by(Artifact.created_at.asc())
        )
        for a in artifacts_result.scalars().all():
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.PULL_REQUEST:
                artifact_map[tid] = {
                    "pr_url": a.pr_url,
                    "repo_url": a.repo_url,
                    "commit_hash": a.commit_hash[:7] if a.commit_hash else None,
                }
            elif a.artifact_type == ArtifactType.ERROR_LOG and a.logs and tid not in error_log_map:
                error_log_map[tid] = a.logs[:300]
            elif a.artifact_type == ArtifactType.RESEARCH_FINDINGS:
                research_artifact_map[tid] = {
                    "artifact_created_at": a.created_at,
                    "finding_count": _count_research_findings(a.logs),
                }
            elif a.artifact_type == ArtifactType.INVESTIGATION_REPORT and a.logs:
                investigation_report_map[tid] = a.logs[:2000]

    for t in recent_tasks:
        tid = str(t.id)
        task_type = ((t.payloads or [{}])[0]).get("type", "create")
        pr_info = artifact_map.get(tid, {})

        # Determine timestamp: last status_log entry or updated_at fallback
        completed_at = None
        if t.status_log:
            last_entry = t.status_log[-1]
            try:
                completed_at = datetime.fromisoformat(last_entry["t"])
            except (KeyError, ValueError):
                pass
        if completed_at is None:
            # fall back to created_at since Task has no updated_at column
            completed_at = t.created_at

        duration_seconds = None
        if completed_at is not None and t.started_at is not None:
            duration_seconds = (completed_at - t.started_at).total_seconds()

        if t.status == TaskStatus.COMPLETED:
            events.append({
                "id": f"task-{tid}",
                "type": "TASK_COMPLETED",
                "timestamp": completed_at.isoformat() if completed_at else None,
                "title": t.title or "Completed task",
                "task_id": tid,
                "task_type": task_type,
                "pr_url": pr_info.get("pr_url"),
                "repo_url": pr_info.get("repo_url"),
                "commit_hash": pr_info.get("commit_hash"),
                "error_summary": None,
                "finding_count": None,
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
                "duration_seconds": duration_seconds,
            })
        elif t.status == TaskStatus.FAILED:
            events.append({
                "id": f"task-{tid}",
                "type": "TASK_FAILED",
                "timestamp": completed_at.isoformat() if completed_at else None,
                "title": t.title or "Failed task",
                "task_id": tid,
                "task_type": task_type,
                "pr_url": None,
                "repo_url": None,
                "commit_hash": None,
                "error_summary": error_log_map.get(tid),
                "finding_count": None,
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
                "duration_seconds": duration_seconds,
            })

        # --- 3: Research findings events (one per task) ---
        if tid in research_artifact_map:
            ra = research_artifact_map[tid]
            ra_ts = ra["artifact_created_at"]
            events.append({
                "id": f"research-{tid}",
                "type": "RESEARCH_FINDINGS",
                "timestamp": ra_ts.isoformat() if ra_ts else None,
                "title": f"Research findings: {t.title or tid}",
                "task_id": tid,
                "task_type": task_type,
                "pr_url": None,
                "repo_url": None,
                "commit_hash": None,
                "error_summary": None,
                "finding_count": ra["finding_count"],
                "investigation_summary": investigation_report_map.get(tid),
                "deploy_sha": None,
                "deploy_status": None,
                "rollback_count": None,
            })

    # --- 3b: In-progress tasks (QUEUED / PROVISIONING / IN_PROGRESS) ---
    inprogress_filters = [
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        Task.project_id.in_(user_project_ids),
    ]
    if project_id:
        inprogress_filters.append(Task.project_id == project_id)
    if since is not None:
        inprogress_filters.append(Task.created_at >= since)

    inprogress_result = await session.execute(
        select(Task).where(*inprogress_filters)
    )
    inprogress_tasks = inprogress_result.scalars().all()

    for t in inprogress_tasks:
        tid = str(t.id)
        task_type = ((t.payloads or [{}])[0]).get("type", "create")
        events.append({
            "id": f"inprogress-{tid}",
            "type": "TASK_IN_PROGRESS",
            "timestamp": t.started_at.isoformat() if t.started_at else t.created_at.isoformat(),
            "title": t.title or "In-progress task",
            "task_id": tid,
            "task_type": task_type,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "latest_status_message": t.latest_status_message,
            "pr_url": None,
            "repo_url": None,
            "commit_hash": None,
            "error_summary": None,
            "finding_count": None,
            "deploy_sha": None,
            "deploy_status": None,
            "rollback_count": None,
        })

    # --- 4: Deploy event from DeploymentState singleton ---
    state = await session.get(DeploymentState, 1)
    if state and state.deployed_at:
        short_sha = state.current_sha[:7] if state.current_sha else None
        deploy_event: Dict[str, Any] = {
            "id": f"deploy-{short_sha or 'unknown'}",
            "type": "DEPLOY_EVENT",
            "timestamp": state.deployed_at.isoformat(),
            "title": f"Deploy {short_sha or 'unknown'} — {state.status}",
            "task_id": None,
            "task_type": None,
            "pr_url": None,
            "repo_url": None,
            "commit_hash": short_sha,
            "error_summary": None,
            "finding_count": None,
            "deploy_sha": short_sha,
            "deploy_status": str(state.status) if state.status else None,
            "rollback_count": state.rollback_count if (state.rollback_count or 0) > 0 else None,
        }
        if (state.rollback_count or 0) > 0 and state.rolled_back_from_sha:
            deploy_event["rolled_back_from_sha"] = state.rolled_back_from_sha[:7]
        events.append(deploy_event)

    # Sort descending by timestamp, nulls last
    events.sort(key=lambda e: e["timestamp"] or "", reverse=True)

    if event_types:
        events = [e for e in events if e["type"] in event_types]

    total = len(events)
    return {"events": events[:limit], "total": total}


def _count_research_findings(logs: Optional[str]) -> int:
    """Parse research findings artifact logs and return finding count."""
    if not logs:
        return 0
    try:
        parsed = json.loads(logs[:100000])
        if isinstance(parsed, list):
            return len(parsed)
        if isinstance(parsed, dict):
            inner = parsed.get("logs", [])
            if isinstance(inner, list):
                return len(inner)
    except (json.JSONDecodeError, AttributeError):
        pass
    return 0


# ── Zone Health ──────────────────────────────────────────────────────────────

@router.get("/zone-health")
async def get_zone_health_endpoint(
    current_user: dict = Depends(get_current_user),
):
    """Get GCP zone health status for VM provisioning."""
    from app.compute import get_zone_health
    return get_zone_health()


# ── Alerts ───────────────────────────────────────────────────────────────────

@router.get("/alerts")
async def get_alerts(
    project_id: Optional[str] = None,
    include_dismissed: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    query = select(Alert).where(Alert.project_id.in_(_user_project_ids(current_user["user_id"]))).order_by(Alert.created_at.desc())
    if project_id:
        query = query.where(Alert.project_id == uuid.UUID(project_id))
    if not include_dismissed:
        query = query.where(Alert.dismissed_at.is_(None))
    query = query.limit(limit).offset(offset)
    result = await session.execute(query)
    alerts = result.scalars().all()
    return [
        {
            "id": str(a.id),
            "project_id": str(a.project_id),
            "severity": a.severity,
            "title": a.title,
            "message": a.message,
            "task_id": str(a.task_id) if a.task_id else None,
            "created_at": a.created_at.isoformat(),
            "dismissed_at": a.dismissed_at.isoformat() if a.dismissed_at else None,
        }
        for a in alerts
    ]


@router.post("/alerts/{alert_id}/dismiss")
async def dismiss_alert(
    alert_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    alert = await session.get(Alert, uuid.UUID(alert_id))
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    # Verify alert belongs to user's project
    project = await session.get(Project, alert.project_id)
    if not project or project.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.dismissed_at = _utcnow()
    session.add(alert)
    await session.commit()
    return {"status": "dismissed"}


@router.post("/alerts/dismiss-all")
async def dismiss_all_alerts(
    project_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    stmt = update(Alert).where(Alert.dismissed_at.is_(None), Alert.project_id.in_(_user_project_ids(current_user["user_id"])))
    if project_id:
        stmt = stmt.where(Alert.project_id == project_id)
    stmt = stmt.values(dismissed_at=_utcnow())
    result = await session.execute(stmt)
    await session.commit()
    return {"dismissed_count": result.rowcount}


# ── Autonomy Debug ────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/autonomy-debug")
async def get_autonomy_debug(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a detailed snapshot of why autonomy is or isn't spawning for a project."""
    from app.shared_state import runtime_config as _runtime_config, get_circuit_breaker
    import app.shared_state as _shared_state_mod

    project = await _get_user_project(project_id, current_user["user_id"], session)

    now = _utcnow()
    blocking_guards = []

    # Guard 1: autonomous_disabled
    if not project.autonomous_enabled:
        blocking_guards.append({
            "guard": "autonomous_disabled",
            "reason": "project.autonomous_enabled is False",
        })

    # Guard 2: circuit_breaker_open
    raw_breaker = get_circuit_breaker(str(project_id))
    breaker_state = raw_breaker["state"]
    cooldown_ends_at = None
    if raw_breaker.get("opened_at"):
        cooldown_end = raw_breaker["opened_at"] + timedelta(seconds=raw_breaker["cooldown_seconds"])
        cooldown_ends_at = cooldown_end.isoformat()
        if breaker_state == "OPEN" and now < cooldown_end:
            blocking_guards.append({
                "guard": "circuit_breaker_open",
                "reason": "Circuit breaker is OPEN and cooldown has not elapsed",
                "opened_at": raw_breaker["opened_at"].isoformat(),
                "cooldown_ends_at": cooldown_ends_at,
                "failure_count": raw_breaker["failure_count"],
            })

    circuit_breaker_info = {
        "state": breaker_state,
        "failure_count": raw_breaker["failure_count"],
        "success_count": raw_breaker["success_count"],
        "opened_at": raw_breaker["opened_at"].isoformat() if raw_breaker.get("opened_at") else None,
        "cooldown_seconds": raw_breaker["cooldown_seconds"],
        "cooldown_ends_at": cooldown_ends_at,
    }

    # Guard 3: no_eligible_directions
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    cooldown_seconds_dir = cooldown_minutes * 60
    directions_info = []
    for d in (project.directions or []):
        if d.get("status") != "active":
            continue
        last_researched_at = d.get("last_researched_at")
        if last_researched_at:
            last_dt = datetime.fromisoformat(last_researched_at)
            elapsed = (now - last_dt).total_seconds()
            remaining = cooldown_seconds_dir - elapsed
            is_eligible = remaining <= 0
            seconds_until_eligible = max(0, int(remaining)) if not is_eligible else None
        else:
            is_eligible = True
            seconds_until_eligible = None
        directions_info.append({
            "id": d.get("id"),
            "text": d.get("text", ""),
            "status": d.get("status", "active"),
            "last_researched_at": last_researched_at,
            "cooldown_minutes": cooldown_minutes,
            "is_eligible": is_eligible,
            "seconds_until_eligible": seconds_until_eligible,
        })

    eligible_directions = [d for d in directions_info if d["is_eligible"]]
    if not eligible_directions:
        # Either no active directions at all, or all are in cooldown
        min_wait = min(
            (d["seconds_until_eligible"] for d in directions_info if d["seconds_until_eligible"] is not None),
            default=None,
        )
        reason = (
            "No active directions exist"
            if not directions_info
            else "All active directions are still in cooldown"
        )
        blocking_guards.append({
            "guard": "no_eligible_directions",
            "reason": reason,
            "time_until_eligible_seconds": min_wait,
        })

    # Guard 4: research_task_active — only block on actively running research,
    # not EVALUATING parents (they're just waiting on children to finish)
    active_tasks_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )
    active_tasks = active_tasks_result.scalars().all()
    blocking_research_task_id = None
    for t in active_tasks:
        if ((t.payloads or [{}])[0]).get("type") == "research":
            blocking_research_task_id = str(t.id)
            break
    if blocking_research_task_id:
        blocking_guards.append({
            "guard": "research_task_active",
            "reason": "A research task is actively running (QUEUED/PROVISIONING/IN_PROGRESS)",
            "task_id": blocking_research_task_id,
        })

    # Guard 5: rate_limit_exceeded
    one_hour_ago = now - timedelta(hours=1)
    recent_tasks_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )
    recent_tasks = recent_tasks_result.scalars().all()
    tasks_this_hour = sum(1 for t in recent_tasks if ((t.payloads or [{}])[0]).get("type") == "research")
    max_per_hour = _runtime_config.get("max_autonomous_tasks_per_hour", 5)
    if tasks_this_hour >= max_per_hour:
        blocking_guards.append({
            "guard": "rate_limit_exceeded",
            "reason": f"tasks_this_hour ({tasks_this_hour}) >= max_per_hour ({max_per_hour})",
            "tasks_this_hour": tasks_this_hour,
            "max_per_hour": max_per_hour,
        })

    # Guard 6: waiting_for_interval — interval gate hasn't elapsed since last_idle_check
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    last_idle_check = _shared_state_mod.last_idle_check
    if last_idle_check:
        interval_elapsed = (now - last_idle_check).total_seconds() >= interval_minutes * 60
        next_check_at = (last_idle_check + timedelta(minutes=interval_minutes)).isoformat()
    else:
        interval_elapsed = True
        next_check_at = now.isoformat()

    if not interval_elapsed:
        secs_until_check = max(0, int((last_idle_check + timedelta(minutes=interval_minutes) - now).total_seconds()))
        blocking_guards.append({
            "guard": "waiting_for_interval",
            "reason": f"Research interval ({interval_minutes}m) has not elapsed since last check",
            "next_check_at": next_check_at,
            "seconds_until_check": secs_until_check,
        })

    return {
        "spawn_blocked": len(blocking_guards) > 0,
        "blocking_guards": blocking_guards,
        "circuit_breaker": circuit_breaker_info,
        "rate_limit": {
            "tasks_this_hour": tasks_this_hour,
            "max_per_hour": max_per_hour,
        },
        "directions": directions_info,
        "next_check_at": next_check_at,
    }


@router.post("/projects/{project_id}/circuit-breaker/reset")
async def reset_circuit_breaker(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually reset a project's circuit breaker to CLOSED state."""
    await _get_user_project(project_id, current_user["user_id"], session)
    import app.shared_state as _shared_state
    from app.main import _persist_circuit_breakers, broadcast_to_project

    key = str(project_id)
    breaker = _shared_state.circuit_breakers.get(key)
    if not breaker:
        raise HTTPException(status_code=404, detail="No circuit breaker state found for this project")

    base_cooldown = breaker.get("base_cooldown", 1800)
    breaker["state"] = "CLOSED"
    breaker["failure_count"] = 0
    breaker["cooldown_seconds"] = base_cooldown
    breaker["opened_at"] = None
    breaker["half_open_task_id"] = None

    await _persist_circuit_breakers()

    user_email = current_user.get("email", current_user.get("sub", "unknown"))
    logger.info(f"[ADMIN] Circuit breaker manually reset for project {project_id} by {user_email}")

    await broadcast_to_project(key, "autonomy_updated", {"project_id": key, "circuit_breaker_reset": True})

    return {
        "project_id": key,
        "state": breaker["state"],
        "failure_count": breaker["failure_count"],
        "cooldown_seconds": breaker["cooldown_seconds"],
        "opened_at": None,
        "half_open_task_id": None,
    }


# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

class StuckTaskInfo(TypedDict):
    task_id: str
    title: str
    status: TaskStatus
    stuck_reason: str
    stuck_for_seconds: int
    attempt_count: int
    failure_class: Optional[str]
    unresolvable_deps: List[str]


class CircuitBreakerInfo(TypedDict):
    state: str
    failure_count: int
    cooldown_seconds: int
    cooldown_remaining_seconds: Optional[int]
    opened_at: Optional[str]


class ProjectSummary(TypedDict):
    project_id: str
    stuck_count: int
    queued_count: int
    in_progress_count: int
    circuit_breaker_state: str


def _classify_stuck_failure(task: Task) -> Optional[str]:
    """Lightweight failure classifier for stuck tasks based on status message."""
    msg = (task.latest_status_message or "").lower()
    if any(kw in msg for kw in ["provision", "quota", "vm failed", "connection refused", "ssh", "startup script"]):
        return "PROVISION_FAILURE"
    if any(kw in msg for kw in ["rate limit", "rate_limit", "429", "overloaded", "credit", "too many requests"]):
        return "RATE_LIMIT"
    if any(kw in msg for kw in ["api.github.com", "api.anthropic.com", "502", "503", "ssl", "certificate"]):
        return "EXTERNAL_API"
    if any(kw in msg for kw in ["no heartbeat", "reaped", "timed out", "killed", "sigterm"]):
        return "AGENT_TIMEOUT"
    return None


async def _build_stuck_tasks(
    tasks: List[Task],
    now: datetime,
    session: AsyncSession,
) -> List[StuckTaskInfo]:
    """Detect stuck tasks and build the diagnostics list."""
    stuck: List[StuckTaskInfo] = []
    provision_timeout_secs = 5 * 60
    no_heartbeat_secs = 5 * 60
    long_queued_secs = 30 * 60
    stuck_deps_secs = 10 * 60

    # Pre-fetch all dependency statuses in a single batch query to avoid N+1
    all_dep_uuids: set[uuid.UUID] = set()
    for task in tasks:
        if task.status == TaskStatus.QUEUED:
            created_age = (now - task.created_at).total_seconds()
            if (task.depends_on or []) and created_age > stuck_deps_secs:
                for d in task.depends_on:
                    try:
                        all_dep_uuids.add(uuid.UUID(d))
                    except (ValueError, AttributeError):
                        pass

    dep_status_map: Dict[str, TaskStatus] = {}
    if all_dep_uuids:
        dep_result = await session.execute(
            select(Task.id, Task.status).where(Task.id.in_(all_dep_uuids))
        )
        for row in dep_result.all():
            dep_status_map[str(row.id)] = row.status

    for task in tasks:
        stuck_reason = None
        stuck_for_seconds = 0
        unresolvable_deps: List[str] = []

        if task.status == TaskStatus.PROVISIONING and task.last_heartbeat is None and task.started_at is not None:
            age = (now - task.started_at).total_seconds()
            if age > provision_timeout_secs:
                stuck_reason = "provision_timeout"
                stuck_for_seconds = int(age - provision_timeout_secs)

        elif task.status == TaskStatus.IN_PROGRESS and task.last_heartbeat is not None:
            heartbeat_age = (now - task.last_heartbeat).total_seconds()
            if heartbeat_age > no_heartbeat_secs:
                stuck_reason = "no_heartbeat"
                stuck_for_seconds = int(heartbeat_age - no_heartbeat_secs)

        elif task.status == TaskStatus.QUEUED:
            created_age = (now - task.created_at).total_seconds()
            dep_ids = task.depends_on or []

            if dep_ids and created_age > stuck_deps_secs:
                # Check whether all deps are terminal-failure using pre-fetched map
                valid_dep_uuids = []
                for d in dep_ids:
                    try:
                        valid_dep_uuids.append(uuid.UUID(d))
                    except (ValueError, AttributeError):
                        pass
                if valid_dep_uuids:
                    known_rows = [
                        (str(uid), dep_status_map[str(uid)])
                        for uid in valid_dep_uuids
                        if str(uid) in dep_status_map
                    ]
                    failed_dep_ids = [
                        dep_id
                        for dep_id, status in known_rows
                        if status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                    ]
                    if failed_dep_ids and len(failed_dep_ids) == len(known_rows):
                        stuck_reason = "stuck_deps"
                        stuck_for_seconds = int(created_age - stuck_deps_secs)
                        unresolvable_deps = failed_dep_ids

            elif not dep_ids and created_age > long_queued_secs:
                stuck_reason = "long_queued"
                stuck_for_seconds = int(created_age - long_queued_secs)

        if stuck_reason:
            stuck.append({
                "task_id": str(task.id),
                "title": task.title,
                "status": task.status,
                "stuck_reason": stuck_reason,
                "stuck_for_seconds": stuck_for_seconds,
                "attempt_count": task.attempt_count,
                "failure_class": _classify_stuck_failure(task),
                "unresolvable_deps": unresolvable_deps,
            })

    return stuck


def _format_circuit_breaker(cb: dict) -> CircuitBreakerInfo:
    opened_at = cb.get("opened_at")
    cooldown_remaining = None
    if opened_at and cb.get("state") == "OPEN":
        elapsed = (_utcnow() - opened_at).total_seconds()
        cooldown_remaining = max(0, int(cb.get("cooldown_seconds", 1800) - elapsed))
    return {
        "state": cb.get("state", "CLOSED"),
        "failure_count": cb.get("failure_count", 0),
        "cooldown_seconds": cb.get("cooldown_seconds", 1800),
        "cooldown_remaining_seconds": cooldown_remaining,
        "opened_at": opened_at.isoformat() if opened_at else None,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/projects/{project_id}/diagnostics
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/diagnostics")
async def get_project_diagnostics(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a structured health snapshot for a single project's pipeline."""
    from app.shared_state import get_circuit_breaker
    from app.compute import get_zone_health_summary

    project = await _get_user_project(project_id, current_user["user_id"], session)

    now = _utcnow()

    active_statuses = [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
    active_result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
    )
    active_tasks = active_result.scalars().all()

    stuck_tasks = await _build_stuck_tasks(active_tasks, now, session)

    queued_count = sum(1 for t in active_tasks if t.status == TaskStatus.QUEUED)
    in_progress_count = sum(1 for t in active_tasks if t.status == TaskStatus.IN_PROGRESS)

    cb_raw = get_circuit_breaker(str(project_id))
    circuit_breaker = _format_circuit_breaker(cb_raw)

    try:
        zone_health = get_zone_health_summary()
    except Exception:
        zone_health = {"available": 0, "exhausted": []}

    return {
        "project_id": str(project_id),
        "stuck_tasks": stuck_tasks,
        "circuit_breaker": circuit_breaker,
        "queued_count": queued_count,
        "in_progress_count": in_progress_count,
        "zone_health": zone_health,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/diagnostics  (aggregated across all projects)
# ---------------------------------------------------------------------------

@router.get("/diagnostics")
async def get_global_diagnostics(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Aggregate pipeline diagnostics across all projects."""
    from app.shared_state import get_circuit_breaker, circuit_breakers
    from app.compute import get_zone_health_summary

    now = _utcnow()

    active_statuses = [TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
    active_result = await session.execute(
        select(Task).where(
            Task.status.in_(active_statuses),
            Task.archived.is_(False),
        )
    )
    active_tasks = active_result.scalars().all()

    # Group tasks by project
    by_project: Dict[str, List[Task]] = {}
    for task in active_tasks:
        pid = str(task.project_id)
        by_project.setdefault(pid, []).append(task)

    all_stuck: List[StuckTaskInfo] = []
    project_summaries: List[ProjectSummary] = []
    total_queued = 0
    total_in_progress = 0

    for pid, tasks in by_project.items():
        stuck = await _build_stuck_tasks(tasks, now, session)
        queued = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)
        in_prog = sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS)
        total_queued += queued
        total_in_progress += in_prog
        all_stuck.extend(stuck)

        cb_raw = get_circuit_breaker(pid)
        project_summaries.append({
            "project_id": pid,
            "stuck_count": len(stuck),
            "queued_count": queued,
            "in_progress_count": in_prog,
            "circuit_breaker_state": cb_raw.get("state", "CLOSED"),
        })

    # All known circuit breakers (includes projects with no active tasks)
    all_circuit_breakers = {
        pid: _format_circuit_breaker(cb)
        for pid, cb in circuit_breakers.items()
    }

    try:
        zone_health = get_zone_health_summary()
    except Exception:
        zone_health = {"available": 0, "exhausted": []}

    return {
        "stuck_tasks": all_stuck,
        "total_stuck_count": len(all_stuck),
        "queued_count": total_queued,
        "in_progress_count": total_in_progress,
        "projects": project_summaries,
        "circuit_breakers": all_circuit_breakers,
        "zone_health": zone_health,
    }


@router.get("/digest")
async def get_digest(
    hours: int = Query(24, ge=1, le=168),
    project_id: Optional[uuid.UUID] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin)
):
    """Return a catch-up digest of activity within the requested time window."""
    window_start = _utcnow() - timedelta(hours=hours)
    # Extend lookback to capture long-running tasks that completed within the window
    lookback_start = _utcnow() - timedelta(hours=min(hours * 2, 24 * 7))

    def _derive_completed_at(t: Task):
        if t.status_log:
            try:
                return datetime.fromisoformat(t.status_log[-1]["t"])
            except (KeyError, ValueError):
                pass
        return t.created_at

    # --- Query tasks from extended lookback window ---
    task_filters = [Task.created_at >= lookback_start]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    tasks_result = await session.execute(select(Task).where(*task_filters))
    all_tasks = tasks_result.scalars().all()

    # Post-filter completed/failed by completion timestamp, not creation time
    completed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.COMPLETED and _derive_completed_at(t) >= window_start
    ]
    failed_tasks = [
        t for t in all_tasks
        if t.status == TaskStatus.FAILED and _derive_completed_at(t) >= window_start
    ]
    # Approval tasks use created_at (they haven't completed yet)
    approval_tasks = [
        t for t in all_tasks
        if t.requires_approval
        and t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        and t.created_at >= window_start
    ]

    # --- Query active tasks (not filtered by window — they may have started before it) ---
    _active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    active_filters = [Task.status.in_(_active_statuses)]
    if project_id:
        active_filters.append(Task.project_id == project_id)
    active_result = await session.execute(
        select(Task).where(*active_filters).order_by(Task.started_at.asc().nullsfirst())
    )
    active_tasks_all = active_result.scalars().all()

    # research_cycles: completed tasks with task_type == 'research'
    research_cycles = sum(
        1 for t in completed_tasks
        if ((t.payloads or [{}])[0]).get("type", "create") == "research"
    )

    # --- Batch artifact query ---
    all_task_ids = [t.id for t in all_tasks]
    pr_count = 0
    pr_artifact_map: Dict[str, str] = {}   # task_id -> pr_url
    error_log_map: Dict[str, str] = {}     # task_id -> truncated error snippet

    if all_task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(all_task_ids),
                Artifact.artifact_type.in_([
                    ArtifactType.PULL_REQUEST,
                    ArtifactType.ERROR_LOG,
                ])
            )
            .order_by(Artifact.created_at.asc())
        )
        for a in artifacts_result.scalars().all():
            tid = str(a.task_id)
            if a.artifact_type == ArtifactType.PULL_REQUEST:
                if a.created_at >= window_start:
                    pr_count += 1
                pr_artifact_map[tid] = a.pr_url or ""
            elif a.artifact_type == ArtifactType.ERROR_LOG and a.logs and tid not in error_log_map:
                error_log_map[tid] = a.logs[:200]

    # --- needs_attention: failed + pending approval ---
    _now = _utcnow()
    needs_attention = []
    for t in failed_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "reason": "failed",
            "error_snippet": error_log_map.get(tid),
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
        })
    for t in approval_tasks:
        tid = str(t.id)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        needs_attention.append({
            "task_id": tid,
            "title": t.title or "Pending approval",
            "reason": "approval_required",
            "error_snippet": None,
            "pr_url": pr_artifact_map.get(tid),
            "waiting_since": waiting_since.isoformat() if waiting_since else None,
            "waiting_hours": waiting_hours,
        })
    needs_attention.sort(key=lambda x: (0 if x["reason"] == "approval_required" else 1, -(x["waiting_hours"] or 0)))

    # --- recent_failures (up to 10, desc) ---
    sorted_failures = sorted(failed_tasks, key=_derive_completed_at, reverse=True)
    recent_failures = []
    for t in sorted_failures[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        waiting_since = _derive_completed_at(t)
        waiting_hours = round(((_now - waiting_since).total_seconds() / 3600)) if waiting_since else None
        recent_failures.append({
            "task_id": tid,
            "title": t.title or "Failed task",
            "failed_at": ts.isoformat() if ts else None,
            "error_snippet": error_log_map.get(tid),
            "waiting_hours": waiting_hours,
        })

    # --- recent_completions (up to 10, desc) ---
    sorted_completions = sorted(completed_tasks, key=_derive_completed_at, reverse=True)
    recent_completions = []
    for t in sorted_completions[:10]:
        tid = str(t.id)
        ts = _derive_completed_at(t)
        task_type = ((t.payloads or [{}])[0]).get("type", "create")
        recent_completions.append({
            "task_id": tid,
            "title": t.title or "Completed task",
            "completed_at": ts.isoformat() if ts else None,
            "pr_url": pr_artifact_map.get(tid),
            "task_type": task_type,
        })

    # --- active_tasks list (up to 5, oldest started first) ---
    active_tasks_list = [
        {
            "task_id": str(t.id),
            "title": t.title or "Active task",
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "task_type": ((t.payloads or [{}])[0]).get("type", "create"),
            "started_at": t.started_at.isoformat() if t.started_at else None,
        }
        for t in active_tasks_all[:5]
    ]

    running_statuses = {TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING}
    queued_statuses = {TaskStatus.QUEUED, TaskStatus.EVALUATING}
    running_count = sum(1 for t in active_tasks_all if t.status in running_statuses)
    queued_count = sum(1 for t in active_tasks_all if t.status in queued_statuses)

    return {
        "window_hours": hours,
        "generated_at": _utcnow().isoformat(),
        "completed_count": len(completed_tasks),
        "failed_count": len(failed_tasks),
        "pending_approval_count": len(approval_tasks),
        "research_cycles": research_cycles,
        "prs_opened": pr_count,
        "active_count": len(active_tasks_all),
        "running_count": running_count,
        "queued_count": queued_count,
        "active_tasks": active_tasks_list,
        "needs_attention": needs_attention,
        "recent_failures": recent_failures,
        "recent_completions": recent_completions,
    }


# ── User Management ──────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    email: str
    role: str = "user"

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v:
            raise ValueError("Invalid email address")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("admin", "user"):
            raise ValueError("Role must be 'admin' or 'user'")
        return v


class UserUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("admin", "user"):
            raise ValueError("Role must be 'admin' or 'user'")
        return v


@router.get("/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    result = await session.execute(
        select(AllowedUser).order_by(AllowedUser.created_at.desc())
    )
    users = result.scalars().all()
    return [
        {
            "id": str(u.id),
            "email": u.email,
            "role": u.role,
            "display_name": u.display_name,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.post("/users", status_code=201)
async def add_user(
    payload: UserCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    existing = (await session.execute(
        select(AllowedUser).where(AllowedUser.email == payload.email)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="User with this email already exists")

    user = AllowedUser(email=payload.email, role=payload.role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "last_login_at": None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    user = await session.get(AllowedUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.role = payload.role
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.delete("/users/{user_id}")
async def remove_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    user = await session.get(AllowedUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent self-deletion
    if user.firebase_uid == current_user.get("user_id") or user.email == current_user.get("email"):
        raise HTTPException(status_code=400, detail="Cannot remove your own access")

    await session.delete(user)
    await session.commit()
    return {"message": f"User {user.email} removed"}
