import copy
import httpx
import os
import re
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Header, BackgroundTasks, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
import ipaddress
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func as _func
from sqlmodel import select
import uuid
import asyncio
import json
from datetime import datetime, timedelta, timezone


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


from app.database import init_db, get_session, AsyncSessionLocal, engine
from app.models import Project, Task, Artifact, TaskHistory, ProjectStatus, TaskStatus, ArtifactType, UserIntegration, MaxPlanAccount, DeploymentState, DeploymentStatus, Alert, AlertSeverity, _on_task_terminal_callbacks
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
import time
import hmac
import hashlib
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.shared_state import limiter

logger = logging.getLogger(__name__)

# --- Runtime Config & Autonomous State (shared with admin.py) ---
from app.shared_state import runtime_config as _runtime_config
import app.shared_state as _shared_state

# Expected schema revision — must match the latest alembic migration.
# Update this when adding new migrations.
EXPECTED_SCHEMA_REVISION = "0020"

# --- Task Priority Map ---
# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}

# Deadlock detection throttle — only check every 60 seconds
_last_deadlock_check: float = 0.0

# GCP VM count cache — updated by cleanup service, used by orchestrator concurrency check
_last_gcp_vm_count: int = 0
_last_gcp_vm_check: float = 0.0
_GCP_VM_CHECK_INTERVAL: float = 120.0  # Seconds between GCP VM count refreshes

# Reaper: track tasks skipped by skip_locked to force-reap after repeated skips
_reaper_skip_counts: Dict[uuid.UUID, int] = {}

# --- Max Plan Account Pool ---
# In-memory cache of DB-persisted accounts. Keeps active_tasks count (runtime only).
# Each entry: {id, name, oauth_token, claude_config_json, enabled, active_tasks, total_tasks, token_created_at}
_max_plan_pool: List[Dict[str, Any]] = []
_max_plan_pool_lock = asyncio.Lock()

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
            # Resolve Secret Manager resource names to actual tokens
            token = acct.oauth_token
            if token and token.startswith("projects/"):
                try:
                    from app.secrets_manager import get_user_secret
                    token = await get_user_secret(token)
                except Exception as e:
                    logger.error(f"[POOL] Failed to fetch token from Secret Manager for account {acct.name}: {e}")
                    token = None
            _max_plan_pool.append({
                "id": acct_id,
                "name": acct.name,
                "oauth_token": token,
                "claude_config_json": acct.claude_config_json,
                "enabled": acct.enabled,
                "active_tasks": active_per_account.get(acct_id, 0),
                "total_tasks": acct.total_tasks,
                "token_created_at": acct.token_created_at.isoformat() if acct.token_created_at else None,
            })
        total_active = sum(active_per_account.values())
        logger.info(f"[STARTUP] Loaded {len(_max_plan_pool)} Max plan accounts ({total_active} active tasks reconciled)")

async def select_max_plan_account() -> Optional[Dict[str, Any]]:
    """Select the Max plan account with the lowest active task count.
    Pre-increments active_tasks atomically under lock to prevent races."""
    async with _max_plan_pool_lock:
        available = [a for a in _max_plan_pool if a.get("enabled", True)]
        if not available:
            return None
        account = min(available, key=lambda a: a["active_tasks"])
        account["active_tasks"] = account.get("active_tasks", 0) + 1
        return account

async def release_max_plan_account(account_id: str):
    """Decrement active tasks for an account when a task finishes."""
    async with _max_plan_pool_lock:
        for account in _max_plan_pool:
            if account["id"] == account_id:
                if account["active_tasks"] <= 0:
                    logger.warning(f"[POOL] Account {account['name']} ({account_id}) release underflow — active_tasks was {account['active_tasks']}")
                account["active_tasks"] = max(0, account["active_tasks"] - 1)
                break

async def recheck_disabled_accounts():
    """Re-enable accounts that were disabled by rate limiting after a cooldown.
    Usage windows typically reset after some time — re-enable and let the next
    task attempt determine if usage has actually recovered."""
    async with _max_plan_pool_lock:
        now = _utcnow()
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


async def _ensure_vm_deleted(task_or_vm_id, *, reason: str = "terminal_transition") -> bool:
    """Guarantee VM deletion for any terminal state transition.

    Accepts either a Task object or a raw vm_instance_id string.
    Retries up to 3 times with exponential backoff.
    Returns True if deletion succeeded or was unnecessary.
    """
    if isinstance(task_or_vm_id, Task):
        vm_id = task_or_vm_id.vm_instance_id
        task_id = str(task_or_vm_id.id)
    else:
        vm_id = task_or_vm_id
        task_id = "unknown"

    if not vm_id:
        return True  # No VM to delete

    logger.info(f"[VM CLEANUP] Deleting VM {vm_id} (task={task_id}, reason={reason})")

    for attempt in range(3):
        try:
            result = await delete_agent_vm(vm_id)
            if result:
                logger.info(f"[VM CLEANUP] Deleted VM {vm_id}")
                return True
            else:
                logger.warning(f"[VM CLEANUP] delete_agent_vm returned False for {vm_id} (attempt {attempt + 1}/3)")
        except Exception as e:
            logger.error(f"[VM CLEANUP] Failed to delete VM {vm_id} (attempt {attempt + 1}/3): {e}", exc_info=True)

        if attempt < 2:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff

    logger.error(f"[VM CLEANUP] FAILED to delete VM {vm_id} after 3 attempts — will rely on cleanup sweep")
    return False


async def _get_actual_gcp_vm_count() -> int:
    """Count actual agent-vm-* instances across all GCP zones.
    Cached for _GCP_VM_CHECK_INTERVAL seconds to avoid API spam.
    Updated passively by the cleanup service, with active refresh as fallback."""
    global _last_gcp_vm_count, _last_gcp_vm_check

    now = time.time()
    if now - _last_gcp_vm_check < _GCP_VM_CHECK_INTERVAL:
        return _last_gcp_vm_count

    from app.compute import FALLBACK_ZONES, _list_vms_in_zone, LOCAL_MODE
    if LOCAL_MODE:
        from app.compute import _local_processes
        _last_gcp_vm_count = len(_local_processes)
        _last_gcp_vm_check = now
        return _last_gcp_vm_count

    try:
        unique_zones = list({z for z, _ in FALLBACK_ZONES})
        zone_tasks = [asyncio.to_thread(_list_vms_in_zone, zone) for zone in unique_zones]
        zone_results = await asyncio.wait_for(
            asyncio.gather(*zone_tasks, return_exceptions=True),
            timeout=10  # Short timeout — cache is usually fresh from cleanup service
        )
        total = 0
        for zr in zone_results:
            if isinstance(zr, list):
                total += len(zr)
        _last_gcp_vm_count = total
        _last_gcp_vm_check = now
        return total
    except Exception as e:
        logger.warning(f"[GCP] Failed to count VMs: {e}")
        return _last_gcp_vm_count  # Return stale count on failure


_recorded_task_ids: set = set()  # Track which task IDs have already recorded results

# Webhook dedup: track recent GitHub delivery IDs to prevent duplicate processing
from collections import OrderedDict, deque
_webhook_delivery_ids: OrderedDict = OrderedDict()  # id -> timestamp
_webhook_dedup_lock = asyncio.Lock()
_WEBHOOK_DEDUP_MAX = 1000
_WEBHOOK_DEDUP_TTL = 600  # 10 minutes

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

    entry = (_utcnow().isoformat(), "success" if success else "failure", failure_class)
    deploy_monitoring["task_results"].append(entry)  # deque(maxlen=100) handles cap automatically

    if success:
        _shared_state.deploy_monitoring["last_completion_time"] = _utcnow()


async def _verify_schema_version():
    """Verify the database schema matches the expected alembic revision.
    Fail fast on mismatch to prevent runtime errors from missing columns."""
    from sqlalchemy import text as sa_text
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(sa_text(
                "SELECT version_num FROM alembic_version LIMIT 1"))
            row = result.first()
            if not row:
                logger.warning("[STARTUP] No alembic_version row — database may be uninitialized")
                return
            if not row[0].startswith(EXPECTED_SCHEMA_REVISION):
                msg = (f"Schema version mismatch: database at '{row[0]}', "
                       f"code expects '{EXPECTED_SCHEMA_REVISION}'")
                logger.error(f"[STARTUP] {msg}")
                if os.getenv("K_REVISION"):  # Only hard-fail in Cloud Run
                    raise RuntimeError(msg)
        except Exception as e:
            if "alembic_version" in str(e).lower():
                logger.warning("[STARTUP] alembic_version table not found — skipping schema check")
                return
            raise


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
                deployed_at=_utcnow(),
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
            if getattr(state, 'runtime_max_global_autonomous_tasks_per_hour', None) is not None:
                _runtime_config["max_global_autonomous_tasks_per_hour"] = state.runtime_max_global_autonomous_tasks_per_hour
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
        # Restore circuit breaker state — prevents deploy from clearing tripped breakers
        try:
            if state.circuit_breaker_states:
                for proj_id, breaker_data in state.circuit_breaker_states.items():
                    restored = {**breaker_data}
                    if restored.get("opened_at") and isinstance(restored["opened_at"], str):
                        restored["opened_at"] = datetime.fromisoformat(restored["opened_at"])
                    _shared_state.circuit_breakers[proj_id] = restored
                logger.info(f"[DEPLOY] Restored circuit breaker state for {len(state.circuit_breaker_states)} projects")
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore circuit breaker state: {e}", exc_info=True)

        # Restore zone health — prevents retry storms on exhausted zones after restart
        try:
            if state.zone_health_state:
                from app.compute import _zone_health, _zone_lock, _zone_failures
                from datetime import timezone as tz
                with _zone_lock:
                    for zone, data in state.zone_health_state.items():
                        restored = {}
                        for k, v in data.items():
                            if isinstance(v, str) and k in ("last_success", "last_failure", "cooldown_expires_at"):
                                try:
                                    restored[k] = datetime.fromisoformat(v)
                                except ValueError:
                                    logger.warning(f"[DEPLOY] Skipping corrupted zone health key {k} for zone {zone}: {v!r}")
                                    continue
                            else:
                                restored[k] = v
                        _zone_health[zone] = restored
                        # Restore _zone_failures for zones still in cooldown
                        if restored.get("cooldown_expires_at") and restored["cooldown_expires_at"] > datetime.now(tz.utc):
                            _zone_failures[zone] = restored.get("last_failure", datetime.now(tz.utc))
                logger.info(f"[DEPLOY] Restored zone health state for {len(state.zone_health_state)} zones")
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore zone health state: {e}", exc_info=True)

        # Auto-recovery from HALTED after 1 hour cooldown
        if state.status == DeploymentStatus.HALTED:
            try:
                if state.halted_at:
                    elapsed = (_utcnow() - state.halted_at).total_seconds()
                    if elapsed > 3600:  # 1 hour
                        state.rollback_count = 0
                        state.status = DeploymentStatus.MONITORING
                        state.halted_at = None
                        state.deployed_at = _utcnow()
                        # Restore VM capacity so dispatch actually works
                        restored = state.pre_pause_max_vms or int(os.getenv("MAX_CONCURRENT_VMS", "5"))
                        _runtime_config["max_concurrent_vms"] = restored
                        state.pre_pause_max_vms = None
                        session.add(state)
                        await session.commit()
                        logger.warning(f"[DEPLOY] Auto-recovery from HALTED after {elapsed:.0f}s cooldown — max_concurrent_vms restored to {restored}")
                    else:
                        remaining = 3600 - elapsed
                        logger.info(f"[DEPLOY] HALTED — {remaining:.0f}s remaining until auto-recovery")
            except Exception as e:
                logger.warning(f"[DEPLOY] Could not check HALTED auto-recovery (halted_at column may not exist yet): {e}")

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
            state.deployed_at = _utcnow()
            session.add(state)
            await session.commit()

            logger.warning(f"[DEPLOY] ROLLBACK DETECTED — reverted from {state.rolled_back_from_sha[:8]} to {_current_sha[:8]} "
                  f"(consecutive rollbacks: {state.rollback_count})")

            if state.rollback_count >= 3:
                state.status = DeploymentStatus.HALTED
                state.halted_at = _utcnow()
                session.add(state)
                await session.commit()
                logger.error(f"[DEPLOY] HALTED — {state.rollback_count} consecutive rollbacks, manual intervention required")
                _runtime_config["max_concurrent_vms"] = 0
            return

        # New deploy detected — update revision tracking
        state.current_revision = _current_revision
        state.current_sha = _current_sha
        state.deployed_at = _utcnow()
        state.rolled_back_from_sha = None
        state.rollback_reason = None

        # Only reset monitoring window if not PAUSED/HALTED — those are explicit
        # states that must be cleared by auto-resume or manual action, not by deploys.
        # Resetting monitoring data while paused would lose baseline context.
        if state.status not in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
            from app.shared_state import deploy_monitoring
            results = deploy_monitoring["task_results"]
            if results:
                failures = sum(1 for _, outcome, _ in results if outcome == "failure")
                deploy_monitoring["pre_deploy_failure_rate"] = failures / len(results)
            else:
                # No previous data (idle→active). Use generous baseline to avoid false rollbacks.
                deploy_monitoring["pre_deploy_failure_rate"] = 0.15
            deploy_monitoring["previous_window_results"] = list(results)
            deploy_monitoring["previous_window_start"] = deploy_monitoring.get("window_start")
            deploy_monitoring["task_results"] = deque(maxlen=100)
            deploy_monitoring["window_start"] = _utcnow()
            state.status = DeploymentStatus.MONITORING
        session.add(state)
        await session.commit()
        logger.info(f"[DEPLOY] New deploy detected — revision={_current_revision}, status={state.status}, starting monitoring window")


