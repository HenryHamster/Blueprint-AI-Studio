from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional, Dict, Any, List
import uuid
import httpx
import logging
from datetime import datetime

from app.database import get_session
from app.models import Task, TaskStatus, TaskHistory, Project, Artifact, ArtifactType, UserIntegration, MaxPlanAccount
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

@router.get("/tasks")
async def get_all_tasks(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[TaskStatus] = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    query = select(Task).order_by(Task.created_at.desc()).offset(offset).limit(limit)
    if status:
        query = query.where(Task.status == status)

    result = await session.execute(query)
    tasks = result.scalars().all()

    # Join artifacts to include PR URLs, error logs, and research findings
    task_ids = [t.id for t in tasks]
    pr_map = {}
    logs_map = {}
    research_map = {}
    if task_ids:
        artifact_stmt = select(Artifact).where(Artifact.task_id.in_(task_ids))
        artifacts = (await session.execute(artifact_stmt)).scalars().all()
        for a in artifacts:
            if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                pr_map[str(a.task_id)] = a.pr_url
            if a.artifact_type == ArtifactType.ERROR_LOG and a.logs:
                logs_map[str(a.task_id)] = a.logs[:2000]
            if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                research_map[str(a.task_id)] = a.logs[:100000]

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
            "created_by": t.created_by,
            "requires_approval": t.requires_approval,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "depends_on": t.depends_on or [],
            "plan_output": t.plan_output,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "last_heartbeat": t.last_heartbeat.isoformat() if t.last_heartbeat else None,
            "attempt_count": t.attempt_count,
            "payloads": t.payloads,
            "pr_url": pr_map.get(str(t.id)),
            "error_logs": logs_map.get(str(t.id)),
            "research_findings": research_map.get(str(t.id)),
        }
        result_list.append(task_dict)

    return result_list


class CreateTaskPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1)
    base_branch: str = Field(default="main", pattern=r"^[a-zA-Z0-9_./-]+$")
    task_type: str = Field(default="create", pattern=r"^(create|plan|research|review|fix)$")
    pr_number: Optional[int] = None
    branch: Optional[str] = None


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

            # Delete VM when cancelling an active task
            if new_status == TaskStatus.CANCELLED and task.vm_instance_id:
                background_tasks.add_task(delete_agent_vm, task.vm_instance_id)

            # Reset retry state when manually re-queuing a task
            if new_status == TaskStatus.QUEUED:
                task.attempt_count = 0
                task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex[:8]}"
                task.last_heartbeat = None
                changes["attempt_count"] = 0
                changes["vm_instance_id"] = task.vm_instance_id
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status")

    if "requires_approval" in payload:
        task.requires_approval = bool(payload["requires_approval"])
        changes["requires_approval"] = task.requires_approval

    if "payloads" in payload:
        task.payloads = payload["payloads"]
        changes["payloads"] = "updated"

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

    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "title": task.title,
        "status": task.status,
        "vm_instance_id": task.vm_instance_id,
        "attempt_count": task.attempt_count,
        "requires_approval": task.requires_approval,
    }


@router.get("/settings")
async def get_settings(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    from app.main import _runtime_config, _max_plan_pool
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
        "github_account": github_account,
        "using_max_plan": len([a for a in _max_plan_pool if a.get("enabled", True)]) > 0,
        "max_plan_accounts": [
            {"id": a["id"], "name": a["name"], "enabled": a.get("enabled", True),
             "active_tasks": a["active_tasks"], "total_tasks": a["total_tasks"]}
            for a in _max_plan_pool
        ],
    }


@router.patch("/settings")
async def update_settings(
    payload: Dict[str, Any],
    current_user: dict = Depends(get_current_user)
):
    from app.main import _runtime_config
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
    return _runtime_config


# --- Max Plan Account Pool ---

@router.post("/max-plan-accounts")
async def add_max_plan_account(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Add a Max plan account to the pool. Provide 'name' and 'credentials_json' (contents of ~/.claude/credentials.json)."""
    import json as _json
    from app.main import _max_plan_pool
    name = payload.get("name")
    credentials_json = payload.get("credentials_json")
    if not name or not credentials_json:
        raise HTTPException(status_code=400, detail="'name' and 'credentials_json' are required")
    try:
        _json.loads(credentials_json)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="credentials_json must be valid JSON")

    # Persist to database
    db_account = MaxPlanAccount(
        name=name,
        credentials_json=credentials_json,
    )
    session.add(db_account)
    await session.commit()
    await session.refresh(db_account)

    # Sync to in-memory pool
    _max_plan_pool.append({
        "id": str(db_account.id),
        "name": db_account.name,
        "credentials_json": db_account.credentials_json,
        "enabled": db_account.enabled,
        "active_tasks": 0,
        "total_tasks": db_account.total_tasks,
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
    if "credentials_json" in payload:
        import json as _json
        try:
            _json.loads(payload["credentials_json"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="credentials_json must be valid JSON")
        db_account.credentials_json = payload["credentials_json"]
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
        if "credentials_json" in payload:
            mem_account["credentials_json"] = db_account.credentials_json

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
        "added_at": datetime.utcnow().isoformat(),
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
    session.add(project)
    await session.commit()
    return {"direction": next(d for d in directions if d.get("id") == direction_id)}


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
    session.add(project)
    await session.commit()
    return {"autonomous_enabled": project.autonomous_enabled}
