import copy
import os
import re
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Header, BackgroundTasks, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
import ipaddress
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
import uuid
import asyncio
import json
from datetime import datetime, timedelta

from app.database import init_db, get_session, AsyncSessionLocal, engine
from app.models import Project, Task, Artifact, TaskHistory, ProjectStatus, TaskStatus, ArtifactType, UserIntegration, MaxPlanAccount, DeploymentState, DeploymentStatus, Alert, AlertSeverity
from app.compute import provision_agent_vm, delete_agent_vm, cleanup_orphaned_vms, get_provision_error
from app.storage import generate_upload_signed_url, upload_json_payload, ARTIFACT_BUCKET_NAME
from app.secrets_manager import store_user_secret, get_user_secret
from app.auth import get_current_user, verify_agent_jwt, create_agent_token, create_ws_ticket, verify_ws_ticket
from app.telemetry import setup_telemetry
from app.admin import router as admin_router
from app.security import scan_pr_for_injection
from app.github import (
    validate_fine_grained_pat,
    generate_installation_access_token,
    GITHUB_WEBHOOK_SECRET,
    GITHUB_CLIENT_ID,
    exchange_oauth_code,
    get_user_installations,
    get_github_user,
    update_commit_status,
    create_pr_comment,
    create_pr_review_comment,
    create_issue,
    get_issues,
    create_branch,
    get_branch_sha,
    delete_branch,
    create_pull_request,
    merge_pull_request,
    get_pull_request,
    get_pr_files,
    get_check_runs,
    close_github_client,
)
import hmac
import hashlib
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

logger = logging.getLogger(__name__)

# Initialize the rate limiter
# In a multi-instance production environment (like Cloud Run),
# you would configure this to use Redis (e.g., storage_uri="redis://...")
# For now, we use the default in-memory storage.
limiter = Limiter(key_func=get_remote_address)

# --- Runtime Config & Autonomous State (shared with admin.py) ---
from app.shared_state import runtime_config as _runtime_config
import app.shared_state as _shared_state

# --- Task Priority Map ---
# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}

# --- Max Plan Account Pool ---
# In-memory cache of DB-persisted accounts. Keeps active_tasks count (runtime only).
# Each entry: {id, name, oauth_token, claude_config_json, enabled, active_tasks, total_tasks, token_created_at}
_max_plan_pool: List[Dict[str, Any]] = []