def should_rollback() -> tuple:
    """Causation analysis: determine if failures are caused by the new deploy.
    Returns (should_rollback: bool, reason: str, response: str).
    Response is one of: ROLLBACK, PAUSE, SELF_HEAL"""
    from app.shared_state import deploy_monitoring, external_health

    results = deploy_monitoring["task_results"]
    if not results:
        return False, "No task results to analyze", "SELF_HEAL"

    # Minimum sample size — don't make rollback decisions on tiny data
    if len(results) < 5:
        return False, f"Insufficient data ({len(results)} results, need 5+)", "SELF_HEAL"

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


async def _get_deploy_state_for_update(session: AsyncSession) -> "DeploymentState | None":
    """Fetch DeploymentState with FOR UPDATE to prevent concurrent overwrites."""
    return (await session.execute(
        select(DeploymentState).where(DeploymentState.id == 1).with_for_update()
    )).scalars().first()


async def _check_monitoring_window():
    """Called each orchestrator cycle. If monitoring window has elapsed without issues, mark STABLE."""
    from app.shared_state import deploy_monitoring
    window_start = deploy_monitoring.get("window_start")
    if not window_start:
        return

    duration = deploy_monitoring["extended_window"] if deploy_monitoring["is_extended"] else deploy_monitoring["window_duration"]
    elapsed = (_utcnow() - window_start).total_seconds()

    if elapsed < duration:
        return  # Still within window

    # Window elapsed — check results
    results = deploy_monitoring["task_results"]
    if not results:
        # No tasks ran during window — if 2x the window has passed, mark stable (idle)
        if elapsed >= duration * 2:
            async with AsyncSessionLocal() as session:
                state = await _get_deploy_state_for_update(session)
                if state and state.status == DeploymentStatus.MONITORING:
                    old_status = state.status
                    state.status = DeploymentStatus.STABLE
                    state.last_known_good_revision = state.current_revision
                    state.last_known_good_sha = state.current_sha
                    state.rollback_count = 0
                    session.add(state)
                    await session.commit()
                    logger.info("[DEPLOY] Monitoring idle timeout — no tasks ran, marking STABLE")
                    logger.info('deployment_monitor_state_change', extra={
                        'old_status': str(old_status),
                        'new_status': str(state.status),
                        'rollback_triggered': False,
                    })
            deploy_monitoring["window_start"] = None
        return

    async with AsyncSessionLocal() as session:
        state = await _get_deploy_state_for_update(session)
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
                logger.info('deployment_monitor_state_change', extra={
                    'old_status': str(state.status),
                    'new_status': str(state.status),
                    'rollback_triggered': True,
                })
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
            old_status = state.status
            state.status = DeploymentStatus.STABLE
            state.last_known_good_revision = state.current_revision
            state.last_known_good_sha = state.current_sha
            state.rollback_count = 0
            session.add(state)
            await session.commit()
            logger.info(f"[DEPLOY] Monitoring window passed — marked STABLE (revision={state.current_revision})")
            logger.info('deployment_monitor_state_change', extra={
                'old_status': str(old_status),
                'new_status': str(state.status),
                'rollback_triggered': False,
            })

    deploy_monitoring["window_start"] = None


async def _check_external_health():
    """Periodic check of external dependencies. Updates shared_state.external_health."""
    ext = _shared_state.external_health
    now = _utcnow()

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
        ext["errors"].pop("db", None)
    except Exception as e:
        logger.warning(f"[HEALTH] DB health check failed: {e}", exc_info=True)
        ext["db"] = False
        ext["errors"]["db"] = f"{type(e).__name__}: {str(e)[:200]}"
        failed.append("db")

    # GitHub API check — passive first, active fallback.
    # Primary: check if recent agent operations succeeded (avoids connection pool
    # contention that caused false auto-pause under load).
    # Fallback: if no recent operations, make a dedicated active check.
    github_stale_threshold = 60  # 1 minute — detect outages faster during idle
    github_age = (now - _shared_state.last_github_success).total_seconds() if _shared_state.last_github_success else None

    if github_age is not None and github_age < github_stale_threshold:
        # Recent success — GitHub is healthy, no active check needed
        ext["github_api"] = True
        ext["errors"].pop("github_api", None)
    else:
        # No recent success — either idle or actually down. Make a dedicated active check
        # using a separate short-lived client to avoid connection pool contention.
        check_method = "passive" if github_age is not None else "active (cold start)"
        try:
            from app.github import generate_github_app_jwt
            jwt_token = generate_github_app_jwt()
            async with httpx.AsyncClient(timeout=10.0) as health_client:
                resp = await health_client.get("https://api.github.com/app",
                                               headers={"Authorization": f"Bearer {jwt_token}"})
            ext["github_api"] = resp.status_code == 200
            if ext["github_api"]:
                _shared_state.last_github_success = now
                ext["errors"].pop("github_api", None)
            else:
                logger.warning(f"[HEALTH] GitHub API returned {resp.status_code}")
                ext["errors"]["github_api"] = f"HTTP {resp.status_code} (check: {check_method})"
                failed.append("github_api")
        except Exception as e:
            logger.warning(f"[HEALTH] GitHub API health check failed: {type(e).__name__}: {e!r}")
            ext["github_api"] = False
            ext["errors"]["github_api"] = f"{type(e).__name__}: {str(e)[:200]} (check: {check_method})"
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
    await recheck_disabled_accounts()

    # Log zone health transitions (available ↔ exhausted)
    global _last_exhausted_zones
    zone_summary = _get_zone_health_summary()
    current_exhausted = set(zone_summary.get("exhausted", []))
    available_zones = zone_summary.get("available", 0)
    total_zones = zone_summary.get("total_zones", 0)
    for zone in current_exhausted - _last_exhausted_zones:
        logger.warning('zone_health_exhausted', extra={
            'zone': zone,
            'available_zones': available_zones,
            'total_zones': total_zones,
        })
    for zone in _last_exhausted_zones - current_exhausted:
        logger.info('zone_health_recovered', extra={
            'zone': zone,
            'available_zones': available_zones,
            'total_zones': total_zones,
        })
    _last_exhausted_zones = current_exhausted

    if failed:
        logger.error(f"[HEALTH] External dependency check failed: {', '.join(failed)}")


