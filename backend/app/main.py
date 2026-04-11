import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import re
import time
import uuid
import asyncio
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Literal, Optional
from fastapi import FastAPI, Depends, HTTPException, Header, BackgroundTasks, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import func as _func
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select
from app.database import init_db, get_session, AsyncSessionLocal, engine
from app.models import Project, ProjectMember, Task, Artifact, TaskHistory, ProjectStatus, TaskStatus, ArtifactType, UserIntegration, AllowedUser, MaxPlanAccount, DeploymentState, DeploymentStatus, Alert, AlertSeverity, ProjectMessage, MessageType
from app.compute import provision_agent_vm, delete_agent_vm, cleanup_orphaned_vms, get_provision_error, get_vm_zone, reconcile_orphaned_secrets, any_zone_available, soonest_zone_recovery
from app.storage import upload_json_payload
from app.secrets_manager import store_user_secret, get_user_secret
from app.auth import get_current_user, verify_agent_jwt, create_agent_token, create_ws_ticket, verify_ws_ticket
from app.telemetry import setup_telemetry
from app.admin import router as admin_router
from app.routers.agent import router as agent_router
from app.routers.agent import verify_agent_token, get_github_token_for_task, invalidate_github_token_cache, _github_token_cache, _has_local_cycle
from app.routers.github import router as github_router
from app.routers.projects import router as projects_router
from app.routers.directions import router as directions_router
from app.routers.deploy import router as deploy_router
from app.security import scan_pr_for_injection
from app.github import (
    validate_fine_grained_pat,
    generate_installation_access_token,
    GITHUB_WEBHOOK_SECRET,
    GITHUB_CLIENT_ID,
    exchange_oauth_code,
    get_user_installations,
    get_github_user,
    create_pr_comment,
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
from app.shared_state import limiter
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app import orchestrator as _orch
from app.lifecycle import lifespan
from app.reaper import reaper_service  # noqa: F401 — re-exported for test patches
from app.services import (  # noqa: F401 — re-exported for test patches
    gcp_cleanup_service,
    secret_reconciliation_service,
    _persist_circuit_breakers,
    _persist_zone_health,
    _check_evaluating_timeouts,
    _recover_orphaned_tasks,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cap_str(value: Any, max_len: int, field_name: str, warnings: Optional[List[str]] = None) -> str:
    """Cap a string field, appending a human-readable warning if truncation occurred."""
    s = str(value) if value is not None else ""
    if len(s) > max_len:
        if warnings is not None:
            warnings.append(
                f"{field_name} was truncated from {len(s)} to {max_len} chars — "
                f"resubmit with content under {max_len} chars to avoid losing text"
            )
        return s[:max_len]
    return s


def _cap_list(lst: list, max_items: int, field_name: str, warnings: Optional[List[str]] = None) -> list:
    """Cap a list to the last N items, warning if older items were dropped."""
    if len(lst) > max_items:
        dropped = len(lst) - max_items
        if warnings is not None:
            warnings.append(
                f"{field_name} kept only the newest {max_items} of {len(lst)} items — "
                f"{dropped} oldest item(s) were dropped"
            )
        return lst[-max_items:]
    return lst


# Strategic / agent-facing input caps — raised from previous 500/40/5000 values.
# Any truncation at these caps must surface a warning in the API response.
STRATEGIC_DIRECTION_TEXT_MAX = 2000
STRATEGIC_RATIONALE_MAX = 2000
STRATEGIC_SYSTEM_ID_MAX = 100
STRATEGIC_NOTES_HISTORY_MAX = 50
STRATEGIC_MESSAGE_CONTENT_MAX = 20000
STRATEGIC_RESOLUTION_MAX = 20000
STATUS_LATEST_MESSAGE_MAX = 2000

# Expected schema revision — must match the latest alembic migration.
# Update this when adding new migrations.
EXPECTED_SCHEMA_REVISION = "0031"

# --- Task Priority Map ---
# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}

# GCP VM count cache — updated by cleanup service, used by orchestrator concurrency check
_last_gcp_vm_count: int = 0
_last_gcp_vm_check: float = 0.0
_GCP_VM_CHECK_INTERVAL: float = 120.0  # Seconds between GCP VM count refreshes


# --- Max Plan Account Pool ---
# In-memory cache of DB-persisted accounts. Keeps active_tasks count (runtime only).
# Per-user pools keyed by user_id. Flat lookup by account_id for O(1) release.
_max_plan_pools: Dict[str, List[Dict[str, Any]]] = {}  # user_id → accounts
_account_by_id: Dict[str, Dict[str, Any]] = {}  # account_id → account dict
_max_plan_pool_lock = asyncio.Lock()
_pending_rl_persists: List[str] = []  # Account IDs whose rate-limit state failed to persist

# Rate-limit keyword patterns — shared between classify_failure() and reaper
RATE_LIMIT_KEYWORDS = ["rate limit", "rate_limit", "429", "credit", "billing",
                       "usage limit", "usage exhausted", "too many requests",
                       "resource_exhausted", "daily limit", "tokens per minute"]

# Backward compat: flat view for code that needs all accounts
def _all_accounts() -> List[Dict[str, Any]]:
    return list(_account_by_id.values())

async def load_max_plan_pool():
    """Load Max plan accounts from database into per-user pools on startup.
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

        _max_plan_pools.clear()
        _account_by_id.clear()
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
            entry = {
                "id": acct_id,
                "user_id": acct.user_id,
                "name": acct.name,
                "oauth_token": token,
                "claude_config_json": acct.claude_config_json,
                "enabled": acct.enabled,
                "active_tasks": active_per_account.get(acct_id, 0),
                "total_tasks": acct.total_tasks,
                "token_created_at": acct.token_created_at.isoformat() if acct.token_created_at else None,
            }
            # Restore rate-limit state from DB (skip expired cooldowns)
            rl_state = acct.rate_limit_state or {}
            db_disabled_models = rl_state.get("disabled_models", {})
            if db_disabled_models:
                now = _utcnow()
                restored_dm = {}
                for model_name, info in db_disabled_models.items():
                    at_str = info.get("at")
                    if at_str:
                        at_dt = datetime.fromisoformat(at_str)
                        hits = info.get("hits", 1)
                        cooldown_minutes = min(30 * (2 ** (hits - 1)), 240)
                        if (now - at_dt).total_seconds() < cooldown_minutes * 60:
                            restored_dm[model_name] = {"hits": hits, "at": at_dt}
                if restored_dm:
                    entry["disabled_models"] = restored_dm
                    logger.info(f"[STARTUP] Restored rate-limit state for {acct.name}: {list(restored_dm.keys())}")
            _max_plan_pools.setdefault(acct.user_id, []).append(entry)
            _account_by_id[acct_id] = entry
        total_active = sum(active_per_account.values())
        logger.info(f"[STARTUP] Loaded {len(_account_by_id)} Max plan accounts for {len(_max_plan_pools)} users ({total_active} active tasks reconciled)")

def _account_available_for_model(acct: dict, model: Optional[str]) -> bool:
    """Check if an account is available for the given model."""
    if not acct.get("oauth_token"):
        return False  # Token missing (Secret Manager failure at startup)
    if not acct.get("enabled", True):
        return False  # Entirely disabled (legacy path)
    dm = acct.get("disabled_models", {})
    if not model:
        # No model specified — account is available only if it has no disabled models.
        # Most tasks don't carry an explicit model, so we must respect per-model disables
        # even when model is None, otherwise rate-limited accounts get re-selected.
        return len(dm) == 0
    return model not in dm


def _extract_task_model(task: Task) -> Optional[str]:
    """Extract which model a task uses from its payload."""
    payloads = task.payloads or []
    if payloads:
        return payloads[0].get("model")
    return None


async def select_max_plan_account(user_id: str, model: Optional[str] = None, blacklist: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Select the Max plan account with the lowest active task count for a specific user.
    Filters by model availability — accounts with a specific model disabled are skipped.
    Pre-increments active_tasks atomically under lock to prevent races."""
    async with _max_plan_pool_lock:
        user_pool = _max_plan_pools.get(user_id, [])
        available = [
            a for a in user_pool
            if _account_available_for_model(a, model) and (not blacklist or a["id"] not in blacklist)
        ]
        if not available:
            return None
        account = min(available, key=lambda a: a["active_tasks"])
        account["active_tasks"] = account.get("active_tasks", 0) + 1
        return account

async def release_max_plan_account(account_id: str):
    """Decrement active tasks for an account when a task finishes."""
    async with _max_plan_pool_lock:
        account = _account_by_id.get(account_id)
        if account:
            if account["active_tasks"] <= 0:
                logger.warning(f"[POOL] Account {account['name']} ({account_id}) release underflow — active_tasks was {account['active_tasks']}")
            account["active_tasks"] = max(0, account["active_tasks"] - 1)

async def recheck_disabled_accounts():
    """Re-enable accounts/models that were disabled by rate limiting after cooldown.
    Checks both per-model disables and legacy full-account disables."""
    changed_accounts = []
    async with _max_plan_pool_lock:
        now = _utcnow()

        for acct in _account_by_id.values():
            # Per-model cooldown checks
            dm = acct.get("disabled_models", {})
            for model_name, info in list(dm.items()):
                disabled_at = info.get("at")
                if not disabled_at:
                    continue
                hits = info.get("hits", 1)
                cooldown_minutes = min(30 * (2 ** (hits - 1)), 240)
                if (now - disabled_at).total_seconds() >= cooldown_minutes * 60:
                    del dm[model_name]
                    changed_accounts.append(acct)
                    logger.warning(f"[POOL] Re-enabled account {acct['name']} model {model_name} after {cooldown_minutes:.0f}min cooldown (hit #{hits})")

            # Legacy full-account disable (backward compat)
            if not acct.get("enabled", True):
                disabled_at = acct.get("disabled_at")
                if not disabled_at:
                    continue
                rl_hit_count = acct.get("rl_hit_count", 1)
                cooldown_minutes = min(30 * (2 ** (rl_hit_count - 1)), 240)
                if (now - disabled_at).total_seconds() >= cooldown_minutes * 60:
                    acct["enabled"] = True
                    acct["disabled_at"] = None
                    changed_accounts.append(acct)
                    logger.warning(f"[POOL] Re-enabled account {acct['name']} after {cooldown_minutes:.0f}min cooldown (rl_hit #{rl_hit_count})")

    # Retry any previously failed rate-limit persists
    if _pending_rl_persists:
        retry_ids = list(_pending_rl_persists)
        for acct_id in retry_ids:
            acct = _account_by_id.get(acct_id)
            if acct and acct not in changed_accounts:
                changed_accounts.append(acct)

    # Persist changes to DB (outside lock to avoid holding it during IO)
    if changed_accounts:
        try:
            async with AsyncSessionLocal() as rl_session:
                for acct in changed_accounts:
                    db_acct = await rl_session.get(MaxPlanAccount, uuid.UUID(acct["id"]))
                    if db_acct:
                        dm = acct.get("disabled_models", {})
                        db_acct.rate_limit_state = {
                            "disabled_models": {
                                m: {"hits": i["hits"], "at": i["at"].isoformat()}
                                for m, i in dm.items()
                            }
                        } if dm else None
                        db_acct.enabled = acct.get("enabled", True)
                        rl_session.add(db_acct)
                await rl_session.commit()
            # Clear any retried accounts on success
            for acct in changed_accounts:
                if acct["id"] in _pending_rl_persists:
                    _pending_rl_persists.remove(acct["id"])
        except Exception as e:
            logger.warning(f"[POOL] Failed to persist rate-limit state changes: {e}")


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

    # Database / internal service unavailable — checked before provision failures because
    # PostgreSQL errors like "could not connect to server: Connection refused" contain
    # "connection refused" which would otherwise be misclassified as PROVISION_FAILURE.
    if any(kw in combined for kw in ["could not connect to server", "connection to server",
                                      "database connection", "psycopg", "asyncpg",
                                      "operator does not exist"]):
        return "EXTERNAL_API"

    # Provision failures — GCP quota, network, VM startup issues, SSH/init script errors, disk exhaustion
    if any(kw in combined for kw in ["provision", "quota", "cpus_all_regions", "in_use_addresses",
                                      "vm failed to start", "failed to provision",
                                      "connection refused", "connection reset by peer",
                                      "timed out waiting for operation", "sshd", "startup script",
                                      "no space left on device", "disk quota exceeded",
                                      "filesystem is full",
                                      "address already in use", "failed to connect"]):
        return "PROVISION_FAILURE"

    # Transient API overload — server-side, not account-specific (must be checked before RATE_LIMIT
    # because "overloaded" would otherwise trigger the rate limit classifier and disable accounts)
    if "overloaded" in combined and not any(kw in combined for kw in ["usage limit", "usage exhausted", "billing", "credit"]):
        return "EXTERNAL_API"

    # GitHub rate limits (429, "rate limit") — external API issue, NOT Claude account exhaustion.
    # Must be checked before generic RATE_LIMIT to prevent disabling Claude accounts for GitHub outages.
    if any(gh_kw in combined for gh_kw in ["github", "api.github.com"]) and \
       any(kw in combined for kw in ["rate limit", "429", "too many requests"]):
        return "EXTERNAL_API"

    # Rate limit — Claude API usage exhausted, account-level billing/quota issues
    if any(kw in combined for kw in RATE_LIMIT_KEYWORDS):
        return "RATE_LIMIT"

    # External API failures — GitHub, Claude API outages, DNS/TLS/network errors
    if any(kw in combined for kw in ["api.github.com", "api.anthropic.com",
                                      "502 bad gateway", "503 service unavailable",
                                      "github api error", "claude api",
                                      "connection timed out", "name resolution failed",
                                      "ssl", "certificate",
                                      "context_length_exceeded", "token limit exceeded"]):
        return "EXTERNAL_API"

    # Agent timeout — reaped by heartbeat, execution timeout, process killed by OS/orchestrator, OOM
    if any(kw in combined for kw in ["no heartbeat", "reaped", "never started", "timed out",
                                      "execution timed out", "killed", "sigterm",
                                      "out of memory", "oom-kill", "memory limit exceeded",
                                      "cannot allocate memory",
                                      "exit code 137", "exit code 143",
                                      "non-zero exit code", "process exited",
                                      "container exited", "node process killed",
                                      "oom killer"]):
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

# Webhook dedup state moved to app.routers.github

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
            # Wrapped in try/except to handle race between multiple Cloud Run instances
            try:
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
            except Exception:
                await session.rollback()
                # Another instance likely inserted first — fetch the row they created
                state = await session.get(DeploymentState, 1)
                if not state:
                    raise  # Genuine error, not a race

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
            if getattr(state, 'runtime_autonomous_direction_cooldown_minutes', None) is not None:
                _runtime_config["autonomous_direction_cooldown_minutes"] = state.runtime_autonomous_direction_cooldown_minutes
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore autonomous research config (migration pending?): {e}", exc_info=True)
        # Restore dict/list runtime config from JSONB column
        try:
            config_json = getattr(state, 'runtime_config_json', None)
            if config_json and isinstance(config_json, dict):
                for key in ("task_type_models", "task_type_efforts", "agent_prompt_suffixes",
                             "autonomous_periodic_tasks", "health_check_urls"):
                    if key in config_json:
                        _runtime_config[key] = config_json[key]
                if "autonomous_research_wave_size" in config_json:
                    _runtime_config["autonomous_research_wave_size"] = int(config_json["autonomous_research_wave_size"])
                if "strategic_research_frequency" in config_json:
                    _runtime_config["strategic_research_frequency"] = int(config_json["strategic_research_frequency"])
                logger.info(f"[DEPLOY] Restored runtime_config_json: {list(config_json.keys())}")
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore runtime_config_json (migration pending?): {e}", exc_info=True)
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
                        if not isinstance(data, dict):
                            continue
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

        # Restore pause/health streaks
        try:
            if state.zone_health_state:
                global _pause_failure_streak, _health_recovery_streak
                _pause_failure_streak = state.zone_health_state.get("_pause_failure_streak", 0)
                _health_recovery_streak = state.zone_health_state.get("_health_recovery_streak", 0)
                if _pause_failure_streak or _health_recovery_streak:
                    logger.info(f"[DEPLOY] Restored health streaks: pause_failures={_pause_failure_streak}, recovery={_health_recovery_streak}")
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore health streaks: {e}", exc_info=True)

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
                        await _create_halted_recovery_alerts(session)
                        await session.commit()
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

            await _create_rollback_alerts(session, state.rolled_back_from_sha or "", _current_sha)
            await session.commit()

            if state.rollback_count >= 3:
                state.status = DeploymentStatus.HALTED
                state.halted_at = _utcnow()
                session.add(state)
                await session.commit()
                logger.error(f"[DEPLOY] HALTED — {state.rollback_count} consecutive rollbacks, manual intervention required")
                _runtime_config["max_concurrent_vms"] = 0
                await _create_halted_alerts(session, state.rollback_count, state.last_known_good_revision, state.halted_at)
                await session.commit()
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
            import httpx
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

    # Claude API — check credentials + account availability
    has_credentials = bool(_account_by_id) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_credentials:
        ext["claude_api"] = False
        failed.append("claude_api")
    else:
        # For Max plan account pools: only mark unhealthy if ALL accounts are disabled.
        # Individual account rate limits are handled by per-account cooldowns —
        # the pipeline can continue with remaining enabled accounts.
        all_accounts = list(_account_by_id.values())
        if all_accounts:
            # Use _account_available_for_model to check: has token, enabled, not all models disabled
            available_accounts = [a for a in all_accounts if _account_available_for_model(a, None)]
            if not available_accounts:
                ext["claude_api"] = False
                failed.append("claude_api")
                disabled_names = [a["name"] for a in all_accounts]
                logger.warning(f"[HEALTH] All Claude API accounts unavailable: {', '.join(disabled_names)}")
            else:
                ext["claude_api"] = True
                # Log partial degradation for visibility (not a pipeline pause)
                unavailable_accounts = [a for a in all_accounts if not _account_available_for_model(a, None)]
                if unavailable_accounts:
                    logger.info(f"[HEALTH] Claude API degraded — {len(unavailable_accounts)}/{len(all_accounts)} accounts unavailable, {len(available_accounts)} still available")
        else:
            # No account pool — using raw ANTHROPIC_API_KEY. Fall back to burst detection
            # since there's no per-account cooldown mechanism for a single key.
            recent_results = list(_shared_state.deploy_monitoring["task_results"])[-5:]
            rate_limit_count = sum(1 for r in recent_results if r[2] == "RATE_LIMIT")
            if rate_limit_count >= 3:
                ext["claude_api"] = False
                failed.append("claude_api")
                logger.warning(f"[HEALTH] Claude API rate limited (single key) — {rate_limit_count}/5 recent tasks hit limits")
            else:
                ext["claude_api"] = True

    # GCP — check is implicit via provisioning success/failure tracking
    # Mark unhealthy only if recent provisions all failed
    recent_provisions = [r for r in _shared_state.deploy_monitoring["task_results"]
                         if r[2] == "PROVISION_FAILURE"]
    if len(recent_provisions) >= 3:
        # Check if ALL recent failures are provision failures (guard against empty filter)
        recent_all = list(_shared_state.deploy_monitoring["task_results"])[-5:]
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


async def _send_webhook_notification(event_type: str, title: str, details: str) -> None:
    """Fire-and-forget webhook notification for critical pipeline events."""
    url = os.getenv("PIPELINE_WEBHOOK_URL")
    if not url:
        return
    import httpx
    payload = {
        "event_type": event_type,
        "title": title,
        "details": details,
        "timestamp": _utcnow().isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(f"[WEBHOOK] POST returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"[WEBHOOK] Failed to send notification: {e}")


async def _create_auto_pause_alerts(session: AsyncSession, failed_deps: list) -> None:
    """Create WARNING alerts on all projects when the pipeline auto-pauses due to external dep failures."""
    title = "Pipeline auto-paused: external dependencies down"
    msg = (
        f"Pipeline auto-paused: external dependencies down — {', '.join(failed_deps)}. "
        "Will auto-resume when healthy."
    )
    projects = (await session.execute(select(Project))).scalars().all()
    for project in projects:
        existing = (await session.execute(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.task_id.is_(None),
                Alert.severity == AlertSeverity.WARNING,
                Alert.dismissed_at.is_(None),
                Alert.title == title,
            )
        )).scalars().first()
        if existing is not None:
            continue
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(project.id),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
        )
    await _send_webhook_notification("AUTO_PAUSED", title, msg)


async def _create_auto_resume_alerts(session: AsyncSession) -> None:
    """Create INFO alerts and dismiss existing auto-pause warnings when the pipeline auto-resumes."""
    pause_title = "Pipeline auto-paused: external dependencies down"
    resume_title = "Pipeline auto-resumed: all external dependencies healthy."
    now = _utcnow()
    projects = (await session.execute(select(Project))).scalars().all()
    for project in projects:
        # Dismiss existing auto-pause WARNING alerts for this project
        existing_warnings = (await session.execute(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.severity == AlertSeverity.WARNING,
                Alert.dismissed_at.is_(None),
                Alert.title == pause_title,
            )
        )).scalars().all()
        for w in existing_warnings:
            w.dismissed_at = now
            session.add(w)
        # Create INFO resume alert
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.INFO,
            title=resume_title,
            message=resume_title,
        )
        session.add(alert)
        await broadcast_to_project(
            str(project.id),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "INFO", "title": alert.title},
        )


async def _create_orchestrator_error_alerts(session: AsyncSession, consecutive_errors: int) -> None:
    """Create ERROR alerts on all projects when the orchestrator hits repeated errors (deduplicated)."""
    title = "Orchestrator error loop"
    msg = (
        f"The orchestrator has encountered {consecutive_errors} consecutive errors. "
        "The pipeline may be stalled. Check logs for details."
    )
    projects = (await session.execute(select(Project))).scalars().all()
    for project in projects:
        existing = (await session.execute(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.task_id.is_(None),
                Alert.severity == AlertSeverity.ERROR,
                Alert.dismissed_at.is_(None),
                Alert.title == title,
            )
        )).scalars().first()
        if existing is not None:
            continue
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(project.id),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
        )


