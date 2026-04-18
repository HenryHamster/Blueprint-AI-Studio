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
from app.routers.admin_projects import router as admin_projects_router
from app.routers.admin_settings import router as admin_settings_router
from app.routers.agent import router as agent_router
from app.routers.agent import verify_agent_token, get_github_token_for_task, invalidate_github_token_cache, _github_token_cache, _has_local_cycle
from app.routers.github import router as github_router
from app.routers.projects import router as projects_router
from app.routers.directions import router as directions_router
from app.routers.deploy import router as deploy_router
from app.routers.admin_users import router as admin_users_router
from app.routers.admin_notifications import router as admin_notifications_router
from app.routers.admin_analytics import router as admin_analytics_router
from app.routers.admin_orchestrator_errors import router as admin_orchestrator_errors_router
from app.routers.admin_scope_measurement import router as admin_scope_measurement_router
from app.routers.agent_status import router as agent_status_router
from app.routers.admin_onboard import router as admin_onboard_router
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
    close_github_client,
)
from app.shared_state import limiter
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app import orchestrator as _orch
from app.lifecycle import lifespan
from app.task_lifecycle import _process_post_commit
from app.rollup_management import _check_and_trigger_rollup
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

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

from app.strategic_constants import (  # noqa: E402
    STRATEGIC_DIRECTION_TEXT_MAX,
    STRATEGIC_RATIONALE_MAX,
    STRATEGIC_SYSTEM_ID_MAX,
    STRATEGIC_NOTES_HISTORY_MAX,
    STRATEGIC_MESSAGE_CONTENT_MAX,
    STRATEGIC_RESOLUTION_MAX,
    STATUS_LATEST_MESSAGE_MAX,
)

# Expected schema revision — must match the latest alembic migration.
# Update this when adding new migrations.
EXPECTED_SCHEMA_REVISION = "0035"

# --- Task Priority Map ---
# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}

# GCP VM count cache — updated by cleanup service, used by orchestrator concurrency check
_last_gcp_vm_count: int = 0
_last_gcp_vm_check: float = 0.0
_GCP_VM_CHECK_INTERVAL: float = 120.0  # Seconds between GCP VM count refreshes

# --- Max Plan Account Pool ---
# Pool state and logic extracted to max_plan_pool.py. Re-exported here for
# backward compatibility with callers that import from app.main.
from app.max_plan_pool import (  # noqa: E402
    _max_plan_pools,
    _account_by_id,
    _max_plan_pool_lock,
    _pending_rl_persists,
    RATE_LIMIT_PATTERNS,
    _all_accounts,
    load_max_plan_pool,
    _account_available_for_model,
    _extract_task_model,
    select_max_plan_account,
    release_max_plan_account,
    recheck_disabled_accounts,
)

# --- Deployment State Management ---

# Current revision from Cloud Run (set on startup)
_current_revision: str = os.getenv("K_REVISION", "local-dev")
_current_sha: str = os.getenv("DEPLOY_SHA", "unknown")

async def _ensure_vm_deleted(task_or_vm_id, *, reason: str = "terminal_transition") -> bool:
    """Guarantee VM deletion for any terminal state transition.

    Accepts either a Task object or a raw vm_instance_id string.
    Retries up to 3 times with exponential backoff.
    Returns True if deletion succeeded or was unnecessary.
    """
    global _last_gcp_vm_count

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
                if _last_gcp_vm_count > 0:
                    _last_gcp_vm_count -= 1
                    logger.info(f"[VM CLEANUP] Decremented cached VM count to {_last_gcp_vm_count}")
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