_pause_failure_streak: int = 0  # consecutive health check cycles with critical failures
_health_recovery_streak: int = 0  # consecutive healthy cycles while PAUSED (gate for auto-resume)
_last_exhausted_zones: set = set()  # tracks zone health for transition logging

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
                state = await _get_deploy_state_for_update(session)
                if state and state.status not in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
                    state.status = DeploymentStatus.PAUSED
                    state.pause_reason = f"External dependency down: {', '.join(critical_failures)}"
                    state.paused_at = _utcnow()
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
            state = await _get_deploy_state_for_update(session)
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
        if breaker["opened_at"] and (_utcnow() - breaker["opened_at"]).total_seconds() >= breaker["cooldown_seconds"]:
            breaker["state"] = "HALF_OPEN"
            logger.warning(f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)")
        else:
            return  # Still in cooldown
    elif breaker["state"] == "HALF_OPEN":
        # Check if existing test task is stuck — treat as failure after 30 min
        test_id = breaker.get("half_open_task_id")
        if test_id:
            test_task = await session.get(Task, uuid.UUID(test_id))
            if test_task and test_task.started_at and (_utcnow() - test_task.started_at).total_seconds() > 1800:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                return  # Back to cooldown
    # Guard: must have active directions that aren't in cooldown
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    now = _utcnow()
    active_directions = []
    for d in (project.directions or []):
        if d.get("status") != "active":
            continue
        last_researched = d.get("last_researched_at")
        if last_researched:
            minutes_ago = (now - datetime.fromisoformat(last_researched)).total_seconds() / 60
            if minutes_ago < cooldown_minutes:
                logger.debug(
                    f"[RESEARCH] Skipping direction {d.get('id')}: last researched "
                    f"{minutes_ago:.1f}m ago (cooldown: {cooldown_minutes}m)"
                )
                continue
        active_directions.append(d)
    if not active_directions:
        return
    # Pick ONE direction per research task to avoid putting all on cooldown at once.
    # active_directions preserves UI drag-order (index 0 = highest priority).
    # Among eligible directions, prefer those never researched or oldest researched.
    import random
    never_researched = [d for d in active_directions if not d.get("last_researched_at")]
    if never_researched:
        chosen_direction = random.choice(never_researched)
    else:
        # All have been researched before — pick the one researched longest ago
        active_directions.sort(key=lambda d: d.get("last_researched_at") or "")
        chosen_direction = active_directions[0]

    # Guard: no research task actively running (queued/provisioning/in_progress).
    # EVALUATING parents are just waiting on children — they don't block new research.
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    for t in active_tasks:
        if ((t.payloads or [{}])[0]).get("type") == "research":
            return
    # Guard: global rate limit across ALL projects
    from sqlalchemy import func
    one_hour_ago = _utcnow() - timedelta(hours=1)
    global_max = _runtime_config.get("max_global_autonomous_tasks_per_hour",
                                      _runtime_config["max_autonomous_tasks_per_hour"] * 10)
    global_recent = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if global_recent >= global_max:
        logger.warning(f"[AUTONOMOUS] Global rate limit reached: {global_recent}/{global_max} tasks/hour")
        return
    # Guard: per-project rate limit for research spawns
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

    # Autonomous research creates independent tasks (no parent_task_id), so they
    # must always branch from main.  Staging branches (research/*, plan/*) are
    # ephemeral — they get deleted when their parent's rollup merges.  An
    # independent task using someone else's staging branch will break when that
    # rollup cleans up.  Actual subresearch (spawned by _handle_research_completion
    # with parent_task_id set) correctly inherits its parent's staging branch.
    base_branch = "main"

    # Use per-type defaults, fall back to autonomous-specific config
    research_model = _runtime_config.get("task_type_models", {}).get("research") or _runtime_config.get("autonomous_research_model") or None
    research_effort = _runtime_config.get("task_type_efforts", {}).get("research") or _runtime_config.get("autonomous_research_effort") or None
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")
    research_payload: dict = {
        "type": "research",
        "directions": [chosen_direction],
        "base_branch": base_branch,
        **({"model": research_model} if research_model else {}),
        **({"effort": research_effort} if research_effort else {}),
        **({"prompt_suffix": research_suffix} if research_suffix else {}),
    }
    direction_text = (chosen_direction.get("text") or "general improvement")[:80]
    new_task = Task(
        project_id=project.id,
        title=f"Research: {direction_text}",
        description=f"Autonomous research for direction: {direction_text}",
        payloads=[research_payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
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
        if breaker["opened_at"] and (_utcnow() - breaker["opened_at"]).total_seconds() >= breaker["cooldown_seconds"]:
            breaker["state"] = "HALF_OPEN"
            logger.warning(f"[CIRCUIT BREAKER] OPEN → HALF-OPEN for {project.id} (cooldown elapsed, allowing one test task)")
        else:
            return  # Still in cooldown
    elif breaker["state"] == "HALF_OPEN":
        test_id = breaker.get("half_open_task_id")
        if test_id:
            test_task = await session.get(Task, uuid.UUID(test_id))
            if test_task and test_task.started_at and (_utcnow() - test_task.started_at).total_seconds() > 1800:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} stuck for 30m+, treating as failure → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                return

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

    # Guard: global rate limit across ALL projects
    one_hour_ago = _utcnow() - timedelta(hours=1)
    global_max = _runtime_config.get("max_global_autonomous_tasks_per_hour",
                                      _runtime_config["max_autonomous_tasks_per_hour"] * 10)
    global_recent = (await session.execute(
        select(func.count()).select_from(Task).where(
            Task.created_by == "ORCHESTRATOR",
            Task.created_at >= one_hour_ago,
        )
    )).scalar() or 0
    if global_recent >= global_max:
        logger.warning(f"[AUTONOMOUS] Global rate limit reached: {global_recent}/{global_max} tasks/hour")
        return
    # Guard: per-project rate limit
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

    now = _utcnow()
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
    # agent_prompt_suffixes: supported config key — dict of task_type → prompt suffix string,
    # appended to the agent prompt for matching task types (validated in admin PATCH endpoint).
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
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
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
    now = _utcnow()
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
                    await asyncio.wait_for(connection.send_json(message), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"[WS] Send timeout for client (project={project_id}, event={event_type})")
                    self.disconnect(connection, project_id)
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

# JWT revocation: tasks that have reached terminal state (COMPLETED/FAILED/CANCELLED).
# Maps task_id -> timestamp of termination. Entries older than _TERMINATED_TTL are evicted
# to prevent unbounded growth (JWTs expire after 8h, so older entries are redundant).
# After a cold start this dict is empty; the DB fallback below covers that case.
_terminated_task_ids: dict[str, float] = {}
_TERMINATED_TTL = 9 * 3600  # 9h — slightly longer than 8h JWT max lifetime
_on_task_terminal_callbacks.append(lambda tid: _terminated_task_ids.__setitem__(tid, time.time()))
# TTL cache for "known active" tasks — avoids DB query per request.
# Maps task_id -> expiry timestamp. Only checked on _terminated_task_ids miss.
_agent_active_cache: dict[str, float] = {}
_AGENT_CACHE_TTL = 60.0  # seconds
_AGENT_CACHE_MAX = 5000  # evict expired entries when cache exceeds this size


async def verify_agent_token(request: Request, x_agent_token: str = Header(..., description="Secret token for agent authentication")):
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

                now = _utcnow()
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

                    # Track tasks skipped by skip_locked — force-reap after 3 consecutive skips
                    locked_ids = set(orphaned_ids) - {t.id for t in orphaned_tasks}
                    for lid in locked_ids:
                        _reaper_skip_counts[lid] = _reaper_skip_counts.get(lid, 0) + 1
                    for t in orphaned_tasks:
                        _reaper_skip_counts.pop(t.id, None)

                    # Force-lock persistently skipped tasks (blocking, up to lock_timeout)
                    force_lock_ids = [lid for lid, count in _reaper_skip_counts.items() if count >= 3]
                    if force_lock_ids:
                        try:
                            force_stmt = select(Task).where(
                                Task.id.in_(force_lock_ids)
                            ).with_for_update()
                            force_locked = list((await session.execute(force_stmt)).scalars().all())
                            for t in force_locked:
                                _reaper_skip_counts.pop(t.id, None)
                                if t.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
                                    continue
                                if t.last_heartbeat and t.last_heartbeat >= five_minutes_ago:
                                    continue
                                orphaned_tasks.append(t)
                                logger.warning(f"[REAPER] Force-locked persistently skipped task {t.id}")
                        except Exception as e:
                            logger.warning(f"[REAPER] Force-lock failed (lock timeout?): {e}")

                    # Clean stale entries from skip counts
                    orphaned_id_set = set(orphaned_ids)
                    for tid in list(_reaper_skip_counts.keys()):
                        if tid not in orphaned_id_set:
                            del _reaper_skip_counts[tid]

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

                # Collect old VM IDs for deletion after commit (can't set attrs on SQLModel)
                _reaper_old_vms: Dict[str, str] = {}

                for task in orphaned_tasks:
                    # Re-validate status before acting — task may have completed/been cancelled
                    # between the initial query and now
                    fresh_task = await session.get(Task, task.id)
                    if fresh_task is None or fresh_task.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
                        logger.info(f"[REAPER] Skipping task {task.id} — status transitioned to {fresh_task.status if fresh_task else 'gone'} before reap")
                        continue

                    # Determine why the reaper flagged this task
                    if task.last_heartbeat:
                        age = int((now - task.last_heartbeat).total_seconds())
                        reap_reason = f"No heartbeat for {age}s (last: {task.last_heartbeat.isoformat()})"
                        reap_reason_code = 'heartbeat_timeout'
                    elif task.started_at:
                        age = int((now - task.started_at).total_seconds())
                        reap_reason = f"No heartbeat received after {age}s since provisioning"
                        reap_reason_code = 'provision_timeout'
                    else:
                        age = int((now - task.created_at).total_seconds())
                        reap_reason = f"Never started — {age}s since creation, no heartbeat"
                        reap_reason_code = 'never_started'

                    task_type = ((task.payloads or [{}])[0]).get("type", "unknown")
                    logger.info('reaper_task_reaped', extra={
                        'task_id': str(task.id),
                        'task_type': task_type,
                        'reap_reason': reap_reason_code,
                        'vm_instance_id': task.vm_instance_id,
                    })

                    # Capture old VM ID before any overwrite — deletion happens after commit
                    if task.vm_instance_id:
                        _reaper_old_vms[str(task.id)] = task.vm_instance_id

                    # Release Max Plan account slot
                    acct_id = (task.context or {}).get("max_plan_account_id")
                    if acct_id:
                        await release_max_plan_account(acct_id)

                    # Separate provision failures from agent failures for retry budgeting
                    is_provision_failure = task.status == TaskStatus.PROVISIONING
                    provision_failures = (task.context or {}).get("provision_failures", 0)
                    max_provision_retries = 2
                    can_retry = False

                    if is_provision_failure:
                        provision_failures += 1
                        task.context = {**(task.context or {}), "provision_failures": provision_failures}
                        can_retry = provision_failures < max_provision_retries
                        if not can_retry:
                            logger.error(f"[REAPER] Task {task.id} provision failed {provision_failures} times — giving up")
                    else:
                        can_retry = task.attempt_count < _runtime_config["max_retries"]

                    if can_retry:
                        # Check if this is a research/plan parent with work children —
                        # re-evaluate instead of re-run.
                        task_type = ((task.payloads or [{}])[0]).get("type", "")
                        is_parent_type = task_type in ("research", "plan")
                        children = []
                        if is_parent_type:
                            children = (await session.execute(
                                select(Task).where(Task.parent_task_id == task.id)
                            )).scalars().all()
                        if children:
                            logger.info(f"[REAPER] Re-evaluating parent task {task.id}")
                            await _requeue_parent_task(task, session)
                        else:
                            if not is_provision_failure:
                                task.attempt_count += 1
                            retry_label = f"provision {provision_failures}/{max_provision_retries}" if is_provision_failure else f"attempt {task.attempt_count}/{_runtime_config['max_retries']}"
                            logger.info(f"[REAPER] Retrying task {task.id} ({retry_label})")
                            task.latest_status_message = f"Reaped ({retry_label}): {reap_reason}"
                            task.status = TaskStatus.QUEUED
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                            task.last_heartbeat = None
                            task.started_at = None
                            session.add(task)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": task.status, "attempt_count": task.attempt_count}
                        )
                    else:
                        fail_reason = f"provision failed {provision_failures} times" if is_provision_failure else f"failed after {_runtime_config['max_retries']} attempts"
                        logger.error(f"[REAPER] Task {task.id} {fail_reason}.")
                        task.status = TaskStatus.FAILED
                        task.latest_status_message = f"{fail_reason.capitalize()} — last reap: {reap_reason}"
                        record_task_result(success=False, failure_class=classify_failure(task), task_id=str(task.id))
                        session.add(task)

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.FAILED}
                        )

                if orphaned_tasks:
                    await session.commit()

                    # Delete old VMs after commit (IDs captured before overwrite)
                    for old_vm in _reaper_old_vms.values():
                        await _ensure_vm_deleted(old_vm, reason="reaper")

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
            try:
                await session.rollback()
            except Exception:
                pass

        # Sleep for 1 minute before checking again
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break