async def load_max_plan_pool():
    """Load Max plan accounts from database into in-memory pool on startup.
    Reconciles active_tasks from DB by counting tasks currently PROVISIONING/IN_PROGRESS."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(MaxPlanAccount))
        accounts = result.scalars().all()

        # Count actually active tasks per account to reconcile after restarts
        active_tasks = (await session.execute(
            select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
        )).scalars().all()
        active_per_account: Dict[str, int] = {}
        for t in active_tasks:
            acct_id = (t.context or {}).get("max_plan_account_id")
            if acct_id:
                active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1

        _max_plan_pool.clear()
        for acct in accounts:
            acct_id = str(acct.id)
            _max_plan_pool.append({
                "id": acct_id,
                "name": acct.name,
                "oauth_token": acct.oauth_token,
                "claude_config_json": acct.claude_config_json,
                "enabled": acct.enabled,
                "active_tasks": active_per_account.get(acct_id, 0),
                "total_tasks": acct.total_tasks,
                "token_created_at": acct.token_created_at.isoformat() if acct.token_created_at else None,
            })
        total_active = sum(active_per_account.values())
        logger.info(f"[STARTUP] Loaded {len(_max_plan_pool)} Max plan accounts ({total_active} active tasks reconciled)")

def select_max_plan_account() -> Optional[Dict[str, Any]]:
    """Select the Max plan account with the lowest active task count."""
    available = [a for a in _max_plan_pool if a.get("enabled", True)]
    if not available:
        return None
    return min(available, key=lambda a: a["active_tasks"])

def release_max_plan_account(account_id: str):
    """Decrement active tasks for an account when a task finishes."""
    for account in _max_plan_pool:
        if account["id"] == account_id:
            account["active_tasks"] = max(0, account["active_tasks"] - 1)
            break

def recheck_disabled_accounts():
    """Re-enable accounts that were disabled by rate limiting after a cooldown.
    Usage windows typically reset after some time — re-enable and let the next
    task attempt determine if usage has actually recovered."""
    now = datetime.utcnow()
    COOLDOWN_MINUTES = 30

    for acct in _max_plan_pool:
        if acct.get("enabled", True):
            continue
        disabled_at = acct.get("disabled_at")
        if disabled_at and (now - disabled_at).total_seconds() >= COOLDOWN_MINUTES * 60:
            acct["enabled"] = True
            acct["disabled_at"] = None
            logger.warning(f"[POOL] Re-enabled account {acct['name']} after {COOLDOWN_MINUTES}min cooldown")


# --- Deployment State Management ---

# Current revision from Cloud Run (set on startup)
_current_revision: str = os.getenv("K_REVISION", "local-dev")
_current_sha: str = os.getenv("DEPLOY_SHA", "unknown")


def classify_failure(task: Task, logs: Optional[str] = None) -> str:
    """Classify a task failure for deployment monitoring.
    Returns: CODE_ERROR, PROVISION_FAILURE, AGENT_TIMEOUT, EXTERNAL_API"""
    status_msg = (task.latest_status_message or "").lower()
    logs_lower = (logs or "").lower()
    combined = status_msg + " " + logs_lower

    # Provision failures — GCP quota, network, VM issues
    if any(kw in combined for kw in ["provision", "quota", "cpus_all_regions", "in_use_addresses",
                                      "vm failed to start", "failed to provision"]):
        return "PROVISION_FAILURE"

    # Rate limit — Claude API usage exhausted
    if any(kw in combined for kw in ["rate limit", "rate_limit", "429", "overloaded", "credit", "billing",
                                      "usage limit", "token limit exceeded"]):
        return "RATE_LIMIT"

    # External API failures — GitHub, Claude API outages
    if any(kw in combined for kw in ["api.github.com", "api.anthropic.com",
                                      "502 bad gateway", "503 service unavailable",
                                      "github api error", "claude api"]):
        return "EXTERNAL_API"

    # Agent timeout — reaped by heartbeat timeout
    if any(kw in combined for kw in ["no heartbeat", "reaped", "never started", "timed out"]):
        return "AGENT_TIMEOUT"

    # Default: code error
    return "CODE_ERROR"


_recorded_task_ids: set = set()  # Track which task IDs have already recorded results

def record_task_result(success: bool, failure_class: str = "", task_id: str = ""):
    """Record a task result for deployment monitoring. Deduplicates by task_id."""
    from app.shared_state import deploy_monitoring
    # Deduplicate: only record one result per task
    if task_id:
        if task_id in _recorded_task_ids:
            return
        _recorded_task_ids.add(task_id)
        # Keep the set bounded
        if len(_recorded_task_ids) > 200:
            _recorded_task_ids.clear()

    entry = (datetime.utcnow().isoformat(), "success" if success else "failure", failure_class)
    deploy_monitoring["task_results"].append(entry)
    # Keep only last 100 results
    if len(deploy_monitoring["task_results"]) > 100:
        deploy_monitoring["task_results"] = deploy_monitoring["task_results"][-100:]

    if success:
        _shared_state.deploy_monitoring["last_completion_time"] = datetime.utcnow()


async def _init_deployment_state():
    """Check/initialize deployment state on startup. Detects new deploys and rollbacks."""
    async with AsyncSessionLocal() as session:
        state = await session.get(DeploymentState, 1)

        if not state:
            # First boot — create the singleton row
            state = DeploymentState(
                id=1,
                current_revision=_current_revision,
                current_sha=_current_sha,
                deployed_at=datetime.utcnow(),
                status=DeploymentStatus.STABLE,
                last_known_good_revision=_current_revision,
                last_known_good_sha=_current_sha,
            )
            session.add(state)
            await session.commit()
            logger.info(f"[DEPLOY] First boot — initialized deployment state (revision={_current_revision})")
            return

        # Restore persisted runtime config values so settings survive restarts
        # Wrapped in try/except: if migration hasn't added these columns yet, skip gracefully
        try:
            if state.runtime_max_concurrent_vms is not None:
                _runtime_config["max_concurrent_vms"] = state.runtime_max_concurrent_vms
            if state.runtime_max_retries is not None:
                _runtime_config["max_retries"] = state.runtime_max_retries
            if state.runtime_max_autonomous_tasks_per_hour is not None:
                _runtime_config["max_autonomous_tasks_per_hour"] = state.runtime_max_autonomous_tasks_per_hour
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore runtime config (migration pending?): {e}", exc_info=True)
        try:
            if state.runtime_autonomous_research_interval_minutes is not None:
                _runtime_config["autonomous_research_interval_minutes"] = state.runtime_autonomous_research_interval_minutes
            if state.runtime_autonomous_research_model is not None:
                _runtime_config["autonomous_research_model"] = state.runtime_autonomous_research_model
            if state.runtime_autonomous_research_effort is not None:
                _runtime_config["autonomous_research_effort"] = state.runtime_autonomous_research_effort
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore autonomous research config (migration pending?): {e}", exc_info=True)
        # Respect paused/halted state — these override the persisted value
        if state.status in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
            _runtime_config["max_concurrent_vms"] = 0
            logger.info(f"[DEPLOY] max_concurrent_vms forced to 0 — state is {state.status} "
                  f"(reason: {state.pause_reason or 'unknown'}). "
                  f"Set max VMs > 0 in settings to resume.")

        if state.current_revision == _current_revision:
            # Same revision — normal restart (cold start, scale event, etc.)
            logger.info(f"[DEPLOY] Normal restart — same revision {_current_revision}, status={state.status}")
            return

        # Revision changed — is it a new deploy or a rollback?
        if _current_revision == state.last_known_good_revision and state.status == DeploymentStatus.MONITORING:
            # We're back on the last known good revision while we were monitoring a new deploy
            # This means the new deploy was rolled back (externally or via Cloud Run)
            state.status = DeploymentStatus.ROLLED_BACK
            state.rolled_back_from_sha = state.current_sha
            state.rollback_reason = "Revision reverted to last known good during monitoring window"
            state.rollback_count += 1
            state.current_revision = _current_revision
            state.current_sha = _current_sha
            state.deployed_at = datetime.utcnow()
            session.add(state)
            await session.commit()

            logger.warning(f"[DEPLOY] ROLLBACK DETECTED — reverted from {state.rolled_back_from_sha[:8]} to {_current_sha[:8]} "
                  f"(consecutive rollbacks: {state.rollback_count})")

            if state.rollback_count >= 3:
                state.status = DeploymentStatus.HALTED
                session.add(state)
                await session.commit()
                logger.error(f"[DEPLOY] HALTED — {state.rollback_count} consecutive rollbacks, manual intervention required")
                _runtime_config["max_concurrent_vms"] = 0
            return

        # New deploy — start monitoring window
        # Capture pre-deploy failure rate as baseline
        from app.shared_state import deploy_monitoring
        results = deploy_monitoring["task_results"]
        if results:
            failures = sum(1 for _, outcome, _ in results if outcome == "failure")
            deploy_monitoring["pre_deploy_failure_rate"] = failures / len(results)
        deploy_monitoring["task_results"] = []  # Reset for new deploy window
        deploy_monitoring["window_start"] = datetime.utcnow()

        state.current_revision = _current_revision
        state.current_sha = _current_sha
        state.deployed_at = datetime.utcnow()
        state.status = DeploymentStatus.MONITORING
        state.rolled_back_from_sha = None
        state.rollback_reason = None
        session.add(state)
        await session.commit()
        logger.info(f"[DEPLOY] New deploy detected — revision={_current_revision}, starting monitoring window")


def should_rollback() -> tuple:
    """Causation analysis: determine if failures are caused by the new deploy.
    Returns (should_rollback: bool, reason: str, response: str).
    Response is one of: ROLLBACK, PAUSE, SELF_HEAL"""
    from app.shared_state import deploy_monitoring, external_health

    results = deploy_monitoring["task_results"]
    if not results:
        return False, "No task results to analyze", "SELF_HEAL"

    # 1. Are we already on last_known_good? Rollback won't help.
    # (This is checked by the caller against DeploymentState)

    # 2. Were things already broken before this deploy?
    pre_rate = deploy_monitoring.get("pre_deploy_failure_rate", 0.0)
    if pre_rate > 0.3:
        return False, f"System was already failing before deploy (pre-deploy rate: {pre_rate:.0%})", "SELF_HEAL"

    # 3. Are external services down?
    failed_deps = external_health.get("failed_deps", [])
    critical_down = [d for d in failed_deps if d in ("db", "github_api", "gcp")]
    if critical_down:
        return False, f"External dependency down: {', '.join(critical_down)}", "PAUSE"

    # 4. Analyze post-deploy error types vs pre-deploy
    post_failures = [r for r in results if r[1] == "failure"]
    post_code_errors = [r for r in post_failures if r[2] == "CODE_ERROR"]
    post_failure_rate = len(post_failures) / len(results) if results else 0

    # If all failures are non-code (provision, external), don't rollback
    if post_failures and not post_code_errors:
        return False, "All failures are external (provision/API), not code errors", "PAUSE"

    # 5. Check if failure rate is significantly higher than pre-deploy
    if post_failure_rate <= pre_rate + 0.1:
        return False, f"Failure rate unchanged ({post_failure_rate:.0%} vs {pre_rate:.0%} pre-deploy)", "SELF_HEAL"

    # 6. High code error rate after deploy — likely caused by the deploy
    if len(post_code_errors) >= 3 and post_failure_rate > 0.5:
        return True, f"High code error rate after deploy: {post_failure_rate:.0%} ({len(post_code_errors)} code errors)", "ROLLBACK"

    # 7. 100% failure rate is a strong signal
    if post_failure_rate == 1.0 and len(results) >= 3:
        return True, f"100% failure rate across {len(results)} tasks after deploy", "ROLLBACK"

    # Not enough signal to attribute to deploy
    return False, f"Failure rate {post_failure_rate:.0%} — monitoring, not yet attributable", "SELF_HEAL"


async def _check_monitoring_window():
    """Called each orchestrator cycle. If monitoring window has elapsed without issues, mark STABLE."""
    from app.shared_state import deploy_monitoring
    window_start = deploy_monitoring.get("window_start")
    if not window_start:
        return

    duration = deploy_monitoring["extended_window"] if deploy_monitoring["is_extended"] else deploy_monitoring["window_duration"]
    elapsed = (datetime.utcnow() - window_start).total_seconds()

    if elapsed < duration:
        return  # Still within window

    # Window elapsed — check results
    results = deploy_monitoring["task_results"]
    if not results:
        # No tasks ran during window — if 2x the window has passed, mark stable (idle)
        if elapsed >= duration * 2:
            async with AsyncSessionLocal() as session:
                state = await session.get(DeploymentState, 1)
                if state and state.status == DeploymentStatus.MONITORING:
                    state.status = DeploymentStatus.STABLE
                    state.last_known_good_revision = state.current_revision
                    state.last_known_good_sha = state.current_sha
                    state.rollback_count = 0
                    session.add(state)
                    await session.commit()
                    logger.info("[DEPLOY] Monitoring idle timeout — no tasks ran, marking STABLE")
            deploy_monitoring["window_start"] = None
        return

    async with AsyncSessionLocal() as session:
        state = await session.get(DeploymentState, 1)
        if not state or state.status != DeploymentStatus.MONITORING:
            deploy_monitoring["window_start"] = None
            return

        # Run causation analysis
        do_rollback, reason, response = should_rollback()

        if do_rollback:
            if state.current_revision != state.last_known_good_revision:
                # Phase B: recommend rollback with reasoning (don't auto-trigger)
                state.rollback_reason = reason
                session.add(state)
                await session.commit()
                logger.warning(f"[DEPLOY] ROLLBACK RECOMMENDED: {reason}")
            else:
                # No rollback target available — stay in MONITORING, don't promote to STABLE
                state.rollback_reason = f"Issues detected but no rollback target: {reason}"
                session.add(state)
                await session.commit()
                logger.warning("[DEPLOY] Issues detected but no rollback target — staying in MONITORING")
                return  # Don't clear window_start so we keep monitoring
        elif response == "PAUSE":
            # External issue — handled by _check_pause_mode
            logger.info(f"[DEPLOY] External issue during monitoring: {reason}")
        else:
            # Passed monitoring window — mark stable
            state.status = DeploymentStatus.STABLE
            state.last_known_good_revision = state.current_revision
            state.last_known_good_sha = state.current_sha
            state.rollback_count = 0
            session.add(state)
            await session.commit()
            logger.info(f"[DEPLOY] Monitoring window passed — marked STABLE (revision={state.current_revision})")

    deploy_monitoring["window_start"] = None


async def _check_external_health():
    """Periodic check of external dependencies. Updates shared_state.external_health."""
    ext = _shared_state.external_health
    now = datetime.utcnow()

    # Check every 60 seconds (authenticated via App JWT = 5,000 req/hr)
    if ext["last_check"] and (now - ext["last_check"]).total_seconds() < 60:
        return
    ext["last_check"] = now
    failed = []

    # DB check — if we can run this function, DB is likely fine, but verify explicitly
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(select(Task).limit(1))
        ext["db"] = True
    except Exception as e:
        logger.warning(f"[HEALTH] DB health check failed: {e}", exc_info=True)
        ext["db"] = False
        failed.append("db")

    # GitHub API check — authenticated via App JWT (5,000 req/hr vs 60 unauthenticated)
    try:
        from app.github import _get_client, generate_github_app_jwt
        client = _get_client()
        jwt_token = generate_github_app_jwt()
        resp = await client.get("https://api.github.com/app", timeout=5.0,
                                headers={"Authorization": f"Bearer {jwt_token}"})
        ext["github_api"] = resp.status_code == 200
        if not ext["github_api"]:
            failed.append("github_api")
    except Exception as e:
        logger.warning(f"[HEALTH] GitHub API health check failed: {e}", exc_info=True)
        ext["github_api"] = False
        failed.append("github_api")

    # Claude API — check credentials + rate limit failures
    has_credentials = bool(_max_plan_pool) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_credentials:
        ext["claude_api"] = False
        failed.append("claude_api")
    else:
        # Check for rate limit burst — 3+ RATE_LIMIT failures in last 5 results means API is exhausted
        recent_results = _shared_state.deploy_monitoring["task_results"][-5:]
        rate_limit_count = sum(1 for r in recent_results if r[2] == "RATE_LIMIT")
        if rate_limit_count >= 3:
            ext["claude_api"] = False
            failed.append("claude_api")
            logger.warning(f"[HEALTH] Claude API rate limited — {rate_limit_count}/5 recent tasks hit limits")
        else:
            ext["claude_api"] = True

    # GCP — check is implicit via provisioning success/failure tracking
    # Mark unhealthy only if recent provisions all failed
    recent_provisions = [r for r in _shared_state.deploy_monitoring["task_results"]
                         if r[2] == "PROVISION_FAILURE"]
    if len(recent_provisions) >= 3:
        # Check if ALL recent failures are provision failures (guard against empty filter)
        recent_all = _shared_state.deploy_monitoring["task_results"][-5:]
        recent_failures = [r for r in recent_all if r[1] == "failure"]
        if recent_failures and all(r[2] == "PROVISION_FAILURE" for r in recent_failures):
            ext["gcp"] = False
            failed.append("gcp")
        else:
            ext["gcp"] = True
    else:
        ext["gcp"] = True

    ext["failed_deps"] = failed

    # Re-enable rate-limited accounts after cooldown
    recheck_disabled_accounts()

    if failed:
        logger.error(f"[HEALTH] External dependency check failed: {', '.join(failed)}")


_pause_failure_streak: int = 0  # consecutive health check cycles with critical failures
_health_recovery_streak: int = 0  # consecutive healthy cycles while PAUSED (gate for auto-resume)

async def _check_pause_mode():
    """Auto-pause when critical external deps fail, auto-resume when they recover."""
    global _pause_failure_streak, _health_recovery_streak
    ext = _shared_state.external_health
    failed = ext.get("failed_deps", [])
    # Auto-pause on critical deps including Claude API (rate limits, not just missing credentials)
    critical_failures = [d for d in failed if d in ("db", "github_api", "gcp", "claude_api")]

    if critical_failures:
        _health_recovery_streak = 0
        _pause_failure_streak += 1
        # Require 3 consecutive failures before pausing (avoid flaky single checks)
        if _pause_failure_streak < 3:
            logger.info(f"[PAUSE] Health check failure {_pause_failure_streak}/3: {', '.join(critical_failures)} — not pausing yet")
            return
        # Should we pause?
        if _runtime_config["max_concurrent_vms"] > 0:
            async with AsyncSessionLocal() as session:
                state = await session.get(DeploymentState, 1)
                if state and state.status not in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
                    state.status = DeploymentStatus.PAUSED
                    state.pause_reason = f"External dependency down: {', '.join(critical_failures)}"
                    state.paused_at = datetime.utcnow()
                    state.pre_pause_max_vms = _runtime_config["max_concurrent_vms"]
                    state.runtime_max_concurrent_vms = _runtime_config["max_concurrent_vms"]
                    session.add(state)
                    await session.commit()
                    # Only set to 0 after saving pre_pause value
                    _runtime_config["max_concurrent_vms"] = 0
            logger.warning(f"[PAUSE] Auto-paused pipeline — {', '.join(critical_failures)} down")
            await broadcast_deploy_state()
    else:
        _pause_failure_streak = 0
        # All deps healthy — should we auto-resume?
        async with AsyncSessionLocal() as session:
            state = await session.get(DeploymentState, 1)
            if state and state.status == DeploymentStatus.PAUSED and state.pre_pause_max_vms is not None:
                _health_recovery_streak += 1
                if _health_recovery_streak < 3:
                    logger.info(f"[PAUSE] Recovery streak {_health_recovery_streak}/3 — all deps healthy, waiting for confirmation")
                    return
                restore = state.pre_pause_max_vms
                state.status = DeploymentStatus.MONITORING if _shared_state.deploy_monitoring["window_start"] else DeploymentStatus.STABLE
                state.pause_reason = None
                state.paused_at = None
                state.pre_pause_max_vms = None
                state.runtime_max_concurrent_vms = restore
                session.add(state)
                await session.commit()
                _runtime_config["max_concurrent_vms"] = restore
                _health_recovery_streak = 0
                logger.info(f"[PAUSE] Auto-resumed pipeline — all deps healthy for 3 consecutive checks (max_vms={restore})")
                await broadcast_deploy_state()


async def _spawn_research_task(project: Project, session: AsyncSession):
    """Spawn a research task for a project if conditions are met."""
    # Guard: autonomous must be enabled
    if not project.autonomous_enabled:
        return
    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if breaker["state"] == "OPEN":
        # Check if cooldown has elapsed → transition to HALF-OPEN
        if breaker["opened_at"] and (datetime.utcnow() - breaker["opened_at"]).total_seconds() >= breaker["cooldown_seconds"]:
            breaker["state"] = "HALF_OPEN"
            logger.warning(f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)")
        else:
            return  # Still in cooldown
    # Guard: must have active directions that aren't in cooldown
    default_cooldown_hours = _runtime_config.get("autonomous_direction_cooldown_hours", 4)
    active_directions = [
        d for d in (project.directions or [])
        if d.get("status") == "active"
        and (
            not d.get("last_researched_at")
            or (datetime.utcnow() - datetime.fromisoformat(d["last_researched_at"])).total_seconds()
               >= default_cooldown_hours * 3600
        )
    ]
    if not active_directions:
        return
    # Guard: no existing research task already running/queued/evaluating
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]),
        )
    )).scalars().all()
    for t in active_tasks:
        if ((t.payloads or [{}])[0]).get("type") == "research":
            return
    # Guard: rate limit autonomous research spawns (not all orchestrator tasks)
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_research = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalars().all()
    recent_research_count = sum(1 for t in recent_research if ((t.payloads or [{}])[0]).get("type") == "research")
    if recent_research_count >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Infer base_branch from the most recent task in this project, or default to main
    recent_task = (await session.execute(
        select(Task).where(Task.project_id == project.id)
        .order_by(Task.created_at.desc()).limit(1)
    )).scalars().first()
    base_branch = ((recent_task.payloads or [{}])[0]).get("base_branch", "main") if recent_task else "main"

    # Use per-type defaults, fall back to autonomous-specific config
    research_model = _runtime_config.get("task_type_models", {}).get("research") or _runtime_config.get("autonomous_research_model") or None
    research_effort = _runtime_config.get("task_type_efforts", {}).get("research") or _runtime_config.get("autonomous_research_effort") or None
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")
    research_payload: dict = {
        "type": "research",
        "directions": active_directions,
        "base_branch": base_branch,
        **({"model": research_model} if research_model else {}),
        **({"effort": research_effort} if research_effort else {}),
        **({"prompt_suffix": research_suffix} if research_suffix else {}),
    }
    new_task = Task(
        project_id=project.id,
        title="Research: autonomous improvement scan",
        description="Automated research task triggered by autonomous loop",
        payloads=[research_payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "AUTONOMOUS_LOOP", "trigger": "post_merge_or_idle"}
    )
    session.add(history)
    await session.commit()
    await session.refresh(new_task)

    # Track this task for HALF_OPEN → CLOSED transition
    if breaker["state"] == "HALF_OPEN":
        breaker["half_open_task_id"] = str(new_task.id)

    logger.info(f"[AUTONOMOUS] Spawned research task {new_task.id} for project {project.id}")


async def _spawn_periodic_task(project: Project, task_type: str, session: AsyncSession):
    """Spawn a periodic non-research task (qa, security_audit, deps) if its schedule is due."""
    from sqlalchemy import func

    # Guard: circuit breaker must allow spawning
    breaker = _shared_state.get_circuit_breaker(str(project.id))
    if breaker["state"] == "OPEN":
        if breaker["opened_at"] and (datetime.utcnow() - breaker["opened_at"]).total_seconds() >= breaker["cooldown_seconds"]:
            breaker["state"] = "HALF_OPEN"
            logger.warning(f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)")
        else:
            return  # Still in cooldown

    periodic_cfg = _runtime_config.get("autonomous_periodic_tasks", {}).get(task_type, {})
    if not periodic_cfg.get("enabled", False):
        return

    interval_hours = periodic_cfg.get("interval_hours", 168)

    # Guard: no task of this type already QUEUED/PROVISIONING/IN_PROGRESS
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    for t in active_tasks:
        if ((t.payloads or [{}])[0]).get("type") == task_type:
            return

    # Guard: rate limit
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_auto = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.project_id == project.id,
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if recent_auto >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # Check when this type last ran (most recent completed task of this type)
    completed_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.COMPLETED,
        ).order_by(Task.created_at.desc()).limit(100)
    )).scalars().all()
    last_run = None
    for t in completed_tasks:
        if ((t.payloads or [{}])[0]).get("type") == task_type:
            last_run = t.created_at
            break

    now = datetime.utcnow()
    if last_run is not None:
        elapsed = (now - last_run).total_seconds() / 3600
        if elapsed < interval_hours:
            return

    # Infer base_branch from the most recent task in this project
    recent_task = (await session.execute(
        select(Task).where(Task.project_id == project.id)
        .order_by(Task.created_at.desc()).limit(1)
    )).scalars().first()
    base_branch = ((recent_task.payloads or [{}])[0]).get("base_branch", "main") if recent_task else "main"

    task_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    task_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None
    task_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get(task_type)
    payload: dict = {
        "type": task_type,
        "base_branch": base_branch,
        **({"model": task_model} if task_model else {}),
        **({"effort": task_effort} if task_effort else {}),
        **({"prompt_suffix": task_suffix} if task_suffix else {}),
    }
    new_task = Task(
        project_id=project.id,
        title=f"{task_type.upper()} scan: autonomous periodic",
        description=f"Automated {task_type} task triggered by autonomous periodic scheduler",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "AUTONOMOUS_LOOP", "trigger": "periodic_schedule", "task_type": task_type}
    )
    session.add(history)
    await session.commit()
    await session.refresh(new_task)

    if breaker["state"] == "HALF_OPEN":
        breaker["half_open_task_id"] = str(new_task.id)

    logger.info(f"[AUTONOMOUS] Spawned periodic {task_type} task {new_task.id} for project {project.id}")


async def _check_autonomous_research(owner: str, repo: str):
    """Post-merge hook: check if we should spawn a research task."""
    try:
        async with AsyncSessionLocal() as session:
            project = (await session.execute(
                select(Project).where(
                    Project.github_owner == owner,
                    Project.github_repo == repo,
                    Project.autonomous_enabled == True,
                )
            )).scalars().first()
            if not project:
                return

            # Spawn if there are available VM slots (don't wait for empty queue)
            from sqlalchemy import func
            active_count = (await session.execute(
                select(func.count()).select_from(Task).where(
                    Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                )
            )).scalar() or 0

            if active_count < _runtime_config["max_concurrent_vms"]:
                await _spawn_research_task(project, session)
    except Exception as e:
        logger.error(f"[AUTONOMOUS] Error checking research trigger for {owner}/{repo}: {e}", exc_info=True)


async def _check_idle_research():
    """Periodic check: spawn research if projects are idle with active directions."""
    now = datetime.utcnow()
    interval_minutes = _runtime_config.get("autonomous_research_interval_minutes", 30)
    if _shared_state.last_idle_check and (now - _shared_state.last_idle_check) < timedelta(minutes=interval_minutes):
        return
    _shared_state.last_idle_check = now

    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func

            projects = (await session.execute(
                select(Project).where(Project.autonomous_enabled == True)
            )).scalars().all()

            for project in projects:
                active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
                if not active_directions:
                    continue

                # Spawn if there are available VM slots (don't wait for empty queue)
                active_count = (await session.execute(
                    select(func.count()).select_from(Task).where(
                        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                    )
                )).scalar() or 0

                if active_count < _runtime_config["max_concurrent_vms"]:
                    await _spawn_research_task(project, session)

                    for task_type in ("security_audit", "qa", "deps"):
                        await _spawn_periodic_task(project, task_type, session)
    except Exception as e:
        logger.error(f"[AUTONOMOUS] Error in idle research check: {e}", exc_info=True)


def _get_websocket_client_ip(websocket: WebSocket) -> str:
    """Return the real client IP for a WebSocket connection.

    In Cloud Run, the GCP load balancer terminates external TLS and forwards
    traffic over an internal (RFC-1918) TCP connection.  We trust
    X-Forwarded-For only when the direct TCP peer is a private IP, because
    external attackers cannot forge their TCP source address.  If the direct
    connection is already from a public IP (e.g. local dev, direct access),
    we use it as-is and ignore X-Forwarded-For to prevent spoofing.
    """
    if not websocket.client:
        return "unknown"
    direct_ip = websocket.client.host
    try:
        is_private = ipaddress.ip_address(direct_ip).is_private
    except ValueError:
        return direct_ip
    if is_private:
        xff = websocket.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return direct_ip


# --- In-Memory Event Bus for WebSockets ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
        self.ip_connections: Dict[str, int] = {}
        self.MAX_CONNECTIONS_PER_IP = 20

    async def check_and_add_ip(self, websocket: WebSocket) -> bool:
        client_ip = _get_websocket_client_ip(websocket)
        if client_ip != "unknown":
            if self.ip_connections.get(client_ip, 0) >= self.MAX_CONNECTIONS_PER_IP:
                await websocket.close(code=1008, reason="Too many connections from this IP")
                return False
            self.ip_connections[client_ip] = self.ip_connections.get(client_ip, 0) + 1
        return True

    def remove_ip(self, websocket: WebSocket):
        client_ip = _get_websocket_client_ip(websocket)
        if client_ip != "unknown" and client_ip in self.ip_connections:
            self.ip_connections[client_ip] -= 1
            if self.ip_connections[client_ip] <= 0:
                del self.ip_connections[client_ip]

    async def connect(self, websocket: WebSocket, project_id: str) -> bool:
        if not await self.check_and_add_ip(websocket):
            return False

        if project_id not in self.active_connections:
            self.active_connections[project_id] = []
        self.active_connections[project_id].append(websocket)
        return True

    def disconnect(self, websocket: WebSocket, project_id: str):
        if project_id in self.active_connections:
            if websocket in self.active_connections[project_id]:
                self.active_connections[project_id].remove(websocket)
            if not self.active_connections[project_id]:
                del self.active_connections[project_id]
        
        self.remove_ip(websocket)

    async def broadcast(self, project_id: str, event_type: str, data: dict):
        if project_id in self.active_connections:
            message = {"event": event_type, "data": data}
            # Create a copy of the list to avoid RuntimeError if modified during iteration
            for connection in list(self.active_connections[project_id]):
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.warning(f"[WS] Failed to send message to client (project={project_id}, event={event_type}): {e}")
                    self.disconnect(connection, project_id)

    async def broadcast_to_all(self, event_type: str, data: dict):
        """Broadcasts a message to all connected WebSocket clients across all projects."""
        for project_id in list(self.active_connections.keys()):
            await self.broadcast(project_id, event_type, data)

manager = ConnectionManager()

async def broadcast_to_project(project_id: str, event_type: str, data: dict):
    """Broadcasts a message to all WebSocket clients listening to a specific project."""
    await manager.broadcast(project_id, event_type, data)

async def broadcast_deploy_state():
    """Assemble and broadcast current deploy state to all connected WebSocket clients."""
    try:
        from app.admin import build_deploy_state_payload
        async with AsyncSessionLocal() as session:
            payload = await build_deploy_state_payload(session)
        await manager.broadcast_to_all("deploy_state", payload)
    except Exception as e:
        logger.warning(f"[WS] Failed to broadcast deploy state: {e}")

def verify_agent_token(x_agent_token: str = Header(..., description="Secret token for agent authentication")):
    """
    Validates the short-lived JWT passed by the agent VM.
    """
    payload = verify_agent_jwt(x_agent_token)
    return payload

async def reaper_service():
    """
    Background worker that scans for orphaned VMs.
    Marks them as FAILED and issues a delete command to GCP if they haven't sent a heartbeat in 5 minutes.
    """
    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Step 1: Read active tasks WITHOUT locks (avoids blocking heartbeat writes)
                stmt = select(Task).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_tasks = (await session.execute(stmt)).scalars().all()

                now = datetime.utcnow()
                five_minutes_ago = now - timedelta(minutes=5)
                ten_minutes_ago = now - timedelta(minutes=10)

                orphaned_ids = []
                for task in active_tasks:
                    if not task.vm_instance_id:
                        continue
                    if task.last_heartbeat:
                        if task.last_heartbeat < five_minutes_ago:
                            orphaned_ids.append(task.id)
                    elif task.started_at and task.started_at < ten_minutes_ago:
                        orphaned_ids.append(task.id)
                    elif not task.started_at and task.created_at < ten_minutes_ago:
                        orphaned_ids.append(task.id)

                # Step 2: Lock ONLY orphaned rows (heartbeats on healthy tasks are never blocked)
                orphaned_tasks = []
                if orphaned_ids:
                    lock_stmt = select(Task).where(
                        Task.id.in_(orphaned_ids)
                    ).with_for_update(skip_locked=True)
                    orphaned_tasks = list((await session.execute(lock_stmt)).scalars().all())
                    # Re-verify after acquiring lock (heartbeat may have arrived between step 1 and 2)
                    verified = []
                    for t in orphaned_tasks:
                        if t.last_heartbeat:
                            if t.last_heartbeat < five_minutes_ago:
                                verified.append(t)
                        elif t.started_at:
                            if t.started_at < ten_minutes_ago:
                                verified.append(t)
                        elif t.created_at < ten_minutes_ago:
                            verified.append(t)
                    orphaned_tasks = verified

                for task in orphaned_tasks:
                    # Re-validate status before acting — task may have completed/been cancelled
                    # between the initial query and now
                    fresh_task = await session.get(Task, task.id)
                    if fresh_task is None or fresh_task.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
                        print(f"[REAPER] Skipping task {task.id} — status transitioned to {fresh_task.status if fresh_task else 'gone'} before reap")
                        continue

                    # Determine why the reaper flagged this task
                    if task.last_heartbeat:
                        age = int((now - task.last_heartbeat).total_seconds())
                        reap_reason = f"No heartbeat for {age}s (last: {task.last_heartbeat.isoformat()})"
                    elif task.started_at:
                        age = int((now - task.started_at).total_seconds())
                        reap_reason = f"No heartbeat received after {age}s since provisioning"
                    else:
                        age = int((now - task.created_at).total_seconds())
                        reap_reason = f"Never started — {age}s since creation, no heartbeat"

                    logger.info(f"[REAPER] Terminating orphaned task {task.id} (VM: {task.vm_instance_id}) — {reap_reason}")

                    if task.vm_instance_id:
                        asyncio.create_task(delete_agent_vm(task.vm_instance_id))

                    # Release Max Plan account slot
                    acct_id = (task.context or {}).get("max_plan_account_id")
                    if acct_id:
                        release_max_plan_account(acct_id)

                    if task.attempt_count < _runtime_config["max_retries"]:
                        # Check if this is a parent with children — re-evaluate instead of re-run
                        children = (await session.execute(
                            select(Task).where(Task.parent_task_id == task.id)
                        )).scalars().all()
                        if children:
                            logger.info(f"[REAPER] Re-evaluating parent task {task.id}")
                            await _requeue_parent_task(task, session)
                        else:
                            logger.info(f"[REAPER] Retrying task {task.id} (Attempt {task.attempt_count + 1}/3)")
                            task.latest_status_message = f"Reaped (attempt {task.attempt_count + 1}/{_runtime_config['max_retries']}): {reap_reason}"
                            task.attempt_count += 1
                            task.status = TaskStatus.QUEUED
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex[:16]}"
                            task.last_heartbeat = None
                            task.started_at = None  # Fresh timeout window for the new VM
                            session.add(task)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status, "attempt_count": task.attempt_count}
                        )
                    else:
                        logger.error(f"[REAPER] Task {task.id} failed after {_runtime_config['max_retries']} attempts.")
                        task.status = TaskStatus.FAILED
                        task.latest_status_message = f"Failed after {_runtime_config['max_retries']} attempts — last reap: {reap_reason}"
                        record_task_result(success=False, failure_class=classify_failure(task), task_id=str(task.id))
                        session.add(task)

                        # Broadcast failure to frontend
                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.FAILED}
                        )

                if orphaned_tasks:
                    await session.commit()

                    # Trigger rollup checks for failed tasks with parents (after commit)
                    for task in orphaned_tasks:
                        if task.status == TaskStatus.FAILED and task.parent_task_id:
                            try:
                                await _check_and_trigger_rollup(task, session)
                            except Exception as e:
                                logger.error(f"[REAPER] Rollup check failed for task {task.id}: {e}", exc_info=True)
                    
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[REAPER] Error: {e}", exc_info=True)
            
        # Sleep for 1 minute before checking again
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break

async def gcp_cleanup_service():
    """
    Background worker that runs every 15 minutes to ensure GCP VMs are in sync with the database.
    It deletes any VMs prefixed with 'agent-vm-' that do not have an active task.
    """
    while True:
        try:
            async with AsyncSessionLocal() as session:
                # Get all active tasks that should have a VM running
                stmt = select(Task.vm_instance_id).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
                active_vm_ids = (await session.execute(stmt)).scalars().all()
                active_vm_ids = [vid for vid in active_vm_ids if vid]
                
                # Call compute.py to clean up
                await cleanup_orphaned_vms(active_vm_ids)
                
        except asyncio.CancelledError:
            logger.info("[GCP CLEANUP] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[GCP CLEANUP] Error: {e}", exc_info=True)
            
        try:
            await asyncio.sleep(900) # Run every 15 minutes
        except asyncio.CancelledError:
            logger.info("[GCP CLEANUP] Service shutting down.")
            break

async def orchestrator_worker():
    """
    Background worker that polls for QUEUED tasks and provisions VMs.
    This ensures tasks are not lost if the server restarts before provisioning.
    """
    while True:
        queued_tasks = []
        try:
            global _orchestrator_last_cycle
            _orchestrator_last_cycle = datetime.utcnow()

            async with AsyncSessionLocal() as session:
                # Fetch all active tasks once — used for both concurrency limit and counter reconciliation
                active_tasks_result = (await session.execute(
                    select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                )).scalars().all()

                # Recover stale PROVISIONING tasks (orchestrator crashed mid-dispatch)
                two_min_ago = datetime.utcnow() - timedelta(minutes=2)
                for t in active_tasks_result:
                    if (t.status == TaskStatus.PROVISIONING
                        and t.last_heartbeat is None
                        and t.started_at and t.started_at < two_min_ago):
                        t.status = TaskStatus.QUEUED
                        t.started_at = None
                        t.vm_instance_id = None
                        session.add(t)
                        logger.warning(f"[ORCHESTRATOR] Recovered stale PROVISIONING task {t.id} → QUEUED")
                if session.dirty:
                    await session.commit()
                    # Re-fetch after recovery
                    active_tasks_result = (await session.execute(
                        select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                    )).scalars().all()

                # Enforce concurrency limit
                active_count = len(active_tasks_result)
                slots_available = max(0, _runtime_config["max_concurrent_vms"] - active_count)
                if slots_available == 0:
                    await asyncio.sleep(5)
                    continue

                # Reconcile Max Plan active_tasks counters from DB each cycle
                active_per_account: Dict[str, int] = {}
                for t in active_tasks_result:
                    acct_id = (t.context or {}).get("max_plan_account_id")
                    if acct_id:
                        active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1
                for acct in _max_plan_pool:
                    acct["active_tasks"] = active_per_account.get(acct["id"], 0)

                # Step 1: Find candidate tasks WITHOUT lock (read-only scan for dep filtering)
                candidate_stmt = select(Task).where(
                    Task.status == TaskStatus.QUEUED,
                    Task.requires_approval == False
                ).order_by(Task.priority.desc(), Task.created_at.asc()).limit(max(slots_available * 2, 50))  # Fetch extra to account for dep/conflict filtering
                candidate_tasks = (await session.execute(candidate_stmt)).scalars().all()

                # Step 2: Filter out tasks with unmet dependencies (no lock needed)
                eligible_ids = []
                for task in candidate_tasks:
                    deps = task.depends_on or []
                    if not deps:
                        eligible_ids.append(task.id)
                        continue

                    # Check all dependencies are COMPLETED with PR merged
                    try:
                        dep_uuids = [uuid.UUID(d) for d in deps]
                    except (ValueError, AttributeError) as e:
                        logger.error(f"[ORCHESTRATOR] Task {task.id} has invalid dependency UUID: {e}", exc_info=True)
                        eligible_ids.append(task.id)
                        continue
                    dep_result = await session.execute(select(Task).where(Task.id.in_(dep_uuids)))
                    dep_tasks = dep_result.scalars().all()

                    all_met = True
                    any_failed = False
                    for dt in dep_tasks:
                        if dt.status in [TaskStatus.FAILED, TaskStatus.CANCELLED]:
                            any_failed = True
                            break
                        if dt.status != TaskStatus.COMPLETED:
                            all_met = False
                            continue
                        # For create tasks, also require PR to be merged
                        dt_type = ((dt.payloads or [{}])[0]).get("type", "create")
                        if dt_type in ("create", "") and not await _verify_pr_merged(dt, session):
                            all_met = False

                    if any_failed:
                        # Use separate session to avoid polluting the main dispatch session
                        async with AsyncSessionLocal() as cancel_session:
                            cancel_task = await cancel_session.get(Task, task.id)
                            if cancel_task:
                                cancel_task.status = TaskStatus.CANCELLED
                                cancel_task.latest_status_message = "Cancelled: dependency task failed"
                                cancel_session.add(cancel_task)
                                await cancel_session.commit()
                        await broadcast_to_project(str(task.project_id), "task_updated",
                                                   {"task_id": str(task.id), "status": TaskStatus.CANCELLED})
                        logger.error(f"[ORCHESTRATOR] Cancelled task {task.id} — dependency failed")
                    elif all_met:
                        eligible_ids.append(task.id)

                # Step 2.5: File-conflict check — defer tasks that would conflict with in-progress work
                # Build a set of files currently being modified by IN_PROGRESS tasks
                in_progress_files: set[str] = set()
                for t in active_tasks_result:
                    if t.status == TaskStatus.IN_PROGRESS:
                        in_progress_files.update((t.context or {}).get("related_files", []))

                # Only apply conflict check if we have file data to work with
                if in_progress_files:
                    non_conflicting = []
                    for eid in eligible_ids:
                        candidate = next((t for t in candidate_tasks if t.id == eid), None)
                        if candidate is None:
                            non_conflicting.append(eid)
                            continue
                        candidate_files = set((candidate.context or {}).get("related_files", []))
                        if not candidate_files:
                            non_conflicting.append(eid)
                            continue
                        overlap = candidate_files & in_progress_files
                        if not overlap:
                            non_conflicting.append(eid)
                            continue
                        # Large scope tasks (refactors, etc.) defer on any overlap
                        scope = (candidate.context or {}).get("scope", "small")
                        task_type = ((candidate.payloads or [{}])[0]).get("type", "")
                        is_large = scope == "large" or task_type in ("refactor", "plan")
                        # Otherwise: defer if at least 2 files overlap AND >= 50% of candidate's files
                        should_defer = is_large or (len(overlap) >= 2 and len(overlap) >= len(candidate_files) * 0.5)
                        if should_defer:
                            logger.info(f"[DISPATCH] Deferring task {eid} — file conflict ({len(overlap)}/{len(candidate_files)} files, scope={scope}): {', '.join(sorted(overlap))}")
                        else:
                            non_conflicting.append(eid)
                    eligible_ids = non_conflicting

                # Respect slot limit after conflict filtering
                eligible_ids = eligible_ids[:slots_available]

                # Step 3: Lock ONLY the tasks we're about to dispatch (minimal lock duration)
                queued_tasks = []
                if eligible_ids:
                    lock_stmt = select(Task).where(
                        Task.id.in_(eligible_ids),
                        Task.status == TaskStatus.QUEUED  # Re-check status under lock
                    ).with_for_update(skip_locked=True)
                    queued_tasks = list((await session.execute(lock_stmt)).scalars().all())

                logger.info(f"[ORCHESTRATOR] Cycle: {len(queued_tasks)} queued, {active_count} active, {slots_available} slots, pool={len(_max_plan_pool)} accounts")

                for task in queued_tasks:
                    logger.info(f"[ORCHESTRATOR] Picking up queued task {task.id} (VM: {task.vm_instance_id})")

                    # Select Max plan account (primary) or fall back to API key
                    account = select_max_plan_account()
                    oauth_token = None
                    claude_config_json = None
                    account_id = None
                    if account:
                        account_id = account["id"]
                        oauth_token = account.get("oauth_token")
                        claude_config_json = account.get("claude_config_json")
                        logger.info(f"[ORCHESTRATOR] Using Max plan account '{account['name']}'")
                    else:
                        # No Max plan account — check for API key fallback
                        if os.getenv("ANTHROPIC_API_KEY"):
                            logger.info("[ORCHESTRATOR] No Max plan accounts, using ANTHROPIC_API_KEY fallback")
                        else:
                            # No credentials at all — skip this task, don't waste a VM
                            logger.warning(f"[ORCHESTRATOR] No credentials available — skipping task {task.id}")
                            async with AsyncSessionLocal() as msg_session:
                                msg_task = await msg_session.get(Task, task.id)
                                if msg_task:
                                    msg_task.latest_status_message = "Waiting for credentials — add a Max plan account or set ANTHROPIC_API_KEY"
                                    msg_session.add(msg_task)
                                    await msg_session.commit()
                            continue

                    # Store which account this task uses for cleanup on completion
                    task.context = {**(task.context or {}), "max_plan_account_id": account_id}

                    # Mark as PROVISIONING to prevent other workers from picking it up
                    task.status = TaskStatus.PROVISIONING
                    task.started_at = datetime.utcnow()
                    _shared_state.deploy_monitoring["last_dispatch_time"] = datetime.utcnow()
                    session.add(task)
                    try:
                        await session.commit()
                        # Increment account active_tasks so next iteration picks a different account
                        if account:
                            account["active_tasks"] = account.get("active_tasks", 0) + 1
                        logger.info(f"[ORCHESTRATOR] Task {task.id} committed as PROVISIONING — launching VM")
                    except Exception as e:
                        import traceback
                        logger.error(f"[ORCHESTRATOR] FAILED to commit PROVISIONING for task {task.id}: {e}", exc_info=True)
                        traceback.print_exc()
                        await session.rollback()
                        continue

                    # Trigger GCP Compute Engine provisioning in the background
                    # All values passed as args to avoid stale closure captures across loop iterations
                    async def _provision_and_handle(vm_id: str, t_id: uuid.UUID, p_id: uuid.UUID,
                                                    payloads: list, token: str, config_json: str,
                                                    acct_id_for_release: str):
                        payload_uri = None
                        if payloads:
                            try:
                                payload_uri = await upload_json_payload(str(t_id), payloads)
                            except Exception as e:
                                logger.error(f"[ORCHESTRATOR] Failed to upload payload for task {t_id}: {e}", exc_info=True)

                        success = await provision_agent_vm(vm_id, t_id, payload_uri,
                                                           oauth_token=token, claude_config_json=config_json)
                        if not success:
                            logger.error(f"[ORCHESTRATOR] Failed to provision VM {vm_id} for task {t_id}")
                            record_task_result(success=False, failure_class="PROVISION_FAILURE", task_id=str(t_id))
                            # Release the Max Plan account slot
                            if acct_id_for_release:
                                release_max_plan_account(acct_id_for_release)
                            # Mark as failed immediately to avoid 10 min wait
                            try:
                                async with AsyncSessionLocal() as inner_session:
                                    failed_task = await inner_session.get(Task, t_id)
                                    if failed_task:
                                        failed_task.status = TaskStatus.FAILED
                                        provision_err = get_provision_error(vm_id)
                                        failed_task.latest_status_message = f"VM provisioning failed ({vm_id}): {provision_err or 'unknown error'}"
                                        inner_session.add(failed_task)
                                        await inner_session.commit()
                                        await broadcast_to_project(
                                            str(p_id),
                                            "task_updated",
                                            {"task_id": str(t_id), "status": TaskStatus.FAILED}
                                        )
                            except Exception as e:
                                logger.error(f"[ORCHESTRATOR] Error marking task {t_id} as failed: {e}", exc_info=True)

                    asyncio.create_task(_provision_and_handle(
                        task.vm_instance_id, task.id, task.project_id,
                        task.payloads, oauth_token, claude_config_json, account_id
                    ))
                    
                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.PROVISIONING}
                    )

                # Check tasks waiting on CI (review approved, CI pending)
                CI_WAIT_TIMEOUT_MINUTES = 30
                ci_waiting_tasks = (await session.execute(
                    select(Task).where(Task.status == TaskStatus.EVALUATING)
                )).scalars().all()
                for ci_task in ci_waiting_tasks:
                    ci_loop = (ci_task.context or {}).get("review_loop", {})
                    if not ci_loop.get("ci_waiting"):
                        continue
                    ci_pr_number = ci_loop.get("pr_number")
                    if not ci_pr_number:
                        continue

                    # Timeout: if CI has been pending too long, proceed to merge anyway
                    ci_wait_started = ci_loop.get("ci_wait_started")
                    if ci_wait_started:
                        try:
                            started = datetime.fromisoformat(ci_wait_started)
                            if (datetime.utcnow() - started).total_seconds() > CI_WAIT_TIMEOUT_MINUTES * 60:
                                logger.error(f"[CI] PR #{ci_pr_number} CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min")
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = f"CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number} — manual inspection required"
                                session.add(ci_task)
                                await session.commit()
                                continue
                        except (ValueError, TypeError) as e:
                            logger.error(f"[CI] Malformed ci_wait_started for task {ci_task.id}: {ci_wait_started} ({e})", exc_info=True)

                    try:
                        ci_gh_token, ci_owner, ci_repo = await get_github_token_for_task(ci_task.id, session)

                        # Re-fetch current PR head to handle force-pushes
                        pr_data = await get_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
                        pr_state = pr_data.get("state", "open")

                        # Check if PR was already merged (prevents double merge)
                        if pr_state == "closed" or pr_data.get("merged"):
                            ci_task.status = TaskStatus.COMPLETED
                            ci_task.latest_status_message = f"PR #{ci_pr_number} already merged/closed"
                            session.add(ci_task)
                            await session.commit()
                            logger.info(f"[CI] PR #{ci_pr_number} already merged/closed, skipping")
                            continue

                        # Check for merge conflicts — CI won't trigger on conflicting PRs
                        if pr_data.get("mergeable") is False:
                            ci_payload = (ci_task.payloads or [{}])[0]
                            ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
                            ci_base = ci_loop.get("base_branch", "") or ci_payload.get("base_branch", "main")
                            ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get("original_task_id", "")
                            fix_prompt = (
                                f"PR #{ci_pr_number} has merge conflicts and CI cannot run.\n\n"
                                f"Rebase the branch onto {ci_base} and resolve all conflicts."
                            )
                            fix_task = await _spawn_pipeline_task(
                                ci_task, "fix", session, ci_pr_number, ci_branch, ci_base,
                                fix_prompt=fix_prompt, original_task_id=ci_orig_id
                            )
                            if fix_task:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
                                logger.info(f"[CI] PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}")
                            else:
                                # Fix not spawned — likely duplicate guard (fix already running) or iteration cap
                                ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts — fix already in progress or iteration limit reached"
                                logger.info(f"[CI] PR #{ci_pr_number} merge conflicts, fix not spawned — leaving in EVALUATING")
                            session.add(ci_task)
                            await session.commit()
                            continue

                        ci_head_sha = pr_data.get("head", {}).get("sha", "")
                        if not ci_head_sha:
                            ci_task.latest_status_message = f"CI waiting on PR #{ci_pr_number} — HEAD SHA unavailable"
                            session.add(ci_task)
                            await session.commit()
                            logger.info(f"[CI] PR #{ci_pr_number} has no HEAD SHA — skipping CI check this cycle")
                            continue

                        ci_result = await get_check_runs(ci_gh_token, ci_owner, ci_repo, ci_head_sha)
                        if ci_result["status"] != "completed":
                            details = [d["name"] for d in ci_result.get("details", []) if d.get("status") in ("queued", "in_progress")]
                            ci_task.latest_status_message = f"CI in progress on PR #{ci_pr_number}: {', '.join(details) or 'waiting for checks'}..."
                            session.add(ci_task)
                            await session.commit()
                            continue

                        ci_payload = (ci_task.payloads or [{}])[0]
                        ci_branch = ci_loop.get("branch", "") or ci_payload.get("branch", "")
                        ci_base = ci_loop.get("base_branch", "") or ci_payload.get("base_branch", "main")
                        ci_orig_id = ci_loop.get("original_task_id", "") or ci_payload.get("original_task_id", "")

                        if ci_result["conclusion"] == "success":
                            # CI passed — merge
                            merge_result = await merge_pull_request(ci_gh_token, ci_owner, ci_repo, ci_pr_number)
                            if merge_result.get("merged"):
                                await create_pr_comment(ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                    "**Blueprint CI + Review**: All checks passed, PR auto-merged.")
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI passed, PR #{ci_pr_number} merged"
                                if ci_orig_id:
                                    try:
                                        orig = await session.get(Task, uuid.UUID(ci_orig_id))
                                        if orig:
                                            orig.context = {**(orig.context or {}), "pr_merged": True}
                                            if orig.status == TaskStatus.EVALUATING and not (orig.context or {}).get("review_loop", {}).get("ci_waiting"):
                                                orig.status = TaskStatus.COMPLETED
                                                orig.latest_status_message = "Rollup PR merged"
                                            session.add(orig)
                                    except Exception as e:
                                        logger.warning(f"[CI] Failed to set pr_merged on {ci_orig_id}: {e}", exc_info=True)
                                logger.info(f"[CI] PR #{ci_pr_number} CI passed, merged")
                            else:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI passed but merge blocked: {merge_result.get('message', 'unknown')}"
                                logger.error(f"[CI] PR #{ci_pr_number} CI passed but merge blocked")
                        else:
                            # CI failed — check for existing active fix before spawning
                            active_tasks = (await session.execute(
                                select(Task).where(
                                    Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                                )
                            )).scalars().all()
                            existing_fix = next(
                                (t for t in active_tasks
                                 if (t.payloads or [{}])[0].get("type") == "fix"
                                 and (t.payloads or [{}])[0].get("pr_number") == ci_pr_number),
                                None
                            )
                            if existing_fix:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix already active → {existing_fix.id}"
                                logger.error(f"[CI] PR #{ci_pr_number} CI failed, fix already exists → {existing_fix.id}")
                                session.add(ci_task)
                                await session.commit()
                                continue
                            ci_errors = ci_result.get("failed_logs", "CI checks failed")
                            await create_pr_comment(ci_gh_token, ci_owner, ci_repo, ci_pr_number,
                                f"**Blueprint CI**: Checks failed.\n\n```\n{ci_errors[:2000]}\n```")
                            ci_fix_prompt = (
                                f"CI checks failed on PR #{ci_pr_number} (code review already approved).\n\n"
                                f"CI errors:\n{ci_errors}\n\n"
                                f"Fix only the build/lint/test errors — the review is already approved."
                            )
                            fix_task = await _spawn_pipeline_task(
                                ci_task, "fix", session, ci_pr_number, ci_branch, ci_base,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=ci_orig_id
                            )
                            if fix_task:
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, spawned fix → {fix_task.id}"
                                logger.error(f"[CI] PR #{ci_pr_number} CI failed, spawned fix → {fix_task.id}")
                            else:
                                ci_task.status = TaskStatus.FAILED
                                ci_task.latest_status_message = f"CI failed on PR #{ci_pr_number}, fix not spawned (iteration cap)"
                                logger.error(f"[CI] PR #{ci_pr_number} CI failed, fix spawn returned None")

                        session.add(ci_task)
                        await session.commit()
                        await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                   {"task_id": str(ci_task.id), "status": ci_task.status})
                    except Exception as e:
                        logger.error(f"[CI] Error checking CI for task {ci_task.id}: {e}", exc_info=True)
                        try:
                            ci_task.latest_status_message = f"CI check error: {type(e).__name__}: {e!r}"[:500]
                            session.add(ci_task)
                            await session.commit()
                        except Exception as e:
                            logger.error(f"[CI] Failed to update ci_task status after error for task {ci_task.id}: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.info("[ORCHESTRATOR] Worker shutting down.")
            break
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Error: {e}", exc_info=True)
            
        # Monitoring checks run every cycle (not just when idle)
        try:
            await _check_monitoring_window()
            await _check_external_health()
            await _check_pause_mode()
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Monitoring check error: {e}", exc_info=True)

        # Sleep based on whether we found tasks
        try:
            if queued_tasks:
                await asyncio.sleep(0.5) # Yield control briefly if we are processing a backlog
            else:
                await asyncio.sleep(5) # Sleep longer if queue is empty
                # Check for idle autonomous research when nothing is queued
                await _check_idle_research()
        except asyncio.CancelledError:
            logger.info("[ORCHESTRATOR] Worker shutting down.")
            break

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB tables
    await init_db()

    # Load persisted Max plan accounts into in-memory pool
    await load_max_plan_pool()

    # Check deployment state (detect new deploy, rollback, or normal restart)
    await _init_deployment_state()

    # Start the Reaper Service background task
    reaper_task = asyncio.create_task(reaper_service())
    
    # Start the GCP Cleanup Service background task
    gcp_cleanup_task = asyncio.create_task(gcp_cleanup_service())
    
    # Start the Orchestrator Worker background task
    orchestrator_task = asyncio.create_task(orchestrator_worker())

    # Store references for admin restart endpoint
    from app.shared_state import background_tasks as _bg_tasks
    _bg_tasks["orchestrator"] = orchestrator_task
    _bg_tasks["reaper"] = reaper_task
    _bg_tasks["gcp_cleanup"] = gcp_cleanup_task

    # Monitor background tasks for silent crashes
    async def _monitor_background_tasks():
        tasks = _bg_tasks
        while True:
            await asyncio.sleep(30)
            for name, t in tasks.items():
                if t.done():
                    exc = t.exception() if not t.cancelled() else None
                    logger.error(f"[MONITOR] Background task '{name}' DIED! Exception: {exc}")
                    # Restart the orchestrator if it died
                    if name == "orchestrator" and not t.cancelled():
                        logger.info("[MONITOR] Restarting orchestrator worker...")
                        tasks["orchestrator"] = asyncio.create_task(orchestrator_worker())
    monitor_task = asyncio.create_task(_monitor_background_tasks())

    yield
    # Shutdown: Clean up resources
    reaper_task.cancel()
    gcp_cleanup_task.cancel()
    orchestrator_task.cancel()
    monitor_task.cancel()
    await close_github_client()

app = FastAPI(
    title="Blueprint Orchestrator",
    description="Secure Orchestration & State Management",
    lifespan=lifespan
)

# Telemetry must be set up before the app starts (cannot add middleware during lifespan)
setup_telemetry(app, engine)

app.include_router(admin_router)

# Timestamp updated each orchestrator cycle — used by /debug endpoint
_orchestrator_last_cycle: Optional[datetime] = None

def _get_zone_health_summary() -> dict:
    """Get zone health summary from compute module, with fallback if not in GCP mode."""
    try:
        from app.compute import get_zone_health_summary
        return get_zone_health_summary()
    except Exception:
        return {"total_zones": 0, "available": 0, "exhausted": []}


@app.get("/api/debug/orchestrator")
async def debug_orchestrator(session: AsyncSession = Depends(get_session), current_user: dict = Depends(get_current_user)):
    """Diagnostic endpoint: check if the orchestrator loop is alive."""
    from sqlalchemy import func
    queued = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval == False)
    )).scalar() or 0
    active = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
    )).scalar() or 0
    ago = None
    if _orchestrator_last_cycle:
        ago = (datetime.utcnow() - _orchestrator_last_cycle).total_seconds()
    # Include deployment state
    deploy_state = None
    try:
        state = await session.get(DeploymentState, 1)
        if state:
            deploy_state = {
                "revision": state.current_revision,
                "sha": state.current_sha[:8] if state.current_sha else "",
                "status": state.status,
                "deployed_at": state.deployed_at.isoformat() if state.deployed_at else None,
                "last_known_good_sha": state.last_known_good_sha[:8] if state.last_known_good_sha else "",
                "rollback_count": state.rollback_count,
                "rollback_reason": state.rollback_reason,
                "pause_reason": state.pause_reason,
            }
    except Exception as e:
        logger.warning(f"[DEBUG] Failed to fetch deployment state for /api/debug/orchestrator: {e}", exc_info=True)

    return {
        "orchestrator_alive": _orchestrator_last_cycle is not None,
        "last_cycle_seconds_ago": ago,
        "queued_tasks": queued,
        "active_tasks": active,
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "pool_size": len(_max_plan_pool),
        "available_count": sum(1 for a in _max_plan_pool if a.get("enabled", True)),
        "disabled_count": sum(1 for a in _max_plan_pool if not a.get("enabled", True)),
        "pool_accounts": [
            {
                "name": a["name"],
                "enabled": a.get("enabled", True),
                "active": a["active_tasks"],
                "disabled_at": a["disabled_at"].isoformat() if a.get("disabled_at") else None,
                "reenable_at": (a["disabled_at"] + timedelta(minutes=30)).isoformat() if a.get("disabled_at") else None,
            }
            for a in _max_plan_pool
        ],
        "deploy_state": deploy_state,
        "deploy_monitoring": {
            "window_active": _shared_state.deploy_monitoring["window_start"] is not None,
            "task_results_count": len(_shared_state.deploy_monitoring["task_results"]),
            "last_dispatch": _shared_state.deploy_monitoring["last_dispatch_time"].isoformat() if _shared_state.deploy_monitoring["last_dispatch_time"] else None,
            "last_completion": _shared_state.deploy_monitoring["last_completion_time"].isoformat() if _shared_state.deploy_monitoring["last_completion_time"] else None,
            "external_health": _shared_state.external_health,
        },
        "zone_health": _get_zone_health_summary(),
    }


@app.get("/api/health/detailed")
async def health_detailed(session: AsyncSession = Depends(get_session), current_user: dict = Depends(get_current_user)):
    """Authenticated endpoint returning structured health status for all system components."""
    import time
    from sqlalchemy import text
    from sqlalchemy import func

    components: Dict[str, Any] = {}

    # --- DB ---
    db_status = "ok"
    db_latency_ms: Optional[float] = None
    try:
        t0 = time.monotonic()
        await session.execute(text("SELECT 1"))
        db_latency_ms = round((time.monotonic() - t0) * 1000, 2)
        if db_latency_ms > 500:
            db_status = "degraded"
    except Exception:
        db_status = "down"
    components["db"] = {"status": db_status, "latency_ms": db_latency_ms}

    # --- GitHub API ---
    gh_ok = _shared_state.external_health.get("github_api", True)
    last_check = _shared_state.external_health.get("last_check")
    components["github_api"] = {
        "status": "ok" if gh_ok else "down",
        "last_check": last_check.isoformat() if isinstance(last_check, datetime) else last_check,
    }

    # --- Orchestrator ---
    cycle_age: Optional[float] = None
    if _orchestrator_last_cycle:
        cycle_age = (datetime.utcnow() - _orchestrator_last_cycle).total_seconds()
    if cycle_age is None:
        orch_status = "down"
    elif cycle_age < 60:
        orch_status = "ok"
    elif cycle_age < 120:
        orch_status = "degraded"
    else:
        orch_status = "down"
    try:
        queued = (await session.execute(
            select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval == False)
        )).scalar() or 0
        active = (await session.execute(
            select(func.count()).select_from(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
        )).scalar() or 0
    except Exception:
        queued = active = 0
    components["orchestrator"] = {
        "status": orch_status,
        "cycle_age_seconds": cycle_age,
        "queued_tasks": queued,
        "active_tasks": active,
    }

    # --- Zone Health ---
    zone_summary = _get_zone_health_summary()
    total_zones = zone_summary.get("total_zones", 0)
    available_zones = zone_summary.get("available", 0)
    exhausted_zones = zone_summary.get("exhausted", [])
    if total_zones == 0 or available_zones > 0:
        zone_status = "ok"
    elif len(exhausted_zones) < total_zones:
        zone_status = "degraded"
    else:
        zone_status = "down"
    components["zone_health"] = dict(zone_summary, status=zone_status)

    # --- Max Plan Pool ---
    total_accounts = len(_max_plan_pool)
    available_accounts = len([a for a in _max_plan_pool if a.get("enabled", True)])
    if total_accounts == 0:
        pool_status = "ok"  # not using Max Plan accounts
    elif available_accounts > 0:
        pool_status = "ok"
    else:
        pool_status = "down"
    components["max_plan_pool"] = {"status": pool_status, "total": total_accounts, "available": available_accounts}

    # --- Circuit Breakers ---
    breaker_states: Dict[str, Any] = {}
    for project_id, breaker in _shared_state.circuit_breakers.items():
        breaker_states[project_id] = {
            "state": breaker.get("state", "CLOSED"),
            "failure_count": breaker.get("failure_count", 0),
            "opened_at": breaker["opened_at"].isoformat() if breaker.get("opened_at") else None,
        }
    components["circuit_breaker"] = {"status": "ok", "projects": breaker_states}

    # --- Autonomous ---
    autonomous_enabled = _runtime_config.get("max_concurrent_vms", 0) > 0
    consecutive_failures = sum(_shared_state.autonomous_failure_counts.values()) if _shared_state.autonomous_failure_counts else 0
    components["autonomous"] = {
        "status": "ok" if consecutive_failures < 3 else "degraded",
        "enabled": autonomous_enabled,
        "consecutive_failures": consecutive_failures,
    }

    # --- Overall Status ---
    statuses = [c["status"] for c in components.values() if isinstance(c, dict) and "status" in c]
    if "down" in statuses:
        overall = "unhealthy"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    return {"status": overall, "components": components}


@app.get("/health")
async def health_check():
    """Unauthenticated health endpoint for external monitoring (Cloud Function, load balancer)."""
    from app.shared_state import external_health

    orchestrator_alive = _orchestrator_last_cycle is not None
    cycle_age = None
    if _orchestrator_last_cycle:
        cycle_age = (datetime.utcnow() - _orchestrator_last_cycle).total_seconds()

    # Orchestrator is unhealthy if it hasn't cycled in 60+ seconds
    orchestrator_healthy = orchestrator_alive and (cycle_age is not None and cycle_age < 60)

    healthy = orchestrator_healthy and all(v for k, v in external_health.items() if k not in ("last_check", "failed_deps"))

    status_code = 200 if healthy else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if healthy else "unhealthy",
            "revision": _current_revision,
            "orchestrator_alive": orchestrator_alive,
            "orchestrator_cycle_age_seconds": cycle_age,
            "external_deps": {k: v for k, v in external_health.items() if k not in ("last_check", "failed_deps")},
        }
    )

# Attach the rate limiter to the app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": {"code": 422, "message": "Validation Error", "details": exc.errors()}},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": 500, "message": "Internal Server Error"}},
    )

# Add CORS Middleware so the frontend can call these APIs
# In production, set ALLOWED_ORIGINS to the specific frontend domain (e.g., "https://my-app.com")
# Multiple origins can be comma-separated.
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
if allowed_origins_env == "*":
    allowed_origins = ["*"]
elif allowed_origins_env:
    allowed_origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()]
else:
    allowed_origins = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"],
)


class DeployPayload(BaseModel):
    name: str
    payloads: List[Dict[str, Any]]
    github_owner: Optional[str] = Field(None, pattern="^[a-zA-Z0-9_.-]+$")
    github_repo: Optional[str] = Field(None, pattern="^[a-zA-Z0-9_.-]+$")

class AgentStatusPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    status: TaskStatus
    artifact_type: Optional[ArtifactType] = None
    commit_hash: Optional[str] = None
    repo_url: Optional[str] = None
    pr_url: Optional[str] = None
    storage_uri: Optional[str] = None
    logs: Optional[str] = Field(None, max_length=500000)
    verdict_json: Optional[str] = Field(None, max_length=10000)

class UploadUrlRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    filename: str

class HeartbeatMetrics(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rss_mb: Optional[float] = None
    heap_used_mb: Optional[float] = None
    disk_free_mb: Optional[float] = None
    disk_total_mb: Optional[float] = None

class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    status_message: Optional[str] = None
    metrics: Optional[HeartbeatMetrics] = None

class SpawnTaskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: uuid.UUID
    payloads: List[Dict[str, Any]] = Field(max_length=20)
    requires_approval: bool = False
    context: Optional[Dict[str, Any]] = None
    priority: Optional[int] = None

class GitHubCommitStatusRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    sha: str = Field(..., pattern="^[a-fA-F0-9]{40}$")
    state: str = Field(..., pattern="^(error|failure|pending|success)$")
    description: str = ""
    context: str = "blueprint-agent"
    target_url: str = ""

class GitHubPRCommentRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    pr_number: int
    body: str

class GitHubPRReviewCommentRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    pr_number: int
    commit_id: str = Field(..., pattern="^[a-fA-F0-9]{40}$")
    path: str
    line: int
    body: str

class GitHubCreateIssueRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    title: str
    body: str = ""
    assignees: Optional[List[str]] = None

class GitHubGetIssuesRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    state: str = Field("open", pattern="^(open|closed|all)$")

class GitHubCreateBranchRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    branch_name: str = Field(..., pattern="^[a-zA-Z0-9_./-]+$")
    sha: str = Field(..., pattern="^[a-fA-F0-9]{40}$")

class GitHubCreatePRRequest(BaseModel):
    task_id: uuid.UUID
    owner: str
    repo: str
    title: str
    head: str = Field(..., pattern="^[a-zA-Z0-9_./:-]+$")
    base: str = Field(..., pattern="^[a-zA-Z0-9_./:-]+$")
    body: str = ""

@app.get("/")
def read_root():
    return {"status": "online", "service": "Blueprint Orchestrator"}

@app.post("/api/deploy")
@limiter.limit("5/minute")
async def deploy_blueprint(
    request: Request,
    payload: DeployPayload, 
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    # 1. Create a new Project
    project = Project(
        name=payload.name, 
        user_id=current_user["user_id"],
        status=ProjectStatus.DEPLOYING,
        github_owner=payload.github_owner,
        github_repo=payload.github_repo
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    
    # 2. Create Tasks based on the flexible payloads
    created_tasks = []
    
    # Create a parent task for the entire deployment
    parent_task = Task(
        project_id=project.id,
        title=f"Deploy {payload.name}",
        description="Parent task orchestrating the deployment.",
        status=TaskStatus.IN_PROGRESS,
        created_by="USER"
    )
    session.add(parent_task)
    await session.commit()
    await session.refresh(parent_task)
    
    for p in payload.payloads:
        # GCP instance names must match a specific regex (lowercase, hyphens, numbers)
        vm_name = f"agent-vm-{uuid.uuid4().hex[:16]}"
        
        task = Task(
            project_id=project.id,
            title=p.get("title", "Untitled Subtask"),
            description=p.get("description", ""),
            parent_task_id=parent_task.id,
            payloads=[p],
            vm_instance_id=vm_name,
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR"
        )
        session.add(task)
        created_tasks.append(task)
        
    await session.commit()
    
    # 3. The Orchestrator Worker will automatically pick up QUEUED tasks
    # and provision the VMs in the background.
    
    return {
        "status": "accepted",
        "project_id": project.id,
        "message": f"Project created. {len(created_tasks)} tasks queued for provisioning."
    }

# --- Frontend Read APIs ---

@app.get("/api/projects")
@limiter.limit("30/minute")
async def list_projects(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[ProjectStatus] = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    List all projects for the authenticated user.
    """
    stmt = select(Project).where(Project.user_id == current_user["user_id"])
    if status:
        stmt = stmt.where(Project.status == status)
        
    stmt = stmt.order_by(Project.created_at.desc()).offset(offset).limit(limit)
    projects = (await session.execute(stmt)).scalars().all()
    return {
        "projects": projects,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "count": len(projects)
        }
    }