async def _create_halted_alerts(session: AsyncSession, rollback_count: int, last_known_good: str, halted_at: "datetime") -> None:
    """Create ERROR alerts on all projects when deployment enters HALTED state."""
    title = f"Deployment HALTED — {rollback_count} consecutive rollbacks, manual intervention required"
    msg = (
        f"Deployment halted after {rollback_count} consecutive rollbacks. "
        f"Last known good revision: {last_known_good or 'unknown'}. "
        f"Halted at: {halted_at.isoformat()}. "
        "Pipeline will auto-recover after 1h cooldown, or manually set max VMs > 0."
    )
    projects = (await session.execute(select(Project))).scalars().all()
    for project in projects:
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.ERROR,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(project.id),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
        )
    await _send_webhook_notification("PIPELINE_HALTED", title, msg)


async def _create_rollback_alerts(session: AsyncSession, rolled_back_from: str, rolled_back_to: str) -> None:
    """Create WARNING alerts on all projects when an automatic rollback is detected.
    Deduplicates: skips if an undismissed rollback alert already exists for a project."""
    title = "Automatic rollback detected — revision reverted to last known good"
    msg = (
        f"Deployment automatically rolled back from {rolled_back_from[:8]} "
        f"to {rolled_back_to[:8]}."
    )
    projects = (await session.execute(select(Project))).scalars().all()
    for project in projects:
        existing = (await session.execute(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.task_id.is_(None),
                Alert.dismissed_at.is_(None),
                Alert.title.contains("Automatic rollback"),
            )
        )).scalars().first()
        if existing is not None:
            continue
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.WARNING,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(project.id),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
        )


async def _create_halted_recovery_alerts(session: AsyncSession) -> None:
    """Create INFO alerts and dismiss existing HALTED ERROR alerts when deployment auto-recovers."""
    recovery_title = "Deployment auto-recovered from HALTED state after 1h cooldown"
    now = _utcnow()
    projects = (await session.execute(select(Project))).scalars().all()
    for project in projects:
        # Dismiss existing HALTED ERROR alerts for this project
        existing_halted = (await session.execute(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.severity == AlertSeverity.ERROR,
                Alert.dismissed_at.is_(None),
                Alert.title.contains("HALTED"),
            )
        )).scalars().all()
        for h in existing_halted:
            h.dismissed_at = now
            session.add(h)
        # Create INFO recovery alert
        alert = Alert(
            project_id=project.id,
            severity=AlertSeverity.INFO,
            title=recovery_title,
            message=recovery_title,
        )
        session.add(alert)
        await broadcast_to_project(
            str(project.id),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "INFO", "title": alert.title},
        )


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
                    await _create_auto_pause_alerts(session, critical_failures)
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
                await _create_auto_resume_alerts(session)
                await session.commit()
                initial_vms = min(2, restore)
                _runtime_config["max_concurrent_vms"] = initial_vms
                _health_recovery_streak = 0
                logger.info(f"[PAUSE] Auto-resumed pipeline with {initial_vms} VMs (target: {restore})")
                await broadcast_deploy_state()


def _compute_merge_rate(d: dict) -> float:
    """Return merged-PRs-per-cycle for a direction. Returns 0.5 (neutral) if never researched."""
    history = d.get("research_history", [])
    agg = d.get("history_aggregate", {})
    total_cycles = len(history) + (agg.get("total_cycles") or 0)
    total_merged = sum(e.get("prs_merged", 0) for e in history) + (agg.get("total_prs_merged") or 0)
    if total_cycles == 0:
        return 0.5  # unknown, neutral
    return total_merged / total_cycles


def _build_outcome_evaluation(project) -> dict:
    """Build a planned-vs-actual assessment for the strategic agent.

    Compares what the vision predicted against what directions actually produced.
    All data lives on project.directions and project.vision — no DB queries needed.
    """
    vision = project.vision or {}
    directions = project.directions or []
    now_iso = _utcnow().isoformat()

    # Determine evaluation window: since last vision update, or all time
    window_from = vision.get("last_updated")
    vision_rev = vision.get("revision", 0)

    # Per-direction outcomes
    direction_outcomes = []
    for d in directions:
        status = d.get("status", "active")
        if status not in ("active", "expired", "paused"):
            continue
        history = d.get("research_history", [])
        agg = d.get("history_aggregate", {})

        # Filter history to evaluation window
        if window_from:
            window_history = [h for h in history if (h.get("timestamp") or "") >= window_from]
        else:
            window_history = list(history)

        cycles = len(window_history)
        merged = sum(h.get("prs_merged", 0) for h in window_history)
        failed = sum(h.get("tasks_failed", 0) for h in window_history)

        # Extract confirmed vs proposed findings from research_context
        confirmed = []
        proposed = []
        failed_approaches = []
        ctx = d.get("research_context", "")
        if ctx:
            parsed = _parse_research_context(ctx)
            for item in parsed.get("established", []):
                if "(confirmed)" in item:
                    confirmed.append(item.replace("(confirmed)", "").strip())
                elif "(proposed)" in item:
                    proposed.append(item.replace("(proposed)", "").strip())
                else:
                    confirmed.append(item)  # Unmarked items are treated as confirmed
            failed_approaches = parsed.get("failed", [])

        outcome: dict = {
            "id": d.get("id"),
            "text": (d.get("text") or "")[:200],
            "system_id": d.get("system_id"),
            "status": status,
            "cycles": cycles,
            "merged": merged,
            "failed": failed,
        }
        if confirmed:
            outcome["confirmed"] = confirmed[:15]
        if proposed:
            outcome["proposed"] = proposed[:10]
        if failed_approaches:
            outcome["failed_approaches"] = failed_approaches[:10]
        # Include lifetime totals for context
        lifetime_cycles = len(history) + (agg.get("total_cycles") or 0)
        lifetime_merged = sum(h.get("prs_merged", 0) for h in history) + (agg.get("total_prs_merged") or 0)
        if lifetime_cycles > cycles:
            outcome["lifetime"] = {"cycles": lifetime_cycles, "merged": lifetime_merged}

        direction_outcomes.append(outcome)

    # Overall health
    total_cycles = sum(d["cycles"] for d in direction_outcomes)
    total_merged = sum(d["merged"] for d in direction_outcomes)
    total_failed = sum(d["failed"] for d in direction_outcomes)
    success_rate = round(total_merged / max(total_merged + total_failed, 1), 2)

    # Trend: compare first half vs second half of all window history entries
    all_window_entries = []
    for d in directions:
        history = d.get("research_history", [])
        if window_from:
            entries = [h for h in history if (h.get("timestamp") or "") >= window_from]
        else:
            entries = list(history)
        all_window_entries.extend(entries)
    all_window_entries.sort(key=lambda h: h.get("timestamp") or "")
    trend = "stable"
    if len(all_window_entries) >= 4:
        mid = len(all_window_entries) // 2
        first_half = all_window_entries[:mid]
        second_half = all_window_entries[mid:]
        first_rate = sum(h.get("prs_merged", 0) for h in first_half) / max(len(first_half), 1)
        second_rate = sum(h.get("prs_merged", 0) for h in second_half) / max(len(second_half), 1)
        if second_rate > first_rate * 1.3:
            trend = "improving"
        elif second_rate < first_rate * 0.7:
            trend = "declining"

    # System-level status — cross-reference vision systems with direction outcomes
    systems = []
    vision_systems = vision.get("systems", [])
    if isinstance(vision_systems, list):
        for sys in vision_systems:
            if not isinstance(sys, dict):
                continue
            sys_id = sys.get("id")
            if not sys_id:
                continue
            # Find directions mapped to this system
            mapped = [d for d in direction_outcomes if d.get("system_id") == sys_id]
            sys_merged = sum(d["merged"] for d in mapped)
            sys_failed = sum(d["failed"] for d in mapped)
            systems.append({
                "id": sys_id,
                "name": sys.get("name", ""),
                "planned_status": sys.get("status", "unknown"),
                "directions_count": len(mapped),
                "merged": sys_merged,
                "failed": sys_failed,
            })

    result: dict = {
        "window": {"from": window_from or "all_time", "to": now_iso, "vision_revision": vision_rev},
        "overall": {
            "cycles": total_cycles, "merged": total_merged, "failed": total_failed,
            "success_rate": success_rate, "trend": trend,
        },
    }
    if systems:
        result["systems"] = systems
    if direction_outcomes:
        result["directions"] = direction_outcomes
    return result


async def _spawn_design_doc_update(project: Project, session: AsyncSession):
    """Spawn a design doc update task (Phase 1 of strategic cycle).

    This runs as a separate task type so it doesn't block tactical research.
    On completion, it chains to _spawn_strategic_review (Phase 2).
    """
    # Guard: no design_doc_update or strategic_review already running
    active = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    for t in active:
        t_type = (t.first_payload).get("type")
        if t_type in ("design_doc_update", "strategic_review"):
            logger.debug(f"[STRATEGIC] Skipping design doc update — {t_type} task {t.id} already active")
            return

    doc_model = _runtime_config.get("task_type_models", {}).get("design_doc_update") or \
                _runtime_config.get("task_type_models", {}).get("research") or None
    doc_effort = _runtime_config.get("task_type_efforts", {}).get("design_doc_update") or \
                 _runtime_config.get("task_type_efforts", {}).get("research") or None

    # Fetch open messages for the design doc agent
    open_msgs_result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status == "open",
        ).order_by(ProjectMessage.created_at.asc()).limit(20)
    )
    open_msgs = [
        {"id": str(m.id), "type": m.message_type, "content": m.content,
         "context": m.context, "from": m.from_type,
         "created_at": m.created_at.isoformat() if m.created_at else None}
        for m in open_msgs_result.scalars().all()
    ]

    payload: dict = {
        "type": "design_doc_update",
        "design_doc": project.design_doc or {},
        "proposed_changes": (project.design_doc or {}).get("proposed_changes", []),
        "project_name": project.name,
        "vision": project.vision or {},
        "open_messages": open_msgs,
        "base_branch": "main",
        **({"model": doc_model} if doc_model else {}),
        **({"effort": doc_effort} if doc_effort else {}),
    }
    new_task = Task(
        project_id=project.id,
        title=f"Architecture review: {project.name}",
        description="Update project design document",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "STRATEGIC_CYCLE", "phase": "design_doc_update"},
    )
    session.add(history)
    await session.commit()
    logger.info(f"[STRATEGIC] Spawned design_doc_update task {new_task.id} for project {project.id}")


async def _spawn_strategic_review(project: Project, session: AsyncSession):
    """Spawn Phase 2 of the strategic cycle: vision + direction review.

    Called by _handle_design_doc_completion after the design doc is updated.
    Uses type 'strategic_review' so it doesn't block tactical research.
    """
    # Build direction stats with rich data (same as previous strategic payload)
    all_active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
    direction_stats = []
    for d in all_active_directions:
        history = d.get("research_history", [])
        agg = d.get("history_aggregate", {})
        total_cycles = len(history) + (agg.get("total_cycles") or 0)
        total_merged = sum(h.get("prs_merged", 0) for h in history) + (agg.get("total_prs_merged") or 0)
        total_failed = sum(h.get("tasks_failed", 0) for h in history) + (agg.get("total_tasks_failed") or 0)
        recent_history = history[-5:]
        recent_findings = []
        recent_failures = []
        for h in recent_history:
            for desc in (h.get("key_finding_descriptions") or []):
                if desc and desc not in recent_findings:
                    recent_findings.append(desc)
            for fd in (h.get("failed_task_details") or []):
                if fd:
                    recent_failures.append(fd)
        stat_entry: dict = {
            "id": d.get("id"), "text": d.get("text"), "priority": d.get("priority"),
            "status": d.get("status"), "total_cycles": total_cycles,
            "prs_merged": total_merged, "tasks_failed": total_failed,
            "possibly_saturated": d.get("possibly_saturated", False),
        }
        ctx = d.get("research_context")
        if ctx:
            stat_entry["research_context"] = ctx[:3000]
        if recent_findings:
            stat_entry["recent_findings"] = recent_findings[:10]
        if recent_failures:
            stat_entry["recent_failures"] = recent_failures[:5]
        if d.get("system_id"):
            stat_entry["system_id"] = d["system_id"]
        direction_stats.append(stat_entry)

    # Fetch open messages
    open_msgs_result = await session.execute(
        select(ProjectMessage).where(
            ProjectMessage.project_id == project.id,
            ProjectMessage.status == "open",
        ).order_by(ProjectMessage.created_at.asc()).limit(20)
    )
    open_msgs = [
        {"id": str(m.id), "type": m.message_type, "content": m.content,
         "context": m.context, "from": m.from_type,
         "created_at": m.created_at.isoformat() if m.created_at else None}
        for m in open_msgs_result.scalars().all()
    ]

    review_model = _runtime_config.get("task_type_models", {}).get("strategic_review") or \
                   _runtime_config.get("task_type_models", {}).get("research") or None
    review_effort = _runtime_config.get("task_type_efforts", {}).get("strategic_review") or \
                    _runtime_config.get("task_type_efforts", {}).get("research") or None

    payload: dict = {
        "type": "strategic_review",
        "research_mode": "strategic",
        "design_doc": project.design_doc or {},
        "vision": project.vision or {},
        "directions_summary": direction_stats,
        "outcome_evaluation": _build_outcome_evaluation(project),
        "project_name": project.name,
        "open_messages": open_msgs,
        "base_branch": "main",
        **({"model": review_model} if review_model else {}),
        **({"effort": review_effort} if review_effort else {}),
    }
    new_task = Task(
        project_id=project.id,
        title=f"Strategic review: {project.name}",
        description="Vision and direction review",
        payloads=[payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    history = TaskHistory(
        task_id=new_task.id, action="SPAWNED",
        details={"spawned_by": "STRATEGIC_CYCLE", "phase": "strategic_review"},
    )
    session.add(history)
    await session.commit()
    logger.info(f"[STRATEGIC] Spawned strategic_review task {new_task.id} for project {project.id}")


async def _spawn_research_task(project: Project, session: AsyncSession):
    """Spawn a research task for a project if conditions are met."""
    # Guard: autonomous must be enabled
    if not project.autonomous_enabled:
        return
    # Guard: don't spawn if user has no available accounts (avoids immediate failure)
    user_pool = _max_plan_pools.get(project.user_id, [])
    research_model = _runtime_config.get("task_type_models", {}).get("research")
    has_available = any(_account_available_for_model(a, research_model) for a in user_pool) or bool(os.getenv("ANTHROPIC_API_KEY"))
    if not has_available:
        logger.info(f"[AUTONOMOUS] Skipping research for {project.name} — no available accounts")
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
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
                return  # Back to cooldown
            elif not test_task:
                logger.warning(f"[CIRCUIT BREAKER] HALF_OPEN test task {test_id} no longer exists (deleted/reaped) → OPEN")
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)
                breaker["half_open_task_id"] = None
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task deleted/reaped — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
                return  # Back to cooldown
    # Guard: must have active directions that aren't in cooldown or mid-cycle
    cooldown_minutes = _runtime_config.get("autonomous_direction_cooldown_minutes", 240)
    now = _utcnow()

    # Build set of direction IDs that still have an unrolled research cycle
    # (EVALUATING parent = children still running/merging, not rolled up yet)
    busy_direction_ids: set = set()
    evaluating_research = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status == TaskStatus.EVALUATING,
        )
    )).scalars().all()
    for t in evaluating_research:
        payload = t.first_payload
        if payload.get("type") == "research":
            # New-format tasks include all active directions but only one is targeted
            current_did = payload.get("current_direction_id")
            if current_did:
                busy_direction_ids.add(current_did)
            else:
                # Legacy format: single direction in list
                for direction in payload.get("directions", []):
                    did = direction.get("id")
                    if did:
                        busy_direction_ids.add(did)

    active_directions = []
    auto_paused_any = False
    directions_modified = False
    for d in (project.directions or []):
        if d.get("status") != "active":
            continue
        did = d.get("id")
        # Skip directions with an unrolled research cycle still in progress
        if did and did in busy_direction_ids:
            logger.debug(
                f"[RESEARCH] Skipping direction {did}: previous research cycle not yet rolled up"
            )
            continue
        last_researched = d.get("last_researched_at")
        if last_researched:
            minutes_ago = (now - datetime.fromisoformat(last_researched)).total_seconds() / 60
            effective_cooldown = cooldown_minutes
            if d.get("possibly_saturated"):
                effective_cooldown = cooldown_minutes * 2.0
                logger.debug(
                    f"[RESEARCH] Direction {did} saturated — applying 2x cooldown ({cooldown_minutes*2}m)"
                )
            if minutes_ago < effective_cooldown:
                logger.debug(
                    f"[RESEARCH] Skipping direction {did}: last researched "
                    f"{minutes_ago:.1f}m ago (cooldown: {effective_cooldown}m)"
                )
                continue
        # Auto-pause check — must come after cooldown to only evaluate directions eligible for research
        pause_reason = _check_direction_health(d)
        if pause_reason:
            d["status"] = "paused"
            d["paused_reason"] = pause_reason
            d["paused_at"] = _utcnow().isoformat()
            d["auto_paused"] = True
            logger.info(f"[AUTONOMOUS] Auto-paused direction {did}: {pause_reason}")
            auto_paused_any = True
            continue
        # Positive completion detection — mark for user review but don't pause
        completion_suggestion = _check_direction_completion(d)
        if completion_suggestion:
            if not d.get("possibly_complete"):
                d["possibly_complete"] = True
                d["completion_suggestion"] = completion_suggestion
                logger.info(f"[AUTONOMOUS] Direction {did} possibly complete: {completion_suggestion}")
                directions_modified = True
        else:
            if d.get("possibly_complete"):
                d.pop("possibly_complete", None)
                d.pop("completion_suggestion", None)
                directions_modified = True
        active_directions.append(d)
    if auto_paused_any or directions_modified:
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
    # Allow strategic cycles to proceed even without active directions (they create directions).
    # Stop forcing after 3 consecutive strategic failures to prevent infinite loops.
    vision_data = project.vision or {}
    strategic_failures = vision_data.get("_strategic_failures", 0)
    force_strategic = not vision_data.get("purpose") and strategic_failures < 3
    if not active_directions and not force_strategic:
        return
    # Pick ONE direction per research task to avoid putting all on cooldown at once.
    # active_directions preserves UI drag-order (index 0 = highest priority).
    # Among eligible directions, prefer those never researched or oldest researched.
    chosen_direction: Optional[dict] = None
    # Build vision priority lookup: directions tagged with a system_id that appears
    # earlier in vision.priorities get a lower (better) rank.  This makes vision
    # operational — the strategic agent's priority ordering actually influences which
    # direction gets researched next.
    vision_priorities = (vision_data.get("priorities") or []) if vision_data else []
    vision_priority_index: dict = {}
    for idx, sys_id in enumerate(vision_priorities):
        if isinstance(sys_id, str):
            vision_priority_index[sys_id] = idx

    def _vision_rank(d: dict) -> int:
        """0 = top vision priority, len = unlisted. Directions without system_id get neutral rank."""
        sid = d.get("system_id")
        if not sid:
            return len(vision_priorities)  # neutral — don't penalize untagged
        return vision_priority_index.get(sid, len(vision_priorities) + 1)  # unlisted slightly below neutral

    if active_directions:
        import random
        priority_rank = {"high": 0, "medium": 1, "low": 2}
        never_researched = [d for d in active_directions if not d.get("last_researched_at")]
        if never_researched:
            # Pick from the highest priority tier among never-researched,
            # breaking ties by vision alignment
            best_pri = min(priority_rank.get(d.get("priority", "medium"), 1) for d in never_researched)
            top_tier = [d for d in never_researched if priority_rank.get(d.get("priority", "medium"), 1) == best_pri]
            if vision_priorities and len(top_tier) > 1:
                top_tier.sort(key=_vision_rank)
                best_vr = _vision_rank(top_tier[0])
                top_tier = [d for d in top_tier if _vision_rank(d) == best_vr]
            chosen_direction = random.choice(top_tier)
        else:
            # All have been researched — sort by priority, vision alignment,
            # saturation, merge rate, then oldest researched
            for d in active_directions:
                did = d.get("id", "?")
                merge_rate = _compute_merge_rate(d)
                logger.debug(f"[RESEARCH] Direction {did} merge_rate={merge_rate:.2f} vision_rank={_vision_rank(d)}")
            active_directions.sort(key=lambda d: (
                priority_rank.get(d.get("priority", "medium"), 1),
                _vision_rank(d),  # vision-aligned directions get researched sooner
                1 if d.get("possibly_saturated") else 0,
                -_compute_merge_rate(d),  # higher merge rate preferred
                d.get("last_researched_at") or ""
            ))
            chosen_direction = active_directions[0]
    elif not force_strategic:
        return  # No directions and not a strategic cycle — nothing to do

    # Strategic cycle check — runs BEFORE the research blocking guard so it's
    # never blocked by an active tactical research task.  The design_doc_update
    # and strategic_review use their own task types and run in parallel.
    all_active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
    total_research_count = sum(
        len(d.get("research_history", [])) for d in all_active_directions
    )
    strategic_frequency = _runtime_config.get("strategic_research_frequency", 6)
    is_strategic = (
        (strategic_frequency > 0 and total_research_count > 0 and total_research_count % strategic_frequency == 0)
        or force_strategic
    )
    if is_strategic:
        await _spawn_design_doc_update(project, session)
        logger.info(f"[AUTONOMOUS] Spawning design doc update (strategic cycle #{total_research_count}, vision rev {(project.vision or {}).get('revision', 0)})")

    # Guard: no research task actively running (queued/provisioning/in_progress).
    # EVALUATING parents are just waiting on children — they don't block new research.
    active_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    for t in active_tasks:
        if (t.first_payload).get("type") == "research":
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
    recent_research_count = sum(1 for t in recent_research if (t.first_payload).get("type") == "research")
    if recent_research_count >= _runtime_config["max_autonomous_tasks_per_hour"]:
        return

    # If no active directions exist (e.g., all paused), only strategic runs (already spawned above).
    # Don't attempt tactical research — chosen_direction would be None.
    if not active_directions:
        return

    # Autonomous research creates independent tasks (no parent_task_id), so they
    # must always branch from main.  Staging branches (research/*, plan/*) are
    # ephemeral — they get deleted when their parent's rollup merges.  An
    # independent task using someone else's staging branch will break when that
    # rollup cleans up.  Actual subresearch (spawned by _handle_research_completion
    # with parent_task_id set) correctly inherits its parent's staging branch.
    base_branch = "main"

    # Use per-type defaults
    research_model = _runtime_config.get("task_type_models", {}).get("research") or None
    research_effort = _runtime_config.get("task_type_efforts", {}).get("research") or None
    research_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("research")
    # Every Nth cycle is "undirected" — the agent explores the whole codebase
    # without a specific direction lens, finding cross-cutting improvements.
    undirected_frequency = _runtime_config.get("undirected_research_frequency", 4)
    is_undirected = undirected_frequency > 0 and total_research_count > 0 and total_research_count % undirected_frequency == 0
    # Don't run undirected on strategic cycles
    if is_strategic:
        is_undirected = False

    # Build tactical research payload
    if True:
        research_payload: dict = {
            "type": "research",
            "directions": all_active_directions,
            "current_direction_id": None if is_undirected else chosen_direction.get("id"),
            "design_doc": project.design_doc or {},
            "base_branch": base_branch,
            **({"model": research_model} if research_model else {}),
            **({"effort": research_effort} if research_effort else {}),
            **({"prompt_suffix": research_suffix} if research_suffix else {}),
        }
        # Pre-extract design doc context for the current direction's system_id
        # so the research agent sees relevant architectural context upfront
        if not is_undirected and chosen_direction:
            dir_system_id = chosen_direction.get("system_id")
            if dir_system_id and project.design_doc:
                doc = project.design_doc
                doc_systems = doc.get("systems", [])
                doc_capabilities = doc.get("capabilities", {})
                relevant: dict = {"system_id": dir_system_id}
                # Find matching system entry
                for sys in (doc_systems if isinstance(doc_systems, list) else []):
                    if isinstance(sys, dict) and sys.get("id") == dir_system_id:
                        relevant["system"] = sys
                        break
                # Find capabilities that might relate (by keyword match on system name/id)
                if isinstance(doc_capabilities, dict):
                    sys_name = (relevant.get("system", {}).get("name") or dir_system_id).lower()
                    related_caps = {}
                    for cap_name, cap_val in doc_capabilities.items():
                        if isinstance(cap_val, dict) and cap_val.get("status") in ("missing", "partial"):
                            related_caps[cap_name] = cap_val
                    if related_caps:
                        relevant["missing_capabilities"] = related_caps
                research_payload["direction_design_context"] = relevant
        if not is_undirected and chosen_direction.get("possibly_complete"):
            research_payload["direction_possibly_complete"] = True
            research_payload["completion_suggestion"] = chosen_direction.get("completion_suggestion", "")
        if is_undirected:
            anomalies = await _compute_research_anomalies(project.id, session)
            if anomalies:
                research_payload["anomaly_signals"] = anomalies
        if is_undirected:
            direction_text = "Cross-cutting codebase analysis"
            logger.info(f"[AUTONOMOUS] Spawning UNDIRECTED research cycle (cycle #{total_research_count})")
        else:
            direction_text = (chosen_direction.get("text") or "general improvement")[:80]
    new_task = Task(
        project_id=project.id,
        title=f"Research: {direction_text}",
        description=f"Autonomous research for: {direction_text}",
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
                await _maybe_create_circuit_breaker_alert(
                    project.id, session,
                    f"HALF_OPEN test task stuck for 30m+ — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )
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
        if (t.first_payload).get("type") == task_type:
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
        if (t.first_payload).get("type") == task_type:
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
    base_branch = (recent_task.first_payload).get("base_branch", "main") if recent_task else "main"

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
            projects = (await session.execute(
                select(Project).where(
                    Project.github_owner == owner,
                    Project.github_repo == repo,
                    Project.autonomous_enabled.is_(True),
                )
            )).scalars().all()
            if not projects:
                return

            # Spawn if VM slots are available (PROVISIONING + IN_PROGRESS, not blocked QUEUED)
            from sqlalchemy import func
            vm_consuming = (await session.execute(
                select(func.count()).select_from(Task).where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                )
            )).scalar() or 0

            if vm_consuming < _runtime_config["max_concurrent_vms"]:
                for project in projects:
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

            # Expire stale proposals before processing
            await _expire_stale_proposals(session)

            projects = (await session.execute(
                select(Project).where(Project.autonomous_enabled.is_(True))
            )).scalars().all()

            for project in projects:
                # Log pending proposals awaiting review
                pending_proposals = [d for d in (project.directions or []) if d.get("status") == "proposed"]
                if pending_proposals:
                    logger.info(f"[DIRECTIONS] Project {project.name} has {len(pending_proposals)} pending direction proposals awaiting review")

                active_directions = [d for d in (project.directions or []) if d.get("status") == "active"]
                # Allow projects with no directions through if vision needs bootstrapping
                vision_needs_bootstrap = not (project.vision or {}).get("purpose")
                if not active_directions and not vision_needs_bootstrap:
                    continue

                # Spawn if VM slots are available — count only tasks consuming VMs,
                # not QUEUED tasks blocked on dependencies
                vm_consuming = (await session.execute(
                    select(func.count()).select_from(Task).where(
                        Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                    )
                )).scalar() or 0

                if vm_consuming < _runtime_config["max_concurrent_vms"]:
                    await _spawn_research_task(project, session)

                    for task_type in ("security_audit", "qa", "deps"):
                        await _spawn_periodic_task(project, task_type, session)

                # Check persistent anomalies for direction proposals (regardless of VM slots)
                await _check_anomaly_proposals(project, session)
                await session.commit()
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
        self._lock = asyncio.Lock()

    async def check_and_add_ip(self, websocket: WebSocket) -> bool:
        client_ip = _get_websocket_client_ip(websocket)
        if client_ip != "unknown":
            if self.ip_connections.get(client_ip, 0) >= self.MAX_CONNECTIONS_PER_IP:
                await websocket.close(code=1008, reason="Too many connections from this IP")
                return False
            self.ip_connections[client_ip] = self.ip_connections.get(client_ip, 0) + 1
        return True

    def _remove_ip(self, websocket: WebSocket):
        client_ip = _get_websocket_client_ip(websocket)
        if client_ip != "unknown" and client_ip in self.ip_connections:
            self.ip_connections[client_ip] -= 1
            if self.ip_connections[client_ip] <= 0:
                del self.ip_connections[client_ip]

    # Keep old name for any external callers
    def remove_ip(self, websocket: WebSocket):
        self._remove_ip(websocket)

    async def connect(self, websocket: WebSocket, project_id: str) -> bool:
        async with self._lock:
            if not await self.check_and_add_ip(websocket):
                return False

            if project_id not in self.active_connections:
                self.active_connections[project_id] = []
            self.active_connections[project_id].append(websocket)
            return True

    async def disconnect(self, websocket: WebSocket, project_id: str):
        async with self._lock:
            if project_id in self.active_connections:
                if websocket in self.active_connections[project_id]:
                    self.active_connections[project_id].remove(websocket)
                if not self.active_connections[project_id]:
                    del self.active_connections[project_id]

            self._remove_ip(websocket)

    async def broadcast(self, project_id: str, event_type: str, data: dict):
        async with self._lock:
            if project_id not in self.active_connections:
                return
            connections = list(self.active_connections[project_id])  # noqa: PERF101 — snapshot under lock
        message = {"event": event_type, "data": data}
        for connection in connections:
            try:
                await asyncio.wait_for(connection.send_json(message), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"[WS] Send timeout for client (project={project_id}, event={event_type})")
                await self.disconnect(connection, project_id)
            except Exception as e:
                logger.warning(f"[WS] Failed to send message to client (project={project_id}, event={event_type}): {e}")
                await self.disconnect(connection, project_id)

    async def broadcast_to_all(self, event_type: str, data: dict):
        """Broadcasts a message to all connected WebSocket clients across all projects."""
        for project_id in list(self.active_connections.keys()):  # noqa: PERF101 — broadcast() can remove entries via disconnect()
            await self.broadcast(project_id, event_type, data)

manager = ConnectionManager()

async def broadcast_to_project(project_id: str, event_type: str, data: dict):
    """Broadcasts a message to all WebSocket clients listening to a specific project.

    Swallows exceptions to prevent crashing HTTP handlers after DB commits.
    """
    try:
        await manager.broadcast(project_id, event_type, data)
    except Exception as e:
        logger.warning(f"[WS] Broadcast failed (project={project_id}, event={event_type}): {e}")


async def _maybe_create_failure_alert(
    task: "Task",
    session: AsyncSession,
    error_snippet: str | None = None,
) -> None:
    """Create an ERROR alert for a task failure unless it's a smoketest or retry task.
    Deduplicates: skips if an undismissed alert already exists for this task."""
    task_type = (task.first_payload).get("type", "")
    if task_type == "smoketest":
        return
    if (task.context or {}).get("is_retry") is True:
        return
    existing = await session.execute(
        select(Alert).where(Alert.task_id == task.id, Alert.dismissed_at.is_(None))
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.ERROR,
        title=f"Task failed: {task.title or str(task.id)[:8]}",
        message=f"Task type: {task_type}. {error_snippet or 'See task logs for details.'}",
        task_id=task.id,
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
    )


async def _maybe_create_circuit_breaker_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    reason: str,
) -> None:
    """Create a WARNING alert when the circuit breaker opens for a project.
    Deduplicates: skips if an undismissed circuit-breaker alert already exists."""
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.task_id.is_(None),
            Alert.severity == AlertSeverity.WARNING,
            Alert.dismissed_at.is_(None),
            Alert.title == "Circuit breaker opened: autonomous tasks paused",
        )
    )
    if existing.scalars().first() is not None:
        return
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title="Circuit breaker opened: autonomous tasks paused",
        message=reason,
    )
    session.add(alert)
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )
    await _send_webhook_notification("CIRCUIT_BREAKER_OPEN", alert.title, alert.message)


