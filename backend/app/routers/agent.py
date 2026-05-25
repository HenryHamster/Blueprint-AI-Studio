"""Agent-facing API endpoints (lifecycle + GitHub proxy)."""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.auth import verify_agent_jwt, create_agent_token
from app.database import get_session, AsyncSessionLocal
from app.models import (
    Project,
    Task,
    TaskStatus,
    TaskHistory,
    Artifact,
    ArtifactType,
    UserIntegration,
    _on_task_terminal_callbacks,
    _on_task_revive_callbacks,
    _utcnow,
)
from app.time_utils import _to_naive_utc
from app.schemas import (
    UploadUrlRequest,
    HeartbeatRequest,
    SpawnTaskRequest,
    GitHubCommitStatusRequest,
    GitHubPRCommentRequest,
    GitHubPRReviewCommentRequest,
    GitHubSubmitPRReviewRequest,
    GitHubCreateIssueRequest,
    GitHubGetIssuesRequest,
    GitHubCreateBranchRequest,
    GitHubCreatePRRequest,
)
from pydantic import BaseModel, ConfigDict, Field
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
    get_check_runs,
)
from app.broadcast import broadcast_to_project
from app.sr_blind_spots import (
    aggregate_sr_blind_spot_themes,
    aggregate_sr_co_failure_pairs,
)
from app.pipeline_health import compute_pipeline_health_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


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
                logger.warning(
                    f"[CHECK RUN] No head_sha for PR #{pr_number} task {task_id}"
                )
                return
            result = await create_check_run(
                gh_token, owner, repo, head_sha, name=check_run_name
            )
            if result and result.get("id"):
                task = await session.get(Task, task_id)
                if task:
                    task.context = {
                        **(task.context or {}),
                        "check_run_id": result["id"],
                    }
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[CHECK RUN] Created check run {result['id']} for review task {task_id} PR #{pr_number}"
                    )
    except Exception as e:  # noqa: BLE001 — best-effort background task, never block claim response
        logger.warning(
            f"[CHECK RUN] Failed to create check run for task {task_id}: {e}"
        )


# ---------------------------------------------------------------------------
# JWT revocation state (moved from main.py)
# ---------------------------------------------------------------------------

# Maps task_id -> timestamp of termination. Entries older than _TERMINATED_TTL
# are evicted to prevent unbounded growth (JWTs expire after 8h).
_terminated_task_ids: dict[str, float] = {}
_TERMINATED_TTL = 9 * 3600  # 9h — slightly longer than 8h JWT max lifetime
_on_task_terminal_callbacks.append(
    lambda tid: _terminated_task_ids.__setitem__(tid, time.time())
)
# Production 2026-05-05: when a task is requeued (terminal → QUEUED, or any
# in-flight set to a non-terminal status), drop the deny-list entry so the
# next ``/api/agent/claim`` doesn't 401 with "Task is no longer active". The
# cache could otherwise persist across the lifecycle of the orchestrator
# revision, causing claim 401 loops. See models.py:_on_task_revive_callbacks
# for the full rationale.
_on_task_revive_callbacks.append(lambda tid: _terminated_task_ids.pop(tid, None))

# Issue M3 (warm-VM continuity audit, deferred): JWTs for terminated tasks
# remain valid for the JWT's full TTL (8h). The is_idempotent_path branch
# accepts these terminal-task tokens on /next-role and /vm-heartbeat so a
# legitimate warm chain can poll for the next role. A leaked terminal-task
# JWT can therefore spam-keepalive a phantom VM identity (blocking
# warm-pin clearout for 240s per pinned task) and / or claim any future
# task pinned to that VM. A normal warm chain transitions roles in
# minutes; a replay 30+ minutes after the JWT's iat is suspicious. Reject
# terminal-task tokens that are too old to plausibly be the same agent
# that was just running the prior role.
_TERMINAL_IDEMPOTENT_FRESHNESS_SECS = int(
    os.getenv("AGENT_TERMINAL_IDEMPOTENT_FRESHNESS_SECS", "1800")
)

# TTL cache for "known active" tasks — avoids DB query per request.
_agent_active_cache: dict[str, float] = {}
_AGENT_CACHE_TTL = 60.0  # seconds
_AGENT_CACHE_MAX = 5000  # evict expired entries when cache exceeds this size

# Retain strong references to fire-and-forget background tasks so the event loop
# doesn't GC them before completion (asyncio only holds a weak reference).
_bg_tasks: set[asyncio.Task] = set()

# Keys an agent JWT-holder MUST NOT be able to inject into a spawned child
# task's context. Without this filter, a compromised or malicious agent could
# call /api/agent/spawn with `req.context = {"warm_target_vm_id": "<victim>"}`
# and the orchestrator's skip-provision branch would honor the pin, hijacking
# another VM's warm chain. Phase 2c (warm-VM continuity) issue #3.
_AGENT_SPAWN_FORBIDDEN_CONTEXT_KEYS = frozenset(
    {
        "warm_target_vm_id",
        "warm_target_pr",
        "warm_handoff_deadline",
        # Defense-in-depth — these are also orchestrator-internal:
        "_timing",
    }
)


async def verify_agent_token(
    request: Request,
    x_agent_token: str = Header(
        ..., description="Secret token for agent authentication"
    ),
):
    """
    Validates the short-lived JWT passed by the agent VM.

    Audit 2026-04-25 (W3/E2): the path-based exemption for /status and
    /credentials/rotate has been replaced by a stricter VM-instance check.
    Same-VM idempotent retries on terminal tasks are still allowed (so a
    legitimate post-commit retry from the original agent succeeds), but a
    DIFFERENT VM trying to override the status of a requeued task is now
    rejected with 403 vm_instance_mismatch — failing fast on a real
    failure mode, not on legitimate work.

    Rejects tokens for tasks that are no longer active (completed/failed/cancelled),
    EXCEPT when the same VM that the task is currently assigned to is retrying
    /status or /credentials/rotate (idempotency).
    """
    payload = verify_agent_jwt(x_agent_token)
    task_id = payload.get("sub")
    if not task_id:
        return payload
    token_vm = payload.get("vm_instance_id")
    # /next-role and /vm-heartbeat are reached AFTER a task has terminated
    # (the agent polls for the next role using the prior task's JWT). Treat
    # them as idempotent paths so the verify-agent-token guard accepts the
    # terminal-task JWT, but still rejects a different VM trying to use it.
    is_idempotent_path = request.url.path in (
        "/api/agent/status",
        "/api/agent/credentials/rotate",
        "/api/agent/next-role",
        "/api/agent/vm-heartbeat",
    )
    # Issue M3 freshness check (warm-VM rev): only the warm-VM idempotent
    # paths /next-role and /vm-heartbeat carry the replay risk for terminal
    # tasks (the in-progress paths /status and /credentials/rotate already
    # guard via vm_instance_mismatch). Apply the freshness window only to
    # the warm-VM paths so the same-VM /status retry path stays intact.
    is_warm_idempotent_path = request.url.path in (
        "/api/agent/next-role",
        "/api/agent/vm-heartbeat",
    )

    def _enforce_terminal_freshness() -> None:
        """Reject a terminal-task JWT on warm-idempotent paths if its iat is
        outside the freshness window. A leaked JWT replayed an hour later is
        not a legitimate warm-chain continuation."""
        if not is_warm_idempotent_path:
            return
        iat = payload.get("iat")
        if iat is None:
            raise HTTPException(
                status_code=401,
                detail="terminal_token_expired_for_idempotent",
            )
        try:
            iat_secs = float(iat)
        except (TypeError, ValueError) as conv_exc:
            raise HTTPException(
                status_code=401,
                detail="terminal_token_expired_for_idempotent",
            ) from conv_exc
        if (time.time() - iat_secs) > _TERMINAL_IDEMPOTENT_FRESHNESS_SECS:
            raise HTTPException(
                status_code=401,
                detail="terminal_token_expired_for_idempotent",
            )

    # Fast path: known terminated (in-memory, O(1))
    # Same-VM retry on idempotent paths is allowed; a different-VM call is rejected.
    if task_id in _terminated_task_ids and not is_idempotent_path:
        raise HTTPException(status_code=401, detail="Task is no longer active")
    if task_id in _terminated_task_ids and is_warm_idempotent_path:
        # Cache says terminated AND we're on a warm-VM idempotent path —
        # apply the freshness window before falling through to the DB check.
        _enforce_terminal_freshness()
    # On idempotent paths with terminated task we fall through to the DB
    # check so we can compare token_vm against task.vm_instance_id (the
    # cache alone doesn't carry that field).

    # Advisory cache: keep recent-active bookkeeping for eviction/telemetry,
    # but never skip the DB proof step. A warmed cache cannot prove the task
    # still exists, is still active, or is still assigned to this VM.
    cached_expiry = _agent_active_cache.get(task_id)
    if cached_expiry and cached_expiry <= time.time():
        _agent_active_cache.pop(task_id, None)

    # DB proof path: required before every authenticated request so fail-closed
    # behavior applies even when an earlier call warmed the cache.
    # Use app.dependency_overrides to respect test session overrides
    try:
        app = request.app
        session_factory = app.dependency_overrides.get(get_session, get_session)
        async for session in session_factory():
            task = await session.get(Task, uuid.UUID(task_id))
            if task is None:
                raise HTTPException(status_code=401, detail="Task is no longer active")
            # Audit W3/E10: project_id authz when the token carries it.
            token_project = payload.get("project_id")
            if token_project and str(task.project_id) != token_project:
                raise HTTPException(status_code=403, detail="project_mismatch")
            # Audit W3/E2: stale-VM guard. If the task has been reassigned
            # to a different VM (reaper requeue), the original agent's
            # token must NOT be allowed to mutate state for the new VM's
            # task. Same-VM retries are still allowed (idempotent path).
            if token_vm and task.vm_instance_id and task.vm_instance_id != token_vm:
                raise HTTPException(status_code=403, detail="vm_instance_mismatch")
            if task.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                _terminated_task_ids[task_id] = time.time()
                # Same-VM idempotent retry on terminal task is OK; different
                # VM (or any non-idempotent path) fails fast.
                if not is_idempotent_path:
                    raise HTTPException(
                        status_code=401, detail="Task is no longer active"
                    )
                # Issue M3: warm-VM idempotent paths additionally require a
                # fresh JWT (iat within _TERMINAL_IDEMPOTENT_FRESHNESS_SECS)
                # so a leaked terminal-task token can't be replayed hours
                # later to spam-keepalive a phantom VM identity.
                _enforce_terminal_freshness()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(
            f"[AUTH] DB check failed for task {task_id}, rejecting request: {e}"
        )
        raise HTTPException(status_code=503, detail="agent_auth_db_unavailable") from e

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

_github_token_cache: Dict[
    str, Dict
] = {}  # project_id -> {"token", "owner", "repo", "fetched_at"}
_github_token_cache_lock = (
    asyncio.Lock()
)  # Prevent concurrent cache miss → duplicate token generation
_GITHUB_TOKEN_TTL = 3000  # 50 minutes (installation tokens expire after 60min)


def invalidate_github_token_cache(project_id: str) -> None:
    """Remove a project's cached GitHub token, forcing re-generation on next use."""
    removed = _github_token_cache.pop(project_id, None)
    if removed:
        logger.info(f"[GITHUB] Invalidated cached token for project {project_id}")


