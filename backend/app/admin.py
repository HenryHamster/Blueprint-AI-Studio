from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional, Dict, Any, List
import uuid
import httpx
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import func

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

TERMINAL_STATUSES = [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]


@router.get("/tasks")
async def get_all_tasks(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[TaskStatus] = None,
    terminal_only: bool = Query(False),
    search: Optional[str] = Query(None, max_length=200),
    created_after: Optional[datetime] = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    # Build shared filter conditions
    filters = []
    if status:
        filters.append(Task.status == status)
    elif terminal_only:
        filters.append(Task.status.in_(TERMINAL_STATUSES))
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
            "context": {k: v for k, v in (t.context or {}).items() if not k.startswith("max_plan_")},
            "pr_url": pr_map.get(str(t.id)),
            "error_logs": logs_map.get(str(t.id)),
            "research_findings": research_map.get(str(t.id)),
            "history": history_map.get(str(t.id), []),
        }
        result_list.append(task_dict)

    # Global active count (not affected by pagination/filters)
    active_count = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING, TaskStatus.EVALUATING])
        )
    )).scalar() or 0
    queued_count = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED)
    )).scalar() or 0

    return {"tasks": result_list, "total": total, "active_count": active_count, "queued_count": queued_count}


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

        # Security approval: if unblocking a completed task with a PR, continue pipeline
        # instead of re-running — spawn the review that was skipped
        if was_blocked and not task.requires_approval and task.status == TaskStatus.COMPLETED:
            from app.main import _spawn_pipeline_task
            artifact = (await session.execute(
                select(Artifact).where(Artifact.task_id == task.id,
                                       Artifact.artifact_type == ArtifactType.PULL_REQUEST)
            )).scalars().first()
            if artifact and artifact.pr_url:
                pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
                branch = f"blueprint/task-{str(task.id).replace('-','')[:8]}"
                task_payload = (task.payloads or [{}])[0]
                base_branch = task_payload.get("base_branch", "main")
                original_prompt = task_payload.get("prompt", task.description or "")
                review_task = await _spawn_pipeline_task(
                    task, "review", session, pr_number, branch, base_branch,
                    original_prompt=original_prompt, original_task_id=str(task.id)
                )
                if review_task:
                    changes["spawned_review"] = str(review_task.id)
                    # Remove QUEUED status if the frontend also sent it — task stays COMPLETED
                    if "status" in payload and TaskStatus(payload["status"]) == TaskStatus.QUEUED:
                        task.status = TaskStatus.COMPLETED
                        changes["status"] = TaskStatus.COMPLETED

    if "payloads" in payload:
        task.payloads = payload["payloads"]
        changes["payloads"] = "updated"

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