# Webhook dedup state moved to app.routers.github

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
                             "autonomous_periodic_tasks", "health_check_urls",
                             "notification_config"):
                    if key in config_json:
                        _runtime_config[key] = config_json[key]
                if "strategic_research_frequency" in config_json:
                    _runtime_config["strategic_research_frequency"] = int(config_json["strategic_research_frequency"])
                if "require_approval_gates" in config_json:
                    _runtime_config["require_approval_gates"] = bool(config_json["require_approval_gates"])
                if "daily_cost_ceiling_usd" in config_json:
                    _runtime_config["daily_cost_ceiling_usd"] = float(config_json["daily_cost_ceiling_usd"])
                for int_key in ("max_provision_retries", "max_rollup_retries",
                                "architecture_review_frequency", "max_active_directions",
                                "max_parallel_research_per_project"):
                    if int_key in config_json:
                        _runtime_config[int_key] = int(config_json[int_key])
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
                                    if k == "cooldown_expires_at":
                                        fallback = datetime.now(tz.utc) + timedelta(minutes=30)
                                        logger.warning(f"[DEPLOY] Corrupted cooldown_expires_at for zone {zone} ({v!r}); failing safe to {fallback.isoformat()}")
                                        restored[k] = fallback
                                    elif k == "last_failure":
                                        fallback = datetime.now(tz.utc)
                                        logger.warning(f"[DEPLOY] Corrupted last_failure for zone {zone} ({v!r}); failing safe to {fallback.isoformat()}")
                                        restored[k] = fallback
                                    else:
                                        logger.warning(f"[DEPLOY] Skipping corrupted zone health key {k} for zone {zone}: {v!r}")
                                        continue
                            elif k == "recent_outcomes" and isinstance(v, list):
                                from collections import deque as _deque
                                from app.compute import ZONE_HISTORY_WINDOW
                                valid = [e for e in v if isinstance(e, bool)]
                                if len(valid) < len(v):
                                    logger.warning(f"[DEPLOY] Filtered {len(v) - len(valid)} non-boolean element(s) from recent_outcomes for zone {zone}")
                                restored[k] = _deque(valid[-ZONE_HISTORY_WINDOW:], maxlen=ZONE_HISTORY_WINDOW)
                            elif k == "consecutive_failures":
                                if isinstance(v, int):
                                    restored[k] = v
                                else:
                                    logger.warning(f"[DEPLOY] consecutive_failures for zone {zone} is not an int ({v!r}), defaulting to 0")
                                    restored[k] = 0
                            elif k == "last_failure_reason":
                                if v is None or isinstance(v, str):
                                    restored[k] = v
                                else:
                                    logger.warning(f"[DEPLOY] last_failure_reason for zone {zone} is not a string ({v!r}), defaulting to None")
                                    restored[k] = None
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
                import app.deploy_health as _dh
                _dh._restore_health_streaks(state.zone_health_state)
                if _dh._pause_failure_streak or _dh._health_recovery_history or _dh._last_auto_resume_at:
                    logger.info(
                        f"[DEPLOY] Restored health streaks: pause_failures={_dh._pause_failure_streak}, "
                        f"recovery_history={list(_dh._health_recovery_history)}, "
                        f"last_auto_resume_at={_dh._last_auto_resume_at}"
                    )
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore health streaks: {e}", exc_info=True)

        # Restore persistent anomaly state — preserves occurrence counts across restarts
        try:
            pstate = getattr(state, 'persistent_anomaly_state', None)
            if pstate and isinstance(pstate, dict):
                raw_anomalies = pstate.get("persistent_anomalies", {})
                for proj_id, proj_anomalies in raw_anomalies.items():
                    restored_proj: dict = {}
                    for k, v in proj_anomalies.items():
                        entry: dict = {}
                        for fk, fv in v.items():
                            # first_seen/last_seen are ISO strings everywhere they're
                            # written and compared (anomaly_detection.py). Coerce any
                            # pre-existing datetime back to isoformat so legacy
                            # persisted pods don't crash the stale-cutoff comparison.
                            if fk in ("first_seen", "last_seen") and isinstance(fv, datetime):
                                entry[fk] = fv.isoformat()
                            else:
                                entry[fk] = fv
                        restored_proj[k] = entry
                    _shared_state.persistent_anomalies[proj_id] = restored_proj
                raw_adjustments = pstate.get("anomaly_adjustments", {})
                if raw_adjustments:
                    _shared_state.anomaly_adjustments.update(raw_adjustments)
                # Re-apply active adjustments so max_concurrent_vms stays reduced
                if _shared_state.anomaly_adjustments.get("active"):
                    adj = _shared_state.anomaly_adjustments
                    pre_vms = adj.get("pre_adjustment_max_vms")
                    if pre_vms is not None:
                        _shared_state.runtime_config["max_concurrent_vms"] = max(2, pre_vms // 2)
                    logger.info(
                        "[DEPLOY] Re-applied active anomaly adjustments: "
                        f"max_vms={_shared_state.runtime_config['max_concurrent_vms']}, "
                        f"failure_gate={adj.get('failure_gate_threshold')}"
                    )
                logger.info(f"[DEPLOY] Restored anomaly state for {len(raw_anomalies)} projects")
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore anomaly state: {e}", exc_info=True)

        # Restore direction failure streaks — holds survive restarts silently
        try:
            dstate = getattr(state, 'direction_streak_state', None)
            if dstate and isinstance(dstate, dict):
                for direction_id, streak in dstate.items():
                    restored_streak: dict = {}
                    for k, v in streak.items():
                        if isinstance(v, str) and k in ("last_failure_at", "held_at"):
                            try:
                                restored_streak[k] = datetime.fromisoformat(v)
                            except ValueError:
                                restored_streak[k] = v
                        else:
                            restored_streak[k] = v
                    _shared_state.direction_failure_streaks[direction_id] = restored_streak
                held_count = sum(1 for s in dstate.values() if s.get("held"))
                logger.info(
                    f"[DEPLOY] Restored direction failure streaks for {len(dstate)} directions "
                    f"({held_count} held)"
                )
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore direction failure streaks: {e}", exc_info=True)

        # Restore pipeline events ring buffer — alert windows survive restarts
        try:
            estate = getattr(state, 'pipeline_events_state', None)
            if estate and isinstance(estate, dict):
                from collections import deque as _deque
                events_list = estate.get("events", [])
                restored_events: _deque = _deque(maxlen=500)
                for e in events_list:
                    restored: dict = {}
                    for k, v in e.items():
                        if k == "ts" and isinstance(v, str):
                            try:
                                restored[k] = datetime.fromisoformat(v)
                            except ValueError:
                                restored[k] = v
                        else:
                            restored[k] = v
                    restored_events.append(restored)
                _shared_state.deploy_monitoring["pipeline_events"] = restored_events
                logger.info(f"[DEPLOY] Restored {len(restored_events)} pipeline events")
        except Exception as e:
            logger.warning(f"[DEPLOY] Could not restore pipeline events: {e}", exc_info=True)

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
                failures = sum(1 for r in results if r[1] == "failure")
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

from app.broadcast import manager, broadcast_to_project, _sanitize_log

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
    try:
        from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
        import asyncio as _asyncio
        _asyncio.ensure_future(dispatch_external_notification(
            task.project_id,
            "TASK_FAILED",
            {
                "task_id": str(task.id),
                "task_title": task.title or str(task.id)[:8],
                "task_type": task_type,
                "error_snippet": error_snippet or "",
            },
        ))
    except Exception as _ne:
        logger.warning(f"[NOTIFY] TASK_FAILED dispatch setup failed: {_ne}")

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

app = FastAPI(
    title="Blueprint Orchestrator",
    description="Secure Orchestration & State Management",
    lifespan=lifespan
)

# Telemetry must be set up before the app starts (cannot add middleware during lifespan)
setup_telemetry(app, engine)

app.include_router(admin_router)
app.include_router(admin_projects_router)
app.include_router(admin_settings_router)
app.include_router(agent_router)
app.include_router(github_router)
app.include_router(projects_router)
app.include_router(directions_router)
app.include_router(deploy_router)
app.include_router(admin_users_router)
app.include_router(admin_notifications_router)
app.include_router(admin_analytics_router)
app.include_router(admin_orchestrator_errors_router)
app.include_router(admin_scope_measurement_router)
app.include_router(agent_status_router)
app.include_router(admin_onboard_router)

def _get_zone_health_summary() -> dict:
    """Get zone health summary from compute module, with fallback if not in GCP mode."""
    try:
        from app.compute import get_zone_health_summary
        return get_zone_health_summary()
    except Exception:
        return {"total_zones": 0, "available": 0, "exhausted": []}


def _get_notification_stats() -> dict:
    """Return notification delivery stats from notification_dispatch module."""
    try:
        from app.notification_dispatch import get_notification_stats  # noqa: PLC0415
        return get_notification_stats()
    except Exception:
        return {}


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
                    {"created_at": r[0], "status": r[1], "failure_class": r[2] if len(r) > 2 else ""}
                    for r in recent_results
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
        "notification_stats": _get_notification_stats(),
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

    # --- GitHub API Quota (populated by _github_request on every response) ---
    # Surfaces X-RateLimit-Remaining so the dashboard can show a live quota
    # widget.  critical < 100, degraded < 500, ok otherwise (unknown = no
    # recent call observed).  top_endpoints shows the per-endpoint call
    # breakdown for the current reset window so we can see what's actually
    # burning the quota when it gets low (instead of guessing).
    gq = dict(_shared_state.github_quota)
    _q_remaining = gq.get("remaining")
    if _q_remaining is None:
        _q_status = "unknown"
    elif _q_remaining < 100:
        _q_status = "critical"
    elif _q_remaining < 500:
        _q_status = "degraded"
    else:
        _q_status = "ok"
    # Top 15 most-called endpoint templates this window.  Caps the payload
    # regardless of how many distinct endpoints we've seen.
    _top_calls = sorted(
        _shared_state.github_call_counts.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )[:15]
    # Cache-hit counters — calls we did NOT send to GitHub thanks to dedup /
    # short-circuit.  Compare against total_calls_this_window: if hits >> calls,
    # the caches are earning their keep.  See github.py:get_check_runs_cached,
    # security.py:scan_pr_for_injection, rollup_management.py:_verify_pr_merged.
    _cache_hits = dict(_shared_state.github_cache_hits)
    components["github_quota"] = {
        **gq,
        "status": _q_status,
        "top_endpoints": [{"endpoint": ep, "count": n} for ep, n in _top_calls],
        "total_calls_this_window": sum(_shared_state.github_call_counts.values()),
        "cache_hits_this_window": _cache_hits,
        "calls_avoided_this_window": sum(_cache_hits.values()),
    }

    # --- Pipeline Health (rolling reaper signals) ---
    ph_snapshot = _shared_state.get_pipeline_health_snapshot()
    ph_status = "degraded" if ph_snapshot["alerts_active"] else "ok"
    components["pipeline_health"] = dict(ph_snapshot, status=ph_status)

    # --- Overall Status ---
    statuses = [c["status"] for c in components.values() if isinstance(c, dict) and "status" in c]
    if "down" in statuses:
        overall = "unhealthy"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    return {"status": overall, "components": components, "pipeline_health": ph_snapshot}

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
    logger.error(f"[UNHANDLED] {request.method} {request.url.path}: {type(exc).__name__}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {str(exc)[:200]}"},
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

from app.task_completion import (  # noqa: E402 — re-exports for backward compatibility
    _parse_plan_output,
    _extract_json_array,
    _validate_plan_dependencies,
    _log_file_overlaps,
    _inject_file_overlap_deps,
    _ensure_branch,
    _handle_plan_completion,
    _handle_security_audit_completion,
    _handle_perf_completion,
    _handle_investigation_completion,
    _handle_design_doc_completion,
    _compute_first_attempt_succeeded,
    TASK_PRIORITY_MAP,
)

from app.anomaly_detection import (  # noqa: E402
    _normalize_error_message,
    _error_fingerprint,
    _compute_research_anomalies,
    _check_anomaly_proposals,
    _expire_stale_proposals,
)

from app.research_completion import (  # noqa: E402
    _compute_momentum_factor,
    _update_cycle_history,
    _compute_saturation,
    _parse_research_findings,
    _parse_research_insights,
    _handle_research_completion,
)

from app.research_context import (  # noqa: E402
    _parse_research_context,
    _parse_eviction_counts,
    _format_eviction_body,
    _build_research_context,
)

from app.strategic_completion import (  # noqa: E402
    _check_direction_health,
    _check_direction_completion,
    _is_direction_duplicate,
    _handle_strategic_completion,
)

from app.deploy_health import (  # noqa: E402 — re-exports for backward compatibility
    should_rollback,
    _get_deploy_state_for_update,
    _check_monitoring_window,
    _check_external_health,
    _create_auto_pause_alerts,
    _create_auto_resume_alerts,
    _check_pause_mode,
    broadcast_deploy_state,
)

async def _lock_project_or_429(session: AsyncSession, project_id: uuid.UUID) -> Project:
    """Acquire a row lock on a Project for atomic read-modify-write.

    Uses SKIP LOCKED so concurrent agent writes don't queue behind the 30s
    lock_timeout set in database.py. If another handler currently holds the
    lock, returns 429 Retry-After so the agent retries (same contract as
    agent_status.py uses for task-row contention; see commit 88a77903).
    Cloud Run flags any 5xx as ContainerError and quarantines the instance,
    so 429 is deliberate — it conveys "temporary, retry" without tripping
    container-fault classification.
    """
    result = await session.execute(
        select(Project).where(Project.id == project_id).with_for_update(skip_locked=True)
    )
    project = result.scalars().first()
    if project is not None:
        return project
    # Distinguish "row locked" from "row missing" with a non-locking read.
    exists = (await session.execute(
        select(Project.id).where(Project.id == project_id)
    )).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="Project not found")
    raise HTTPException(
        status_code=429,
        detail="Project row locked by concurrent write, retry",
        headers={"Retry-After": "2"},
    )


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
    """Agent endpoint for design doc operations. Read (list/get) is open to all agents.
    Write (set/delete) is restricted to design_doc_update/strategic_review agents."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Write operations restricted to strategic agents; reads open to all
    if payload.op not in ("list", "get") and token.get("task_type") not in ("design_doc_update", "strategic_review"):
        raise HTTPException(status_code=403, detail="Write operations restricted to design_doc_update/strategic_review agents")

    if payload.op == "list":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        return {"keys": [k for k in doc.keys() if not k.startswith("_")]}

    if payload.op == "get":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        if not payload.key:
            raise HTTPException(status_code=400, detail="key is required for get")
        value = doc.get(payload.key)
        if value is None:
            raise HTTPException(status_code=404, detail=f"Key '{payload.key}' not found")
        return {"key": payload.key, "value": value}

    if not payload.key or not isinstance(payload.key, str):
        raise HTTPException(status_code=400, detail="key is required for set/delete")
    key = payload.key.strip()
    if not key or len(key) > 100 or key.startswith("_"):
        raise HTTPException(status_code=400, detail="Invalid key (max 100 chars, no _ prefix)")

    # Lock project for atomic update (skip_locked + 429 avoids lock_timeout cascade)
    project = await _lock_project_or_429(session, task.project_id)

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
        # Rebuild file-to-system reverse index atomically when a systems/* key is set
        if key.startswith("systems/"):
            file_to_system: dict = {}
            for doc_key, doc_val in doc.items():
                if doc_key.startswith("systems/") and isinstance(doc_val, dict):
                    sys_id = doc_key[len("systems/"):]
                    for fpath in doc_val.get("modules", []):
                        if isinstance(fpath, str):
                            file_to_system[fpath] = sys_id
            doc["_file_to_system"] = file_to_system
    elif payload.op == "delete":
        doc.pop(key, None)
        # Rebuild reverse index if a systems/* key is deleted to remove stale entries
        if key.startswith("systems/"):
            file_to_system: dict = {}
            for doc_key, doc_val in doc.items():
                if doc_key.startswith("systems/") and isinstance(doc_val, dict):
                    sys_id = doc_key[len("systems/"):]
                    for fpath in doc_val.get("modules", []):
                        if isinstance(fpath, str):
                            file_to_system[fpath] = sys_id
            doc["_file_to_system"] = file_to_system
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

# ── Agent Playbook ───────────────────────────────────────────────────────
# Any agent task type can read/write persistent operational knowledge stored
# as playbook/* keys in the project's design_doc JSONB column.

from app.payload_schemas import ALL_TASK_TYPES as _VALID_TASK_TYPES, validate_internal_payload as _validate_internal_payload  # noqa: E402

class PlaybookUpdate(BaseModel):
    task_id: uuid.UUID
    op: str  # "get", "set", "delete", "list"
    key: Optional[str] = None  # alphanumeric + hyphens, max 50 chars
    value: Optional[str] = None  # plain text, max 2KB
    applicable_types: Optional[List[str]] = None  # task types this entry applies to; None = all types

_PLAYBOOK_KEY_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9\-]{0,49}$')

@app.post("/api/agent/playbook")
@limiter.limit("120/minute")
async def update_playbook(
    request: Request,
    payload: PlaybookUpdate,
    session: AsyncSession = Depends(get_session),
    token: dict = Depends(verify_agent_token),
):
    """Agent endpoint for persistent operational playbook. Any task type may call this."""
    if str(payload.task_id) != token.get("sub"):
        raise HTTPException(status_code=403, detail="Task ID mismatch")
    # NOTE: unlike design-doc, no task_type restriction — any agent may use playbook

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    op = payload.op
    if op not in ("get", "set", "delete", "list"):
        raise HTTPException(status_code=400, detail=f"Unknown op: {op}")

    if op == "list":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        keys = [k[len("playbook/"):] for k in doc if k.startswith("playbook/")]
        return {"status": "ok", "op": op, "keys": keys}

    if op == "get":
        project = await session.get(Project, task.project_id)
        doc = project.design_doc or {} if project else {}
        entries = {}
        for k, v in doc.items():
            if k.startswith("playbook/") and isinstance(v, dict):
                short_key = k[len("playbook/"):]
                entries[short_key] = {
                    "content": v.get("content", ""),
                    "updated_at": v.get("updated_at", ""),
                    "source_task_id": v.get("source_task_id", ""),
                }
        return {"status": "ok", "op": op, "entries": entries}

    # set / delete require a valid key
    if not payload.key:
        raise HTTPException(status_code=400, detail="key is required for set/delete")
    key = payload.key.strip()
    if key.startswith("_") or not _PLAYBOOK_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="Invalid key: must be alphanumeric+hyphens, max 50 chars, no _ prefix")

    # Lock project for atomic update (skip_locked + 429 avoids lock_timeout cascade)
    project = await _lock_project_or_429(session, task.project_id)

    import json as _json
    doc = dict(project.design_doc or {})
    meta = doc.get("_meta", {"revision": 0})

    if op == "set":
        if payload.value is None:
            raise HTTPException(status_code=400, detail="value is required for set")
        if len(payload.value) > 2048:
            raise HTTPException(status_code=400, detail=f"Value too large ({len(payload.value)} bytes, max 2KB)")
        if payload.applicable_types is not None:
            invalid = [t for t in payload.applicable_types if t not in _VALID_TASK_TYPES]
            if invalid:
                raise HTTPException(status_code=400, detail=f"Invalid task type(s): {invalid}. Must be one of: {sorted(_VALID_TASK_TYPES)}")
        playbook_key = f"playbook/{key}"
        existing = doc.get(playbook_key, {})
        entry: dict = {
            "content": payload.value,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": existing.get("confirmed_count", 0),
        }
        if payload.applicable_types is not None:
            entry["applicable_types"] = payload.applicable_types
        doc[playbook_key] = entry
        # Size check: all playbook/* keys combined max 30KB
        playbook_total = sum(
            len(_json.dumps(v)) for k, v in doc.items() if k.startswith("playbook/")
        )
        if playbook_total > 30_000:
            del doc[playbook_key]  # rollback
            raise HTTPException(status_code=400, detail=f"Playbook too large ({playbook_total} bytes, max 30KB total)")
    elif op == "delete":
        doc.pop(f"playbook/{key}", None)

    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by_task"] = str(task.id)
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()

    return {"status": "ok", "op": op, "key": key, "revision": meta["revision"]}

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
    op: str  # "set_vision", "create_direction", "update_direction", "retire_direction", "create_message", "resolve_message", "spawn_task"
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
    # Message ops (create/resolve) are available to all agent types.
    # Direction/vision ops are restricted to strategic_review only.
    MESSAGE_OPS = ("create_message", "resolve_message")
    if payload.op not in MESSAGE_OPS and token.get("task_type") not in ("strategic_review",):
        raise HTTPException(status_code=403, detail="Only strategic_review agents can modify directions/vision")

    task = await session.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Lock project for atomic update (skip_locked + 429 avoids lock_timeout cascade)
    project = await _lock_project_or_429(session, task.project_id)

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
        new_vision = {**dict(data), "revision": (current.get("revision", 0) or 0) + 1, "last_updated": _utcnow().isoformat()}
        # Preserve internal tracking fields; reset failure counters on success
        new_vision.update({k: v for k, v in current.items() if k.startswith("_") and k not in new_vision})
        new_vision.pop("_strategic_failures", None)
        new_vision.pop("_strategic_failures_at", None)
        project.vision = new_vision
        flag_modified(project, "vision")
        session.add(project)
        # Stamp task so the completion handler knows a real vision update occurred
        task.context = {**(task.context or {}), "_strategic_vision_set": True}
        flag_modified(task, "context")
        session.add(task)
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
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return JSONResponse(content=_resp({"status": "skipped", "reason": "duplicate"}), status_code=409)
        active_count = sum(1 for d in directions if d.get('status') == 'active')
        max_active = _runtime_config.get('max_active_directions', 5)
        if active_count >= max_active:
            ctx = task.context or {}
            task.context = {**ctx, "_strategic_api_called": True}
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            return JSONResponse(content=_resp({'status': 'skipped', 'reason': f'Direction cap reached: {active_count}/{max_active} active'}), status_code=409)
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
        if data.get("category") and data["category"] in ("product", "structural"):
            new_dir["category"] = data["category"]
        directions.append(new_dir)
        project.directions = directions
        flag_modified(project, "directions")
        session.add(project)
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_dirs_created": ctx.get("_strategic_dirs_created", 0) + 1}
        flag_modified(task, "context")
        session.add(task)
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
                        ctx = task.context or {}
                        task.context = {**ctx, "_strategic_api_called": True}
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
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
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_api_called": True}
        flag_modified(task, "context")
        session.add(task)
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
        ctx = task.context or {}
        task.context = {**ctx, "_strategic_dirs_retired": ctx.get("_strategic_dirs_retired", 0) + 1}
        flag_modified(task, "context")
        session.add(task)
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
        # Broadcast so the UI can update immediately instead of waiting for poll
        await broadcast_to_project(
            str(project.id), "message_created",
            {"message_id": str(new_msg.id), "type": msg_type.value, "from": "strategic"},
        )
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

    elif payload.op == "spawn_task":
        # Allow strategic review to directly spawn a research or plan task for
        # targeted investigation, jumping the normal autonomous research queue.
        # Restricted to investigation types — implementation must go through
        # normal direction → research → plan → create flow.
        ALLOWED_SPAWN_TYPES = {"research", "plan"}
        spawn_type = data.get("type")
        if spawn_type not in ALLOWED_SPAWN_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"spawn_task type must be one of {sorted(ALLOWED_SPAWN_TYPES)}; got '{spawn_type}'",
            )
        raw_prompt = str(data.get("prompt", "")).strip()
        if not raw_prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        raw_title = str(data.get("title", "")).strip()
        if not raw_title:
            raise HTTPException(status_code=400, detail="title is required")
        # Cap to prevent runaway prompts consuming token budget
        prompt = _cap_str(raw_prompt, 10_000, "prompt", warnings)
        title = _cap_str(raw_title, 200, "title", warnings)
        direction_id = data.get("direction_id")
        if direction_id and not any(
            d.get("id") == direction_id for d in (project.directions or [])
        ):
            raise HTTPException(status_code=400, detail=f"Unknown direction_id: {direction_id}")

        # Build payload — use the same validator the research/plan spawners use
        # so the new task is indistinguishable from normal autonomous spawns
        # except for the context flags.
        spawn_payload: dict = {
            "type": spawn_type,
            "prompt": prompt,
            "title": title,
            "base_branch": "main",
        }
        if direction_id:
            spawn_payload["direction_id"] = direction_id
        # Apply per-type model/effort defaults
        type_model = _runtime_config.get("task_type_models", {}).get(spawn_type) or None
        type_effort = _runtime_config.get("task_type_efforts", {}).get(spawn_type) or None
        if type_model:
            spawn_payload["model"] = type_model
        if type_effort:
            spawn_payload["effort"] = type_effort
        spawn_payload = _validate_internal_payload(
            spawn_payload, f"strategic_spawn_task:{spawn_type}"
        )

        new_task_ctx: dict = {
            "spawned_by": "strategic_direct",
            "parent_strategic_task_id": str(task.id),
        }
        if direction_id:
            new_task_ctx["direction_id"] = direction_id

        new_task = Task(
            project_id=project.id,
            title=title,
            description=prompt[:500],
            payloads=[spawn_payload],
            context=new_task_ctx,
            status=TaskStatus.QUEUED,
            priority=100,  # ahead of default (50) — course-correction runs first
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            created_by="STRATEGIC_AGENT",
        )
        session.add(new_task)
        await session.flush()

        ctx = task.context or {}
        task.context = {
            **ctx,
            "_strategic_tasks_spawned": ctx.get("_strategic_tasks_spawned", 0) + 1,
        }
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[STRATEGIC API] Spawned {spawn_type} task {new_task.id} "
            f"(priority=100) from strategic review {task.id} for project {project.id}"
        )
        return _resp({
            "status": "ok",
            "op": "spawn_task",
            "task_id": str(new_task.id),
            "type": spawn_type,
            "priority": 100,
        })

    else:
        raise HTTPException(status_code=400, detail=f"Unknown op: {payload.op}")


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
