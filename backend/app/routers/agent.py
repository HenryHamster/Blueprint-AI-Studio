"""Agent-facing API endpoints (lifecycle + GitHub proxy)."""
import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.auth import verify_agent_jwt, create_agent_token
from app.database import get_session, AsyncSessionLocal
from app.models import (
    Project, Task, TaskStatus, TaskHistory, Artifact, ArtifactType,
    UserIntegration, _on_task_terminal_callbacks, _utcnow,
)
from app.schemas import (
    UploadUrlRequest, HeartbeatRequest, SpawnTaskRequest,
    GitHubCommitStatusRequest, GitHubPRCommentRequest,
    GitHubPRReviewCommentRequest, GitHubCreateIssueRequest,
    GitHubGetIssuesRequest, GitHubCreateBranchRequest, GitHubCreatePRRequest,
)
from app.shared_state import limiter
import app.shared_state as _shared_state
from app.storage import generate_upload_signed_url, ARTIFACT_BUCKET_NAME
from app.secrets_manager import get_user_secret
from app.github import (
    generate_installation_access_token,
    update_commit_status,
    create_pr_comment,
    create_pr_review_comment,
    create_issue,
    get_issues,
    create_branch,
    create_pull_request,
)
from app.broadcast import broadcast_to_project

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# ---------------------------------------------------------------------------
# JWT revocation state (moved from main.py)
# ---------------------------------------------------------------------------

# Maps task_id -> timestamp of termination. Entries older than _TERMINATED_TTL
# are evicted to prevent unbounded growth (JWTs expire after 8h).
_terminated_task_ids: dict[str, float] = {}
_TERMINATED_TTL = 9 * 3600  # 9h — slightly longer than 8h JWT max lifetime
_on_task_terminal_callbacks.append(lambda tid: _terminated_task_ids.__setitem__(tid, time.time()))

# TTL cache for "known active" tasks — avoids DB query per request.
_agent_active_cache: dict[str, float] = {}
_AGENT_CACHE_TTL = 60.0  # seconds
_AGENT_CACHE_MAX = 5000  # evict expired entries when cache exceeds this size


async def verify_agent_token(
    request: Request,
    x_agent_token: str = Header(..., description="Secret token for agent authentication"),
):
    """
    Validates the short-lived JWT passed by the agent VM.
    Rejects tokens for tasks that are no longer active (completed/failed/cancelled).
    The status endpoint is exempt because agents may retry their own completion POST
    (the endpoint has its own idempotency guard).
    """
    payload = verify_agent_jwt(x_agent_token)
    task_id = payload.get("sub")
    if not task_id:
        return payload

    # Fast path: known terminated (in-memory, O(1))
    # Allow status endpoint through — it has its own idempotency guard for retries
    if task_id in _terminated_task_ids:
        if request.url.path != "/api/agent/status":
            raise HTTPException(status_code=401, detail="Task is no longer active")
        return payload

    # Fast path: recently verified as active (TTL cache)
    cached_expiry = _agent_active_cache.get(task_id)
    if cached_expiry and time.time() < cached_expiry:
        return payload

    # Slow path: DB check (runs at most once per AGENT_CACHE_TTL per task)
    # Use app.dependency_overrides to respect test session overrides
    try:
        app = request.app
        session_factory = app.dependency_overrides.get(get_session, get_session)
        async for session in session_factory():
            task = await session.get(Task, uuid.UUID(task_id))
            if task and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                _terminated_task_ids[task_id] = time.time()
                # Allow status endpoint through — agents may retry their own completion POST
                if request.url.path != "/api/agent/status":
                    raise HTTPException(status_code=401, detail="Task is no longer active")
            # Task not found in DB — fail open (may not be committed yet, or test scenario)
    except HTTPException:
        raise
    except Exception as e:
        # DB error — fail open to avoid blocking agents during DB issues
        logger.warning(f"[AUTH] DB check failed for task {task_id}, allowing request: {e}")

    now = time.time()
    _agent_active_cache[task_id] = now + _AGENT_CACHE_TTL
    # Evict expired entries to prevent unbounded growth
    if len(_agent_active_cache) > _AGENT_CACHE_MAX:
        _agent_active_cache.clear()
    # Evict stale terminated entries (JWT expired, entry is redundant)
    if len(_terminated_task_ids) > _AGENT_CACHE_MAX:
        cutoff = now - _TERMINATED_TTL
        stale = [k for k, v in _terminated_task_ids.items() if v <= cutoff]
        for k in stale:
            _terminated_task_ids.pop(k, None)
    return payload