async def gcp_cleanup_service():
    """
    Background worker that runs every 5 minutes to ensure GCP VMs are in sync with the database.
    Deletes any VMs prefixed with 'agent-vm-' that do not have an active task.
    Zone-parallel with per-cycle timeout.
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

                # Run cleanup with hard timeout (2 minutes)
                try:
                    cleanup_result = await asyncio.wait_for(
                        cleanup_orphaned_vms(active_vm_ids),
                        timeout=120
                    )
                    # Log divergence and partial failures from structured result
                    if hasattr(cleanup_result, 'failed') and cleanup_result.failed:
                        logger.warning(
                            f"[GCP CLEANUP] Partial failure: deleted {len(cleanup_result.deleted)}, "
                            f"failed {len(cleanup_result.failed)}: {cleanup_result.failed}"
                        )
                    # Cache total GCP VM count for orchestrator concurrency check
                    if hasattr(cleanup_result, 'total_gcp_vms'):
                        global _last_gcp_vm_count, _last_gcp_vm_check
                        _last_gcp_vm_count = cleanup_result.total_gcp_vms
                        _last_gcp_vm_check = time.time()
                except asyncio.TimeoutError:
                    logger.error("[GCP CLEANUP] Cycle timed out after 120s")

        except asyncio.CancelledError:
            logger.info("[GCP CLEANUP] Service shutting down.")
            break
        except Exception as e:
            logger.error(f"[GCP CLEANUP] Error: {e}", exc_info=True)

        try:
            await asyncio.sleep(300)  # Run every 5 minutes
        except asyncio.CancelledError:
            logger.info("[GCP CLEANUP] Service shutting down.")
            break

_last_pool_refresh: datetime = _utcnow()
_last_zone_health_persist: datetime = _utcnow()


async def _persist_circuit_breakers():
    """Save circuit breaker state to DB so it survives restarts."""
    try:
        serializable = {}
        for k, v in _shared_state.circuit_breakers.items():
            serializable[k] = {
                **v,
                "opened_at": v["opened_at"].isoformat() if v.get("opened_at") else None,
            }
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.circuit_breaker_states = serializable
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[CIRCUIT BREAKER] Failed to persist state: {e}", exc_info=True)


async def _persist_zone_health():
    """Snapshot zone health to DB so cooldowns survive restarts."""
    try:
        from app.compute import _zone_health, _zone_lock
        with _zone_lock:
            snapshot = {}
            for zone, data in _zone_health.items():
                snapshot[zone] = {
                    k: v.isoformat() if isinstance(v, datetime) else v
                    for k, v in data.items()
                }
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.zone_health_state = snapshot
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[ZONE HEALTH] Failed to persist state: {e}", exc_info=True)


async def orchestrator_worker():
    """
    Background worker that polls for QUEUED tasks and provisions VMs.
    This ensures tasks are not lost if the server restarts before provisioning.
    """
    global _last_pool_refresh
    while True:
        queued_tasks = []
        try:
            global _orchestrator_last_cycle
            _orchestrator_last_cycle = _utcnow()
            cycle_start = time.monotonic()
            tasks_recovered = 0

            # Periodic pool refresh — pick up admin changes without restart
            if (_utcnow() - _last_pool_refresh).total_seconds() >= 300:
                await load_max_plan_pool()
                _last_pool_refresh = _utcnow()

            # Periodic zone health persist — prevents retry storms after restart
            global _last_zone_health_persist
            if (_utcnow() - _last_zone_health_persist).total_seconds() >= 60:
                await _persist_zone_health()
                _last_zone_health_persist = _utcnow()

            async with AsyncSessionLocal() as session:
                # Fetch all active tasks once — used for both concurrency limit and counter reconciliation
                active_tasks_result = (await session.execute(
                    select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                )).scalars().all()

                # Recover stale PROVISIONING tasks (orchestrator crashed mid-dispatch)
                five_min_ago = _utcnow() - timedelta(minutes=5)
                stale_vm_ids = []
                for t in active_tasks_result:
                    if (t.status == TaskStatus.PROVISIONING
                        and t.last_heartbeat is None
                        and t.started_at and t.started_at < five_min_ago):
                        stale_vm_ids.append(t.vm_instance_id)
                        t.status = TaskStatus.QUEUED
                        t.started_at = None
                        t.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        session.add(t)
                        tasks_recovered += 1
                        logger.warning(f"[ORCHESTRATOR] Recovered stale PROVISIONING task {t.id} → QUEUED")
                if session.dirty:
                    await session.commit()
                    # Delete old VMs from stale provisions (after commit)
                    for old_vm in stale_vm_ids:
                        if old_vm:
                            await _ensure_vm_deleted(old_vm, reason="stale_provisioning")
                    # Re-fetch after recovery
                    active_tasks_result = (await session.execute(
                        select(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
                    )).scalars().all()

                # Enforce concurrency limit
                active_count = len(active_tasks_result)
                # Cross-check with actual GCP VM count (cached, refreshed every 2 min)
                actual_vm_count = await _get_actual_gcp_vm_count()
                effective_count = max(active_count, actual_vm_count)
                if actual_vm_count > active_count + 2:
                    logger.warning(
                        f"[ORCHESTRATOR] GCP VM count ({actual_vm_count}) > DB active count ({active_count}) "
                        f"— possible orphans, using GCP count for slot calculation"
                    )
                slots_available = max(0, _runtime_config["max_concurrent_vms"] - effective_count)
                _shared_state.tasks_active_gauge = active_count
                if slots_available == 0:
                    # Update queued gauge even when we skip dispatching
                    _shared_state.tasks_queued_gauge = (await session.execute(
                        select(_func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval == False)
                    )).scalar() or 0
                    logger.info('orchestrator_cycle_complete', extra={
                        'cycle_duration_seconds': round(time.monotonic() - cycle_start, 3),
                        'tasks_dispatched': 0,
                        'tasks_recovered': tasks_recovered,
                    })
                    await asyncio.sleep(5)
                    continue

                # Reconcile Max Plan active_tasks counters from DB each cycle
                active_per_account: Dict[str, int] = {}
                for t in active_tasks_result:
                    acct_id = (t.context or {}).get("max_plan_account_id")
                    if acct_id:
                        active_per_account[acct_id] = active_per_account.get(acct_id, 0) + 1
                async with _max_plan_pool_lock:
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

                    # Detect deleted dependencies — treat as failed to prevent premature dispatch
                    if len(dep_tasks) < len(dep_uuids):
                        found_ids = {str(dt.id) for dt in dep_tasks}
                        missing = [d for d in deps if d not in found_ids]
                        logger.error(f"[ORCHESTRATOR] Task {task.id} has {len(missing)} deleted dependencies: {missing}")

                    all_met = True
                    any_failed = len(dep_tasks) < len(dep_uuids)  # Missing deps treated as failed
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
                        await _ensure_vm_deleted(task, reason="dependency_cancelled")
                        logger.error(f"[ORCHESTRATOR] Cancelled task {task.id} — dependency failed")
                    elif all_met:
                        eligible_ids.append(task.id)

                # Step 2.4: Deadlock detection — break circular deps among stuck QUEUED tasks
                global _last_deadlock_check
                now = time.time()
                if now - _last_deadlock_check > 60:
                    _last_deadlock_check = now
                    stuck_tasks = [t for t in candidate_tasks
                                   if t.id not in eligible_ids
                                   and (t.depends_on or [])
                                   and t.status == TaskStatus.QUEUED]
                    if stuck_tasks:
                        stuck_ids = {t.id for t in stuck_tasks}
                        for stask in stuck_tasks:
                            dep_uuids = set()
                            for d in (stask.depends_on or []):
                                try:
                                    dep_uuids.add(uuid.UUID(d))
                                except (ValueError, AttributeError):
                                    continue
                            # Verify each dep's actual DB status before clearing
                            # Only clear if deps are genuinely unresolvable (FAILED/CANCELLED/deleted)
                            candidate_ids = {t.id for t in candidate_tasks}
                            genuinely_blocked = True
                            async with AsyncSessionLocal() as check_session:
                                for dep_id in dep_uuids:
                                    if dep_id in stuck_ids:
                                        continue  # Stuck in this pool — potential cycle
                                    dep_task = await check_session.get(Task, dep_id)
                                    if dep_task is None:
                                        continue  # Deleted dep — genuinely unresolvable
                                    if dep_task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                                        continue  # Terminal — genuinely unresolvable
                                    if dep_task.status == TaskStatus.QUEUED:
                                        # QUEUED but outside fetch window — NOT a deadlock, just low priority
                                        genuinely_blocked = False
                                        break
                                    if dep_task.status in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING):
                                        # Still running — NOT a deadlock
                                        genuinely_blocked = False
                                        break
                            if genuinely_blocked and dep_uuids:
                                async with AsyncSessionLocal() as fix_session:
                                    fix_task = await fix_session.get(Task, stask.id)
                                    if fix_task:
                                        logger.warning(
                                            f"[ORCHESTRATOR] Breaking deadlock: clearing depends_on "
                                            f"for task {stask.id} (had {len(dep_uuids)} stuck/failed deps)"
                                        )
                                        fix_task.depends_on = []
                                        fix_task.latest_status_message = "Dependencies cleared: deadlock detected"
                                        fix_session.add(fix_task)
                                        await fix_session.commit()
                                eligible_ids.append(stask.id)
                                break  # One per check — conservative

                    # Broader scan: check ALL QUEUED tasks older than 30 min (not just candidate batch)
                    thirty_min_ago = _utcnow() - timedelta(minutes=30)
                    broad_stmt = select(Task).where(
                        Task.status == TaskStatus.QUEUED,
                        Task.created_at < thirty_min_ago,
                        Task.requires_approval == False,
                    )
                    broad_stuck = (await session.execute(broad_stmt)).scalars().all()
                    broad_stuck = [t for t in broad_stuck if (t.depends_on or []) and t.id not in eligible_ids]
                    if broad_stuck:
                        broad_stuck_ids = {t.id for t in broad_stuck}
                        for stask in broad_stuck:
                            if stask.id in {t.id for t in candidate_tasks}:
                                continue  # Already checked above
                            dep_uuids = set()
                            for d in (stask.depends_on or []):
                                try:
                                    dep_uuids.add(uuid.UUID(d))
                                except (ValueError, AttributeError):
                                    continue
                            genuinely_blocked = True
                            async with AsyncSessionLocal() as check_session:
                                for dep_id in dep_uuids:
                                    if dep_id in broad_stuck_ids:
                                        continue
                                    dep_task = await check_session.get(Task, dep_id)
                                    if dep_task is None or dep_task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                                        continue
                                    genuinely_blocked = False
                                    break
                            if genuinely_blocked and dep_uuids:
                                async with AsyncSessionLocal() as fix_session:
                                    fix_task = await fix_session.get(Task, stask.id)
                                    if fix_task:
                                        logger.warning(
                                            f"[ORCHESTRATOR] Breaking deadlock (broad scan): clearing depends_on "
                                            f"for task {stask.id} (stuck >{30}min with {len(dep_uuids)} unresolvable deps)"
                                        )
                                        fix_task.depends_on = []
                                        fix_task.latest_status_message = "Dependencies cleared: deadlock detected (broad scan)"
                                        fix_session.add(fix_task)
                                        await fix_session.commit()
                                break  # One per check — conservative

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
                _shared_state.tasks_queued_gauge = (await session.execute(
                    select(_func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval == False)
                )).scalar() or 0
                # tasks_active_gauge already set above after active_count is computed

                for task in queued_tasks:
                    # Ensure VM ID exists (cleared during stale PROVISIONING recovery)
                    if not task.vm_instance_id:
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    logger.info(f"[ORCHESTRATOR] Picking up queued task {task.id} (VM: {task.vm_instance_id})")

                    # Select Max plan account (primary) or fall back to API key
                    account = await select_max_plan_account()
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
                    task.started_at = _utcnow()
                    _shared_state.deploy_monitoring["last_dispatch_time"] = _utcnow()
                    session.add(task)
                    try:
                        await session.commit()
                        # active_tasks already pre-incremented in select_max_plan_account()
                        logger.info(f"[ORCHESTRATOR] Task {task.id} committed as PROVISIONING — launching VM")
                    except Exception as e:
                        import traceback
                        logger.error(f"[ORCHESTRATOR] FAILED to commit PROVISIONING for task {task.id}: {e}", exc_info=True)
                        traceback.print_exc()
                        # Release the pre-incremented slot on commit failure
                        if account_id:
                            await release_max_plan_account(account_id)
                        await session.rollback()
                        continue

                    # Trigger GCP Compute Engine provisioning in the background
                    # All values passed as args to avoid stale closure captures across loop iterations
                    async def _provision_and_handle(vm_id: str, t_id: uuid.UUID, p_id: uuid.UUID,
                                                    payloads: list, token: str, config_json: str,
                                                    acct_id_for_release: str):
                        try:
                            payload_uri = None
                            if payloads:
                                try:
                                    payload_uri = await upload_json_payload(str(t_id), payloads)
                                except Exception as e:
                                    logger.error(f"[ORCHESTRATOR] Failed to upload payload for task {t_id}: {e}", exc_info=True)

                            success = await provision_agent_vm(vm_id, t_id, payload_uri,
                                                               oauth_token=token, claude_config_json=config_json)
                        except Exception as e:
                            logger.error(f"[ORCHESTRATOR] Provision crashed for task {t_id}: {e}", exc_info=True)
                            success = False
                        if not success:
                            logger.error(f"[ORCHESTRATOR] Failed to provision VM {vm_id} for task {t_id}")
                            record_task_result(success=False, failure_class="PROVISION_FAILURE", task_id=str(t_id))
                            # Release the Max Plan account slot
                            if acct_id_for_release:
                                await release_max_plan_account(acct_id_for_release)
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
                            # Delete partially-created VM (may exist if insert succeeded but wait failed)
                            await _ensure_vm_deleted(vm_id, reason="provision_failure")

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

                    try:
                        # Timeout: if CI has been pending too long, proceed to merge anyway
                        ci_wait_started = ci_loop.get("ci_wait_started")
                        if ci_wait_started:
                            try:
                                started = datetime.fromisoformat(ci_wait_started)
                                if (_utcnow() - started).total_seconds() > CI_WAIT_TIMEOUT_MINUTES * 60:
                                    logger.error(f"[CI] PR #{ci_pr_number} CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min")
                                    ci_task.status = TaskStatus.FAILED
                                    ci_task.latest_status_message = f"CI wait timed out after {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number} — manual inspection required"
                                    session.add(ci_task)
                                    await session.commit()
                                    await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                               {"task_id": str(ci_task.id), "status": ci_task.status})
                                    await _ensure_vm_deleted(ci_task, reason="ci_timeout")
                                    continue
                            except (ValueError, TypeError) as e:
                                logger.error(f"[CI] Malformed ci_wait_started for task {ci_task.id}: {ci_wait_started} ({e})", exc_info=True)

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
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            logger.info(f"[CI] PR #{ci_pr_number} already merged/closed, skipping")
                            continue

                        # Check for merge conflicts — CI won't trigger on conflicting PRs
                        # GitHub returns None while computing mergeability — skip this cycle and recheck later
                        if pr_data.get("mergeable") is None:
                            continue
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
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            continue

                        ci_head_sha = pr_data.get("head", {}).get("sha", "")
                        if not ci_head_sha:
                            ci_task.latest_status_message = f"CI waiting on PR #{ci_pr_number} — HEAD SHA unavailable"
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
                            logger.info(f"[CI] PR #{ci_pr_number} has no HEAD SHA — skipping CI check this cycle")
                            continue

                        ci_result = await get_check_runs(ci_gh_token, ci_owner, ci_repo, ci_head_sha)
                        if ci_result["status"] != "completed":
                            details = [d["name"] for d in ci_result.get("details", []) if d.get("status") in ("queued", "in_progress")]
                            ci_task.latest_status_message = f"CI in progress on PR #{ci_pr_number}: {', '.join(details) or 'waiting for checks'}..."
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                       {"task_id": str(ci_task.id), "status": ci_task.status})
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
                                await broadcast_to_project(str(ci_task.project_id), "task_updated",
                                                           {"task_id": str(ci_task.id), "status": ci_task.status})
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
                            try:
                                await session.rollback()
                            except Exception:
                                pass

            logger.info('orchestrator_cycle_complete', extra={
                'cycle_duration_seconds': round(time.monotonic() - cycle_start, 3),
                'tasks_dispatched': len(queued_tasks),
                'tasks_recovered': tasks_recovered,
            })
        except asyncio.CancelledError:
            logger.info("[ORCHESTRATOR] Worker shutting down.")
            break
        except Exception as e:
            logger.error('orchestrator_cycle_error', extra={
                'cycle_duration_seconds': round(time.monotonic() - cycle_start, 3),
            }, exc_info=True)
            try:
                await session.rollback()
            except Exception:
                pass

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

    # Verify schema version matches expected migration
    await _verify_schema_version()

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

    # Monitor background tasks for silent crashes (with exponential backoff)
    async def _monitor_background_tasks():
        tasks = _bg_tasks
        restart_state: Dict[str, Dict] = {}  # name -> {attempts, last_restart}
        task_factories = {
            "orchestrator": orchestrator_worker,
            "reaper": reaper_service,
            "gcp_cleanup": gcp_cleanup_service,
        }
        while True:
            await asyncio.sleep(30)
            for name, t in list(tasks.items()):
                if t.done():
                    exc = t.exception() if not t.cancelled() else None
                    logger.error(f"[MONITOR] Background task '{name}' DIED! Exception: {exc}")
                    factory = task_factories.get(name)
                    if factory and not t.cancelled():
                        state = restart_state.setdefault(name, {"attempts": 0, "last_restart": None})
                        now = _utcnow()
                        # Reset attempts if last restart was >5 min ago (ran long enough to be stable)
                        if state["last_restart"] and (now - state["last_restart"]).total_seconds() > 300:
                            state["attempts"] = 0
                        backoff = min(2 ** state["attempts"], 300)  # 1s, 2s, 4s, ... cap 5min
                        if backoff > 1:
                            logger.warning(f"[MONITOR] Backing off {backoff}s before restarting {name} (attempt #{state['attempts'] + 1})")
                            await asyncio.sleep(backoff)
                        state["attempts"] += 1
                        state["last_restart"] = _utcnow()
                        logger.info(f"[MONITOR] Restarting {name}...")
                        tasks[name] = asyncio.create_task(factory())
    monitor_task = asyncio.create_task(_monitor_background_tasks())

    yield
    # Shutdown: graceful cleanup with timeout to let in-flight operations finish
    tasks_to_cancel = [reaper_task, gcp_cleanup_task, orchestrator_task, monitor_task]
    for t in tasks_to_cancel:
        t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks_to_cancel, return_exceptions=True),
            timeout=10.0
        )
    except asyncio.TimeoutError:
        logger.warning("[SHUTDOWN] Background tasks didn't finish within 10s grace period")
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
        ago = (_utcnow() - _orchestrator_last_cycle).total_seconds()
    # Include deployment state
    deploy_state = None
    try:
        state = await session.get(DeploymentState, 1)
        if state:
            monitoring_since = _shared_state.deploy_monitoring.get("window_start")
            recent_results = _shared_state.deploy_monitoring.get("task_results", [])[-10:]
            deploy_state = {
                "revision": state.current_revision,
                "sha": state.current_sha[:8] if state.current_sha else "",
                "status": state.status,
                "deployed_at": state.deployed_at.isoformat() if state.deployed_at else None,
                "last_known_good_sha": state.last_known_good_sha[:8] if state.last_known_good_sha else "",
                "rollback_count": state.rollback_count,
                "rollback_reason": state.rollback_reason,
                "pause_reason": state.pause_reason,
                "monitoring_since": monitoring_since.isoformat() if monitoring_since else None,
                "recent_outcomes": [
                    {"created_at": ts, "status": outcome, "failure_class": fc}
                    for ts, outcome, fc in recent_results
                ],
            }
    except Exception as e:
        logger.warning(f"[DEBUG] Failed to fetch deployment state for /api/debug/orchestrator: {e}", exc_info=True)

    # Snapshot pool under lock for consistent read
    async with _max_plan_pool_lock:
        pool_snapshot = {
            "size": len(_max_plan_pool),
            "available": sum(1 for a in _max_plan_pool if a.get("enabled", True)),
            "disabled": sum(1 for a in _max_plan_pool if not a.get("enabled", True)),
            "accounts": [
                {
                    "name": a["name"],
                    "enabled": a.get("enabled", True),
                    "active": a["active_tasks"],
                    "disabled_at": a["disabled_at"].isoformat() if a.get("disabled_at") else None,
                    "reenable_at": (a["disabled_at"] + timedelta(minutes=30)).isoformat() if a.get("disabled_at") else None,
                }
                for a in _max_plan_pool
            ],
        }

    return {
        "orchestrator_alive": _orchestrator_last_cycle is not None,
        "last_cycle_seconds_ago": ago,
        "queued_tasks": queued,
        "active_tasks": active,
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "actual_gcp_vm_count": _last_gcp_vm_count,
        "gcp_vm_count_age_seconds": int(time.time() - _last_gcp_vm_check) if _last_gcp_vm_check else None,
        "pool_size": pool_snapshot["size"],
        "available_count": pool_snapshot["available"],
        "disabled_count": pool_snapshot["disabled"],
        "pool_accounts": pool_snapshot["accounts"],
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
        if db_latency_ms > 200:
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
        cycle_age = (_utcnow() - _orchestrator_last_cycle).total_seconds()
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


@app.get("/api/version")
async def get_version():
    """Public endpoint returning current backend version for frontend compatibility checks."""
    return {"sha": _current_sha, "revision": _current_revision}


@app.get("/health")
async def health_check():
    """Unauthenticated health endpoint for external monitoring (Cloud Function, load balancer)."""
    from app.shared_state import external_health

    orchestrator_alive = _orchestrator_last_cycle is not None
    cycle_age = None
    if _orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orchestrator_last_cycle).total_seconds()

    # Orchestrator is unhealthy if it hasn't cycled in 60+ seconds
    orchestrator_healthy = orchestrator_alive and (cycle_age is not None and cycle_age < 60)

    # Only DB is critical for 503 — GitHub/Claude/GCP outages shouldn't remove us from LB
    critical_healthy = external_health.get("db", True)
    degraded_deps = [
        k for k in ("github_api", "claude_api", "gcp")
        if not external_health.get(k, True)
    ]

    healthy = orchestrator_healthy and critical_healthy
    status = "healthy" if healthy and not degraded_deps else "degraded" if healthy else "unhealthy"
    status_code = 200 if healthy else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "revision": _current_revision,
            "orchestrator_alive": orchestrator_alive,
            "orchestrator_cycle_age_seconds": cycle_age,
            "external_deps": {k: v for k, v in external_health.items() if k not in ("last_check", "failed_deps", "errors")},
            "degraded_deps": degraded_deps,
        }
    )

_METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")


@app.get("/metrics")
async def prometheus_metrics(request: Request):
    """Prometheus-compatible metrics endpoint (text/plain exposition format).

    Unauthenticated by default. If METRICS_TOKEN env var is set, requires
    ?token=<value> query param matching that value.
    """
    if _METRICS_TOKEN:
        token = request.query_params.get("token", "")
        if not hmac.compare_digest(token, _METRICS_TOKEN):
            return JSONResponse(status_code=403, content={"error": "forbidden"})

    cycle_age: Optional[float] = None
    if _orchestrator_last_cycle:
        cycle_age = (datetime.utcnow() - _orchestrator_last_cycle).total_seconds()

    active_vms = sum(a.get("active_tasks", 0) for a in _max_plan_pool)
    consecutive_failures = sum(_shared_state.autonomous_failure_counts.values()) if _shared_state.autonomous_failure_counts else 0

    lines: list[str] = []

    def gauge(name: str, help_text: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        label_str = ""
        if labels:
            label_str = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"{name}{label_str} {value}")

    def counter(name: str, help_text: str, value: float) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")

    gauge("blueprint_tasks_queued", "Number of tasks currently queued", _shared_state.tasks_queued_gauge)
    gauge("blueprint_tasks_active", "Number of tasks currently provisioning or in progress", _shared_state.tasks_active_gauge)
    counter("blueprint_tasks_completed_total", "Cumulative number of completed tasks", _shared_state.tasks_completed_total)
    counter("blueprint_tasks_failed_total", "Cumulative number of failed tasks", _shared_state.tasks_failed_total)
    gauge("blueprint_orchestrator_cycle_age_seconds", "Seconds since last orchestrator cycle", cycle_age if cycle_age is not None else -1)
    gauge("blueprint_active_vms", "Current active VM count from Max Plan pool", active_vms)

    if _shared_state.circuit_breakers:
        lines.append("# HELP blueprint_circuit_breaker_open 1 if circuit breaker is OPEN, 0 otherwise")
        lines.append("# TYPE blueprint_circuit_breaker_open gauge")
        for project_id, breaker in _shared_state.circuit_breakers.items():
            is_open = 1 if breaker.get("state") == "OPEN" else 0
            lines.append(f'blueprint_circuit_breaker_open{{project_id="{project_id}"}} {is_open}')

    gauge("blueprint_autonomous_consecutive_failures", "Consecutive autonomous task failures", consecutive_failures)

    body = "\n".join(lines) + "\n"
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


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

    @field_validator("branch_name")
    @classmethod
    def validate_branch_safe(cls, v: str) -> str:
        if ".." in v:
            raise ValueError("Branch name cannot contain '..'")
        if v.startswith("/") or v.startswith("-"):
            raise ValueError("Branch name cannot start with '/' or '-'")
        return v

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
        vm_name = f"agent-vm-{uuid.uuid4().hex}"
        
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

# Log sanitization patterns — mirrors agent/src/index.ts sanitize() to catch secrets
# that bypass agent-side sanitization (compromised agent, direct WS injection)
_SANITIZE_PATTERNS = [
    (re.compile(r'https://x-access-token:[^@]+@'), 'https://x-access-token:***@'),
    (re.compile(r'ghp_[A-Za-z0-9_]+'), 'ghp_***'),
    (re.compile(r'ghs_[A-Za-z0-9_]+'), 'ghs_***'),
    (re.compile(r'github_pat_[A-Za-z0-9_]+'), 'github_pat_***'),
    (re.compile(r'sk-ant-[A-Za-z0-9_\-]+'), 'sk-ant-***'),
    (re.compile(r'eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+'), 'eyJ***'),
    (re.compile(r'Bearer\s+[^\s,\'"]+', re.IGNORECASE), 'Bearer ***'),
    (re.compile(r'oauth_token[=:]\s*[^\s&\'"]+', re.IGNORECASE), 'oauth_token=***'),
    (re.compile(r'-----BEGIN[A-Z\s]+PRIVATE\s+KEY-----[\s\S]*?-----END[A-Z\s]+PRIVATE\s+KEY-----'), '***PRIVATE KEY***'),
]

def _sanitize_log(text: str) -> str:
    """Redact secrets from log text before broadcasting to WebSocket clients."""
    if not text:
        return text
    for pattern, replacement in _SANITIZE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text

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
                    "log": _sanitize_log(data.get("content", ""))
                })
            elif data.get("type") == "status":
                # Optionally handle status updates via WS too
                pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove_ip(websocket)

@app.post("/api/agent/refresh-token")
@limiter.limit("60/minute")
async def agent_refresh_token(request: Request, token: dict = Depends(verify_agent_token)):
    """
    Accepts a valid (non-expired) agent JWT and returns a new one with a fresh 8-hour TTL.
    """
    new_token = create_agent_token(token["sub"], token["vm_instance_id"])
    return {"token": new_token}


@app.post("/api/agent/claim")
@limiter.limit("60/minute")
async def claim_task(
    request: Request,
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

@app.post("/api/agent/heartbeat")
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

def _has_local_cycle(adj: Dict[str, list]) -> bool:
    """Check for cycles in a small adjacency list (dependency graph)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in adj}
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


