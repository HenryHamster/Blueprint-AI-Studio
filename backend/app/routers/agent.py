"""Agent-facing API endpoints (lifecycle + GitHub proxy)."""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
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
    GitHubPRReviewCommentRequest, GitHubSubmitPRReviewRequest, GitHubCreateIssueRequest,
    GitHubGetIssuesRequest, GitHubCreateBranchRequest, GitHubCreatePRRequest,
)
from app.payload_schemas import validate_task_payload
from app.shared_state import limiter
import app.shared_state as _shared_state
from app.storage import generate_upload_signed_url, ARTIFACT_BUCKET_NAME
from app.secrets_manager import get_user_secret
from app.github import (
    generate_installation_access_token,
    update_commit_status,
    create_pr_comment,
    create_pr_review_comment,
    submit_pr_review,
    create_issue,
    get_issues,
    create_branch,
    create_pull_request,
    get_pull_request,
    create_check_run,
)
from app.broadcast import broadcast_to_project

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


async def _create_review_check_run_bg(
    task_id: uuid.UUID,
    pr_number: int,
    check_run_name: str,
) -> None:
    """Background helper: create an in_progress Check Run when a review task claims work.

    Opens its own DB session so it can run after the claim response is returned.
    Stores the check_run_id in task.context so the verdict handler can update it later.
    Errors are swallowed — check run creation is best-effort and must not affect the pipeline.
    """
    try:
        async with AsyncSessionLocal() as session:
            gh_token, owner, repo = await get_github_token_for_task(task_id, session)
            pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            head_sha = pr_data.get("head", {}).get("sha", "")
            if not head_sha:
                logger.warning(f"[CHECK RUN] No head_sha for PR #{pr_number} task {task_id}")
                return
            result = await create_check_run(gh_token, owner, repo, head_sha, name=check_run_name)
            if result and result.get("id"):
                task = await session.get(Task, task_id)
                if task:
                    task.context = {**(task.context or {}), "check_run_id": result["id"]}
                    session.add(task)
                    await session.commit()
                    logger.info(f"[CHECK RUN] Created check run {result['id']} for review task {task_id} PR #{pr_number}")
    except Exception as e:  # noqa: BLE001 — best-effort background task, never block claim response
        logger.warning(f"[CHECK RUN] Failed to create check run for task {task_id}: {e}")


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

# Retain strong references to fire-and-forget background tasks so the event loop
# doesn't GC them before completion (asyncio only holds a weak reference).
_bg_tasks: set[asyncio.Task] = set()


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
    except Exception as e:  # noqa: BLE001 — fail open on DB errors so agents aren't blocked by transient DB issues
        logger.warning(f"[AUTH] DB check failed for task {task_id}, allowing request: {e}")

    now = time.time()
    _agent_active_cache[task_id] = now + _AGENT_CACHE_TTL
    # Evict expired entries to prevent unbounded growth
    if len(_agent_active_cache) > _AGENT_CACHE_MAX:
        stale = [k for k, v in _agent_active_cache.items() if v <= now]
        for k in stale:
            _agent_active_cache.pop(k, None)
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
    except Exception:  # noqa: BLE001 — fall through to stored-installation + PAT fallbacks
        logger.debug(f"[GITHUB] App-level discovery failed for {project.github_owner}/{project.github_repo}, trying fallbacks")

    if not token and integration and integration.github_installation_id:
        try:
            token = await generate_installation_access_token(
                integration.github_installation_id,
                repositories=[project.github_repo]
            )
        except Exception:  # noqa: BLE001 — fall through to PAT fallback
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
# Helper: parse a Failed Approaches entry into {text, failure_type}
# ---------------------------------------------------------------------------

_KNOWN_FAILURE_CATEGORIES = frozenset({
    "CODE_ERROR", "PROVISION_FAILURE", "AGENT_TIMEOUT", "EXTERNAL_API", "RATE_LIMIT",
    "rejected in review",
})


def _parse_failed_approach(line: str) -> dict:
    """Parse '<title> (<category>) [at=<iso> stage=<stage>] — <reason>'.

    Returns {text, failure_type, failed_at, failure_stage}. Direction outcomes
    write entries with the failure category in parentheses immediately after the
    title, optionally followed by a ``[at=YYYY-MM-DD stage=<stage>]`` metadata
    block. Extract those so agents can reason about *why* a prior approach
    failed, *when* it failed, and *at what stage*. Only recognise parens whose
    contents match a known category so prose-level parens don't get treated as
    metadata.
    """
    failure_type = ""
    failed_at = ""
    failure_stage = ""
    text = line
    open_idx = line.find("(")
    if open_idx > 0:
        close_idx = line.find(")", open_idx)
        if close_idx > open_idx:
            inner = line[open_idx + 1:close_idx].strip()
            if inner in _KNOWN_FAILURE_CATEGORIES:
                failure_type = inner
                rest = line[close_idx + 1:]
                # Look for a [at=... stage=...] metadata block that follows the
                # category paren with only whitespace in between.
                meta_start = -1
                for i, ch in enumerate(rest):
                    if ch == "[":
                        meta_start = i
                        break
                    if not ch.isspace():
                        break
                if meta_start >= 0:
                    meta_end = rest.find("]", meta_start)
                    if meta_end > meta_start:
                        meta = rest[meta_start + 1:meta_end]
                        for part in meta.split():
                            if part.startswith("at="):
                                failed_at = part[len("at="):]
                            elif part.startswith("stage="):
                                failure_stage = part[len("stage="):]
                        rest = rest[:meta_start].rstrip() + rest[meta_end + 1:]
                stripped = (line[:open_idx].rstrip() + rest).strip()
                text = stripped if stripped else line
    return {
        "text": text[:200],
        "failure_type": failure_type,
        "failed_at": failed_at,
        "failure_stage": failure_stage,
    }


# ---------------------------------------------------------------------------
# Helper: failure stage pattern analysis
# ---------------------------------------------------------------------------

def _compute_failure_stage_pattern(history: list) -> "dict | None":
    """
    Analyze recent research_history stage_dropout data to identify dominant failure stages.

    Returns a pattern dict if 60%+ of non-infra failures cluster at one stage, else None.
    Considers code_error, approval, and staging_only stages — agents can't address infra failures.
    Uses the last 3 research cycles for recency.
    """
    recent = history[-3:]
    code_error_total = 0
    approval_total = 0
    staging_only_total = 0
    for entry in recent:
        sd = entry.get("stage_dropout") or {}
        code_error_total += sd.get("tasks_failed_code_error", 0)
        approval_total += sd.get("tasks_required_approval", 0)
        staging_only_total += sd.get("tasks_merged_staging_only", 0)
    total_non_infra = code_error_total + approval_total + staging_only_total
    if total_non_infra < 3:
        return None
    code_error_rate = code_error_total / total_non_infra
    approval_rate = approval_total / total_non_infra
    staging_only_rate = staging_only_total / total_non_infra
    threshold = 0.6
    if code_error_rate >= threshold:
        return {
            "stage": "code_error",
            "rate": round(code_error_rate, 2),
            "message": (
                "Recent tasks for this direction frequently fail at the code/test stage — "
                "pay extra attention to test coverage and run tests early."
            ),
        }
    if approval_rate >= threshold:
        return {
            "stage": "approval",
            "rate": round(approval_rate, 2),
            "message": (
                "Recent tasks for this direction frequently stall waiting for approval — "
                "consider smaller, more targeted changes that require less manual review."
            ),
        }
    if staging_only_rate >= threshold:
        return {
            "stage": "staging_only",
            "rate": round(staging_only_rate, 2),
            "message": (
                "Recent tasks for this direction frequently merge to staging but fail the rollup to main — "
                "watch for integration-level issues like merge conflicts and CI failures against the target branch."
            ),
        }
    return None