async def get_github_token_for_task(
    task_id: uuid.UUID,
    session: AsyncSession,
    force_refresh: bool = False,
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
            if (
                cached
                and (_utcnow() - cached["fetched_at"]).total_seconds()
                < _GITHUB_TOKEN_TTL
            ):
                return cached["token"], cached["owner"], cached["repo"]

    project_stmt = select(Project).where(Project.id == task.project_id)
    project = (await session.execute(project_stmt)).scalars().first()
    if not project or not project.user_id:
        raise HTTPException(status_code=404, detail="Project or user not found")

    if not project.github_owner or not project.github_repo:
        raise HTTPException(
            status_code=403,
            detail="Project does not have an authorized GitHub repository",
        )

    integration_stmt = select(UserIntegration).where(
        UserIntegration.user_id == project.user_id
    )
    integration = (await session.execute(integration_stmt)).scalars().first()

    # Prefer dynamic App-level discovery — always finds the correct installation
    # for this specific repo, even with multiple installations across orgs.
    token = None
    from app.github import get_repo_installation_token

    try:
        token = await get_repo_installation_token(
            project.github_owner, project.github_repo
        )
    except Exception:  # noqa: BLE001 — fall through to stored-installation + PAT fallbacks
        logger.debug(
            f"[GITHUB] App-level discovery failed for {project.github_owner}/{project.github_repo}, trying fallbacks"
        )

    if not token and integration and integration.github_installation_id:
        try:
            token = await generate_installation_access_token(
                integration.github_installation_id, repositories=[project.github_repo]
            )
        except Exception:  # noqa: BLE001 — fall through to PAT fallback
            logger.debug(
                f"[GITHUB] Stored installation token failed for {project.github_owner}/{project.github_repo}"
            )

    if not token and integration and integration.github_pat:
        token = await get_user_secret(integration.github_pat)

    if not token:
        raise HTTPException(
            status_code=400,
            detail="GitHub integration not found. Ensure the GitHub App is installed on this repository, or register a PAT via /api/github/pat.",
        )

    # Track successful GitHub API interaction for passive health checking
    _shared_state.last_github_success = _utcnow()

    # Cache token to avoid re-generating for every API call within the same project
    async with _github_token_cache_lock:
        _github_token_cache[project_id_str] = {
            "token": token,
            "owner": project.github_owner,
            "repo": project.github_repo,
            "fetched_at": _utcnow(),
        }
        # Evict stale entries
        stale_cutoff = _utcnow() - timedelta(seconds=_GITHUB_TOKEN_TTL + 600)
        for k in [
            k for k, v in _github_token_cache.items() if v["fetched_at"] < stale_cutoff
        ]:
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

_KNOWN_FAILURE_CATEGORIES = frozenset(
    {
        "CODE_ERROR",
        "PROVISION_FAILURE",
        "AGENT_TIMEOUT",
        "EXTERNAL_API",
        "RATE_LIMIT",
        "rejected in review",
    }
)


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
            inner = line[open_idx + 1 : close_idx].strip()
            if inner in _KNOWN_FAILURE_CATEGORIES:
                failure_type = inner
                rest = line[close_idx + 1 :]
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
                        meta = rest[meta_start + 1 : meta_end]
                        for part in meta.split():
                            if part.startswith("at="):
                                failed_at = part[len("at=") :]
                            elif part.startswith("stage="):
                                failure_stage = part[len("stage=") :]
                        rest = rest[:meta_start].rstrip() + rest[meta_end + 1 :]
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
# Helper: compact strategic vision for the claim response
# ---------------------------------------------------------------------------


def _build_vision_summary(vision_raw: dict | None) -> "dict | None":
    """Return a size-bounded, agent-safe view of project.vision.

    Strips internal fields (``_strategic_failures``, ``_strategic_cycle_history``,
    ``_strategic_frequency_override``, etc.) and truncates free-text entries so
    the claim payload stays bounded. Returns None when there is nothing
    meaningful to surface — so callers can skip injection entirely.
    """
    if not vision_raw or not isinstance(vision_raw, dict):
        return None
    summary: dict = {}
    purpose = vision_raw.get("purpose")
    if isinstance(purpose, str) and purpose.strip():
        summary["purpose"] = purpose.strip()[:600]
    success = vision_raw.get("success_criteria")
    if isinstance(success, str) and success.strip():
        summary["success_criteria"] = success.strip()[:600]
    elif isinstance(success, list):
        compact = [
            str(s).strip()[:200] for s in success if isinstance(s, str) and s.strip()
        ]
        if compact:
            summary["success_criteria"] = compact[:5]
    context_field = vision_raw.get("context")
    if isinstance(context_field, str) and context_field.strip():
        summary["context"] = context_field.strip()[:600]
    priorities = vision_raw.get("priorities")
    if isinstance(priorities, list):
        compact_p: list = []
        for p in priorities[:5]:
            if isinstance(p, str) and p.strip():
                compact_p.append(p.strip()[:200])
            elif isinstance(p, dict):
                text = p.get("text") or p.get("title") or p.get("name")
                if isinstance(text, str) and text.strip():
                    compact_p.append(text.strip()[:200])
        if compact_p:
            summary["priorities"] = compact_p
    systems = vision_raw.get("systems")
    if isinstance(systems, list):
        compact_s: list = []
        for sys in systems[:6]:
            if not isinstance(sys, dict):
                continue
            name = sys.get("name") or sys.get("id")
            if not isinstance(name, str) or not name.strip():
                continue
            entry = {"name": name.strip()[:80]}
            status = sys.get("status")
            if isinstance(status, str) and status.strip():
                entry["status"] = status.strip()[:40]
            desc = sys.get("description")
            if isinstance(desc, str) and desc.strip():
                entry["description"] = desc.strip()[:200]
            compact_s.append(entry)
        if compact_s:
            summary["systems"] = compact_s
    revision = vision_raw.get("revision")
    if isinstance(revision, int):
        summary["revision"] = revision
    last_updated = vision_raw.get("last_updated")
    if isinstance(last_updated, str) and last_updated:
        summary["last_updated"] = last_updated[:40]
    if not summary:
        return None
    return summary


# ---------------------------------------------------------------------------
# Helper: direction narrative (what we know, what's open)
# ---------------------------------------------------------------------------


def _build_direction_narrative(direction: dict) -> "dict | None":
    """Extract a short "where we are" narrative from a direction.

    Pulls the top bullets from the Established Findings and Open Questions
    sections of research_context, plus the most recent productive cycle
    outcome. The aim is to give tactical agents a sense of trajectory —
    what's been confirmed, what's still unknown — without dumping the full
    research document into the prompt.
    """
    if not isinstance(direction, dict):
        return None
    from app.research_context import _parse_research_context

    rc_text = direction.get("research_context", "") or ""
    parsed = _parse_research_context(rc_text) if rc_text else {}
    established = parsed.get("established", []) if isinstance(parsed, dict) else []
    open_questions = (
        parsed.get("open_questions", []) if isinstance(parsed, dict) else []
    )

    def _trim(items: list, limit: int, char_cap: int) -> list:
        return [
            item.strip()[:char_cap]
            for item in items[:limit]
            if isinstance(item, str) and item.strip()
        ]

    narrative: dict = {}
    top_established = _trim(established, 3, 240)
    if top_established:
        narrative["established_findings"] = top_established
    top_open = _trim(open_questions, 2, 240)
    if top_open:
        narrative["open_questions"] = top_open

    history = direction.get("research_history") or []
    productive = [
        h for h in history if isinstance(h, dict) and h.get("tasks_created", 0) > 0
    ]
    if productive:
        last = productive[-1]
        merged = int(last.get("prs_merged", 0) or 0)
        created = int(last.get("tasks_created", 0) or 0)
        rejected = int(last.get("prs_rejected", 0) or 0)
        if created > 0:
            narrative["last_cycle"] = {
                "tasks_created": created,
                "prs_merged": merged,
                "prs_rejected": rejected,
            }
    if not narrative:
        return None
    return narrative


# ---------------------------------------------------------------------------
# Agent lifecycle endpoints
# ---------------------------------------------------------------------------


@router.post("/refresh-token")
@limiter.limit("120/minute")
async def agent_refresh_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Accepts a valid (non-expired) agent JWT and returns a new one with a fresh 8-hour TTL.

    Audit 2026-04-25 (W3/E1/E2): refresh now also validates that the task
    is still assigned to this VM — a stale agent whose task has been
    requeued to a different VM gets 409 instead of a renewed token. The
    project_id is included in the new token so the W3 cross-project
    authz check (E10) can apply uniformly.
    """
    task_id = token["sub"]
    vm_id = token["vm_instance_id"]
    task = await session.get(Task, uuid.UUID(task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    if task.vm_instance_id != vm_id:
        raise HTTPException(status_code=409, detail="vm_instance_mismatch")
    if task.status not in (
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.QUEUED,
    ):
        raise HTTPException(status_code=409, detail=f"task_not_active:{task.status}")
    task.last_token_refresh_at = _utcnow()
    await session.commit()
    new_token = create_agent_token(
        task_id,
        vm_id,
        task_type=token.get("task_type", ""),
        project_id=token.get("project_id", "") or str(task.project_id),
    )
    return {"token": new_token}


async def _stamp_reviewer_recovery_count(
    session: AsyncSession,
    project_id: uuid.UUID,
    high_iter_rows,
) -> None:
    """Count tasks tagged as reviewer-recovery-after-incomplete-SR and stamp the
    aggregate onto ``Project.design_doc.sr_catch_rate_trends``.

    Detection scans both the top-level ``reviewer_recovery_origin`` marker and
    per-category ``check_results[*].origin`` entries — the marker can land in
    either spot depending on which path tagged it. No-op when nothing matched.
    """
    recovery_count = sum(
        1
        for t in high_iter_rows
        if (
            (t.context or {})
            .get("_self_review_outcome", {})
            .get("reviewer_recovery_origin")
            == "reviewer_recovery_after_incomplete_sr"
        )
        or any(
            isinstance(v, dict)
            and v.get("origin") == "reviewer_recovery_after_incomplete_sr"
            for v in (
                (t.context or {}).get("_self_review_outcome", {}).get("check_results")
                or {}
            ).values()
        )
    )
    if recovery_count <= 0:
        return
    from app.task_locking import project_lock

    async with project_lock(session, project_id, skip_locked=True) as proj:
        if proj is None:
            return
        doc = dict(proj.design_doc or {})
        trends = dict(doc.get("sr_catch_rate_trends") or {})
        trends["reviewer_recovery_after_incomplete_sr_count"] = recovery_count
        doc["sr_catch_rate_trends"] = trends
        proj.design_doc = doc
        flag_modified(proj, "design_doc")
        session.add(proj)


async def _refresh_sr_catch_rate(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Fetch SR-relevant tasks and refresh the catch-rate trend metric.

    Restricts the fetch to tasks carrying ``_self_review_outcome`` so the
    2000-row budget spans more calendar time on high-throughput projects.
    Postgres: ``has_key`` hits the GIN index on context; SQLite (standalone
    tests) omits the JSON filter and ``_stats_for_tasks`` skips non-SR rows.
    """
    from app.sr_analytics import compute_sr_catch_rate_trends

    _sr_cutoff = _utcnow() - timedelta(days=28)
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    _where = [
        Task.project_id == project_id,
        Task.status.in_([TaskStatus.FAILED, TaskStatus.COMPLETED]),
        Task.created_at >= _sr_cutoff,
    ]
    if dialect_name != "sqlite":
        _where.append(Task.context.has_key("_self_review_outcome"))
    _sr_rows = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.context,
                        Task.created_at,
                        Task.project_id,
                        Task.strategic_cycle_id,
                        Task.payloads,
                    )
                )
                .where(*_where)
                .order_by(Task.created_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    logger.debug(
        "[SR_CATCH_RATE] fetched %d SR-relevant tasks for catch-rate window",
        len(_sr_rows),
    )
    await compute_sr_catch_rate_trends(
        project_id=project_id,
        session=session,
        all_tasks=list(_sr_rows),
    )


@router.post("/claim")
@limiter.limit("200/minute")
async def claim_task(
    request: Request,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    vm_id = token.get("vm_instance_id")
    # Phase 2c Race A mitigation: a successful claim is a strong liveness
    # signal — we wouldn't be here without an authenticated VM. Recording
    # this widens the warm-VM continuity liveness window beyond just the
    # /vm-heartbeat and /next-role endpoints.
    _shared_state.record_vm_heartbeat(vm_id)
    stmt = (
        select(Task)
        .where(
            Task.vm_instance_id == vm_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING]),
        )
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    task = result.scalars().first()

    idempotent_replay = False
    if not task:
        # Distinguish "no task" from "task locked by another process"
        check = await session.execute(
            select(Task.id).where(
                Task.vm_instance_id == vm_id,
                Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING]),
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
        # within 5 min — covers slow GCP provisioning and the agent's worst-case
        # retry window of API_TIMEOUT * API_MAX_RETRIES with backoff) and replay
        # the same response.
        replay_cutoff = _utcnow() - timedelta(minutes=5)
        replay_stmt = select(Task).where(
            Task.vm_instance_id == vm_id,
            Task.status == TaskStatus.IN_PROGRESS,
            Task.last_heartbeat >= replay_cutoff,
        )
        task = (await session.execute(replay_stmt)).scalars().first()
        if not task:
            raise HTTPException(
                status_code=404, detail="No queued task found for this VM instance."
            )
        idempotent_replay = True

    if not idempotent_replay:
        task.status = TaskStatus.IN_PROGRESS
        task.last_heartbeat = (
            _utcnow()
        )  # Immediate heartbeat — don't wait 30s for agent interval
        # Record time-to-claim covering the full dispatch->agent-ready interval.
        # Use vm_dispatched_at (set before GCE create call) when available;
        # fall back to vm_provisioned_at for tasks created before migration 0113.
        _cold_start_anchor = task.vm_dispatched_at or task.vm_provisioned_at
        if _cold_start_anchor:
            claim_latency = (task.last_heartbeat - _cold_start_anchor).total_seconds()
            _stgs = dict((task.context or {}).get("_cold_start_stages") or {})
            _stgs["claim_at"] = task.last_heartbeat.isoformat()
            # Backfill provision_slot_acquired_at from compute's in-memory store if
            # not already present — lets efficiency snapshot compute dispatch_to_slot_ms.
            if "provision_slot_acquired_at" not in _stgs and task.vm_instance_id:
                from app.compute import get_provision_slot_acquired_at as _get_slot_at

                _slot_ts = _get_slot_at(task.vm_instance_id)
                if _slot_ts:
                    _stgs["provision_slot_acquired_at"] = _slot_ts
            task.context = {
                **(task.context or {}),
                "claim_latency_secs": round(claim_latency, 1),
                "_cold_start_stages": _stgs,
            }
            # Promote claim_latency to the dedicated vm_cold_start_ms column
            # for the efficiency direction (d-91d8dede). Guarded by IS NULL
            # so re-provision restamps of vm_provisioned_at (orchestrator.py
            # ~line 3240) cannot clobber the genuine first-claim measurement.
            if task.vm_cold_start_ms is None:
                task.vm_cold_start_ms = max(0, int(claim_latency * 1000))
        session.add(task)
        await session.commit()
        await session.refresh(task)

    # Broadcast status change to frontend
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.IN_PROGRESS},
    )

    # Strip internal runtime-routing fields before returning task context to the agent.
    safe_context = {
        k: v
        for k, v in (task.context or {}).items()
        if not k.startswith("max_plan_") and not k.startswith("llm_")
    }
    response = {
        "task_id": str(task.id),
        "title": task.title,
        "description": task.description,
        "payloads": task.payloads,
        "context": safe_context,
    }

    # Add project metadata so agents can adapt to different project structures
    project = await session.get(Project, task.project_id)
    if project:
        _build_cfg = project.build_config or {}
        response["project_metadata"] = {
            "project_id": str(project.id),
            "project_name": project.name,
            "github_owner": project.github_owner,
            "github_repo": project.github_repo,
            "build_config": _build_cfg,
            "enable_fresh_reviewer": _build_cfg.get("enable_fresh_reviewer", False),
            "enable_parallel_review": _build_cfg.get("enable_parallel_review", False),
            "enable_parallel_fix": _build_cfg.get("enable_parallel_fix", False),
            "enable_self_improvement": project.enable_self_improvement,
        }

    task_type = task.first_payload.get("type", "")
    if task_type in ("research", "plan", "create", "fix", "refactor", "review"):
        # Active tasks (IN_PROGRESS or EVALUATING) in the same project.
        # Phase B4 (H-P6): load_only drops status_log/plan_output JSONB.
        # Reads below: id, title, first_payload (payloads), status, context.
        # Audit R3-W2 (H6): cap fetch at 50 rows. The downstream loop
        # consumes these as a bounded set (rendered into the agent's
        # related-tasks payload), so unbounded fetch on a busy project
        # could pull 500 MB before truncating in Python.
        active_stmt = (
            select(Task)
            .options(
                load_only(
                    Task.id,
                    Task.title,
                    Task.status,
                    Task.payloads,
                    Task.context,
                    Task.project_id,
                )
            )
            .where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]),
            )
            .order_by(Task.created_at.desc())
            .limit(50)
        )
        active_tasks_rows = (await session.execute(active_stmt)).scalars().all()
        active_tasks = []
        for t in active_tasks_rows:
            p = t.first_payload
            active_tasks.append(
                {
                    "id": str(t.id),
                    "title": t.title,
                    "task_type": p.get("type", ""),
                    "status": t.status,
                    "prompt": p.get("prompt", p.get("description", "")),
                    "related_files": (t.context or {}).get("related_files", []),
                    "direction_id": (t.context or {}).get("direction_id"),
                }
            )

        # Queued tasks in the same project. Same projection as active_stmt.
        # Audit R3-W2 (H6): cap fetch at 50 rows (same rationale as above).
        queued_stmt = (
            select(Task)
            .options(
                load_only(
                    Task.id,
                    Task.title,
                    Task.status,
                    Task.payloads,
                    Task.context,
                    Task.project_id,
                )
            )
            .where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status == TaskStatus.QUEUED,
            )
            .order_by(Task.created_at.desc())
            .limit(50)
        )
        queued_tasks_rows = (await session.execute(queued_stmt)).scalars().all()
        queued_tasks = []
        for t in queued_tasks_rows:
            p = t.first_payload
            queued_tasks.append(
                {
                    "id": str(t.id),
                    "title": t.title,
                    "task_type": p.get("type", ""),
                    "prompt": p.get("prompt", p.get("description", "")),
                    "related_files": (t.context or {}).get("related_files", []),
                    "direction_id": (t.context or {}).get("direction_id"),
                }
            )

        # Recently completed tasks (10 most recent): completed first, then failed/cancelled.
        # Phase B4 (H-P6): drop status_log/plan_output JSONB. Loop reads below
        # also call classify_failure(t) which touches latest_status_message,
        # context, payloads — all included.
        _completed_loadonly = load_only(
            Task.id,
            Task.title,
            Task.status,
            Task.payloads,
            Task.context,
            Task.project_id,
            Task.description,
            Task.created_at,
            Task.latest_status_message,
            Task.account_id,
            Task.provider,
            Task.model,
        )
        completed_stmt = (
            select(Task)
            .options(_completed_loadonly)
            .where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status == TaskStatus.COMPLETED,
            )
            .order_by(Task.created_at.desc())
            .limit(10)
        )
        completed_tasks_rows = list(
            (await session.execute(completed_stmt)).scalars().all()
        )

        failed_cancelled_stmt = (
            select(Task)
            .options(_completed_loadonly)
            .where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
            )
            .order_by(Task.created_at.desc())
            .limit(max(0, 15 - len(completed_tasks_rows)))
        )
        failed_cancelled_rows = (
            (await session.execute(failed_cancelled_stmt)).scalars().all()
        )
        completed_tasks_rows = completed_tasks_rows + list(failed_cancelled_rows)

        completed_task_ids = [t.id for t in completed_tasks_rows]
        completed_artifacts: list = []
        if completed_task_ids:
            art_stmt = select(Artifact).where(
                Artifact.task_id.in_(completed_task_ids),
                Artifact.artifact_type.in_(
                    [ArtifactType.PULL_REQUEST, ArtifactType.RESEARCH_FINDINGS]
                ),
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
            t_prompt = (p.get("prompt") or p.get("description") or t.description or "")[
                :300
            ]
            t_related_files = (t_ctx.get("related_files") or [])[:10]
            t_direction_id = t_ctx.get("direction_id")
            is_failed = t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
            t_changed_files = (t_ctx.get("_pr_changed_files") or [])[:10] or None
            recently_completed_tasks.append(
                {
                    "id": str(t.id),
                    "title": t.title,
                    "task_type": p.get("type", ""),
                    "status": t.status.value
                    if hasattr(t.status, "value")
                    else str(t.status),
                    "pr_url": pr_url,
                    "research_findings_summary": research_findings_summary,
                    "failure_reason": (t.latest_status_message or "")[:500]
                    if is_failed and t.latest_status_message
                    else None,
                    "failure_class": classify_failure(t)
                    if is_failed and t.latest_status_message
                    else None,
                    "review_iterations": (t_ctx.get("review_loop", {}) or {}).get(
                        "iteration", 0
                    )
                    if t_ctx.get("review_loop")
                    else None,
                    "prompt": t_prompt or None,
                    "related_files": t_related_files,
                    "direction_id": t_direction_id,
                    "changed_files": t_changed_files,
                }
            )

        # Build lessons from high-iteration tasks (3+ review cycles).
        # Phase B4 (H-P6): drop status_log/plan_output. Loop reads context,
        # title, status, latest_status_message, plus classify_failure() inputs.
        lessons = []
        high_iter_stmt = (
            select(Task)
            .options(
                load_only(
                    Task.id,
                    Task.title,
                    Task.status,
                    Task.payloads,
                    Task.context,
                    Task.latest_status_message,
                    Task.created_at,
                    Task.project_id,
                    Task.account_id,
                    Task.provider,
                    Task.model,
                )
            )
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
                        lessons.append(
                            {
                                "task_title": ht.title,
                                "iterations": 0,
                                "last_feedback": (ht.latest_status_message or "")[:500],
                                "failure_class": "CODE_ERROR",
                                "direction_id": (ht.context or {}).get("direction_id"),
                            }
                        )
                if len(lessons) >= 8:
                    break

        # Direction-filtered recent tasks: up to 5 completed/failed from the same direction
        current_direction_id = (task.context or {}).get("direction_id")
        direction_recent_tasks = []
        if current_direction_id:
            # Phase B4 (H-P6): drop status_log/plan_output. Loop reads same
            # set as completed_stmt — id/title/status/context/payloads/etc.
            dir_stmt = (
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.title,
                        Task.status,
                        Task.payloads,
                        Task.context,
                        Task.project_id,
                        Task.description,
                        Task.created_at,
                        Task.latest_status_message,
                        Task.account_id,
                        Task.provider,
                        Task.model,
                    )
                )
                .where(
                    Task.project_id == task.project_id,
                    Task.id != task.id,
                    Task.status.in_(
                        [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                    ),
                )
                .order_by(Task.created_at.desc())
                .limit(50)
            )
            dir_rows = (await session.execute(dir_stmt)).scalars().all()
            dir_matches = [
                t
                for t in dir_rows
                if (t.context or {}).get("direction_id") == current_direction_id
            ][:5]
            dir_match_ids = [t.id for t in dir_matches]
            dir_artifacts_by_task: dict = {}
            if dir_match_ids:
                dir_art_stmt = select(Artifact).where(
                    Artifact.task_id.in_(dir_match_ids),
                    Artifact.artifact_type.in_(
                        [ArtifactType.PULL_REQUEST, ArtifactType.RESEARCH_FINDINGS]
                    ),
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
                t_prompt = (
                    p.get("prompt") or p.get("description") or t.description or ""
                )[:300]
                direction_recent_tasks.append(
                    {
                        "id": str(t.id),
                        "title": t.title,
                        "task_type": p.get("type", ""),
                        "status": t.status.value
                        if hasattr(t.status, "value")
                        else str(t.status),
                        "pr_url": pr_url,
                        "research_findings_summary": research_findings_summary,
                        "failure_reason": (t.latest_status_message or "")[:500]
                        if is_failed and t.latest_status_message
                        else None,
                        "failure_class": classify_failure(t)
                        if is_failed and t.latest_status_message
                        else None,
                        "review_iterations": (t_ctx.get("review_loop", {}) or {}).get(
                            "iteration", 0
                        )
                        if t_ctx.get("review_loop")
                        else None,
                        "prompt": t_prompt or None,
                        "related_files": (t_ctx.get("related_files") or [])[:10],
                        "direction_id": t_ctx.get("direction_id"),
                        "scope": t_ctx.get("scope"),
                        "changed_files": (t_ctx.get("_pr_changed_files") or [])[:10]
                        or None,
                    }
                )

        # Open PRs: distinct pr_url from PULL_REQUEST artifacts for non-terminal tasks in project
        open_prs_stmt = (
            select(Artifact.pr_url)
            .join(Task, Artifact.task_id == Task.id)
            .where(
                Task.project_id == task.project_id,
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                Artifact.pr_url.isnot(None),
                Task.status.not_in(
                    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                ),
            )
            .distinct()
            .limit(20)
        )
        open_prs_rows = (await session.execute(open_prs_stmt)).scalars().all()

        sibling_tasks = []
        if task.parent_task_id:
            # Phase B4 (H-P6): drop status_log/plan_output. Reads: id/title/
            # status/context/payloads — all included.
            _sibling_loadonly = load_only(
                Task.id,
                Task.title,
                Task.status,
                Task.payloads,
                Task.context,
                Task.latest_status_message,
                Task.description,
                Task.created_at,
                Task.parent_task_id,
                Task.project_id,
                Task.account_id,
                Task.provider,
                Task.model,
            )
            sibling_result = await session.execute(
                select(Task)
                .options(_sibling_loadonly)
                .where(
                    Task.parent_task_id == task.parent_task_id,
                    Task.id != task.id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                        ]
                    ),
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
                select(Task)
                .options(_sibling_loadonly)
                .where(
                    Task.parent_task_id == task.parent_task_id,
                    Task.id != task.id,
                    Task.status.in_(
                        [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                    ),
                )
                .order_by(Task.created_at.desc())
                .limit(5)
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
                sib_status = (
                    sib.status.value
                    if hasattr(sib.status, "value")
                    else str(sib.status)
                )
                pr_url = (sib.context or {}).get(
                    "rollup_pr_url"
                ) or terminal_sibling_pr_urls.get(sib.id)
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
            pipeline_health = await compute_pipeline_health_summary(
                session, task.project_id
            )
        except Exception:
            logger.exception(
                "[AGENT] Failed to compute pipeline health summary (non-critical)"
            )

        # Aggregate review issue distribution for create/fix/refactor tasks (last 14 days)
        review_issue_distribution = None
        top_first_review_issues = None
        direction_top_first_review_issues = None
        scope_specific_review_issues = None
        current_task_scope = (task.context or {}).get("scope")
        if task_type in ("create", "fix", "refactor"):
            try:
                from app.review_learning import _categorize_review_issues

                cutoff_14d = _utcnow() - timedelta(days=14)
                combined_review_history = []
                for ht in high_iter_rows:
                    # Task.created_at is TIMESTAMPTZ since 0061; cutoff_14d is naive.
                    ht_created = _to_naive_utc(ht.created_at)
                    if ht_created and ht_created < cutoff_14d:
                        continue
                    review_loop = (ht.context or {}).get("review_loop", {})
                    rev_hist = (
                        review_loop.get("review_history", []) if review_loop else []
                    )
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

                from app.review_learning import (
                    compute_recurring_first_review_descriptions,
                )

                try:
                    top_first_review_issues = (
                        compute_recurring_first_review_descriptions(
                            high_iter_rows,
                            cutoff_14d,
                        )
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute top_first_review_issues (non-critical)"
                    )

                # Direction-scoped recurring issues: per-task presence within the current direction.
                # Requires at least 3 direction-scoped tasks with review history to avoid noisy signals.
                try:
                    if current_direction_id:
                        direction_top_first_review_issues = (
                            compute_recurring_first_review_descriptions(
                                high_iter_rows,
                                cutoff_14d,
                                task_filter=lambda ht: (
                                    (ht.context or {}).get("direction_id")
                                    == current_direction_id
                                ),
                                min_tasks=3,
                                count_unique_tasks=True,
                            )
                        )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute direction_top_first_review_issues (non-critical)"
                    )

                try:
                    if current_task_scope:
                        scope_specific_review_issues = (
                            compute_recurring_first_review_descriptions(
                                high_iter_rows,
                                cutoff_14d,
                                task_filter=lambda ht: (
                                    (ht.context or {}).get("scope")
                                    == current_task_scope
                                ),
                                min_tasks=3,
                                extra_fields={"scope": current_task_scope},
                            )
                        )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute scope_specific_review_issues (non-critical)"
                    )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute review_issue_distribution (non-critical)"
                )

        # Compute review effectiveness trends (stores in project.design_doc as side effect)
        review_effectiveness = None
        if task_type in ("create", "fix", "refactor"):
            try:
                from app.review_learning import compute_review_effectiveness

                review_effectiveness = await compute_review_effectiveness(
                    project_id=task.project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute review_effectiveness (non-critical)"
                )
            try:
                await _refresh_sr_catch_rate(session, task.project_id)
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute sr_catch_rate_trends (non-critical)"
                )
            try:
                from app.sr_analytics import compute_sr_false_attestation_trends

                await compute_sr_false_attestation_trends(
                    project_id=task.project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute sr_false_attestation_trends (non-critical)"
                )
            try:
                from app.sr_analytics import (
                    compute_sr_regression_risk_test_signal_trends,
                )

                await compute_sr_regression_risk_test_signal_trends(
                    project_id=task.project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute sr_regression_risk_test_signal_trends (non-critical)"
                )
            try:
                from app.sr_analytics import compute_fresh_reviewer_trend

                await compute_fresh_reviewer_trend(
                    project_id=task.project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute fresh_reviewer_trend (non-critical)"
                )
            try:
                from app.sr_analytics import compute_edges_trend

                await compute_edges_trend(
                    project_id=task.project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception("[AGENT] Failed to compute edges_trend (non-critical)")
            try:
                await _stamp_reviewer_recovery_count(
                    session, task.project_id, high_iter_rows
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute reviewer_recovery_after_incomplete_sr_count (non-critical)"
                )
            try:
                from app.sr_analytics import compute_sr_reviewer_disagreements

                await compute_sr_reviewer_disagreements(
                    project_id=task.project_id,
                    session=session,
                    all_tasks=list(high_iter_rows),
                )
            except Exception:
                logger.exception(
                    "[AGENT] Failed to compute sr_reviewer_disagreements (non-critical)"
                )

        direction_concurrent_files: list[str] = []
        if current_direction_id:
            _seen_files: set[str] = set()
            for _at in active_tasks:
                if _at.get("direction_id") != current_direction_id:
                    continue
                for _f in _at.get("related_files") or []:
                    if isinstance(_f, str) and _f and _f not in _seen_files:
                        _seen_files.add(_f)
                        direction_concurrent_files.append(_f)

        response["project_context"] = {
            "active_tasks": active_tasks,
            "direction_concurrent_files": direction_concurrent_files,
            "queued_tasks": queued_tasks,
            "recently_completed_tasks": recently_completed_tasks,
            "direction_recent_tasks": direction_recent_tasks
            if current_direction_id
            else [],
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
                _ctx["_top_review_issue_categories"] = list(
                    review_issue_distribution.keys()
                )
            task.context = _ctx
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
        except Exception:
            logger.exception(
                "[AGENT] Failed to record review measurement attribution (non-critical)"
            )
    else:
        # Lightweight pipeline health for all other task types
        try:
            pipeline_health = await compute_pipeline_health_summary(
                session, task.project_id
            )
            if pipeline_health:
                response["project_context"] = {"pipeline_health": pipeline_health}
        except Exception:
            logger.exception(
                "[AGENT] Failed to compute lightweight pipeline health (non-critical)"
            )

    # Task types that benefit from untagged operational playbook entries.
    # Types not in this set only receive entries with explicit applicable_types matching them.
    _PLAYBOOK_DEFAULT_TYPES = {
        "create",
        "fix",
        "refactor",
        "review",
        "qa",
        "smoketest",
        "deps",
        "migrate",
        "debug",
        "docs",
    }

    # Inject playbook for ALL task types (outside the project_context gate)
    project_for_playbook = await session.get(Project, task.project_id)
    if project_for_playbook and project_for_playbook.design_doc:
        task_type = (task.first_payload).get("type", "create")
        playbook_entries = {}
        _playbook_candidates = []
        _task_direction_id = str((task.context or {}).get("direction_id") or "")
        _task_related_files = {
            str(f)
            for f in ((task.context or {}).get("related_files") or [])
            if isinstance(f, str) and f
        }
        _file_to_system_raw = (project_for_playbook.design_doc or {}).get(
            "_file_to_system"
        ) or {}
        _file_to_system = (
            _file_to_system_raw if isinstance(_file_to_system_raw, dict) else {}
        )
        _task_systems = {
            str(_file_to_system.get(f))
            for f in _task_related_files
            if isinstance(_file_to_system.get(f), str) and _file_to_system.get(f)
        }
        _my_task_id = str(task.id)
        for k, v in (project_for_playbook.design_doc or {}).items():
            if k.startswith("playbook/") and isinstance(v, dict):
                # Anti-poisoning: skip entries that are pending writes from
                # OTHER non-completed tasks. The source task can still see
                # its own pending entries; peers see them only after the
                # source task COMPLETES (pending_source cleared) or skip
                # them entirely if the source FAILED (entry removed). See
                # main.py update_playbook for the rationale.
                pending_source = v.get("pending_source")
                if pending_source and pending_source != _my_task_id:
                    continue
                applicable_types = v.get("applicable_types")
                if applicable_types is not None:
                    if task_type not in applicable_types:
                        continue
                elif task_type not in _PLAYBOOK_DEFAULT_TYPES:
                    continue
                short_key = k[len("playbook/") :]
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
                        short_key,
                        referenced,
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
                        short_key,
                        age_penalty,
                        stale_penalty,
                    )
                # Effectiveness score: confirmed / referenced ratio (higher = better)
                effectiveness_score = confirmed / max(referenced, 1)
                entry_direction = str(
                    v.get("learned_from_direction") or v.get("direction_key") or ""
                )
                raw_direction_successes = v.get("direction_successes")
                direction_successes = (
                    raw_direction_successes
                    if isinstance(raw_direction_successes, dict)
                    else {}
                )
                direction_success_count = (
                    direction_successes.get(_task_direction_id, 0)
                    if _task_direction_id
                    else 0
                )
                direction_match = int(
                    bool(
                        _task_direction_id
                        and (
                            (entry_direction and entry_direction == _task_direction_id)
                            or (
                                isinstance(direction_success_count, (int, float))
                                and direction_success_count > 0
                            )
                        )
                    )
                )
                entry_systems = {
                    str(s)
                    for s in (
                        [v.get("learned_from_system")]
                        if v.get("learned_from_system")
                        else (v.get("systems") or [])
                    )
                    if isinstance(s, str) and s
                }
                system_match = int(
                    bool(
                        _task_systems
                        and entry_systems
                        and (_task_systems & entry_systems)
                    )
                )
                entry_related = {
                    str(f)
                    for f in (v.get("related_files") or [])
                    if isinstance(f, str) and f
                }
                file_overlap_count = len(_task_related_files & entry_related)
                relevance_reasons: list[str] = []
                if direction_match:
                    relevance_reasons.append("same direction")
                if system_match:
                    relevance_reasons.append("same system")
                if file_overlap_count > 0:
                    relevance_reasons.append(
                        f"{file_overlap_count} overlapping file(s)"
                    )
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
                _playbook_candidates.append(
                    (
                        -direction_match,
                        -system_match,
                        -file_overlap_count,
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
                        relevance_reasons,
                    )
                )
        _playbook_candidates.sort()
        # Cap shown entries: the sort already prioritizes direction match →
        # system match → file overlap → effectiveness, so the head of the
        # list is the most relevant subset. Without a cap, every fix task
        # was getting flooded with ~30 entries (most irrelevant), burning
        # prompt tokens and attention. Configurable via runtime_config so
        # we can tune per task type without a redeploy. Defaults: fix/qa/
        # review get 8 (focused work, narrow scope); create/refactor get 12
        # (broader investigation, more priors useful); other types get 10.
        _playbook_caps = (
            (_shared_state.runtime_config.get("playbook_max_entries") or {})
            if isinstance(
                _shared_state.runtime_config.get("playbook_max_entries"), dict
            )
            else {}
        )
        _default_caps = {"fix": 8, "qa": 8, "review": 8, "create": 12, "refactor": 12}
        _cap = int(_playbook_caps.get(task_type, _default_caps.get(task_type, 10)))
        _capped_candidates = _playbook_candidates[: max(_cap, 0)]
        for (
            _,
            _,
            _,
            _,
            _,
            _,
            _,
            _,
            short_key,
            content,
            eff_score,
            conf_count,
            age_days_val,
            is_stale,
            relevance_reasons,
        ) in _capped_candidates:
            playbook_entries[short_key] = {
                "content": content,
                "effectiveness_score": round(eff_score, 3),
                "confirmed_count": conf_count,
                "age_days": age_days_val,
                "stale": is_stale,
                "relevance": {
                    "reasons": relevance_reasons,
                    "local_match": bool(relevance_reasons),
                },
            }
        if len(_playbook_candidates) > len(_capped_candidates):
            logger.info(
                "Playbook capped to %d/%d entries for task %s (type=%s)",
                len(_capped_candidates),
                len(_playbook_candidates),
                task.id,
                task_type,
            )
        total_playbook_count = sum(
            1
            for k in (project_for_playbook.design_doc or {})
            if k.startswith("playbook/")
        )
        response["first_task_on_repo"] = total_playbook_count == 0
        if playbook_entries:
            response["playbook"] = playbook_entries
            # Track reference counts + stale-ref check — fire-and-forget, must not fail claim response
            try:
                now_iso = _utcnow().isoformat()
                from app.task_locking import project_lock as _pb_project_lock

                async with _pb_project_lock(
                    session, task.project_id, skip_locked=True
                ) as _pb_proj:
                    if _pb_proj is not None:
                        doc = dict(_pb_proj.design_doc or {})
                        for short_key in playbook_entries:
                            full_key = f"playbook/{short_key}"
                            if full_key in doc and isinstance(doc[full_key], dict):
                                entry = dict(doc[full_key])
                                entry["reference_count"] = (
                                    entry.get("reference_count", 0) + 1
                                )
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
                        # Check file-path references (config-driven; skipped when unset)
                        try:
                            from app.playbook_learning import check_and_flag_stale_refs

                            repo_root: str = (
                                _shared_state.runtime_config.get(
                                    "playbook_repo_root", ""
                                )
                                or ""
                            )
                            if repo_root:
                                doc = check_and_flag_stale_refs(doc, repo_root)
                        except Exception:  # noqa: BLE001 — best-effort, never block claim
                            logger.debug(
                                "[AGENT] Stale-ref check skipped (non-critical)"
                            )
                        try:
                            from app.playbook_learning import retire_idle_auto_entries

                            idle_days = int(
                                _shared_state.runtime_config.get(
                                    "playbook_idle_auto_days", 30
                                )
                            )
                            retire_idle_auto_entries(doc, idle_days=idle_days)
                        except Exception:  # noqa: BLE001 — best-effort, never block claim
                            logger.debug(
                                "[AGENT] Idle auto-entry retirement skipped (non-critical)"
                            )
                        try:
                            from app.playbook_learning import (
                                retire_ineffective_patterns,
                            )

                            retire_ineffective_patterns(doc)
                        except Exception:  # noqa: BLE001 — best-effort, never block claim
                            logger.debug(
                                "[AGENT] Low-success-rate retirement skipped (non-critical)"
                            )
                        _pb_proj.design_doc = doc
                        flag_modified(_pb_proj, "design_doc")
                        session.add(_pb_proj)
                # Store shown playbook keys in task context for outcome tracking at completion
                task_ctx = dict(task.context or {})
                task_ctx["_shown_playbook_keys"] = list(playbook_entries.keys())
                task.context = task_ctx
                flag_modified(task, "context")
                session.add(task)
                await session.commit()
            except Exception:
                logger.exception(
                    "[AGENT] Failed to update playbook reference counts (non-critical)"
                )

    # Inject direction quality signal when total PRs >= 3 (reuse project already loaded above)
    direction_id = (task.context or {}).get("direction_id")
    if direction_id and project_for_playbook:
        direction = next(
            (
                d
                for d in (project_for_playbook.directions or [])
                if d.get("id") == direction_id
            ),
            None,
        )
        if direction:
            history = direction.get("research_history", [])
            agg = direction.get("history_aggregate", {})
            total_merged = sum(e.get("prs_merged", 0) for e in history) + (
                agg.get("total_prs_merged") or 0
            )
            total_rejected = sum(e.get("prs_rejected", 0) for e in history) + (
                agg.get("total_prs_rejected") or 0
            )
            total_prs = total_merged + total_rejected
            if total_prs >= 3:
                acceptance_rate = total_merged / total_prs
                merge_time_values = [
                    h["avg_time_to_merge_hours"]
                    for h in history
                    if h.get("avg_time_to_merge_hours") is not None
                ]
                avg_ttm = (
                    round(sum(merge_time_values) / len(merge_time_values), 2)
                    if merge_time_values
                    else None
                )
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
                    {
                        "tasks_created": h.get("tasks_created", 0),
                        "prs_merged": h.get("prs_merged", 0),
                    }
                    for h in history[-2:]
                    if h.get("tasks_created", 0) > 0
                ]
                failure_stage_pattern = _compute_failure_stage_pattern(history)

                # Aggregate scope_breakdown from the last 3 productive research cycles
                scope_success_rates: dict[str, dict[str, float | int]] = {}
                expansion_weighted: dict[str, dict[str, float]] = {}
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
                            scope_name,
                            {
                                "created": 0,
                                "merged": 0,
                                "failed": 0,
                                "merged_cross_module": 0,
                            },
                        )
                        agg_entry["created"] += int(counts.get("created", 0) or 0)
                        merged_this = int(counts.get("merged", 0) or 0)
                        agg_entry["merged"] += merged_this
                        agg_entry["failed"] += int(counts.get("failed", 0) or 0)
                        agg_entry["merged_cross_module"] += int(
                            counts.get("merged_cross_module", 0) or 0
                        )
                        ratio = counts.get("avg_expansion_ratio_merged") or counts.get(
                            "avg_expansion_ratio"
                        )
                        if (
                            isinstance(ratio, (int, float))
                            and ratio > 0
                            and merged_this > 0
                        ):
                            ew = expansion_weighted.setdefault(
                                scope_name, {"sum": 0.0, "weight": 0.0}
                            )
                            ew["sum"] += float(ratio) * merged_this
                            ew["weight"] += merged_this
                scope_success_rates = {
                    k: v for k, v in scope_success_rates.items() if v["created"] >= 1
                }
                for scope_name, ew in expansion_weighted.items():
                    if ew["weight"] > 0 and scope_name in scope_success_rates:
                        scope_success_rates[scope_name]["avg_expansion_ratio"] = round(
                            ew["sum"] / ew["weight"], 2
                        )

                # Review-iteration trend: rolling median across last 3 cycles + last-cycle value
                from app.direction_metrics import (
                    _compute_rolling_median_review_iterations,
                )

                review_iteration_rolling_median = (
                    _compute_rolling_median_review_iterations(direction)
                )
                review_iteration_last_cycle: float | None = None
                if history:
                    last_val = history[-1].get("median_review_iterations")
                    if isinstance(last_val, (int, float)):
                        review_iteration_last_cycle = float(last_val)

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
                    if (
                        not analysis_plan
                        and not test_failure_summary
                        and not status_message
                    ):
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
                        prompt_version = self_review_outcome.get("prompt_version")
                        if prompt_version is not None:
                            sro_compact["prompt_version"] = prompt_version
                        check_results = self_review_outcome.get("check_results")
                        if isinstance(check_results, dict):
                            failed_checks = [
                                str(name)
                                for name, result in check_results.items()
                                if isinstance(result, dict)
                                and result.get("passed") is False
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
                    # Audit W1: previous_error_fingerprint is now a column on the Task.
                    fingerprint = pt.previous_error_fingerprint
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
                                            desc = str(
                                                issue.get("description", "")
                                            ).strip()
                                            if not desc:
                                                continue
                                            item = {
                                                "severity": sev.upper(),
                                                "description": desc[:150],
                                            }
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
                                    feedback_text = str(
                                        last_entry.get("feedback") or ""
                                    ).strip()
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
                    has_changed_files = (
                        isinstance(changed_files, list) and changed_files
                    )
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
                        s_entry["pr_changed_files"] = [
                            str(f) for f in changed_files[:10]
                        ]
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
                dfa_entry: dict = {}
                try:
                    dfa_all = (project_for_playbook.design_doc or {}).get(
                        "direction_first_attempt_feedback"
                    ) or {}
                    dfa_entry = dfa_all.get(str(direction_id)) or {}
                    dfa_cats = {
                        k: v
                        for k, v in dfa_entry.items()
                        if k not in ("total", "last_updated")
                        and isinstance(v, int)
                        and v > 0
                    }
                    if dfa_cats:
                        direction_first_attempt_feedback = dict(
                            sorted(dfa_cats.items(), key=lambda kv: -kv[1])
                        )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to read direction_first_attempt_feedback (non-critical)"
                    )

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
                if review_iteration_rolling_median is not None:
                    response["direction_context"]["review_iteration_rolling_median"] = (
                        review_iteration_rolling_median
                    )
                if review_iteration_last_cycle is not None:
                    response["direction_context"]["review_iteration_last_cycle"] = (
                        review_iteration_last_cycle
                    )
                from app.direction_outcomes import summarize_review_quality_feedback

                review_quality_feedback = summarize_review_quality_feedback(
                    history, dfa_entry
                )
                if review_quality_feedback:
                    response["direction_context"]["review_quality_feedback"] = (
                        review_quality_feedback
                    )

            # Surface saturation / deadlock signals to research agents so they can
            # pivot to direction_update/direction_note findings on stuck directions.
            if task_type == "research":
                from app.research_completion import _detect_dedup_deadlock

                deadlocked, _ = _detect_dedup_deadlock(history)
                research_recent_outcomes = [
                    {
                        "tasks_created": h.get("tasks_created", 0),
                        "prs_merged": h.get("prs_merged", 0),
                    }
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
                            scope_name,
                            {
                                "created": 0,
                                "merged": 0,
                                "failed": 0,
                                "merged_cross_module": 0,
                            },
                        )
                        agg["created"] += int(counts.get("created", 0) or 0)
                        agg["merged"] += int(counts.get("merged", 0) or 0)
                        agg["failed"] += int(counts.get("failed", 0) or 0)
                        agg["merged_cross_module"] += int(
                            counts.get("merged_cross_module", 0) or 0
                        )
                research_scope_rates = {
                    k: v for k, v in research_scope_rates.items() if v["created"] >= 1
                }
                research_top_review_issues = None
                try:
                    from app.review_learning import (
                        compute_recurring_first_review_descriptions,
                    )

                    if current_direction_id:
                        research_top_review_issues = (
                            compute_recurring_first_review_descriptions(
                                high_iter_rows,
                                _utcnow() - timedelta(days=14),
                                task_filter=lambda ht: (
                                    (ht.context or {}).get("direction_id")
                                    == current_direction_id
                                ),
                                min_tasks=3,
                                count_unique_tasks=True,
                            )
                        )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research top_first_review_issues (non-critical)"
                    )
                research_review_effectiveness = None
                try:
                    from app.review_learning import compute_review_effectiveness

                    research_review_effectiveness = await compute_review_effectiveness(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research review_effectiveness (non-critical)"
                    )
                try:
                    await _refresh_sr_catch_rate(session, task.project_id)
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research sr_catch_rate_trends (non-critical)"
                    )
                try:
                    from app.playbook_learning import (
                        compute_sr_false_attestation_trends,
                    )

                    await compute_sr_false_attestation_trends(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research sr_false_attestation_trends (non-critical)"
                    )
                try:
                    from app.playbook_learning import (
                        compute_sr_regression_risk_test_signal_trends,
                    )

                    await compute_sr_regression_risk_test_signal_trends(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research sr_regression_risk_test_signal_trends (non-critical)"
                    )
                try:
                    from app.sr_analytics import compute_fresh_reviewer_trend

                    await compute_fresh_reviewer_trend(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research fresh_reviewer_trend (non-critical)"
                    )
                try:
                    from app.sr_analytics import compute_edges_trend

                    await compute_edges_trend(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research edges_trend (non-critical)"
                    )
                try:
                    await _stamp_reviewer_recovery_count(
                        session, task.project_id, high_iter_rows
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research reviewer_recovery_after_incomplete_sr_count (non-critical)"
                    )
                try:
                    from app.sr_analytics import compute_sr_reviewer_disagreements

                    await compute_sr_reviewer_disagreements(
                        project_id=task.project_id,
                        session=session,
                        all_tasks=list(high_iter_rows),
                    )
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to compute research sr_reviewer_disagreements (non-critical)"
                    )
                success_summary: list[str] = []
                for scope_name, counts in research_scope_rates.items():
                    created = counts["created"]
                    merged = counts["merged"]
                    if created >= 2:
                        success_summary.append(
                            f"{scope_name}: {merged}/{created} merged ({round(merged / created * 100)}%)"
                        )
                from app.direction_outcomes import (
                    summarize_review_quality_feedback,
                    synthesize_prompt_quality_patterns,
                )

                prompt_quality_patterns = synthesize_prompt_quality_patterns(history)
                response["direction_context"] = {
                    "possibly_saturated": bool(
                        direction.get("possibly_saturated", False)
                    ),
                    "deadlock_detected": bool(deadlocked),
                    "recent_outcomes": research_recent_outcomes,
                    "failure_stage_pattern": research_failure_stage_pattern,
                    "scope_success_rates": research_scope_rates,
                    "top_first_review_issues": research_top_review_issues,
                    "success_patterns": success_summary,
                    "review_effectiveness": research_review_effectiveness,
                }
                if prompt_quality_patterns:
                    response["direction_context"]["prompt_quality_patterns"] = (
                        prompt_quality_patterns
                    )
                research_dfa_entry: dict = {}
                try:
                    if project_for_playbook and current_direction_id:
                        research_dfa_entry = (
                            (project_for_playbook.design_doc or {}).get(
                                "direction_first_attempt_feedback"
                            )
                            or {}
                        ).get(str(current_direction_id)) or {}
                except Exception:
                    logger.exception(
                        "[AGENT] Failed to read direction_first_attempt_feedback for research (non-critical)"
                    )
                research_review_quality_feedback = summarize_review_quality_feedback(
                    history, research_dfa_entry
                )
                if research_review_quality_feedback:
                    response["direction_context"]["review_quality_feedback"] = (
                        research_review_quality_feedback
                    )

                from app.direction_outcomes import (
                    summarize_dedup_filter,
                    summarize_failure_hotspots,
                )

                failure_hotspots = summarize_failure_hotspots(
                    direction, task.project_id
                )
                if failure_hotspots:
                    response["direction_context"]["failure_hotspots"] = failure_hotspots
                dedup_summary = summarize_dedup_filter(history)
                if dedup_summary:
                    response["direction_context"]["dedup_filter_summary"] = (
                        dedup_summary
                    )

    # Inject co-change affinity context for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        related_files = (task.context or {}).get("related_files", [])
        if related_files:
            cochange_index = (project_for_playbook.design_doc or {}).get(
                "cochange_index"
            )
            if cochange_index:
                cochange_scores: dict[str, float] = {}
                related_set = set(related_files)
                for rf in related_files:
                    for co_file, score in (cochange_index.get(rf) or {}).items():
                        if co_file not in related_set:
                            cochange_scores[co_file] = (
                                cochange_scores.get(co_file, 0.0) + score
                            )
                if cochange_scores:
                    top_cochange = sorted(
                        cochange_scores.items(), key=lambda x: x[1], reverse=True
                    )[:10]
                    if "project_context" not in response:
                        response["project_context"] = {}
                    response["project_context"]["cochange_files"] = [
                        f for f, _ in top_cochange
                    ]

    # Inject within-set co-change affinity hints for plan tasks so the planner
    # can group high-affinity files into the same subtask and split low-affinity
    # files into independent ones. Only pairs where both files are in the
    # finding's related_files and score >= 1.0 are surfaced.
    if task_type == "plan" and project_for_playbook:
        plan_related_files = (task.context or {}).get("related_files", [])
        if plan_related_files:
            cochange_index = (project_for_playbook.design_doc or {}).get(
                "cochange_index"
            ) or {}
            cochange_hints: dict[str, dict[str, float]] = {}
            related_set = set(plan_related_files)
            for rf in plan_related_files:
                for co_file, score in (cochange_index.get(rf) or {}).items():
                    if (
                        co_file in related_set
                        and isinstance(score, (int, float))
                        and score >= 1.0
                    ):
                        cochange_hints.setdefault(rf, {})[co_file] = round(
                            float(score), 3
                        )
            if cochange_hints:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["cochange_hints"] = cochange_hints

    if task_type == "plan" and direction_id and project_for_playbook:
        direction_for_decomp = next(
            (
                d
                for d in (project_for_playbook.directions or [])
                if d.get("id") == direction_id
            ),
            None,
        )
        if direction_for_decomp:
            from app.playbook_learning import summarize_decomposition_outcomes

            decomposition_outcomes = summarize_decomposition_outcomes(
                direction_for_decomp
            )
            if decomposition_outcomes:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["decomposition_outcomes"] = (
                    decomposition_outcomes
                )

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
                cat_counts = {
                    k: v
                    for k, v in profile.items()
                    if k not in ("total", "last_updated") and isinstance(v, int)
                }
                sorted_cats = sorted(cat_counts.items(), key=lambda x: -x[1])
                top_cats = {
                    cat: round(count / total, 2)
                    for cat, count in sorted_cats[:3]
                    if count / total >= 0.15
                }
                if top_cats:
                    module_warnings.append(
                        {
                            "file": file_path,
                            "top_categories": top_cats,
                            "sample_size": total,
                        }
                    )
            if module_warnings:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["module_feedback_warnings"] = (
                    module_warnings
                )

    # Inject SR catch-rate blind spots for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        try:
            sr_trends = (project_for_playbook.design_doc or {}).get(
                "sr_catch_rate_trends"
            )
            if sr_trends and isinstance(sr_trends.get("categories"), dict):

                def _cat_entry(data: dict) -> dict:
                    entry: dict = {"miss_rate": data["miss_rate"]}
                    if isinstance(data.get("weighted_miss_rate"), (int, float)):
                        entry["weighted_miss_rate"] = data["weighted_miss_rate"]
                    if isinstance(data.get("examples"), list) and data["examples"]:
                        entry["examples"] = data["examples"]
                    fp_rate = data.get("false_positive_rate")
                    if isinstance(fp_rate, (int, float)):
                        entry["false_positive_rate"] = fp_rate
                        entry["precision"] = round(1 - float(fp_rate), 3)
                    fp_count = data.get("fp_count")
                    tp_count = data.get("tp_count")
                    if isinstance(fp_count, int) and isinstance(tp_count, int):
                        entry["sr_flag_sample_size"] = fp_count + tp_count
                    sev_cal = data.get("severity_calibration")
                    if isinstance(sev_cal, dict):
                        entry["severity_calibration"] = sev_cal
                    quadrant = data.get("precision_recall_quadrant")
                    if isinstance(quadrant, str) and quadrant:
                        entry["precision_recall_quadrant"] = quadrant
                    p_delta = data.get("precision_delta")
                    if isinstance(p_delta, (int, float)):
                        entry["precision_delta"] = p_delta
                    r_delta = data.get("recall_delta")
                    if isinstance(r_delta, (int, float)):
                        entry["recall_delta"] = r_delta
                    # Regression signals consumed by the agent-side
                    # failure-mode hint (self-review.ts:formatFailureModeHintForSrPrompt):
                    # it renders only when below_floor/trend marks the category
                    # regressing AND miss_mode_breakdown carries a dominant_mode.
                    trend = data.get("trend")
                    if isinstance(trend, str) and trend:
                        entry["trend"] = trend
                    below_floor = data.get("below_floor")
                    if isinstance(below_floor, bool):
                        entry["below_floor"] = below_floor
                    engagement_thinning = data.get("engagement_thinning")
                    if isinstance(engagement_thinning, bool):
                        entry["engagement_thinning"] = engagement_thinning
                    miss_mode_breakdown = data.get("miss_mode_breakdown")
                    if isinstance(miss_mode_breakdown, dict):
                        entry["miss_mode_breakdown"] = miss_mode_breakdown
                    return entry

                cats = {
                    cat: _cat_entry(data)
                    for cat, data in sr_trends["categories"].items()
                    if isinstance(data, dict)
                    and isinstance(data.get("miss_rate"), (int, float))
                }
                if cats:
                    catch_rate_payload: dict = {"categories": cats}
                    by_task_type = sr_trends.get("by_task_type") or {}
                    if isinstance(by_task_type, dict) and task_type in by_task_type:
                        type_cats = by_task_type[task_type]
                        if isinstance(type_cats, dict):
                            catch_rate_payload["for_task_type"] = {
                                cat: _cat_entry(cdata)
                                for cat, cdata in type_cats.items()
                                if isinstance(cdata, dict)
                                and isinstance(cdata.get("miss_rate"), (int, float))
                            }
                    response["project_context"]["self_review_catch_rate"] = (
                        catch_rate_payload
                    )

                by_direction = sr_trends.get("by_direction") or {}
                task_direction_id = (task.context or {}).get("direction_id")
                if (
                    task_direction_id
                    and isinstance(by_direction, dict)
                    and isinstance(by_direction.get(task_direction_id), dict)
                ):
                    dir_entry = by_direction[task_direction_id]
                    dir_categories_raw = dir_entry.get("categories")
                    if isinstance(dir_categories_raw, dict):
                        dir_cats = {
                            cat: _cat_entry(cdata)
                            for cat, cdata in dir_categories_raw.items()
                            if isinstance(cdata, dict)
                            and isinstance(cdata.get("miss_rate"), (int, float))
                        }
                        if dir_cats:
                            # Prefer direction_text already stored in the snapshot;
                            # fall back to scanning project.directions for older entries.
                            direction_text = dir_entry.get("direction_text") or ""
                            if not direction_text:
                                for d in project_for_playbook.directions or []:
                                    if d.get("id") == task_direction_id:
                                        direction_text = (
                                            d.get("text")
                                            or d.get("description")
                                            or d.get("title")
                                            or ""
                                        )
                                        break
                            direction_payload: dict = {
                                "direction_id": task_direction_id,
                                "direction_text": direction_text,
                                "categories": dir_cats,
                            }
                            if isinstance(dir_entry.get("sample_count"), int):
                                direction_payload["sample_count"] = dir_entry[
                                    "sample_count"
                                ]
                            if isinstance(dir_entry.get("miss_rate"), (int, float)):
                                direction_payload["miss_rate"] = dir_entry["miss_rate"]
                            if isinstance(
                                dir_entry.get("overall_catch_rate"), (int, float)
                            ):
                                direction_payload["overall_catch_rate"] = dir_entry[
                                    "overall_catch_rate"
                                ]
                            if isinstance(dir_entry.get("trend"), str):
                                direction_payload["trend"] = dir_entry["trend"]
                            if isinstance(
                                dir_entry.get("previous_miss_rate"), (int, float)
                            ):
                                direction_payload["previous_miss_rate"] = dir_entry[
                                    "previous_miss_rate"
                                ]
                            if isinstance(dir_entry.get("rate_history"), list):
                                direction_payload["rate_history"] = dir_entry[
                                    "rate_history"
                                ]
                            response["project_context"][
                                "self_review_catch_rate_for_direction"
                            ] = direction_payload
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject self_review_catch_rate (non-critical)"
            )

    # Inject project-wide SR blind-spot themes (last 14 days, top 8). Surfaces
    # recurring areas where self-review has been most uncertain so the agent
    # can apply extra scrutiny in those areas. Shape:
    #   project_context.sr_blind_spot_themes = [{"theme": str, "count": int}, ...]
    # No design_doc cache exists for this aggregate today — we scan tasks
    # directly. Mirroring the sr_catch_rate_trends precompute path is a
    # future optimization (see app/sr_blind_spots.py).
    if task_type in ("create", "fix", "refactor", "migrate") and project_for_playbook:
        try:
            themes = await aggregate_sr_blind_spot_themes(
                session,
                project_for_playbook.id,
                days=14,
                limit=8,
            )
            if themes:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["sr_blind_spot_themes"] = themes
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject sr_blind_spot_themes (non-critical)"
            )

    # Inject SR co-failure pairs (top 5 pairs, lift >= 1.5). When categories A and B
    # miss together at a significantly higher rate than by chance, fixing A requires
    # re-checking B. Shape: project_context.sr_co_failure_pairs =
    #   [{"cat_a": str, "cat_b": str, "lift": float}, ...]
    # Scans last 14 days of tasks; returns None when sample < min_sample threshold.
    if task_type in ("create", "fix", "refactor", "migrate") and project_for_playbook:
        try:
            high_iter_tasks = await session.scalars(
                select(Task)
                .where(
                    Task.project_id == project_for_playbook.id,
                    Task.status == TaskStatus.COMPLETED,
                    # Task has no `completed_at` column — that name is a property
                    # on the strategic-cycle model, not Task. For a COMPLETED
                    # task, created_at bounds the recent-window scan (mirrors
                    # sr_blind_spots._scan_fp_stats, the sibling 14-day SR scan).
                    Task.created_at > _utcnow() - timedelta(days=14),
                )
                .options(
                    load_only(Task.id, Task.context, Task.payloads, Task.created_at)
                )
                .order_by(Task.created_at.desc())
                .limit(2000)
            )
            pairs = aggregate_sr_co_failure_pairs(high_iter_tasks)
            if pairs:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["sr_co_failure_pairs"] = [
                    {"cat_a": p["cat_a"], "cat_b": p["cat_b"], "lift": p["lift"]}
                    for p in pairs
                ]
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject sr_co_failure_pairs (non-critical)"
            )

    # Inject persistent SR blind-spot hint for categories exceeding disagreement threshold
    # across >=2 consecutive 7-day windows. Shape: project_context.persistent_blind_spots
    if task_type in ("create", "fix", "refactor", "migrate") and project_for_playbook:
        try:
            from app.dispatcher_read_api import (
                get_sr_persistent_blind_spot_hint,
            )

            _pbs = await get_sr_persistent_blind_spot_hint(
                session, project_for_playbook.id
            )
            if _pbs:
                if "project_context" not in response:
                    response["project_context"] = {}
                response["project_context"]["persistent_blind_spots"] = _pbs
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject persistent_blind_spots (non-critical)"
            )

    # Inject concrete recent miss examples for the worst-performing SR categories.
    # Unlike sr_blind_spot_themes (aggregated keyword patterns), these are verbatim
    # reviewer findings that SR missed in the most recent window — giving the agent
    # specific "this exact thing slipped past last time" examples to act on.
    # Shape: project_context.sr_recent_miss_examples = {cat: [{severity, desc}, ...]}
    # Bounded: top 3 categories by weighted_miss_rate, ≤3 examples each, ≤1200 chars total.
    # When the task has a direction_id, prefer per-direction examples; fall back to global
    # when the direction has no qualifying data in by_direction.
    if task_type in ("create", "fix", "refactor", "migrate") and project_for_playbook:
        try:
            _miss_trends = (project_for_playbook.design_doc or {}).get(
                "sr_catch_rate_trends"
            )
            if _miss_trends and isinstance(_miss_trends.get("categories"), dict):
                # Resolve candidate categories: prefer direction-scoped when available.
                _direction_id = (task.context or {}).get("direction_id")
                _candidate_cats: dict | None = None
                if _direction_id:
                    _dir_entry = (_miss_trends.get("by_direction") or {}).get(
                        str(_direction_id)
                    ) or {}
                    _dir_cats = _dir_entry.get("categories")
                    if isinstance(_dir_cats, dict) and any(
                        isinstance(v, dict)
                        and isinstance(v.get("examples"), list)
                        and v["examples"]
                        for v in _dir_cats.values()
                    ):
                        _candidate_cats = _dir_cats
                if _candidate_cats is None:
                    _candidate_cats = _miss_trends["categories"]

                _ranked = sorted(
                    [
                        (cat, data)
                        for cat, data in _candidate_cats.items()
                        if isinstance(data, dict)
                        and isinstance(data.get("examples"), list)
                        and data["examples"]
                    ],
                    key=lambda x: (
                        x[1].get("weighted_miss_rate") or x[1].get("miss_rate") or 0.0
                    ),
                    reverse=True,
                )[:3]
                _miss_examples: dict = {}
                _total_chars = 0
                for _cat, _data in _ranked:
                    _cat_exs = []
                    for _ex in _data["examples"][:3]:
                        _desc = (str(_ex.get("desc") or ""))[:200]
                        if not _desc:
                            continue
                        if _total_chars + len(_desc) > 1200:
                            break
                        _total_chars += len(_desc)
                        _cat_exs.append(
                            {
                                "severity": _ex.get("severity", "MINOR"),
                                "desc": _desc,
                            }
                        )
                    if _cat_exs:
                        _miss_examples[_cat] = _cat_exs
                    if _total_chars >= 1200:
                        break
                if _miss_examples:
                    if "project_context" not in response:
                        response["project_context"] = {}
                    response["project_context"]["sr_recent_miss_examples"] = (
                        _miss_examples
                    )
        except Exception:
            logger.exception(
                "[AGENT] Failed to inject sr_recent_miss_examples (non-critical)"
            )

    # Inject scoped architecture context for create/fix/refactor tasks
    if task_type in ("create", "fix", "refactor") and project_for_playbook:
        design_doc = project_for_playbook.design_doc or {}
        file_to_system_raw = design_doc.get("_file_to_system", {})
        file_to_system = (
            file_to_system_raw if isinstance(file_to_system_raw, dict) else {}
        )
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
                    arch_ctx.append(
                        {
                            "name": sys_data.get("name", sys_name),
                            "status": str(sys_data.get("status", "")),
                            "ready_for_features": bool(
                                sys_data.get("ready_for_features", False)
                            ),
                            "description": desc[:200] if isinstance(desc, str) else "",
                        }
                    )
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
                        _arch_task_ctx["_shown_arch_system_names"] = list(
                            seen_systems.keys()
                        )
                        task.context = _arch_task_ctx
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                    except Exception:
                        logger.exception(
                            "[AGENT] Failed to track arch context injection (non-critical)"
                        )

    # Inject strategic vision so tactical agents understand the "why" behind
    # their work. Useful for every task type — the orchestrator's strategic
    # layer publishes this, but until now it was never read by implementers.
    if project_for_playbook:
        vision_summary = _build_vision_summary(project_for_playbook.vision)
        if vision_summary:
            response["vision"] = vision_summary

    # Inject direction narrative (established findings + open questions + last
    # cycle outcome) for task types that work inside a direction. Gives agents
    # a sense of trajectory rather than just a snapshot of recent tasks.
    if (
        direction_id
        and project_for_playbook
        and task_type
        in (
            "create",
            "fix",
            "refactor",
            "research",
            "plan",
        )
    ):
        _narrative_direction = next(
            (
                d
                for d in (project_for_playbook.directions or [])
                if d.get("id") == direction_id
            ),
            None,
        )
        if _narrative_direction:
            narrative = _build_direction_narrative(_narrative_direction)
            if narrative:
                if "direction_context" not in response:
                    response["direction_context"] = {}
                response["direction_context"]["narrative"] = narrative

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
    token: dict = Depends(verify_agent_token),
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

    return {"upload_url": url, "storage_uri": storage_uri}