async def _create_strategic_alert(
    project_id: "uuid.UUID",
    session: AsyncSession,
    message: str,
) -> None:
    """Create a WARNING alert for strategic cycle failures. Deduplicates by title."""
    title = "Strategic cycle issue"
    existing = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.severity == AlertSeverity.WARNING,
            Alert.dismissed_at.is_(None),
            Alert.title == title,
        )
    )
    if existing.scalars().first():
        return  # Don't spam duplicate alerts
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title=title,
        message=message[:1000],
    )
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(project_id), "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": title},
    )


async def broadcast_deploy_state():
    """Assemble and broadcast current deploy state to all connected WebSocket clients."""
    try:
        from app.admin import build_deploy_state_payload
        async with AsyncSessionLocal() as session:
            payload = await build_deploy_state_payload(session)
        await manager.broadcast_to_all("deploy_state", payload)
    except Exception as e:
        logger.warning(f"[WS] Failed to broadcast deploy state: {e}")



app = FastAPI(
    title="Blueprint Orchestrator",
    description="Secure Orchestration & State Management",
    lifespan=lifespan
)

# Telemetry must be set up before the app starts (cannot add middleware during lifespan)
setup_telemetry(app, engine)

app.include_router(admin_router)
app.include_router(agent_router)
app.include_router(github_router)
app.include_router(projects_router)
app.include_router(directions_router)
app.include_router(deploy_router)

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
        select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False))
    )).scalar() or 0
    active = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
    )).scalar() or 0
    ago = None
    if _orch._orchestrator_last_cycle:
        ago = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()
    # Include deployment state
    deploy_state = None
    try:
        state = await session.get(DeploymentState, 1)
        if state:
            monitoring_since = _shared_state.deploy_monitoring.get("window_start")
            recent_results = list(_shared_state.deploy_monitoring.get("task_results", []))[-10:]
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
    all_accts = _all_accounts()
    async with _max_plan_pool_lock:
        pool_snapshot = {
            "size": len(all_accts),
            "available": sum(1 for a in all_accts if a.get("enabled", True)),
            "disabled": sum(1 for a in all_accts if not a.get("enabled", True)),
            "accounts": [
                {
                    "name": a["name"],
                    "enabled": a.get("enabled", True),
                    "active": a["active_tasks"],
                    "disabled_at": a["disabled_at"].isoformat() if a.get("disabled_at") else None,
                    "reenable_at": (a["disabled_at"] + timedelta(minutes=30)).isoformat() if a.get("disabled_at") else None,
                }
                for a in all_accts
            ],
        }

    hung = False
    if _orch._orchestrator_cycle_started and _orch._orchestrator_last_cycle:
        if _orch._orchestrator_cycle_started > _orch._orchestrator_last_cycle:
            hung_seconds = (_utcnow() - _orch._orchestrator_cycle_started).total_seconds()
            hung = hung_seconds > 300  # 5 minutes without completing a cycle

    return {
        "orchestrator_alive": _orch._orchestrator_last_cycle is not None,
        "orchestrator_hung": hung,
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
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()
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
            select(func.count()).select_from(Task).where(Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False))
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
    total_accounts = len(_account_by_id)
    available_accounts = sum(1 for a in _account_by_id.values() if a.get("enabled", True))
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