# ---------------------------------------------------------------------------
# Agent lifecycle endpoints
# ---------------------------------------------------------------------------

@router.post("/refresh-token")
@limiter.limit("120/minute")
async def agent_refresh_token(request: Request, token: dict = Depends(verify_agent_token)):
    """
    Accepts a valid (non-expired) agent JWT and returns a new one with a fresh 8-hour TTL.
    """
    new_token = create_agent_token(token["sub"], token["vm_instance_id"], task_type=token.get("task_type", ""))
    return {"token": new_token}


@router.post("/claim")
@limiter.limit("200/minute")
async def claim_task(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    vm_id = token.get("vm_instance_id")
    stmt = select(Task).where(
        Task.vm_instance_id == vm_id,
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING])
    ).with_for_update(skip_locked=True)
    result = await session.execute(stmt)
    task = result.scalars().first()

    idempotent_replay = False
    if not task:
        # Distinguish "no task" from "task locked by another process"
        check = await session.execute(
            select(Task.id).where(
                Task.vm_instance_id == vm_id,
                Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING])
            )
        )
        if check.scalars().first():
            # Use 429 (not 503) so Cloud Run doesn't classify this as ContainerError
            # and quarantine the instance — see commit 88a77903 for the same pattern
            # applied to agent_status.py.
            raise HTTPException(
                status_code=429,
                detail="Task is temporarily locked, retry shortly",
                headers={"Retry-After": "2"},
            )
        # Idempotency window: apiPost retries on network errors / 30s timeout
        # aborts, but the first POST may have already committed PROVISIONING →
        # IN_PROGRESS server-side before the response was lost. Without this
        # replay, the retry hits the QUEUED/PROVISIONING filter, returns 404,
        # and the agent dies with PRE_CLAIM_FAILED on a task that was actually
        # claimed successfully. Match this VM's just-claimed task (last heartbeat
        # within 3 min — longer than the agent's worst-case retry window of
        # API_TIMEOUT * API_MAX_RETRIES with backoff) and replay the same response.
        replay_cutoff = _utcnow() - timedelta(minutes=3)
        replay_stmt = select(Task).where(
            Task.vm_instance_id == vm_id,
            Task.status == TaskStatus.IN_PROGRESS,
            Task.last_heartbeat >= replay_cutoff,
        )
        task = (await session.execute(replay_stmt)).scalars().first()
        if not task:
            raise HTTPException(status_code=404, detail="No queued task found for this VM instance.")
        idempotent_replay = True

    if not idempotent_replay:
        task.status = TaskStatus.IN_PROGRESS
        task.last_heartbeat = _utcnow()  # Immediate heartbeat — don't wait 30s for agent interval
        # Record time-to-claim so we can measure the real distribution of VM cold-boot
        # + auth validation + claim and tighten the reaper's pre-claim threshold later.
        if task.vm_provisioned_at:
            claim_latency = (task.last_heartbeat - task.vm_provisioned_at).total_seconds()
            task.context = {**(task.context or {}), "claim_latency_secs": round(claim_latency, 1)}
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

    # Add project metadata so agents can adapt to different project structures
    project = await session.get(Project, task.project_id)
    if project:
        response["project_metadata"] = {
            "project_id": str(project.id),
            "project_name": project.name,
            "github_owner": project.github_owner,
            "github_repo": project.github_repo,
            "is_self_repo": project.is_self_repo,
            "build_config": project.build_config or {},
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
            t_ctx = t.context or {}
            t_prompt = (p.get("prompt") or p.get("description") or t.description or "")[:300]
            t_related_files = (t_ctx.get("related_files") or [])[:10]
            t_direction_id = t_ctx.get("direction_id")
            is_failed = t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            t_changed_files = (t_ctx.get("_pr_changed_files") or [])[:10] or None
            recently_completed_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "task_type": p.get("type", ""),
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "pr_url": pr_url,
                "research_findings_summary": research_findings_summary,
                "failure_reason": (t.latest_status_message or "")[:500] if is_failed and t.latest_status_message else None,
                "failure_class": classify_failure(t) if is_failed and t.latest_status_message else None,
                "review_iterations": (t_ctx.get("review_loop", {}) or {}).get("iteration", 0) if t_ctx.get("review_loop") else None,
                "prompt": t_prompt or None,
                "related_files": t_related_files,
                "direction_id": t_direction_id,
                "changed_files": t_changed_files,
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
                last_feedback = review_hist[-1].get("feedback", "")[:300]
                first_entry = next(
                    (e for e in review_hist if e.get("iteration") == 0),
                    review_hist[0],
                )
                first_feedback = first_entry.get("feedback", "")[:300]
                lesson = {
                    "task_title": ht.title,
                    "iterations": len(review_hist) + 1,
                    "last_feedback": last_feedback,
                    "direction_id": (ht.context or {}).get("direction_id"),
                }
                if first_feedback and first_feedback != last_feedback:
                    lesson["first_feedback"] = first_feedback
                lessons.append(lesson)
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
                            "direction_id": (ht.context or {}).get("direction_id"),
                        })
                if len(lessons) >= 8:
                    break

        # Direction-filtered recent tasks: up to 5 completed/failed from the same direction
        current_direction_id = (task.context or {}).get("direction_id")
        direction_recent_tasks = []
        if current_direction_id:
            dir_stmt = select(Task).where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]),
            ).order_by(Task.created_at.desc()).limit(50)
            dir_rows = (await session.execute(dir_stmt)).scalars().all()
            dir_matches = [
                t for t in dir_rows
                if (t.context or {}).get("direction_id") == current_direction_id
            ][:5]
            dir_match_ids = [t.id for t in dir_matches]
            dir_artifacts_by_task: dict = {}
            if dir_match_ids:
                dir_art_stmt = select(Artifact).where(
                    Artifact.task_id.in_(dir_match_ids),
                    Artifact.artifact_type.in_([ArtifactType.PULL_REQUEST, ArtifactType.RESEARCH_FINDINGS]),
                )
                for a in (await session.execute(dir_art_stmt)).scalars().all():
                    dir_artifacts_by_task.setdefault(a.task_id, []).append(a)
            for t in dir_matches:
                p = t.first_payload
                t_ctx = t.context or {}
                t_arts = dir_artifacts_by_task.get(t.id, [])
                pr_url = t_ctx.get("rollup_pr_url")
                if not pr_url:
                    for a in t_arts:
                        if a.artifact_type == ArtifactType.PULL_REQUEST and a.pr_url:
                            pr_url = a.pr_url
                            break
                research_findings_summary = None
                for a in t_arts:
                    if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
                        research_findings_summary = a.logs[:8000]
                        break
                is_failed = t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                t_prompt = (p.get("prompt") or p.get("description") or t.description or "")[:300]
                direction_recent_tasks.append({
                    "id": str(t.id),
                    "title": t.title,
                    "task_type": p.get("type", ""),
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "pr_url": pr_url,
                    "research_findings_summary": research_findings_summary,
                    "failure_reason": (t.latest_status_message or "")[:500] if is_failed and t.latest_status_message else None,
                    "failure_class": classify_failure(t) if is_failed and t.latest_status_message else None,
                    "review_iterations": (t_ctx.get("review_loop", {}) or {}).get("iteration", 0) if t_ctx.get("review_loop") else None,
                    "prompt": t_prompt or None,
                    "related_files": (t_ctx.get("related_files") or [])[:10],
                    "direction_id": t_ctx.get("direction_id"),
                    "scope": t_ctx.get("scope"),
                    "changed_files": (t_ctx.get("_pr_changed_files") or [])[:10] or None,
                })

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
            sibling_tasks.extend(
                {
                    "id": str(sib.id),
                    "title": sib.title,
                    "status": sib.status,
                    "related_files": (sib.context or {}).get("related_files", []),
                    "task_type": (sib.first_payload).get("type", ""),
                    "active": True,
                }
                for sib in sibling_result.scalars().all()
            )

            terminal_sibling_result = await session.execute(
                select(Task).where(
                    Task.parent_task_id == task.parent_task_id,
                    Task.id != task.id,
                    Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
                ).order_by(Task.created_at.desc()).limit(5)
            )
            terminal_siblings = list(terminal_sibling_result.scalars().all())
            terminal_sibling_pr_urls: dict = {}
            if terminal_siblings:
                sib_art_stmt = select(Artifact).where(
                    Artifact.task_id.in_([s.id for s in terminal_siblings]),
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    Artifact.pr_url.isnot(None),
                )
                for art in (await session.execute(sib_art_stmt)).scalars().all():
                    terminal_sibling_pr_urls.setdefault(art.task_id, art.pr_url)
            for sib in terminal_siblings:
                sib_status = sib.status.value if hasattr(sib.status, "value") else str(sib.status)
                pr_url = (sib.context or {}).get("rollup_pr_url") or terminal_sibling_pr_urls.get(sib.id)
                entry = {
                    "id": str(sib.id),
                    "title": sib.title,
                    "status": sib_status,
                    "related_files": (sib.context or {}).get("related_files", []),
                    "task_type": (sib.first_payload).get("type", ""),
                    "active": False,
                    "pr_url": pr_url,
                }
                if sib.status == TaskStatus.FAILED and sib.latest_status_message:
                    entry["failure_class"] = classify_failure(sib)
                    entry["error_excerpt"] = sib.latest_status_message[:150]
                sib_changed = ((sib.context or {}).get("_pr_changed_files") or [])[:5]
                if sib_changed:
                    entry["changed_files"] = sib_changed
                sibling_tasks.append(entry)

        # Pipeline health summary (for gated task types; else-branch covers the rest)
        pipeline_health = None
        try:
            from app.routers.admin_digest import compute_pipeline_health_summary
            pipeline_health = await compute_pipeline_health_summary(session, task.project_id)
        except Exception:
            logger.exception("[AGENT] Failed to compute pipeline health summary (non-critical)")

        # Aggregate review issue distribution for create/fix/refactor tasks (last 14 days)
        review_issue_distribution = None
        top_first_review_issues = None
        direction_top_first_review_issues = None
        scope_specific_review_issues = None
        current_task_scope = (task.context or {}).get("scope")
        if task_type in ("create", "fix", "refactor"):
            try:
                from app.playbook_learning import _categorize_review_issues
                cutoff_14d = _utcnow() - timedelta(days=14)
                combined_review_history = []
                for ht in high_iter_rows:
                    if ht.created_at and ht.created_at < cutoff_14d:
                        continue
                    review_loop = (ht.context or {}).get("review_loop", {})
                    rev_hist = review_loop.get("review_history", []) if review_loop else []
                    if rev_hist:
                        combined_review_history.extend(rev_hist)
                if combined_review_history:
                    buckets = _categorize_review_issues(combined_review_history)
                    total_issues = sum(len(v) for v in buckets.values())
                    if total_issues >= 5:
                        sorted_cats = sorted(buckets.items(), key=lambda x: -len(x[1]))
                        top3 = sorted_cats[:3]
                        review_issue_distribution = {
                            cat: round(len(issues) / total_issues, 2)
                            for cat, issues in top3
                        }

                from app.playbook_learning import compute_recurring_first_review_descriptions
                try:
                    top_first_review_issues = compute_recurring_first_review_descriptions(
                        high_iter_rows, cutoff_14d,
                    )
                except Exception:
                    logger.exception("[AGENT] Failed to compute top_first_review_issues (non-critical)")

                # Direction-scoped recurring issues: per-task presence within the current direction.
                # Requires at least 3 direction-scoped tasks with review history to avoid noisy signals.
                try:
                    if current_direction_id:
                        direction_top_first_review_issues = compute_recurring_first_review_descriptions(
                            high_iter_rows, cutoff_14d,
                            task_filter=lambda ht: (ht.context or {}).get("direction_id") == current_direction_id,
                            min_tasks=3,
                            count_unique_tasks=True,
                        )
                except Exception:
                    logger.exception("[AGENT] Failed to compute direction_top_first_review_issues (non-critical)")

                try:
                    if current_task_scope:
                        scope_specific_review_issues = compute_recurring_first_review_descriptions(
                            high_iter_rows, cutoff_14d,
                            task_filter=lambda ht: (ht.context or {}).get("scope") == current_task_scope,
                            min_tasks=3,
                            extra_fields={"scope": current_task_scope},
                        )
                except Exception:
                    logger.exception("[AGENT] Failed to compute scope_specific_review_issues (non-critical)")
            except Exception:
                logger.exception("[AGENT] Failed to compute review_issue_distribution (non-critical)")

        # Compute review effectiveness trends (stores in project.design_doc as side effect)
        review_effectiveness = None
        if task_type in ("create", "fix", "refactor"):
            try:
                from app.playbook_learning import compute_review_effectiveness
                review_effectiveness = await compute_review_effectiveness(
                    project_id=task.project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception("[AGENT] Failed to compute review_effectiveness (non-critical)")

        response["project_context"] = {
            "active_tasks": active_tasks,
            "queued_tasks": queued_tasks,
            "recently_completed_tasks": recently_completed_tasks,
            "direction_recent_tasks": direction_recent_tasks if current_direction_id else [],
            "open_prs": list(open_prs_rows),
            "sibling_tasks": sibling_tasks,
            "lessons": lessons,
            "pipeline_health": pipeline_health,
            "review_issue_distribution": review_issue_distribution,
            "top_first_review_issues": top_first_review_issues,
            "scope_specific_review_issues": scope_specific_review_issues,
            "review_effectiveness": review_effectiveness,
        }

        # Record review measurement attribution in task.context for outcome tracking
        try:
            _had_measurements = bool(review_issue_distribution)
            _ctx = dict(task.context or {})
            _ctx["_had_review_measurements"] = _had_measurements
            if _had_measurements:
                _ctx["_top_review_issue_categories"] = list(review_issue_distribution.keys())
            task.context = _ctx
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
        except Exception:
            logger.exception("[AGENT] Failed to record review measurement attribution (non-critical)")
    else:
        # Lightweight pipeline health for all other task types
        try:
            from app.routers.admin_digest import compute_pipeline_health_summary
            pipeline_health = await compute_pipeline_health_summary(session, task.project_id)
            if pipeline_health:
                response["project_context"] = {"pipeline_health": pipeline_health}
        except Exception:
            logger.exception("[AGENT] Failed to compute lightweight pipeline health (non-critical)")

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
        _playbook_candidates = []
        for k, v in (project_for_playbook.design_doc or {}).items():
            if k.startswith("playbook/") and isinstance(v, dict):
                applicable_types = v.get("applicable_types")
                if applicable_types is not None:
                    if task_type not in applicable_types:
                        continue
                elif task_type not in _PLAYBOOK_DEFAULT_TYPES:
                    continue
                short_key = k[len("playbook/"):]
                # Skip auto-archived (stale) entries
                if v.get("stale"):
                    continue
                confirmed = v.get("confirmed_count", 0)
                referenced = v.get("reference_count", 0)
                # Demote entries with high reference count but zero confirmations
                ineffective = 1 if (referenced >= 10 and confirmed == 0) else 0
                if ineffective:
                    logger.info(
                        "Playbook entry '%s' demoted (reference_count=%d, confirmed_count=0)",
                        short_key, referenced,
                    )
                # Age penalty: deprioritise entries older than 30 days with < 3 confirmations
                age_penalty = 0
                updated_at_str = v.get("updated_at", "")
                if updated_at_str and confirmed < 3:
                    try:
                        updated_dt = datetime.fromisoformat(
                            updated_at_str.replace("Z", "+00:00")
                        )
                        # _utcnow() returns naive UTC; strip tzinfo so comparison is always naive
                        if updated_dt.tzinfo is not None:
                            updated_dt = updated_dt.replace(tzinfo=None)
                        if (_utcnow() - updated_dt).days > 30:
                            age_penalty = 1
                    except (ValueError, TypeError):
                        pass
                # Stale file-reference penalty
                stale_penalty = 1 if v.get("stale_file_refs") else 0
                if age_penalty or stale_penalty:
                    logger.debug(
                        "Playbook entry '%s' penalty: age=%d stale=%d",
                        short_key, age_penalty, stale_penalty,
                    )
                # Effectiveness score: confirmed / referenced ratio (higher = better)
                effectiveness_score = confirmed / max(referenced, 1)
                # Age in days for metadata (0 if updated_at unavailable)
                age_days = 0
                if updated_at_str:
                    try:
                        updated_dt2 = datetime.fromisoformat(
                            updated_at_str.replace("Z", "+00:00")
                        )
                        if updated_dt2.tzinfo is not None:
                            updated_dt2 = updated_dt2.replace(tzinfo=None)
                        age_days = (_utcnow() - updated_dt2).days
                    except (ValueError, TypeError):
                        pass
                _playbook_candidates.append((
                    ineffective,
                    stale_penalty,
                    age_penalty,
                    -confirmed,
                    -effectiveness_score,
                    short_key,
                    v.get("content", ""),
                    effectiveness_score,
                    confirmed,
                    age_days,
                    bool(age_penalty or stale_penalty),
                ))
        _playbook_candidates.sort()
        for (_, _, _, _, _, short_key, content,
             eff_score, conf_count, age_days_val, is_stale) in _playbook_candidates:
            playbook_entries[short_key] = {
                "content": content,
                "effectiveness_score": round(eff_score, 3),
                "confirmed_count": conf_count,
                "age_days": age_days_val,
                "stale": is_stale,
            }
        total_playbook_count = sum(
            1 for k in (project_for_playbook.design_doc or {}) if k.startswith("playbook/")
        )
        response["first_task_on_repo"] = total_playbook_count == 0
        if playbook_entries:
            response["playbook"] = playbook_entries
            # Track reference counts + stale-ref check — fire-and-forget, must not fail claim response
            try:
                now_iso = _utcnow().isoformat()
                doc = dict(project_for_playbook.design_doc)
                for short_key in playbook_entries:
                    full_key = f"playbook/{short_key}"
                    if full_key in doc and isinstance(doc[full_key], dict):
                        entry = dict(doc[full_key])
                        entry["reference_count"] = entry.get("reference_count", 0) + 1
                        entry["last_referenced_at"] = now_iso
                        outcomes = list(entry.get("recent_outcomes", []))
                        outcomes.append(False)
                        outcomes = outcomes[-10:]
                        entry["recent_outcomes"] = outcomes
                        # Auto-archive: if 10 entries and all False, mark stale
                        if len(outcomes) >= 10 and not any(outcomes):
                            entry["stale"] = True
                            entry["effectiveness_score"] = 0.0
                        doc[full_key] = entry
                # Check file-path references against the repo root (config-driven; skipped when unset)
                try:
                    from app.playbook_learning import check_and_flag_stale_refs
                    repo_root: str = _shared_state.runtime_config.get("playbook_repo_root", "") or ""
                    if repo_root:
                        doc = check_and_flag_stale_refs(doc, repo_root)
                except Exception:  # noqa: BLE001 — stale-ref check is best-effort, never block claim
                    logger.debug("[AGENT] Stale-ref check skipped (non-critical)")
                project_for_playbook.design_doc = doc
                flag_modified(project_for_playbook, "design_doc")
                session.add(project_for_playbook)
                # Store shown playbook keys in task context for outcome tracking at completion
                task_ctx = dict(task.context or {})
                task_ctx["_shown_playbook_keys"] = list(playbook_entries.keys())
                task.context = task_ctx
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
            except Exception:
                logger.exception("[AGENT] Failed to update playbook reference counts (non-critical)")

    # Inject direction quality signal when total PRs >= 3 (reuse project already loaded above)
    direction_id = (task.context or {}).get("direction_id")
    if direction_id and project_for_playbook:
        direction = next(
            (d for d in (project_for_playbook.directions or []) if d.get("id") == direction_id),
            None,
        )
        if direction:
            history = direction.get("research_history", [])
            agg = direction.get("history_aggregate", {})
            total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
            total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (agg.get("total_prs_rejected") or 0)
            total_prs = total_merged + total_rejected
            if total_prs >= 3:
                acceptance_rate = total_merged / total_prs
                merge_time_values = [
                    h["avg_time_to_merge_hours"]
                    for h in history
                    if h.get("avg_time_to_merge_hours") is not None
                ]
                avg_ttm = round(sum(merge_time_values) / len(merge_time_values), 2) if merge_time_values else None
                # Fetch last 3 tasks rejected by operator for this direction
                rejected_stmt = (
                    select(Task)
                    .where(
                        Task.project_id == task.project_id,
                        Task.context["direction_id"].as_string() == str(direction_id),
                        Task.context["pr_rejected"].as_string() == "true",
                    )
                    .order_by(Task.created_at.desc())
                    .limit(3)
                )
                rejected_rows = (await session.execute(rejected_stmt)).scalars().all()
                rejection_context = [
                    {
                        "title": rt.title,
                        "reason": (
                            (rt.context or {}).get("pr_rejection_note")
                            or rt.latest_status_message
                            or ""
                        )[:200],
                    }
                    for rt in rejected_rows
                ]
                direction_quality = {
                    "direction_id": direction_id,
                    "acceptance_rate": round(acceptance_rate, 3),
                    "prs_merged": total_merged,
                    "prs_rejected": total_rejected,
                    "avg_time_to_merge_hours": avg_ttm,
                    "rejection_context": rejection_context,
                }
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["direction_quality"] = direction_quality

            # Inject direction context (title, goals, failed approaches) for implementing agents
            if task_type in ("create", "fix", "refactor"):
                failed_approaches: list = []
                rc_text = direction.get("research_context", "") or ""
                in_failed_section = False
                for rc_line in rc_text.splitlines():
                    rc_stripped = rc_line.strip()
                    if rc_stripped == "## Failed Approaches":
                        in_failed_section = True
                    elif rc_stripped.startswith("## "):
                        in_failed_section = False
                    elif in_failed_section and rc_stripped.startswith("- "):
                        failed_approaches.append(rc_stripped[2:])
                structured_failed_approaches = [
                    _parse_failed_approach(a) for a in failed_approaches[:5]
                ]
                recent_outcomes = [
                    {"tasks_created": h.get("tasks_created", 0), "prs_merged": h.get("prs_merged", 0)}
                    for h in history[-2:]
                    if h.get("tasks_created", 0) > 0
                ]
                failure_stage_pattern = _compute_failure_stage_pattern(history)

                # Aggregate scope_breakdown from the last 3 productive research cycles
                scope_success_rates: dict[str, dict[str, int]] = {}
                productive_cycles = [
                    h for h in history if h.get("tasks_created", 0) > 0
                ][-3:]
                for cycle in productive_cycles:
                    sb = cycle.get("scope_breakdown") or {}
                    if not isinstance(sb, dict):
                        continue
                    for scope_name, counts in sb.items():
                        if not isinstance(counts, dict):
                            continue
                        agg_entry = scope_success_rates.setdefault(
                            scope_name, {"created": 0, "merged": 0, "failed": 0, "merged_cross_module": 0}
                        )
                        agg_entry["created"] += int(counts.get("created", 0) or 0)
                        agg_entry["merged"] += int(counts.get("merged", 0) or 0)
                        agg_entry["failed"] += int(counts.get("failed", 0) or 0)
                        agg_entry["merged_cross_module"] += int(counts.get("merged_cross_module", 0) or 0)
                scope_success_rates = {
                    k: v for k, v in scope_success_rates.items() if v["created"] >= 1
                }

                # Pull recent failed/cancelled sibling tasks with analysis artifacts
                prior_analysis_cutoff = _utcnow() - timedelta(days=14)
                prior_stmt = (
                    select(Task)
                    .where(
                        Task.project_id == task.project_id,
                        Task.id != task.id,
                        Task.context["direction_id"].as_string() == str(direction_id),
                        Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
                        Task.created_at >= prior_analysis_cutoff,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(5)
                )
                prior_rows = (await session.execute(prior_stmt)).scalars().all()
                direction_prior_analysis: list = []
                total_chars = 0
                for pt in prior_rows:
                    pctx = pt.context or {}
                    analysis_plan = pctx.get("_analysis_plan")
                    test_failure_summary = pctx.get("_test_failure_summary")
                    status_message = (pt.latest_status_message or "").strip()
                    self_review_outcome = pctx.get("_self_review_outcome")
                    if not analysis_plan and not test_failure_summary and not status_message:
                        continue
                    entry = {
                        "title": pt.title,
                        "failure_category": classify_failure(pt),
                    }
                    scope = pctx.get("scope")
                    if isinstance(scope, str) and scope:
                        entry["scope"] = scope
                    if analysis_plan:
                        entry["analysis_plan"] = str(analysis_plan)[:500]
                    if test_failure_summary:
                        entry["test_failure_summary"] = str(test_failure_summary)[:300]
                    if status_message:
                        entry["latest_status_message"] = status_message[:300]
                    if isinstance(self_review_outcome, dict) and self_review_outcome:
                        sro_compact: dict = {}
                        verdict = self_review_outcome.get("verdict")
                        if verdict:
                            sro_compact["verdict"] = str(verdict)[:50]
                        notes = self_review_outcome.get("notes")
                        if notes:
                            sro_compact["notes"] = str(notes)[:200]
                        check_results = self_review_outcome.get("check_results")
                        if isinstance(check_results, dict):
                            failed_checks = [
                                str(name)
                                for name, result in check_results.items()
                                if isinstance(result, dict) and result.get("passed") is False
                            ]
                            if failed_checks:
                                sro_compact["failed_checks"] = failed_checks[:10]
                        if sro_compact:
                            entry["self_review_outcome"] = sro_compact
                    changed_files = pctx.get("_pr_changed_files")
                    if isinstance(changed_files, list) and changed_files:
                        entry["pr_changed_files"] = [str(f) for f in changed_files[:10]]
                    diff_summary = pctx.get("_pr_diff_summary")
                    if isinstance(diff_summary, list) and diff_summary:
                        compact_diff = []
                        for d in diff_summary[:10]:
                            if not isinstance(d, dict):
                                continue
                            item = {"file": str(d.get("file", ""))}
                            added = d.get("additions", d.get("added"))
                            removed = d.get("deletions", d.get("removed"))
                            if isinstance(added, int):
                                item["added"] = added
                            if isinstance(removed, int):
                                item["removed"] = removed
                            change_type = d.get("change_type")
                            if change_type:
                                item["change_type"] = str(change_type)
                            if item["file"]:
                                compact_diff.append(item)
                        if compact_diff:
                            entry["pr_diff_summary"] = compact_diff
                    fingerprint = pctx.get("_previous_error_fingerprint")
                    if fingerprint:
                        entry["previous_error_fingerprint"] = str(fingerprint)
                    review_loop = pctx.get("review_loop")
                    if isinstance(review_loop, dict):
                        iteration = review_loop.get("iteration", 0)
                        if isinstance(iteration, int):
                            entry["review_iterations"] = iteration
                        review_history = review_loop.get("review_history")
                        if isinstance(review_history, list) and review_history:
                            last_entry = review_history[-1]
                            if isinstance(last_entry, dict):
                                issues = last_entry.get("issues")
                                compact_issues: list = []
                                if isinstance(issues, list):
                                    for sev_filter in ("critical", "major"):
                                        for issue in issues:
                                            if not isinstance(issue, dict):
                                                continue
                                            sev = str(issue.get("severity", "")).lower()
                                            if sev != sev_filter:
                                                continue
                                            desc = str(issue.get("description", "")).strip()
                                            if not desc:
                                                continue
                                            item = {"severity": sev.upper(), "description": desc[:150]}
                                            file_path = issue.get("file")
                                            if file_path:
                                                item["file"] = str(file_path)
                                                line = issue.get("line")
                                                if isinstance(line, int):
                                                    item["line"] = line
                                            compact_issues.append(item)
                                            if len(compact_issues) >= 3:
                                                break
                                        if len(compact_issues) >= 3:
                                            break
                                if compact_issues:
                                    entry["review_feedback"] = compact_issues
                                else:
                                    feedback_text = str(last_entry.get("feedback") or "").strip()
                                    if feedback_text:
                                        entry["review_feedback"] = feedback_text[:200]
                    entry_len = len(json.dumps(entry))
                    if total_chars + entry_len > 2000:
                        break
                    total_chars += entry_len
                    direction_prior_analysis.append(entry)

                # Pull recent successful sibling tasks with analysis artifacts
                success_stmt = (
                    select(Task)
                    .where(
                        Task.project_id == task.project_id,
                        Task.id != task.id,
                        Task.context["direction_id"].as_string() == str(direction_id),
                        Task.status == TaskStatus.COMPLETED,
                        Task.created_at >= prior_analysis_cutoff,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(5)
                )
                success_rows = (await session.execute(success_stmt)).scalars().all()
                direction_successful_analysis: list = []
                success_chars = 0
                for st in success_rows:
                    sctx = st.context or {}
                    analysis_plan = sctx.get("_analysis_plan")
                    changed_files = sctx.get("_pr_changed_files")
                    has_changed_files = isinstance(changed_files, list) and changed_files
                    if not analysis_plan and not has_changed_files:
                        continue
                    s_entry = {
                        "title": st.title,
                    }
                    if analysis_plan:
                        s_entry["analysis_plan"] = str(analysis_plan)[:500]
                    scope = sctx.get("scope")
                    if scope:
                        s_entry["scope"] = str(scope)
                    if has_changed_files:
                        s_entry["pr_changed_files"] = [str(f) for f in changed_files[:10]]
                    review_loop = sctx.get("review_loop")
                    if isinstance(review_loop, dict):
                        iteration = review_loop.get("iteration", 0)
                        if isinstance(iteration, int):
                            s_entry["review_iterations"] = iteration
                    s_entry_len = len(json.dumps(s_entry))
                    if success_chars + s_entry_len > 1500:
                        break
                    success_chars += s_entry_len
                    direction_successful_analysis.append(s_entry)

                direction_first_attempt_feedback: Dict[str, int] | None = None
                try:
                    dfa_all = (project_for_playbook.design_doc or {}).get(
                        "direction_first_attempt_feedback"
                    ) or {}
                    dfa_entry = dfa_all.get(str(direction_id)) or {}
                    dfa_cats = {
                        k: v for k, v in dfa_entry.items()
                        if k not in ("total", "last_updated") and isinstance(v, int) and v > 0
                    }
                    if dfa_cats:
                        direction_first_attempt_feedback = dict(
                            sorted(dfa_cats.items(), key=lambda kv: -kv[1])
                        )
                except Exception:
                    logger.exception("[AGENT] Failed to read direction_first_attempt_feedback (non-critical)")

                response["direction_context"] = {
                    "title": direction.get("title", ""),
                    "priority": direction.get("priority", ""),
                    "failed_approaches": structured_failed_approaches,
                    "recent_outcomes": recent_outcomes,
                    "failure_stage_pattern": failure_stage_pattern,
                    "direction_prior_analysis": direction_prior_analysis,
                    "direction_successful_analysis": direction_successful_analysis,
                    "scope_success_rates": scope_success_rates,
                    "top_first_review_issues": direction_top_first_review_issues,
                    "first_attempt_feedback": direction_first_attempt_feedback,
                }

            # Surface saturation / deadlock signals to research agents so they can
            # pivot to direction_update/direction_note findings on stuck directions.
            if task_type == "research":
                from app.research_completion import _detect_dedup_deadlock
                deadlocked, _ = _detect_dedup_deadlock(history)
                research_recent_outcomes = [
                    {"tasks_created": h.get("tasks_created", 0), "prs_merged": h.get("prs_merged", 0)}
                    for h in history[-2:]
                    if h.get("tasks_created", 0) > 0
                ]
                research_failure_stage_pattern = _compute_failure_stage_pattern(history)
                research_scope_rates: dict[str, dict[str, int]] = {}
                for cycle in [h for h in history if h.get("tasks_created", 0) > 0][-3:]:
                    sb = cycle.get("scope_breakdown") or {}
                    if not isinstance(sb, dict):
                        continue
                    for scope_name, counts in sb.items():
                        if not isinstance(counts, dict):
                            continue
                        agg = research_scope_rates.setdefault(
                            scope_name, {"created": 0, "merged": 0, "failed": 0, "merged_cross_module": 0}
                        )
                        agg["created"] += int(counts.get("created", 0) or 0)
                        agg["merged"] += int(counts.get("merged", 0) or 0)
                        agg["failed"] += int(counts.get("failed", 0) or 0)
                        agg["merged_cross_module"] += int(counts.get("merged_cross_module", 0) or 0)
                research_scope_rates = {k: v for k, v in research_scope_rates.items() if v["created"] >= 1}
                research_top_review_issues = None
                try:
                    from app.playbook_learning import compute_recurring_first_review_descriptions
                    if current_direction_id:
                        research_top_review_issues = compute_recurring_first_review_descriptions(
                            high_iter_rows, _utcnow() - timedelta(days=14),
                            task_filter=lambda ht: (ht.context or {}).get("direction_id") == current_direction_id,
                            min_tasks=3,
                            count_unique_tasks=True,
                        )
                except Exception:
                    logger.exception("[AGENT] Failed to compute research top_first_review_issues (non-critical)")
                research_review_effectiveness = None
                try:
                    from app.playbook_learning import compute_review_effectiveness
                    research_review_effectiveness = await compute_review_effectiveness(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception("[AGENT] Failed to compute research review_effectiveness (non-critical)")
                success_summary: list[str] = []
                for scope_name, counts in research_scope_rates.items():
                    created = counts["created"]
                    merged = counts["merged"]
                    if created >= 2:
                        success_summary.append(
                            f"{scope_name}: {merged}/{created} merged ({round(merged / created * 100)}%)"
                        )
                # Aggregate prompt_telemetry from last 3 productive cycles so research
                # agents can calibrate finding-prompt construction against empirical
                # merged-vs-failed patterns (test mentions, symbol mentions, length).
                prompt_telemetry_patterns: dict | None = None
                pt_agg: dict[str, dict[str, float]] = {
                    "merged": {"count": 0, "char_count": 0.0, "related_file_count": 0.0,
                               "files_to_budget_ratio": 0.0, "mentions_tests": 0.0,
                               "mentions_symbols": 0.0, "_n_avg": 0},
                    "failed": {"count": 0, "char_count": 0.0, "related_file_count": 0.0,
                               "files_to_budget_ratio": 0.0, "mentions_tests": 0.0,
                               "mentions_symbols": 0.0, "_n_avg": 0},
                }
                for cycle in [h for h in history if h.get("tasks_created", 0) > 0][-3:]:
                    pt = cycle.get("prompt_telemetry")
                    if not isinstance(pt, dict):
                        continue
                    for bucket in ("merged", "failed"):
                        b = pt.get(bucket)
                        if not isinstance(b, dict):
                            continue
                        n = int(b.get("count") or 0)
                        if n <= 0:
                            continue
                        agg = pt_agg[bucket]
                        agg["count"] += n
                        for k_avg in ("char_count", "related_file_count", "files_to_budget_ratio"):
                            v = b.get(f"avg_{k_avg}")
                            if isinstance(v, (int, float)):
                                agg[k_avg] += float(v) * n
                        for k_frac in ("mentions_tests", "mentions_symbols"):
                            v = b.get(f"frac_{k_frac}")
                            if isinstance(v, (int, float)):
                                agg[k_frac] += float(v) * n
                        agg["_n_avg"] += n
                pt_summary: dict[str, dict] = {}
                for bucket, agg in pt_agg.items():
                    n = int(agg["_n_avg"])
                    if n <= 0:
                        continue
                    pt_summary[bucket] = {
                        "count": int(agg["count"]),
                        "avg_char_count": round(agg["char_count"] / n, 1),
                        "avg_related_file_count": round(agg["related_file_count"] / n, 2),
                        "avg_files_to_budget_ratio": round(agg["files_to_budget_ratio"] / n, 3),
                        "frac_mentions_tests": round(agg["mentions_tests"] / n, 3),
                        "frac_mentions_symbols": round(agg["mentions_symbols"] / n, 3),
                    }
                if pt_summary.get("merged") or pt_summary.get("failed"):
                    prompt_telemetry_patterns = pt_summary
                response["direction_context"] = {
                    "possibly_saturated": bool(direction.get("possibly_saturated", False)),
                    "deadlock_detected": bool(deadlocked),
                    "recent_outcomes": research_recent_outcomes,
                    "failure_stage_pattern": research_failure_stage_pattern,
                    "scope_success_rates": research_scope_rates,
                    "top_first_review_issues": research_top_review_issues,
                    "success_patterns": success_summary,
                    "review_effectiveness": research_review_effectiveness,
                    "prompt_telemetry_patterns": prompt_telemetry_patterns,
                }

    # Inject co-change affinity context for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        related_files = (task.context or {}).get("related_files", [])
        if related_files:
            cochange_index = (project_for_playbook.design_doc or {}).get("cochange_index")
            if cochange_index:
                cochange_scores: dict[str, float] = {}
                related_set = set(related_files)
                for rf in related_files:
                    for co_file, score in (cochange_index.get(rf) or {}).items():
                        if co_file not in related_set:
                            cochange_scores[co_file] = cochange_scores.get(co_file, 0.0) + score
                if cochange_scores:
                    top_cochange = sorted(cochange_scores.items(), key=lambda x: x[1], reverse=True)[:10]
                    if "project_context" not in response:
                        response["project_context"] = {}
                    response["project_context"]["cochange_files"] = [f for f, _ in top_cochange]

    # Inject within-set co-change affinity hints for plan tasks so the planner
    # can group high-affinity files into the same subtask and split low-affinity
    # files into independent ones. Only pairs where both files are in the
    # finding's related_files and score >= 1.0 are surfaced.
    if task_type == "plan" and project_for_playbook:
        plan_related_files = (task.context or {}).get("related_files", [])
        if plan_related_files:
            cochange_index = (project_for_playbook.design_doc or {}).get("cochange_index") or {}
            cochange_hints: dict[str, dict[str, float]] = {}
            related_set = set(plan_related_files)
            for rf in plan_related_files:
                for co_file, score in (cochange_index.get(rf) or {}).items():
                    if co_file in related_set and isinstance(score, (int, float)) and score >= 1.0:
                        cochange_hints.setdefault(rf, {})[co_file] = round(float(score), 3)
            if cochange_hints:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["cochange_hints"] = cochange_hints

    if task_type == "plan" and direction_id and project_for_playbook:
        direction_for_decomp = next(
            (d for d in (project_for_playbook.directions or []) if d.get("id") == direction_id),
            None,
        )
        if direction_for_decomp:
            from app.playbook_learning import summarize_decomposition_outcomes
            decomposition_outcomes = summarize_decomposition_outcomes(direction_for_decomp)
            if decomposition_outcomes:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["decomposition_outcomes"] = decomposition_outcomes

    # Inject per-module review feedback warnings for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        module_feedback = (project_for_playbook.design_doc or {}).get("module_feedback")
        if module_feedback:
            task_related_files = (task.context or {}).get("related_files", [])
            module_warnings = []
            for file_path in task_related_files:
                profile = module_feedback.get(file_path)
                if not profile or not isinstance(profile, dict):
                    continue
                total = profile.get("total", 0)
                if total < 3:
                    continue
                cat_counts = {k: v for k, v in profile.items() if k not in ("total", "last_updated") and isinstance(v, int)}
                sorted_cats = sorted(cat_counts.items(), key=lambda x: -x[1])
                top_cats = {
                    cat: round(count / total, 2)
                    for cat, count in sorted_cats[:3]
                    if count / total >= 0.15
                }
                if top_cats:
                    module_warnings.append({"file": file_path, "top_categories": top_cats, "sample_size": total})
            if module_warnings:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["module_feedback_warnings"] = module_warnings

    # Inject scoped architecture context for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        design_doc = project_for_playbook.design_doc or {}
        file_to_system = design_doc.get("_file_to_system", {})
        if file_to_system:
            arch_related_files = (task.context or {}).get("related_files", [])
            seen_systems: dict[str, dict] = {}
            for rf in arch_related_files:
                sys_name = file_to_system.get(rf)
                if sys_name and sys_name not in seen_systems:
                    sys_entry = design_doc.get(f"systems/{sys_name}")
                    if isinstance(sys_entry, dict):
                        seen_systems[sys_name] = sys_entry
                        if len(seen_systems) >= 5:
                            break
            if seen_systems:
                arch_ctx = []
                for sys_name, sys_data in seen_systems.items():
                    desc = sys_data.get("description", "")
                    arch_ctx.append({
                        "name": sys_data.get("name", sys_name),
                        "status": str(sys_data.get("status", "")),
                        "ready_for_features": bool(sys_data.get("ready_for_features", False)),
                        "description": desc[:200] if isinstance(desc, str) else "",
                    })
                # Safety: drop trailing entries if total exceeds 1500 chars
                while arch_ctx and len(json.dumps(arch_ctx)) > 1500:
                    arch_ctx.pop()
                if arch_ctx:
                    if "project_context" not in response:
                        response["project_context"] = {}
                    response["project_context"]["architecture_context"] = arch_ctx
                    # Track arch context injection in task.context for effectiveness measurement
                    try:
                        _arch_task_ctx = dict(task.context or {})
                        _arch_task_ctx["_had_arch_context"] = True
                        _arch_task_ctx["_shown_arch_system_names"] = list(seen_systems.keys())
                        task.context = _arch_task_ctx
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                    except Exception:
                        logger.exception("[AGENT] Failed to track arch context injection (non-critical)")

    # Inject build_config so the agent knows how to lint/test/build
    if project_for_playbook and project_for_playbook.build_config:
        response["build_config"] = project_for_playbook.build_config
    else:
        response["build_config"] = {}

    # Fire-and-forget: create an in_progress Check Run for review tasks so the
    # Checks tab shows the review is underway.  Must not block the claim response.
    if task_type == "review":
        _pr_number = task.first_payload.get("pr_number")
        if _pr_number and project_for_playbook:
            _check_run_name = (project_for_playbook.build_config or {}).get(
                "check_run_name", "Blueprint Code Review"
            )
            _check_run_fut = asyncio.ensure_future(
                _create_review_check_run_bg(task.id, _pr_number, _check_run_name)
            )
            _bg_tasks.add(_check_run_fut)
            _check_run_fut.add_done_callback(_bg_tasks.discard)

    return response

@router.post("/artifacts/upload-url")
@limiter.limit("300/minute")
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
@limiter.limit("600/minute")
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
    if req.metrics or req.tool_trace is not None:
        ctx = dict(task.context or {})
        if req.metrics:
            metrics_dump = req.metrics.model_dump(exclude_none=True)
            ctx['last_metrics'] = metrics_dump
            history = list(ctx.get('metrics_history', []))
            entry: dict = {'t': _utcnow().isoformat()}
            for k in ('rss_mb', 'heap_used_mb', 'disk_free_mb', 'disk_total_mb', 'cgroup_mb', 'cgroup_limit_mb'):
                if metrics_dump.get(k) is not None:
                    entry[k] = metrics_dump[k]
            history.append(entry)
            if len(history) > 30:
                history = history[-30:]
            ctx['metrics_history'] = history
        if req.tool_trace is not None:
            # Overwrite rather than append — the agent already maintains a
            # ring buffer of the latest entries, so each heartbeat carries the
            # authoritative recent-tool-calls window. Last-successful
            # heartbeat's value is the death trace after reap.
            ctx['_tool_trace'] = [t.model_dump() for t in req.tool_trace]
        task.context = ctx

    session.add(task)
    await session.commit()

    return {"status": "ok"}

@router.post("/spawn")
@limiter.limit("150/minute")
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

    # Plan-spawned children are created by _handle_plan_completion AFTER branch creation,
    # so they get the correct base_branch. Don't pre-spawn them here.
    if (req.context or {}).get("agent_spawned"):
        return {"message": "Plan children deferred to post-branch-creation", "task_ids": [], "requires_approval": False}

    new_tasks = []
    for payload in req.payloads:
        try:
            payload = validate_task_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
            requires_approval=req.requires_approval,
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
@limiter.limit("200/minute")
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
@limiter.limit("500/minute")
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
@limiter.limit("200/minute")
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
@limiter.limit("200/minute")
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

@router.post("/github/pr-review")
@limiter.limit("150/minute")
async def agent_github_submit_pr_review(
    request: Request,
    req: GitHubSubmitPRReviewRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Agent endpoint to submit a full PR review (APPROVE/REQUEST_CHANGES/COMMENT) via
    the GitHub Pull Request Reviews API.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await submit_pr_review(
        token=gh_token,
        owner=owner,
        repo=repo,
        pr_number=req.pr_number,
        event=req.event,
        body=req.body,
        comments=req.comments,
    )
    return {"status": "success", "data": result}

@router.post("/github/issues")
@limiter.limit("100/minute")
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
@limiter.limit("150/minute")
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
@limiter.limit("100/minute")
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
@limiter.limit("100/minute")
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

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)

    # Idempotency: if this task already has a PR artifact, verify the PR is still
    # open before returning it. If it was closed (e.g. branch deleted on requeue),
    # fall through and create a new PR — otherwise the agent reports COMPLETED on
    # a phantom PR and the rollup verifier later flips it to FAILED.
    existing_artifact = (await session.execute(
        select(Artifact).where(
            Artifact.task_id == req.task_id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST
        )
    )).scalars().first()
    if existing_artifact and existing_artifact.pr_url:
        try:
            pr_number = int(existing_artifact.pr_url.rstrip("/").split("/")[-1])
            pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            if pr_data.get("state") == "open":
                logger.info(f"[PR] Task {req.task_id} already has open PR artifact: {existing_artifact.pr_url}")
                return {"status": "success", "data": {"html_url": existing_artifact.pr_url}}
            logger.warning(
                f"[PR] Task {req.task_id} cached PR #{pr_number} is {pr_data.get('state')} "
                f"(merged={pr_data.get('merged')}) — recreating PR and replacing artifact"
            )
            await session.delete(existing_artifact)
            await session.flush()
        except HTTPException as exc:
            if exc.status_code in (404, 410):
                logger.warning(f"[PR] Task {req.task_id} cached PR no longer exists ({exc.status_code}) — recreating")
                await session.delete(existing_artifact)
                await session.flush()
            else:
                raise
        except (ValueError, IndexError):
            logger.warning(f"[PR] Could not parse PR number from cached URL {existing_artifact.pr_url} — recreating")
            await session.delete(existing_artifact)
            await session.flush()

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
