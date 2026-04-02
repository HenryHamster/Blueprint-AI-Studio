from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional, Dict, Any, List
import uuid
import json
import httpx
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session
from app.models import Task, TaskStatus, TaskHistory, Project, Artifact, ArtifactType, UserIntegration, MaxPlanAccount, DeploymentState, DeploymentStatus
from app.auth import get_current_user, require_admin
from app.compute import delete_agent_vm
from app.github import generate_installation_access_token

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)]
)

@router.get("/projects")
async def get_all_projects(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    query = select(Project).order_by(Project.created_at.desc())
    result = await session.execute(query)
    projects = result.scalars().all()
    return projects

@router.post("/autonomous/pause-all")
async def pause_all_autonomous(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Disable autonomous mode on all projects at once."""
    projects = (await session.execute(select(Project))).scalars().all()
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
    filters = []
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
    if request.task_ids is not None:
        archived = []
        for raw_id in request.task_ids:
            try:
                task = await session.get(Task, uuid.UUID(raw_id))
            except (ValueError, AttributeError):
                continue
            if task and task.status in TERMINAL_STATUSES and not task.archived:
                task.archived = True
                session.add(task)
                archived.append(task)
    elif request.filter in ('completed', 'terminal'):
        if request.filter == 'completed':
            statuses = [TaskStatus.COMPLETED]
        else:
            statuses = TERMINAL_STATUSES
        query = select(Task).where(Task.archived == False, Task.status.in_(statuses))
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

    task = await session.get(Task, tid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

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
    prompt: str = Field(..., min_length=1)
    base_branch: str = Field(default="main", pattern=r"^[a-zA-Z0-9_./-]+$")
    task_type: str = Field(default="create", pattern=r"^(create|plan|research|review|fix|security_audit|qa|deps|docs)$")
    pr_number: Optional[int] = None
    branch: Optional[str] = None
    model: Optional[str] = Field(default=None, min_length=1)
    effort: Optional[str] = Field(default=None, pattern=r"^(low|medium|high)$")


@router.post("/tasks")
async def create_task_manually(
    payload: CreateTaskPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Create a Claude Code task. Auto-creates a project for the repo if one doesn't exist.
    """
    # Parse owner/repo
    github_owner, github_repo = payload.github_repo.split("/", 1)

    # Find or create project for this repo
    stmt = select(Project).where(
        Project.github_owner == github_owner,
        Project.github_repo == github_repo
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
    vm_name = f"agent-vm-{uuid.uuid4().hex[:8]}"

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
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
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
    if payload.action == "fix":
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
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
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
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.archived = False
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@router.patch("/tasks/{task_id}")
async def modify_task(
    task_id: uuid.UUID,
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    changes = {}
    if "status" in payload:
        try:
            new_status = TaskStatus(payload["status"])
            task.status = new_status
            changes["status"] = new_status.value

            # Clear approval flag on terminal statuses
            if new_status in (TaskStatus.CANCELLED, TaskStatus.FAILED):
                task.requires_approval = False

            # Delete VM and release account when cancelling an active task
            if new_status == TaskStatus.CANCELLED and task.vm_instance_id:
                background_tasks.add_task(delete_agent_vm, task.vm_instance_id)
                acct_id = (task.context or {}).get("max_plan_account_id")
                if acct_id:
                    from app.main import release_max_plan_account
                    release_max_plan_account(acct_id)

            # Reset retry state when manually re-queuing a task
            if new_status == TaskStatus.QUEUED:
                # Check if this is a parent with children — re-evaluate instead of re-run
                from app.main import _requeue_parent_task
                children = (await session.execute(
                    select(Task).where(Task.parent_task_id == task.id)
                )).scalars().all()
                if children:
                    retried = await _requeue_parent_task(task, session)
                    changes["status"] = TaskStatus.EVALUATING
                    changes["retried_children"] = retried
                else:
                    task.attempt_count = 0
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex[:8]}"
                    task.last_heartbeat = None
                    changes["attempt_count"] = 0
                    changes["vm_instance_id"] = task.vm_instance_id
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status")

    if "requires_approval" in payload:
        was_blocked = task.requires_approval
        task.requires_approval = bool(payload["requires_approval"])
        changes["requires_approval"] = task.requires_approval

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
                            print(f"[ADMIN] Human-approved merge of PR #{pr_number} (critical files)")
                        else:
                            changes["merge_error"] = merge_result.get("message", "unknown")
                            task.latest_status_message = f"Approved but merge failed: {merge_result.get('message', 'unknown')}"
                            print(f"[ADMIN] Approved merge of PR #{pr_number} failed: {merge_result.get('message')}")
                    except Exception as e:
                        changes["merge_error"] = str(e)[:200]
                        task.latest_status_message = f"Approved but merge failed: {str(e)[:200]}"
                        print(f"[ADMIN] Approved merge failed: {e}")
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
                # Task stays COMPLETED regardless
                if "status" in payload and TaskStatus(payload["status"]) == TaskStatus.QUEUED:
                    task.status = TaskStatus.COMPLETED
                    changes["status"] = TaskStatus.COMPLETED

    if "payloads" in payload:
        task.payloads = payload["payloads"]
        changes["payloads"] = "updated"

    if "parent_task_id" in payload:
        if payload["parent_task_id"]:
            task.parent_task_id = uuid.UUID(payload["parent_task_id"])
        else:
            task.parent_task_id = None
        changes["parent_task_id"] = str(task.parent_task_id)

    if "context" in payload and isinstance(payload["context"], dict):
        task.context = {**(task.context or {}), **payload["context"]}
        changes["context"] = "updated"

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
        if "context" in payload and task.parent_task_id:
            # pr_merged was set via admin → trigger rollup
            if isinstance(payload["context"], dict) and payload["context"].get("pr_merged"):
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


@router.post("/tasks/{task_id}/rollup")
async def trigger_rollup(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Manually trigger rollup PR for an EVALUATING parent task."""
    from app.main import get_github_token_for_task, _spawn_pipeline_task, broadcast_to_project
    from app.github import create_pull_request, merge_pull_request, delete_branch

    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
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
    from app.main import _max_plan_pool
    from app.models import UserIntegration

    # Check if the user has a GitHub integration
    github_account = None
    stmt = select(UserIntegration).where(UserIntegration.user_id == current_user["user_id"])
    integration = (await session.execute(stmt)).scalars().first()
    if integration and integration.github_account_name:
        github_account = integration.github_account_name

    return {
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "max_retries": _runtime_config["max_retries"],
        "max_autonomous_tasks_per_hour": _runtime_config["max_autonomous_tasks_per_hour"],
        "autonomous_research_interval_minutes": _runtime_config.get("autonomous_research_interval_minutes", 30),
        "autonomous_research_model": _runtime_config.get("autonomous_research_model") or "",
        "autonomous_research_effort": _runtime_config.get("autonomous_research_effort") or "",
        "github_account": github_account,
        "using_max_plan": len([a for a in _max_plan_pool if a.get("enabled", True)]) > 0,
        "max_plan_accounts": [
            {"id": a["id"], "name": a["name"], "enabled": a.get("enabled", True),
             "active_tasks": a["active_tasks"], "total_tasks": a["total_tasks"],
             "token_created_at": a.get("token_created_at")}
            for a in _max_plan_pool
        ],
    }


@router.patch("/settings")
async def update_settings(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.shared_state import runtime_config as _runtime_config
    if "max_concurrent_vms" in payload:
        try:
            val = int(payload["max_concurrent_vms"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="max_concurrent_vms must be a number")
        if not (1 <= val <= 30):
            raise HTTPException(status_code=400, detail="max_concurrent_vms must be between 1 and 30")
        _runtime_config["max_concurrent_vms"] = val
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
    if "autonomous_research_interval_minutes" in payload:
        try:
            val = int(payload["autonomous_research_interval_minutes"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="autonomous_research_interval_minutes must be a number")
        if not (1 <= val <= 1440):
            raise HTTPException(status_code=400, detail="autonomous_research_interval_minutes must be between 1 and 1440")
        _runtime_config["autonomous_research_interval_minutes"] = val
    if "autonomous_research_model" in payload:
        val = payload["autonomous_research_model"] or None
        if val is not None and val not in ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"):
            raise HTTPException(status_code=400, detail="Invalid autonomous_research_model")
        _runtime_config["autonomous_research_model"] = val
    if "autonomous_research_effort" in payload:
        val = payload["autonomous_research_effort"] or None
        if val is not None and val not in ("low", "medium", "high"):
            raise HTTPException(status_code=400, detail="autonomous_research_effort must be low, medium, or high")
        _runtime_config["autonomous_research_effort"] = val

    # Persist to DB so values survive Cloud Run restarts (only fields present in payload)
    state = await session.get(DeploymentState, 1)
    if state:
        if "max_concurrent_vms" in payload:
            state.runtime_max_concurrent_vms = _runtime_config["max_concurrent_vms"]
        if "max_retries" in payload:
            state.runtime_max_retries = _runtime_config["max_retries"]
        if "max_autonomous_tasks_per_hour" in payload:
            state.runtime_max_autonomous_tasks_per_hour = _runtime_config["max_autonomous_tasks_per_hour"]
        if "autonomous_research_interval_minutes" in payload:
            state.runtime_autonomous_research_interval_minutes = _runtime_config["autonomous_research_interval_minutes"]
        if "autonomous_research_model" in payload:
            state.runtime_autonomous_research_model = _runtime_config["autonomous_research_model"]
        if "autonomous_research_effort" in payload:
            state.runtime_autonomous_research_effort = _runtime_config["autonomous_research_effort"]
        session.add(state)
        await session.commit()

    return _runtime_config


# --- Max Plan Account Pool ---

@router.post("/max-plan-accounts")
async def add_max_plan_account(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Add a Max plan account. Provide 'name' and 'oauth_token' (sk-ant-oat01-... from `claude setup-token`).
    Optionally provide 'claude_config_json' (content of ~/.claude.json for onboarding bypass)."""
    from app.main import _max_plan_pool
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

    now = datetime.utcnow()
    db_account = MaxPlanAccount(
        name=name,
        oauth_token=oauth_token,
        claude_config_json=claude_config_json,
        token_created_at=now,
    )
    session.add(db_account)
    await session.commit()
    await session.refresh(db_account)

    # Sync to in-memory pool
    _max_plan_pool.append({
        "id": str(db_account.id),
        "name": db_account.name,
        "oauth_token": db_account.oauth_token,
        "claude_config_json": db_account.claude_config_json,
        "enabled": db_account.enabled,
        "active_tasks": 0,
        "total_tasks": db_account.total_tasks,
        "token_created_at": now.isoformat(),
    })
    return {"id": str(db_account.id), "name": db_account.name, "message": "Account added to pool"}


@router.delete("/max-plan-accounts/{account_id}")
async def remove_max_plan_account(
    account_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.main import _max_plan_pool

    # Delete from database
    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account ID")
    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")
    await session.delete(db_account)
    await session.commit()

    # Remove from in-memory pool
    _max_plan_pool[:] = [a for a in _max_plan_pool if a["id"] != account_id]
    return {"message": "Account removed"}


@router.patch("/max-plan-accounts/{account_id}")
async def update_max_plan_account(
    account_id: str,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.main import _max_plan_pool

    # Update in database
    try:
        acct_uuid = uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid account ID")
    db_account = await session.get(MaxPlanAccount, acct_uuid)
    if not db_account:
        raise HTTPException(status_code=404, detail="Account not found")

    if "enabled" in payload:
        db_account.enabled = bool(payload["enabled"])
    if "name" in payload:
        db_account.name = payload["name"]
    if "oauth_token" in payload:
        new_token = payload["oauth_token"]
        if not new_token.startswith("sk-ant-"):
            raise HTTPException(status_code=400, detail="oauth_token should start with 'sk-ant-'")
        import re
        if not re.fullmatch(r'sk-ant-[A-Za-z0-9_\-]+', new_token):
            raise HTTPException(status_code=400, detail="oauth_token contains invalid characters")
        db_account.oauth_token = new_token
        db_account.token_created_at = datetime.utcnow()
    if "claude_config_json" in payload:
        import json as _json
        try:
            _json.loads(payload["claude_config_json"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="claude_config_json must be valid JSON")
        db_account.claude_config_json = payload["claude_config_json"]
    db_account.updated_at = datetime.utcnow()
    session.add(db_account)
    await session.commit()

    # Sync to in-memory pool
    mem_account = next((a for a in _max_plan_pool if a["id"] == account_id), None)
    if mem_account:
        if "enabled" in payload:
            mem_account["enabled"] = db_account.enabled
        if "name" in payload:
            mem_account["name"] = db_account.name
        if "oauth_token" in payload:
            mem_account["oauth_token"] = db_account.oauth_token
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


@router.get("/projects/{project_id}/directions")
async def get_directions(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"directions": project.directions or [], "autonomous_enabled": project.autonomous_enabled}


@router.post("/projects/{project_id}/directions")
async def add_direction(
    project_id: uuid.UUID,
    payload: DirectionPayload,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    direction = {
        "id": f"d-{uuid.uuid4().hex[:8]}",
        "text": payload.text,
        "priority": payload.priority,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
    }
    directions = list(project.directions or [])
    directions.append(direction)
    project.directions = directions
    session.add(project)
    await session.commit()

    return {"direction": direction}


@router.delete("/projects/{project_id}/directions/{direction_id}")
async def remove_direction(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    directions = [d for d in (project.directions or []) if d.get("id") != direction_id]
    if len(directions) == len(project.directions or []):
        raise HTTPException(status_code=404, detail="Direction not found")

    project.directions = directions
    session.add(project)
    await session.commit()
    return {"message": "Direction removed"}


@router.patch("/projects/{project_id}/directions/reorder")
async def reorder_directions(
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await session.get(Project, project_id)
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
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate inputs
    if "priority" in payload and payload["priority"] not in ("high", "medium", "low"):
        raise HTTPException(status_code=400, detail="priority must be high, medium, or low")
    if "status" in payload and payload["status"] not in ("active", "completed", "paused"):
        raise HTTPException(status_code=400, detail="status must be active, completed, or paused")
    if "text" in payload:
        if not isinstance(payload["text"], str) or len(payload["text"]) > 500 or len(payload["text"]) < 1:
            raise HTTPException(status_code=400, detail="text must be 1-500 characters")

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
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    direction = next((d for d in (project.directions or []) if d.get("id") == direction_id), None)
    if not direction:
        raise HTTPException(status_code=404, detail="Direction not found")

    vm_name = f"agent-vm-{uuid.uuid4().hex[:8]}"
    task_payload = {
        "type": "research",
        "directions": [direction],
        "base_branch": project.base_branch or "main",
    }
    direction_text = direction.get("text", "")
    new_task = Task(
        project_id=project.id,
        title=f"Research: {direction_text[:60]}",
        description=direction_text,
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
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
        )
    )
    tasks = result.scalars().all()

    counts: Dict[str, int] = {}
    for task in tasks:
        payload = (task.payloads or [{}])[0]
        if payload.get("type") != "research":
            continue
        for direction in payload.get("directions", []):
            d_id = direction.get("id")
            if d_id:
                counts[d_id] = counts.get(d_id, 0) + 1

    return {"counts": counts}


@router.post("/projects/{project_id}/directions/{direction_id}/research")
async def trigger_direction_research(
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Spawn a research task scoped to a single direction."""
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    direction = next((d for d in (project.directions or []) if d.get("id") == direction_id), None)
    if not direction:
        raise HTTPException(status_code=404, detail="Direction not found")

    new_task = Task(
        project_id=project.id,
        title=f"Research: {direction['text'][:80]}",
        description=f"Targeted research for direction: {direction['text']}",
        payloads=[{
            "type": "research",
            "directions": [direction],
            "base_branch": "main",
        }],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:8]}",
        status=TaskStatus.QUEUED,
        created_by="USER",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="CREATED",
        details={"created_by": "USER", "method": "direction_research", "direction_id": direction_id}
    )
    session.add(history)
    await session.commit()
    await session.refresh(new_task)

    return {"id": str(new_task.id), "title": new_task.title, "status": new_task.status}


@router.patch("/projects/{project_id}/autonomous")
async def toggle_autonomous(
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

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
        select(Project).order_by(Project.created_at.desc())
    )).scalars().all()

    one_hour_ago = datetime.utcnow() - timedelta(hours=1)

    # Batch query: count autonomous tasks per project in the last hour
    tasks_per_project = dict((await session.execute(
        select(Task.project_id, func.count()).where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        ).group_by(Task.project_id)
    )).all())

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
    current_user: dict = Depends(get_current_user)
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
    print("[ADMIN] Orchestrator restarted via admin endpoint")
    return {"status": "restarted"}


# --- Deployment State & Health ---

@router.get("/deploy-state")
async def get_deploy_state(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Get current deployment state including monitoring status and health."""
    from app.shared_state import deploy_monitoring, external_health, runtime_config

    state = await session.get(DeploymentState, 1)
    if not state:
        return {"error": "No deployment state found — service may not have initialized yet"}

    # Calculate monitoring window progress
    window_progress = None
    if deploy_monitoring["window_start"]:
        duration = deploy_monitoring["extended_window"] if deploy_monitoring["is_extended"] else deploy_monitoring["window_duration"]
        elapsed = (datetime.utcnow() - deploy_monitoring["window_start"]).total_seconds()
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
        "monitoring_window": window_progress,
        "failure_rate": round(failure_rate, 3),
        "task_results_count": len(results),
        "external_health": external_health,
        "max_concurrent_vms": runtime_config["max_concurrent_vms"],
    }


@router.post("/deploy-state/pause")
async def pause_pipeline(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Manually pause the pipeline (set max_concurrent_vms=0). Used for manual intervention."""
    from app.shared_state import runtime_config

    state = await session.get(DeploymentState, 1)
    if state:
        state.status = DeploymentStatus.PAUSED
        state.pause_reason = "Manual pause via admin"
        state.paused_at = datetime.utcnow()
        state.pre_pause_max_vms = runtime_config["max_concurrent_vms"]
        session.add(state)
        await session.commit()

    old_vms = runtime_config["max_concurrent_vms"]
    runtime_config["max_concurrent_vms"] = 0
    print(f"[ADMIN] Pipeline PAUSED manually (was max_concurrent_vms={old_vms})")
    return {"status": "paused", "previous_max_vms": old_vms}


@router.post("/deploy-state/resume")
async def resume_pipeline(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
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

    restore_vms = 5  # default
    if state.pre_pause_max_vms is not None:
        restore_vms = state.pre_pause_max_vms
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
    print(f"[ADMIN] Pipeline RESUMED (max_concurrent_vms={restore_vms})")
    return {"status": "resumed", "max_concurrent_vms": restore_vms}


@router.post("/deploy-state/mark-stable")
async def mark_deploy_stable(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
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
    print(f"[ADMIN] Deploy manually marked STABLE (sha={state.current_sha[:8]})")
    return {"status": "stable", "sha": state.current_sha}


@router.post("/deploy-state/rollback")
async def trigger_rollback(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
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
            print(f"[ADMIN] Cloud Run rollback failed: {rollback_error}")
    except Exception as e:
        rollback_error = str(e)[:500]
        print(f"[ADMIN] Rollback command failed: {e}")

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

    print(f"[ADMIN] ROLLBACK triggered: {state.current_sha[:8]} → {state.last_known_good_sha[:8]}")

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
    current_user: dict = Depends(get_current_user)
):
    """Post-rollback state audit and repair. Fixes stuck tasks, orphaned parents, stale evaluating tasks."""
    from app.models import TaskHistory
    repairs = []

    # 1. Tasks stuck in PROVISIONING/IN_PROGRESS with no heartbeat for 15+ min → FAILED
    fifteen_min_ago = datetime.utcnow() - timedelta(minutes=15)
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
                try:
                    from app.compute import delete_agent_vm
                    import asyncio
                    asyncio.create_task(delete_agent_vm(t.vm_instance_id))
                except Exception:
                    pass

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
    all_task_ids = set(str(t.id) for t in (await session.execute(select(Task.id))).scalars().all())
    orphan_children = (await session.execute(
        select(Task).where(Task.parent_task_id.isnot(None))
    )).scalars().all()

    for t in orphan_children:
        if str(t.parent_task_id) not in all_task_ids:
            t.parent_task_id = None
            t.latest_status_message = f"Repaired: cleared orphaned parent reference"
            session.add(t)
            session.add(TaskHistory(task_id=t.id, action="REPAIR", details={"reason": "orphaned_parent_ref"}))
            repairs.append(f"Cleared orphaned parent ref on {t.id}")

    await session.commit()
    print(f"[ADMIN] State repair completed: {len(repairs)} repairs")
    return {"repairs": repairs, "count": len(repairs)}


@router.get("/activity")
async def get_activity(
    project_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Return a reverse-chronological list of recent system events."""
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    events: List[Dict[str, Any]] = []

    # --- 1 & 2: Completed and Failed tasks in last 7 days ---
    task_filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        Task.created_at >= seven_days_ago,
    ]
    if project_id:
        task_filters.append(Task.project_id == project_id)

    tasks_result = await session.execute(
        select(Task).where(*task_filters)
    )
    recent_tasks = tasks_result.scalars().all()

    if not recent_tasks:
        task_ids = []
    else:
        task_ids = [t.id for t in recent_tasks]

    # Fetch relevant artifacts in one query
    artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> artifact info
    research_artifact_map: Dict[str, Dict[str, Any]] = {}  # task_id -> research artifact
    error_log_map: Dict[str, str] = {}  # task_id -> truncated error logs

    if task_ids:
        artifacts_result = await session.execute(
            select(Artifact)
            .where(
                Artifact.task_id.in_(task_ids),
                Artifact.artifact_type.in_([
                    ArtifactType.PULL_REQUEST,
                    ArtifactType.ERROR_LOG,
                    ArtifactType.RESEARCH_FINDINGS,
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