@app.get("/api/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Return the authenticated user's profile and role."""
    return {
        "user_id": current_user["user_id"],
        "email": current_user["email"],
        "role": current_user["role"],
    }


@app.get("/api/version")
async def get_version():
    """Public endpoint returning current backend version for frontend compatibility checks."""
    return {"sha": _current_sha, "revision": _current_revision}


@app.get("/health")
async def health_check():
    """Unauthenticated health endpoint for external monitoring (Cloud Function, load balancer)."""
    from app.shared_state import external_health

    orchestrator_alive = _orch._orchestrator_last_cycle is not None
    cycle_age = None
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()

    # Orchestrator is unhealthy if it hasn't cycled in 60+ seconds
    orchestrator_healthy = orchestrator_alive and (cycle_age is not None and cycle_age < 60)

    # Detect hung orchestrator (cycle started but not completed in 5+ minutes)
    orchestrator_hung = False
    if _orch._orchestrator_cycle_started and _orch._orchestrator_last_cycle:
        if _orch._orchestrator_cycle_started > _orch._orchestrator_last_cycle:
            hung_seconds = (_utcnow() - _orch._orchestrator_cycle_started).total_seconds()
            orchestrator_hung = hung_seconds > 300

    # Only DB is critical for 503 — GitHub/Claude/GCP outages shouldn't remove us from LB
    critical_healthy = external_health.get("db", True)
    degraded_deps = [
        k for k in ("github_api", "claude_api", "gcp")
        if not external_health.get(k, True)
    ]
    if orchestrator_hung:
        degraded_deps.append("orchestrator_hung")

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
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()

    active_vms = sum(a.get("active_tasks", 0) for a in _account_by_id.values())
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
    # Pydantic v2 error dicts may contain non-serializable objects in ctx
    errors = []
    for e in exc.errors():
        clean = dict(e)
        ctx = clean.pop("ctx", None)
        if ctx:
            clean["ctx"] = {k: str(v) for k, v in ctx.items()}
        errors.append(clean)
    return JSONResponse(
        status_code=422,
        content={"error": {"code": 422, "message": "Validation Error", "details": errors}},
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


from app.schemas import (
    DeployPayload,
    AgentStatusPayload,
)

@app.get("/")
def read_root():
    return {"status": "online", "service": "Blueprint Orchestrator"}

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


async def _requeue_parent_task(parent: Task, session: AsyncSession):
    """Re-evaluate a parent task by retrying its failed children.
    Cancels FAILED children and clones them as new QUEUED tasks.
    Remaps depends_on from old (cancelled) IDs to new (cloned) IDs.
    Leaves COMPLETED, CANCELLED, and active (QUEUED/IN_PROGRESS) children untouched.
    Sets parent back to EVALUATING — no VM needed."""
    # Don't resurrect a cancelled parent
    if parent.status == TaskStatus.CANCELLED:
        logger.info(f"[REQUEUE] Skipping requeue — parent {parent.id} is CANCELLED")
        return 0

    # Cap re-evaluations to prevent infinite loops when children consistently fail
    MAX_PARENT_REQUEUES = 3
    parent_ctx = parent.context or {}
    requeue_count = parent_ctx.get("_requeue_count", 0)
    if requeue_count >= MAX_PARENT_REQUEUES:
        logger.warning(f"[REQUEUE] Parent {parent.id} hit re-evaluation limit ({requeue_count} attempts) — failing permanently")
        parent.status = TaskStatus.FAILED
        parent.latest_status_message = f"Re-evaluation limit reached ({requeue_count} attempts) — children consistently failing"
        session.add(parent)
        await session.commit()
        # Create failure alert
        await _maybe_create_failure_alert(parent, session, parent.latest_status_message)
        return 0

    children = (await session.execute(
        select(Task).where(Task.parent_task_id == parent.id)
    )).scalars().all()

    # Refresh base_branch from parent's staging branch — the original child's
    # base_branch may reference a deleted branch (e.g., plan/* after rollup merge)
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

    parent.context = {**(parent.context or {}), "_requeue_count": requeue_count + 1}
    parent.status = TaskStatus.EVALUATING
    parent.latest_status_message = f"Requeued — retrying {retried_count} failed subtask(s)"
    parent.attempt_count += 1
    session.add(parent)
    await session.commit()

    for child in children:
        await session.refresh(child)
    await broadcast_to_project(str(parent.project_id), "task_updated",
                               {"task_id": str(parent.id), "status": TaskStatus.EVALUATING})
    logger.info(f"[REQUEUE] Parent {parent.id} re-evaluating — {retried_count} failed children retried")
    return retried_count


MAX_REVIEW_FIX_ITERATIONS = 8


def _max_review_iterations(task: Task) -> int:
    """Scope-aware review-fix iteration limit."""
    ctx = task.context or {}
    scope = ctx.get('scope', 'medium')
    fix_reason = ctx.get('review_loop', {}).get('fix_reason', 'code_review')

    # CI-only fixes should converge fast (lint/type/test errors)
    if fix_reason == 'ci_failure':
        limit = 3
    # Small scope tasks: less code, fewer things to fix
    elif scope == 'small':
        limit = 5
    # Medium scope: moderate iteration budget
    elif scope == 'medium':
        limit = 6
    # Large scope or unknown: full budget
    else:
        limit = 8
    return min(limit, MAX_REVIEW_FIX_ITERATIONS)


async def _spawn_pipeline_task(
    source_task: Task, task_type: str, session: AsyncSession,
    pr_number: int, branch: str, base_branch: str,
    original_prompt: str = "", fix_prompt: str = "", original_task_id: str = "",
    current_feedback: str = "", fix_reason: str = "", current_issues: list = None
) -> Optional[Task]:
    """Spawn a review or fix task as part of the automated review pipeline.
    Returns None if the review-fix cycle limit has been reached.
    Returns the existing active task if a review/fix is already active for this PR."""
    # Guard: don't spawn if PR is already merged
    orig_id = original_task_id or (source_task.context or {}).get("review_loop", {}).get("original_task_id", "") or (source_task.first_payload).get("original_task_id", "")
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

    # Guard: don't spawn if there's already an ACTIVE same-type task for this PR.
    # This prevents duplicate concurrent spawns (race window between commit and pipeline spawn)
    # but allows the intentional review→fix→review cycle to continue.
    active_pipeline = (await session.execute(
        select(Task).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS, TaskStatus.EVALUATING]),
        )
    )).scalars().all()
    for t in active_pipeline:
        if t.id == source_task.id:
            continue
        t_payload = t.first_payload
        if t_payload.get("pr_number") == pr_number and t_payload.get("type") == task_type:
            logger.warning(f"[PIPELINE] Skipping {task_type} spawn for PR #{pr_number} — already active: {t.id}")
            source_task.latest_status_message = f"{task_type.title()} already active for PR #{pr_number} → task {str(t.id)[:8]}"
            session.add(source_task)
            return t

    # Per-PR iteration cap: count completed review/fix tasks for this PR across all chains
    # Filter to tasks matching this PR number (check payloads JSONB)
    pr_pipeline_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == source_task.project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
        )
    )).scalars().all()
    pr_iterations = sum(
        1 for t in pr_pipeline_tasks
        if t.first_payload.get("pr_number") == pr_number
        and t.first_payload.get("type") in ("review", "fix")
    )
    global_cap = _max_review_iterations(source_task)
    if pr_iterations >= global_cap:
        logger.warning(f"[PIPELINE] PR #{pr_number} hit global review-fix cap ({pr_iterations} iterations across all chains)")
        source_task.latest_status_message = f"Review-fix cap reached for PR #{pr_number} ({pr_iterations} total iterations)"
        session.add(source_task)
        return None

    # Track review-fix iteration count to prevent infinite loops (per-chain)
    source_loop = (source_task.context or {}).get("review_loop", {})
    source_payload = source_task.first_payload
    iteration = source_loop.get("iteration", 0) + 1
    per_chain_cap = _max_review_iterations(source_task)

    if iteration > per_chain_cap:
        # Cap reached — require manual intervention
        orig_id = original_task_id or source_loop.get("original_task_id", "") or source_payload.get("original_task_id", "")
        if orig_id:
            try:
                orig = await session.get(Task, uuid.UUID(orig_id))
                if orig:
                    orig.requires_approval = True
                    orig.latest_status_message = f"Review-fix cycle reached limit ({per_chain_cap} iterations) — manual review required"
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
                        "original_task_type": source_task.first_payload.get("type", ""),
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
                t_payload = t.first_payload
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
                        "original_prompt": original_prompt,
                        "original_task_id": resolved_original_id,
                        **({"model": type_model} if type_model else {}),
                        **({"effort": type_effort} if type_effort else {})}

    # Build review_history: carry forward from source task, appending current feedback if spawning a fix
    prev_review_history = source_loop.get("review_history", [])
    new_review_entry = {"iteration": iteration - 1, "feedback": current_feedback}
    if current_issues:
        new_review_entry["issues"] = current_issues
    new_review_history = (
        [*prev_review_history, new_review_entry]
        if task_type == "fix" and current_feedback
        else prev_review_history
    )

    # Stuck-cycle detection: if the same CRITICAL/MAJOR issues repeat across consecutive
    # iterations, escalate for manual review instead of consuming the full iteration budget.
    if task_type == "fix" and iteration >= 2 and len(new_review_history) >= 2:
        prev_entry = new_review_history[-2]
        curr_entry = new_review_history[-1]
        prev_issues_list = prev_entry.get("issues") or []
        curr_issues_list = curr_entry.get("issues") or []
        prev_critical_major = {
            i.get("description", "").lower().strip()
            for i in prev_issues_list
            if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
            and i.get("description", "").strip()
        }
        curr_critical_major = [
            i.get("description", "").lower().strip()
            for i in curr_issues_list
            if i.get("severity", "").upper() in ("CRITICAL", "MAJOR")
            and i.get("description", "").strip()
        ]
        if curr_critical_major and prev_critical_major:
            matched_count = sum(
                1 for desc in curr_critical_major
                if any(desc in prev_desc or prev_desc in desc for prev_desc in prev_critical_major)
            )
            total_count = len(curr_critical_major)
            if matched_count / total_count >= 0.5:
                logger.warning(
                    f"[PIPELINE] Stuck cycle detected for PR #{pr_number}: "
                    f"{matched_count}/{total_count} issues repeated"
                )
                # resolved_original_id is always non-empty (falls back to str(source_task.id))
                try:
                    stuck_orig = await session.get(Task, uuid.UUID(resolved_original_id))
                    if stuck_orig:
                        stuck_orig.requires_approval = True
                        stuck_orig.latest_status_message = (
                            f"Review-fix cycle appears stuck — same issues repeated across "
                            f"{iteration} iterations. Manual review required."
                        )
                        session.add(stuck_orig)
                        await session.commit()
                except Exception as e:
                    logger.warning(
                        f"[PIPELINE] Failed to mark stuck cycle on {resolved_original_id}: {e}",
                        exc_info=True,
                    )
                return None

    source_ctx = source_task.context or {}
    propagated_scope = source_ctx.get('scope', source_loop.get('scope', 'medium'))
    new_task = Task(
        project_id=source_task.project_id,
        title=title,
        description=fix_prompt or f"Automated {task_type} for PR #{pr_number}",
        parent_task_id=parent_id,
        payloads=[payload_data],
        context={"scope": propagated_scope,
                 "review_loop": {"pr_number": pr_number, "branch": branch,
                                  "base_branch": base_branch,
                                  "original_task_id": resolved_original_id,
                                  "iteration": iteration,
                                  **({"review_history": new_review_history} if new_review_history else {}),
                                  **({"fix_reason": fix_reason} if fix_reason else {})}},
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


def _compute_saturation(history: list) -> bool:
    """Return True if last 3 cycles all produced 0 or 1 tasks."""
    if len(history) < 3:
        return False
    recent = history[-3:]
    return all(entry.get("tasks_created", 0) <= 1 for entry in recent)


def _parse_research_context(text: str) -> dict:
    """Parse a research_context string into its 3 sections and optional eviction summary."""
    result: dict = {"established": [], "failed": [], "open_questions": [], "eviction_summary": None}
    if not text:
        return result
    section_map = {
        "## Established Findings": "established",
        "## Failed Approaches": "failed",
        "## Open Questions": "open_questions",
    }
    current_key: Optional[str] = None
    in_eviction = False
    eviction_lines: list = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "## Eviction Summary":
            in_eviction = True
            current_key = None
        elif stripped in section_map:
            in_eviction = False
            current_key = section_map[stripped]
        elif in_eviction and stripped:
            eviction_lines.append(stripped)
        elif current_key and stripped.startswith("- "):
            result[current_key].append(stripped[2:])
    if eviction_lines:
        result["eviction_summary"] = "\n".join(eviction_lines)
    return result


def _parse_eviction_counts(summary_text: Optional[str]) -> dict:
    """Parse counts and failed titles from an eviction summary body."""
    result: dict = {"established": 0, "failed": 0, "open_questions": 0, "failed_titles": []}
    if not summary_text:
        return result
    for line in summary_text.splitlines():
        line = line.strip()
        # Check "Failed:" prefix before "evicted" keyword — titles may themselves contain "evicted"
        if line.startswith("Failed:"):
            titles_str = line[len("Failed:"):].strip()
            result["failed_titles"] = [t.strip() for t in titles_str.split(";") if t.strip()]
        elif "evicted" in line:
            m = re.search(r'(\d+) established', line)
            if m:
                result["established"] = int(m.group(1))
            m = re.search(r'(\d+) failed', line)
            if m:
                result["failed"] = int(m.group(1))
            m = re.search(r'(\d+) open question', line)
            if m:
                result["open_questions"] = int(m.group(1))
    return result


def _format_eviction_body(established: int, failed: int, open_questions: int, failed_titles: list) -> str:
    """Format the eviction summary body content (without the ## header line)."""
    count_parts = []
    if established > 0:
        count_parts.append(f"{established} established")
    if failed > 0:
        count_parts.append(f"{failed} failed")
    if open_questions > 0:
        plural = "s" if open_questions != 1 else ""
        count_parts.append(f"{open_questions} open question{plural}")
    lines = []
    if count_parts:
        lines.append(", ".join(count_parts) + " evicted")
    if failed_titles:
        lines.append("Failed: " + "; ".join(failed_titles))
    return "\n".join(lines)


def _build_research_context(
    prior_context: Optional[str],
    finding_titles: list,
    key_finding_descriptions: list,
    cycle_insights: list,
    history_entry: dict,
) -> str:
    """Build a compounding research_context document deterministically (no LLM)."""
    parsed = _parse_research_context(prior_context or "")

    # Parse prior eviction summary for accumulation
    prior_eviction = _parse_eviction_counts(parsed["eviction_summary"])

    # Append new findings to Established Findings as (proposed)
    for i, title in enumerate(finding_titles):
        desc = key_finding_descriptions[i] if i < len(key_finding_descriptions) else ""
        if desc:
            first_sentence = desc.split(".")[0].strip()
            entry_text = f"{title} (proposed). {first_sentence}" if first_sentence else f"{title} (proposed)"
        else:
            entry_text = f"{title} (proposed)"
        parsed["established"].append(entry_text)

    # Add cycle_insights to Open Questions (deduplicate)
    existing_oq = set(parsed["open_questions"])
    for insight in cycle_insights:
        if insight and insight not in existing_oq:
            parsed["open_questions"].append(insight)
            existing_oq.add(insight)

    def _format_doc(sections: dict, eviction_body: Optional[str] = None) -> str:
        parts: list = []
        if eviction_body:
            parts.append("## Eviction Summary")
            parts.append(eviction_body)
            parts.append("")
        for header, key in [
            ("## Established Findings", "established"),
            ("## Failed Approaches", "failed"),
            ("## Open Questions", "open_questions"),
        ]:
            parts.append(header)
            for item in sections[key]:
                parts.append(f"- {item}")
            parts.append("")
        return "\n".join(parts).strip() + "\n"

    MAX_MAIN_CHARS = 7500
    EVICTION_MAX = 500
    TOTAL_MAX = MAX_MAIN_CHARS + EVICTION_MAX  # 8000

    doc = _format_doc(parsed)
    if len(doc) <= MAX_MAIN_CHARS:
        # No trimming needed; preserve any existing eviction summary
        if prior_eviction["established"] or prior_eviction["failed"] or prior_eviction["open_questions"]:
            eviction_body = _format_eviction_body(
                prior_eviction["established"],
                prior_eviction["failed"],
                prior_eviction["open_questions"],
                prior_eviction["failed_titles"],
            )
            return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]
        return doc

    # Snapshot sections before trimming
    before = {k: list(v) for k, v in parsed.items() if k in ("established", "failed", "open_questions")}

    # Trim oldest entries (from top of each section) to stay under main limit
    while len(doc) > MAX_MAIN_CHARS:
        trimmed = False
        for key in sorted(
            ["established", "failed", "open_questions"],
            key=lambda k: -len(parsed[k]),
        ):
            if len(parsed[key]) > 1:
                parsed[key] = parsed[key][1:]
                trimmed = True
                break
        if not trimmed:
            break
        doc = _format_doc(parsed)

    # Compute newly evicted entries
    new_established = len(before["established"]) - len(parsed["established"])
    new_failed = len(before["failed"]) - len(parsed["failed"])
    new_oq = len(before["open_questions"]) - len(parsed["open_questions"])

    evicted_failed_titles = [e[:60] for e in before["failed"][:new_failed]] if new_failed > 0 else []

    # Merge with prior eviction summary
    total_established = prior_eviction["established"] + new_established
    total_failed = prior_eviction["failed"] + new_failed
    total_oq = prior_eviction["open_questions"] + new_oq
    all_failed_titles = prior_eviction["failed_titles"] + evicted_failed_titles

    eviction_body = _format_eviction_body(total_established, total_failed, total_oq, all_failed_titles)
    return _format_doc(parsed, eviction_body[:EVICTION_MAX])[:TOTAL_MAX]


async def _compute_research_anomalies(project_id, session: AsyncSession) -> list:
    """Analyze recent task data to identify pipeline anomalies for undirected research focus.

    Returns a list of {signal: str, severity: 'high'|'medium', details: str} dicts.
    """
    now = _utcnow()
    window_48h = now - timedelta(hours=48)
    prior_window_start = now - timedelta(hours=96)

    # Query last 50 terminal tasks for this project
    terminal_tasks = (await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
        ).order_by(Task.created_at.desc()).limit(50)
    )).scalars().all()

    anomalies = []

    # --- Failure class analysis ---
    recent_failures = [t for t in terminal_tasks if t.created_at and t.created_at >= window_48h]
    prior_failures = [t for t in terminal_tasks if t.created_at and prior_window_start <= t.created_at < window_48h]

    # Group by failure class
    recent_by_class: dict = {}
    for t in recent_failures:
        cls = classify_failure(t)
        recent_by_class.setdefault(cls, []).append(t)

    prior_by_class: dict = {}
    for t in prior_failures:
        cls = classify_failure(t)
        prior_by_class[cls] = prior_by_class.get(cls, 0) + 1

    for cls, tasks in recent_by_class.items():
        count = len(tasks)
        prior_count = prior_by_class.get(cls, 0)
        if count > 3 and prior_count > 0 and count >= prior_count * 2:
            anomalies.append({
                "signal": f"{cls} failures increased {prior_count}x → {count}x in last 48h",
                "severity": "high",
                "details": (
                    f"{cls} failure count rose from {prior_count} to {count} between the prior "
                    f"48h window and the current one. Investigate common error patterns."
                ),
            })
        elif count > 3:
            anomalies.append({
                "signal": f"{cls} failures: {count} occurrences in last 48h",
                "severity": "medium",
                "details": (
                    f"{count} tasks failed with class {cls} in the last 48h. "
                    "Consider investigating shared root causes."
                ),
            })

    # --- Direction merge rate and failure rate analysis ---
    project = (await session.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if project:
        for direction in (project.directions or []):
            if direction.get("status") != "active":
                continue
            history = direction.get("research_history") or []
            d_id = direction.get("id", "?")
            d_text = (direction.get("text") or "")[:60]

            # Declining merge rate across last 3 cycles
            if len(history) >= 3:
                last3 = history[-3:]
                rates = []
                for entry in last3:
                    created = entry.get("tasks_created", 0)
                    merged = entry.get("prs_merged", 0)
                    rates.append(merged / created if created > 0 else 0.0)
                if rates[0] > rates[1] > rates[2] and rates[0] > 0.1:
                    anomalies.append({
                        "signal": f"Direction '{d_id}' merge rate declining: {rates[0]:.2f} → {rates[1]:.2f} → {rates[2]:.2f}",
                        "severity": "medium",
                        "details": (
                            f"Direction \"{d_text}\" (id: {d_id}) has seen a consistent decline in "
                            f"merge rate over the last 3 cycles ({rates[0]:.2f} → {rates[1]:.2f} → {rates[2]:.2f}). "
                            "May indicate implementation barriers or stale findings."
                        ),
                    })

            # High failure rate in last 2 cycles (>50%)
            if len(history) >= 2:
                last2 = history[-2:]
                total_created = sum(e.get("tasks_created", 0) for e in last2)
                total_failed = sum(e.get("tasks_failed", 0) for e in last2)
                if total_created > 0 and total_failed / total_created > 0.5:
                    fail_pct = int(total_failed / total_created * 100)
                    anomalies.append({
                        "signal": f"Direction '{d_id}' has {fail_pct}% task failure rate in last 2 cycles",
                        "severity": "high",
                        "details": (
                            f"Direction \"{d_text}\" (id: {d_id}) had {total_failed}/{total_created} tasks fail "
                            f"in the last 2 research cycles ({fail_pct}% failure rate). "
                            "The findings may be generating unimplementable tasks."
                        ),
                    })

    # --- Persist anomaly tracking ---
    proj_key = str(project_id)
    proj_anomalies = _shared_state.persistent_anomalies.setdefault(proj_key, {})
    now_iso = now.isoformat()
    seen_keys: set = set()
    for a in anomalies:
        # Build a stable key from the signal's failure class or direction id
        signal = a["signal"]
        # Extract anomaly key: e.g. "CODE_ERROR" from "CODE_ERROR failures..."
        # or "Direction_d-abc123" from "Direction 'd-abc123' ..."
        if signal.startswith("Direction '"):
            # Include direction ID to avoid key collisions across directions
            parts = signal.split("'")
            anomaly_key = f"Direction_{parts[1]}" if len(parts) > 1 else signal.split()[0]
        else:
            parts = signal.split()
            anomaly_key = parts[0] if parts else signal
        if anomaly_key in seen_keys:
            # Already processed this key this cycle — update metadata only, don't double-count
            if anomaly_key in proj_anomalies:
                proj_anomalies[anomaly_key]["last_seen"] = now_iso
                if a["severity"] == "high":
                    proj_anomalies[anomaly_key]["severity"] = "high"
                    proj_anomalies[anomaly_key]["latest_details"] = a["details"]
            continue
        seen_keys.add(anomaly_key)
        if anomaly_key in proj_anomalies:
            proj_anomalies[anomaly_key]["occurrence_count"] += 1
            proj_anomalies[anomaly_key]["last_seen"] = now_iso
            proj_anomalies[anomaly_key]["severity"] = a["severity"]
            proj_anomalies[anomaly_key]["latest_details"] = a["details"]
        else:
            proj_anomalies[anomaly_key] = {
                "first_seen": now_iso,
                "last_seen": now_iso,
                "occurrence_count": 1,
                "severity": a["severity"],
                "latest_details": a["details"],
            }
    # Expire anomalies not seen for 48h
    stale_cutoff = (now - timedelta(hours=48)).isoformat()
    for key in list(proj_anomalies):
        if key not in seen_keys and proj_anomalies[key]["last_seen"] < stale_cutoff:
            del proj_anomalies[key]

    return anomalies


async def _check_anomaly_proposals(project, session):
    """Generate direction proposals from persistent anomalies (occurrence_count >= 3).

    Creates at most 1 proposal per call. Proposals require human approval (status='proposed').
    """
    proj_key = str(project.id)
    proj_anomalies = _shared_state.persistent_anomalies.get(proj_key, {})
    if not proj_anomalies:
        return

    proj_directions = list(project.directions or [])
    rejected_proposals = [d for d in proj_directions if d.get("status") == "rejected"]
    pending_count = sum(1 for d in proj_directions if d.get("status") == "proposed")
    max_proposals = 10

    if pending_count >= max_proposals:
        return

    now_iso = _utcnow().isoformat()
    for anomaly_key, info in sorted(proj_anomalies.items(), key=lambda x: x[1].get("occurrence_count", 0), reverse=True):
        if info["occurrence_count"] < 3:
            continue

        # Build proposal text based on the anomaly key pattern
        details = info.get("latest_details", "")
        if anomaly_key.startswith("Direction_"):
            # Direction-level anomaly (declining merge rate / high failure)
            proposal_text = f"Investigate {anomaly_key} anomaly — {details}"[:500]
        else:
            # Failure class anomaly (e.g. CODE_ERROR, PROVISION_FAILURE)
            proposal_text = f"Investigate and reduce {anomaly_key} task failures — {details}"[:500]

        if _is_direction_duplicate(proposal_text, proj_directions, rejected_proposals):
            logger.info(f"[ANOMALY] Skipping proposal for {anomaly_key} — overlaps with existing direction")
            continue

        new_direction = {
            "id": f"d-{uuid.uuid4().hex[:8]}",
            "text": proposal_text,
            "priority": "high" if info["severity"] == "high" else "medium",
            "paths": [],
            "added_at": now_iso,
            "status": "proposed",
            "proposed_by": "ANOMALY_DETECTOR",
            "proposal_rationale": (
                f"Anomaly '{anomaly_key}' detected {info['occurrence_count']} times "
                f"since {info['first_seen']}. Severity: {info['severity']}."
            )[:500],
            "proposed_at": now_iso,
        }
        proj_directions.append(new_direction)
        project.directions = proj_directions
        flag_modified(project, "directions")
        session.add(project)
        logger.info(f"[ANOMALY] Created direction proposal for persistent anomaly: {anomaly_key}")

        # Reset occurrence count so we don't re-propose until it recurs
        info["occurrence_count"] = 0

        # Cap at 1 anomaly-driven proposal per cycle
        break


async def _expire_stale_proposals(session) -> int:
    """Expire direction proposals older than 30 days across all projects."""
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    projects = (await session.execute(
        select(Project).where(Project.autonomous_enabled.is_(True))
    )).scalars().all()
    total_expired = 0
    for project in projects:
        dirs = list(project.directions or [])
        expired_count = 0
        for d in dirs:
            if d.get("status") == "proposed" and (d.get("proposed_at") or d.get("added_at", "")) < cutoff:
                d["status"] = "expired"
                d["expired_at"] = _utcnow().isoformat()
                expired_count += 1
        if expired_count:
            project.directions = dirs
            flag_modified(project, "directions")
            session.add(project)
            total_expired += expired_count
            logger.info(f"[DIRECTIONS] Expired {expired_count} stale proposals for project {project.id}")
    if total_expired:
        await session.commit()
    return total_expired


def _check_direction_health(direction: dict) -> Optional[str]:
    """Return a pause reason if this direction should be auto-paused, else None."""
    history = direction.get("research_history") or []

    # Rule 1: Persistent saturation — possibly_saturated AND last 5+ cycles all produced ≤1 task
    if direction.get("possibly_saturated") and len(history) >= 5:
        recent5 = history[-5:]
        if all(entry.get("tasks_created", 0) <= 1 for entry in recent5):
            return "Saturated: last 5 research cycles produced no actionable findings"

    # Rule 2: Zero merge rate — 5+ cycles with 0 PRs merged total, and the most
    # recent cycle has no tasks still in-flight (tasks_created == tasks_completed + tasks_failed)
    if len(history) >= 5:
        total_merged = sum(entry.get("prs_merged", 0) for entry in history)
        if total_merged == 0:
            last = history[-1]
            created = last.get("tasks_created", 0)
            finished = last.get("tasks_completed", 0) + last.get("tasks_failed", 0)
            if created == 0 or finished >= created:
                total_created = sum(entry.get("tasks_created", 0) for entry in history)
                return f"Unproductive: 0 PRs merged from {len(history)} research cycles ({total_created} tasks created)"

    # Rule 3: High failure rate — last 3 cycles have >70% tasks failed
    if len(history) >= 3:
        recent3 = history[-3:]
        total_created_3 = sum(entry.get("tasks_created", 0) for entry in recent3)
        total_failed_3 = sum(entry.get("tasks_failed", 0) for entry in recent3)
        if total_created_3 > 0 and total_failed_3 / total_created_3 > 0.7:
            return "High failure rate: >70% of spawned tasks failed in last 3 cycles"

    return None


def _check_direction_completion(direction: dict) -> Optional[str]:
    """Return a completion suggestion string if this direction appears successfully complete, else None."""
    history = direction.get("research_history") or []
    if len(history) < 4:
        return None
    total_merged = sum(entry.get("prs_merged", 0) for entry in history)
    if total_merged < 3:
        return None
    recent2 = history[-2:]
    recent_created = sum(e.get("tasks_created", 0) for e in recent2)
    recent_merged = sum(e.get("prs_merged", 0) for e in recent2)
    if recent_created <= 2 and recent_merged <= 1:
        return (
            f"Direction appears complete: {total_merged} PRs merged across "
            f"{len(history)} cycles, last 2 cycles low-yield"
        )
    return None


def _is_direction_duplicate(proposal_text: str, existing_directions: list, rejected_proposals: list) -> bool:
    """Return True if proposal_text overlaps with an existing or recently rejected direction."""
    STOPWORDS = {'the', 'a', 'an', 'to', 'for', 'and', 'or', 'in', 'of', 'is', 'it',
                 'this', 'that', 'with', 'by', 'we', 'should', 'be', 'are', 'on', 'as',
                 'feat', 'fix', 'refactor', 'add', 'update', 'improve'}
    words = set(proposal_text.lower().split()) - STOPWORDS
    if len(words) < 2:
        return False
    for d in existing_directions:
        existing_text = (d.get("text") or "").lower()
        existing_words = set(existing_text.split()) - STOPWORDS
        if len(words & existing_words) >= 4:
            return True
    cutoff = (_utcnow() - timedelta(days=30)).isoformat()
    for entry in rejected_proposals:
        if entry.get("rejected_at", "") < cutoff:
            continue
        rejected_words = set((entry.get("text") or "").lower().split()) - STOPWORDS
        if len(words & rejected_words) >= 4:
            return True
    return False


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


def _parse_research_insights(task_id: str) -> List[Dict[str, Any]]:
    """Read structured research insights from the agent's /tmp output file."""
    insights_path = f"/tmp/research_insights_{task_id}.json"
    try:
        if not os.path.exists(insights_path):
            return []
        with open(insights_path, "r") as f:
            raw = f.read(200_000)
        data = json.loads(raw)
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict) and "summary" in i]
    except Exception as e:
        logger.warning(f"[RESEARCH] Failed to read insights file {insights_path}: {e}")
    return []


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


