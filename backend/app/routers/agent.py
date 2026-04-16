"""Agent-facing API endpoints (lifecycle + GitHub proxy)."""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
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
    except Exception as e:
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
# Helper: failure stage pattern analysis
# ---------------------------------------------------------------------------

def _compute_failure_stage_pattern(history: list) -> "dict | None":
    """
    Analyze recent research_history stage_dropout data to identify dominant failure stages.

    Returns a pattern dict if 60%+ of non-infra failures cluster at one stage, else None.
    Only considers code_error and approval stages — agents can't address infra failures.
    Uses the last 3 research cycles for recency.
    """
    recent = history[-3:]
    code_error_total = 0
    approval_total = 0
    for entry in recent:
        sd = entry.get("stage_dropout") or {}
        code_error_total += sd.get("tasks_failed_code_error", 0)
        approval_total += sd.get("tasks_required_approval", 0)
    total_non_infra = code_error_total + approval_total
    if total_non_infra < 3:
        return None
    code_error_rate = code_error_total / total_non_infra
    approval_rate = approval_total / total_non_infra
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
    return None


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
            logger.exception("[AGENT] Failed to compute pipeline health summary (non-critical)")

        # Aggregate review issue distribution for create/fix/refactor tasks (last 14 days)
        review_issue_distribution = None
        top_first_review_issues = None
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

                # Extract top recurring specific issue descriptions from first reviews (iteration 0)
                try:
                    from collections import Counter
                    desc_counter: Counter = Counter()
                    desc_to_cat: dict = {}
                    for ht in high_iter_rows:
                        if ht.created_at and ht.created_at < cutoff_14d:
                            continue
                        review_loop = (ht.context or {}).get("review_loop", {})
                        rev_hist = review_loop.get("review_history", []) if review_loop else []
                        for entry in rev_hist:
                            if entry.get("iteration") != 0:
                                continue
                            for issue in entry.get("issues", []):
                                desc = issue.get("description") or ""
                                cat = issue.get("category", "other")
                                if not desc:
                                    continue
                                normalized = desc.lower().strip()[:120]
                                desc_counter[normalized] += 1
                                if normalized not in desc_to_cat:
                                    desc_to_cat[normalized] = cat
                    recurring = [(desc, cnt) for desc, cnt in desc_counter.items() if cnt >= 2]
                    if recurring:
                        recurring.sort(key=lambda x: -x[1])
                        top_first_review_issues = [
                            {"description": desc, "count": cnt, "category": desc_to_cat.get(desc, "other")}
                            for desc, cnt in recurring[:3]
                        ]
                except Exception:
                    logger.exception("[AGENT] Failed to compute top_first_review_issues (non-critical)")
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
            "open_prs": list(open_prs_rows),
            "sibling_tasks": sibling_tasks,
            "lessons": lessons,
            "pipeline_health": pipeline_health,
            "review_issue_distribution": review_issue_distribution,
            "top_first_review_issues": top_first_review_issues,
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
            from app.routers.admin_analytics import compute_pipeline_health_summary
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
                _playbook_candidates.append((
                    ineffective,
                    stale_penalty,
                    age_penalty,
                    -confirmed,
                    -referenced,
                    short_key,
                    v.get("content", ""),
                ))
        _playbook_candidates.sort()
        for _, _, _, _, _, short_key, content in _playbook_candidates:
            playbook_entries[short_key] = content
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
                        doc[full_key] = entry
                # Check file-path references against the repo root (config-driven; skipped when unset)
                try:
                    from app.playbook_learning import check_and_flag_stale_refs  # noqa: PLC0415
                    repo_root: str = _shared_state.runtime_config.get("playbook_repo_root", "") or ""
                    if repo_root:
                        doc = check_and_flag_stale_refs(doc, repo_root)
                except Exception:
                    logger.debug("[AGENT] Stale-ref check skipped (non-critical)")
                project_for_playbook.design_doc = doc
                flag_modified(project_for_playbook, "design_doc")
                session.add(project_for_playbook)
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
                recent_outcomes = [
                    {"tasks_created": h.get("tasks_created", 0), "prs_merged": h.get("prs_merged", 0)}
                    for h in history[-2:]
                    if h.get("tasks_created", 0) > 0
                ]
                failure_stage_pattern = _compute_failure_stage_pattern(history)
                response["direction_context"] = {
                    "title": direction.get("title", ""),
                    "priority": direction.get("priority", ""),
                    "failed_approaches": [a[:120] for a in failed_approaches[:5]],
                    "recent_outcomes": recent_outcomes,
                    "failure_stage_pattern": failure_stage_pattern,
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
            asyncio.ensure_future(
                _create_review_check_run_bg(task.id, _pr_number, _check_run_name)
            )

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

    # Plan-spawned children are created by _handle_plan_completion AFTER branch creation,
    # so they get the correct base_branch. Don't pre-spawn them here.
    if (req.context or {}).get("agent_spawned"):
        return {"message": "Plan children deferred to post-branch-creation", "task_ids": [], "requires_approval": False}

    new_tasks = []
    for payload in req.payloads:
        try:
            payload = validate_task_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
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

@router.post("/github/pr-review")
@limiter.limit("30/minute")
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