# ---------------------------------------------------------------------------
# GitHub token cache (moved from main.py)
# ---------------------------------------------------------------------------

_github_token_cache: Dict[str, Dict] = {}  # project_id -> {"token", "owner", "repo", "fetched_at"}
_github_token_cache_lock = asyncio.Lock()  # Prevent concurrent cache miss → duplicate token generation
_GITHUB_TOKEN_TTL = 3000  # 50 minutes (installation tokens expire after 60min)


def invalidate_github_token_cache(project_id: str) -> None:
    """Remove a project's cached GitHub token, forcing re-generation on next use."""
    removed = _github_token_cache.pop(project_id, None)
    if removed:
        logger.info(f"[GITHUB] Invalidated cached token for project {project_id}")


async def get_github_token_for_task(
    task_id: uuid.UUID, session: AsyncSession, force_refresh: bool = False,
) -> tuple[str, str, str]:
    stmt = select(Task).where(Task.id == task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project_id_str = str(task.project_id)

    # Return cached token if still fresh (installation tokens expire after 60min)
    if not force_refresh:
        async with _github_token_cache_lock:
            cached = _github_token_cache.get(project_id_str)
            if cached and (_utcnow() - cached["fetched_at"]).total_seconds() < _GITHUB_TOKEN_TTL:
                return cached["token"], cached["owner"], cached["repo"]

    project_stmt = select(Project).where(Project.id == task.project_id)
    project = (await session.execute(project_stmt)).scalars().first()
    if not project or not project.user_id:
        raise HTTPException(status_code=404, detail="Project or user not found")

    if not project.github_owner or not project.github_repo:
        raise HTTPException(status_code=403, detail="Project does not have an authorized GitHub repository")

    integration_stmt = select(UserIntegration).where(UserIntegration.user_id == project.user_id)
    integration = (await session.execute(integration_stmt)).scalars().first()

    # Prefer dynamic App-level discovery — always finds the correct installation
    # for this specific repo, even with multiple installations across orgs.
    token = None
    from app.github import get_repo_installation_token
    try:
        token = await get_repo_installation_token(project.github_owner, project.github_repo)
    except Exception:
        logger.debug(f"[GITHUB] App-level discovery failed for {project.github_owner}/{project.github_repo}, trying fallbacks")

    if not token and integration and integration.github_installation_id:
        try:
            token = await generate_installation_access_token(
                integration.github_installation_id,
                repositories=[project.github_repo]
            )
        except Exception:
            logger.debug(f"[GITHUB] Stored installation token failed for {project.github_owner}/{project.github_repo}")

    if not token and integration and integration.github_pat:
        token = await get_user_secret(integration.github_pat)

    if not token:
        raise HTTPException(
            status_code=400,
            detail="GitHub integration not found. Ensure the GitHub App is installed on this repository, or register a PAT via /api/github/pat."
        )

    # Track successful GitHub API interaction for passive health checking
    _shared_state.last_github_success = _utcnow()

    # Cache token to avoid re-generating for every API call within the same project
    async with _github_token_cache_lock:
        _github_token_cache[project_id_str] = {
            "token": token, "owner": project.github_owner, "repo": project.github_repo,
            "fetched_at": _utcnow()
        }
        # Evict stale entries
        stale_cutoff = _utcnow() - timedelta(seconds=_GITHUB_TOKEN_TTL + 600)
        for k in [k for k, v in _github_token_cache.items() if v["fetched_at"] < stale_cutoff]:
            _github_token_cache.pop(k, None)

    return token, project.github_owner, project.github_repo


# ---------------------------------------------------------------------------
# Helper: cycle detection for spawn dependency graphs
# ---------------------------------------------------------------------------

def _has_local_cycle(adj: Dict[str, list]) -> bool:
    """Check for cycles in a small adjacency list (dependency graph)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(adj, WHITE)
    def dfs(n):
        color[n] = GRAY
        for dep in adj.get(n, []):
            if dep not in color:
                continue  # External dep, not in our subgraph
            if color[dep] == GRAY:
                return True
            if color[dep] == WHITE and dfs(dep):
                return True
        color[n] = BLACK
        return False
    return any(color[n] == WHITE and dfs(n) for n in adj)


# ---------------------------------------------------------------------------
# Agent lifecycle endpoints
# ---------------------------------------------------------------------------

@router.post("/refresh-token")
@limiter.limit("60/minute")
async def agent_refresh_token(request: Request, token: dict = Depends(verify_agent_token)):
    """
    Accepts a valid (non-expired) agent JWT and returns a new one with a fresh 8-hour TTL.
    """
    new_token = create_agent_token(token["sub"], token["vm_instance_id"], task_type=token.get("task_type", ""))
    return {"token": new_token}


@router.post("/claim")
@limiter.limit("60/minute")
async def claim_task(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    stmt = select(Task).where(
        Task.vm_instance_id == token.get("vm_instance_id"),
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING])
    ).with_for_update(skip_locked=True)
    result = await session.execute(stmt)
    task = result.scalars().first()

    if not task:
        # Distinguish "no task" from "task locked by another process"
        check = await session.execute(
            select(Task.id).where(
                Task.vm_instance_id == token.get("vm_instance_id"),
                Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING])
            )
        )
        if check.scalars().first():
            raise HTTPException(status_code=503, detail="Task is temporarily locked, retry shortly")
        raise HTTPException(status_code=404, detail="No queued task found for this VM instance.")

    task.status = TaskStatus.IN_PROGRESS
    task.last_heartbeat = _utcnow()  # Immediate heartbeat — don't wait 30s for agent interval
    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Broadcast status change to frontend
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.IN_PROGRESS}
    )

    # Strip internal fields (max_plan_account_id) from context before returning to agent
    safe_context = {k: v for k, v in (task.context or {}).items() if not k.startswith("max_plan_")}
    response = {
        "task_id": str(task.id),
        "title": task.title,
        "description": task.description,
        "payloads": task.payloads,
        "context": safe_context
    }

    task_type = task.first_payload.get("type", "")
    if task_type in ("research", "plan", "create", "fix", "refactor", "review"):
        # Active tasks (IN_PROGRESS or EVALUATING) in the same project
        active_stmt = select(Task).where(
            Task.project_id == task.project_id,
            Task.id != task.id,
            Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING])
        )
        active_tasks_rows = (await session.execute(active_stmt)).scalars().all()
        active_tasks = []
        for t in active_tasks_rows:
            p = t.first_payload
            active_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "task_type": p.get("type", ""),
                "status": t.status,
                "prompt": p.get("prompt", p.get("description", "")),
                "related_files": (t.context or {}).get("related_files", []),
                "direction_id": (t.context or {}).get("direction_id"),
            })

        # Queued tasks in the same project
        queued_stmt = select(Task).where(
            Task.project_id == task.project_id,
            Task.id != task.id,
            Task.status == TaskStatus.QUEUED
        )
        queued_tasks_rows = (await session.execute(queued_stmt)).scalars().all()
        queued_tasks = []
        for t in queued_tasks_rows:
            p = t.first_payload
            queued_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "task_type": p.get("type", ""),
                "prompt": p.get("prompt", p.get("description", "")),
                "related_files": (t.context or {}).get("related_files", []),
                "direction_id": (t.context or {}).get("direction_id"),
            })

        # Recently completed tasks (10 most recent): completed first, then failed/cancelled
        completed_stmt = select(Task).where(
            Task.project_id == task.project_id,
            Task.id != task.id,
            Task.status == TaskStatus.COMPLETED
        ).order_by(Task.created_at.desc()).limit(10)
        completed_tasks_rows = list((await session.execute(completed_stmt)).scalars().all())

        failed_cancelled_stmt = select(Task).where(
            Task.project_id == task.project_id,
            Task.id != task.id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED])
        ).order_by(Task.created_at.desc()).limit(max(0, 15 - len(completed_tasks_rows)))
        failed_cancelled_rows = (await session.execute(failed_cancelled_stmt)).scalars().all()
        completed_tasks_rows = completed_tasks_rows + list(failed_cancelled_rows)

        completed_task_ids = [t.id for t in completed_tasks_rows]
        completed_artifacts: list = []
        if completed_task_ids:
            art_stmt = select(Artifact).where(
                Artifact.task_id.in_(completed_task_ids),
                Artifact.artifact_type.in_([ArtifactType.PULL_REQUEST, ArtifactType.RESEARCH_FINDINGS])
            )
            completed_artifacts = (await session.execute(art_stmt)).scalars().all()

        artifacts_by_completed_task: dict = {}
        for a in completed_artifacts:
            artifacts_by_completed_task.setdefault(a.task_id, []).append(a)

        from app.failure_classification import classify_failure
        recently_completed_tasks = []
        for t in completed_tasks_rows:
            p = t.first_payload
            task_artifacts = artifacts_by_completed_task.get(t.id, [])
            pr_url = t.context.get("rollup_pr_url") if t.context else None
            if not pr_url:
                for a in task_artifacts:
                    if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                        pr_url = a.pr_url
                        break
            research_findings_summary = None
            for a in task_artifacts:
                if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                    research_findings_summary = a.logs[:8000]
                    break
            recently_completed_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "task_type": p.get("type", ""),
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "pr_url": pr_url,
                "research_findings_summary": research_findings_summary,
                "failure_reason": (t.latest_status_message or "")[:500] if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED) and t.latest_status_message else None,
                "failure_class": classify_failure(t) if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED) and t.latest_status_message else None,
                "review_iterations": (t.context or {}).get("review_loop", {}).get("iteration", 0) if (t.context or {}).get("review_loop") else None,
            })

        # Build lessons from high-iteration tasks (3+ review cycles)
        lessons = []
        high_iter_stmt = (
            select(Task)
            .where(
                Task.project_id == task.project_id,
                Task.status.in_([TaskStatus.FAILED, TaskStatus.COMPLETED]),
            )
            .order_by(Task.created_at.desc())
            .limit(50)
        )
        high_iter_rows = (await session.execute(high_iter_stmt)).scalars().all()
        for ht in high_iter_rows:
            review_loop = (ht.context or {}).get("review_loop", {})
            if not review_loop:
                continue
            iteration = review_loop.get("iteration", 0)
            if iteration < 2:
                continue
            review_hist = review_loop.get("review_history", [])
            if review_hist:
                last_feedback = review_hist[-1].get("feedback", "")[:500]
                lessons.append({
                    "task_title": ht.title,
                    "iterations": len(review_hist) + 1,
                    "last_feedback": last_feedback,
                })
            if len(lessons) >= 8:
                break

        # Second pass: collect CODE_ERROR failures with no review loop
        if len(lessons) < 8:
            for ht in high_iter_rows:
                if (ht.context or {}).get("review_loop"):
                    continue  # already handled above
                if ht.status == TaskStatus.FAILED and ht.latest_status_message:
                    fc = classify_failure(ht)
                    if fc == "CODE_ERROR":
                        lessons.append({
                            "task_title": ht.title,
                            "iterations": 0,
                            "last_feedback": (ht.latest_status_message or "")[:500],
                            "failure_class": "CODE_ERROR",
                        })
                if len(lessons) >= 8:
                    break

        # Open PRs: distinct pr_url from PULL_REQUEST artifacts for non-terminal tasks in project
        open_prs_stmt = (
            select(Artifact.pr_url)
            .join(Task, Artifact.task_id == Task.id)
            .where(
                Task.project_id == task.project_id,
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                Artifact.pr_url.isnot(None),
                Task.status.not_in([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
            )
            .distinct()
            .limit(20)
        )
        open_prs_rows = (await session.execute(open_prs_stmt)).scalars().all()

        sibling_tasks = []
        if task.parent_task_id:
            sibling_result = await session.execute(
                select(Task).where(
                    Task.parent_task_id == task.parent_task_id,
                    Task.id != task.id,
                    Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
            )
            for sib in sibling_result.scalars().all():
                sibling_tasks.append({
                    "id": str(sib.id),
                    "title": sib.title,
                    "status": sib.status,
                    "related_files": (sib.context or {}).get("related_files", []),
                    "task_type": (sib.first_payload).get("type", ""),
                })

        # Pipeline health summary (for gated task types; else-branch covers the rest)
        pipeline_health = None
        try:
            from app.routers.admin_analytics import compute_pipeline_health_summary
            pipeline_health = await compute_pipeline_health_summary(session, task.project_id)
        except Exception:
            pass  # non-critical — don't fail the request

        response["project_context"] = {
            "active_tasks": active_tasks,
            "queued_tasks": queued_tasks,
            "recently_completed_tasks": recently_completed_tasks,
            "open_prs": list(open_prs_rows),
            "sibling_tasks": sibling_tasks,
            "lessons": lessons,
            "pipeline_health": pipeline_health,
        }
    else:
        # Lightweight pipeline health for all other task types
        try:
            from app.routers.admin_analytics import compute_pipeline_health_summary
            pipeline_health = await compute_pipeline_health_summary(session, task.project_id)
            if pipeline_health:
                response["project_context"] = {"pipeline_health": pipeline_health}
        except Exception:
            pass

    # Task types that benefit from untagged operational playbook entries.
    # Types not in this set only receive entries with explicit applicable_types matching them.
    _PLAYBOOK_DEFAULT_TYPES = {
        "create", "fix", "refactor", "review", "qa", "smoketest",
        "deps", "migrate", "debug", "docs",
    }

    # Inject playbook for ALL task types (outside the project_context gate)
    project_for_playbook = await session.get(Project, task.project_id)
    if project_for_playbook and project_for_playbook.design_doc:
        task_type = (task.first_payload).get("type", "create")
        playbook_entries = {}
        for k, v in (project_for_playbook.design_doc or {}).items():
            if k.startswith("playbook/") and isinstance(v, dict):
                applicable_types = v.get("applicable_types")
                if applicable_types is not None:
                    if task_type not in applicable_types:
                        continue
                elif task_type not in _PLAYBOOK_DEFAULT_TYPES:
                    continue
                short_key = k[len("playbook/"):]
                playbook_entries[short_key] = v.get("content", "")
        if playbook_entries:
            response["playbook"] = playbook_entries

    return response

@router.post("/artifacts/upload-url")
@limiter.limit("30/minute")
async def get_upload_url(
    request: Request,
    req: UploadUrlRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agents call this to get a temporary Signed URL to upload screenshots/logs directly to GCS.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    stmt = select(Task).where(Task.id == req.task_id)
    task = (await session.execute(stmt)).scalars().first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    import os
    safe_filename = os.path.basename(req.filename)
    url = await generate_upload_signed_url(str(req.task_id), safe_filename)
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/tasks/{req.task_id}/{safe_filename}"

    return {
        "upload_url": url,
        "storage_uri": storage_uri
    }

@router.post("/heartbeat")
@limiter.limit("120/minute")
async def agent_heartbeat(
    request: Request,
    req: HeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Endpoint for agents to send a periodic heartbeat.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # SKIP LOCKED: if the status endpoint holds FOR UPDATE on this row
    # (processing completion/pipeline), skip instead of blocking.  Blocking
    # would starve the heartbeat (agent's 30s HTTP timeout fires before the
    # 10s lock_timeout, but retries compound and the lock holder may take
    # longer than the timeout), causing last_heartbeat to go stale and the
    # reaper to kill the task.
    # Skipping one heartbeat is safe — the reaper's threshold is 5 minutes,
    # and the next heartbeat (30s later) will succeed once the lock releases.
    stmt = select(Task).where(
        Task.id == req.task_id,
        Task.vm_instance_id == token.get("vm_instance_id")
    ).with_for_update(skip_locked=True)
    task = (await session.execute(stmt)).scalars().first()

    if not task:
        # Row is either locked (skip_locked skipped it) or vm_instance_id changed.
        # Distinguish: check without FOR UPDATE to see if the task exists at all.
        check_stmt = select(Task).where(Task.id == req.task_id)
        check_task = (await session.execute(check_stmt)).scalars().first()
        if not check_task:
            raise HTTPException(status_code=404, detail="Task not found")
        if check_task.vm_instance_id != token.get("vm_instance_id"):
            raise HTTPException(status_code=404, detail="Task not found for this VM")
        # Row exists but was locked — tell agent to retry next cycle
        return JSONResponse(
            status_code=409,
            content={"status": "locked", "detail": "Task row locked, retry next cycle"},
            headers={"Retry-After": "30"},
        )

    # Signal agent to stop if task was cancelled/completed externally
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise HTTPException(status_code=409, detail="Task already terminal")

    task.last_heartbeat = _utcnow()
    if task.vm_started_at is None:
        task.vm_started_at = _utcnow()
    if req.status_message:
        task.latest_status_message = req.status_message
    if req.metrics:
        ctx = dict(task.context or {})
        ctx['last_metrics'] = req.metrics.model_dump(exclude_none=True)
        task.context = ctx

    session.add(task)
    await session.commit()

    return {"status": "ok"}

@router.post("/spawn")
@limiter.limit("60/minute")
async def spawn_subtask(
    request: Request,
    req: SpawnTaskRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Endpoint for agents to request the creation of a new sub-task/VM.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # Get parent task
    parent_task = await session.get(Task, req.task_id)
    if not parent_task:
        raise HTTPException(status_code=404, detail="Parent task not found")

    # Depth guard: prevent unbounded recursive spawning
    MAX_SPAWN_DEPTH = 5
    spawn_depth = (req.context or {}).get("spawn_depth", 0)
    if not spawn_depth:
        # Infer depth from parent chain
        depth = 0
        current = parent_task
        while current.parent_task_id and depth < MAX_SPAWN_DEPTH:
            current = await session.get(Task, current.parent_task_id)
            if not current:
                break
            depth += 1
        spawn_depth = depth
    if spawn_depth >= MAX_SPAWN_DEPTH:
        raise HTTPException(status_code=400, detail=f"Spawn depth limit ({MAX_SPAWN_DEPTH}) exceeded")

    # When a plan agent spawns children it immediately posts a status update that triggers
    # backend enrichment (_handle_plan_completion).  There is a window between task creation
    # here and the enrichment commit where a worker could pick up a child with the wrong
    # base_branch (parent's branch instead of the plan branch).  Hold children with
    # requires_approval=True so the orchestrator ignores them until enrichment sets the
    # correct value based on child_risk.
    is_plan_spawn = bool((req.context or {}).get("agent_spawned"))

    new_tasks = []
    for payload in req.payloads:
        parent_payload = parent_task.first_payload
        parent_context = {
            "parent_task_id": str(parent_task.id),
            "parent_title": parent_task.title,
            "parent_prompt": (parent_payload.get("prompt") or parent_payload.get("description") or "")[:500]
        }
        new_task = Task(
            project_id=parent_task.project_id,
            title=payload.get("title", f"Subtask of {parent_task.title}"),
            description=payload.get("description", ""),
            payloads=[payload],
            context={**(req.context or {}), "spawn_depth": spawn_depth + 1, "parent_context": parent_context},
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by=f"AGENT_{parent_task.id}",
            # Plan-spawned children are held (requires_approval=True) until _handle_plan_completion
            # enriches them with the correct base_branch and sets the real approval flag.
            requires_approval=True if is_plan_spawn else req.requires_approval,
            parent_task_id=parent_task.id,
            **({"priority": req.priority} if req.priority is not None else {}),
        )
        session.add(new_task)
        new_tasks.append(new_task)

    await session.commit()

    # Log history for each new task
    for task in new_tasks:
        await session.refresh(task)
        history = TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": f"AGENT_{parent_task.id}",
                "requires_approval": req.requires_approval,
                "context": req.context
            }
        )
        session.add(history)

    await session.commit()

    return {
        "message": f"Successfully spawned {len(new_tasks)} subtasks",
        "task_ids": [str(t.id) for t in new_tasks],
        "requires_approval": req.requires_approval
    }


# ---------------------------------------------------------------------------
# GitHub proxy endpoints
# ---------------------------------------------------------------------------

@router.get("/github/token")
@limiter.limit("30/minute")
async def agent_github_get_token(
    request: Request,
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to retrieve the GitHub token for cloning repositories.
    """
    if str(task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(task_id, session)
    return {
        "status": "success",
        "data": {
            "token": gh_token,
            "owner": owner,
            "repo": repo
        }
    }

@router.post("/github/commit-status")
@limiter.limit("30/minute")
async def agent_github_commit_status(
    request: Request,
    req: GitHubCommitStatusRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to update the commit status on GitHub.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await update_commit_status(
        token=gh_token,
        owner=owner,
        repo=repo,
        sha=req.sha,
        state=req.state,
        description=req.description,
        context=req.context,
        target_url=req.target_url
    )
    return {"status": "success", "data": result}

@router.post("/github/pr-comment")
@limiter.limit("30/minute")
async def agent_github_pr_comment(
    request: Request,
    req: GitHubPRCommentRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to post a general comment to a PR.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_pr_comment(
        token=gh_token,
        owner=owner,
        repo=repo,
        pr_number=req.pr_number,
        body=req.body
    )
    return {"status": "success", "data": result}

@router.post("/github/pr-review-comment")
@limiter.limit("30/minute")
async def agent_github_pr_review_comment(
    request: Request,
    req: GitHubPRReviewCommentRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to post an inline code review comment to a PR.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_pr_review_comment(
        token=gh_token,
        owner=owner,
        repo=repo,
        pr_number=req.pr_number,
        commit_id=req.commit_id,
        path=req.path,
        line=req.line,
        body=req.body
    )
    return {"status": "success", "data": result}

@router.post("/github/issues")
@limiter.limit("30/minute")
async def agent_github_create_issue(
    request: Request,
    req: GitHubCreateIssueRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to create a new issue.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_issue(
        token=gh_token,
        owner=owner,
        repo=repo,
        title=req.title,
        body=req.body,
        assignees=req.assignees
    )
    return {"status": "success", "data": result}

@router.get("/github/issues")
@limiter.limit("30/minute")
async def agent_github_get_issues(
    request: Request,
    req: GitHubGetIssuesRequest = Depends(),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to retrieve issues.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await get_issues(
        token=gh_token,
        owner=owner,
        repo=repo,
        state=req.state
    )
    return {"status": "success", "data": result}

@router.post("/github/branches")
@limiter.limit("30/minute")
async def agent_github_create_branch(
    request: Request,
    req: GitHubCreateBranchRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to create a new branch.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_branch(
        token=gh_token,
        owner=owner,
        repo=repo,
        branch_name=req.branch_name,
        sha=req.sha
    )
    return {"status": "success", "data": result}

@router.post("/github/pull-requests")
@limiter.limit("30/minute")
async def agent_github_create_pr(
    request: Request,
    req: GitHubCreatePRRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to create a pull request.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # Idempotency: if this task already has a PR artifact, return it
    existing_artifact = (await session.execute(
        select(Artifact).where(
            Artifact.task_id == req.task_id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST
        )
    )).scalars().first()
    if existing_artifact and existing_artifact.pr_url:
        logger.info(f"[PR] Task {req.task_id} already has PR artifact: {existing_artifact.pr_url}")
        return {"status": "success", "data": {"html_url": existing_artifact.pr_url}}

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_pull_request(
        token=gh_token,
        owner=owner,
        repo=repo,
        title=req.title,
        head=req.head,
        base=req.base,
        body=req.body
    )

    # Save artifact immediately so the PR is linked even if the agent is reaped
    # before it can report COMPLETED status.  Without this, orphan recovery can't
    # find the PR and the pipeline stalls.
    pr_url = result.get("html_url", "")
    if pr_url:
        artifact = Artifact(task_id=req.task_id, artifact_type=ArtifactType.PULL_REQUEST, pr_url=pr_url)
        session.add(artifact)
        await session.commit()
        logger.info(f"[PR] Saved PR artifact for task {req.task_id}: {pr_url}")

    return {"status": "success", "data": result}