async def _requeue_parent_task(parent: Task, session: AsyncSession):
    """Re-evaluate a parent task by retrying its failed children.
    Cancels FAILED children and clones them as new QUEUED tasks.
    Remaps depends_on from old (cancelled) IDs to new (cloned) IDs.
    Leaves COMPLETED, CANCELLED, and active (QUEUED/IN_PROGRESS) children untouched.
    Sets parent back to EVALUATING — no VM needed."""
    children = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id)
    )).scalars().all()

    # Refresh base_branch from parent's staging branch — the original child's
    # base_branch may reference a deleted branch (e.g., plan/* after rollup merge)
    parent_ctx = parent.context or {}
    parent_staging = parent_ctx.get("plan_branch") or parent_ctx.get("research_branch")

    # Pass 1: Clone failed children, build old→new ID mapping
    old_to_new: Dict[str, str] = {}
    cloned_pairs: list = []  # (original, clone)
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
            depends_on=[],  # Set in pass 2 after all clones exist
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=child.requires_approval,
        )
        session.add(new_child)
        await session.flush()  # Assigns new_child.id
        old_to_new[str(child.id)] = str(new_child.id)
        cloned_pairs.append((child, new_child))
        retried_count += 1

        history = TaskHistory(task_id=new_child.id, action="SPAWNED",
                              details={"spawned_by": "REQUEUE", "retrying": str(child.id)})
        session.add(history)

    # Pass 2: Remap depends_on from old (cancelled) IDs to new (cloned) IDs
    for original, clone in cloned_pairs:
        remapped = []
        for dep_id in (original.depends_on or []):
            if dep_id in old_to_new:
                remapped.append(old_to_new[dep_id])  # Point to the clone
            else:
                remapped.append(dep_id)  # Keep (COMPLETED sibling or still-active task)
        clone.depends_on = remapped

    # Pass 3: In-memory cycle check on cloned subgraph
    if cloned_pairs:
        graph = {str(c.id): c.depends_on or [] for _, c in cloned_pairs}
        if _has_local_cycle(graph):
            logger.warning(f"[REQUEUE] Circular deps detected in cloned children of {parent.id} — clearing all depends_on")
            for _, clone in cloned_pairs:
                clone.depends_on = []

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
                logger.info(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (original task {orig_id[:8]})")
                source_task.latest_status_message = f"PR #{pr_number} already merged — skipping {task_type}"
                session.add(source_task)
                return None
        except Exception:
            pass
    # Also check the source task itself
    if (source_task.context or {}).get("pr_merged"):
        logger.info(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — PR already merged (source task)")
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

    # Per-PR iteration cap: count ALL completed review/fix tasks for this PR across all chains
    from sqlalchemy import func as sa_func
    pr_total = (await session.execute(
        select(sa_func.count(Task.id)).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        )
    )).scalar() or 0
    # Filter to tasks matching this PR number (check payloads JSONB)
    pr_pipeline_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        )
    )).scalars().all()
    pr_iterations = sum(
        1 for t in pr_pipeline_tasks
        if (t.payloads or [{}])[0].get("pr_number") == pr_number
        and (t.payloads or [{}])[0].get("type") in ("review", "fix")
    )
    if pr_iterations >= MAX_REVIEW_FIX_ITERATIONS:
        logger.warning(f"[PIPELINE] PR #{pr_number} hit global review-fix cap ({pr_iterations} iterations across all chains)")
        source_task.latest_status_message = f"Review-fix cap reached for PR #{pr_number} ({pr_iterations} total iterations)"
        session.add(source_task)
        return None

    # Track review-fix iteration count to prevent infinite loops (per-chain)
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

    vm_name = f"agent-vm-{uuid.uuid4().hex}"

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