def _log_file_overlaps(subtasks: List[Dict], label: str = "PLAN") -> List[tuple]:
    """Log warnings when subtasks share related_files (for observability).

    Returns a list of (i, j, overlapping_files) tuples for each overlapping pair.
    """
    overlaps = []
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
                overlaps.append((i, j, overlap))
    return overlaps


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
    last_err = None
    for _attempt in range(3):
        try:
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            sha = await get_branch_sha(gh_token, owner, repo, source_branch)
            await create_branch(gh_token, owner, repo, branch_name, sha)
            logger.info(f"[{label}] Created branch {branch_name} from {source_branch}")
            return True
        except Exception as e:
            last_err = e
            if gh_token:
                try:
                    await get_branch_sha(gh_token, owner, repo, branch_name)
                    logger.info(f"[{label}] Branch {branch_name} already exists — reusing")
                    return True
                except Exception:
                    pass
            if _attempt < 2:
                logger.warning(f"[{label}] Branch creation attempt {_attempt + 1} failed, retrying in {5 * (2 ** _attempt)}s: {e!r}")
                await asyncio.sleep(5 * (2 ** _attempt))
            else:
                if gh_token:
                    logger.error(f"[{label}] Branch {branch_name} creation failed after 3 attempts: {last_err!r}")
                else:
                    logger.error(f"[{label}] Cannot create branch — GitHub token unavailable: {last_err!r}")
    e = last_err
    task.status = TaskStatus.FAILED
    task.latest_status_message = f"Failed to create {label.lower()} branch: {type(e).__name__}: {e!r}"[:500]
    session.add(task)
    await _maybe_create_failure_alert(task, session, f"Failed to create branch: {type(e).__name__}: {e!r}"[:200])
    await session.commit()
    return False


async def _handle_plan_completion(task: Task, logs: str, session: AsyncSession):
    """Parse plan output and create child tasks with dependency tracking."""
    plan_data = _parse_plan_output(logs)
    if not plan_data or not plan_data.get("subtasks"):
        task.latest_status_message = "Plan agent produced no valid subtasks"
        task.status = TaskStatus.FAILED
        session.add(task)
        await _maybe_create_failure_alert(task, session, "Plan agent produced no valid subtasks")
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
        await _maybe_create_failure_alert(task, session, "Plan has circular dependencies")
        await session.commit()
        return

    # Store parsed plan and set parent to EVALUATING (children executing)
    task.plan_output = plan_data
    task.status = TaskStatus.EVALUATING
    session.add(task)

    parent_base = (task.first_payload).get("base_branch", "main")
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
        parent_payload = task.first_payload
        model = parent_payload.get('model')
        effort = parent_payload.get('effort')
        create_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get("create")
        parent_direction_id = (task.context or {}).get('direction_id')
        parent_research_task_id = (task.context or {}).get('research_task_id')

        for i, child in enumerate(existing_children):
            child_payload = child.first_payload
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
            child.requires_approval = False  # high-risk children execute freely; approval gates the final rollup merge
            child_approval_reason = child_payload.get('approval_reason') if child_risk == 'high' else None
            existing_ctx = child.context or {}
            child.context = {
                **existing_ctx,
                "plan_branch": plan_branch,
                "parent_base": parent_base,
                "scope": child_payload.get('scope', 'small'),
                "risk": child_risk,
                "related_files": related_files,
                **({"approval_reason": child_approval_reason} if child_approval_reason else {}),
                # Inherit direction_id from plan parent if present and not overridden by finding
                **({"direction_id": parent_direction_id} if parent_direction_id and not existing_ctx.get('direction_id') else {}),
                **({"research_task_id": parent_research_task_id} if parent_research_task_id and not existing_ctx.get('research_task_id') else {}),
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

        overlaps = _log_file_overlaps(subtasks, "PLAN")
        for (i, j, shared_files) in overlaps:
            for idx in (i, j):
                if idx < len(existing_children):
                    ctx = dict(existing_children[idx].context or {})
                    warnings = ctx.get('file_overlap_warnings', [])
                    other_title = subtasks[j if idx == i else i].get('title', f'#{j if idx == i else i}')
                    warnings.append({'shared_files': sorted(shared_files), 'with_task': other_title})
                    ctx['file_overlap_warnings'] = warnings
                    existing_children[idx].context = ctx
                    session.add(existing_children[idx])
        # Single atomic commit: base_branch, requires_approval, context, and depends_on
        # are all written together so no worker can observe a partially-enriched child.
        await session.commit()

        for ct in existing_children:
            await broadcast_to_project(str(task.project_id), "task_updated",
                                       {"task_id": str(ct.id), "status": str(ct.status)})

        logger.info(f"[PLAN] Enriched {len(existing_children)} agent-spawned subtasks for plan {task.id}")
        return

    # First pass: create all child tasks
    parent_payload = task.first_payload
    model = parent_payload.get('model')
    effort = parent_payload.get('effort')
    plan_parent_context = {
        "parent_task_id": str(task.id),
        "parent_title": task.title,
        "parent_prompt": (parent_payload.get("prompt") or parent_payload.get("description") or "")[:500]
    }
    parent_direction_id = (task.context or {}).get('direction_id')
    parent_research_task_id = (task.context or {}).get('research_task_id')
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
                # Inherit direction_id from plan parent if present and not overridden by finding
                **({"direction_id": parent_direction_id} if parent_direction_id else {}),
                **({"research_task_id": parent_research_task_id} if parent_research_task_id else {}),
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
    # Save original (explicit) deps so we can restore them if injection creates a cycle
    original_deps = {i: list(st.get("depends_on_indices", [])) for i, st in enumerate(subtasks)}
    _inject_file_overlap_deps(subtasks, "PLAN")

    # Re-validate after file overlap injection — injection can create cycles
    # (e.g., A explicitly depends on B, but file overlap adds B → A)
    if not _validate_plan_dependencies(subtasks):
        logger.error("[PLAN] Circular dependency after file overlap injection — reverting to explicit deps only")
        for i, st in enumerate(subtasks):
            if original_deps.get(i):
                st["depends_on_indices"] = original_deps[i]
            else:
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

    base_branch = (task.first_payload).get("base_branch", "main")

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

    base_branch = (task.first_payload).get("base_branch", "main")

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


async def _handle_design_doc_completion(task: Task, logs: str, session: AsyncSession):
    """Handle Phase 1 completion: chain to Phase 2 (strategic review).

    The design doc itself was already updated incrementally via POST /api/agent/design-doc
    during agent execution. This handler records status and spawns the next phase.
    Chain and status are committed together so a failed Phase 2 spawn doesn't leave
    the task marked COMPLETED with no follow-up.
    """
    result = await session.execute(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    project = result.scalars().first()
    if not project:
        logger.error(f"[DESIGN DOC] Task {task.id}: project {task.project_id} not found")
        await _create_strategic_alert(task.project_id, session,
            f"Design doc update failed: project not found (task {str(task.id)[:8]})")
        return

    doc = project.design_doc or {}
    rev = doc.get("_meta", {}).get("revision", 0)
    page_count = sum(1 for k in doc if not k.startswith("_"))
    logger.info(f"[DESIGN DOC] Task {task.id} completed: {page_count} pages, rev {rev}")

    if page_count == 0:
        await _create_strategic_alert(task.project_id, session,
            f"Design doc update produced 0 pages (task {str(task.id)[:8]})")

    # Chain: spawn Phase 2 (strategic review)
    # Do this BEFORE committing task status so a failed spawn doesn't leave
    # the task as COMPLETED with no Phase 2 follow-up.
    try:
        await session.refresh(project)
        await _spawn_strategic_review(project, session)
        task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — strategic review spawned"
    except Exception as chain_exc:
        logger.error(f"[DESIGN DOC] Failed to chain Phase 2 for task {task.id}: {chain_exc}", exc_info=True)
        task.latest_status_message = f"Design doc: {page_count} pages, rev {rev} — FAILED to spawn strategic review: {chain_exc!r}"[:500]
        await _create_strategic_alert(task.project_id, session,
            f"Strategic review failed to spawn after design doc update (task {str(task.id)[:8]}): {chain_exc!r}")
    session.add(task)
    await session.commit()


async def _handle_strategic_completion(task: Task, logs: str, session: AsyncSession):
    """Handle strategic review completion.

    Vision and direction updates were already applied incrementally via POST /api/agent/strategic
    during agent execution. This handler just records final status and checks for issues.
    Falls back to log parsing for backward compatibility if the agent used the old approach.
    """
    project = await session.get(Project, task.project_id)
    if not project:
        logger.error(f"[STRATEGIC] Task {task.id}: project {task.project_id} not found")
        return

    vision_rev = (project.vision or {}).get("revision", 0)
    active_dirs = sum(1 for d in (project.directions or []) if d.get("status") == "active")

    # Check if the agent used the new incremental API during execution
    task_payload = task.first_payload
    spawned_vision_rev = (task_payload.get("vision") or {}).get("revision", 0)
    api_was_used = (task.context or {}).get("_strategic_api_used", False) or vision_rev > spawned_vision_rev

    if api_was_used:
        task.latest_status_message = f"Strategic review complete — vision rev {vision_rev}, {active_dirs} active directions"
        session.add(task)
        await session.commit()
        logger.info(f"[STRATEGIC] Task {task.id} complete via API: vision rev {vision_rev}, {active_dirs} active directions")
        return

    # Fallback: try log-based parsing for backward compatibility
    try:
        parsed = json.loads(logs[:500_000]) if logs else None
    except (json.JSONDecodeError, ValueError):
        parsed = None

    if not parsed or not isinstance(parsed, dict):
        # No API calls and no parseable output — record failure
        project = await session.get(Project, task.project_id)
        if project:
            current_vision = project.vision or {}
            failures = current_vision.get("_strategic_failures", 0) + 1
            current_vision["_strategic_failures"] = failures
            project.vision = current_vision
            flag_modified(project, "vision")
            session.add(project)
        task.latest_status_message = "Strategic output could not be parsed"
        session.add(task)
        await session.commit()
        failure_num = current_vision.get("_strategic_failures", "?") if project else "?"
        logger.warning(f"[STRATEGIC] Task {task.id}: unparseable output (failure #{failure_num})")
        if project:
            await _create_strategic_alert(task.project_id, session,
                f"Strategic review could not parse agent output (failure #{failure_num}, task {str(task.id)[:8]})")
        return

    new_vision = parsed.get("vision")
    direction_ops = parsed.get("direction_operations", [])

    # Lock project row to prevent concurrent direction/vision mutations
    result = await session.execute(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    project = result.scalars().first()
    if not project:
        logger.error(f"[STRATEGIC] Task {task.id}: project {task.project_id} not found")
        return

    # Store raw output for admin visibility
    task.plan_output = parsed
    session.add(task)

    # Update vision — merge with current DB state to avoid overwriting concurrent user edits
    if new_vision and isinstance(new_vision, dict):
        current_vision = dict(project.vision or {})
        # Check if user edited vision while the strategic cycle was running
        payload_vision_rev = (task.first_payload.get("vision") or {}).get("revision", 0)
        db_vision_rev = current_vision.get("revision", 0)
        if db_vision_rev > payload_vision_rev:
            # User edited vision since this cycle started — merge carefully
            # Keep user's purpose/criteria if they changed them, take agent's systems/priorities/context
            logger.info(f"[STRATEGIC] Vision was edited during cycle (payload rev {payload_vision_rev}, DB rev {db_vision_rev}) — merging")
            merged = dict(current_vision)  # Start from user's current state
            # Agent's structural analysis takes precedence for these fields
            for key in ("systems", "priorities", "context"):
                if key in new_vision:
                    merged[key] = new_vision[key]
            # Only update purpose/criteria if the user didn't change them
            if current_vision.get("purpose") == (task.first_payload.get("vision") or {}).get("purpose"):
                if "purpose" in new_vision:
                    merged["purpose"] = new_vision["purpose"]
            if current_vision.get("success_criteria") == (task.first_payload.get("vision") or {}).get("success_criteria"):
                if "success_criteria" in new_vision:
                    merged["success_criteria"] = new_vision["success_criteria"]
            # Preserve any extra fields the agent added
            for key in new_vision:
                if key not in merged:
                    merged[key] = new_vision[key]
            new_vision = merged
        new_vision["revision"] = (db_vision_rev or 0) + 1
        new_vision["last_updated"] = _utcnow().isoformat()
        new_vision.pop("_strategic_failures", None)  # Reset failure counter on success
        project.vision = new_vision
        flag_modified(project, "vision")
        logger.info(f"[STRATEGIC] Updated vision for project {project.id} to revision {new_vision['revision']}")
    else:
        # No vision produced — increment failure counter to prevent infinite strategic loops
        current_vision = dict(project.vision or {})
        failures = current_vision.get("_strategic_failures", 0) + 1
        current_vision["_strategic_failures"] = failures
        project.vision = current_vision
        flag_modified(project, "vision")
        logger.warning(f"[STRATEGIC] Task {task.id}: no vision produced (failure #{failures})")
        if failures >= 2:
            await _create_strategic_alert(task.project_id, session,
                f"Strategic review produced no vision ({failures} consecutive failures, task {str(task.id)[:8]})")

    # Process direction operations (capped to prevent flooding)
    MAX_DIRECTION_OPS = 10
    MAX_DIRECTION_CREATES = 5
    ops_applied = 0
    if direction_ops and isinstance(direction_ops, list):
        if len(direction_ops) > MAX_DIRECTION_OPS:
            logger.warning(f"[STRATEGIC] Task {task.id}: truncating {len(direction_ops)} direction ops to {MAX_DIRECTION_OPS}")
            direction_ops = direction_ops[:MAX_DIRECTION_OPS]
        directions = list(project.directions or [])
        creates_count = 0

        for op_entry in direction_ops:
            if not isinstance(op_entry, dict):
                continue
            op = op_entry.get("op")
            rationale = str(op_entry.get("rationale", ""))
            if len(rationale) > STRATEGIC_RATIONALE_MAX:
                logger.warning(f"[STRATEGIC] Task {task.id}: truncating rationale {len(rationale)}→{STRATEGIC_RATIONALE_MAX} chars")
                rationale = rationale[:STRATEGIC_RATIONALE_MAX]

            if op == "create":
                if creates_count >= MAX_DIRECTION_CREATES:
                    continue
                text = op_entry.get("text", "").strip()
                if not text:
                    continue
                if len(text) > STRATEGIC_DIRECTION_TEXT_MAX:
                    logger.warning(f"[STRATEGIC] Task {task.id}: truncating direction text {len(text)}→{STRATEGIC_DIRECTION_TEXT_MAX} chars")
                text = text[:STRATEGIC_DIRECTION_TEXT_MAX]
                # Dedup: skip if an active/proposed direction has very similar text
                text_lower = text.lower()
                if any(d.get("status") in ("active", "proposed") and
                       d.get("text", "").lower().strip() == text_lower
                       for d in directions):
                    logger.info(f"[STRATEGIC] Skipping duplicate direction '{text[:60]}' for project {project.id}")
                    continue
                raw_priority = op_entry.get("priority", "medium")
                priority = raw_priority if raw_priority in ("high", "medium", "low") else "medium"
                new_dir = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": text,
                    "status": "active",
                    "priority": priority,
                    "notes": [{"text": f"Created by strategic review: {rationale}", "added_at": _utcnow().isoformat()}] if rationale else [],
                    "research_history": [],
                }
                if op_entry.get("paths"):
                    new_dir["paths"] = op_entry["paths"]
                if op_entry.get("system_id"):
                    sid = str(op_entry["system_id"])
                    if len(sid) > STRATEGIC_SYSTEM_ID_MAX:
                        logger.warning(f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid)}→{STRATEGIC_SYSTEM_ID_MAX} chars")
                    new_dir["system_id"] = sid[:STRATEGIC_SYSTEM_ID_MAX]
                directions.append(new_dir)
                ops_applied += 1
                creates_count += 1
                logger.info(f"[STRATEGIC] Created direction '{text[:60]}' for project {project.id}")

            elif op == "update":
                dir_id = op_entry.get("direction_id")
                changes = op_entry.get("changes", {})
                if not dir_id or not changes:
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        if "text" in changes:
                            new_text = str(changes["text"])
                            if len(new_text) > STRATEGIC_DIRECTION_TEXT_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: truncating direction text {len(new_text)}→{STRATEGIC_DIRECTION_TEXT_MAX} chars")
                            d["text"] = new_text[:STRATEGIC_DIRECTION_TEXT_MAX]
                        if "priority" in changes and changes["priority"] in ("high", "medium", "low"):
                            d["priority"] = changes["priority"]
                        if "paths" in changes:
                            d["paths"] = changes["paths"]
                        if "system_id" in changes:
                            if changes["system_id"]:
                                sid_new = str(changes["system_id"])
                                if len(sid_new) > STRATEGIC_SYSTEM_ID_MAX:
                                    logger.warning(f"[STRATEGIC] Task {task.id}: truncating system_id {len(sid_new)}→{STRATEGIC_SYSTEM_ID_MAX} chars")
                                d["system_id"] = sid_new[:STRATEGIC_SYSTEM_ID_MAX]
                            else:
                                d["system_id"] = None
                        if "status" in changes and changes["status"] in ("active", "paused", "expired"):
                            # Block reactivating a direction the user manually paused
                            if changes["status"] == "active" and d.get("status") == "paused" and not d.get("auto_paused"):
                                logger.warning(f"[STRATEGIC] Blocked reactivation of user-paused direction {dir_id}")
                            else:
                                d["status"] = changes["status"]
                        # Record rationale as a note
                        if rationale:
                            notes = d.get("notes", [])
                            notes.append({"text": f"Strategic update: {rationale}", "added_at": _utcnow().isoformat()})
                            if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}")
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ops_applied += 1
                        logger.info(f"[STRATEGIC] Updated direction {dir_id} for project {project.id}: {list(changes.keys())}")
                        break

            elif op == "retire":
                dir_id = op_entry.get("direction_id")
                if not dir_id:
                    continue
                for d in directions:
                    if d.get("id") == dir_id:
                        d["status"] = "expired"
                        if rationale:
                            notes = d.get("notes", [])
                            notes.append({"text": f"Retired by strategic review: {rationale}", "added_at": _utcnow().isoformat()})
                            if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                                logger.warning(f"[STRATEGIC] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {dir_id}")
                            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        ops_applied += 1
                        logger.info(f"[STRATEGIC] Retired direction {dir_id} for project {project.id}")
                        break

        if ops_applied > 0:
            project.directions = directions
            flag_modified(project, "directions")

    session.add(project)

    # Process messages from agent (questions, blockers, notes)
    msgs_created = 0
    for msg_entry in (parsed.get("messages") or [])[:5]:
        if not isinstance(msg_entry, dict):
            continue
        content = (msg_entry.get("content") or "").strip()
        if not content:
            continue
        if len(content) > STRATEGIC_MESSAGE_CONTENT_MAX:
            logger.warning(f"[STRATEGIC] Task {task.id}: truncating message content {len(content)}→{STRATEGIC_MESSAGE_CONTENT_MAX} chars")
            content = content[:STRATEGIC_MESSAGE_CONTENT_MAX]
        try:
            msg_type = MessageType(msg_entry.get("type", "NOTE").upper())
        except ValueError:
            msg_type = MessageType.NOTE
        new_msg = ProjectMessage(
            project_id=project.id,
            message_type=msg_type,
            from_type="strategic",
            from_task_id=task.id,
            content=content,
            context=msg_entry.get("context") or {},
        )
        session.add(new_msg)
        msgs_created += 1

    # Resolve messages the agent addressed
    msgs_resolved = 0
    # Support both formats: resolved_messages (with resolution text) and resolved_message_ids (legacy)
    resolved_entries = parsed.get("resolved_messages") or []
    if not resolved_entries:
        # Fall back to legacy format (plain ID list)
        resolved_entries = [{"id": mid} for mid in (parsed.get("resolved_message_ids") or [])]
    for entry in resolved_entries[:10]:
        try:
            msg_id = entry.get("id") if isinstance(entry, dict) else str(entry)
            resolution_text = entry.get("resolution", "Addressed by strategic review") if isinstance(entry, dict) else "Addressed by strategic review"
            msg = await session.get(ProjectMessage, uuid.UUID(str(msg_id)))
            if msg and msg.project_id == project.id and msg.status == "open":
                msg.status = "resolved"
                if resolution_text:
                    rt = str(resolution_text)
                    if len(rt) > STRATEGIC_RESOLUTION_MAX:
                        logger.warning(f"[STRATEGIC] Task {task.id}: truncating resolution {len(rt)}→{STRATEGIC_RESOLUTION_MAX} chars")
                    msg.resolution = rt[:STRATEGIC_RESOLUTION_MAX]
                else:
                    msg.resolution = "Addressed by strategic review"
                msg.resolved_at = _utcnow()
                session.add(msg)
                msgs_resolved += 1
        except (ValueError, TypeError):
            pass

    vision_rev = (project.vision or {}).get("revision", "?")
    task.latest_status_message = f"Strategic review complete — vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} messages, {msgs_resolved} resolved"
    session.add(task)
    await session.commit()  # Single commit: vision + directions + messages + task status
    logger.info(f"[STRATEGIC] Task {task.id} complete: vision rev {vision_rev}, {ops_applied} direction ops, {msgs_created} msgs created, {msgs_resolved} resolved")