@app.get("/api/projects/{project_id}")
@limiter.limit("60/minute")
async def get_project(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Get a specific project's details.
    """
    stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(stmt)).scalars().first()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    return project

@app.get("/api/projects/{project_id}/tasks")
@limiter.limit("60/minute")
async def get_project_tasks(
    request: Request,
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[TaskStatus] = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Get all tasks and their artifacts for a specific project.
    """
    # Verify project exists and belongs to user
    project_stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(project_stmt)).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    # Fetch tasks
    tasks_stmt = select(Task).where(Task.project_id == project_id)
    if status:
        tasks_stmt = tasks_stmt.where(Task.status == status)
        
    tasks_stmt = tasks_stmt.order_by(Task.created_at.asc()).offset(offset).limit(limit)
    tasks = (await session.execute(tasks_stmt)).scalars().all()
    
    # Fetch artifacts for these tasks
    task_ids = [t.id for t in tasks]
    artifacts = []
    if task_ids:
        artifacts_stmt = select(Artifact).where(Artifact.task_id.in_(task_ids))
        artifacts = (await session.execute(artifacts_stmt)).scalars().all()
        
    # Group artifacts by task_id
    artifacts_by_task = {}
    research_map = {}
    for a in artifacts:
        if a.task_id not in artifacts_by_task:
            artifacts_by_task[a.task_id] = []
        artifacts_by_task[a.task_id].append(a)
        if a.artifact_type == ArtifactType.RESEARCH_FINDINGS and a.logs:
            try:
                parsed = json.loads(a.logs[:100000])
                research_map[a.task_id] = parsed if isinstance(parsed, list) else parsed.get('logs', [])
            except (json.JSONDecodeError, AttributeError):
                pass

    # Combine
    result = []
    for t in tasks:
        task_dict = {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "task_type": ((t.payloads or [{}])[0]).get("type", "create"),
            "vm_instance_id": t.vm_instance_id,
            "payloads": t.payloads,
            "created_at": t.created_at,
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
            "context": {k: v for k, v in (t.context or {}).items() if not k.startswith("max_plan_")},
            "artifacts": artifacts_by_task.get(t.id, []),
            "research_findings": research_map.get(t.id),
        }
        result.append(task_dict)
        
    return {
        "tasks": result,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "count": len(result)
        }
    }