def _extract_json_array(text: str, *required_keys: str) -> Optional[str]:
    """Find the first JSON array in text containing objects with the required keys.

    Uses bracket-depth counting (O(n)) instead of regex with greedy groups,
    which can cause O(n*k) backtracking on malformed input and block the
    async event loop for seconds.
    """
    # Cap input to prevent scanning megabytes of raw output
    text = text[:500_000]
    for key in required_keys:
        if f'"{key}"' not in text:
            return None
    # Find outermost [ ... ] containing the required keys
    start = text.find("[")
    while start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    if all(f'"{k}"' in candidate for k in required_keys):
                        return candidate
                    break  # This bracket pair didn't match, try next [
        start = text.find("[", start + 1)
    return None


def _parse_research_findings(text: str) -> Optional[List[Dict[str, Any]]]:
    """Extract structured research findings JSON array from agent output text."""
    json_str = _extract_json_array(text, "title", "prompt")
    if json_str:
        try:
            findings = json.loads(json_str)
            if isinstance(findings, list):
                return findings
        except json.JSONDecodeError:
            # JSON may be truncated — try to recover complete objects
            # Find last complete top-level object using depth tracking
            depth = 0
            last_close = -1
            in_string = False
            escape_next = False
            for i, c in enumerate(json_str):
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
                try:
                    truncated = json_str[:last_close + 1] + ']'
                    findings = json.loads(truncated)
                    if isinstance(findings, list):
                        logger.info(f"[RESEARCH] Recovered {len(findings)} findings from truncated JSON")
                        return findings
                except json.JSONDecodeError:
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


def _inject_file_overlap_deps(subtasks: List[Dict], label: str = "PLAN") -> None:
    """Add depends_on_indices entries to enforce serial execution for subtasks sharing files.

    For each file touched by more than one subtask, builds a chain in index order so that
    subtask[i+1] depends on subtask[i].  Auto-generated deps are merged (union, no duplicates)
    with any explicit depends_on_indices already present in the plan.
    """
    # Build file → [subtask indices] mapping
    file_to_indices: Dict[str, List[int]] = {}
    for idx, subtask in enumerate(subtasks):
        for f in subtask.get("related_files", []):
            file_to_indices.setdefault(f, []).append(idx)

    # For each file group with >1 subtask, inject chain deps
    for file_path, indices in file_to_indices.items():
        if len(indices) < 2:
            continue
        # indices are already in insertion order (which matches plan order)
        for pos in range(len(indices) - 1):
            predecessor = indices[pos]
            successor = indices[pos + 1]
            existing = subtasks[successor].setdefault("depends_on_indices", [])
            if predecessor not in existing:
                existing.append(predecessor)
                succ_title = subtasks[successor].get("title", f"#{successor}")
                pred_title = subtasks[predecessor].get("title", f"#{predecessor}")
                logger.debug(f"[{label}] Serializing subtasks {pred_title!r} → {succ_title!r} due to shared file: {file_path}")