class ViewportFailureScreenshotUrlRequest(BaseModel):
    task_id: uuid.UUID
    component: str = Field(max_length=200)
    width: int = Field(ge=1, le=4096)


@router.post("/viewport/failure-screenshot-upload-url")
@limiter.limit("120/minute")
async def get_viewport_failure_screenshot_upload_url(
    request: Request,
    req: ViewportFailureScreenshotUrlRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Generate a signed upload URL for a viewport failure screenshot at a stable
    per-project/component/width GCS path.

    The stable path (viewport-failures/{project_id}/{component}-{width}.png) lets the
    regression panel construct a download URL without a per-task DB lookup.  The GCS URI
    is also stamped onto task.context so the regression dispatcher can propagate it to the
    RegressionRemediationCase.failure_screenshot_url when that column is added.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    stmt = select(Task).where(Task.id == req.task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    import datetime as _dt
    import re as _re

    from google.cloud import storage as _gcs

    from app.storage import ARTIFACT_BUCKET_NAME, GCP_PROJECT_ID

    safe_component = _re.sub(r"[^A-Za-z0-9._-]", "_", req.component)[:100]
    object_name = (
        f"viewport-failures/{task.project_id}/{safe_component}-{req.width}.png"
    )
    storage_uri = f"gs://{ARTIFACT_BUCKET_NAME}/{object_name}"

    def _generate() -> str:
        blob = (
            _gcs.Client(project=GCP_PROJECT_ID)
            .bucket(ARTIFACT_BUCKET_NAME)
            .blob(object_name)
        )
        return blob.generate_signed_url(
            version="v4",
            expiration=_dt.timedelta(minutes=15),
            method="PUT",
            content_type="image/png",
        )

    try:
        upload_url = await asyncio.to_thread(_generate)
    except Exception as exc:
        logger.warning(
            "[VIEWPORT] signed upload URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(
            status_code=503, detail="Artifact storage unavailable"
        ) from exc

    ctx = dict(task.context or {})
    ctx["viewport_failure_screenshot_uri"] = storage_uri
    task.context = ctx
    flag_modified(task, "context")
    session.add(task)
    await session.commit()

    return {"upload_url": upload_url, "storage_uri": storage_uri}


# Beacon prefix → _cold_start_stages key mapping for per-stage breakdown.
# Prefixes mirror compute_startup_script.py beacon strings; checked on every
# heartbeat that carries a status_message matching one of these prefixes.
_COLD_START_BEACON_MAP: dict[str, str] = {
    "PROVISION_PROGRESS: creds fetched": "beacon_a_at",
    "PROVISION_PROGRESS: env file materialized": "beacon_b_at",
    "PROVISION_PROGRESS: payload ready": "beacon_c_at",
    "PROVISION_PROGRESS: reached systemctl phase": "beacon_d_at",
    "Startup script completed": "startup_complete_at",
}


@router.post("/heartbeat")
@limiter.limit("600/minute")
async def agent_heartbeat(
    request: Request,
    req: HeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Endpoint for agents to send a periodic heartbeat.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # Phase 2c Race A mitigation: every backend contact from a VM is a
    # liveness signal. Without this, the orchestrator's skip-provision
    # branch could clear a freshly-written warm pin before the agent's
    # first /vm-heartbeat call lands (~200-700 ms after spawn). Source the
    # vm_instance_id from the trusted JWT, never from the request body.
    _shared_state.record_vm_heartbeat(token.get("vm_instance_id"))

    # SKIP LOCKED: if the status endpoint holds FOR UPDATE on this row
    # (processing completion/pipeline), skip instead of blocking.  Blocking
    # would starve the heartbeat (agent's 30s HTTP timeout fires before the
    # 10s lock_timeout, but retries compound and the lock holder may take
    # longer than the timeout), causing last_heartbeat to go stale and the
    # reaper to kill the task.
    # Skipping one heartbeat is safe — the reaper's threshold is 5 minutes,
    # and the next heartbeat (30s later) will succeed once the lock releases.
    stmt = (
        select(Task)
        .where(
            Task.id == req.task_id, Task.vm_instance_id == token.get("vm_instance_id")
        )
        .with_for_update(skip_locked=True)
    )
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
        # Log interesting lifecycle beacons so they're visible in Cloud
        # Logging without needing to inspect the task row. Anything that
        # looks like a structured forensic signal (PROVISION_*, AGENT_*,
        # PRE_CLAIM_*, FATAL:, STARTUP_FAILED) gets echoed — a silent
        # pre-claim death should then show up as a specific beacon landing
        # instead of a log void.
        msg = req.status_message
        if any(
            msg.startswith(prefix)
            for prefix in (
                "PROVISION_",
                "AGENT_BOOTED",
                "AGENT_AUTH_OK",
                "AGENT_CLAIMING",
                "PRE_CLAIM_",
                "[FATAL",
                "STARTUP_FAILED",
            )
        ):
            logger.info(
                "[HEARTBEAT_BEACON] task=%s vm=%s msg=%s",
                req.task_id,
                token.get("vm_instance_id"),
                msg[:300],
            )
        # Stamp per-stage cold-start timestamps for breakdown telemetry.
        for _beacon_prefix, _stage_key in _COLD_START_BEACON_MAP.items():
            if msg.startswith(_beacon_prefix):
                _stgs = dict((task.context or {}).get("_cold_start_stages") or {})
                if _stage_key not in _stgs:
                    _stgs[_stage_key] = _utcnow().isoformat()
                    task.context = {**(task.context or {}), "_cold_start_stages": _stgs}
                break
    # Heartbeat counter (R4-W2 H1+H2+H3+M10): increment on EVERY heartbeat
    # (hoisted out of the metrics-conditional block — metrics-less heartbeats
    # were never advancing the counter, biasing the audit-trail gate). ORM
    # mutation replaces the prior Core ``UPDATE`` (1 round-trip vs 2; in-
    # memory value stays in sync with persisted value, fixing the
    # ``synchronize_session="auto"`` off-by-one).
    task.heartbeat_count = (task.heartbeat_count or 0) + 1
    hb_count = task.heartbeat_count  # already incremented in-memory

    if req.metrics or req.tool_trace is not None:
        # Phase B2 (audit 2026-04-29 / finding C3): hot fields (last_metrics,
        # last_tool_use_at) are dedicated columns — cheap in-place column
        # UPDATEs, no whole-blob TOAST page rewrites every 30s/VM. JSONB
        # mirror writes for these two fields were retired in Phase F (audit
        # 2026-04-29 round 2 / finding H8) so the column writes are now the
        # sole source of truth. Frontend (TaskDetailDrawerMeta) must read
        # the columns directly — reading task.context.last_metrics will go
        # stale.
        #
        # Audit-trail fields (metrics_history ring buffer, _tool_trace death-
        # trace window) are post-mortem-only, so we persist them every 5th
        # heartbeat (~2.5 min at 30s cadence) instead of every cycle. The
        # agent-side ring buffer is the in-flight source of truth.
        metrics_dump: dict | None = None
        if req.metrics:
            metrics_dump = req.metrics.model_dump(exclude_none=True)

        # Derive last_tool_use_at from the tail of the tool_trace ring buffer.
        # The agent stamps each entry's ``t`` as ISO-8601. Fall back to current
        # time if parsing fails so we never crash a heartbeat on a bad value.
        latest_tool_use_at: datetime | None = None
        if req.tool_trace:
            try:
                last_t = req.tool_trace[-1].t
                latest_tool_use_at = datetime.fromisoformat(
                    last_t.replace("Z", "+00:00")
                )
                # Strip tz so the column (naive DateTime, matching the rest of
                # the timestamp columns on Task) doesn't reject tz-aware values.
                if latest_tool_use_at.tzinfo is not None:
                    latest_tool_use_at = latest_tool_use_at.astimezone(
                        timezone.utc
                    ).replace(tzinfo=None)
            except (ValueError, AttributeError, IndexError):
                latest_tool_use_at = _utcnow()

        # Hot-path writes: dedicated columns. Cheap in-place column UPDATE.
        if metrics_dump is not None:
            task.last_metrics_json = metrics_dump
        if latest_tool_use_at is not None:
            task.last_tool_use_at = latest_tool_use_at

        # Every-5th-heartbeat gate for the audit-trail ring buffers. Round-1
        # used ``int(last_heartbeat.timestamp()) % 5`` which was a per-VM
        # lottery: at 30s cadence consecutive heartbeats from the same VM
        # hit the same modulo bucket (delta=30s, 30%5==0), so ~20% of VMs
        # always persisted and ~80% never persisted (audit C1).
        # The counter lives in a dedicated ``Task.heartbeat_count`` Integer
        # column (alembic 0066) and is bumped above (R4-W2) — the gate just
        # reads the in-memory value.
        persist_audit_trail = hb_count % 5 == 0

        if persist_audit_trail:
            ctx = dict(task.context or {})
            if metrics_dump is not None:
                history = list(ctx.get("metrics_history", []))
                entry: dict = {"t": _utcnow().isoformat()}
                for k in (
                    "rss_mb",
                    "heap_used_mb",
                    "disk_free_mb",
                    "disk_total_mb",
                    "cgroup_mb",
                    "cgroup_limit_mb",
                ):
                    if metrics_dump.get(k) is not None:
                        entry[k] = metrics_dump[k]
                history.append(entry)
                if len(history) > 30:
                    history = history[-30:]
                ctx["metrics_history"] = history
            if req.tool_trace is not None:
                # Overwrite — the agent's ring buffer already trims to the
                # latest window, so each heartbeat carries the authoritative
                # recent-tool-calls window. Last-successful value is the
                # death trace after reap.
                ctx["_tool_trace"] = [t.model_dump() for t in req.tool_trace]
            # Only assign on the 5th-cycle write — assigning on every
            # heartbeat would re-serialize the whole JSONB blob and defeat
            # the column-counter optimisation we just made.
            task.context = ctx

    if req.fix_loop_events:
        # Fold optional fix-loop telemetry events into Task.fix_loop_history.
        # Validation lives at the Pydantic boundary (FixLoopEventIn); invalid
        # entries are dropped silently so a malformed event from a forward-rolled
        # agent doesn't 422 the whole heartbeat.
        from app.fix_loop_telemetry import FixLoopEventIn, apply_fix_loop_events

        validated: list[FixLoopEventIn] = []
        for raw in req.fix_loop_events:
            try:
                validated.append(FixLoopEventIn(**raw))
            except Exception as exc:  # noqa: BLE001, PERF203
                logger.info(
                    "[FIX_LOOP] dropping invalid event for task %s: %s",
                    req.task_id,
                    exc,
                )
        if validated:
            apply_fix_loop_events(task, validated)

    # Persist prompt fingerprint on the dedicated column. Mirror to context only
    # when needed for older readers / backfills, so repeated heartbeats stay
    # idempotent and don't force unnecessary JSONB rewrites.
    if req.prompt_fingerprint:
        if task.prompt_fingerprint != req.prompt_fingerprint:
            task.prompt_fingerprint = req.prompt_fingerprint
        if (task.context or {}).get("prompt_fingerprint") != req.prompt_fingerprint:
            ctx = dict(task.context or {})
            ctx["prompt_fingerprint"] = req.prompt_fingerprint
            task.context = ctx

    # Per-task LLM telemetry (efficiency direction d-91d8dede). Cumulative
    # counters posted on every heartbeat — last write wins. Backwards-
    # compatible: older agents that don't ship these fields leave the column
    # untouched. Mirrors the every-heartbeat cadence of last_metrics_json so
    # downstream aggregations always see the freshest snapshot.
    if (
        req.llm_call_count is not None
        or req.total_input_tokens is not None
        or req.total_output_tokens is not None
    ):
        task.api_call_summary = {
            "llm_call_count": int(req.llm_call_count or 0),
            "total_input_tokens": int(req.total_input_tokens or 0),
            "total_output_tokens": int(req.total_output_tokens or 0),
            "last_updated_at": _utcnow().isoformat(),
        }

    # Record VM image identity so the orchestrator can detect stale Packer images.
    # Called on every heartbeat that carries image fields; no-ops when both are None.
    if req.vm_image_name is not None or req.vm_image_build_ts is not None:
        from app.compute import record_vm_image

        record_vm_image(
            token.get("vm_instance_id"), req.vm_image_name, req.vm_image_build_ts
        )

    session.add(task)
    await session.commit()

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Phase 2 (warm-VM continuity): /next-role + /vm-heartbeat
# ---------------------------------------------------------------------------


class NextRoleRequest(BaseModel):
    """Body for POST /api/agent/next-role.

    The agent on a warm VM polls this endpoint after a role completes to
    discover whether another QUEUED task has been pinned to the same VM.
    The JWT proves the agent owns the prior task; the body re-asserts the
    VM identity (defence in depth — JWT + body cross-check) and the prior
    PR number so the orchestrator can scope the lookup.
    """

    model_config = ConfigDict(extra="ignore")
    vm_instance_id: str = Field(..., min_length=1)
    prior_task_id: uuid.UUID
    prior_pr_number: int | None = None


class VmHeartbeatRequest(BaseModel):
    """Body for POST /api/agent/vm-heartbeat — VM-scoped liveness."""

    model_config = ConfigDict(extra="ignore")
    vm_instance_id: str = Field(..., min_length=1)
    # Same field names as HeartbeatRequest (schemas.py) so agent can use
    # one payload shape for both endpoints. Recorded into compute._vm_images
    # so the orchestrator can detect stale Packer images on warm VMs that
    # never run a task heartbeat.
    vm_image_name: str | None = Field(None, max_length=200)
    vm_image_build_ts: int | None = None


@router.post("/vm-heartbeat", status_code=204)
@limiter.limit("600/minute")
async def vm_heartbeat(
    request: Request,
    req: VmHeartbeatRequest,
    token: dict = Depends(verify_agent_token),
):
    """VM-scoped heartbeat used by warm VMs while polling /next-role.

    The reaper kills VMs whose task hasn't heartbeated, but during the
    next-role poll the prior task is already terminal, so the agent
    needs a separate liveness signal that targets the VM identity, not
    the task identity. The orchestrator's provisioning loop consults
    `_shared_state.vm_last_heartbeat` to decide whether to skip
    cold-provisioning a fresh VM.
    """
    if req.vm_instance_id != token.get("vm_instance_id"):
        raise HTTPException(status_code=403, detail="vm_instance_mismatch")
    _shared_state.record_vm_heartbeat(req.vm_instance_id)
    if req.vm_image_name is not None or req.vm_image_build_ts is not None:
        from app.compute import record_vm_image

        record_vm_image(req.vm_instance_id, req.vm_image_name, req.vm_image_build_ts)
    return Response(status_code=204)


async def _finalize_warm_claim(
    session: AsyncSession,
    task: Task,
    vm_instance_id: str,
    *,
    origin: str,
) -> dict:
    """Flip a locked QUEUED task to IN_PROGRESS for a warm VM and build the
    /next-role claim response.

    Shared by the chain-continuity (``origin="next_role_endpoint"``) and the
    backlog-reuse (``origin="backlog_reuse"``) paths so both stamp warm
    telemetry identically. Sets ``context._timing.warm_start`` and resets
    ``vm_provisioned_at`` to now so the task is counted as warm (and pays ~0
    cold-start) by the efficiency snapshot, mints a fresh task-scoped JWT,
    commits, and broadcasts the status change. On JWT-mint failure it rolls
    back (restoring QUEUED) and raises 500.
    """
    from app.compute import VM_PROVISIONING_MODEL

    task.vm_instance_id = vm_instance_id
    task.status = TaskStatus.IN_PROGRESS
    task.last_heartbeat = _utcnow()
    # vm_provisioned_at marks when the VM was made available to the task. For
    # warm-start we set it to "now" so claim_latency / reaper logic operates on
    # a fresh window — and so the subsequent idempotent /claim never stamps a
    # cold-start sample (the warm task stays vm_cold_start_ms=NULL).
    task.vm_provisioned_at = _utcnow()
    task.vm_spot = VM_PROVISIONING_MODEL == "SPOT"
    task.vm_warm_start = True
    if task.started_at is None:
        task.started_at = _utcnow()
    # Carry warm-start signal forward into context._timing. setdefault keeps
    # this server-side write idempotent; the agent's status update remains the
    # authoritative source and may overwrite warm_start. warm_claim_origin
    # distinguishes chain continuity from backlog reuse for post-mortem and
    # telemetry (efficiency direction d-91d8dede).
    ctx = dict(task.context or {})
    existing_timing = dict(ctx.get("_timing") or {})
    existing_timing.setdefault("warm_start", True)
    existing_timing["warm_claim_origin"] = origin
    existing_timing["prior_role_vm_id"] = vm_instance_id
    ctx["_timing"] = existing_timing
    task.context = ctx
    flag_modified(task, "context")
    # Mint the new JWT BEFORE committing the IN_PROGRESS transition. If mint
    # raises, the rollback restores QUEUED so a retry can succeed instead of
    # leaving the task stuck IN_PROGRESS with no live agent.
    task_type = ""
    try:
        payloads = task.payloads or []
        if payloads and isinstance(payloads[0], dict):
            task_type = str(payloads[0].get("type") or "")
    except Exception:  # noqa: BLE001 — task_type is best-effort metadata
        task_type = ""
    try:
        agent_token = create_agent_token(
            task_id=str(task.id),
            vm_instance_id=vm_instance_id,
            task_type=task_type,
            project_id=str(task.project_id) if task.project_id else "",
        )
    except Exception as exc:
        await session.rollback()
        logger.exception("[NEXT-ROLE] JWT mint failed; rolling back warm-claim")
        raise HTTPException(status_code=500, detail="token_mint_failed") from exc

    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Broadcast AFTER the commit so a rollback path doesn't fire a spurious
    # event.
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": TaskStatus.IN_PROGRESS},
    )
    return {
        "action": "claim",
        "task_id": str(task.id),
        "warm_start": True,
        "agent_token": agent_token,
    }


@router.post("/next-role")
@limiter.limit("600/minute")
async def next_role(
    request: Request,
    req: NextRoleRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Look up a warm-pinned QUEUED task for this VM and claim it.

    Phase 2b. Returns one of:
      - 200 ``{"action": "claim", "task_id": ..., "warm_start": True}`` —
        a QUEUED task whose context.warm_target_vm_id matches this VM
        was found and atomically transitioned to IN_PROGRESS.
      - 200 ``{"action": "exit", "reason": "another_vm_owns_this_pr"}`` —
        an IN_PROGRESS task for the same PR is pinned to a different
        VM; the agent should exit cleanly.
      - 204 — no warm-pinned task for this VM right now; the agent
        should keep polling until ``next_role_poll_timeout_secs``.

    JWT cross-check: the body's ``vm_instance_id`` MUST equal the JWT's
    ``vm_instance_id`` claim. Any mismatch is rejected 403.
    """
    if req.vm_instance_id != token.get("vm_instance_id"):
        raise HTTPException(status_code=403, detail="vm_instance_mismatch")

    # Liveness signal: receiving the request itself proves the VM is alive.
    _shared_state.record_vm_heartbeat(req.vm_instance_id)

    # Race guard: refuse warm-claim on a VM that was just scheduled for
    # orchestrator-initiated deletion. With fire_and_forget_vm_delete
    # (main.py — released the agent_completion lock window), there is now
    # a ~30-60s window where the VM is still alive in GCP but the delete
    # is in flight. Without this check, the warm-start claim races into
    # that window, gets the VM, and the pending delete kills the agent
    # mid-task with [FATAL:SIGTERM] (production 2026-05-05 task cfd73cec
    # on VM agent-vm-c44258...). The marker is recorded synchronously
    # before scheduling so it is always observable here.
    from app.main import is_orchestrator_initiated_delete

    _is_doomed, _delete_reason = is_orchestrator_initiated_delete(req.vm_instance_id)
    if _is_doomed:
        logger.info(
            f"[NEXT-ROLE] Refusing warm-claim on doomed VM {req.vm_instance_id} "
            f"(delete reason={_delete_reason})"
        )
        return Response(status_code=204)

    # Postgres can use the JSONB ``?`` operator (``has_key``) against
    # ``ix_task_context_gin``; SQLite test runs fall back to the portable
    # JSON-extract form. Computed once here because both the chain-continuity
    # lookup and the chain-fork exit below need it.
    use_jsonb_has_key = session.get_bind().dialect.name == "postgresql"

    # Circuit-breaker / feature-flag gate: if warm continuity is disabled or
    # auto-suppressed (pin-clear rate exceeded the threshold), skip the warm-
    # chain-continuity lookup so no new warm chains start. The orchestrator's
    # provisioning loop clears outstanding warm pins when the breaker is active;
    # skipping the lookup here closes the brief race window between the breaker
    # tripping and the next orchestrator cycle. This gate governs ONLY
    # chain-continuity claims — backlog-reuse (its own ``warm_backlog_reuse_enabled``
    # flag) and the chain-fork exit below still run.
    warm_continuity_active = (
        _shared_state.runtime_config.get("warm_continuity_enabled", True)
        and not _shared_state.is_warm_continuity_suppressed()
    )
    if warm_continuity_active:
        # Look up a QUEUED task pinned to this VM. Optional prior_pr_number
        # filter narrows further so a stray pin from an unrelated chain
        # doesn't get claimed.
        warm_vm_filters = [Task.status == TaskStatus.QUEUED]
        if use_jsonb_has_key:
            warm_vm_filters.append(Task.context.has_key("warm_target_vm_id"))
        warm_vm_filters.append(
            Task.context["warm_target_vm_id"].as_string() == req.vm_instance_id
        )
        stmt = select(Task).where(*warm_vm_filters)
        if req.prior_pr_number is not None:
            prior_pr_filters = []
            if use_jsonb_has_key:
                prior_pr_filters.append(Task.context.has_key("warm_target_pr"))
            prior_pr_filters.append(
                Task.context["warm_target_pr"].as_string() == str(req.prior_pr_number)
            )
            stmt = stmt.where(*prior_pr_filters)
        stmt = stmt.with_for_update(skip_locked=True)

        task = (await session.execute(stmt)).scalars().first()

        if task is not None:
            result = await _finalize_warm_claim(
                session, task, req.vm_instance_id, origin="next_role_endpoint"
            )
            logger.info(
                f"[NEXT-ROLE] Warm-start claim (chain): task={result['task_id']} "
                f"vm={req.vm_instance_id} prior_task={req.prior_task_id} "
                f"prior_pr={req.prior_pr_number}"
            )
            return result
    else:
        logger.info(
            "[NEXT-ROLE] Warm continuity disabled/suppressed (circuit breaker) — "
            "skipping chain-continuity claim for VM %s",
            req.vm_instance_id,
        )

    # Backlog-reuse branch (efficiency direction d-91d8dede). When the flag is
    # enabled an idle VM with no chain-pinned follow-on may claim the next
    # eligible QUEUED task from its project's general backlog and run it warm.
    # Attempted BEFORE the chain-fork exit below so a forked chain doesn't
    # strand an otherwise reusable VM. When the flag is OFF this whole block is
    # skipped and behavior is byte-for-byte the pre-existing chain-only path —
    # including, on no claim, the chain-fork exit / 204 so no idle VM is kept
    # alive when there is no genuine pending work to reuse it for.
    if _shared_state.runtime_config.get("warm_backlog_reuse_enabled", False):
        if _shared_state.is_warm_backlog_reuse_suppressed():
            from app.warm_backlog_reuse import (
                record_claim_skipped as _record_claim_skipped,
            )

            _record_claim_skipped("circuit_breaker_tripped")
        else:
            _project_claim = token.get("project_id")
            _project_uuid = None
            if _project_claim:
                try:
                    _project_uuid = uuid.UUID(str(_project_claim))
                except (ValueError, TypeError):
                    _project_uuid = None
            if _project_uuid is not None:
                from app.warm_backlog_reuse import (
                    record_next_role_poll_during_defer as _record_next_role_poll,
                    record_warm_claim_succeeded as _record_warm_claim_succeeded,
                    select_backlog_reuse_candidate,
                )

                # Record poll if we're inside a teardown-defer window the
                # agent_status completion handler already registered. Check
                # BEFORE refreshing so only pre-existing windows count.
                if _shared_state.is_backlog_first_refusal_active(str(_project_uuid)):
                    _record_next_role_poll()

                # Refresh the project's first-refusal window so the orchestrator
                # holds off cold-provisioning eligible backlog tasks while this VM
                # polls — biasing the race toward reuse. The SKIP-LOCKED claim
                # below still guarantees no double-provision if both race.
                _shared_state.register_backlog_first_refusal(str(_project_uuid))

                backlog_task = await select_backlog_reuse_candidate(
                    session, _project_uuid, req.vm_instance_id
                )
                if backlog_task is not None:
                    try:
                        result = await _finalize_warm_claim(
                            session,
                            backlog_task,
                            req.vm_instance_id,
                            origin="backlog_reuse",
                        )
                    except Exception:
                        _shared_state.record_backlog_reuse_failure("finalize_error")
                        raise
                    _record_warm_claim_succeeded()
                    logger.info(
                        f"[NEXT-ROLE] Warm-start claim (backlog_reuse): "
                        f"task={result['task_id']} vm={req.vm_instance_id} "
                        f"project={_project_uuid}"
                    )
                    return result
                # select_backlog_reuse_candidate records the skip reason
                # (no_eligible vs lost_claim_race) itself — it is the only
                # caller that can tell the two None cases apart.

    # No matching warm-pinned task. Check whether another VM owns an
    # IN_PROGRESS task for this PR — if so, the warm chain has already
    # forked and this VM should exit cleanly.
    if req.prior_pr_number is not None:
        other_pr_filters = [
            Task.status == TaskStatus.IN_PROGRESS,
            Task.context["warm_target_pr"].as_string() == str(req.prior_pr_number),
            Task.vm_instance_id.is_not(None),
            Task.vm_instance_id != req.vm_instance_id,
        ]
        if use_jsonb_has_key:
            other_pr_filters.insert(1, Task.context.has_key("warm_target_pr"))
        other_stmt = select(Task).where(*other_pr_filters)
        other = (await session.execute(other_stmt)).scalars().first()
        if other is not None:
            return {
                "action": "exit",
                "reason": "another_vm_owns_this_pr",
            }

    # Nothing to claim. Tell the agent to keep polling.
    return Response(status_code=204)


@router.post("/spawn")
@limiter.limit("150/minute")
async def spawn_subtask(
    request: Request,
    req: SpawnTaskRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
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
        raise HTTPException(
            status_code=400, detail=f"Spawn depth limit ({MAX_SPAWN_DEPTH}) exceeded"
        )

    # Plan-spawned children are created by _handle_plan_completion AFTER branch creation,
    # so they get the correct base_branch. Don't pre-spawn them here.
    if (req.context or {}).get("agent_spawned"):
        return {
            "message": "Plan children deferred to post-branch-creation",
            "task_ids": [],
            "requires_approval": False,
        }

    # Strip orchestrator-internal keys before merging into the new task's
    # context. An agent JWT-holder MUST NOT be able to forge a warm-VM pin
    # that hijacks another VM's chain. Log a warning when this happens so
    # we can spot the attack pattern in production.
    raw_context = req.context or {}
    forbidden_present = [
        k for k in raw_context if k in _AGENT_SPAWN_FORBIDDEN_CONTEXT_KEYS
    ]
    if forbidden_present:
        logger.warning(
            "[SPAWN] Stripped forbidden context keys from agent spawn: parent_task=%s keys=%s",
            parent_task.id,
            forbidden_present,
        )
    sanitized_context = {
        k: v
        for k, v in raw_context.items()
        if k not in _AGENT_SPAWN_FORBIDDEN_CONTEXT_KEYS
    }

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
            "parent_prompt": (
                parent_payload.get("prompt") or parent_payload.get("description") or ""
            )[:500],
        }
        new_task = Task(
            project_id=parent_task.project_id,
            title=payload.get("title", f"Subtask of {parent_task.title}"),
            description=payload.get("description", ""),
            payloads=[payload],
            context={
                **sanitized_context,
                "spawn_depth": spawn_depth + 1,
                "parent_context": parent_context,
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            # Fixed value — parent linkage lives on parent_task_id, no need
            # to encode parent UUID into the created_by string. The CHECK
            # constraint chk_task_created_by (alembic 0065) only allows the
            # canonical set, of which "AGENT" is the spawn-by-running-agent member.
            created_by="AGENT",
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
                "context": req.context,
            },
        )
        session.add(history)

    await session.commit()

    return {
        "message": f"Successfully spawned {len(new_tasks)} subtasks",
        "task_ids": [str(t.id) for t in new_tasks],
        "requires_approval": req.requires_approval,
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
    token: dict = Depends(verify_agent_token),
):
    """
    Agent endpoint to retrieve the GitHub token for cloning repositories.
    """
    if str(task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(task_id, session)
    return {
        "status": "success",
        "data": {"token": gh_token, "owner": owner, "repo": repo},
    }


@router.get("/github/check-runs")
@limiter.limit("200/minute")
async def agent_github_get_check_runs(
    request: Request,
    commit_sha: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Agent proxy endpoint to retrieve check-run results for a commit SHA.
    Returns only read-only check-run data; the raw GitHub token is never
    forwarded to the caller.
    """
    task_id_str = token.get("sub")
    if not task_id_str:
        raise HTTPException(status_code=403, detail="Invalid token: missing sub")
    task_id = uuid.UUID(task_id_str)
    if not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise HTTPException(status_code=422, detail="Invalid commit_sha")

    gh_token, owner, repo = await get_github_token_for_task(task_id, session)
    result = await get_check_runs(
        token=gh_token, owner=owner, repo=repo, ref=commit_sha
    )
    return {
        "status": "success",
        "data": {
            "status": result.get("status"),
            "conclusion": result.get("conclusion"),
            "details": result.get("details", []),
        },
    }


@router.post("/github/commit-status")
@limiter.limit("500/minute")
async def agent_github_commit_status(
    request: Request,
    req: GitHubCommitStatusRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
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
        target_url=req.target_url,
    )
    return {"status": "success", "data": result}


@router.post("/github/pr-comment")
@limiter.limit("200/minute")
async def agent_github_pr_comment(
    request: Request,
    req: GitHubPRCommentRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Agent endpoint to post a general comment to a PR.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_pr_comment(
        token=gh_token, owner=owner, repo=repo, pr_number=req.pr_number, body=req.body
    )
    return {"status": "success", "data": result}


@router.post("/github/pr-review-comment")
@limiter.limit("200/minute")
async def agent_github_pr_review_comment(
    request: Request,
    req: GitHubPRReviewCommentRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
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
        body=req.body,
    )
    return {"status": "success", "data": result}


@router.post("/github/pr-review")
@limiter.limit("150/minute")
async def agent_github_submit_pr_review(
    request: Request,
    req: GitHubSubmitPRReviewRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
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
    token: dict = Depends(verify_agent_token),
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
        assignees=req.assignees,
    )
    return {"status": "success", "data": result}


@router.get("/github/issues")
@limiter.limit("150/minute")
async def agent_github_get_issues(
    request: Request,
    req: GitHubGetIssuesRequest = Depends(),
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Agent endpoint to retrieve issues.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await get_issues(token=gh_token, owner=owner, repo=repo, state=req.state)
    return {"status": "success", "data": result}


@router.post("/github/branches")
@limiter.limit("100/minute")
async def agent_github_create_branch(
    request: Request,
    req: GitHubCreateBranchRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """
    Agent endpoint to create a new branch.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    gh_token, owner, repo = await get_github_token_for_task(req.task_id, session)
    result = await create_branch(
        token=gh_token, owner=owner, repo=repo, branch_name=req.branch_name, sha=req.sha
    )
    return {"status": "success", "data": result}


@router.post("/github/pull-requests")
@limiter.limit("100/minute")
async def agent_github_create_pr(
    request: Request,
    req: GitHubCreatePRRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
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
    existing_artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == req.task_id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing_artifact and existing_artifact.pr_url:
        try:
            pr_number = int(existing_artifact.pr_url.rstrip("/").split("/")[-1])
            pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            if pr_data.get("state") == "open":
                logger.info(
                    f"[PR] Task {req.task_id} already has open PR artifact: {existing_artifact.pr_url}"
                )
                return {
                    "status": "success",
                    "data": {"html_url": existing_artifact.pr_url},
                }
            logger.warning(
                f"[PR] Task {req.task_id} cached PR #{pr_number} is {pr_data.get('state')} "
                f"(merged={pr_data.get('merged')}) — recreating PR and replacing artifact"
            )
            await session.delete(existing_artifact)
            await session.flush()
        except HTTPException as exc:
            if exc.status_code in (404, 410):
                logger.warning(
                    f"[PR] Task {req.task_id} cached PR no longer exists ({exc.status_code}) — recreating"
                )
                await session.delete(existing_artifact)
                await session.flush()
            else:
                raise
        except (ValueError, IndexError):
            logger.warning(
                f"[PR] Could not parse PR number from cached URL {existing_artifact.pr_url} — recreating"
            )
            await session.delete(existing_artifact)
            await session.flush()

    result = await create_pull_request(
        token=gh_token,
        owner=owner,
        repo=repo,
        title=req.title,
        head=req.head,
        base=req.base,
        body=req.body,
    )

    # Save artifact immediately so the PR is linked even if the agent is reaped
    # before it can report COMPLETED status.  Without this, orphan recovery can't
    # find the PR and the pipeline stalls.
    pr_url = result.get("html_url", "")
    if pr_url:
        artifact = Artifact(
            task_id=req.task_id, artifact_type=ArtifactType.PULL_REQUEST, pr_url=pr_url
        )
        session.add(artifact)
        await session.commit()
        logger.info(f"[PR] Saved PR artifact for task {req.task_id}: {pr_url}")

    return {"status": "success", "data": result}


# ---------------------------------------------------------------------------
# Strategic-context fetch endpoints
# ---------------------------------------------------------------------------
# The strategic-spawn payload is now packed under a tight budget (see
# pack_under_budget in payload_schemas.py). When the packer trims a list or
# drops a section entirely, the agent sees that in payload._packer_meta and
# can call these endpoints to pull the full detail it needs. Deliberately
# scoped to read-only project state — mutations stay on the existing
# /api/agent/strategic POST surface.

_RESEARCH_HISTORY_KEEP_ON_FETCH = 20


async def _resolve_agent_project(
    project_id: uuid.UUID,
    token: dict,
    session: AsyncSession,
) -> Project:
    """Confirm the agent's task belongs to *project_id* and return the project.

    The agent JWT carries the task id (``sub`` claim) but not the project id,
    so we look up the task and verify the path matches. Cross-project access
    returns 404 (not 403) so probing for other projects' state can't be used
    to enumerate ids.
    """
    task_id = token.get("sub")
    if not task_id:
        raise HTTPException(status_code=401, detail="Missing task id in token")
    task = await session.get(Task, uuid.UUID(task_id))
    if task is None or task.project_id != project_id:
        raise HTTPException(status_code=404, detail="Project not found")
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}/directions/{direction_id}")
@limiter.limit("60/minute")
async def agent_fetch_direction(
    request: Request,
    project_id: uuid.UUID,
    direction_id: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return one direction by id, with research_history capped at the most
    recent 20 cycles. Used by the agent when ``directions_summary`` was
    list_trimmed and it needs deeper detail on a specific direction."""
    project = await _resolve_agent_project(project_id, token, session)
    direction = next(
        (d for d in (project.directions or []) if d.get("id") == direction_id),
        None,
    )
    if direction is None:
        raise HTTPException(status_code=404, detail="Direction not found")
    out = dict(direction)
    history = out.get("research_history")
    if isinstance(history, list) and len(history) > _RESEARCH_HISTORY_KEEP_ON_FETCH:
        out["research_history"] = history[-_RESEARCH_HISTORY_KEEP_ON_FETCH:]
        out["_history_truncated"] = {
            "shown": _RESEARCH_HISTORY_KEEP_ON_FETCH,
            "total": len(history),
        }
    return {"status": "success", "data": out}


@router.get("/projects/{project_id}/playbook/{key:path}")
@limiter.limit("60/minute")
async def agent_fetch_playbook_entry(
    request: Request,
    project_id: uuid.UUID,
    key: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return a single ``playbook/<key>`` entry from design_doc with full
    content. Used when ``playbook_summary`` was dropped from the strategic
    payload but the agent needs to consult a specific entry."""
    project = await _resolve_agent_project(project_id, token, session)
    my_task_id = token.get("sub") or ""
    design_doc = project.design_doc or {}
    full_key = f"playbook/{key}" if not key.startswith("playbook/") else key
    entry = design_doc.get(full_key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Playbook entry not found")
    # Enforce pending_source visibility: hide entries written by other in-flight
    # tasks (same rule as claim-time loader and /api/agent/playbook get/list).
    if isinstance(entry, dict):
        pending_source = entry.get("pending_source")
        if pending_source and pending_source != my_task_id:
            raise HTTPException(status_code=404, detail="Playbook entry not found")
    return {"status": "success", "data": {"key": full_key, "entry": entry}}


@router.get("/projects/{project_id}/gaps/{gap_id}")
@limiter.limit("60/minute")
async def agent_fetch_gap(
    request: Request,
    project_id: uuid.UUID,
    gap_id: str,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Return a single problem_model gap (active or archived) with full
    history/past_moves/evidence. Used when problem_model was summarized
    (per-gap arrays trimmed) and the agent needs the full lineage of a
    specific gap to reason about it."""
    project = await _resolve_agent_project(project_id, token, session)
    pm = project.problem_model or {}
    for source_key, archived_flag in (("gaps", False), ("archived_gaps", True)):
        for g in pm.get(source_key) or []:
            if isinstance(g, dict) and g.get("id") == gap_id:
                return {
                    "status": "success",
                    "data": g,
                    "from_archive": archived_flag,
                }
    raise HTTPException(status_code=404, detail="Gap not found")


class ReviewerMergeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    reviewer_findings: list[dict]
    reviewer_edges_considered: list[dict] = []


@router.post("/tasks/{original_task_id}/reviewer-merge")
@limiter.limit("30/minute")
async def merge_operator_reviewer_findings(
    request: Request,
    original_task_id: uuid.UUID,
    req: ReviewerMergeRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Merge operator-requested fresh-reviewer findings into an existing task's SR outcome."""
    requesting_task_id = token.get("sub")
    if not requesting_task_id:
        raise HTTPException(status_code=403, detail="No task in token")

    requesting_task = await session.get(Task, uuid.UUID(requesting_task_id))
    if not requesting_task:
        raise HTTPException(status_code=404, detail="Requesting task not found")

    ctx = requesting_task.context or {}
    if str(ctx.get("operator_requested_review_for")) != str(original_task_id):
        raise HTTPException(
            status_code=403, detail="Not authorized for this target task"
        )

    original_task = await session.get(Task, original_task_id)
    if not original_task:
        raise HTTPException(status_code=404, detail="Original task not found")

    orig_ctx = dict(original_task.context or {})
    sr = dict(orig_ctx.get("_self_review_outcome") or {})
    sr["reviewer_findings"] = req.reviewer_findings
    sr["reviewer_recovery_origin"] = "operator_requested_recovery"

    check_results = dict(sr.get("check_results") or {})
    for finding in req.reviewer_findings:
        cat = finding.get("category")
        if cat:
            existing = check_results.get(cat)
            if not existing or existing.get("passed") is True:
                check_results[cat] = {
                    "passed": False,
                    "issue": f"[reviewer] {finding.get('description', '')}",
                    "origin": "operator_requested_recovery",
                }
    sr["check_results"] = check_results

    if req.reviewer_edges_considered:
        sr["reviewer_edges_considered"] = req.reviewer_edges_considered

    orig_ctx["_self_review_outcome"] = sr
    original_task.context = orig_ctx
    flag_modified(original_task, "context")
    session.add(original_task)
    await session.commit()

    await broadcast_to_project(
        str(original_task.project_id),
        "task_updated",
        {"task_id": str(original_task_id)},
    )
    return {"status": "merged", "finding_count": len(req.reviewer_findings)}