@app.post("/api/projects/{project_id}/ws-ticket")
@limiter.limit("30/minute")
async def generate_ws_ticket(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Generates a short-lived ticket for authenticating WebSocket connections.
    """
    # Admins can access any project's WebSocket
    if current_user.get("role") == "admin":
        stmt = select(Project).where(Project.id == project_id)
    else:
        stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(stmt)).scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    ticket = create_ws_ticket(current_user["user_id"], str(project_id))
    return {"ticket": ticket}

@app.websocket("/api/projects/{project_id}/ws")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    """
    WebSocket endpoint for real-time updates.
    The frontend connects to this to receive live status changes and logs.
    """
    await websocket.accept()
    
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        ticket = auth_message.get("ticket")
        if not ticket:
            await websocket.close(code=1008, reason="Missing ticket")
            return
            
        payload = verify_ws_ticket(ticket)
        if payload.get("project_id") != project_id:
            await websocket.close(code=1008, reason="Project ID mismatch")
            return
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="Authentication timeout")
        return
    except Exception:
        await websocket.close(code=1008, reason="Authentication failed")
        return

    if not await manager.connect(websocket, project_id):
        return
    try:
        await websocket.send_json({"event": "connected", "data": {"project_id": project_id}})
        # Send current deploy state immediately so the client hydrates without waiting for polling
        try:
            from app.admin import build_deploy_state_payload
            async with AsyncSessionLocal() as ds_session:
                deploy_payload = await build_deploy_state_payload(ds_session)
            await websocket.send_json({"event": "deploy_state", "data": deploy_payload})
        except Exception as e:
            logger.warning(f"[WS] Failed to send initial deploy state on connect: {e}")
        import time
        last_message_time = time.time()
        message_count = 0
        while True:
            # Keep the connection open and listen for any messages from the client
            data = await websocket.receive_text()
            if len(data) > 10000: # 10KB limit
                await websocket.close(code=1009, reason="Message too big")
                return
            
            # Simple rate limiting: 10 messages per second
            current_time = time.time()
            if current_time - last_message_time < 1.0:
                message_count += 1
                if message_count > 10:
                    await websocket.close(code=1008, reason="Rate limit exceeded")
                    return
            else:
                last_message_time = current_time
                message_count = 1
            # We can handle client-to-server messages here if needed
    except WebSocketDisconnect:
        manager.disconnect(websocket, project_id)

# --- Agent APIs ---

@app.websocket("/api/agent/ws/{task_id}")
async def agent_websocket_endpoint(websocket: WebSocket, task_id: uuid.UUID):
    """
    WebSocket endpoint for agents to stream high-throughput logs and real-time events.
    """
    await websocket.accept()
    
    # Require the client to send the token as the first message
    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        token = auth_message.get("token")
        if not token:
            await websocket.close(code=1008, reason="Missing token")
            return
            
        payload = verify_agent_jwt(token)
        if payload.get("sub") != str(task_id):
            await websocket.close(code=1008, reason="Task ID mismatch")
            return
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="Authentication timeout")
        return
    except Exception:
        await websocket.close(code=1008, reason="Authentication failed")
        return

    # Get project_id for this task
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(Task.id == task_id)
        task = (await session.execute(stmt)).scalars().first()
        if not task:
            await websocket.close(code=1008)
            return
        project_id = str(task.project_id)

    if not await manager.check_and_add_ip(websocket):
        return

    try:
        import time
        last_message_time = time.time()
        message_count = 0
        while True:
            text_data = await websocket.receive_text()
            if len(text_data) > 100000: # 100KB limit for agent logs
                await websocket.close(code=1009, reason="Message too big")
                return
            
            # Simple rate limiting: 20 messages per second
            current_time = time.time()
            if current_time - last_message_time < 1.0:
                message_count += 1
                if message_count > 20:
                    await websocket.close(code=1008, reason="Rate limit exceeded")
                    return
            else:
                last_message_time = current_time
                message_count = 1

            try:
                data = json.loads(text_data)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "log":
                await broadcast_to_project(project_id, "agent_log", {
                    "task_id": str(task_id),
                    "log": data.get("content")
                })
            elif data.get("type") == "status":
                # Optionally handle status updates via WS too
                pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove_ip(websocket)

@app.post("/api/agent/refresh-token")
async def agent_refresh_token(token: dict = Depends(verify_agent_token)):
    """
    Accepts a valid (non-expired) agent JWT and returns a new one with a fresh 8-hour TTL.
    """
    new_token = create_agent_token(token["sub"], token["vm_instance_id"])
    return {"token": new_token}


@app.post("/api/agent/claim")
async def claim_task(
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    stmt = select(Task).where(
        Task.vm_instance_id == token.get("vm_instance_id"),
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING])
    )
    result = await session.execute(stmt)
    task = result.scalars().first()
    
    if not task:
        raise HTTPException(status_code=404, detail="No queued task found for this VM instance.")
        
    task.status = TaskStatus.IN_PROGRESS
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

    task_type = (task.payloads or [{}])[0].get("type", "")
    if task_type in ("research", "plan", "create"):
        # Active tasks (IN_PROGRESS or EVALUATING) in the same project
        active_stmt = select(Task).where(
            Task.project_id == task.project_id,
            Task.id != task.id,
            Task.status.in_([TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING])
        )
        active_tasks_rows = (await session.execute(active_stmt)).scalars().all()
        active_tasks = []
        for t in active_tasks_rows:
            p = (t.payloads or [{}])[0]
            active_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "task_type": p.get("type", ""),
                "status": t.status,
                "prompt": p.get("prompt", p.get("description", "")),
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
            p = (t.payloads or [{}])[0]
            queued_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "task_type": p.get("type", ""),
                "prompt": p.get("prompt", p.get("description", "")),
            })

        # Recently completed tasks (10 most recent)
        completed_stmt = select(Task).where(
            Task.project_id == task.project_id,
            Task.id != task.id,
            Task.status == TaskStatus.COMPLETED
        ).order_by(Task.created_at.desc()).limit(10)
        completed_tasks_rows = (await session.execute(completed_stmt)).scalars().all()

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

        recently_completed_tasks = []
        for t in completed_tasks_rows:
            p = (t.payloads or [{}])[0]
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
                    research_findings_summary = a.logs[:4000]
                    break
            recently_completed_tasks.append({
                "id": str(t.id),
                "title": t.title,
                "task_type": p.get("type", ""),
                "pr_url": pr_url,
                "research_findings_summary": research_findings_summary,
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
            for sib in sibling_result.scalars().all():
                sibling_tasks.append({
                    "id": str(sib.id),
                    "title": sib.title,
                    "status": sib.status,
                    "related_files": (sib.context or {}).get("related_files", []),
                    "task_type": ((sib.payloads or [{}])[0]).get("type", ""),
                })

        response["project_context"] = {
            "active_tasks": active_tasks,
            "queued_tasks": queued_tasks,
            "recently_completed_tasks": recently_completed_tasks,
            "open_prs": list(open_prs_rows),
            "sibling_tasks": sibling_tasks,
        }

    return response

@app.post("/api/agent/artifacts/upload-url")
async def get_upload_url(
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

@app.post("/api/agent/heartbeat")
async def agent_heartbeat(
    req: HeartbeatRequest,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Endpoint for agents to send a periodic heartbeat.
    """
    if str(req.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    # FOR UPDATE serializes with the reaper's FOR UPDATE SKIP LOCKED:
    # - If reaper holds the lock, we wait and then see the changed vm_instance_id (404)
    # - If we hold the lock, reaper skips the row (correct — task is actively heartbeating)
    stmt = select(Task).where(
        Task.id == req.task_id,
        Task.vm_instance_id == token.get("vm_instance_id")
    ).with_for_update()
    task = (await session.execute(stmt)).scalars().first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found for this VM")

    # Signal agent to stop if task was cancelled/completed externally
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise HTTPException(status_code=409, detail="Task already terminal")

    task.last_heartbeat = datetime.utcnow()
    if req.status_message:
        task.latest_status_message = req.status_message
    if req.metrics:
        ctx = dict(task.context or {})
        ctx['last_metrics'] = req.metrics.model_dump(exclude_none=True)
        task.context = ctx

    session.add(task)
    await session.commit()

    return {"status": "ok"}

@app.post("/api/agent/spawn")
async def spawn_subtask(
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
        while current.parent_task_id and depth < MAX_SPAWN_DEPTH + 1:
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
        parent_payload = (parent_task.payloads or [{}])[0]
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
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
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

async def _requeue_parent_task(parent: Task, session: AsyncSession):
    """Re-evaluate a parent task by retrying its failed children.
    Cancels FAILED children and clones them as new QUEUED tasks.
    Leaves COMPLETED, CANCELLED, and active (QUEUED/IN_PROGRESS) children untouched.
    Sets parent back to EVALUATING — no VM needed."""
    children = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id)
    )).scalars().all()

    # Refresh base_branch from parent's staging branch — the original child's
    # base_branch may reference a deleted branch (e.g., plan/* after rollup merge)
    parent_ctx = parent.context or {}
    parent_staging = parent_ctx.get("plan_branch") or parent_ctx.get("research_branch")

    retried_count = 0
    for child in children:
        if child.status != TaskStatus.FAILED:
            continue
        # Cancel the failed child so rollup skips it
        child.status = TaskStatus.CANCELLED
        child.latest_status_message = "Parent requeued — replaced by retry"
        session.add(child)

        # Clone as a new task with fresh VM assignment — refresh base_branch
        # to prevent cloning into a deleted branch
        new_payloads = copy.deepcopy(child.payloads or [])
        if parent_staging and new_payloads:
            new_payloads[0]["base_branch"] = parent_staging
        new_child = Task(
            project_id=child.project_id,
            title=child.title,
            description=child.description,
            parent_task_id=parent.id,
            payloads=new_payloads,
            context={k: v for k, v in (child.context or {}).items()
                     if k != "max_plan_account_id"},  # Strip runtime-only fields
            depends_on=child.depends_on,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=child.requires_approval,
        )
        session.add(new_child)
        retried_count += 1

        history = TaskHistory(task_id=new_child.id, action="SPAWNED",
                              details={"spawned_by": "REQUEUE", "retrying": str(child.id)})
        session.add(history)

    parent.status = TaskStatus.EVALUATING
    parent.latest_status_message = f"Requeued — retrying {retried_count} failed subtask(s)"
    parent.attempt_count = 0
    session.add(parent)
    await session.commit()

    for child in children:
        await session.refresh(child)
    await broadcast_to_project(str(parent.project_id), "task_updated",
                               {"task_id": str(parent.id), "status": TaskStatus.EVALUATING})
    logger.info(f"[REQUEUE] Parent {parent.id} re-evaluating — {retried_count} failed children retried")
    return retried_count


MAX_REVIEW_FIX_ITERATIONS = 30