async def _ensure_branch(
    task: Task, branch_name: str, source_branch: str,
    label: str, session: AsyncSession
) -> bool:
    """Create a branch, or verify it already exists.

    Returns True on success, False on failure (task set to FAILED).
    """
    gh_token = owner = repo = None
    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        sha = await get_branch_sha(gh_token, owner, repo, source_branch)
        await create_branch(gh_token, owner, repo, branch_name, sha)
        logger.info(f"[{label}] Created branch {branch_name} from {source_branch}")
        return True
    except Exception as e:
        if gh_token:
            try:
                await get_branch_sha(gh_token, owner, repo, branch_name)
                logger.info(f"[{label}] Branch {branch_name} already exists — reusing")
                return True
            except Exception:
                logger.error(f"[{label}] Branch {branch_name} does not exist and creation failed: {e!r}")
        else:
            logger.error(f"[{label}] Cannot create branch — GitHub token unavailable: {e!r}")
        task.status = TaskStatus.FAILED
        task.latest_status_message = f"Failed to create {label.lower()} branch: {type(e).__name__}: {e!r}"[:500]
        session.add(task)
        await session.commit()
        return False


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
    if not await _ensure_branch(task, plan_branch, parent_base, "PLAN", session):
        return

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
        logger.info(f"[PLAN] Agent already spawned {len(existing_children)} children, enriching...")
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

        logger.info(f"[PLAN] Enriched {len(existing_children)} agent-spawned subtasks for plan {task.id}")
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
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
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

    # Inject serial dependencies for subtasks that share files (additive only)
    _inject_file_overlap_deps(subtasks, "PLAN")

    # Re-validate after file overlap injection — injection can create cycles
    # (e.g., A explicitly depends on B, but file overlap adds B → A)
    if not _validate_plan_dependencies(subtasks):
        logger.error("[PLAN] Circular dependency after file overlap injection — stripping all depends_on_indices")
        for st in subtasks:
            st.pop("depends_on_indices", None)

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
        json_str = _extract_json_array(logs, "severity")
        if json_str:
            parsed = json.loads(json_str)
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
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
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
        json_str = _extract_json_array(logs, "severity")
        if json_str:
            parsed = json.loads(json_str)
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
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
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
    if not await _ensure_branch(task, research_branch, original_base, "RESEARCH", session):
        return

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
    thirty_days_ago = _utcnow() - timedelta(days=30)
    recent_tasks_for_dedup = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.created_at >= thirty_days_ago,
        )
    )).scalars().all()
    recent_finding_hashes = {
        t.context["finding_hash"] for t in recent_tasks_for_dedup if t.context and t.context.get("finding_hash")
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

    # Sort findings by priority: high → medium → low (preserve original order within each tier).
    # The orchestrator dispatches by (status, priority, created_at), so higher-priority
    # tasks naturally run first without needing artificial wave dependencies.
    priority_order = {"high": 0, "medium": 1, "low": 2}
    indexed_findings = list(enumerate(findings))
    indexed_findings.sort(key=lambda x: priority_order.get(x[1].get("priority", "medium"), 1))

    # Deduplicate findings against recent tasks
    skipped_duplicates = 0
    deduplicated: list[tuple[int, dict, str]] = []  # (orig_i, finding, fhash)
    for orig_i, finding in indexed_findings:
        title = finding.get("title", f"Research finding #{orig_i+1}")
        description = finding.get("description", finding.get("prompt", ""))
        fhash = _finding_hash(title, description)
        if fhash in recent_finding_hashes:
            logger.info(f"[RESEARCH] Skipping finding '{title}' — hash {fhash} matched recent task")
            skipped_duplicates += 1
            continue
        if _is_duplicate(title, existing_titles):
            logger.warning(f"[RESEARCH] Skipping duplicate finding '{title}' — matches existing task")
            skipped_duplicates += 1
            continue
        deduplicated.append((orig_i, finding, fhash))

    # Create all child tasks — no wave dependencies, priority ordering handles dispatch order
    child_tasks = []
    finding_indices = []  # Maps child_tasks index → findings array index

    for orig_i, finding, fhash in deduplicated:
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
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=needs_approval,
            depends_on=[],
            priority=finding_priority,
        )
        session.add(new_task)
        child_tasks.append(new_task)
        finding_indices.append(orig_i)

    await session.commit()
    for ct in child_tasks:
        await session.refresh(ct)

    # Resolve explicit depends_on_indices to actual UUIDs (agent-specified inter-finding deps only)
    finding_to_child = {fi: ci for ci, fi in enumerate(finding_indices)}
    for ci, ct in enumerate(child_tasks):
        fi = finding_indices[ci]
        dep_indices = findings[fi].get("depends_on_indices", [])
        explicit_deps = [
            str(child_tasks[finding_to_child[j]].id)
            for j in dep_indices
            if j in finding_to_child
        ]
        if explicit_deps:
            ct.depends_on = explicit_deps
            session.add(ct)

    # Log file overlap warnings for observability
    _log_file_overlaps(findings, "RESEARCH")

    await session.commit()

    # Update last_researched_at only for directions that actually produced findings,
    # not all directions in the payload. This allows directions with no findings to
    # be re-researched sooner instead of all going on cooldown simultaneously.
    direction_ids_with_findings = set()
    for ct in child_tasks:
        did = (ct.context or {}).get("direction_id")
        if did:
            direction_ids_with_findings.add(did)
    if direction_ids_with_findings:
        project = await session.get(Project, task.project_id)
        if project:
            directions = list(project.directions or [])
            now_iso = _utcnow().isoformat()
            for d in directions:
                if d.get("id") in direction_ids_with_findings:
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
            # Detect closed-but-not-merged (rejected/abandoned PR) — don't block rollup forever
            if pr_data.get("state") == "closed" and not pr_data.get("merged"):
                logger.warning(f"[PR_VERIFY] Task {task.id} PR #{pr_number} was closed without merging — marking as rejected")
                async with AsyncSessionLocal() as write_session:
                    db_task = await write_session.get(Task, task.id)
                    if db_task:
                        db_task.context = {**(db_task.context or {}), "pr_closed_unmerged": True}
                        db_task.status = TaskStatus.FAILED
                        db_task.latest_status_message = f"PR #{pr_number} closed without merging"
                        write_session.add(db_task)
                        await write_session.commit()
                task.context = {**(task.context or {}), "pr_closed_unmerged": True}
                task.status = TaskStatus.FAILED
                return True  # Return True so callers don't block — task is now FAILED which they handle
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
            # Guard 1: Don't delete if children are still active (they push to this branch)
            active_children = (await session.execute(
                select(Task).where(
                    Task.parent_task_id == task.id,
                    Task.status.in_([
                        TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS
                    ])
                )
            )).scalars().all()
            if active_children:
                logger.warning(f"[CLEANUP] Skipping deletion of {staging} — "
                               f"{len(active_children)} child task(s) still active")
            else:
                # Guard 2: Don't delete branches that are target branches for other active rollups
                from sqlalchemy import or_ as sa_or
                referencing_parents = (await session.execute(
                    select(Task).where(
                        Task.status == TaskStatus.EVALUATING,
                        Task.id != task.id,
                        sa_or(
                            Task.context["parent_base"].as_string() == staging,
                            Task.context["original_base"].as_string() == staging,
                        )
                    )
                )).scalars().all()
                if referencing_parents:
                    logger.warning(f"[CLEANUP] Skipping deletion of {staging} — "
                                   f"{len(referencing_parents)} active parent(s) target it as rollup base")
                else:
                    # Guard 3: Don't delete if there are still open PRs targeting it
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
        select(Task).where(Task.parent_task_id == parent.id).with_for_update()
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
            gh_token = owner = repo = None
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

                # Verify target branch exists before creating PR.
                # Primary protection is in _cleanup_branches (won't delete branches
                # referenced by active rollups). This catches external deletions.
                # If the branch is missing, FAIL loudly so we can catch remaining
                # causes of bad branch deletion ordering.
                try:
                    await get_branch_sha(gh_token, owner, repo, target_branch)
                except Exception as branch_err:
                    raise Exception(
                        f"Target branch '{target_branch}' does not exist — "
                        f"cannot create rollup PR. This indicates a branch was "
                        f"deleted out of order. Error: {branch_err}"
                    )

                is_target_intermediate = target_branch.startswith("research/") or target_branch.startswith("plan/")

                # Resume from prior crash — skip PR creation if already done
                existing_pr = parent_ctx.get("rollup_pr_number")
                if existing_pr:
                    # Validate PR is still open — it may have been merged/closed since crash
                    try:
                        pr_state = await get_pull_request(gh_token, owner, repo, existing_pr)
                        if pr_state.get("merged"):
                            logger.info(f"[ROLLUP] PR #{existing_pr} already merged — marking COMPLETED")
                            parent.status = TaskStatus.COMPLETED
                            parent.context = {**parent_ctx, "pr_merged": True}
                            parent.latest_status_message = f"Rollup PR #{existing_pr} already merged"
                            session.add(parent)
                            await session.commit()
                            await _cleanup_branches(parent, session)
                            await broadcast_to_project(str(parent.project_id), "task_updated",
                                                       {"task_id": str(parent.id), "status": TaskStatus.COMPLETED})
                            return
                        if pr_state.get("state") == "closed":
                            logger.warning(f"[ROLLUP] PR #{existing_pr} was closed without merge — clearing marker, will retry")
                            parent.context = {k: v for k, v in parent_ctx.items()
                                             if k not in ("rollup_pr_number", "rollup_pr_url")}
                            parent_ctx = parent.context
                            existing_pr = None
                            session.add(parent)
                            await session.commit()
                    except Exception as e:
                        logger.warning(f"[ROLLUP] Could not verify PR #{existing_pr} state: {e} — proceeding with existing marker")

                if existing_pr:
                    rollup_pr_number = existing_pr
                    rollup_pr_url = parent_ctx.get("rollup_pr_url", "")
                    logger.info(f"[ROLLUP] Resuming with existing PR #{rollup_pr_number}")
                else:
                    pr_title = f"[Blueprint] Rollup: {parent.title}"
                    pr_body = f"Rollup of {completed_count} subtasks from `{staging_branch}`{rollup_note}\n\n---\n*Automated by Blueprint Pipeline*"

                    pr_result = await create_pull_request(gh_token, owner, repo,
                        title=pr_title, head=staging_branch, base=target_branch, body=pr_body)

                    # Empty diff — branches are identical, nothing to merge
                    if pr_result.get("empty_diff"):
                        logger.info(f"[ROLLUP] Empty diff for {staging_branch} → {target_branch} — marking COMPLETED")
                        parent.status = TaskStatus.COMPLETED
                        parent.latest_status_message = f"✓ All {completed_count} subtasks completed — no new changes to merge (fixes already on {target_branch})"
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception:
                            pass
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(str(parent.project_id), "task_updated",
                                                   {"task_id": str(parent.id), "status": TaskStatus.COMPLETED})
                        return

                    rollup_pr_url = pr_result.get("html_url", "")
                    rollup_pr_number = pr_result.get("number")
                    logger.info(f"[ROLLUP] Created PR #{rollup_pr_number}: {staging_branch} → {target_branch}")

                    # Commit marker immediately — if we crash after this point,
                    # restart will find the marker and skip PR creation
                    parent.context = {**parent_ctx, "rollup_pr_number": rollup_pr_number, "rollup_pr_url": rollup_pr_url}
                    parent_ctx = parent.context  # Update local ref
                    session.add(parent)
                    await session.commit()

                # Re-acquire lock on parent for merge phase (marker commit released it)
                parent = (await session.execute(
                    select(Task).where(Task.id == parent.id).with_for_update()
                )).scalars().first()
                parent_ctx = parent.context or {}

                # Guard: concurrent sibling may have created a different rollup PR in the gap
                if parent_ctx.get("rollup_pr_number") != rollup_pr_number:
                    logger.info(f"[ROLLUP] Concurrent sibling created rollup PR for parent {parent.id} — yielding")
                    return

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
                            try:
                                await delete_branch(gh_token, owner, repo, staging_branch)
                                logger.info(f"[ROLLUP] Cleaned up staging branch {staging_branch} (fix spawn failed)")
                            except Exception as cleanup_err:
                                logger.warning(f"[ROLLUP] Failed to clean up staging branch {staging_branch}: {cleanup_err}")
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
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                            logger.info(f"[ROLLUP] Cleaned up staging branch {staging_branch} (review spawn failed)")
                        except Exception as cleanup_err:
                            logger.warning(f"[ROLLUP] Failed to clean up staging branch {staging_branch}: {cleanup_err}")
            except Exception as e:
                logger.warning(f"[ROLLUP] Failed to create rollup PR: {e}", exc_info=True)
                parent.status = TaskStatus.FAILED
                parent.latest_status_message = f"All {completed_count} subtasks done but rollup PR creation failed: {type(e).__name__}: {e!r}"[:500]
                if gh_token:
                    try:
                        await delete_branch(gh_token, owner, repo, staging_branch)
                        logger.info(f"[ROLLUP] Cleaned up staging branch {staging_branch} (rollup exception)")
                    except Exception as cleanup_err:
                        logger.warning(f"[ROLLUP] Failed to clean up staging branch {staging_branch}: {cleanup_err}")
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
@limiter.limit("120/minute")
async def update_agent_status(
    request: Request,
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
    
    # 3. Create Artifact if provided (with duplicate prevention for PR artifacts)
    if payload.artifact_type:
        if payload.artifact_type == ArtifactType.PULL_REQUEST:
            existing = (await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST
                )
            )).scalars().first()
            if existing:
                logger.info(f"[STATUS] Task {task.id} already has PR artifact, skipping duplicate")
            else:
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
        else:
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

    # 3b. Parse and store smoketest result in task.plan_output + document in history
    if payload.artifact_type == ArtifactType.SMOKETEST_RESULT and payload.logs:
        try:
            smoketest_data = json.loads(payload.logs)
            task.plan_output = smoketest_data
            session.add(task)
            # Document every smoketest result (pass or fail) in task history
            all_tests = smoketest_data.get("tests", [])
            pass_count = len([t for t in all_tests if t.get("status") == "pass"])
            fail_count = len([t for t in all_tests if t.get("status") == "fail"])
            session.add(TaskHistory(
                task_id=task.id, action="SMOKETEST_RESULT",
                details={
                    "passed": smoketest_data.get("passed", False),
                    "total_tests": len(all_tests),
                    "passed_count": pass_count,
                    "failed_count": fail_count,
                    "suite_results": smoketest_data.get("suites", {}),
                    "guardrail_violations": smoketest_data.get("guardrail_violations", []),
                }
            ))
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[SMOKETEST] Failed to parse smoketest result for task {task.id}: {e}", exc_info=True)

    # 3b-ii. On smoketest failure, spawn a plan task and create a persistent alert
    if payload.artifact_type == ArtifactType.SMOKETEST_RESULT and payload.status == TaskStatus.FAILED and payload.logs:
        try:
            smoketest_data = json.loads(payload.logs)
            if not smoketest_data.get("passed"):
                failed_tests = [t for t in smoketest_data.get("tests", []) if t.get("status") == "fail"]
                if failed_tests:
                    # Duplicate prevention: skip if a fix task already exists for this smoketest
                    existing_fix = (await session.execute(
                        select(Task).where(Task.parent_task_id == task.id).limit(1)
                    )).scalars().first()
                    if existing_fix:
                        logger.info(f"[SMOKETEST] Fix task {existing_fix.id} already exists for smoketest {task.id}, skipping duplicate")
                    else:
                        base_branch = ((task.payloads or [{}])[0]).get("base_branch", "main")
                        failure_summary = "\n".join(
                            f"- {t['name']}: {t.get('message', 'no details')}" for t in failed_tests
                        )

                        # Determine priority based on which suite failed (higher = dispatched first)
                        suite_results = smoketest_data.get("suites", {})
                        if suite_results.get("startup", {}).get("passed") is False:
                            fix_priority = 90  # Startup failure — critical
                        elif suite_results.get("curated", {}).get("passed") is False:
                            fix_priority = 70  # Curated test failure — high
                        else:
                            fix_priority = 50  # Regression-only failure — normal

                        type_model = _runtime_config.get("task_type_models", {}).get("plan") or None
                        type_effort = _runtime_config.get("task_type_efforts", {}).get("plan") or None
                        plan_task = Task(
                            project_id=task.project_id,
                            title=f"Fix smoketest failures on {base_branch}",
                            description=f"{len(failed_tests)} smoke test(s) failed after merge to {base_branch}",
                            parent_task_id=task.id,
                            priority=fix_priority,
                            payloads=[{
                                "type": "plan",
                                "prompt": (
                                    f"Smoke tests failed after a merge to {base_branch}. "
                                    f"Investigate and plan fixes for these failures:\n\n"
                                    f"{failure_summary}\n\n"
                                    f"IMPORTANT GUARDRAILS:\n"
                                    f"- Do NOT delete or skip tests just to make them pass\n"
                                    f"- First diagnose: is the failure in the CODE or the TEST?\n"
                                    f"- If the test is wrong, fix it to correctly test the intended behavior\n"
                                    f"- If the code is wrong, fix the code\n"
                                    f"- Tests may only be removed if the tested functionality was removed — explain why\n"
                                    f"- Do NOT weaken assertions without justification\n"
                                    f"Diagnose root causes, identify which recent changes caused each failure, "
                                    f"and produce subtasks to fix them."
                                ),
                                "base_branch": base_branch,
                                **({"model": type_model} if type_model else {}),
                                **({"effort": type_effort} if type_effort else {}),
                            }],
                            context={
                                "smoketest_task_id": str(task.id),
                                "failed_tests": [t["name"] for t in failed_tests],
                                "suite_results": suite_results,
                                "guardrail_violations": smoketest_data.get("guardrail_violations", []),
                                "total_tests": len(smoketest_data.get("tests", [])),
                                "passed_count": len([t for t in smoketest_data.get("tests", []) if t.get("status") == "pass"]),
                                "failed_count": len(failed_tests),
                            },
                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                            status=TaskStatus.QUEUED,
                            created_by="ORCHESTRATOR",
                        )
                        session.add(plan_task)
                        await session.flush()
                        history = TaskHistory(
                            task_id=plan_task.id, action="SPAWNED",
                            details={"spawned_by": "SMOKETEST_FAILURE", "priority": fix_priority, "failed_tests": [t["name"] for t in failed_tests]}
                        )
                        session.add(history)

                        # Build suite breakdown for alert message
                        suite_detail = ""
                        for suite_name, suite_data in suite_results.items():
                            if isinstance(suite_data, dict) and suite_data.get("passed") is False:
                                suite_fails = [t for t in suite_data.get("tests", []) if t.get("status") == "fail"]
                                if suite_fails:
                                    suite_detail += f" [{suite_name}: {len(suite_fails)} failed]"

                        alert = Alert(
                            project_id=task.project_id,
                            severity=AlertSeverity.ERROR,
                            title=f"Smoke tests failed on {base_branch}",
                            message=f"{len(failed_tests)} test(s) failed: {', '.join(t['name'] for t in failed_tests[:5])}"
                                    + (f" (+{len(failed_tests) - 5} more)" if len(failed_tests) > 5 else "")
                                    + (suite_detail if suite_detail else ""),
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
                        logger.error(f"[SMOKETEST] Spawned plan task {plan_task.id} (priority={fix_priority}) + alert for {len(failed_tests)} failed smoke tests")
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
                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
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
        # Record result for deployment monitoring and update Prometheus counters
        if payload.status == TaskStatus.COMPLETED:
            record_task_result(success=True, task_id=str(task.id))
            _shared_state.tasks_completed_total += 1
        else:
            failure_class = classify_failure(task, payload.logs)
            record_task_result(success=False, failure_class=failure_class, task_id=str(task.id))
            _shared_state.tasks_failed_total += 1

        # Release Max plan account slot
        acct_id = (task.context or {}).get("max_plan_account_id")
        if acct_id:
            await release_max_plan_account(acct_id)
            # Auto-disable account on rate limit — usage exhausted, stop wasting VMs
            # Re-enables automatically after 30min cooldown
            if payload.status == TaskStatus.FAILED and failure_class == "RATE_LIMIT":
                async with _max_plan_pool_lock:
                    for acct in _max_plan_pool:
                        if acct["id"] == acct_id:
                            acct["enabled"] = False
                            acct["disabled_at"] = _utcnow()
                            logger.warning(f"[POOL] Disabled account {acct['name']} — rate limited (will retry in 30min)")
                            break

        old_vm_id = task.vm_instance_id
        await _ensure_vm_deleted(old_vm_id, reason="agent_completion")

        # Retry logic for failed tasks
        if payload.status == TaskStatus.FAILED and task.attempt_count < _runtime_config["max_retries"]:
            task.attempt_count += 1
            task.status = TaskStatus.QUEUED
            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
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
                vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
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
                breaker["opened_at"] = _utcnow()
                breaker["half_open_task_id"] = None
                logger.warning(f"[CIRCUIT BREAKER] HALF-OPEN → OPEN for {project_key} (cooldown doubled to {breaker['cooldown_seconds']}s)")

            # CLOSED → OPEN after 3 consecutive failures
            elif breaker["state"] == "CLOSED" and breaker["failure_count"] >= 3:
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = breaker["base_cooldown"]
                await broadcast_to_project(project_key, "autonomous_paused",
                                           {"reason": f"Circuit breaker OPEN after {breaker['failure_count']} consecutive failures"})
                logger.warning(f"[CIRCUIT BREAKER] CLOSED → OPEN for {project_key} after {breaker['failure_count']} failures")
            # Persist breaker state on any transition
            if breaker["state"] != "CLOSED" or breaker["failure_count"] == 0:
                await _persist_circuit_breakers()
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
            # Persist on state transition back to CLOSED
            if breaker["state"] == "CLOSED" and breaker["failure_count"] == 0:
                await _persist_circuit_breakers()

        # ── EARLY COMMIT ──
        # Release the FOR UPDATE lock NOW, before downstream processing.
        # Everything below (review pipeline, completion handlers, rollup) makes
        # GitHub API calls that can hang 15-120+ seconds during connectivity issues.
        # Holding the lock during those calls starves the heartbeat endpoint
        # (which also needs FOR UPDATE on this row), causing last_heartbeat to go
        # stale and the reaper to kill the task.
        # At this point the status change, artifact, and retry logic are all
        # persisted.  expire_on_commit=False keeps the task object usable.
        if session.dirty or session.new:
            await session.commit()

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
                        logger.info(f"[REVIEW PIPELINE] PR #{pr_number} already {merged_label} — skipping review pipeline for task {task.id}")
                        task.latest_status_message = f"PR #{pr_number} already {merged_label} — review skipped"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                except Exception as e:
                    logger.info(f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding with review")

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
                    # Fallback: look for APPROVE/REQUEST_CHANGES as standalone words (quoted or unquoted)
                    upper_text = verdict_text.upper()
                    approve_idx = max(
                        upper_text.rfind('"APPROVE"'),
                        upper_text.rfind("APPROVE"),
                        upper_text.rfind("APPROVED"),
                    )
                    rc_idx = max(
                        upper_text.rfind('"REQUEST_CHANGES"'),
                        upper_text.rfind("REQUEST_CHANGES"),
                        upper_text.rfind("REQUEST CHANGES"),
                    )
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
                                "ci_wait_started": _utcnow().isoformat()}}
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

                        # Critical files gate: block merge if security-critical files were touched
                        if original_task_id:
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
                                        # Supported runtime_config keys for smoketest:
                                        #   health_check_urls: list[str] — live URLs to probe with HTTP GET after local tests
                                        if is_rollup_merge and base_branch == "main":
                                            try:
                                                type_model = _runtime_config.get("task_type_models", {}).get("smoketest") or None
                                                type_effort = _runtime_config.get("task_type_efforts", {}).get("smoketest") or None
                                                health_check_urls = _runtime_config.get("health_check_urls") or []
                                                smoketest_task = Task(
                                                    project_id=task.project_id,
                                                    title=f"Smoketest after rollup merge to {base_branch}",
                                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                                    payloads=[{
                                                        "type": "smoketest",
                                                        "base_branch": base_branch,
                                                        **({"model": type_model} if type_model else {}),
                                                        **({"effort": type_effort} if type_effort else {}),
                                                        **({"health_check_urls": health_check_urls} if health_check_urls else {}),
                                                    }],
                                                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
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
                                        # Spawn security rescan if merged task's parent was a security_audit
                                        if orig.parent_task_id:
                                            try:
                                                audit_parent = await session.get(Task, orig.parent_task_id)
                                                if (audit_parent and
                                                        audit_parent.payloads and
                                                        audit_parent.payloads[0].get("type") == "security_audit" and
                                                        audit_parent.status == TaskStatus.COMPLETED):
                                                    # Only rescan once per original audit
                                                    existing_rescan = (await session.execute(
                                                        select(Task).where(
                                                            Task.parent_task_id == audit_parent.id,
                                                            Task.title.ilike("%rescan%")
                                                        )
                                                    )).scalars().first()
                                                    if not existing_rescan:
                                                        try:
                                                            pr_files = await get_pr_files(gh_token, owner, repo, pr_number)
                                                            changed_files = ", ".join(f["filename"] for f in pr_files)
                                                        except Exception:
                                                            changed_files = "(could not fetch file list)"
                                                        audit_title = audit_parent.title
                                                        type_model = _runtime_config.get("task_type_models", {}).get("security_audit") or None
                                                        type_effort = _runtime_config.get("task_type_efforts", {}).get("security_audit") or None
                                                        rescan_task = Task(
                                                            project_id=orig.project_id,
                                                            title=f"Security rescan: verify {audit_title} fixes",
                                                            description=f"Verify that security fixes merged in PR #{pr_number} addressed reported vulnerabilities.",
                                                            payloads=[{
                                                                "type": "security_audit",
                                                                "prompt": (
                                                                    f"Verify that the security fixes merged in PR #{pr_number} actually addressed "
                                                                    f"the reported vulnerabilities. Focus audit on these files: {changed_files}. "
                                                                    f"Output only PASS or remaining FAIL findings."
                                                                ),
                                                                **({"model": type_model} if type_model else {}),
                                                                **({"effort": type_effort} if type_effort else {}),
                                                            }],
                                                            parent_task_id=audit_parent.id,
                                                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                            status=TaskStatus.QUEUED,
                                                            requires_approval=False,
                                                            created_by="ORCHESTRATOR",
                                                        )
                                                        session.add(rescan_task)
                                                        await session.commit()
                                                        await session.refresh(rescan_task)
                                                        history = TaskHistory(
                                                            task_id=rescan_task.id, action="SPAWNED",
                                                            details={
                                                                "spawned_by": "POST_SECURITY_FIX_MERGE",
                                                                "pr_number": pr_number,
                                                                "audit_parent_id": str(audit_parent.id),
                                                            }
                                                        )
                                                        session.add(history)
                                                        await session.commit()
                                                        await broadcast_to_project(
                                                            str(orig.project_id), "task_updated",
                                                            {"task_id": str(rescan_task.id), "status": TaskStatus.QUEUED}
                                                        )
                                                        logger.info(f"[SECURITY RESCAN] Spawned rescan task {rescan_task.id} after security fix merge PR #{pr_number}")
                                            except Exception as e:
                                                logger.info(f"[SECURITY RESCAN] Failed to spawn security rescan: {e}")
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
                        logger.info(f"[REVIEW PIPELINE] PR #{pr_number} already {merged_label} — skipping fix re-review for task {task.id}")
                        task.latest_status_message = f"PR #{pr_number} already {merged_label} — re-review skipped"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                except Exception as e:
                    logger.info(f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding")

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

            # Completion handlers — wrapped in try/except to prevent VM leak.
            # If a handler throws after the early commit, BackgroundTasks (including
            # delete_agent_vm) won't run on a 500 response. We catch, log, and ensure
            # VM cleanup via asyncio.create_task as a fallback.
            try:
                if task_type == "plan":
                    plan_logs = payload.logs or task.latest_status_message or ""
                    await _handle_plan_completion(task, plan_logs, session)

                elif task_type == "research":
                    research_logs = payload.logs or task.latest_status_message or ""
                    await _handle_research_completion(task, research_logs, session)

                elif task_type == "security_audit":
                    audit_logs = payload.logs or task.latest_status_message or ""
                    await _handle_security_audit_completion(task, audit_logs, session)

                elif task_type == "perf":
                    perf_logs = payload.logs or task.latest_status_message or ""
                    await _handle_perf_completion(task, perf_logs, session)

                elif payload.artifact_type == ArtifactType.INVESTIGATION_REPORT:
                    await _handle_investigation_completion(task, payload, session)
            except Exception as handler_exc:
                logger.error(
                    f"[STATUS] Completion handler failed for {task_type} task {task.id}: {handler_exc!r}",
                    exc_info=True,
                )
                task.latest_status_message = (
                    f"Completed but handler failed: {type(handler_exc).__name__}: {str(handler_exc)[:200]}"
                )
                session.add(task)

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

    # Broadcast AFTER all processing is complete (artifacts saved, pipeline spawned)
    await broadcast_to_project(
        str(task.project_id),
        "task_updated",
        {"task_id": str(task.id), "status": payload.status}
    )

    return {
        "status": "received",
        "message": f"Status update for task {payload.task_id} recorded."
    }

_github_token_cache: Dict[str, Dict] = {}  # project_id -> {"token", "owner", "repo", "fetched_at"}
_GITHUB_TOKEN_TTL = 3000  # 50 minutes (installation tokens expire after 60min)


def invalidate_github_token_cache(project_id: str) -> None:
    """Remove a project's cached GitHub token, forcing re-generation on next use."""
    removed = _github_token_cache.pop(project_id, None)
    if removed:
        logger.info(f"[GITHUB] Invalidated cached token for project {project_id}")


async def get_github_token_for_task(task_id: uuid.UUID, session: AsyncSession, force_refresh: bool = False) -> tuple[str, str, str]:
    stmt = select(Task).where(Task.id == task_id)
    task = (await session.execute(stmt)).scalars().first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project_id_str = str(task.project_id)

    # Return cached token if still fresh (installation tokens expire after 60min)
    if not force_refresh:
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

    # Track successful GitHub API interaction for passive health checking
    _shared_state.last_github_success = _utcnow()

    # Cache token to avoid re-generating for every API call within the same project
    _github_token_cache[project_id_str] = {
        "token": token, "owner": project.github_owner, "repo": project.github_repo,
        "fetched_at": _utcnow()
    }
    # Evict stale entries
    stale_cutoff = _utcnow() - timedelta(seconds=_GITHUB_TOKEN_TTL + 600)
    for k in [k for k, v in _github_token_cache.items() if v["fetched_at"] < stale_cutoff]:
        _github_token_cache.pop(k, None)

    return token, project.github_owner, project.github_repo

@app.get("/api/agent/github/token")
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

@app.post("/api/agent/github/commit-status")
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

@app.post("/api/agent/github/pr-comment")
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

@app.post("/api/agent/github/pr-review-comment")
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

@app.post("/api/agent/github/issues")
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

@app.get("/api/agent/github/issues")
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

@app.post("/api/agent/github/branches")
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

@app.post("/api/agent/github/pull-requests")
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

    vm_ids_to_delete = []
    for task in active_tasks:
        task.status = TaskStatus.CANCELLED
        session.add(task)
        if task.vm_instance_id:
            vm_ids_to_delete.append(task.vm_instance_id)

        await broadcast_to_project(
            str(project_id), 
            "task_updated", 
            {"task_id": str(task.id), "status": TaskStatus.CANCELLED}
        )
        
    await session.commit()
    await broadcast_to_project(str(project_id), "project_updated", {"status": ProjectStatus.CANCELLED})

    # Delete VMs after commit
    for vm_id in vm_ids_to_delete:
        await _ensure_vm_deleted(vm_id, reason="project_cancel")

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
    try:
        secret_name = await store_user_secret(current_user["user_id"], "github-pat", req.pat)
    except Exception as e:
        logger.warning(f"[PAT] Secret Manager unavailable, storing PAT directly in DB: {e}")
        secret_name = req.pat  # Fallback: store raw token in DB

    stmt = select(UserIntegration).where(UserIntegration.user_id == current_user["user_id"])
    integration = (await session.execute(stmt)).scalars().first()
    
    if integration:
        integration.github_pat = secret_name
        integration.updated_at = _utcnow()
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
        {"sub": current_user["user_id"], "exp": _utcnow() + timedelta(minutes=10), "type": "github_oauth"},
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
            integration.updated_at = _utcnow()
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
        
    # Dedup by GitHub delivery ID — prevents duplicate processing on webhook retries
    delivery_id = request.headers.get("x-github-delivery")
    if delivery_id:
        async with _webhook_dedup_lock:
            # Evict stale entries (older than TTL)
            now = time.time()
            while _webhook_delivery_ids:
                oldest_id, oldest_time = next(iter(_webhook_delivery_ids.items()))
                if now - oldest_time > _WEBHOOK_DEDUP_TTL:
                    _webhook_delivery_ids.pop(oldest_id)
                else:
                    break
            if delivery_id in _webhook_delivery_ids:
                logger.info(f"[WEBHOOK] Duplicate delivery {delivery_id}, skipping")
                return {"status": "duplicate", "delivery_id": delivery_id}
            _webhook_delivery_ids[delivery_id] = now
            if len(_webhook_delivery_ids) > _WEBHOOK_DEDUP_MAX:
                _webhook_delivery_ids.popitem(last=False)

    event_type = request.headers.get("x-github-event")
    data = json.loads(payload)

    logger.info(f"[WEBHOOK] Received {event_type} event (delivery={delivery_id})")

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

                # Propagate pr_merged to tasks that created this PR
                pr_number = data["pull_request"].get("number")
                if pr_number:
                    pr_url_suffix = f"/{owner}/{repo}/pull/{pr_number}"
                    art_result = await session.execute(
                        select(Artifact).where(
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            Artifact.pr_url.ilike(f"%{pr_url_suffix}"),
                        )
                    )
                    pr_artifacts = art_result.scalars().all()
                    affected_tasks = []
                    for art in pr_artifacts:
                        pr_task = await session.get(Task, art.task_id)
                        if pr_task and not (pr_task.context or {}).get("pr_merged"):
                            pr_task.context = {**(pr_task.context or {}), "pr_merged": True}
                            session.add(pr_task)
                            affected_tasks.append(pr_task)
                            logger.info(f"[WEBHOOK] Set pr_merged on task {pr_task.id} (PR #{pr_number})")
                    if affected_tasks:
                        await session.commit()
                        for at in affected_tasks:
                            await _check_and_trigger_rollup(at, session)

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