async def _handle_research_completion(task: Task, logs: str, session: AsyncSession):
    """Parse research findings and create tasks from them."""
    # Depth guard: prevent infinite research recursion
    MAX_RESEARCH_DEPTH = 3
    research_depth = (task.context or {}).get("research_depth", 0)
    parent_research_task_id = (task.context or {}).get("parent_research_task_id")
    if research_depth >= MAX_RESEARCH_DEPTH:
        task.latest_status_message = f"Research depth limit ({MAX_RESEARCH_DEPTH}) reached — not spawning further tasks"
        session.add(task)
        await session.commit()
        logger.warning(f"[RESEARCH] Depth limit reached for task {task.id} (depth={research_depth})")
        return

    findings = _parse_research_findings(logs)
    insights = _parse_research_insights(str(task.id))
    if not findings:
        task.latest_status_message = "Research completed with no actionable findings"
        task.plan_output = []
        session.add(task)
        await session.commit()
        return

    # Store all findings for admin visibility before filtering
    task.plan_output = findings
    session.add(task)
    await session.commit()

    # Separate direction proposals and notes from actionable findings — these are processed
    # separately and must not enter the hash-based deduplication loop (they never create tasks,
    # so their hashes would never be stored, allowing identical entries to accumulate).
    direction_proposals = [f for f in findings if f.get("type") == "direction_proposal"]
    direction_notes = [f for f in findings if f.get("type") == "direction_note"]
    findings = [f for f in findings if f.get("type") not in ("direction_proposal", "direction_note")]

    # Cap findings to prevent runaway task creation
    if len(findings) > 15:
        findings = findings[:15]

    original_base = (task.first_payload).get("base_branch", "main")
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

    # Query recent tasks (last 90 days) for hash-based deduplication across research cycles
    ninety_days_ago = _utcnow() - timedelta(days=90)
    recent_tasks_for_dedup = (await session.execute(
        select(Task).where(
            Task.project_id == task.project_id,
            Task.created_at >= ninety_days_ago,
        )
    )).scalars().all()
    recent_finding_hashes = {
        t.context["finding_hash"] for t in recent_tasks_for_dedup if t.context and t.context.get("finding_hash")
    }
    # Build a map of finding_hash -> outcome for failed/cancelled tasks for better logging
    failed_hashes = {
        t.context["finding_hash"]: t.status
        for t in recent_tasks_for_dedup
        if t.context and t.context.get("finding_hash")
        and t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
    }
    # Also suppress tasks that have an active suppress_until field (set on user-cancelled low-priority tasks)
    now_iso = _utcnow().isoformat()
    for t in recent_tasks_for_dedup:
        if t.context and t.context.get("suppress_until") and t.context["suppress_until"] > now_iso:
            fhash = t.context.get("finding_hash")
            if fhash:
                recent_finding_hashes.add(fhash)
                failed_hashes.setdefault(fhash, "suppressed")

    # Query open PR artifacts to detect file-scope conflicts with in-flight work
    open_pr_stmt = (
        select(Artifact.pr_url, Artifact.logs, Task.context)
        .join(Task, Artifact.task_id == Task.id)
        .where(
            Task.project_id == task.project_id,
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
            Artifact.pr_url.isnot(None),
            Task.status.not_in([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
        )
        .limit(20)
    )
    open_pr_rows = (await session.execute(open_pr_stmt)).all()
    open_pr_files: set[str] = set()
    for _, _, task_ctx in open_pr_rows:
        if task_ctx:
            for f in (task_ctx.get('related_files') or []):
                open_pr_files.add(f)

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
            outcome = failed_hashes.get(fhash, "prior")
            logger.info(f"[RESEARCH] Skipping finding '{title}' — hash {fhash} matched {outcome} task")
            skipped_duplicates += 1
            continue
        if _is_duplicate(title, existing_titles):
            logger.warning(f"[RESEARCH] Skipping duplicate finding '{title}' — matches existing task")
            skipped_duplicates += 1
            continue
        # Skip if all related_files for this finding are already covered by open PRs
        finding_files = set(finding.get('related_files', []))
        if finding_files and finding_files.issubset(open_pr_files):
            logger.info(f"[RESEARCH] Skipping finding '{title}' — all related files already have open PRs")
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

        # Determine task type early — some findings don't spawn tasks
        finding_type = finding.get("type", "improvement")

        if finding_type == "design_doc_observation":
            # Append to design doc proposals for Phase 1 to review
            obs_project = await session.get(Project, task.project_id)
            if obs_project:
                doc = dict(obs_project.design_doc or {})
                proposals = doc.get("proposed_changes", [])
                proposals.append({
                    "id": f"pc-{uuid.uuid4().hex[:8]}",
                    "observation": finding.get("description", "")[:500],
                    "section": finding.get("section", "general"),
                    "proposed_by_task": str(task.id),
                    "proposed_at": _utcnow().isoformat(),
                    "status": "pending",
                })
                doc["proposed_changes"] = proposals[-30:]  # cap at 30 pending
                obs_project.design_doc = doc
                flag_modified(obs_project, "design_doc")
                session.add(obs_project)
            continue  # Don't create a task

        if finding_type == "direction_update":
            # Process as a direction modification proposal, not a task
            target_id = finding.get("direction_id")
            if not target_id:
                continue  # direction_update requires a target direction_id
            dir_project = await session.get(Project, task.project_id)
            if not dir_project:
                continue
            directions = list(dir_project.directions or [])
            for d in directions:
                if d.get("id") == target_id:
                    d["pending_update"] = {
                        "suggested_text": finding.get("suggested_text", d.get("text")),
                        "suggested_priority": finding.get("suggested_priority", d.get("priority")),
                        "suggested_paths": finding.get("suggested_paths", d.get("paths", [])),
                        "rationale": finding.get("description", "")[:300],
                        "proposed_at": _utcnow().isoformat(),
                        "proposed_by": str(task.id),
                    }
                    break
            dir_project.directions = directions
            flag_modified(dir_project, "directions")
            session.add(dir_project)
            continue  # Don't create a task for direction_update findings

        if not prompt:
            continue
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
        # Apply per-type model/effort overrides — take the higher of finding vs config
        _model_rank = {"claude-opus-4-6": 3, "claude-sonnet-4-6": 2, "claude-haiku-4-5-20251001": 1}
        _effort_rank = {"max": 4, "high": 3, "medium": 2, "low": 1}
        finding_model = finding.get('model')
        config_model = _runtime_config.get("task_type_models", {}).get(task_type)
        if finding_model and config_model:
            finding_model = finding_model if _model_rank.get(finding_model, 0) >= _model_rank.get(config_model, 0) else config_model
        elif config_model:
            finding_model = config_model
        finding_effort = finding.get('effort')
        config_effort = _runtime_config.get("task_type_efforts", {}).get(task_type)
        if finding_effort and config_effort:
            finding_effort = finding_effort if _effort_rank.get(finding_effort, 0) >= _effort_rank.get(config_effort, 0) else config_effort
        elif config_effort:
            finding_effort = config_effort

        related_files = finding.get("related_files", [])
        full_prompt = prompt + ("\n\nKey files to review or modify:\n" + "\n".join(f"- {f}" for f in related_files)) if related_files else prompt
        type_suffix = _runtime_config.get("agent_prompt_suffixes", {}).get(task_type)

        # For sub-research tasks, enrich prompt with parent context to avoid duplication
        if task_type == "research":
            sibling_titles = [
                f.get('title', '') for _, f, _ in deduplicated
                if f.get('title') and f != finding
            ][:5]
            if sibling_titles:
                parent_summary = (
                    '\n\nContext from parent research task: '
                    f'This sub-research is part of a broader investigation (task {str(task.id)[:8]}) '
                    f'that also produced these parallel findings:\n' +
                    '\n'.join(f'- {t}' for t in sibling_titles) +
                    '\nFocus your investigation on the specific area described above without duplicating these parallel efforts.'
                )
                full_prompt = full_prompt + parent_summary

        finding_priority = TASK_PRIORITY_MAP.get(finding.get("priority", "medium"), 50)
        # Inherit parent direction_id if the finding doesn't specify one
        parent_direction_id = (task.context or {}).get("direction_id")
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
                "parent_research_task_id": str(task.id),
                "finding_index": orig_i,
                "scope": scope,
                "risk": risk,
                "research_branch": research_branch,
                "original_base": original_base,
                "research_depth": research_depth + 1,
                "related_files": related_files,
                "finding_hash": fhash,
                **({"direction_id": direction_id} if direction_id else ({"direction_id": parent_direction_id} if parent_direction_id else {})),
                **({"parent_direction_id": parent_direction_id} if parent_direction_id else {}),
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

    # Log file overlap warnings for observability and surface them to agents
    overlaps = _log_file_overlaps(findings, "RESEARCH")
    for (i, j, shared_files) in overlaps:
        for fi in (i, j):
            ci = finding_to_child.get(fi)
            if ci is not None and ci < len(child_tasks):
                ctx = dict(child_tasks[ci].context or {})
                warnings = ctx.get('file_overlap_warnings', [])
                other_fi = j if fi == i else i
                other_title = findings[other_fi].get('title', f'#{other_fi}')
                warnings.append({'shared_files': sorted(shared_files), 'with_task': other_title})
                ctx['file_overlap_warnings'] = warnings
                child_tasks[ci].context = ctx
                session.add(child_tasks[ci])

    await session.commit()

    # Update last_researched_at for directions that produced findings.
    # Also update the current direction if all findings were deduped — prevents
    # the system from immediately re-researching and hitting the same duplicates.
    direction_ids_with_findings = set()
    for ct in child_tasks:
        did = (ct.context or {}).get("direction_id")
        if did:
            direction_ids_with_findings.add(did)
    # If all findings were deduped, still put the current direction on cooldown
    current_direction_id = (task.context or {}).get("direction_id")
    all_deduped = len(child_tasks) == 0 and skipped_duplicates > 0
    if all_deduped and current_direction_id:
        direction_ids_with_findings.add(current_direction_id)
    if direction_ids_with_findings:
        project = await session.get(Project, task.project_id)
        if project:
            directions = list(project.directions or [])
            now_iso = _utcnow().isoformat()
            for d in directions:
                if d.get("id") in direction_ids_with_findings:
                    d["last_researched_at"] = now_iso
            # Append to research_history for each direction that produced findings
            finding_titles_by_dir: dict = {}
            finding_descriptions_by_dir: dict = {}
            for orig_i, finding, fhash in deduplicated:
                did = finding.get("direction_id")
                if did:
                    finding_titles_by_dir.setdefault(did, []).append(
                        finding.get("title", f"Research finding #{orig_i+1}")
                    )
                    finding_descriptions_by_dir.setdefault(did, []).append(
                        (finding.get("description", finding.get("prompt", "")))[:200]
                    )
            for d in directions:
                did = d.get("id")
                if did in direction_ids_with_findings:
                    history = d.get("research_history", [])
                    dir_child_tasks = [ct for ct in child_tasks if (ct.context or {}).get("direction_id") == did]
                    entry = {
                        "timestamp": now_iso,
                        "task_id": str(task.id),
                        "tasks_created": len(dir_child_tasks),
                        "child_task_ids": [str(ct.id) for ct in dir_child_tasks],
                        "finding_titles": finding_titles_by_dir.get(did, [])[:5],
                        "key_finding_descriptions": finding_descriptions_by_dir.get(did, [])[:5],
                        "cycle_insights": [i["summary"][:200] for i in insights[:3]],
                    }
                    # Keep last 10 history entries; aggregate evicted entries to preserve lifetime stats
                    new_history = history + [entry]
                    if len(new_history) > 10:
                        evicted = new_history[:-10]
                        prev_agg = d.get("history_aggregate", {
                            "total_cycles": 0, "total_tasks_created": 0,
                            "total_prs_merged": 0, "total_tasks_failed": 0,
                            "total_tasks_cancelled": 0, "first_cycle_at": None,
                        })
                        for ev in evicted:
                            prev_agg["total_cycles"] += 1
                            prev_agg["total_tasks_created"] += ev.get("tasks_created", 0)
                            prev_agg["total_prs_merged"] += ev.get("prs_merged", 0)
                            prev_agg["total_tasks_failed"] += ev.get("tasks_failed", 0)
                            prev_agg["total_tasks_cancelled"] += ev.get("tasks_cancelled", 0)
                            if not prev_agg["first_cycle_at"] and ev.get("timestamp"):
                                prev_agg["first_cycle_at"] = ev["timestamp"]
                        d["history_aggregate"] = prev_agg
                    d["research_history"] = new_history[-10:]
                    d["possibly_saturated"] = _compute_saturation(d["research_history"])
                    d["research_context"] = _build_research_context(
                        prior_context=d.get("research_context"),
                        finding_titles=finding_titles_by_dir.get(did, [])[:5],
                        key_finding_descriptions=finding_descriptions_by_dir.get(did, [])[:5],
                        cycle_insights=[i["summary"][:200] for i in insights[:3]],
                        history_entry=entry,
                    )
            project.directions = directions
            flag_modified(project, "directions")
            session.add(project)

            # If this is a sub-research task (depth > 0), also annotate the parent direction's
            # most recent research_history entry with a note about sub-research findings.
            if research_depth > 0 and parent_research_task_id and direction_ids_with_findings:
                parent_project = await session.get(Project, task.project_id)
                if parent_project and parent_project.directions:
                    parent_dirs = list(parent_project.directions)
                    for d in parent_dirs:
                        did = d.get("id")
                        if did not in direction_ids_with_findings:
                            continue
                        history = d.get("research_history", [])
                        if history:
                            last_entry = history[-1]
                            sub_notes = last_entry.get("sub_research_notes", [])
                            sub_notes.append({
                                "sub_task_id": str(task.id),
                                "depth": research_depth,
                                "tasks_created": len(child_tasks),
                                "finding_titles": [f.get("title", "") for _, f, _ in deduplicated[:3]],
                                "insights": [i["summary"][:150] for i in insights[:2]],  # up to 2 insights, capped
                            })
                            last_entry["sub_research_notes"] = sub_notes[-5:]  # keep last 5
                            history[-1] = last_entry
                            d["research_history"] = history
                    parent_project.directions = parent_dirs
                    flag_modified(parent_project, "directions")
                    session.add(parent_project)

    # Process direction notes — append to the target direction's notes list
    if direction_notes:
        note_project = await session.get(Project, task.project_id)
        if note_project:
            note_directions = list(note_project.directions or [])
            changed = False
            for note_finding in direction_notes:
                target_id = note_finding.get("direction_id")
                raw_note = note_finding.get("description") or note_finding.get("prompt") or ""
                if len(raw_note) > STRATEGIC_RATIONALE_MAX:
                    logger.warning(f"[RESEARCH] Task {task.id}: truncating direction note {len(raw_note)}→{STRATEGIC_RATIONALE_MAX} chars for direction {target_id}")
                note_text = raw_note[:STRATEGIC_RATIONALE_MAX]
                if not note_text or not target_id:
                    continue
                for d in note_directions:
                    if d.get("id") == target_id:
                        notes = d.get("notes", [])
                        notes.append({
                            "text": note_text,
                            "added_at": _utcnow().isoformat(),
                            "added_by_task": str(task.id),
                        })
                        if len(notes) > STRATEGIC_NOTES_HISTORY_MAX:
                            logger.warning(f"[RESEARCH] Task {task.id}: dropping {len(notes) - STRATEGIC_NOTES_HISTORY_MAX} oldest notes on direction {target_id}")
                        d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
                        changed = True
                        break
            if changed:
                note_project.directions = note_directions
                flag_modified(note_project, "directions")
                session.add(note_project)

    # Process direction proposals — add to Project.directions with status='proposed'
    proposals_created = 0
    if direction_proposals:
        proposal_project = await session.get(Project, task.project_id)
        if proposal_project:
            proj_directions = list(proposal_project.directions or [])
            rejected_proposals = [d for d in proj_directions if d.get("status") == "rejected"]
            pending_count = sum(1 for d in proj_directions if d.get("status") == "proposed")
            max_proposals = 10
            now_iso = _utcnow().isoformat()
            for proposal in direction_proposals[:5]:  # max 5 per research call
                if pending_count >= max_proposals:
                    logger.info(f"[RESEARCH] Skipping direction proposal — max {max_proposals} pending proposals reached")
                    break
                proposal_text = (proposal.get("prompt") or proposal.get("description") or "")[:500]
                if not proposal_text.strip():
                    continue
                if _is_direction_duplicate(proposal_text, proj_directions, rejected_proposals):
                    logger.info(f"[RESEARCH] Skipping direction proposal — overlaps with existing direction")
                    continue
                new_direction = {
                    "id": f"d-{uuid.uuid4().hex[:8]}",
                    "text": proposal_text,
                    "priority": proposal.get("priority", "medium") if proposal.get("priority") in ("high", "medium", "low") else "medium",
                    "paths": [],
                    "added_at": now_iso,
                    "status": "proposed",
                    "proposed_by": str(task.id),
                    "proposal_rationale": (proposal.get("description") or "")[:500],
                    "proposed_at": now_iso,
                }
                proj_directions.append(new_direction)
                pending_count += 1
                proposals_created += 1
            if proposals_created:
                proposal_project.directions = proj_directions
                flag_modified(proposal_project, "directions")
                session.add(proposal_project)
                logger.info(f"[RESEARCH] Created {proposals_created} direction proposals from task {task.id}")

    # Store a human-readable research summary artifact
    if child_tasks:
        direction_texts = []
        payload0 = task.first_payload
        if payload0.get('directions'):
            direction_texts = [payload0['directions'][0].get('text', 'general improvement')]
        summary_lines = [
            f'# Research Summary',
            f'**Direction**: {direction_texts[0][:200] if direction_texts else "general"}',
            f'**Tasks Created**: {len(child_tasks)} ({skipped_duplicates} duplicates skipped)',
            f'',
            f'## Findings:',
        ]
        for ct in child_tasks[:10]:
            p = ct.first_payload
            summary_lines.append(f'- [{p.get("type", "?")}] {ct.title}: {(ct.description or "")[:150]}')
        summary = '\n'.join(summary_lines)

        summary_artifact = Artifact(
            task_id=task.id,
            artifact_type=ArtifactType.INVESTIGATION_REPORT,
            logs=summary,
        )
        session.add(summary_artifact)

    task.status = TaskStatus.EVALUATING
    direction_id = (task.context or {}).get("direction_id", "?")
    logger.info(
        f"[RESEARCH] Task {task.id}: {len(findings)} raw findings, "
        f"{skipped_duplicates} deduped, {len(child_tasks)} tasks created "
        f"(direction: {str(direction_id)[:8]})"
    )
    task.latest_status_message = (
        f"Research created {len(child_tasks)} tasks from {len(findings)} findings"
        + (f" ({skipped_duplicates} skipped as duplicates)" if skipped_duplicates else "")
        + (f", {proposals_created} direction proposals pending review" if proposals_created else "")
    )
    task.context = {**(task.context or {}), "research_branch": research_branch, "original_base": original_base}
    session.add(task)
    await session.commit()

    await broadcast_to_project(str(task.project_id), "autonomous_activity",
                               {"research_task_id": str(task.id), "tasks_created": len(child_tasks)})
    logger.info(f"[RESEARCH] Created {len(child_tasks)} tasks from research {task.id}")


async def _update_direction_research_outcomes(
    project_id: uuid.UUID, direction_id: str, research_task_id: str, session: AsyncSession
) -> None:
    """Update a direction's research_history entry with child task outcome counts.

    Called whenever a child task of a research cycle reaches a terminal status so
    that research_history entries accumulate live outcome statistics.
    """
    project = await session.get(Project, project_id)
    if not project:
        return
    directions = list(project.directions or [])
    updated = False
    for d in directions:
        if d.get("id") != direction_id:
            continue
        history = d.get("research_history", [])
        for entry in history:
            if entry.get("task_id") != research_task_id:
                continue
            child_ids = entry.get("child_task_ids", [])
            if not child_ids:
                break
            child_uuids = []
            for cid in child_ids:
                try:
                    child_uuids.append(uuid.UUID(cid))
                except ValueError:
                    pass
            if not child_uuids:
                break
            child_tasks_stmt = select(Task).where(Task.id.in_(child_uuids))
            child_tasks_rows = (await session.execute(child_tasks_stmt)).scalars().all()
            entry["tasks_completed"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.COMPLETED)
            entry["tasks_failed"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.FAILED)
            entry["tasks_cancelled"] = sum(1 for ct in child_tasks_rows if ct.status == TaskStatus.CANCELLED)
            entry["prs_merged"] = sum(
                1 for ct in child_tasks_rows
                if ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged")
            )
            # Capture failed task details so research agents can see WHY tasks failed
            failed_details = []
            for ct in child_tasks_rows:
                if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                    reason = (ct.latest_status_message or "")[:120]
                    failed_details.append({"title": ct.title or "", "reason": reason})
            entry["failed_task_details"] = failed_details[:5]  # cap at 5 to avoid bloat

            # Maintain research_context: promote proposed findings based on outcomes
            ctx = d.get("research_context", "")
            if ctx:
                parsed = _parse_research_context(ctx)
                for ct in child_tasks_rows:
                    title = ct.title or ""
                    if not title:
                        continue
                    if ct.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                        # Move matching proposed finding to Failed Approaches
                        reason = (ct.latest_status_message or "")[:120]
                        new_established = []
                        moved = False
                        for item in parsed["established"]:
                            if title.lower() in item.lower():
                                parsed["failed"].append(f"{title} — {reason}" if reason else title)
                                moved = True
                            else:
                                new_established.append(item)
                        if moved:
                            parsed["established"] = new_established
                    elif ct.status == TaskStatus.COMPLETED and (ct.context or {}).get("pr_merged"):
                        # Promote (proposed) finding to (confirmed)
                        new_established = []
                        for item in parsed["established"]:
                            if title.lower() in item.lower() and "(proposed)" in item:
                                new_established.append(item.replace("(proposed)", "(confirmed)"))
                            else:
                                new_established.append(item)
                        parsed["established"] = new_established

                # Reassemble the document (preserve eviction summary if present)
                parts: list = []
                if parsed.get("eviction_summary"):
                    parts.append("## Eviction Summary")
                    parts.append(parsed["eviction_summary"])
                    parts.append("")
                for header, key in [
                    ("## Established Findings", "established"),
                    ("## Failed Approaches", "failed"),
                    ("## Open Questions", "open_questions"),
                ]:
                    parts.append(header)
                    for item in parsed[key]:
                        parts.append(f"- {item}")
                    parts.append("")
                updated_ctx = "\n".join(parts).strip() + "\n"
                d["research_context"] = updated_ctx[:8000]

            updated = True
            break
        if updated:
            break
    if updated:
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()


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
        # Cache in context so partial-rollup path (which checks context.pr_merged directly)
        # doesn't see this task as "unmerged".
        task.context = {**(task.context or {}), "pr_merged": True}
        return True
    if artifact and artifact.pr_url:
        try:
            pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
            gh_token, owner, repo = await get_github_token_for_task(task.id, session)
            pr_data = await get_pull_request(gh_token, owner, repo, pr_number)
            if pr_data.get("merged"):
                # Update in-memory first so caller sees the change even if DB write fails
                task.context = {**(task.context or {}), "pr_merged": True}
                # Persist to DB in separate session (best-effort — may fail if caller
                # holds FOR UPDATE lock on this row, but in-memory update is sufficient
                # for the current rollup cycle)
                try:
                    async with AsyncSessionLocal() as write_session:
                        db_task = await write_session.get(Task, task.id)
                        if db_task:
                            db_task.context = {**(db_task.context or {}), "pr_merged": True}
                            write_session.add(db_task)
                            await write_session.commit()
                except Exception as write_err:
                    logger.warning(f"[PR_VERIFY] Failed to persist pr_merged for task {task.id}: {write_err}")
                return True
            # Detect closed-but-not-merged (rejected/abandoned PR) — don't block rollup forever
            if pr_data.get("state") == "closed" and not pr_data.get("merged"):
                logger.warning(f"[PR_VERIFY] Task {task.id} PR #{pr_number} was closed without merging — marking as rejected")
                # Update in-memory first
                task.context = {**(task.context or {}), "pr_closed_unmerged": True}
                task.status = TaskStatus.FAILED
                try:
                    async with AsyncSessionLocal() as write_session:
                        db_task = await write_session.get(Task, task.id)
                        if db_task:
                            db_task.context = {**(db_task.context or {}), "pr_closed_unmerged": True}
                            db_task.status = TaskStatus.FAILED
                            db_task.latest_status_message = f"PR #{pr_number} closed without merging"
                            write_session.add(db_task)
                            await _maybe_create_failure_alert(
                                db_task, write_session,
                                f"PR #{pr_number} was closed without merging"
                            )
                            await write_session.commit()
                except Exception as write_err:
                    logger.warning(f"[PR_VERIFY] Failed to persist pr_closed_unmerged for task {task.id}: {write_err}")
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
    is_parent = task.plan_output is not None or (task.first_payload).get("type") in ("research", "plan")
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
                if (c.first_payload).get("type", "create") in ROLLUP_TYPES]

    # Guard: if all children are review/fix types (none in ROLLUP_TYPES), mark parent COMPLETED
    if not children and all_children:
        parent.status = TaskStatus.COMPLETED
        parent.latest_status_message = "All subtasks were pipeline tasks (review/fix) — no rollup needed"
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.COMPLETED})
        logger.info(f"[ROLLUP] Parent {parent.id} completed — all children were review/fix pipeline tasks")
        return

    # Also check if any review/fix pipeline tasks are still active — don't rollup
    # while a review/fix cycle is in progress
    pipeline_active = any(
        c.status in (TaskStatus.QUEUED, TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS)
        for c in all_children
        if (c.first_payload).get("type") in ("review", "fix")
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

    # All children terminal but not all succeeded — finalize immediately instead of waiting 6 hours
    if not all_done and active_count == 0 and not pipeline_active:
        unmerged = [c for c in children
                    if c.status == TaskStatus.COMPLETED and not (c.context or {}).get("pr_merged")]
        failed = [c for c in children if c.status == TaskStatus.FAILED]
        completed_merged = [c for c in children
                           if c.status == TaskStatus.COMPLETED and (c.context or {}).get("pr_merged")]

        if unmerged:
            # Some completed but PRs not yet merged — wait for merge verification
            titles = [c.title[:40] for c in unmerged]
            parent.latest_status_message = f"Blocked: {len(unmerged)} subtask(s) completed but PR not merged — {', '.join(titles)}"
            logger.error(f"[ROLLUP] Parent {parent.id} blocked — {len(unmerged)} children completed but unmerged")
            session.add(parent)
            await session.commit()
            return

        # No unmerged PRs — only failed/cancelled/merged children remain
        if completed_merged:
            # Some succeeded — proceed with partial rollup of what worked
            logger.warning(f"[ROLLUP] Parent {parent.id} — {len(failed)} failed, {len(completed_merged)} succeeded. Rolling up successes.")
            all_done = True  # Let rollup proceed with merged children
            # Fall through to rollup logic below
        else:
            # Nothing succeeded — fail the parent immediately
            parent.status = TaskStatus.FAILED
            parent.latest_status_message = f"{len(failed)} of {len(children)} subtask(s) failed, none succeeded"
            session.add(parent)
            await _maybe_create_failure_alert(parent, session, f"{len(failed)} subtasks failed, none succeeded")
            await session.commit()
            await broadcast_to_project(str(parent.project_id), "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED})
            logger.error(f"[ROLLUP] Parent {parent.id} — all {len(failed)} children failed, marking parent FAILED")
            return

    child_statuses = [c.status for c in children]

    # Edge case: all children cancelled — nothing to rollup, mark parent cancelled
    if children and cancelled_count == len(children):
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
        failed_children = [c for c in children if c.status == TaskStatus.FAILED]
        rollup_note = ""
        if cancelled_count > 0:
            rollup_note += f"\n\n*{cancelled_count} subtask(s) were cancelled and excluded from this rollup.*"
        if failed_children:
            rollup_note += f"\n\n*{len(failed_children)} subtask(s) failed and were excluded from this rollup.*"

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

                # Check if any child task was high-risk — if so, require approval on the final merge
                has_high_risk_child = any(
                    (c.context or {}).get("risk") == "high"
                    for c in children
                    if c.status == TaskStatus.COMPLETED
                )
                if has_high_risk_child and not is_target_intermediate:
                    parent.requires_approval = True
                    parent.latest_status_message = (
                        f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting approval "
                        f"(contains high-risk changes)"
                    )
                    parent.context = {**parent_ctx, "rollup_pr_url": rollup_pr_url, "rollup_pr_number": rollup_pr_number}
                    session.add(parent)
                    await session.commit()
                    await broadcast_to_project(str(parent.project_id), "task_updated",
                                               {"task_id": str(parent.id), "status": parent.status})
                    logger.info(f"[ROLLUP] PR #{rollup_pr_number} has high-risk children — requires approval before merge to {target_branch}")
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
                err_str = str(e).lower()
                is_transient = any(kw in err_str for kw in [
                    "api.github.com", "502", "503", "connection", "timeout",
                    "rate limit", "ssl", "name resolution",
                ])
                rollup_retries = (parent.context or {}).get("_rollup_retries", 0) + 1
                if is_transient and rollup_retries <= 3:
                    # Transient GitHub error — leave parent EVALUATING, retry next cycle
                    parent.context = {**(parent.context or {}), "_rollup_retries": rollup_retries}
                    parent.latest_status_message = f"Rollup PR creation failed (attempt {rollup_retries}/3), will retry: {type(e).__name__}"
                    logger.warning(f"[ROLLUP] Transient error creating rollup PR (attempt {rollup_retries}): {e}")
                else:
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


async def _process_post_commit(
    task_id: uuid.UUID,
    payload: AgentStatusPayload,
    _session_factory=None,
) -> dict:
    """Post-commit processing: review pipeline, completion handlers, rollup, aggregation.

    Uses its own session to avoid holding the FOR UPDATE lock from the
    status endpoint while making GitHub API calls (which can hang 15-120s).
    _session_factory is injectable for tests (defaults to AsyncSessionLocal).
    """
    factory = _session_factory or AsyncSessionLocal
    async with factory() as session:
        task = (await session.execute(
            select(Task).where(Task.id == task_id)
        )).scalars().first()
        if not task:
            return {"status": "accepted"}

        # Immediately unblock dependents when task terminally fails (retries exhausted)
        if payload.status == TaskStatus.FAILED:
            try:
                async with AsyncSessionLocal() as dep_session:
                    dep_stmt = select(Task).where(
                        Task.project_id == task.project_id,
                        Task.status == TaskStatus.QUEUED,
                        Task.depends_on.contains([str(task.id)]),
                    )
                    dep_result = await dep_session.execute(dep_stmt)
                    waiting_tasks = dep_result.scalars().all()
                    count = 0
                    for wt in waiting_tasks:
                        wt.depends_on = [d for d in (wt.depends_on or []) if d != str(task.id)]
                        dep_session.add(wt)
                        count += 1
                    if count:
                        await dep_session.commit()
                        logger.info(f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks")
            except Exception as e:
                logger.error(f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}", exc_info=True)

        # 5. Review Pipeline — auto-spawn review/fix tasks based on completed task type
        if payload.status == TaskStatus.COMPLETED:
            task_payload = task.first_payload
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

                if is_intermediate and scope in ("small", "medium"):
                    # Small/medium scope on intermediate branch → security scan then auto-merge, skip review
                    # The rollup PR to main gets the full review — intermediate merges don't need it
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
                issues = []

                # Parse verdict: prefer structured verdict_json field, fall back to log parsing
                import re
                verdict_parsed = False
                verdict_unparseable = False

                # Method 1: verdict_json field (from /tmp/verdict.json on the agent)
                if payload.verdict_json:
                    try:
                        vj = json.loads(payload.verdict_json)
                        if vj.get("verdict"):
                            verdict = vj["verdict"]
                            feedback = vj.get("feedback", verdict_text)
                            issues = vj.get("issues", []) if isinstance(vj.get("issues"), list) else []
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
                                                issues = candidate.get("issues", []) if isinstance(candidate.get("issues"), list) else []
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
                    if approve_idx == -1 and rc_idx == -1:
                        verdict = "REQUEST_CHANGES"
                        feedback = "Review agent did not produce a verdict — manual review required"
                        logger.warning(f"[REVIEW PIPELINE] No verdict found for task {task.id} (len={len(verdict_text)}) — defaulting to REQUEST_CHANGES, marking for manual review")
                        verdict_unparseable = True
                    elif approve_idx > rc_idx:
                        verdict = "APPROVE"
                    elif rc_idx > approve_idx:
                        verdict = "REQUEST_CHANGES"
                    else:
                        verdict = "REQUEST_CHANGES"
                        feedback = "Review agent did not produce a verdict — manual review required"
                        logger.warning(f"[REVIEW PIPELINE] Ambiguous verdict for task {task.id} (len={len(verdict_text)}) — defaulting to REQUEST_CHANGES, marking for manual review")
                        verdict_unparseable = True

                # If verdict was unparseable, flag the original task for manual review
                if verdict_unparseable:
                    source_loop = (task.context or {}).get("review_loop", {})
                    original_task_id = source_loop.get("original_task_id")
                    if original_task_id:
                        try:
                            orig = await session.get(Task, uuid.UUID(original_task_id))
                            if orig:
                                orig.requires_approval = True
                                orig.latest_status_message = "Review agent did not produce a verdict — manual review required"
                                session.add(orig)
                                await session.commit()
                        except Exception as e:
                            logger.warning(f"[REVIEW PIPELINE] Failed to mark requires_approval on {original_task_id}: {e}", exc_info=True)

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
                                original_task_id=original_task_id,
                                fix_reason="ci_failure"
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
                                                        audit_parent.first_payload.get("type") == "security_audit" and
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
                    if issues:
                        critical = [i for i in issues if i.get('severity') == 'critical']
                        major = [i for i in issues if i.get('severity') == 'major']
                        minor = [i for i in issues if i.get('severity') == 'minor']
                        structured = ""
                        if critical:
                            structured += "\n\nCRITICAL issues (must fix):\n" + "\n".join(f"- {i['description']}" for i in critical)
                        if major:
                            structured += "\n\nMajor issues:\n" + "\n".join(f"- {i['description']}" for i in major)
                        if minor:
                            structured += "\n\nMinor issues:\n" + "\n".join(f"- {i['description']}" for i in minor)
                        fix_prompt_text = f"A code review requested changes. Address the following issues:{structured}{ci_errors_text}"
                    else:
                        fix_prompt_text = f"A code review requested changes. Please address the following feedback:\n\n{feedback}{ci_errors_text}"
                    fix_task = await _spawn_pipeline_task(
                        task, "fix", session, pr_number, branch, base_branch,
                        fix_prompt=fix_prompt_text,
                        original_prompt=original_prompt,
                        original_task_id=original_task_id,
                        current_feedback=feedback,
                        current_issues=issues
                    )
                    if fix_task:
                        logger.info(f"[REVIEW PIPELINE] Spawned fix for PR #{pr_number} → task {fix_task.id}")
                    else:
                        task.latest_status_message = f"Fix not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        cap_alert_title = f"PR #{pr_number} stalled — iteration cap reached"
                        existing_cap_alert = (await session.execute(
                            select(Alert).where(
                                Alert.project_id == task.project_id,
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title == cap_alert_title,
                            )
                        )).scalars().first()
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Review requested changes on PR #{pr_number} but the fix iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(str(task.project_id), "alert_created",
                                {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title})
                        else:
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

                fix_reason = review_loop.get("fix_reason", "code_review")
                if not scan_blocked and fix_reason == "ci_failure":
                    # CI-only fix: skip re-review, go straight to CI check + merge.
                    # The original review already approved the code logic.
                    ci_loop = review_loop.copy()
                    ci_loop["ci_waiting"] = True
                    ci_loop["ci_wait_started"] = _utcnow().isoformat()
                    ci_loop["ci_poll_interval"] = 10
                    task.status = TaskStatus.EVALUATING
                    task.context = {**(task.context or {}), "review_loop": ci_loop}
                    task.latest_status_message = f"CI-only fix applied for PR #{pr_number} — waiting for CI (review already approved)"
                    session.add(task)
                    await session.commit()
                    logger.info(f"[REVIEW PIPELINE] CI-only fix for PR #{pr_number} — skipping re-review, waiting for CI")
                elif not scan_blocked:
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
                        cap_alert_title = f"PR #{pr_number} stalled — iteration cap reached"
                        existing_cap_alert = (await session.execute(
                            select(Alert).where(
                                Alert.project_id == task.project_id,
                                Alert.severity == AlertSeverity.WARNING,
                                Alert.dismissed_at.is_(None),
                                Alert.title == cap_alert_title,
                            )
                        )).scalars().first()
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Fix was applied to PR #{pr_number} but the review iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(str(task.project_id), "alert_created",
                                {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title})
                        else:
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

                elif task_type == "design_doc_update":
                    doc_logs = payload.logs or task.latest_status_message or ""
                    await _handle_design_doc_completion(task, doc_logs, session)

                elif task_type == "strategic_review":
                    review_logs = payload.logs or task.latest_status_message or ""
                    await _handle_strategic_completion(task, review_logs, session)

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
                # Detect transient errors (GitHub API, DB, network) and requeue instead of FAILED
                err_str = str(handler_exc).lower()
                is_transient = any(kw in err_str for kw in [
                    "api.github.com", "502", "503", "connection", "timeout",
                    "rate limit", "ssl", "name resolution", "connection refused",
                ])
                if is_transient and task.attempt_count < _runtime_config["max_retries"]:
                    task.status = TaskStatus.QUEUED
                    task.attempt_count += 1
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    task.last_heartbeat = None
                    task.started_at = None
                    task.latest_status_message = f"Handler hit transient error, requeuing (attempt {task.attempt_count}): {type(handler_exc).__name__}"
                    logger.warning(f"[HANDLER] Transient error for {task.id}, requeuing: {handler_exc}")
                else:
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"Completed but handler failed: {type(handler_exc).__name__}: {str(handler_exc)[:200]}"
                    )
                session.add(task)

        # 6. Parent task rollup — delegated to shared function
        if task.parent_task_id and payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
            try:
                await _check_and_trigger_rollup(task, session)
            except Exception as e:
                logger.error(f"[ROLLUP] Rollup check failed for task {task.id}: {e!r}", exc_info=True)

        # 6b. Update direction research history outcomes for child tasks of a research cycle
        _task_ctx = task.context or {}
        if (
            _task_ctx.get("direction_id")
            and _task_ctx.get("research_task_id")
            and payload.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ):
            try:
                await _update_direction_research_outcomes(
                    task.project_id,
                    _task_ctx["direction_id"],
                    _task_ctx["research_task_id"],
                    session,
                )
            except Exception as _outcome_exc:
                logger.warning(
                    f"[RESEARCH] Failed to update direction research outcomes for task {task.id}: {_outcome_exc!r}"
                )

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