async def _spawn_pipeline_task(
    source_task: Task, task_type: str, session: AsyncSession,
    pr_number: int, branch: str, base_branch: str,
    original_prompt: str = "", fix_prompt: str = "", original_task_id: str = "",
    current_feedback: str = ""
) -> Optional[Task]:
    """Spawn a review or fix task as part of the automated review pipeline.
    Returns None if the review-fix cycle limit has been reached.
    Returns the existing active task if a review/fix is already active for this PR."""
    # Guard: don't spawn if PR is already merged
    orig_id = original_task_id or (source_task.context or {}).get("review_loop", {}).get("original_task_id", "") or ((source_task.payloads or [{}])[0]).get("original_task_id", "")
    if orig_id:
        try:
            orig_task = await session.get(Task, uuid.UUID(orig_id))
            if orig_task and (orig_task.context or {}).get("pr_merged"):
                print(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (original task {orig_id[:8]})")
                source_task.latest_status_message = f"PR #{pr_number} already merged — skipping {task_type}"
                session.add(source_task)
                return None
        except Exception:
            pass
    # Also check the source task itself
    if (source_task.context or {}).get("pr_merged"):
        print(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (source task)")
        return None

    # Guard: don't spawn if there's already an active review/fix for this PR
    active_pipeline = (await session.execute(
        select(Task).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]),
        )
    )).scalars().all()
    for t in active_pipeline:
        if t.id == source_task.id:
            continue  # Don't block on ourselves
        t_payload = (t.payloads or [{}])[0]
        if t_payload.get("pr_number") == pr_number and t_payload.get("type") in ("review", "fix"):
            logger.warning(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — already active: {t.id} ({t_payload.get('type')})")
            source_task.latest_status_message = f"{task_type.title()} already active for PR #{pr_number} → task {str(t.id)[:8]}"
            session.add(source_task)
            return t

    # Track review-fix iteration count to prevent infinite loops
    source_loop = (source_task.context or {}).get("review_loop", {})
    source_payload = (source_task.payloads or [{}])[0]
    iteration = source_loop.get("iteration", 0) + 1

    if iteration > MAX_REVIEW_FIX_ITERATIONS:
        # Cap reached — require manual intervention
        orig_id = original_task_id or source_loop.get("original_task_id", "") or source_payload.get("original_task_id", "")
        if orig_id:
            try:
                orig = await session.get(Task, uuid.UUID(orig_id))
                if orig:
                    orig.requires_approval = True
                    orig.latest_status_message = f"Review-fix cycle reached limit ({MAX_REVIEW_FIX_ITERATIONS} iterations) — manual review required"
                    session.add(orig)
                    await session.commit()
            except Exception as e:
                logger.warning(f"[PIPELINE] Failed to mark iteration cap on {orig_id}: {e}", exc_info=True)
        logger.warning(f"[PIPELINE] Review-fix cycle limit reached for PR #{pr_number} after {iteration - 1} iterations")
        return None

    vm_name = f"agent-vm-{uuid.uuid4().hex[:16]}"

    # Determine parent: use the original task's parent (so review/fix are siblings of the
    # create task under the plan/research parent), falling back to original task itself
    resolved_original_id = original_task_id or str(source_task.id)
    parent_id = None
    try:
        orig = await session.get(Task, uuid.UUID(resolved_original_id))
        if orig and orig.parent_task_id:
            parent_id = orig.parent_task_id
        elif orig:
            parent_id = orig.id
    except Exception:
        pass

    # Apply per-task-type model/effort defaults from settings
    type_model = _runtime_config.get("task_type_models", {}).get(task_type) or None
    type_effort = _runtime_config.get("task_type_efforts", {}).get(task_type) or None

    if task_type == "review":
        title = f"Review PR #{pr_number}"
        payload_data = {"type": "review", "pr_number": pr_number, "branch": branch,
                        "base_branch": base_branch, "original_prompt": original_prompt,
                        "original_task_id": resolved_original_id,
                        **({"model": type_model} if type_model else {}),
                        **({"effort": type_effort} if type_effort else {})}
    else:
        title = f"Fix PR #{pr_number}"

        # Enrich fix prompt with history of prior review feedback for this PR
        enriched_fix_prompt = fix_prompt
        try:
            prior_reviews = (await session.execute(
                select(Task).where(
                    Task.project_id == source_task.project_id,
                    Task.status == TaskStatus.COMPLETED,
                ).order_by(Task.created_at.asc())
            )).scalars().all()
            prior_feedback_entries = []
            for t in prior_reviews:
                t_payload = (t.payloads or [{}])[0]
                if t_payload.get("pr_number") == pr_number and t_payload.get("type") == "review":
                    feedback_text = t.latest_status_message or t.description or ""
                    if feedback_text:
                        prior_feedback_entries.append(feedback_text)
            prior_feedback_entries = prior_feedback_entries[-3:]  # last 3 iterations
            if prior_feedback_entries:
                history_section = "\n\n---\n\n".join(prior_feedback_entries)
                enriched_fix_prompt = (
                    f"## Previous review feedback (already addressed):\n{history_section}"
                    f"\n\n## Current review feedback:\n{fix_prompt}"
                )
        except Exception as e:
            logger.warning(f"[PIPELINE] Failed to build review history for PR #{pr_number}: {e}", exc_info=True)

        payload_data = {"type": "fix", "pr_number": pr_number, "branch": branch,
                        "base_branch": base_branch, "prompt": enriched_fix_prompt,
                        "original_task_id": resolved_original_id,
                        **({"model": type_model} if type_model else {}),
                        **({"effort": type_effort} if type_effort else {})}

    # Build review_history: carry forward from source task, appending current feedback if spawning a fix
    prev_review_history = source_loop.get("review_history", [])
    if task_type == "fix" and current_feedback:
        new_review_history = prev_review_history + [{"iteration": iteration - 1, "feedback": current_feedback}]
    else:
        new_review_history = prev_review_history

    new_task = Task(
        project_id=source_task.project_id,
        title=title,
        description=fix_prompt or f"Automated {task_type} for PR #{pr_number}",
        parent_task_id=parent_id,
        payloads=[payload_data],
        context={"review_loop": {"pr_number": pr_number, "branch": branch,
                                  "base_branch": base_branch,
                                  "original_task_id": resolved_original_id,
                                  "iteration": iteration,
                                  **({"review_history": new_review_history} if new_review_history else {})}},
        vm_instance_id=vm_name,
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    history = TaskHistory(task_id=new_task.id, action="SPAWNED",
                          details={"spawned_by": "REVIEW_PIPELINE", "type": task_type, "pr_number": pr_number})
    session.add(history)
    await session.commit()

    await broadcast_to_project(str(source_task.project_id), "task_updated",
                               {"task_id": str(new_task.id), "status": TaskStatus.QUEUED})
    return new_task


def _parse_plan_output(text: str) -> Optional[Dict[str, Any]]:
    """Extract structured plan JSON from agent output text."""
    import re
    try:
        # Find the start of the JSON block containing "subtasks"
        match = re.search(r'\{\s*"subtasks"\s*:\s*\[', text)
        if not match:
            return None
        start = match.start()
        # Walk forward to find the matching closing brace using bracket counting
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    return json.loads(candidate)
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"[PLAN] Failed to parse plan JSON: {e}", exc_info=True)
    return None


def _parse_research_findings(text: str) -> Optional[List[Dict[str, Any]]]:
    """Extract structured research findings JSON array from agent output text."""
    import re
    try:
        # Find JSON array containing objects with "title" and "prompt"
        json_match = re.search(r'\[\s*\{[\s\S]*"title"[\s\S]*"prompt"[\s\S]*\]', text, re.DOTALL)
        if json_match:
            findings = json.loads(json_match.group())
            if isinstance(findings, list):
                return findings
    except json.JSONDecodeError:
        # JSON may be truncated — try to recover complete objects
        try:
            json_match = re.search(r'\[\s*\{[\s\S]*"title"[\s\S]*"prompt"', text, re.DOTALL)
            if json_match:
                raw = json_match.group()
                # Find last complete top-level object using depth tracking
                depth = 0
                last_close = -1
                in_string = False
                escape_next = False
                for i, c in enumerate(raw):
                    if escape_next:
                        escape_next = False
                        continue
                    if c == '\\' and in_string:
                        escape_next = True
                        continue
                    if c == '"' and not escape_next:
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            last_close = i
                if last_close > 0:
                    truncated = raw[:last_close + 1] + ']'
                    findings = json.loads(truncated)
                    if isinstance(findings, list):
                        logger.info(f"[RESEARCH] Recovered {len(findings)} findings from truncated JSON")
                        return findings
        except (json.JSONDecodeError, AttributeError):
            pass
    except AttributeError:
        pass
    return None


def _validate_plan_dependencies(subtasks: List[Dict]) -> bool:
    """Validate that plan dependencies are acyclic (topological sort check)."""
    n = len(subtasks)
    visited = set()
    in_stack = set()

    def has_cycle(node: int) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for dep in subtasks[node].get("depends_on_indices", []):
            if 0 <= dep < n and has_cycle(dep):
                return True
        in_stack.discard(node)
        return False

    return not any(has_cycle(i) for i in range(n) if i not in visited)


def _log_file_overlaps(subtasks: List[Dict], label: str = "PLAN"):
    """Log warnings when subtasks share related_files (for observability)."""
    for i, a in enumerate(subtasks):
        a_files = set(a.get("related_files", []))
        if not a_files:
            continue
        for j, b in enumerate(subtasks):
            if j <= i:
                continue
            b_files = set(b.get("related_files", []))
            overlap = a_files & b_files
            if overlap:
                a_title = a.get("title", f"#{i}")
                b_title = b.get("title", f"#{j}")
                logger.info(f"[{label}] File overlap: '{a_title}' and '{b_title}' share {', '.join(sorted(overlap))}")


async def _handle_plan_completion(task: Task, logs: str, session: AsyncSession):
    """Parse plan output and create child tasks with dependency tracking."""
    plan_data = _parse_plan_output(logs)
    if not plan_data or not plan_data.get("subtasks"):
        task.latest_status_message = "Plan agent produced no valid subtasks"
        task.status = TaskStatus.FAILED
        session.add(task)
        await session.commit()
        return

    subtasks = plan_data["subtasks"]

    # Cap subtask count to prevent runaway VM provisioning
    if len(subtasks) > 10:
        subtasks = subtasks[:10]
        logger.info(f"[PLAN] Capped subtask count from {len(plan_data['subtasks'])} to 10")

    if not _validate_plan_dependencies(subtasks):
        task.latest_status_message = "Plan has circular dependencies"
        task.status = TaskStatus.FAILED
        session.add(task)
        await session.commit()
        return

    # Store parsed plan and set parent to EVALUATING (children executing)
    task.plan_output = plan_data
    task.status = TaskStatus.EVALUATING
    session.add(task)

    parent_base = ((task.payloads or [{}])[0]).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    plan_branch = f"plan/{short_id}"

    # Create staging branch for this plan's subtasks
    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        sha = await get_branch_sha(gh_token, owner, repo, parent_base)
        await create_branch(gh_token, owner, repo, plan_branch, sha)
        logger.info(f"[PLAN] Created branch {plan_branch} from {parent_base}")
    except Exception as e:
        # Branch may already exist from a previous run — reuse it
        logger.error(f"[PLAN] Branch {plan_branch} already exists or creation failed: {e} — reusing", exc_info=True)

    # Store branch info on the plan task for rollup later
    task.context = {**(task.context or {}), "plan_branch": plan_branch, "parent_base": parent_base}
    session.add(task)

    # Check if agent already spawned children via /api/agent/spawn
    existing_children_result = await session.execute(
        select(Task).where(Task.parent_task_id == task.id)
    )
    all_children = existing_children_result.scalars().all()

    # Use spawned_task_ids from plan_data (embedded by agent) to preserve original spawn order.
    # Relying on created_at is non-deterministic: tasks spawned in a single batch share the same
    # timestamp and PostgreSQL returns ties in heap order, which may not match subtasks order.
    spawned_task_ids = plan_data.get("spawned_task_ids", [])
    if spawned_task_ids and all_children:
        children_by_id = {str(c.id): c for c in all_children}
        existing_children = [children_by_id[sid] for sid in spawned_task_ids if sid in children_by_id]
    else:
        existing_children = list(all_children)

    if existing_children:
        # Agent already spawned subtasks — enrich with branch info, dependencies, and approval
        print(f"[PLAN] Agent already spawned {len(existing_children)} children, enriching...")
        parent_payload = (task.payloads or [{}])[0]
        model = parent_payload.get('model')
        effort = parent_payload.get('effort')
        create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")

        for i, child in enumerate(existing_children):
            child_payload = (child.payloads or [{}])[0]
            child_risk = child_payload.get('risk', 'low')
            related_files = child_payload.get('related_files', [])

            # Enrich prompt with related files if not already present
            prompt_text = child_payload.get('prompt', '')
            if related_files and 'Key files to review or modify' not in prompt_text:
                child_payload['prompt'] = prompt_text + "\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)

            # Update base_branch to plan branch and inherit model/effort
            child_payload['base_branch'] = plan_branch
            if model and not child_payload.get('model'):
                child_payload['model'] = model
            if effort and not child_payload.get('effort'):
                child_payload['effort'] = effort
            if create_suffix and not child_payload.get('prompt_suffix'):
                child_payload['prompt_suffix'] = create_suffix

            # Normalize type
            _allowed_types = {'create', 'refactor', 'migrate', 'qa'}
            raw_type = child_payload.get('type', 'create')
            child_payload['type'] = raw_type if raw_type in _allowed_types else 'create'

            child.payloads = [child_payload]
            child.requires_approval = child_risk == 'high'
            child_approval_reason = child_payload.get('approval_reason') if child_risk == 'high' else None
            child.context = {
                **(child.context or {}),
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": child_payload.get('scope', 'small'),
                "risk": child_risk,
                "related_files": related_files,
                **({"approval_reason": child_approval_reason} if child_approval_reason else {}),
            }
            session.add(child)

        # Resolve dependencies in the same pass — do NOT commit between enrichment and
        # dependency wiring.  A mid-enrichment commit would let workers pick up children
        # with the correct base_branch but missing depends_on, causing out-of-order execution.
        for i, subtask_def in enumerate(subtasks):
            if i < len(existing_children):
                dep_indices = subtask_def.get("depends_on_indices", [])
                existing_children[i].depends_on = [
                    str(existing_children[j].id)
                    for j in dep_indices
                    if 0 <= j < len(existing_children)
                ]
                session.add(existing_children[i])

        _log_file_overlaps(subtasks, "PLAN")
        # Single atomic commit: base_branch, requires_approval, context, and depends_on
        # are all written together so no worker can observe a partially-enriched child.
        await session.commit()

        for ct in existing_children:
            await broadcast_to_project(str(task.project_id), "task_updated",
                                       {"task_id": str(ct.id), "status": str(ct.status)})

        print(f"[PLAN] Enriched {len(existing_children)} agent-spawned subtasks for plan {task.id}")
        return

    # First pass: create all child tasks
    parent_payload = (task.payloads or [{}])[0]
    model = parent_payload.get('model')
    effort = parent_payload.get('effort')
    plan_parent_context = {
        "parent_task_id": str(task.id),
        "parent_title": task.title,
        "parent_prompt": (parent_payload.get("prompt") or parent_payload.get("description") or "")[:500]
    }
    child_tasks = []
    for subtask_def in subtasks:
        related_files = subtask_def.get("related_files", [])
        prompt_text = subtask_def.get("prompt", "")
        full_prompt = prompt_text + ("\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)) if related_files else prompt_text
        create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
        subtask_priority = TASK_PRIORITY_MAP.get(subtask_def.get("priority", "medium"), 50)
        subtask_model = subtask_def.get('model') or model
        subtask_effort = subtask_def.get('effort') or effort
        child_scope = subtask_def.get('scope', 'small')
        child_risk = subtask_def.get('risk', 'low')
        child_approval_reason = subtask_def.get('approval_reason') if child_risk == 'high' else None
        _allowed_types = {'create', 'refactor', 'migrate', 'qa'}
        raw_type = subtask_def.get('type', 'create')
        subtask_type = raw_type if raw_type in _allowed_types else 'create'
        child = Task(
            project_id=task.project_id,
            title=subtask_def.get("title", "Untitled subtask"),
            description=subtask_def.get("prompt", ""),
            parent_task_id=task.id,
            requires_approval=False,  # review pipeline handles quality; only merge to main gates
            payloads=[{
                "type": subtask_type,
                "prompt": full_prompt,
                "base_branch": plan_branch,
                **({"model": subtask_model} if subtask_model else {}),
                **({"effort": subtask_effort} if subtask_effort else {}),
                **({"prompt_suffix": create_suffix} if create_suffix else {}),
            }],
            context={
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": child_scope,
                "risk": child_risk,
                "related_files": related_files,
                "parent_context": plan_parent_context,
                **({"approval_reason": child_approval_reason} if child_approval_reason else {}),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            depends_on=[],
            priority=subtask_priority,
        )
        session.add(child)
        child_tasks.append(child)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Second pass: resolve depends_on_indices to actual UUIDs
    for i, subtask_def in enumerate(subtasks):
        dep_indices = subtask_def.get("depends_on_indices", [])
        child_tasks[i].depends_on = [
            str(child_tasks[j].id)
            for j in dep_indices
            if 0 <= j < len(child_tasks)
        ]
        session.add(child_tasks[i])

    # Log file overlap warnings for observability
    _log_file_overlaps(subtasks, "PLAN")

    await session.commit()

    # Record history
    for ct in child_tasks:
        history = TaskHistory(
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "PLAN_DECOMPOSITION", "parent_plan_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    await session.commit()
    logger.info(f"[PLAN] Created {len(child_tasks)} subtasks from plan {task.id}")


async def _handle_security_audit_completion(task: Task, logs: str, session: AsyncSession):
    """Parse security audit findings and spawn create tasks for critical/high severity issues."""
    # Parse the JSON findings array from logs
    findings = None
    try:
        json_match = re.search(r'\[\s*\{[\s\S]*"severity"[\s\S]*\]', logs, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            if isinstance(parsed, list):
                findings = parsed
    except (json.JSONDecodeError, AttributeError):
        pass

    if not findings:
        task.latest_status_message = "Security audit completed with no parseable findings"
        task.plan_output = []
        session.add(task)
        await session.commit()
        return

    # Store all findings for display
    task.plan_output = findings
    session.add(task)
    await session.commit()

    base_branch = ((task.payloads or [{}])[0]).get("base_branch", "main")

    # Spawn create tasks for critical and high severity findings
    actionable = [f for f in findings if f.get("severity") in ("critical", "high")]
    if len(actionable) > 10:
        actionable = actionable[:10]
    if not actionable:
        task.latest_status_message = f"Security audit found {len(findings)} finding(s), none critical/high — no tasks spawned"
        session.add(task)
        await session.commit()
        return

    create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
    child_tasks = []
    for i, finding in enumerate(actionable):
        fix_prompt = finding.get("fix_prompt", finding.get("description", ""))
        if not fix_prompt:
            continue
        title = finding.get("title", f"Security fix #{i+1}")
        severity = finding.get("severity", "high")

        new_task = Task(
            project_id=task.project_id,
            title=f"[{severity.upper()}] {title}",
            description=finding.get("description", fix_prompt[:200]),
            parent_task_id=task.id,
            payloads=[{
                "type": "create",
                "prompt": fix_prompt,
                "base_branch": base_branch,
                **({"prompt_suffix": create_suffix} if create_suffix else {}),
            }],
            context={
                "security_audit_task_id": str(task.id),
                "finding_index": i,
                "severity": severity,
                "file_path": finding.get("file_path", ""),
                "line_number": finding.get("line_number", 0),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=False,  # review pipeline handles quality; only merge to main gates
            depends_on=[],
        )
        session.add(new_task)
        child_tasks.append(new_task)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)
        history = TaskHistory(
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "SECURITY_AUDIT", "parent_audit_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    await session.commit()

    # Post PR comment for medium/low findings that don't get auto-fixed
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    if informational:
        rows = "\n".join(
            "| {severity} | {title} | {loc} | {desc} |".format(
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                loc=(f.get("file_path", "") + (f":{f['line_number']}" if f.get("line_number") else "")),
                desc=f.get("description", ""),
            )
            for f in informational
        )
        comment_body = (
            "## Security Audit Findings (Informational)\n\n"
            "The following medium/low severity findings were identified. "
            "They have not been auto-fixed and require manual review:\n\n"
            "| Severity | Title | File | Description |\n"
            "|----------|-------|------|-------------|\n"
            f"{rows}\n"
        )
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            from app.github import get_open_pull_requests
            open_prs = await get_open_pull_requests(gh_token, owner, repo, base_branch)
            if open_prs:
                pr_number = open_prs[0]["number"]
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)
                logger.info(f"[SECURITY AUDIT] Posted informational findings comment on PR #{pr_number}")
        except Exception as e:
            logger.warning(f"[SECURITY AUDIT] Failed to post PR comment for informational findings: {e}", exc_info=True)

    task.status = TaskStatus.EVALUATING
    task.latest_status_message = (
        f"{len(child_tasks)} critical/high fixed, {len(informational)} medium/low surfaced as PR comment"
    )
    session.add(task)
    await session.commit()

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"security_audit_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[SECURITY AUDIT] Created {len(child_tasks)} fix tasks from audit {task.id}")


async def _handle_perf_completion(task: Task, logs: str, session: AsyncSession):
    """Parse perf findings and spawn create tasks for high-severity issues."""
    findings = None
    try:
        json_match = re.search(r'\[\s*\{[\s\S]*"severity"[\s\S]*\]', logs, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            if isinstance(parsed, list):
                findings = parsed
    except (json.JSONDecodeError, AttributeError):
        pass

    if not findings:
        task.latest_status_message = "Perf analysis completed with no parseable findings"
        task.plan_output = []
        session.add(task)
        return

    # Store all findings for display
    task.plan_output = findings
    session.add(task)

    base_branch = ((task.payloads or [{}])[0]).get("base_branch", "main")

    # Spawn create tasks for high-severity findings
    actionable = [f for f in findings if f.get("severity") == "high"]
    if len(actionable) > 10:
        actionable = actionable[:10]

    child_tasks = []
    for i, finding in enumerate(actionable):
        fix_prompt = finding.get("fix_prompt", finding.get("description", ""))
        if not fix_prompt:
            continue
        title = finding.get("title", f"Perf fix #{i+1}")
        area = finding.get("area", "")

        new_task = Task(
            project_id=task.project_id,
            title=f"[PERF] {title}",
            description=finding.get("description", fix_prompt[:200]),
            parent_task_id=task.id,
            payloads=[{
                "type": "create",
                "prompt": fix_prompt,
                "base_branch": base_branch,
            }],
            context={
                "perf_task_id": str(task.id),
                "finding_index": i,
                "severity": "high",
                "area": area,
                "related_files": finding.get("related_files", []),
            },
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=False,
            depends_on=[],
        )
        session.add(new_task)
        child_tasks.append(new_task)

    await session.flush()
    for ct in child_tasks:
        history = TaskHistory(
            task_id=ct.id, action="SPAWNED",
            details={"spawned_by": "PERF_AUDIT", "parent_perf_id": str(task.id)}
        )
        session.add(history)
        await broadcast_to_project(str(task.project_id), "task_updated",
                                   {"task_id": str(ct.id), "status": TaskStatus.QUEUED})

    # Post a summary comment on open PRs for medium/low findings
    informational = [f for f in findings if f.get("severity") in ("medium", "low")]
    all_surfaced = informational + [f for f in findings if f.get("severity") == "high" and f not in actionable]
    if all_surfaced or findings:
        rows = "\n".join(
            "| {area} | {severity} | {title} | {desc} |".format(
                area=f.get("area", ""),
                severity=f.get("severity", ""),
                title=f.get("title", ""),
                desc=f.get("description", "")[:120],
            )
            for f in findings
        )
        comment_body = (
            "## Performance Analysis Findings\n\n"
            f"Analyzed codebase for performance bottlenecks. "
            f"{len(child_tasks)} high-severity fix task(s) spawned automatically.\n\n"
            "| Area | Severity | Title | Description |\n"
            "|------|----------|-------|-------------|\n"
            f"{rows}\n"
        )
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            from app.github import get_open_pull_requests
            open_prs = await get_open_pull_requests(gh_token, owner, repo, base_branch)
            if open_prs:
                pr_number = open_prs[0]["number"]
                await create_pr_comment(gh_token, owner, repo, pr_number, comment_body)
                logger.info(f"[PERF] Posted findings summary comment on PR #{pr_number}")
        except Exception as e:
            logger.warning(f"[PERF] Failed to post PR comment: {e}", exc_info=True)

    task.status = TaskStatus.EVALUATING
    task.latest_status_message = (
        f"{len(child_tasks)} high-severity fix task(s) spawned, "
        f"{len(informational)} medium/low finding(s) surfaced as PR comment"
    )
    session.add(task)

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"perf_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[PERF] Created {len(child_tasks)} fix tasks from perf analysis {task.id}")


async def _handle_investigation_completion(task: Task, payload, session: AsyncSession):
    """Store investigation report logs in plan_output and mark task completed."""
    if payload.logs:
        try:
            task.plan_output = json.loads(payload.logs)
        except (json.JSONDecodeError, Exception):
            task.plan_output = {"raw": payload.logs}
    task.status = TaskStatus.COMPLETED
    session.add(task)


async def _handle_research_completion(task: Task, logs: str, session: AsyncSession):
    """Parse research findings and create tasks from them."""
    # Depth guard: prevent infinite research recursion
    MAX_RESEARCH_DEPTH = 3
    research_depth = (task.context or {}).get("research_depth", 0)
    if research_depth >= MAX_RESEARCH_DEPTH:
        task.latest_status_message = f"Research depth limit ({MAX_RESEARCH_DEPTH}) reached — not spawning further tasks"
        session.add(task)
        await session.commit()
        logger.warning(f"[RESEARCH] Depth limit reached for task {task.id} (depth={research_depth})")
        return

    findings = _parse_research_findings(logs)
    if not findings:
        task.latest_status_message = "Research completed with no actionable findings"
        session.add(task)
        await session.commit()
        return

    # Cap findings to prevent runaway task creation
    if len(findings) > 30:
        findings = findings[:30]

    original_base = ((task.payloads or [{}])[0]).get("base_branch", "main")
    short_id = str(task.id).replace("-", "")[:8]
    research_branch = f"research/{short_id}"

    # Create staging branch for this research batch
    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        sha = await get_branch_sha(gh_token, owner, repo, original_base)
        await create_branch(gh_token, owner, repo, research_branch, sha)
        logger.info(f"[RESEARCH] Created branch {research_branch} from {original_base}")
    except Exception as e:
        # Branch may already exist from a previous run — reuse it
        logger.error(f"[RESEARCH] Branch {research_branch} already exists or creation failed: {e} — reusing", exc_info=True)

    # Validate dependencies if present
    if not _validate_plan_dependencies(findings):
        logger.warning("[RESEARCH] Findings have circular dependencies — ignoring depends_on_indices")
        for f in findings:
            f.pop("depends_on_indices", None)

    # Query active/queued tasks for deduplication
    existing_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING, TaskStatus.PROVISIONING])
        )
    )).scalars().all()
    existing_titles = [t.title.lower() for t in existing_tasks]

    # Query recent tasks (last 30 days) for hash-based deduplication across research cycles
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    recent_hashed_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.created_at >= thirty_days_ago,
            Task.context["finding_hash"].astext != None,  # noqa: E711
        )
    )).scalars().all()
    recent_finding_hashes = {
        t.context["finding_hash"] for t in recent_hashed_tasks if t.context and t.context.get("finding_hash")
    }

    def _finding_hash(title: str, description: str) -> str:
        return hashlib.sha256((title + description).encode()).hexdigest()[:16]

    def _is_duplicate(finding_title: str, existing: list[str]) -> bool:
        words = set(finding_title.lower().split()) - {'the', 'a', 'an', 'feat', 'fix', 'refactor', 'add', 'update', 'improve'}
        for title in existing:
            overlap = words & set(title.split())
            if len(overlap) >= 3:  # 3+ meaningful words in common
                return True
        return False

    # Sort findings by priority: high → medium → low (preserve original order within each tier)
    priority_order = {"high": 0, "medium": 1, "low": 2}
    indexed_findings = list(enumerate(findings))
    indexed_findings.sort(key=lambda x: priority_order.get(x[1].get("priority", "medium"), 1))

    wave_size = max(1, _runtime_config.get("autonomous_research_wave_size", 10))

    # Group into waves by priority tier, each capped at wave_size
    waves: list[list[tuple[int, dict]]] = []
    current_priority = None
    current_wave: list[tuple[int, dict]] = []
    skipped_duplicates = 0
    for orig_i, finding in indexed_findings:
        title = finding.get("title", f"Research finding #{orig_i+1}")
        description = finding.get("description", finding.get("prompt", ""))
        fhash = _finding_hash(title, description)
        if fhash in recent_finding_hashes:
            print(f"[RESEARCH] Skipping finding '{title}' — hash {fhash} matched recent task")
            skipped_duplicates += 1
            continue
        if _is_duplicate(title, existing_titles):
            logger.warning(f"[RESEARCH] Skipping duplicate finding '{title}' — matches existing task")
            skipped_duplicates += 1
            continue
        p = finding.get("priority", "medium")
        if p != current_priority:
            if current_wave:
                # Flush current wave, splitting into chunks of wave_size
                for chunk_start in range(0, len(current_wave), wave_size):
                    waves.append(current_wave[chunk_start:chunk_start + wave_size])
            current_wave = [(orig_i, finding)]
            current_priority = p
        else:
            current_wave.append((orig_i, finding))
            if len(current_wave) >= wave_size:
                waves.append(current_wave)
                current_wave = []
    if current_wave:
        for chunk_start in range(0, len(current_wave), wave_size):
            waves.append(current_wave[chunk_start:chunk_start + wave_size])

    # First pass: create all child tasks, recording which wave each belongs to
    child_tasks = []
    finding_indices = []  # Track which finding index maps to which child task index
    # wave_task_indices[w] = list of child_tasks indices for wave w
    wave_task_indices: list[list[int]] = [[] for _ in waves]

    for wave_idx, wave in enumerate(waves):
        for orig_i, finding in wave:
            scope = finding.get("scope", "small")
            risk = finding.get("risk", "low")
            title = finding.get("title", f"Research finding #{orig_i+1}")
            prompt = finding.get("prompt", finding.get("description", ""))

            if not prompt:
                continue

            # Determine task type: research findings can spawn sub-research or specialized agents
            finding_type = finding.get("type", "improvement")
            if finding_type == "research":
                task_type = "research"  # deeper investigation before implementation
            elif finding_type in ("security_audit", "qa", "deps", "docs", "migrate", "perf"):
                task_type = finding_type  # spawn specialized agent
            elif finding_type == "refactor":
                task_type = "refactor"  # code quality improvement, no behavior change
            elif scope == "large":
                task_type = "plan"     # large scope needs decomposition
            else:
                task_type = "create"   # small/medium scope is single-PR work
            # Pre-spawn approval removed: the review pipeline handles quality gates.
            # Only final merge to main requires human approval (critical files gate).
            needs_approval = False

            direction_id = finding.get("direction_id")
            finding_model = finding.get('model')
            finding_effort = finding.get('effort')

            related_files = finding.get("related_files", [])
            full_prompt = prompt + ("\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)) if related_files else prompt
            type_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get(task_type)
            finding_priority = TASK_PRIORITY_MAP.get(finding.get("priority", "medium"), 50)
            new_task = Task(
                project_id=task.project_id,
                title=title,
                description=finding.get("description", prompt[:200]),
                parent_task_id=task.id,
                payloads=[{
                    "type": task_type,
                    "prompt": full_prompt,
                    "base_branch": research_branch,
                    **(({'model': finding_model} if finding_model else {})),
                    **(({'effort': finding_effort} if finding_effort else {})),
                    **({"prompt_suffix": type_suffix} if type_suffix else {}),
                }],
                context={
                    "research_task_id": str(task.id),
                    "finding_index": orig_i,
                    "scope": scope,
                    "risk": risk,
                    "research_branch": research_branch,
                    "original_base": original_base,
                    "research_depth": research_depth + 1,
                    "related_files": related_files,
                    "finding_hash": fhash,
                    **({"direction_id": direction_id} if direction_id else {}),
                },
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
                requires_approval=needs_approval,
                depends_on=[],
                priority=finding_priority,
            )
            session.add(new_task)
            ci = len(child_tasks)
            child_tasks.append(new_task)
            finding_indices.append(orig_i)
            wave_task_indices[wave_idx].append(ci)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Second pass: resolve depends_on_indices to actual UUIDs and apply wave dependencies
    # finding_indices maps child_tasks position → findings array position
    finding_to_child = {fi: ci for ci, fi in enumerate(finding_indices)}
    for wave_idx, wave_ci_list in enumerate(wave_task_indices):
        # All tasks in the preceding wave must complete before this wave starts
        prev_wave_uuids = (
            [str(child_tasks[ci].id) for ci in wave_task_indices[wave_idx - 1]]
            if wave_idx > 0 else []
        )
        for ci in wave_ci_list:
            fi = finding_indices[ci]
            dep_indices = findings[fi].get("depends_on_indices", [])
            explicit_deps = [
                str(child_tasks[finding_to_child[j]].id)
                for j in dep_indices
                if j in finding_to_child
            ]
            # Merge wave deps with explicit depends_on_indices (deduplicated)
            merged = list(dict.fromkeys(prev_wave_uuids + explicit_deps))
            child_tasks[ci].depends_on = merged
            if merged:
                session.add(child_tasks[ci])

    # Log file overlap warnings for observability
    _log_file_overlaps(findings, "RESEARCH")

    await session.commit()

    # Update last_researched_at for each direction included in this research
    task_directions = ((task.payloads or [{}])[0]).get("directions", [])
    direction_ids_researched = {d["id"] for d in task_directions if d.get("id")}
    if direction_ids_researched:
        project = await session.get(Project, task.project_id)
        if project:
            directions = list(project.directions or [])
            now_iso = datetime.utcnow().isoformat()
            for d in directions:
                if d.get("id") in direction_ids_researched:
                    d["last_researched_at"] = now_iso
            project.directions = directions
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(project, "directions")
            session.add(project)

    task.status = TaskStatus.EVALUATING
    task.latest_status_message = (
        f"Research created {len(child_tasks)} tasks from {len(findings)} findings"
        + (f" ({skipped_duplicates} skipped as duplicates)" if skipped_duplicates else "")
    )
    task.context = {**(task.context or {}), "research_branch": research_branch, "original_base": original_base}
    session.add(task)
    await session.commit()

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"research_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[RESEARCH] Created {len(child_tasks)} tasks from research {task.id}")


