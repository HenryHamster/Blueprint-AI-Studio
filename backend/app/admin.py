from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional, Dict, Any
import uuid

from app.database import get_session
from app.models import Task, TaskStatus, TaskHistory, Project, Artifact, ArtifactType
from app.auth import get_current_user, require_admin
from app.compute import delete_agent_vm

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

    # Join artifacts to include PR URLs and error logs
    task_ids = [t.id for t in tasks]
    pr_map = {}
    logs_map = {}
    if task_ids:
        artifact_stmt = select(Artifact).where(Artifact.task_id.in_(task_ids))
        artifacts = (await session.execute(artifact_stmt)).scalars().all()
        for a in artifacts:
            if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                pr_map[str(a.task_id)] = a.pr_url
            if a.artifact_type == ArtifactType.ERROR_LOG and a.logs:
                logs_map[str(a.task_id)] = a.logs[:2000]

    # Return tasks with artifact data attached
    result_list = []
    for t in tasks:
        task_dict = {
            "id": str(t.id),
            "project_id": str(t.project_id),
            "title": t.title,
            "description": t.description,
            "status": t.status,
            "vm_instance_id": t.vm_instance_id,
            "latest_status_message": t.latest_status_message,
            "created_by": t.created_by,
            "requires_approval": t.requires_approval,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "last_heartbeat": t.last_heartbeat.isoformat() if t.last_heartbeat else None,
            "attempt_count": t.attempt_count,
            "payloads": t.payloads,
            "pr_url": pr_map.get(str(t.id)),
            "error_logs": logs_map.get(str(t.id)),
        }
        result_list.append(task_dict)

    return result_list


class CreateTaskPayload(BaseModel):
    github_repo: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1)
    base_branch: str = Field(default="main", pattern=r"^[a-zA-Z0-9_./-]+$")


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

    new_task = Task(
        project_id=project.id,
        title=payload.title,
        description=payload.prompt,
        payloads=[{
            "prompt": payload.prompt,
            "base_branch": payload.base_branch,
        }],
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
        "github_account": github_account,
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
    return _runtime_config


# --- Max Plan Account Pool ---

@router.post("/max-plan-accounts")
async def add_max_plan_account(
    payload: Dict[str, Any],
    current_user: dict = Depends(get_current_user)
):
    """Add a Max plan account to the pool. Provide 'name' and 'credentials_json' (contents of ~/.claude/credentials.json)."""
    import json as _json
    from app.main import _max_plan_pool
    name = payload.get("name")
    credentials_json = payload.get("credentials_json")
    if not name or not credentials_json:
        raise HTTPException(status_code=400, detail="'name' and 'credentials_json' are required")
    # Validate JSON format to prevent injection and ensure it's real credentials
    try:
        _json.loads(credentials_json)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="credentials_json must be valid JSON")

    account = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "credentials_json": credentials_json,
        "enabled": True,
        "active_tasks": 0,
        "total_tasks": 0,
    }
    _max_plan_pool.append(account)
    return {"id": account["id"], "name": account["name"], "message": "Account added to pool"}


@router.delete("/max-plan-accounts/{account_id}")
async def remove_max_plan_account(
    account_id: str,
    current_user: dict = Depends(get_current_user)
):
    from app.main import _max_plan_pool
    before = len(_max_plan_pool)
    _max_plan_pool[:] = [a for a in _max_plan_pool if a["id"] != account_id]
    if len(_max_plan_pool) == before:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"message": "Account removed"}


@router.patch("/max-plan-accounts/{account_id}")
async def update_max_plan_account(
    account_id: str,
    payload: Dict[str, Any],
    current_user: dict = Depends(get_current_user)
):
    from app.main import _max_plan_pool
    account = next((a for a in _max_plan_pool if a["id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if "enabled" in payload:
        account["enabled"] = bool(payload["enabled"])
    if "name" in payload:
        account["name"] = payload["name"]
    if "credentials_json" in payload:
        import json as _json
        try:
            _json.loads(payload["credentials_json"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="credentials_json must be valid JSON")
        account["credentials_json"] = payload["credentials_json"]
    return {"id": account["id"], "name": account["name"], "enabled": account["enabled"]}