class DesignDocUpdate(BaseModel):
    """Incremental design doc page update from an agent."""
    task_id: uuid.UUID
    op: str  # "set", "delete", "list"
    key: Optional[str] = None
    value: Optional[Any] = None


@app.post("/api/agent/design-doc")
@limiter.limit("60/minute")
async def update_design_doc(
    request: Request,
    payload: DesignDocUpdate,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Agent endpoint for incremental design doc updates. Each call updates one page."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
    if token.get("task_type") not in ("design_doc_update", "strategic_review"):
        raise HTTPException(status_code=403, detail="Endpoint restricted to design_doc_update/strategic_review agents")

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if payload.op == "list":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        return {"keys": [k for k in doc.keys() if not k.startswith("_")]}

    if not payload.key or not isinstance(payload.key, str):
        raise HTTPException(status_code=400, detail="key is required for set/delete")
    key = payload.key.strip()
    if not key or len(key) > 100 or key.startswith("_"):
        raise HTTPException(status_code=400, detail="Invalid key (max 100 chars, no _ prefix)")

    # Lock project for atomic update
    result = await session.execute(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    project = result.scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    doc = dict(project.design_doc or {})
    meta = doc.get("_meta", {"revision": 0})

    if payload.op == "set":
        if payload.value is None:
            raise HTTPException(status_code=400, detail="value is required for set")
        # Size check: individual value max 50KB, total doc max 500KB
        import json as _json
        val_size = len(_json.dumps(payload.value))
        if val_size > 50_000:
            raise HTTPException(status_code=400, detail=f"Value too large ({val_size} bytes, max 50KB)")
        doc[key] = payload.value
        total_size = len(_json.dumps(doc))
        if total_size > 500_000:
            del doc[key]  # rollback
            raise HTTPException(status_code=400, detail=f"Total design doc too large ({total_size} bytes, max 500KB)")
    elif payload.op == "delete":
        doc.pop(key, None)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown op: {payload.op}")

    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by_task"] = str(task.id)
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    return {"status": "ok", "op": payload.op, "key": key, "revision": meta["revision"]}


# ── Agent Annotations ───────────────────────────────────────────────────
# Any agent can leave comments, issues, or vision suggestions.
# Stored as ProjectMessages so design-doc and strategic agents pick them up
# via the existing open_messages flow.

class AgentAnnotation(BaseModel):
    task_id: uuid.UUID
    annotation_type: Literal["comment", "issue", "vision"]
    content: str
    target_page: Optional[str] = None  # design doc page key (for comments)
    severity: Optional[str] = None     # low/medium/high (for issues)


_ANNOTATION_TYPE_MAP = {
    "comment": MessageType.NOTE,
    "issue": MessageType.BUG_REPORT,
    "vision": MessageType.FEEDBACK,
}


@app.post("/api/agent/annotate")
@limiter.limit("30/minute")
async def agent_annotate(
    request: Request,
    payload: AgentAnnotation,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Any agent can leave annotations — comments, issues, or vision suggestions."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    if len(payload.content) > 2000:
        raise HTTPException(status_code=400, detail="Content exceeds 2000 character limit")
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    if payload.severity and payload.severity not in ("low", "medium", "high"):
        raise HTTPException(status_code=400, detail="Severity must be low, medium, or high")
    if payload.target_page and len(payload.target_page) > 100:
        raise HTTPException(status_code=400, detail="Target page key too long")

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Cap: max 10 annotations per task
    count_stmt = select(_func.count()).select_from(ProjectMessage).where(
        ProjectMessage.from_task_id == task.id,
        ProjectMessage.from_type == "agent",
    )
    count = (await session.execute(count_stmt)).scalar() or 0
    if count >= 10:
        raise HTTPException(status_code=429, detail="Max 10 annotations per task")

    msg = ProjectMessage(
        project_id=task.project_id,
        message_type=_ANNOTATION_TYPE_MAP[payload.annotation_type],
        from_type="agent",
        from_task_id=task.id,
        content=payload.content.strip(),
        context={
            "annotation_type": payload.annotation_type,
            "task_type": token.get("task_type", ""),
            **({"target_page": payload.target_page} if payload.target_page else {}),
            **({"severity": payload.severity} if payload.severity else {}),
        },
    )
    session.add(msg)
    await session.commit()

    logger.info(f"[ANNOTATE] {payload.annotation_type} from task {task.id} ({token.get('task_type', '?')})")
    return {"status": "ok", "annotation_id": str(msg.id)}


class StrategicOp(BaseModel):
    """Incremental strategic review operation from an agent."""
    task_id: uuid.UUID
    op: str  # "set_vision", "create_direction", "update_direction", "retire_direction", "create_message", "resolve_message"
    data: dict = {}


@app.post("/api/agent/strategic")
@limiter.limit("60/minute")
async def strategic_operation(
    request: Request,
    payload: StrategicOp,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Agent endpoint for incremental strategic review operations."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
    if token.get("task_type") not in ("strategic_review",):
        raise HTTPException(status_code=403, detail="Endpoint restricted to strategic_review agents")

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Stamp task so the completion handler knows the API was used
    if not (task.context or {}).get("_strategic_api_used"):
        task.context = {**(task.context or {}), "_strategic_api_used": True}
        flag_modified(task, "context")
        session.add(task)

    # Lock project for atomic update
    result = await session.execute(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    project = result.scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    data = payload.data
    warnings: List[str] = []

    # Size guard — prevent agents from bloating project JSONB columns
    if len(json.dumps(data, default=str)) > 100_000:
        raise HTTPException(status_code=400, detail="Data payload too large (max 100KB)")

    def _resp(body: dict) -> dict:
        if warnings:
            body["warnings"] = list(warnings)
        return body

    if payload.op == "set_vision":
        if not isinstance(data, dict) or not data.get("purpose"):
            raise HTTPException(status_code=400, detail="Vision must have a 'purpose' field")
        current = dict(project.vision or {})
        old_rev = current.get("revision", 0)
        # Merge: keep _strategic_failures if present, add metadata
        new_vision = dict(data)
        new_vision["revision"] = (old_rev or 0) + 1
        new_vision["last_updated"] = _utcnow().isoformat()
        new_vision.pop("_strategic_failures", None)  # Reset on success
        project.vision = new_vision
        flag_modified(project, "vision")
        session.add(project)
        await session.commit()
        logger.info(f"[STRATEGIC API] Vision updated for project {project.id} (rev {new_vision['revision']})")
        return _resp({"status": "ok", "op": "set_vision", "revision": new_vision["revision"]})

    elif payload.op == "create_direction":
        raw_text = str(data.get("text", "")).strip()
        if not raw_text:
            raise HTTPException(status_code=400, detail="Direction text is required")
        text = _cap_str(raw_text, STRATEGIC_DIRECTION_TEXT_MAX, "text", warnings)
        raw_priority = data.get("priority", "medium")
        priority = raw_priority if raw_priority in ("high", "medium", "low") else "medium"
        rationale = _cap_str(data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings)
        directions = list(project.directions or [])
        # Dedup: skip if same text exists
        if any(d.get("text", "").strip().lower() == text.lower() for d in directions if d.get("status") in ("active", "proposed")):
            return _resp({"status": "skipped", "reason": "duplicate"})
        new_dir: dict = {
            "id": f"d-{uuid.uuid4().hex[:8]}",
            "text": text,
            "status": "active",
            "priority": priority,
            "notes": [{"text": f"Created by strategic review: {rationale}", "added_at": _utcnow().isoformat()}] if rationale else [],
            "research_history": [],
        }
        if data.get("system_id"):
            new_dir["system_id"] = _cap_str(data["system_id"], STRATEGIC_SYSTEM_ID_MAX, "system_id", warnings)
        if data.get("paths"):
            new_dir["paths"] = data["paths"]
        directions.append(new_dir)
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
        logger.info(f"[STRATEGIC API] Created direction '{text[:60]}' for project {project.id}")
        return _resp({"status": "ok", "op": "create_direction", "direction_id": new_dir["id"]})

    elif payload.op == "update_direction":
        dir_id = data.get("direction_id")
        changes = data.get("changes", {})
        if not dir_id or not changes:
            raise HTTPException(status_code=400, detail="direction_id and changes required")
        rationale = _cap_str(data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings)
        directions = list(project.directions or [])
        updated = False
        for d in directions:
            if d.get("id") == dir_id:
                if "text" in changes:
                    d["text"] = _cap_str(changes["text"], STRATEGIC_DIRECTION_TEXT_MAX, "changes.text", warnings)
                if "priority" in changes and changes["priority"] in ("high", "medium", "low"):
                    d["priority"] = changes["priority"]
                if "system_id" in changes:
                    d["system_id"] = _cap_str(changes["system_id"], STRATEGIC_SYSTEM_ID_MAX, "changes.system_id", warnings) if changes["system_id"] else None
                if "status" in changes and changes["status"] in ("active", "paused", "expired"):
                    if changes["status"] == "active" and d.get("status") == "paused" and not d.get("auto_paused"):
                        return _resp({"status": "blocked", "reason": "Cannot reactivate user-paused direction"})
                    d["status"] = changes["status"]
                if rationale:
                    notes = d.get("notes", [])
                    notes.append({"text": f"Strategic update: {rationale}", "added_at": _utcnow().isoformat()})
                    d["notes"] = _cap_list(notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings)
                updated = True
                break
        if not updated:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
        logger.info(f"[STRATEGIC API] Updated direction {dir_id}")
        return _resp({"status": "ok", "op": "update_direction", "direction_id": dir_id})

    elif payload.op == "retire_direction":
        dir_id = data.get("direction_id")
        if not dir_id:
            raise HTTPException(status_code=400, detail="direction_id required")
        rationale = _cap_str(data.get("rationale", ""), STRATEGIC_RATIONALE_MAX, "rationale", warnings)
        directions = list(project.directions or [])
        retired = False
        for d in directions:
            if d.get("id") == dir_id:
                d["status"] = "expired"
                notes = d.get("notes", [])
                notes.append({"text": f"Retired by strategic review: {rationale}", "added_at": _utcnow().isoformat()})
                d["notes"] = _cap_list(notes, STRATEGIC_NOTES_HISTORY_MAX, "notes", warnings)
                retired = True
                break
        if not retired:
            raise HTTPException(status_code=404, detail=f"Direction {dir_id} not found")
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        await session.commit()
        logger.info(f"[STRATEGIC API] Retired direction {dir_id}")
        return _resp({"status": "ok", "op": "retire_direction", "direction_id": dir_id})

    elif payload.op == "create_message":
        raw_content = str(data.get("content", "")).strip()
        if not raw_content:
            raise HTTPException(status_code=400, detail="Message content required")
        content = _cap_str(raw_content, STRATEGIC_MESSAGE_CONTENT_MAX, "content", warnings)
        try:
            msg_type = MessageType(str(data.get("type", "NOTE")).upper())
        except ValueError:
            msg_type = MessageType.NOTE
        new_msg = ProjectMessage(
            project_id=project.id,
            message_type=msg_type,
            from_type="strategic",
            from_task_id=task.id,
            content=content,
            context=data.get("context") or {},
        )
        session.add(new_msg)
        await session.commit()
        return _resp({"status": "ok", "op": "create_message", "message_id": str(new_msg.id)})

    elif payload.op == "resolve_message":
        msg_id = data.get("message_id")
        if not msg_id:
            raise HTTPException(status_code=400, detail="message_id required")
        try:
            msg = await session.get(ProjectMessage, uuid.UUID(str(msg_id)))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid message_id")
        if not msg or msg.project_id != project.id:
            raise HTTPException(status_code=404, detail="Message not found")
        if msg.status == "resolved":
            return _resp({"status": "already_resolved"})
        msg.status = "resolved"
        resolution_raw = data.get("resolution", "Addressed by strategic review")
        msg.resolution = _cap_str(resolution_raw, STRATEGIC_RESOLUTION_MAX, "resolution", warnings)
        msg.resolved_at = _utcnow()
        session.add(msg)
        await session.commit()
        return _resp({"status": "ok", "op": "resolve_message", "message_id": str(msg.id)})

    else:
        raise HTTPException(status_code=400, detail=f"Unknown op: {payload.op}")


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
                        base_branch = (task.first_payload).get("base_branch", "main")
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
                base_branch = (task.first_payload).get("base_branch", "main")
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
        # Wrapped in try-except so slot release (below) always runs even if
        # classify_failure() or record_task_result() throw.
        try:
            if payload.status == TaskStatus.COMPLETED:
                record_task_result(success=True, task_id=str(task.id))
                _shared_state.tasks_completed_total += 1
            else:
                failure_class = classify_failure(task, payload.logs)
                record_task_result(success=False, failure_class=failure_class, task_id=str(task.id))
                _shared_state.tasks_failed_total += 1

                # Rate-limited or transient API failures: requeue instead of permanently failing
                if failure_class in ("RATE_LIMIT", "EXTERNAL_API"):
                    acct_id = (task.context or {}).get("max_plan_account_id")
                    model = _extract_task_model(task)
                    if acct_id:
                        await release_max_plan_account(acct_id)
                        # Mark slot as released so the fallback release (line ~6232) doesn't double-release
                        task.context = {**(task.context or {}), "_slot_released": True}
                        if failure_class == "RATE_LIMIT":
                            # Default to sonnet when model is unknown (agent's default model)
                            rl_model = model or "claude-sonnet-4-6"
                            # Disable only the specific model on this account
                            async with _max_plan_pool_lock:
                                acct = _account_by_id.get(acct_id)
                                if acct:
                                    dm = acct.setdefault("disabled_models", {})
                                    info = dm.setdefault(rl_model, {"hits": 0})
                                    info["hits"] += 1
                                    info["at"] = _utcnow()
                                    cooldown = min(30 * (2 ** (info["hits"] - 1)), 240)
                                    cooldown = cooldown * random.uniform(0.85, 1.15)  # ±15% jitter
                                    logger.warning(f"[POOL] Account {acct['name']} model {rl_model} disabled (hit #{info['hits']}, cooldown {cooldown:.0f}min)")
                                # Persist rate-limit state to DB so it survives deploys
                                if acct:
                                    try:
                                        async with AsyncSessionLocal() as rl_session:
                                            db_acct = await rl_session.get(MaxPlanAccount, uuid.UUID(acct_id))
                                            if db_acct:
                                                dm = acct.get("disabled_models", {})
                                                db_acct.rate_limit_state = {
                                                    "disabled_models": {
                                                        m: {"hits": i["hits"], "at": i["at"].isoformat()}
                                                        for m, i in dm.items()
                                                    }
                                                } if dm else None
                                                db_acct.enabled = acct.get("enabled", True)
                                                rl_session.add(db_acct)
                                                await rl_session.commit()
                                    except Exception as e:
                                        logger.warning(f"[POOL] Failed to persist rate-limit state for {acct_id}: {e} — queued for retry")
                                        if acct_id not in _pending_rl_persists:
                                            _pending_rl_persists.append(acct_id)
                        # EXTERNAL_API: don't disable account (transient outage)

                    # Track API failure retries to prevent infinite loops
                    ctx = dict(task.context or {})
                    # Blacklist the account that just hit rate limit so retry picks a different one
                    acct_id_for_blacklist = (task.context or {}).get("max_plan_account_id")
                    if acct_id_for_blacklist and failure_class == "RATE_LIMIT":
                        blacklisted = list(ctx.get("blacklisted_accounts") or [])
                        if acct_id_for_blacklist not in blacklisted:
                            blacklisted.append(acct_id_for_blacklist)
                        ctx["blacklisted_accounts"] = blacklisted
                    should_requeue = True
                    if failure_class == "EXTERNAL_API":
                        api_retries = ctx.get("_api_failure_retries", 0) + 1
                        ctx["_api_failure_retries"] = api_retries
                        if api_retries > 3:
                            logger.warning(f"[AGENT STATUS] Task {task.id} hit {api_retries} EXTERNAL_API failures — not requeuing")
                            should_requeue = False

                    if should_requeue:
                        old_vm_id_rl = task.vm_instance_id  # Save BEFORE reassignment
                        task.status = TaskStatus.QUEUED
                        task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        task.last_heartbeat = None
                        task.started_at = None
                        task.latest_status_message = f"{failure_class.replace('_', ' ').title()} — requeued"
                        # Clear stale state so dispatcher picks it up fresh
                        ctx.pop("retry_after", None)
                        ctx.pop("max_plan_account_id", None)
                        ctx.pop("_slot_released", None)
                        task.context = ctx
                        session.add(task)
                        await session.commit()
                        await broadcast_to_project(str(task.project_id), "task_updated",
                            {"task_id": str(task.id), "status": TaskStatus.QUEUED})
                        logger.info(f"[AGENT STATUS] Task {task.id} {failure_class} — requeued without burning retry")
                        await _ensure_vm_deleted(old_vm_id_rl, reason="rate_limit_requeue")
                        return {"status": "accepted"}
        except Exception as e:
            logger.error(f"[STATUS] Exception during result recording for task {task.id}: {e!r}", exc_info=True)

        # Release Max plan account slot (idempotent — skip if already released by reaper)
        # Runs regardless of whether result recording above succeeded or threw.
        acct_id = (task.context or {}).get("max_plan_account_id")
        if acct_id and not (task.context or {}).get("_slot_released"):
            await release_max_plan_account(acct_id)
            task.context = {**(task.context or {}), "_slot_released": True}

        task.vm_stopped_at = _utcnow()
        old_vm_id = task.vm_instance_id
        await _ensure_vm_deleted(old_vm_id, reason="agent_completion")

        # Retry logic for failed tasks
        if payload.status == TaskStatus.FAILED and task.attempt_count < _runtime_config["max_retries"]:
            task.attempt_count += 1
            task.status = TaskStatus.QUEUED
            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
            task.last_heartbeat = None
            task.started_at = None  # Fresh timeout window for the new VM
            # Add retry delay for transient failures to avoid hammering exhausted resources
            ctx = dict(task.context or {})
            if failure_class == "RATE_LIMIT":
                # Track per-account rate-limit hit count for exponential backoff
                rl_counts = dict(ctx.get("rate_limit_counts") or {})
                if acct_id:
                    rl_counts[str(acct_id)] = rl_counts.get(str(acct_id), 0) + 1
                ctx["rate_limit_counts"] = rl_counts

                rl_hit_count = rl_counts.get(str(acct_id), 1) if acct_id else 1
                # Exponential backoff: 30min * 2^(n-1), capped at 4 hours, ±20% jitter
                base_delay_minutes = min(30 * (2 ** (rl_hit_count - 1)), 240)
                delay_minutes = base_delay_minutes * random.uniform(0.8, 1.2)
                retry_after = (_utcnow() + timedelta(minutes=delay_minutes)).isoformat()
                ctx["retry_after"] = retry_after
                logger.info(f"[ORCHESTRATOR] Task {task.id} rate-limited (hit #{rl_hit_count}) — holding back {delay_minutes:.0f}min (retry_after={retry_after})")
                # Blacklist rate-limited account so the retry picks a different one
                if acct_id:
                    blacklisted = list(ctx.get("blacklisted_accounts") or [])
                    if acct_id not in blacklisted:
                        blacklisted.append(acct_id)
                    ctx["blacklisted_accounts"] = blacklisted
            elif failure_class == "PROVISION_FAILURE":
                delay_minutes = 5 * random.uniform(0.75, 1.25)  # 3.75-6.25 minutes
                retry_after = (_utcnow() + timedelta(minutes=delay_minutes)).isoformat()
                ctx["retry_after"] = retry_after
                logger.info(f"[ORCHESTRATOR] Task {task.id} provision failure — holding back {delay_minutes:.1f}min (retry_after={retry_after})")
            else:
                ctx.pop("retry_after", None)  # Clear any stale retry_after on non-delayed retries
            task.context = ctx
            session.add(task)

            await session.commit()

            await broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {"task_id": str(task.id), "status": TaskStatus.QUEUED, "attempt_count": task.attempt_count}
            )
            return {"status": "retrying", "message": f"Task {task.id} failed, retrying (Attempt {task.attempt_count}/3)"}

        else:
            # Retries exhausted — annotate the final status message with context
            if payload.status == TaskStatus.FAILED:
                max_r = _runtime_config["max_retries"]
                existing_msg = task.latest_status_message or ""
                if "retries exhausted" not in existing_msg:
                    task.latest_status_message = (
                        f"[{failure_class}] Retries exhausted ({task.attempt_count}/{max_r}). "
                        + existing_msg[:300]
                    ).strip()
                    session.add(task)

        # Spawn a debug task for permanently failed tasks (retries exhausted)
        failed_task_payload = task.first_payload
        failed_task_type = failed_task_payload.get("type", "")
        should_auto_debug = (
            payload.status == TaskStatus.FAILED and  # Only for failed tasks (failure_class is unset for COMPLETED)
            failed_task_type != "debug" and  # Don't recursively debug debug tasks
            (task.context or {}).get("debug_task_id") is None and  # Don't re-debug fix tasks spawned by debug
            payload.logs and
            failure_class == "CODE_ERROR" and  # Only debug actual code errors, not infra/timeout/rate-limit
            (
                (task.context or {}).get("auto_debug") is True or
                task.parent_task_id is not None or  # Part of a pipeline (plan/research subtask)
                task.created_by == "ORCHESTRATOR"  # Autonomous research/QA/security audit tasks
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
                    "failed_task_prompt": (failed_task_payload.get("prompt") or task.description or "")[:2000],
                    "failed_task_type": failed_task_type,
                }],
                context={
                    "failed_task_id": str(task.id),
                    "parent_base": base_branch,
                    "related_files": (task.context or {}).get("related_files", []),
                    "scope": (task.context or {}).get("scope"),
                    "failure_class": failure_class,
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
        # Only count code errors toward the breaker — rate limits and provision failures aren't code quality issues
        _cb_failure_class = classify_failure(task, payload.logs)
        if payload.status == TaskStatus.FAILED and task.created_by == "ORCHESTRATOR" and _cb_failure_class not in ("RATE_LIMIT", "PROVISION_FAILURE"):
            project_key = str(task.project_id)
            breaker = _shared_state.get_circuit_breaker(project_key)
            # Only CODE_ERROR and AGENT_TIMEOUT indicate code regressions; infra/rate-limit
            # failures are transient and should not trip the breaker.
            is_code_failure = failure_class in ("CODE_ERROR", "AGENT_TIMEOUT")
            if is_code_failure:
                breaker["failure_count"] += 1
                breaker["success_count"] = 0

            # HALF-OPEN test task failed → back to OPEN with doubled cooldown
            # (any failure on the test task re-opens, including infra failures)
            if breaker["state"] == "HALF_OPEN" and str(task.id) == breaker.get("half_open_task_id"):
                breaker["state"] = "OPEN"
                breaker["cooldown_seconds"] = min(breaker["cooldown_seconds"] * 2, 7200)  # cap at 2 hours
                breaker["opened_at"] = _utcnow()
                breaker["half_open_task_id"] = None
                logger.warning(f"[CIRCUIT BREAKER] HALF-OPEN → OPEN for {project_key} (cooldown doubled to {breaker['cooldown_seconds']}s)")
                await _maybe_create_circuit_breaker_alert(
                    task.project_id, session,
                    f"HALF-OPEN test task failed — circuit breaker re-opened (cooldown: {breaker['cooldown_seconds']}s)"
                )

            # CLOSED → OPEN after 3 consecutive failures
            elif breaker["state"] == "CLOSED" and breaker["failure_count"] >= 3:
                breaker["state"] = "OPEN"
                breaker["opened_at"] = _utcnow()
                breaker["cooldown_seconds"] = breaker["base_cooldown"]
                await broadcast_to_project(project_key, "autonomous_paused",
                                           {"reason": f"Circuit breaker OPEN after {breaker['failure_count']} consecutive failures"})
                logger.warning(f"[CIRCUIT BREAKER] CLOSED → OPEN for {project_key} after {breaker['failure_count']} failures")
                await _maybe_create_circuit_breaker_alert(
                    task.project_id, session,
                    f"Circuit breaker opened after {breaker['failure_count']} consecutive failures (cooldown: {breaker['cooldown_seconds']}s)"
                )
            # Persist breaker state on any transition
            if breaker["state"] != "CLOSED" or breaker["failure_count"] == 0:
                await _orch._persist_circuit_breakers()
        # When review/fix fails permanently, cancel the original create task so rollup
        # skips it (via cancelled-child logic) instead of hanging forever.
        # Other children's work is preserved — only this PR is excluded from the rollup.
        if payload.status == TaskStatus.FAILED:
            task_payload = task.first_payload
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
                await _orch._persist_circuit_breakers()

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

        # Pass a session factory bound to the SAME engine the request used.
        # In production this is AsyncSessionLocal; in tests it's the test engine.
        # This ensures the helper's fresh session doesn't bypass test DB overrides.
        _bind = session.bind
        _factory = async_sessionmaker(_bind, class_=AsyncSession, expire_on_commit=False)
        return await _process_post_commit(task.id, payload, _session_factory=_factory)


# _github_token_cache, invalidate_github_token_cache, get_github_token_for_task,
# and GitHub proxy endpoints — moved to app.routers.agent

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