# --- Rollup & Branch Cleanup Helpers ---
# Extracted so both update_agent_status and admin PATCH can trigger rollup.

async def _verify_pr_merged(task: Task, session: AsyncSession) -> bool:
    """Check if a create task's PR is merged. Uses context cache, falls back to GitHub API.

    Uses its own session for DB writes to avoid polluting the caller's session state.
    The passed session is only used for reads (artifact lookup).
    """
    if (task.context or {}).get("pr_merged"):
        return True
    # Look for PR artifact to verify via API
    art_result = await session.execute(
        select(Artifact).where(Artifact.task_id == task.id, Artifact.artifact_type == ArtifactType.PULL_REQUEST)
    )
    artifact = art_result.scalars().first()
    if not artifact or not artifact.pr_url:
        # No PR artifact — task completed without creating a PR (e.g., no changes needed).
        # Treat as "done" so it doesn't block rollup.
        return True
    if artifact and artifact.pr_url:
        try:
            pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            if pr_data.get("merged"):
                # Use separate session to persist — don't pollute caller's session
                async with AsyncSessionLocal() as write_session:
                    db_task = await write_session.get(Task, task.id)
                    if db_task:
                        db_task.context = {**(db_task.context or {}), "pr_merged": True}
                        write_session.add(db_task)
                        await write_session.commit()
                # Also update the in-memory object so caller sees the change
                task.context = {**(task.context or {}), "pr_merged": True}
                return True
        except Exception as e:
            logger.warning(f"[PR_VERIFY] Failed to verify PR merge for task {task.id}: {e}", exc_info=True)
    return False


async def _cleanup_branches(task: Task, session: AsyncSession) -> None:
    """Delete staging and feature branches that are no longer needed."""
    ctx = task.context or {}
    gh_token = owner = repo = None

    async def _ensure_token():
        nonlocal gh_token, owner, repo
        if not gh_token:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    branches_to_delete = []

    # Staging branches (research/*, plan/*) are shared by ALL sibling tasks.
    # Only delete when the PARENT task completes (rollup), not on individual child completion.
    # Check if this task IS the parent (has plan_output or is a research task).
    is_parent = task.plan_output is not None or ((task.payloads or [{}])[0]).get("type") in ("research", "plan")
    if is_parent:
        staging = ctx.get("plan_branch") or ctx.get("research_branch")
        if staging:
            # Don't delete staging branch if there are still open PRs targeting it
            try:
                _gh, _owner, _repo = await get_github_token_for_task(task.id, session)
                from app.github import get_open_pull_requests
                open_prs = await get_open_pull_requests(_gh, _owner, _repo, staging)
                if open_prs:
                    pr_nums = [f"#{p['number']}" for p in open_prs]
                    logger.warning(f"[CLEANUP] Skipping deletion of {staging} — {len(open_prs)} open PR(s) targeting it: {', '.join(pr_nums)}")
                else:
                    branches_to_delete.append(staging)
            except Exception:
                # If we can't check, don't delete (safe default)
                logger.warning(f"[CLEANUP] Skipping deletion of {staging} — could not verify open PRs")

    # Feature branches after PR merge
    if ctx.get("pr_merged"):
        for payload in (task.payloads or []):
            branch = payload.get("branch")
            if branch and branch.startswith("blueprint/"):
                branches_to_delete.append(branch)

    for branch in branches_to_delete:
        try:
            await _ensure_token()
            deleted = await delete_branch(gh_token, owner, repo, branch)
            if deleted:
                logger.info(f"[CLEANUP] Deleted branch {branch} (task {task.id})")
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to delete {branch}: {e}", exc_info=True)


async def _check_and_trigger_rollup(child_task: Task, session: AsyncSession) -> None:
    """Check if all siblings are done and trigger rollup PR for the parent.

    Called after a child task completes, fails, or is cancelled.
    Also called from admin PATCH when status/context changes.
    """
    if not child_task.parent_task_id:
        return

    # Lock parent row to serialize concurrent rollup checks from sibling completions
    parent = (await session.execute(
        select(Task).where(Task.id == child_task.parent_task_id).with_for_update()
    )).scalars().first()
    if not parent or parent.status != TaskStatus.EVALUATING:
        return

    all_children = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id)
    )).scalars().all()

    # Consider create, refactor, plan, research, and specialized agent tasks for rollup decisions.
    # Review/fix tasks are pipeline helpers that operate on the same PR — excluded.
    ROLLUP_TYPES = {"create", "refactor", "migrate", "", "plan", "research", "security_audit", "qa", "deps", "docs", "smoketest", "perf"}
    children = [c for c in all_children
                if ((c.payloads or [{}])[0]).get("type", "create") in ROLLUP_TYPES]

    # Also check if any review/fix pipeline tasks are still active — don't rollup
    # while a review/fix cycle is in progress
    pipeline_active = any(
        c.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS)
        for c in all_children
        if ((c.payloads or [{}])[0]).get("type") in ("review", "fix")
    )

    # Check all non-cancelled create children are done with PRs merged
    all_done = True
    cancelled_count = 0
    active_count = 0
    for c in children:
        if c.status == TaskStatus.CANCELLED:
            cancelled_count += 1
            continue
        if c.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING):
            active_count += 1
            all_done = False
            continue
        if c.status == TaskStatus.FAILED:
            all_done = False
            continue
        # COMPLETED — check pr_merged for create tasks
        if not await _verify_pr_merged(c, session):
            all_done = False

    # Don't rollup if there are still active tasks or active review/fix pipeline
    if active_count > 0 or pipeline_active:
        if pipeline_active and (parent.latest_status_message or "").startswith("Blocked:"):
            parent.latest_status_message = "Fix/review pipeline running — waiting for PR to merge"
            session.add(parent)
            await session.commit()
        return

    # Surface what's blocking if children are done but not all merged
    if not all_done and active_count == 0 and not pipeline_active:
        unmerged = [c for c in children
                    if c.status == TaskStatus.COMPLETED and not (c.context or {}).get("pr_merged")]
        failed = [c for c in children if c.status == TaskStatus.FAILED]
        if unmerged:
            titles = [c.title[:40] for c in unmerged]
            parent.latest_status_message = f"Blocked: {len(unmerged)} subtask(s) completed but PR not merged — {', '.join(titles)}"
            logger.error(f"[ROLLUP] Parent {parent.id} blocked — {len(unmerged)} children completed but unmerged")
        elif failed:
            parent.latest_status_message = f"{len(failed)} subtask(s) failed — requeue parent to retry"
            logger.error(f"[ROLLUP] Parent {parent.id} blocked — {len(failed)} children failed")
        session.add(parent)
        await session.commit()
        return

    child_statuses = [c.status for c in children]

    # Edge case: all children cancelled — nothing to rollup, mark parent cancelled
    if cancelled_count == len(children):
        parent.status = TaskStatus.CANCELLED
        parent.latest_status_message = f"All {len(children)} subtasks were cancelled"
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.CANCELLED})
        logger.info(f"[ROLLUP] Parent {parent.id} cancelled — all children cancelled")
        return

    if all_done and (len(children) - cancelled_count) > 0:
        # All non-cancelled children merged — create rollup PR
        parent_ctx = parent.context or {}
        staging_branch = parent_ctx.get("plan_branch") or parent_ctx.get("research_branch")
        target_branch = parent_ctx.get("parent_base") or parent_ctx.get("original_base")

        completed_count = len(children) - cancelled_count
        rollup_note = ""
        if cancelled_count > 0:
            rollup_note = f"\n\n*{cancelled_count} subtask(s) were cancelled and excluded from this rollup.*"

        if staging_branch and target_branch and staging_branch != target_branch:
            try:
                gh_token, owner, repo = await get_github_token_for_task(parent.id, session)

                # Verify all PRs targeting the staging branch are resolved before rolling up
                from app.github import get_open_pull_requests
                open_prs = await get_open_pull_requests(gh_token, owner, repo, staging_branch)
                if open_prs:
                    pr_nums = [f"#{p['number']}" for p in open_prs]
                    parent.latest_status_message = f"Rollup waiting: {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)}"
                    session.add(parent)
                    await session.commit()
                    logger.info(f"[ROLLUP] Parent {parent.id} has {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)} — waiting")
                    return

                is_target_intermediate = target_branch.startswith("research/") or target_branch.startswith("plan/")
                pr_title = f"[Blueprint] Rollup: {parent.title}"
                pr_body = f"Rollup of {completed_count} subtasks from `{staging_branch}`{rollup_note}\n\n---\n*Automated by Blueprint Pipeline*"

                pr_result = await create_pull_request(gh_token, owner, repo,
                    title=pr_title, head=staging_branch, base=target_branch, body=pr_body)
                rollup_pr_url = pr_result.get("html_url", "")
                rollup_pr_number = pr_result.get("number")
                logger.info(f"[ROLLUP] Created PR #{rollup_pr_number}: {staging_branch} → {target_branch}")

                # Auto-merge small rollups on intermediate branches; review large ones
                # Measure by total lines changed (additions + deletions)
                try:
                    pr_detail = await get_pull_request(gh_token, owner, repo, rollup_pr_number)
                    lines_changed = pr_detail.get("additions", 0) + pr_detail.get("deletions", 0)
                except Exception:
                    lines_changed = 999  # assume large if we can't check
                is_small_rollup = lines_changed <= 100
                logger.info(f"[ROLLUP] PR #{rollup_pr_number}: {lines_changed} lines changed, {'auto-merge' if is_small_rollup and is_target_intermediate else 'review'}")
                if is_target_intermediate and is_small_rollup:
                    # Always run security scan before any merge
                    try:
                        rollup_scan = await scan_pr_for_injection(gh_token, owner, repo, rollup_pr_number)
                        logger.info(f"[SECURITY] Rollup scan: {rollup_scan['summary']}")
                        if rollup_scan["blocked"]:
                            parent.requires_approval = True
                            parent.latest_status_message = f"Rollup security scan blocked: {rollup_scan['summary']}"
                            parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                            if rollup_scan["findings"]:
                                await create_pr_comment(gh_token, owner, repo, rollup_pr_number, rollup_scan["report"])
                            session.add(parent)
                            await session.commit()
                            return
                        if rollup_scan["findings"]:
                            await create_pr_comment(gh_token, owner, repo, rollup_pr_number, rollup_scan["report"])
                    except Exception as e:
                        logger.error(f"[SECURITY] Rollup scan failed for PR #{rollup_pr_number}, blocking: {e}", exc_info=True)
                        parent.requires_approval = True
                        parent.latest_status_message = f"Rollup security scan failed — requires manual approval: {type(e).__name__}: {e!r}"[:500]
                        parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                        session.add(parent)
                        await session.commit()
                        return

                    merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
                    if merge_result.get("merged"):
                        parent.status = TaskStatus.COMPLETED
                        parent.context = {**parent_ctx, "pr_merged": True, "rollup_pr_url": rollup_pr_url}
                        parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR auto-merged"
                        await delete_branch(gh_token, owner, repo, staging_branch)
                        logger.info(f"[ROLLUP] Auto-merged rollup into {target_branch}, deleted {staging_branch}")
                    else:
                        # Merge failed (conflicts) — spawn review/fix cycle instead of giving up
                        merge_msg = merge_result.get("message", "unknown")
                        parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked: {merge_msg} — spawning fix task"
                        try:
                            pr_files = await get_pr_files(gh_token, owner, repo, rollup_pr_number)
                            files_list = "\n".join(f"  - {f['filename']} ({f['status']})" for f in pr_files)
                        except Exception:
                            files_list = "  (could not fetch file list)"
                        fix_prompt = (
                            f"This rollup PR (#{rollup_pr_number}) merging {staging_branch} into {target_branch} has conflicts.\n"
                            f"Error: {merge_msg}\n\n"
                            f"Files in this PR:\n{files_list}\n\n"
                            f"Steps to resolve:\n"
                            f"1. Run: git fetch origin {target_branch}\n"
                            f"2. Run: git rebase origin/{target_branch}\n"
                            f"3. Resolve any conflicts — keep the intent of the subtask changes while incorporating upstream updates\n"
                            f"4. Run: git rebase --continue after resolving each file\n\n"
                            f"Preserve the intent of the existing changes."
                        )
                        fix_task = await _spawn_pipeline_task(
                            parent, "fix", session, rollup_pr_number, staging_branch, target_branch,
                            fix_prompt=fix_prompt,
                            original_task_id=str(parent.id)
                        )
                        if fix_task:
                            logger.error(f"[ROLLUP] Merge blocked for rollup PR #{rollup_pr_number}, spawned fix → task {fix_task.id}")
                        else:
                            parent.status = TaskStatus.FAILED
                            parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked, fix not spawned (iteration cap)"
                            logger.warning(f"[ROLLUP] WARNING: Rollup fix spawn returned None for PR #{rollup_pr_number}")
                else:
                    parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting review"
                    parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                    review_task = await _spawn_pipeline_task(
                        parent, "review", session, rollup_pr_number, staging_branch, target_branch,
                        original_prompt=parent.description or parent.title,
                        original_task_id=str(parent.id)
                    )
                    if review_task:
                        logger.info(f"[ROLLUP] Spawned review for rollup PR #{rollup_pr_number} → task {review_task.id}")
                    else:
                        parent.status = TaskStatus.FAILED
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} created but review not spawned (iteration cap)"
                        logger.warning(f"[ROLLUP] WARNING: Rollup review spawn returned None for PR #{rollup_pr_number}")
            except Exception as e:
                logger.warning(f"[ROLLUP] Failed to create rollup PR: {e}", exc_info=True)
                parent.status = TaskStatus.FAILED
                parent.latest_status_message = f"All {completed_count} subtasks done but rollup PR creation failed: {type(e).__name__}: {e!r}"[:500]
        else:
            parent.status = TaskStatus.COMPLETED
            parent.latest_status_message = f"All {completed_count} subtasks completed and merged"

        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": parent.status})
        logger.info(f"[ROLLUP] Parent {parent.id} — all children done (completed={completed_count}, cancelled={cancelled_count})")

    elif any(s == TaskStatus.FAILED for s in child_statuses) and active_count == 0:
        # All done but some failed — mark parent as failed but keep staging branch
        # (it may contain merged work from successful children that can be salvaged)
        parent.status = TaskStatus.FAILED
        failed_count = sum(1 for s in child_statuses if s == TaskStatus.FAILED)
        parent.latest_status_message = f"{failed_count} of {len(children)} subtasks failed"
        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.FAILED})
        logger.error(f"[ROLLUP] Parent {parent.id} failed — {failed_count} children failed")


@app.post("/api/agent/status")
async def update_agent_status(
    payload: AgentStatusPayload, 
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token)
):
    """
    Endpoint for agents to report progress, save git artifacts, and update task status.
    """
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
        
    # 1. Fetch task — validate vm_instance_id to prevent stale agents from
    # overwriting status after a task was requeued with a new VM.
    # FOR UPDATE prevents race with reaper: without it, a stale agent's
    # COMPLETED can overwrite the reaper's QUEUED (requeue) between read and write.
    stmt = select(Task).where(
        Task.id == payload.task_id,
        Task.vm_instance_id == token.get("vm_instance_id")
    ).with_for_update()
    result = await session.execute(stmt)
    task = result.scalars().first()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found for this VM")

    # Idempotency guard: if the task is already in a terminal state, ignore duplicate
    # callbacks (e.g. agent HTTP retries).  Still return 200 so the agent doesn't retry.
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return {"status": "already_terminal", "task_id": str(task.id)}

    # 2. Update task status
    task.status = payload.status
    # Preserve logs on the task when no artifact is created (e.g., "no changes" completion)
    if payload.logs and not payload.artifact_type:
        task.latest_status_message = payload.logs[:2000]
    session.add(task)
    
    # Broadcast status change
    await broadcast_to_project(
        str(task.project_id), 
        "task_updated", 
        {"task_id": str(task.id), "status": payload.status}
    )
    
    # 3. Create Artifact if provided
    if payload.artifact_type:
        artifact = Artifact(
            task_id=task.id,
            artifact_type=payload.artifact_type,
            commit_hash=payload.commit_hash,
            repo_url=payload.repo_url,
            pr_url=payload.pr_url,
            storage_uri=payload.storage_uri,
            logs=payload.logs
        )
        session.add(artifact)
        
        # Broadcast artifact creation (e.g., new logs or screenshot available)
        await broadcast_to_project(
            str(task.project_id),
            "artifact_created",
            {
                "task_id": str(task.id),
                "artifact_type": payload.artifact_type,
                "logs": payload.logs,
                "storage_uri": payload.storage_uri
            }
        )

    # 3b. Parse and store smoketest result in task.plan_output for easy retrieval
    if payload.artifact_type == ArtifactType.SMOKETEST_RESULT and payload.logs:
        try:
            smoketest_data = json.loads(payload.logs)
            task.plan_output = smoketest_data
            session.add(task)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[SMOKETEST] Failed to parse smoketest result for task {task.id}: {e}", exc_info=True)

    # 3b-ii. On smoketest failure, spawn a plan task and create a persistent alert
    if payload.artifact_type == ArtifactType.SMOKETEST_RESULT and payload.status == TaskStatus.FAILED and payload.logs:
        try:
            smoketest_data = json.loads(payload.logs)
            if not smoketest_data.get("passed"):
                failed_tests = [t for t in smoketest_data.get("tests", []) if t.get("status") == "fail"]
                if failed_tests:
                    base_branch = ((task.payloads or [{}])[0]).get("base_branch", "main")
                    failure_summary = "\n".join(
                        f"- {t['name']}: {t.get('message', 'no details')}" for t in failed_tests
                    )
                    type_model = _runtime_config.get("task_type_models", {}).get("plan") or None
                    type_effort = _runtime_config.get("task_type_efforts", {}).get("plan") or None
                    plan_task = Task(
                        project_id=task.project_id,
                        title=f"Fix smoketest failures on {base_branch}",
                        description=f"{len(failed_tests)} smoke test(s) failed after merge to {base_branch}",
                        parent_task_id=task.id,
                        payloads=[{
                            "type": "plan",
                            "prompt": (
                                f"Smoke tests failed after a merge to {base_branch}. "
                                f"Investigate and plan fixes for these failures:\n\n"
                                f"{failure_summary}\n\n"
                                f"Diagnose root causes, identify which recent changes likely caused each failure, "
                                f"and produce subtasks to fix them."
                            ),
                            "base_branch": base_branch,
                            **({"model": type_model} if type_model else {}),
                            **({"effort": type_effort} if type_effort else {}),
                        }],
                        context={
                            "smoketest_task_id": str(task.id),
                            "failed_tests": [t["name"] for t in failed_tests],
                        },
                        vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                        status=TaskStatus.QUEUED,
                        created_by="ORCHESTRATOR",
                    )
                    session.add(plan_task)
                    await session.flush()
                    history = TaskHistory(
                        task_id=plan_task.id, action="SPAWNED",
                        details={"spawned_by": "SMOKETEST_FAILURE", "failed_tests": [t["name"] for t in failed_tests]}
                    )
                    session.add(history)

                    # Create persistent alert
                    alert = Alert(
                        project_id=task.project_id,
                        severity=AlertSeverity.ERROR,
                        title=f"Smoke tests failed on {base_branch}",
                        message=f"{len(failed_tests)} test(s) failed: {', '.join(t['name'] for t in failed_tests[:5])}"
                                + (f" (+{len(failed_tests) - 5} more)" if len(failed_tests) > 5 else ""),
                        task_id=task.id,
                    )
                    session.add(alert)

                    await broadcast_to_project(
                        str(task.project_id), "task_updated",
                        {"task_id": str(plan_task.id), "status": TaskStatus.QUEUED}
                    )
                    await broadcast_to_project(
                        str(task.project_id), "alert_created",
                        {"alert_id": str(alert.id), "severity": alert.severity, "title": alert.title}
                    )
                    logger.error(f"[SMOKETEST] Spawned plan task {plan_task.id} + alert for {len(failed_tests)} failed smoke tests")
        except Exception as e:
            logger.warning(f"[SMOKETEST] Failed to spawn plan task after smoketest failure: {e}", exc_info=True)

    # 3c. Parse debug result, store in plan_output, and optionally spawn a fix task
    if payload.artifact_type == ArtifactType.DEBUG_RESULT and payload.logs:
        try:
            debug_data = json.loads(payload.logs)
            task.plan_output = debug_data
            session.add(task)

            # If confidence is high, auto-spawn a create task using the fix_prompt
            if debug_data.get("confidence") == "high" and debug_data.get("fix_prompt"):
                failed_task_id = (task.context or {}).get("failed_task_id", "")
                base_branch = ((task.payloads or [{}])[0]).get("base_branch", "main")
                debug_title = task.title or ""
                fix_title = debug_title.removeprefix("Debug: ") if debug_title.startswith("Debug: ") else debug_title
                fix_task = Task(
                    project_id=task.project_id,
                    title=f"Fix: {fix_title}",
                    description=debug_data["fix_prompt"],
                    parent_task_id=task.parent_task_id or task.id,
                    payloads=[{
                        "type": "create",
                        "prompt": debug_data["fix_prompt"],
                        "base_branch": base_branch,
                    }],
                    context={
                        "debug_task_id": str(task.id),
                        "failed_task_id": failed_task_id,
                        "root_cause": debug_data.get("root_cause", ""),
                    },
                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                    status=TaskStatus.QUEUED,
                    created_by="ORCHESTRATOR",
                )
                session.add(fix_task)
                await session.flush()
                await broadcast_to_project(
                    str(task.project_id),
                    "task_updated",
                    {"task_id": str(fix_task.id), "status": TaskStatus.QUEUED, "title": fix_task.title}
                )
                logger.info(f"[DEBUG] Spawned fix task {fix_task.id} from debug analysis (high confidence)")
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[DEBUG] Failed to parse debug result for task {task.id}: {e}", exc_info=True)

    # 4. External VM Termination
    # If the task is finished or failed, immediately kill the VM to save costs
    if payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
        # Record result for deployment monitoring
        if payload.status == TaskStatus.COMPLETED:
            record_task_result(success=True, task_id=str(task.id))
        else:
            failure_class = classify_failure(task, payload.logs)
            record_task_result(success=False, failure_class=failure_class, task_id=str(task.id))

        # Release Max plan account slot
        acct_id = (task.context or {}).get("max_plan_account_id")
        if acct_id:
            release_max_plan_account(acct_id)
            # Auto-disable account on rate limit — usage exhausted, stop wasting VMs
            # Re-enables automatically after 30min cooldown
            if payload.status == TaskStatus.FAILED and failure_class == "RATE_LIMIT":
                for acct in _max_plan_pool:
                    if acct["id"] == acct_id:
                        acct["enabled"] = False
                        acct["disabled_at"] = datetime.utcnow()
                        logger.warning(f"[POOL] Disabled account {acct['name']} — rate limited (will retry in 30min)")
                        break

        if task.vm_instance_id:
            background_tasks.add_task(delete_agent_vm, task.vm_instance_id)

        # Retry logic for failed tasks
        if payload.status == TaskStatus.FAILED and task.attempt_count < _runtime_config["max_retries"]:
            task.attempt_count += 1
            task.status = TaskStatus.QUEUED
            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex[:16]}"
            task.last_heartbeat = None
            task.started_at = None  # Fresh timeout window for the new VM
            session.add(task)

            await session.commit()
            
            await broadcast_to_project(
                str(task.project_id), 
                "task_updated", 
                {"task_id": str(task.id), "status": TaskStatus.QUEUED, "attempt_count": task.attempt_count}
            )
            return {"status": "retrying", "message": f"Task {task.id} failed, retrying (Attempt {task.attempt_count}/3)"}

        # Spawn a debug task for permanently failed tasks (retries exhausted)
        failed_task_payload = (task.payloads or [{}])[0]
        failed_task_type = failed_task_payload.get("type", "")
        should_auto_debug = (
            payload.status == TaskStatus.FAILED and  # Only for failed tasks (failure_class is unset for COMPLETED)
            failed_task_type != "debug" and  # Don't recursively debug debug tasks
            (task.context or {}).get("debug_task_id") is None and  # Don't re-debug fix tasks spawned by debug
            payload.logs and
            failure_class == "CODE_ERROR" and  # Only debug actual code errors, not infra/timeout/rate-limit
            (
                (task.context or {}).get("auto_debug") is True or
                task.parent_task_id is not None  # Part of a pipeline (plan/research subtask)
            )
        )
        if should_auto_debug:
            base_branch = failed_task_payload.get("base_branch", "main")
            debug_task = Task(
                project_id=task.project_id,
                title=f"Debug: {task.title}",
                description=f"Automated root cause analysis for failed task: {task.title}",
                parent_task_id=task.id,
                payloads=[{
                    "type": "debug",
                    "failed_task_id": str(task.id),
                    "failed_task_title": task.title,
                    "error_logs": (payload.logs or "")[:20000],
                    "base_branch": base_branch,
                }],
                context={
                    "failed_task_id": str(task.id),
                    "parent_base": base_branch,
                },
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
            )
            session.add(debug_task)
            await session.flush()
            await broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {"task_id": str(debug_task.id), "status": TaskStatus.QUEUED, "title": debug_task.title}
            )
            logger.error(f"[DEBUG] Spawned debug task {debug_task.id} for permanently failed task {task.id}")

        # Circuit breaker for autonomous tasks (CLOSED/OPEN/HALF-OPEN)
        if payload.status == TaskStatus.FAILED and task.created_by == "ORCHESTRATOR":
            project_key = str(task.project_id)
            breaker = _shared_state.get_circuit_breaker(project_key)
            breaker["failure_count"] += 1
            breaker["success_count"] = 0

            # HALF-OPEN test task failed → back to OPEN with doubled cooldown
            if breaker["state"] == "HALF_OPEN" and str(task.id) == breaker.get("half_open_task_id"):
                breaker["state"] = "OPEN"
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)  # cap at 2 hours
                breaker["opened_at"] = datetime.utcnow()
                breaker["half_open_task_id"] = None
                logger.warning(f"[CIRCUIT BREAKER] HALF-OPEN → OPEN for {project_key} (cooldown doubled to {breaker['cooldown_seconds']}s)")

            # CLOSED → OPEN after 3 consecutive failures
            elif breaker["state"] == "CLOSED" and breaker["failure_count"] >= 3:
                breaker["state"] = "OPEN"
                breaker["opened_at"] = datetime.utcnow()
                breaker["cooldown_seconds"] = breaker["base_cooldown"]
                await broadcast_to_project(project_key, "autonomous_paused",
                                           {"reason": f"Circuit breaker OPEN after {breaker['failure_count']} consecutive failures"})
                logger.warning(f"[CIRCUIT BREAKER] CLOSED → OPEN for {project_key} after {breaker['failure_count']} failures")
        # When review/fix fails permanently, cancel the original create task so rollup
        # skips it (via cancelled-child logic) instead of hanging forever.
        # Other children's work is preserved — only this PR is excluded from the rollup.
        if payload.status == TaskStatus.FAILED:
            task_payload = (task.payloads or [{}])[0]
            task_type = task_payload.get("type", "")
            if task_type in ("review", "fix"):
                original_task_id = ((task.context or {}).get("review_loop", {}).get("original_task_id", "")
                                    or task_payload.get("original_task_id", ""))
                if original_task_id:
                    try:
                        orig = await session.get(Task, uuid.UUID(original_task_id))
                        if orig and orig.status == TaskStatus.COMPLETED:
                            orig.status = TaskStatus.CANCELLED
                            orig.latest_status_message = f"Pipeline {task_type} task failed — PR excluded from rollup"
                            session.add(orig)
                            await session.commit()
                            # Trigger rollup check so parent can proceed with remaining children
                            if orig.parent_task_id:
                                await _check_and_trigger_rollup(orig, session)
                            logger.error(f"[PIPELINE] Cancelled original task {original_task_id} — {task_type} failed, skipping in rollup")
                    except Exception as e:
                        logger.warning(f"[PIPELINE] Failed to propagate failure to {original_task_id}: {e}", exc_info=True)

        if payload.status == TaskStatus.COMPLETED and task.created_by == "ORCHESTRATOR":
            project_key = str(task.project_id)
            breaker = _shared_state.get_circuit_breaker(project_key)
            breaker["failure_count"] = 0
            breaker["success_count"] += 1

            # HALF-OPEN test task succeeded → back to CLOSED
            if breaker["state"] == "HALF_OPEN" and str(task.id) == breaker.get("half_open_task_id"):
                breaker["state"] = "CLOSED"
                breaker["cooldown_seconds"] = breaker["base_cooldown"]
                breaker["half_open_task_id"] = None
                logger.warning(f"[CIRCUIT BREAKER] HALF-OPEN → CLOSED for {project_key} (test task succeeded)")
            elif breaker["state"] == "OPEN":
                # Shouldn't happen (OPEN blocks spawning), but reset if it does
                breaker["state"] = "CLOSED"

        # 5. Review Pipeline — auto-spawn review/fix tasks based on completed task type
        if payload.status == TaskStatus.COMPLETED:
            task_payload = (task.payloads or [{}])[0]
            task_type = task_payload.get("type", "create")
            review_loop = (task.context or {}).get("review_loop", {})

            # Create task completed with PR → route based on target branch
            if task_type in ("create", "refactor", "docs", "qa", "deps", "migrate", "") and payload.pr_url and payload.artifact_type == ArtifactType.PULL_REQUEST:
                pr_number = int(payload.pr_url.rstrip("/").split("/")[-1])
                short_id = str(task.id).replace('-', '')[:8]
                branch = f"blueprint/docs-{short_id}" if task_type == "docs" else f"blueprint/deps-{short_id}" if task_type == "deps" else f"blueprint/migrate-{short_id}" if task_type == "migrate" else f"blueprint/refactor-{short_id}" if task_type == "refactor" else f"blueprint/task-{short_id}"
                base_branch = task_payload.get("base_branch", "main")
                original_prompt = task_payload.get("prompt", task.description or "")
                is_intermediate = base_branch.startswith("research/") or base_branch.startswith("plan/")
                scope = (task.context or {}).get("scope", "unknown")

                if is_intermediate and scope == "small":
                    # Small scope on intermediate branch → security scan then auto-merge, skip review
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)

                        # Always run security scan before any merge
                        try:
                            scan_result = await scan_pr_for_injection(gh_token, owner, repo, pr_number)
                            logger.info(f"[SECURITY] Small-scope scan: {scan_result['summary']}")
                            if scan_result["blocked"]:
                                task.requires_approval = True
                                task.latest_status_message = f"Security scan blocked: {scan_result['summary']}"
                                session.add(task)
                                await session.commit()
                                if scan_result["findings"]:
                                    await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                                return {"status": "accepted"}
                            if scan_result["findings"]:
                                await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                        except Exception as e:
                            logger.error(f"[SECURITY] Small-scope scan failed for PR #{pr_number}, blocking: {e}", exc_info=True)
                            task.requires_approval = True
                            task.latest_status_message = f"Security scan failed — requires manual approval: {type(e).__name__}: {e!r}"[:500]
                            session.add(task)
                            await session.commit()
                            return {"status": "accepted"}

                        merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)
                        if merge_result.get("merged"):
                            task.context = {**(task.context or {}), "pr_merged": True}
                            session.add(task)
                            await session.commit()
                            await _cleanup_branches(task, session)
                            logger.info(f"[PIPELINE] Auto-merged PR #{pr_number} into {base_branch} (small scope, intermediate)")
                        else:
                            logger.error(f"[PIPELINE] Auto-merge failed for PR #{pr_number}: {merge_result.get('message')}")
                            # Fall through to review pipeline for conflict resolution
                            review_task = await _spawn_pipeline_task(
                                task, "review", session, pr_number, branch, base_branch,
                                original_prompt=original_prompt, original_task_id=str(task.id)
                            )
                            if not review_task:
                                task.latest_status_message = f"Auto-merge failed and review not spawned for PR #{pr_number} — iteration cap"
                                task.requires_approval = True
                                session.add(task)
                                await session.commit()
                                logger.error(f"[PIPELINE] WARNING: Auto-merge failed and review spawn returned None for PR #{pr_number}")
                    except Exception as e:
                        logger.error(f"[PIPELINE] Auto-merge error for PR #{pr_number}: {e}", exc_info=True)
                        # Fallback: spawn review so the pipeline doesn't die
                        try:
                            fallback_review = await _spawn_pipeline_task(
                                task, "review", session, pr_number, branch, base_branch,
                                original_prompt=original_prompt, original_task_id=str(task.id)
                            )
                            if not fallback_review:
                                task.latest_status_message = f"Pipeline stalled: auto-merge failed and review not spawned (iteration cap) for PR #{pr_number}"
                                task.requires_approval = True
                                session.add(task)
                                await session.commit()
                                logger.warning(f"[PIPELINE] WARNING: Fallback review spawn returned None for PR #{pr_number}")
                        except Exception as e:
                            logger.error(f"[PIPELINE] Fallback review spawn failed for PR #{pr_number}: {e}", exc_info=True)
                            task.latest_status_message = f"Pipeline stalled: auto-merge and review spawn both failed — {type(e).__name__}: {e!r}"[:500]
                            session.add(task)
                            await session.commit()
                else:
                    # Full pipeline: security scan → review (for main targets or medium+ scope)
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        scan_result = await scan_pr_for_injection(gh_token, owner, repo, pr_number)
                        logger.info(f"[SECURITY] {scan_result['summary']}")
                        if scan_result["blocked"]:
                            task.requires_approval = True
                            task.latest_status_message = scan_result["summary"]
                            session.add(task)
                            await session.commit()
                        if scan_result["findings"]:
                            await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                    except Exception as e:
                        logger.error(f"[SECURITY] Scan failed for PR #{pr_number}, flagging for approval: {e}", exc_info=True)
                        task.requires_approval = True
                        task.latest_status_message = f"Security scan failed — review will proceed but merge requires manual approval: {type(e).__name__}: {e!r}"[:500]
                        session.add(task)
                        await session.commit()

                    # PR file-path flagging: flag infrastructure/security-critical file changes
                    # Don't block here — let review run, block at merge time
                    try:
                        pr_files = await get_pr_files(gh_token, owner, repo, pr_number)
                        INFRA_FILES = {"backend/app/main.py", "backend/app/compute.py", "backend/app/models.py",
                                       "backend/app/database.py", "backend/app/security.py", "backend/app/auth.py",
                                       "agent/src/index.ts", "CLAUDE.md"}
                        CRITICAL_FILES = {"backend/app/security.py", "backend/app/auth.py", "CLAUDE.md"}
                        touched_infra = [f["filename"] for f in pr_files if f["filename"] in INFRA_FILES]
                        touched_critical = [f for f in touched_infra if f in CRITICAL_FILES]
                        if touched_infra:
                            task.context = {**(task.context or {}), "extended_monitoring": True, "risk_tier": "large",
                                            "touched_infra": touched_infra, "touched_critical": touched_critical}
                            task.latest_status_message = f"Infrastructure change flagged: {', '.join(touched_infra)}"
                            logger.info(f"[PIPELINE] PR #{pr_number} touches infra files: {touched_infra} (critical: {touched_critical})")
                            session.add(task)
                            await session.commit()
                            _shared_state.deploy_monitoring["is_extended"] = True
                    except Exception as e:
                        logger.info(f"[PIPELINE] Could not check PR files for infra flagging: {e}")

                    if not task.requires_approval:
                        review_task = await _spawn_pipeline_task(
                            task, "review", session, pr_number, branch, base_branch,
                            original_prompt=original_prompt, original_task_id=str(task.id)
                        )
                        if review_task:
                            logger.info(f"[REVIEW PIPELINE] Spawned review for PR #{pr_number} → task {review_task.id}")
                        else:
                            task.latest_status_message = f"Review not spawned for PR #{pr_number} — iteration cap reached"
                            task.requires_approval = True
                            session.add(task)
                            await session.commit()
                            logger.warning(f"[REVIEW PIPELINE] WARNING: Review spawn returned None for PR #{pr_number}")

            # Review task completed → parse verdict
            elif task_type == "review":
                pr_number = review_loop.get("pr_number") or task_payload.get("pr_number")
                branch = review_loop.get("branch") or task_payload.get("branch")
                base_branch = review_loop.get("base_branch") or task_payload.get("base_branch", "main")
                original_task_id = review_loop.get("original_task_id", "") or task_payload.get("original_task_id", "")
                original_prompt = task_payload.get("original_prompt", "")

                if not pr_number or not branch:
                    logger.info(f"[REVIEW PIPELINE] Review task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline")
                    task.latest_status_message = "Review pipeline error: missing pr_number or branch"
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip review pipeline if PR is already merged or closed
                try:
                    gh_token_check, owner_check, repo_check = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(gh_token_check, owner_check, repo_check, pr_number)
                    pr_state = pr_data_check.get("state", "open")
                    if pr_state == "closed" or pr_data_check.get("merged"):
                        merged_label = "merged" if pr_data_check.get("merged") else "closed"
                        print(f"[REVIEW PIPELINE] PR #{pr_number} already {merged_label} — skipping review pipeline for task {task.id}")
                        task.latest_status_message = f"PR #{pr_number} already {merged_label} — review skipped"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                except Exception as e:
                    print(f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding with review")

                verdict_text = payload.logs or task.latest_status_message or ""
                verdict = "REQUEST_CHANGES"
                feedback = verdict_text

                # Parse verdict: prefer structured verdict_json field, fall back to log parsing
                import re
                verdict_parsed = False

                # Method 1: verdict_json field (from /tmp/verdict.json on the agent)
                if payload.verdict_json:
                    try:
                        vj = json.loads(payload.verdict_json)
                        if vj.get("verdict"):
                            verdict = vj["verdict"]
                            feedback = vj.get("feedback", verdict_text)
                            verdict_parsed = True
                            logger.info(f"[REVIEW PIPELINE] Verdict from verdict_json field: {verdict}")
                    except (json.JSONDecodeError, TypeError):
                        logger.error(f"[REVIEW PIPELINE] verdict_json field present but invalid for task {task.id}")

                # Method 2: Parse JSON from log output
                if not verdict_parsed:
                    try:
                        # Find all JSON objects containing "verdict" key (handles any key order)
                        for m in re.finditer(r'\{[^{}]*"verdict"', verdict_text):
                            start = m.start()
                            depth = 0
                            for i in range(start, len(verdict_text)):
                                if verdict_text[i] == '{':
                                    depth += 1
                                elif verdict_text[i] == '}':
                                    depth -= 1
                                    if depth == 0:
                                        try:
                                            candidate = json.loads(verdict_text[start:i+1])
                                            if "verdict" in candidate:
                                                verdict = candidate["verdict"]
                                                feedback = candidate.get("feedback", verdict_text)
                                                verdict_parsed = True
                                        except json.JSONDecodeError:
                                            pass
                                        break
                    except Exception as e:
                        logger.error(f"[REVIEW PIPELINE] Verdict parsing error for task {task.id}: {e}", exc_info=True)

                if not verdict_parsed:
                    # Fallback: look for APPROVE/REQUEST_CHANGES as standalone words
                    upper_text = verdict_text.upper()
                    # Only match the verdict keywords, not surrounding review text
                    approve_idx = upper_text.rfind('"APPROVE"')
                    rc_idx = upper_text.rfind('"REQUEST_CHANGES"')
                    if approve_idx > rc_idx:
                        verdict = "APPROVE"
                    elif rc_idx > approve_idx:
                        verdict = "REQUEST_CHANGES"
                    logger.warning(f"[REVIEW PIPELINE] WARNING: No verdict JSON found for task {task.id} (len={len(verdict_text)}), fallback verdict={verdict}")

                # Post the review to the PR as a comment
                try:
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                    review_comment = f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                    await create_pr_comment(gh_token, owner, repo, pr_number, review_comment)
                except Exception as e:
                    logger.warning(f"[REVIEW PIPELINE] Failed to post review comment: {e}", exc_info=True)

                action_taken = False
                if verdict == "APPROVE":
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        # Validate PR head branch matches before merging
                        pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
                        pr_head = pr_data.get("head", {}).get("ref", "")
                        if pr_head and pr_head != branch:
                            logger.info(f"[REVIEW PIPELINE] Branch mismatch: reviewed '{branch}' but PR #{pr_number} head is '{pr_head}' — skipping merge")
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint Review Pipeline**: Merge aborted — reviewed branch `{branch}` does not match PR head `{pr_head}`.")
                            task.latest_status_message = f"Branch mismatch: reviewed {branch}, PR head is {pr_head}"
                            session.add(task)
                            await session.commit()
                            raise ValueError(f"Branch mismatch: {branch} vs {pr_head}")

                        # Check CI status before merging (only for PRs targeting main)
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        is_main_merge = base_branch == "main"
                        ci_status = await get_check_runs(gh_token, owner, repo, head_sha) if (head_sha and is_main_merge) else None

                        if ci_status and ci_status["status"] != "completed":
                            # CI still running — park task in EVALUATING (no VM consumed)
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = f"Review approved, waiting for CI on PR #{pr_number}..."
                            task.context = {**(task.context or {}), "review_loop": {
                                **review_loop, "ci_waiting": True, "verdict": "APPROVE",
                                "head_sha": head_sha, "pr_number": pr_number,
                                "branch": branch, "base_branch": base_branch,
                                "original_task_id": original_task_id,
                                "ci_wait_started": datetime.utcnow().isoformat()}}
                            session.add(task)
                            await session.commit()
                            logger.info(f"[CI] PR #{pr_number} approved, CI pending — waiting")
                            action_taken = True
                            return {"status": "accepted"}

                        if ci_status and ci_status["conclusion"] == "failure":
                            # CI failed — spawn fix with CI errors
                            ci_errors = ci_status["failed_logs"] or "CI checks failed (no details available)"
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint CI**: Checks failed — spawning fix task.\n\n```\n{ci_errors[:2000]}\n```")
                            ci_fix_prompt = (
                                f"CI checks failed on PR #{pr_number} (code review already approved).\n\n"
                                f"CI errors:\n{ci_errors}\n\n"
                                f"Fix only the build/lint/test errors. The code review is already "
                                f"approved — don't change logic, just fix CI failures."
                            )
                            fix_task = await _spawn_pipeline_task(
                                task, "fix", session, pr_number, branch, base_branch,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=original_task_id
                            )
                            if fix_task:
                                logger.error(f"[CI] PR #{pr_number} CI failed, spawned fix → {fix_task.id}")
                            action_taken = True
                            return {"status": "accepted"}

                        # CI passed (or no CI checks exist) — proceed to merge
                        if ci_status and ci_status["conclusion"] == "success":
                            logger.info(f"[CI] PR #{pr_number} CI passed")

                        # Critical files gate: block merge into main if security-critical files were touched
                        # Intermediate branches (research/, plan/) don't need human approval
                        is_main_target = base_branch == "main"
                        if original_task_id and is_main_target:
                            try:
                                orig_task = await session.get(Task, uuid.UUID(original_task_id))
                                critical_touched = (orig_task.context or {}).get("touched_critical", []) if orig_task else []
                                if critical_touched:
                                    orig_task.requires_approval = True
                                    orig_task.status = TaskStatus.EVALUATING
                                    orig_task.latest_status_message = f"Review approved — awaiting human approval to merge (critical files: {', '.join(critical_touched)})"
                                    session.add(orig_task)
                                    await session.commit()
                                    await broadcast_to_project(str(orig_task.project_id), "task_updated",
                                                               {"task_id": str(orig_task.id), "status": TaskStatus.EVALUATING})
                                    await create_pr_comment(gh_token, owner, repo, pr_number,
                                        f"**Blueprint Review Pipeline**: Review approved, but PR touches security-critical files ({', '.join(critical_touched)}). Merge requires human approval.")
                                    logger.error(f"[PIPELINE] PR #{pr_number} review approved but merge blocked — critical files: {critical_touched}")
                                    action_taken = True
                                    return {"status": "accepted"}
                            except Exception as e:
                                logger.error(f"[PIPELINE] Error checking critical files for merge gate: {e}", exc_info=True)

                        merge_result = await merge_pull_request(gh_token, owner, repo, pr_number)

                        if merge_result.get("merged"):
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                "**Blueprint Review Pipeline**: PR auto-merged.")
                            # Mark original create task so dependents can proceed
                            if original_task_id:
                                try:
                                    orig = await session.get(Task, uuid.UUID(original_task_id))
                                    if orig:
                                        orig.context = {**(orig.context or {}), "pr_merged": True}
                                        # If this was a rollup PR review, mark the parent as COMPLETED
                                        is_rollup_merge = orig.status == TaskStatus.EVALUATING
                                        if is_rollup_merge:
                                            orig.status = TaskStatus.COMPLETED
                                            orig.latest_status_message = "Rollup PR merged"
                                        session.add(orig)
                                        await session.commit()
                                        # Clean up branches (staging + feature)
                                        await _cleanup_branches(orig, session)
                                        # Spawn a smoketest after rollup merges to main
                                        if is_rollup_merge and base_branch == "main":
                                            try:
                                                type_model = _runtime_config.get("task_type_models", {}).get("smoketest") or None
                                                type_effort = _runtime_config.get("task_type_efforts", {}).get("smoketest") or None
                                                smoketest_task = Task(
                                                    project_id=task.project_id,
                                                    title=f"Smoketest after rollup merge to {base_branch}",
                                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                                    payloads=[{
                                                        "type": "smoketest",
                                                        "base_branch": base_branch,
                                                        **({"model": type_model} if type_model else {}),
                                                        **({"effort": type_effort} if type_effort else {}),
                                                    }],
                                                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex[:16]}",
                                                    status=TaskStatus.QUEUED,
                                                    created_by="ORCHESTRATOR",
                                                )
                                                session.add(smoketest_task)
                                                await session.commit()
                                                await session.refresh(smoketest_task)
                                                history = TaskHistory(
                                                    task_id=smoketest_task.id, action="SPAWNED",
                                                    details={"spawned_by": "POST_ROLLUP_MERGE", "base_branch": base_branch}
                                                )
                                                session.add(history)
                                                await session.commit()
                                                await broadcast_to_project(
                                                    str(task.project_id), "task_updated",
                                                    {"task_id": str(smoketest_task.id), "status": TaskStatus.QUEUED}
                                                )
                                                logger.info(f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after rollup merge to {base_branch}")
                                            except Exception as e:
                                                logger.warning(f"[SMOKETEST] Failed to spawn smoketest after rollup merge: {e}", exc_info=True)
                                except Exception as e:
                                    logger.warning(f"[REVIEW PIPELINE] Failed to set pr_merged on {original_task_id}: {e}", exc_info=True)
                            logger.info(f"[REVIEW PIPELINE] PR #{pr_number} approved and merged")
                            action_taken = True
                        else:
                            # Merge blocked — spawn fix task to rebase and resolve conflicts
                            merge_msg = merge_result.get("message", "unknown")
                            # Fetch PR file list for conflict context
                            try:
                                pr_files = await get_pr_files(gh_token, owner, repo, pr_number)
                                files_list = "\n".join(f"  - {f['filename']} ({f['status']})" for f in pr_files)
                            except Exception as e:
                                logger.warning(f"[REVIEW PIPELINE] Failed to fetch file list for PR #{pr_number}: {e}")
                                files_list = "  (could not fetch file list)"
                            await create_pr_comment(gh_token, owner, repo, pr_number,
                                f"**Blueprint Review Pipeline**: Merge blocked — {merge_msg}. Spawning fix task to rebase and resolve conflicts.")
                            fix_prompt = (
                                f"This PR (#{pr_number}) was approved but could not be merged.\n"
                                f"Error: {merge_msg}\n\n"
                                f"Files changed in this PR:\n{files_list}\n\n"
                                f"Steps to resolve:\n"
                                f"1. Run: git fetch origin {base_branch}\n"
                                f"2. Run: git rebase origin/{base_branch}\n"
                                f"3. If rebase conflicts occur, resolve them in each file — keep the intent of the PR changes while incorporating upstream updates\n"
                                f"4. Run: git rebase --continue after resolving each conflicting file\n"
                                f"5. If rebase is too complex, abort with git rebase --abort, then use git merge origin/{base_branch} and resolve conflict markers\n\n"
                                f"Preserve the intent of the existing PR changes. Do not alter logic unless necessary to resolve conflicts."
                            )
                            fix_task = await _spawn_pipeline_task(
                                task, "fix", session, pr_number, branch, base_branch,
                                fix_prompt=fix_prompt,
                                original_task_id=original_task_id
                            )
                            if fix_task:
                                logger.error(f"[REVIEW PIPELINE] Merge blocked for PR #{pr_number}, spawned rebase fix → task {fix_task.id}")
                            action_taken = True
                    except Exception as e:
                        logger.warning(f"[REVIEW PIPELINE] Failed to merge PR #{pr_number}: {e}", exc_info=True)
                        task.latest_status_message = f"Review approved but merge/CI-check failed: {type(e).__name__}: {e!r}"[:500]
                        session.add(task)
                        await session.commit()
                else:
                    # REQUEST_CHANGES — include CI errors if available (don't wait)
                    ci_errors_text = ""
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                        pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        if head_sha:
                            ci_check = await get_check_runs(gh_token, owner, repo, head_sha)
                            if ci_check.get("conclusion") == "failure" and ci_check.get("failed_logs"):
                                ci_errors_text = f"\n\nCI build errors (also need fixing):\n{ci_check['failed_logs']}"
                    except Exception as e:
                        logger.error(f"[REVIEW PIPELINE] Could not fetch CI errors for fix context: {e}", exc_info=True)
                    fix_task = await _spawn_pipeline_task(
                        task, "fix", session, pr_number, branch, base_branch,
                        fix_prompt=f"A code review requested changes. Please address the following feedback:\n\n{feedback}{ci_errors_text}",
                        original_task_id=original_task_id,
                        current_feedback=feedback
                    )
                    if fix_task:
                        logger.info(f"[REVIEW PIPELINE] Spawned fix for PR #{pr_number} → task {fix_task.id}")
                    else:
                        task.latest_status_message = f"Fix not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        await session.commit()
                        logger.warning(f"[REVIEW PIPELINE] WARNING: Fix spawn returned None for PR #{pr_number}")
                    action_taken = True

                if not action_taken:
                    task.latest_status_message = f"Review completed but no action taken (verdict={verdict}) — check orchestrator logs"
                    session.add(task)
                    await session.commit()
                    logger.warning(f"[REVIEW PIPELINE] WARNING: Review {task.id} for PR #{pr_number} completed with no downstream action (verdict={verdict})")

            # Fix task completed → security scan → spawn new review
            elif task_type == "fix":
                pr_number = review_loop.get("pr_number") or task_payload.get("pr_number")
                branch = review_loop.get("branch") or task_payload.get("branch")
                base_branch = review_loop.get("base_branch") or task_payload.get("base_branch", "main")
                original_task_id = review_loop.get("original_task_id", "") or task_payload.get("original_task_id", "")

                if not pr_number or not branch:
                    logger.info(f"[REVIEW PIPELINE] Fix task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline")
                    task.latest_status_message = "Fix pipeline error: missing pr_number or branch"
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip if PR is already merged or closed
                try:
                    gh_token_check, owner_check, repo_check = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(gh_token_check, owner_check, repo_check, pr_number)
                    if pr_data_check.get("state") == "closed" or pr_data_check.get("merged"):
                        merged_label = "merged" if pr_data_check.get("merged") else "closed"
                        print(f"[REVIEW PIPELINE] PR #{pr_number} already {merged_label} — skipping fix re-review for task {task.id}")
                        task.latest_status_message = f"PR #{pr_number} already {merged_label} — re-review skipped"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                except Exception as e:
                    print(f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding")

                # Security scan fix output before re-review
                scan_blocked = False
                try:
                    gh_token, owner, repo = await get_github_token_for_task(task.id, session)
                    scan_result = await scan_pr_for_injection(gh_token, owner, repo, pr_number)
                    logger.info(f"[SECURITY] {scan_result['summary']}")
                    if scan_result["blocked"]:
                        scan_blocked = True
                        task.requires_approval = True
                        task.latest_status_message = scan_result["summary"]
                        session.add(task)
                        await session.commit()
                    if scan_result["findings"]:
                        await create_pr_comment(gh_token, owner, repo, pr_number, scan_result["report"])
                except Exception as e:
                    logger.error(f"[SECURITY] Scan failed for fix PR #{pr_number}, flagging for approval: {e}", exc_info=True)
                    task.requires_approval = True
                    task.latest_status_message = f"Security scan failed — review will proceed but merge requires manual approval: {type(e).__name__}: {e!r}"[:500]
                    session.add(task)
                    await session.commit()

                if not scan_blocked:
                    review_task = await _spawn_pipeline_task(
                        task, "review", session, pr_number, branch, base_branch,
                        original_task_id=original_task_id
                    )
                    if review_task:
                        logger.info(f"[REVIEW PIPELINE] Spawned review after fix for PR #{pr_number} → task {review_task.id}")
                    else:
                        task.latest_status_message = f"Fix applied but review not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        await session.commit()
                        logger.warning(f"[REVIEW PIPELINE] WARNING: Review spawn after fix returned None for PR #{pr_number}")

            # Plan task completed → decompose into subtasks
            elif task_type == "plan":
                plan_logs = payload.logs or task.latest_status_message or ""
                await _handle_plan_completion(task, plan_logs, session)

            # Research task completed → create tasks from findings
            elif task_type == "research":
                research_logs = payload.logs or task.latest_status_message or ""
                await _handle_research_completion(task, research_logs, session)

            # Security audit completed → spawn fix tasks for critical/high findings
            elif task_type == "security_audit":
                audit_logs = payload.logs or task.latest_status_message or ""
                await _handle_security_audit_completion(task, audit_logs, session)

            # Perf audit completed → spawn fix tasks for high-severity findings
            elif task_type == "perf":
                perf_logs = payload.logs or task.latest_status_message or ""
                await _handle_perf_completion(task, perf_logs, session)

            # Investigation completed → store report, no child tasks
            elif payload.artifact_type == ArtifactType.INVESTIGATION_REPORT:
                await _handle_investigation_completion(task, payload, session)

        # 6. Parent task rollup — delegated to shared function
        if task.parent_task_id and payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
            await _check_and_trigger_rollup(task, session)

        # 7. Project State Aggregation
        # Only consider executable tasks (those with a VM) for aggregation.
        # Parent/logical tasks are updated to match the project outcome.
        all_tasks_stmt = select(Task).where(Task.project_id == task.project_id)
        all_tasks = (await session.execute(all_tasks_stmt)).scalars().all()
        executable_statuses = [t.status for t in all_tasks if t.vm_instance_id]
        parent_tasks = [t for t in all_tasks if not t.vm_instance_id and t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]]

        project_stmt = select(Project).where(Project.id == task.project_id)
        project = (await session.execute(project_stmt)).scalars().first()

        if project and project.status != ProjectStatus.CANCELLED and executable_statuses:
            new_project_status = None
            if all(s == TaskStatus.COMPLETED for s in executable_statuses):
                new_project_status = ProjectStatus.COMPLETED
            elif any(s == TaskStatus.FAILED for s in executable_statuses):
                new_project_status = ProjectStatus.FAILED
            elif all(s in [TaskStatus.COMPLETED, TaskStatus.CANCELLED] for s in executable_statuses):
                new_project_status = ProjectStatus.CANCELLED

            if new_project_status:
                project.status = new_project_status
                session.add(project)
                # Update parent/logical tasks to match the project outcome
                terminal_task_status = TaskStatus.COMPLETED if new_project_status == ProjectStatus.COMPLETED else TaskStatus.FAILED if new_project_status == ProjectStatus.FAILED else TaskStatus.CANCELLED
                for pt in parent_tasks:
                    pt.status = terminal_task_status
                    session.add(pt)
                await session.commit()
                await broadcast_to_project(str(project.id), "project_updated", {"status": new_project_status})

    # Final commit for any deferred changes (status, artifacts, spawned tasks)
    if session.dirty or session.new:
        await session.commit()

    return {
        "status": "received",
        "message": f"Status update for task {payload.task_id} recorded."
    }

async def get_github_token_for_task(task_id: uuid.UUID, session: AsyncSession) -> tuple[str, str, str]:
    stmt = select(Task).where(Task.id == task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    project_stmt = select(Project).where(Project.id == task.project_id)
    project = (await session.execute(project_stmt)).scalars().first()
    if not project or not project.user_id:
        raise HTTPException(status_code=404, detail="Project or user not found")
        
    if not project.github_owner or not project.github_repo:
        raise HTTPException(status_code=403, detail="Project does not have an authorized GitHub repository")
        
    integration_stmt = select(UserIntegration).where(UserIntegration.user_id == project.user_id)
    integration = (await session.execute(integration_stmt)).scalars().first()

    if integration and integration.github_installation_id:
        token = await generate_installation_access_token(
            integration.github_installation_id,
            repositories=[project.github_repo]
        )
    elif integration and integration.github_pat:
        token = await get_user_secret(integration.github_pat)
    else:
        # Fallback: discover GitHub App installation for this repo directly
        # This handles admin-created tasks where no UserIntegration exists
        from app.github import get_repo_installation_token
        token = await get_repo_installation_token(project.github_owner, project.github_repo)
        if not token:
            raise HTTPException(
                status_code=400,
                detail="GitHub integration not found. Ensure the GitHub App is installed on this repository, or register a PAT via /api/github/pat."
            )
        
    return token, project.github_owner, project.github_repo

@app.get("/api/agent/github/token")
async def agent_github_get_token(
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

@app.post("/api/agent/github/commit-status")
async def agent_github_commit_status(
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

@app.post("/api/agent/github/pr-comment")
async def agent_github_pr_comment(
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

@app.post("/api/agent/github/pr-review-comment")
async def agent_github_pr_review_comment(
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

@app.post("/api/agent/github/issues")
async def agent_github_create_issue(
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

@app.get("/api/agent/github/issues")
async def agent_github_get_issues(
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

@app.post("/api/agent/github/branches")
async def agent_github_create_branch(
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

@app.post("/api/agent/github/pull-requests")
async def agent_github_create_pr(
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
    result = await create_pull_request(
        token=gh_token,
        owner=owner,
        repo=repo,
        title=req.title,
        head=req.head,
        base=req.base,
        body=req.body
    )
    return {"status": "success", "data": result}

@app.post("/api/projects/{project_id}/cancel")
@limiter.limit("10/minute")
async def cancel_project(
    request: Request,
    project_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Halts the project, marks pending tasks as cancelled, and immediately terminates all associated running VMs.
    """
    project_stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(project_stmt)).scalars().first()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    if project.status in [ProjectStatus.COMPLETED, ProjectStatus.FAILED, ProjectStatus.CANCELLED]:
        return {"status": "ignored", "message": f"Project is already in terminal state: {project.status}"}
        
    project.status = ProjectStatus.CANCELLED
    session.add(project)
    
    tasks_stmt = select(Task).where(
        Task.project_id == project_id,
        Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING])
    )
    active_tasks = (await session.execute(tasks_stmt)).scalars().all()

    for task in active_tasks:
        task.status = TaskStatus.CANCELLED
        session.add(task)

        if task.vm_instance_id:
            background_tasks.add_task(delete_agent_vm, task.vm_instance_id)
            
        await broadcast_to_project(
            str(project_id), 
            "task_updated", 
            {"task_id": str(task.id), "status": TaskStatus.CANCELLED}
        )
        
    await session.commit()
    await broadcast_to_project(str(project_id), "project_updated", {"status": ProjectStatus.CANCELLED})
    
    return {"status": "cancelled", "message": f"Project {project_id} and {len(active_tasks)} tasks cancelled."}

@app.get("/api/projects/{project_id}/status")
@limiter.limit("60/minute")
async def get_project_status(
    request: Request,
    project_id: uuid.UUID, 
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Endpoint for the frontend to poll or stream the current status of the deployment 
    and agent activities.
    """
    project_stmt = select(Project).where(Project.id == project_id, Project.user_id == current_user["user_id"])
    project = (await session.execute(project_stmt)).scalars().first()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or unauthorized")
        
    tasks_stmt = select(Task).where(Task.project_id == project_id)
    tasks = (await session.execute(tasks_stmt)).scalars().all()
    
    return {
        "project_id": project.id,
        "name": project.name,
        "status": project.status, 
        "tasks": [
            {
                "task_id": t.id,
                "status": t.status,
                "vm_instance_id": t.vm_instance_id,
                "attempt_count": t.attempt_count
            } for t in tasks
        ]
    }

class GitHubPATInstallRequest(BaseModel):
    pat: str = Field(..., min_length=10)

@app.post("/api/github/pat")
@limiter.limit("5/minute")
async def register_github_pat(
    request: Request,
    req: GitHubPATInstallRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """
    Registers a GitHub Fine-Grained PAT for the current user.
    """
    # Validate PAT
    await validate_fine_grained_pat(req.pat)
    
    # Store the PAT securely in GCP Secret Manager
    secret_name = await store_user_secret(current_user["user_id"], "github-pat", req.pat)
    
    stmt = select(UserIntegration).where(UserIntegration.user_id == current_user["user_id"])
    integration = (await session.execute(stmt)).scalars().first()
    
    if integration:
        integration.github_pat = secret_name
        integration.updated_at = datetime.utcnow()
    else:
        integration = UserIntegration(
            user_id=current_user["user_id"],
            github_pat=secret_name
        )
    
    session.add(integration)
    await session.commit()
    
    return {"status": "success", "message": "GitHub PAT registered successfully."}

@app.get("/api/github/oauth/authorize")
@limiter.limit("10/minute")
async def github_oauth_authorize(
    request: Request,
    current_user: dict = Depends(get_current_user)
):
    """
    Returns the GitHub OAuth URL. The frontend redirects the user there.
    A signed state token ties the OAuth flow to the authenticated user.
    """
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured (GITHUB_CLIENT_ID missing)")

    # Sign a state token so the callback can verify which user initiated the flow
    import jwt as pyjwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    state = pyjwt.encode(
        {"sub": current_user["user_id"], "exp": datetime.utcnow() + timedelta(minutes=10), "type": "github_oauth"},
        AGENT_JWT_SECRET, algorithm=AGENT_JWT_ALGORITHM
    )

    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&state={state}"
        f"&allow_signup=false"
    )
    return {"url": url}


@app.get("/api/github/oauth/callback")
@limiter.limit("10/minute")
async def github_oauth_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """
    GitHub OAuth callback. Exchanges the code for a user token, verifies the user's
    installations, and auto-registers them. This proves the user actually owns the
    GitHub account — no spoofing possible.
    """
    admin_url = os.getenv("ADMIN_UI_URL", "https://blueprint-admin-9c6b7.web.app")

    # Verify the state token to identify the Firebase user who started the flow
    import jwt as pyjwt
    from app.auth import AGENT_JWT_SECRET, AGENT_JWT_ALGORITHM
    try:
        state_payload = pyjwt.decode(state, AGENT_JWT_SECRET, algorithms=[AGENT_JWT_ALGORITHM])
        if state_payload.get("type") != "github_oauth":
            return RedirectResponse(url=f"{admin_url}?github_error=invalid_state")
        firebase_user_id = state_payload["sub"]
    except pyjwt.ExpiredSignatureError:
        return RedirectResponse(url=f"{admin_url}?github_error=state_expired")
    except pyjwt.PyJWTError:
        return RedirectResponse(url=f"{admin_url}?github_error=invalid_state")

    try:
        # Exchange code for GitHub user access token
        token_data = await exchange_oauth_code(code)
        user_token = token_data["access_token"]

        # Get the GitHub user's profile to confirm identity
        await get_github_user(user_token)

        # Discover which of this App's installations the user can access
        installations = await get_user_installations(user_token)
        if not installations:
            return RedirectResponse(url=f"{admin_url}?github_error=no_installations")

        # Use the first installation (most users have one)
        installation = installations[0]
        installation_id = str(installation["id"])
        account_name = installation["account"]["login"]

        # Save to UserIntegration
        stmt = select(UserIntegration).where(UserIntegration.user_id == firebase_user_id)
        integration = (await session.execute(stmt)).scalars().first()

        if integration:
            integration.github_installation_id = installation_id
            integration.github_account_name = account_name
            integration.updated_at = datetime.utcnow()
        else:
            integration = UserIntegration(
                user_id=firebase_user_id,
                github_installation_id=installation_id,
                github_account_name=account_name
            )

        session.add(integration)
        await session.commit()

        return RedirectResponse(url=f"{admin_url}?github_connected={account_name}")

    except Exception as e:
        logger.error(f"[GITHUB OAUTH] Error: {e}", exc_info=True)
        return RedirectResponse(url=f"{admin_url}?github_error=failed")

@app.post("/api/github/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
):
    """
    Receives webhooks from the GitHub App (e.g., push, pull_request).
    """
    signature = request.headers.get("x-hub-signature-256")
    if not signature or not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Missing signature or webhook secret")
        
    payload = await request.body()
    
    # Verify signature
    expected_signature = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
        
    event_type = request.headers.get("x-github-event")
    data = json.loads(payload)
    
    logger.info(f"[WEBHOOK] Received {event_type} event")

    # Post-merge trigger: check if we should spawn autonomous research
    if event_type == "pull_request":
        action = data.get("action")
        merged = data.get("pull_request", {}).get("merged", False)

        if action == "closed" and merged:
            repo_full = data.get("repository", {}).get("full_name", "")
            if "/" in repo_full:
                owner, repo = repo_full.split("/", 1)
                # Verify repo belongs to a registered project
                project_check = await session.execute(
                    select(Project).where(
                        Project.github_owner == owner,
                        Project.github_repo == repo,
                    )
                )
                if not project_check.scalars().first():
                    logger.info(f"[WEBHOOK] Ignoring event for unregistered repo {repo_full}")
                    return {"status": "ignored"}
                background_tasks.add_task(_check_autonomous_research, owner, repo)

    return {"status": "received"}

# --- Static Files for Frontend (One-Line Deploy) ---
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    # Mount the assets directory specifically
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    
    # Serve index.html for all other non-API routes (SPA fallback)
    # Use api_route to handle all methods — prevents 405 for missing API routes
    # (a GET-only catch-all causes Starlette to return 405 for POST/PATCH/DELETE
    # on paths not registered in any router, even if those paths start with "api/")
    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def serve_spa(full_path: str, request: Request = None):
        # Don't intercept API routes or WebSocket upgrade requests
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        if request and request.headers.get("upgrade", "").lower() == "websocket":
            raise HTTPException(status_code=404, detail="WebSocket route")
            
        # Check if the requested file exists (e.g., favicon.ico, robots.txt)
        file_path = os.path.abspath(os.path.join(static_dir, full_path))
        
        # Ensure the resolved path is strictly within the static directory
        if not file_path.startswith(os.path.abspath(static_dir)):
            raise HTTPException(status_code=403, detail="Access denied")
            
        if os.path.isfile(file_path):
            return FileResponse(file_path)
            
        # Fallback to index.html for SPA routing
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)
            
        raise HTTPException(status_code=404, detail="Not found")
