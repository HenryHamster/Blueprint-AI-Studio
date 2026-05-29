import hmac
import logging
import os
import time
import uuid
import asyncio
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, Any
from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from app.database import get_session, AsyncSessionLocal, engine
from app.models import (
    Project,
    Task,
    TaskStatus,
    DeploymentState,
    DeploymentStatus,
    Alert,
    AlertSeverity,
    RegressionRemediationCase,
    RegressionCaseStatus,
)
from app.compute import delete_agent_vm, provision_agent_vm  # noqa: F401  (provision_agent_vm re-exported for test patches; see main.py:108 docstring)
from app.auth import get_current_user
from app.telemetry import setup_telemetry
from app.admin import router as admin_router
from app.routers.admin_projects import router as admin_projects_router
from app.routers.admin_settings import router as admin_settings_router
from app.routers.admin_debug import router as admin_debug_router
from app.routers.admin_pool_accounts import router as admin_pool_accounts_router
from app.routers.admin_project_config import router as admin_project_config_router
from app.routers.agent import router as agent_router
from app.routers.agent_strategic_fetch import router as agent_strategic_fetch_router
from app.routers.agent_warm_continuity import router as agent_warm_continuity_router
from app.routers.agent_credentials import router as agent_credentials_router
from app.routers.github import router as github_router
from app.routers.projects import router as projects_router
from app.routers.directions import router as directions_router
from app.routers.deploy import router as deploy_router
from app.routers.admin_users import router as admin_users_router
from app.routers.admin_notifications import router as admin_notifications_router
from app.routers.admin_analytics import router as admin_analytics_router
from app.routers.admin_digest import router as admin_digest_router
from app.routers.admin_activity import router as admin_activity_router
from app.routers.admin_health import router as admin_health_router
from app.routers.admin_orchestrator_errors import (
    router as admin_orchestrator_errors_router,
)
from app.routers.admin_scope_measurement import router as admin_scope_measurement_router
from app.routers.agent_status import router as agent_status_router
from app.routers.admin_onboard import router as admin_onboard_router
from app.routers.admin_agent_ops import router as admin_agent_ops_router
from app.routers.admin_agent_ops import _bump_size_limit  # noqa: F401  re-exported for test patches (test_agent_annotate.py)
from app.shared_state import limiter
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app import orchestrator as _orch
from app.lifecycle import lifespan
from app.time_utils import _utcnow
from app.broadcast import broadcast_to_project

logger = logging.getLogger(__name__)



# Expected schema revision — must match the latest alembic migration.
# Update this when adding new migrations.
EXPECTED_SCHEMA_REVISION = "0128"

# --- Task Priority Map ---
# Maps priority string labels to integer values stored on Task.priority.
TASK_PRIORITY_MAP: Dict[str, int] = {"high": 100, "medium": 50, "low": 0}

# GCP VM count cache — updated by cleanup service, used by orchestrator concurrency check
_last_gcp_vm_count: int = 0
_last_gcp_vm_check: float = 0.0
_GCP_VM_CHECK_INTERVAL: float = 120.0  # Seconds between GCP VM count refreshes

# Orchestrator-initiated VM-delete tracker. Reaper consults this to distinguish
# "VM DELETED because orchestrator told it to" from "VM DELETED because GCP
# preempted it." Without this, the reaper sees vm_status=DELETED on a SPOT VM
# and bumps spot_preemption_count, treating an orchestrator-completed task as
# a preemption — observed in production 2026-05-04 when status POST 500'd
# during cutover but agent_completion still deleted the VM (reaper later
# misclassified the orphan).
# Key: vm_instance_id (str). Value: (unix_ts, reason). TTL = 30min — pruned
# lazily on read. In-memory only (Cloud Run = single instance per region for
# this service); restart loses entries but the misclassification window then
# closes with the worker.
_orchestrator_deleted_vms: Dict[str, tuple[float, str]] = {}
_ORCHESTRATOR_DELETE_TTL: float = 1800.0  # 30 minutes
# Hard cap so a write-faster-than-read pattern (e.g. reaper paused while
# many deletes happen) cannot grow this dict unboundedly. At ~80 bytes/entry
# this caps memory at ~400 KB. Pruning is by oldest-first when over the cap,
# preserving the most-recently-deleted entries that the reaper is most
# likely to consult next.
_ORCHESTRATOR_DELETE_MAX_ENTRIES: int = 5000


def is_orchestrator_initiated_delete(vm_id: str) -> tuple[bool, str | None]:
    """Return (True, reason) if `vm_id` was deleted by the orchestrator
    within the last _ORCHESTRATOR_DELETE_TTL seconds.

    Side-effect: prunes expired entries on every call AND enforces the size
    cap. Module-level state so the reaper can import it without a circular
    dep on `_ensure_vm_deleted`.
    """
    now = time.time()
    expired = [
        k
        for k, (ts, _) in _orchestrator_deleted_vms.items()
        if now - ts > _ORCHESTRATOR_DELETE_TTL
    ]
    for k in expired:
        _orchestrator_deleted_vms.pop(k, None)
    # Hard-cap eviction: oldest entries first. Defensive — if the dict still
    # has more than the cap after TTL pruning, we drop the oldest until it
    # fits. Logged so operators see the eviction and can investigate why
    # writes outpaced reads.
    if len(_orchestrator_deleted_vms) > _ORCHESTRATOR_DELETE_MAX_ENTRIES:
        sorted_by_ts = sorted(
            _orchestrator_deleted_vms.items(), key=lambda kv: kv[1][0]
        )
        excess = len(_orchestrator_deleted_vms) - _ORCHESTRATOR_DELETE_MAX_ENTRIES
        for k, _ in sorted_by_ts[:excess]:
            _orchestrator_deleted_vms.pop(k, None)
        logger.warning(
            "[VM TRACK] _orchestrator_deleted_vms exceeded %d entries — evicted %d oldest",
            _ORCHESTRATOR_DELETE_MAX_ENTRIES,
            excess,
        )
    entry = _orchestrator_deleted_vms.get(vm_id)
    if entry is None:
        return False, None
    return True, entry[1]


# --- Max Plan Account Pool ---
# Pool state and logic extracted to max_plan_pool.py. Re-exported here for
# backward compatibility with callers (orchestrator, tests) that import from
# app.main. The 2026-05-03 ruff --fix sweep removed the public-API helpers
# below as "unused" because main.py doesn't call them directly — but tests
# patch them via app.main.X (see test_pipeline_reliability.py:105+) and
# orchestrator code imports them via the same path. noqa: F401 pins them so
# a future --fix can't repeat the regression.
from app.max_plan_pool import (  # noqa: F401
    _account_by_id,
    _max_plan_pool_lock,
    _all_accounts,
    _max_plan_pools,
    load_max_plan_pool,
    _extract_task_model,
    select_max_plan_account,
    release_max_plan_account,
    recheck_disabled_accounts,
)

# --- Deployment State Management ---

# Current revision from Cloud Run (set on startup)
_current_revision: str = os.getenv("K_REVISION", "local-dev")
_current_sha: str = os.getenv("DEPLOY_SHA", "unknown")


async def _ensure_vm_deleted(
    task_or_vm_id, *, reason: str = "terminal_transition"
) -> bool:
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
    # Record the orchestrator-initiated delete BEFORE attempting the GCP
    # call. Even if the call fails partway and the VM ends up in TERMINATED
    # rather than DELETED, the marker still tells the reaper this was an
    # orchestrator action — preventing a spot_preemption misclassification
    # if the VM is later cleaned up by another path.
    _orchestrator_deleted_vms[vm_id] = (time.time(), reason)

    for attempt in range(3):
        try:
            result = await delete_agent_vm(vm_id)
            if result:
                logger.info(f"[VM CLEANUP] Deleted VM {vm_id}")
                if _last_gcp_vm_count > 0:
                    _last_gcp_vm_count -= 1
                    logger.info(
                        f"[VM CLEANUP] Decremented cached VM count to {_last_gcp_vm_count}"
                    )
                return True
            else:
                logger.warning(
                    f"[VM CLEANUP] delete_agent_vm returned False for {vm_id} (attempt {attempt + 1}/3)"
                )
        except Exception as e:
            logger.error(
                f"[VM CLEANUP] Failed to delete VM {vm_id} (attempt {attempt + 1}/3): {e}",
                exc_info=True,
            )

        if attempt < 2:
            await asyncio.sleep(2**attempt)  # 1s, 2s backoff

    logger.error(
        f"[VM CLEANUP] FAILED to delete VM {vm_id} after 3 attempts — will rely on cleanup sweep"
    )
    return False


# Strong references for fire-and-forget VM-delete tasks. asyncio.create_task
# returns a weak handle — without holding a reference here, GC may collect
# the coroutine mid-flight and the GCP delete is silently dropped.
_background_vm_deletes: set[asyncio.Task] = set()


def fire_and_forget_vm_delete(
    task_or_vm_id, *, reason: str = "terminal_transition"
) -> None:
    """Schedule _ensure_vm_deleted in the background; release the caller's
    row lock immediately.

    Use from terminal-status request handlers (e.g. /api/agent/status). The
    awaited form blocks the FOR UPDATE row lock for 60-90s during GCP I/O,
    causing concurrent agent POSTs on the same row to receive 429 and
    risking a lost-COMPLETED-commit if Cloud Run terminates the request
    mid-block. Production incident 2026-05-05 task 04605f9d: agent posted
    APPROVE verdict, handler held lock through GCP delete, retry got 429,
    status update never committed, reaper swept work that had already
    succeeded and re-ran the entire review.

    The orchestrator-deleted marker is recorded synchronously by
    _ensure_vm_deleted (line above) before its first await, so the reaper
    sees the orchestrator's intent immediately even if the background
    delete is delayed by event-loop pressure. Cleanup-sweep is the safety
    net if the background task is dropped (Cloud Run shutdown mid-flight).
    """
    if isinstance(task_or_vm_id, Task):
        vm_id = task_or_vm_id.vm_instance_id
    else:
        vm_id = task_or_vm_id
    if not vm_id:
        return
    # Record marker synchronously before scheduling. _ensure_vm_deleted
    # also writes this on its first non-await line, but writing here too
    # closes the window where Cloud Run terminates the worker between
    # create_task() and the event loop picking up the coroutine — a peer
    # observing vm_status=DELETED during that window would otherwise
    # misclassify the orchestrator action as spot-preemption.
    _orchestrator_deleted_vms[vm_id] = (time.time(), reason)
    bg = asyncio.create_task(_ensure_vm_deleted(vm_id, reason=reason))
    _background_vm_deletes.add(bg)
    bg.add_done_callback(_background_vm_deletes.discard)


async def _deferred_backlog_teardown(vm_id: str, reason: str) -> None:
    """Hold a just-idle VM's teardown briefly so it can claim backlog work.

    Backlog reuse (efficiency direction d-91d8dede): when a COMPLETED task's
    VM has eligible QUEUED backlog work in its project, this defers teardown
    by runtime_config['warm_backlog_teardown_defer_secs'] (read fresh per call)
    so the agent's /next-role poll can claim that work and run it warm. After
    the window, the VM is deleted iff it never claimed anything.

    Crucially this does NOT pre-record the orchestrator-deleted marker (unlike
    fire_and_forget_vm_delete): that marker makes is_orchestrator_initiated_delete
    return True, and the /next-role doomed-VM guard would then refuse the VM's
    own warm claim. The marker is only written if/when we actually delete.
    """
    from app.warm_backlog_reuse import _teardown_defer_secs

    await asyncio.sleep(_teardown_defer_secs())
    try:
        async with AsyncSessionLocal() as s:
            claimed = (
                await s.execute(
                    select(Task.id)
                    .where(
                        Task.vm_instance_id == vm_id,
                        Task.status.in_(
                            [
                                TaskStatus.PROVISIONING,
                                TaskStatus.IN_PROGRESS,
                                TaskStatus.EVALUATING,
                            ]
                        ),
                    )
                    .limit(1)
                )
            ).first()
    except Exception:
        # If the re-check query fails, fall through to teardown — never leak a
        # VM by assuming it was reused.
        logger.exception(
            "[BACKLOG-REUSE] Deferred-teardown re-check failed for VM %s — "
            "tearing down",
            vm_id,
        )
        claimed = None
    if claimed is not None:
        logger.info(
            "[BACKLOG-REUSE] VM %s claimed backlog work during defer window — "
            "skipping teardown",
            vm_id,
        )
        return
    logger.info(
        "[BACKLOG-REUSE] VM %s claimed nothing within defer window — tearing down",
        vm_id,
    )
    await _ensure_vm_deleted(vm_id, reason=reason)


def schedule_deferred_backlog_teardown(
    vm_id: str, *, reason: str = "agent_completion"
) -> None:
    """Fire-and-forget the deferred-teardown coroutine (see above)."""
    if not vm_id:
        return
    bg = asyncio.create_task(_deferred_backlog_teardown(vm_id, reason))
    _background_vm_deletes.add(bg)
    bg.add_done_callback(_background_vm_deletes.discard)


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
        zone_tasks = [
            asyncio.to_thread(_list_vms_in_zone, zone) for zone in unique_zones
        ]
        zone_results = await asyncio.wait_for(
            asyncio.gather(*zone_tasks, return_exceptions=True),
            timeout=10,  # Short timeout — cache is usually fresh from cleanup service
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
            result = await session.execute(
                sa_text("SELECT version_num FROM alembic_version LIMIT 1")
            )
            row = result.first()
            if not row:
                logger.warning(
                    "[STARTUP] No alembic_version row — database may be uninitialized"
                )
                return
            # Exact equality required — `0055_a` != `0055_b` would slip past startswith.
            # Raise unconditionally so dev environments catch mismatches the same way Cloud Run does.
            if row[0] != EXPECTED_SCHEMA_REVISION:
                msg = (
                    f"Schema version mismatch: database at '{row[0]}', "
                    f"code expects '{EXPECTED_SCHEMA_REVISION}'"
                )
                logger.error(f"[STARTUP] {msg}")
                raise RuntimeError(msg)
        except Exception as e:
            if "alembic_version" in str(e).lower():
                logger.warning(
                    "[STARTUP] alembic_version table not found — skipping schema check"
                )
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
                logger.info(
                    f"[DEPLOY] First boot — initialized deployment state (revision={_current_revision})"
                )
                return
            except Exception:
                await session.rollback()
                # Another instance likely inserted first — fetch the row they created
                state = await session.get(DeploymentState, 1)
                if not state:
                    raise  # Genuine error, not a race

        # Restore persisted runtime config values so settings survive restarts.
        # Shared with the periodic cross-instance sync (see sync_runtime_config_loop).
        try:
            from app.shared_state import sync_runtime_config_from_state

            restored = await sync_runtime_config_from_state(state)
            if restored:
                logger.info(f"[DEPLOY] Restored runtime config from DB: {restored}")
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore runtime config (migration pending?): {e}",
                exc_info=True,
            )
        # Restore circuit breaker state — prevents deploy from clearing tripped breakers
        try:
            if state.circuit_breaker_states:
                for proj_id, breaker_data in state.circuit_breaker_states.items():
                    restored = {**breaker_data}
                    if restored.get("opened_at") and isinstance(
                        restored["opened_at"], str
                    ):
                        restored["opened_at"] = datetime.fromisoformat(
                            restored["opened_at"]
                        )
                    _shared_state.circuit_breakers[proj_id] = restored
                logger.info(
                    f"[DEPLOY] Restored circuit breaker state for {len(state.circuit_breaker_states)} projects"
                )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore circuit breaker state: {e}", exc_info=True
            )

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
                            if isinstance(v, str) and k in (
                                "last_success",
                                "last_failure",
                                "cooldown_expires_at",
                            ):
                                try:
                                    restored[k] = datetime.fromisoformat(v)
                                except ValueError:
                                    if k == "cooldown_expires_at":
                                        fallback = datetime.now(tz.utc) + timedelta(
                                            minutes=30
                                        )
                                        logger.warning(
                                            f"[DEPLOY] Corrupted cooldown_expires_at for zone {zone} ({v!r}); failing safe to {fallback.isoformat()}"
                                        )
                                        restored[k] = fallback
                                    elif k == "last_failure":
                                        fallback = datetime.now(tz.utc)
                                        logger.warning(
                                            f"[DEPLOY] Corrupted last_failure for zone {zone} ({v!r}); failing safe to {fallback.isoformat()}"
                                        )
                                        restored[k] = fallback
                                    else:
                                        logger.warning(
                                            f"[DEPLOY] Skipping corrupted zone health key {k} for zone {zone}: {v!r}"
                                        )
                                        continue
                            elif k == "recent_outcomes" and isinstance(v, list):
                                from collections import deque as _deque
                                from app.compute import ZONE_HISTORY_WINDOW

                                valid = [e for e in v if isinstance(e, bool)]
                                if len(valid) < len(v):
                                    logger.warning(
                                        f"[DEPLOY] Filtered {len(v) - len(valid)} non-boolean element(s) from recent_outcomes for zone {zone}"
                                    )
                                restored[k] = _deque(
                                    valid[-ZONE_HISTORY_WINDOW:],
                                    maxlen=ZONE_HISTORY_WINDOW,
                                )
                            elif k == "consecutive_failures":
                                if isinstance(v, int):
                                    restored[k] = v
                                else:
                                    logger.warning(
                                        f"[DEPLOY] consecutive_failures for zone {zone} is not an int ({v!r}), defaulting to 0"
                                    )
                                    restored[k] = 0
                            elif k == "last_failure_reason":
                                if v is None or isinstance(v, str):
                                    restored[k] = v
                                else:
                                    logger.warning(
                                        f"[DEPLOY] last_failure_reason for zone {zone} is not a string ({v!r}), defaulting to None"
                                    )
                                    restored[k] = None
                            else:
                                restored[k] = v
                        _zone_health[zone] = restored
                        # Restore _zone_failures for zones still in cooldown
                        if restored.get("cooldown_expires_at") and restored[
                            "cooldown_expires_at"
                        ] > datetime.now(tz.utc):
                            _zone_failures[zone] = restored.get(
                                "last_failure", datetime.now(tz.utc)
                            )
                logger.info(
                    f"[DEPLOY] Restored zone health state for {len(state.zone_health_state)} zones"
                )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore zone health state: {e}", exc_info=True
            )

        # Restore pause/health streaks
        try:
            if state.zone_health_state:
                import app.deploy_health as _dh

                _dh._restore_health_streaks(state.zone_health_state)
                if (
                    _dh._pause_failure_streak
                    or _dh._health_recovery_history
                    or _dh._last_auto_resume_at
                ):
                    logger.info(
                        f"[DEPLOY] Restored health streaks: pause_failures={_dh._pause_failure_streak}, "
                        f"recovery_history={list(_dh._health_recovery_history)}, "
                        f"last_auto_resume_at={_dh._last_auto_resume_at}"
                    )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore health streaks: {e}", exc_info=True
            )

        # Restore persistent anomaly state — preserves occurrence counts across restarts
        try:
            pstate = getattr(state, "persistent_anomaly_state", None)
            if pstate and isinstance(pstate, dict):
                raw_anomalies = pstate.get("persistent_anomalies", {})
                for proj_id, proj_anomalies in raw_anomalies.items():
                    restored_proj: dict = {}
                    for k, v in proj_anomalies.items():
                        if not isinstance(v, dict):
                            restored_proj[k] = v
                            continue
                        entry: dict = {}
                        for fk, fv in v.items():
                            # first_seen/last_seen are ISO strings everywhere they're
                            # written and compared (anomaly_detection.py). Coerce any
                            # pre-existing datetime back to isoformat so legacy
                            # persisted pods don't crash the stale-cutoff comparison.
                            if fk in ("first_seen", "last_seen") and isinstance(
                                fv, datetime
                            ):
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
                        _shared_state.runtime_config["max_concurrent_vms"] = max(
                            2, pre_vms // 2
                        )
                    logger.info(
                        "[DEPLOY] Re-applied active anomaly adjustments: "
                        f"max_vms={_shared_state.runtime_config['max_concurrent_vms']}, "
                        f"failure_gate={adj.get('failure_gate_threshold')}"
                    )
                logger.info(
                    f"[DEPLOY] Restored anomaly state for {len(raw_anomalies)} projects"
                )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore anomaly state: {e}", exc_info=True
            )

        # Seed task_results from DB so fast-recovery has data after restart
        # (in-memory deque is empty otherwise, blocking recovery for ~2h).
        if _shared_state.anomaly_adjustments.get("active"):
            try:
                from app.anomaly_detection import seed_task_results_from_db

                seeded = await seed_task_results_from_db(session)
                logger.info(
                    f"[DEPLOY] Seeded {seeded} task_results from DB for fast-recovery"
                )
            except Exception as e:
                logger.warning(
                    f"[DEPLOY] Could not seed task_results from DB: {e}", exc_info=True
                )

        # Restore direction failure streaks — holds survive restarts silently
        try:
            dstate = getattr(state, "direction_streak_state", None)
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
                    _shared_state.direction_failure_streaks[direction_id] = (
                        restored_streak
                    )
                held_count = sum(1 for s in dstate.values() if s.get("held"))
                logger.info(
                    f"[DEPLOY] Restored direction failure streaks for {len(dstate)} directions "
                    f"({held_count} held)"
                )
        except Exception as e:
            logger.warning(
                f"[DEPLOY] Could not restore direction failure streaks: {e}",
                exc_info=True,
            )

        # Restore pipeline events ring buffer — alert windows survive restarts
        try:
            estate = getattr(state, "pipeline_events_state", None)
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
            logger.warning(
                f"[DEPLOY] Could not restore pipeline events: {e}", exc_info=True
            )

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
                        restored = state.pre_pause_max_vms or int(
                            os.getenv("MAX_CONCURRENT_VMS", "5")
                        )
                        _runtime_config["max_concurrent_vms"] = restored
                        state.pre_pause_max_vms = None
                        session.add(state)
                        await session.commit()
                        logger.warning(
                            f"[DEPLOY] Auto-recovery from HALTED after {elapsed:.0f}s cooldown — max_concurrent_vms restored to {restored}"
                        )
                        await _create_halted_recovery_alerts(session)
                        await session.commit()
                    else:
                        remaining = 3600 - elapsed
                        logger.info(
                            f"[DEPLOY] HALTED — {remaining:.0f}s remaining until auto-recovery"
                        )
            except Exception as e:
                logger.warning(
                    f"[DEPLOY] Could not check HALTED auto-recovery (halted_at column may not exist yet): {e}"
                )

        # Respect paused/halted state — these override the persisted value
        if state.status in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
            _runtime_config["max_concurrent_vms"] = 0
            logger.info(
                f"[DEPLOY] max_concurrent_vms forced to 0 — state is {state.status} "
                f"(reason: {state.pause_reason or 'unknown'}). "
                f"Set max VMs > 0 in settings to resume."
            )

        if state.current_revision == _current_revision:
            # Same revision — normal restart (cold start, scale event, etc.)
            logger.info(
                f"[DEPLOY] Normal restart — same revision {_current_revision}, status={state.status}"
            )
            return

        # Revision changed — is it a new deploy or a rollback?
        if (
            _current_revision == state.last_known_good_revision
            and state.status == DeploymentStatus.MONITORING
        ):
            # We're back on the last known good revision while we were monitoring a new deploy
            # This means the new deploy was rolled back (externally or via Cloud Run)
            state.status = DeploymentStatus.ROLLED_BACK
            state.rolled_back_from_sha = state.current_sha
            state.rollback_reason = (
                "Revision reverted to last known good during monitoring window"
            )
            state.rollback_count += 1
            state.current_revision = _current_revision
            state.current_sha = _current_sha
            state.deployed_at = _utcnow()
            session.add(state)
            await session.commit()

            logger.warning(
                f"[DEPLOY] ROLLBACK DETECTED — reverted from {state.rolled_back_from_sha[:8]} to {_current_sha[:8]} "
                f"(consecutive rollbacks: {state.rollback_count})"
            )

            await _create_rollback_alerts(
                session, state.rolled_back_from_sha or "", _current_sha
            )
            await session.commit()

            if state.rollback_count >= 3:
                state.status = DeploymentStatus.HALTED
                state.halted_at = _utcnow()
                session.add(state)
                await session.commit()
                logger.error(
                    f"[DEPLOY] HALTED — {state.rollback_count} consecutive rollbacks, manual intervention required"
                )
                _runtime_config["max_concurrent_vms"] = 0
                await _create_halted_alerts(
                    session,
                    state.rollback_count,
                    state.last_known_good_revision,
                    state.halted_at,
                )
                await session.commit()
            return

        # New deploy detected — update revision tracking. Capture the prior
        # deploy's sha + timestamp first so investigation payloads can compute
        # the prior_deploy_sha..current_sha commit range without a live checkout.
        state.prior_deploy_sha = state.current_sha
        state.prior_deployed_at = state.deployed_at
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
            results_list = list(results)
            # E11: split the deque into 3 chunks and use the median rate as
            # the pre-deploy baseline. A single snapshot lets a transient
            # spike right before deploy poison the whole monitoring window
            # — rollback logic then either over-reads the baseline (skips
            # rollback when the deploy IS bad) or under-reads it (rolls
            # back code that's actually fine). 3-chunk median dampens
            # both directions while still picking up a real degradation.
            if len(results_list) >= 9:
                chunks = 3
                chunk_size = len(results_list) // chunks
                rates: list[float] = []
                for i in range(chunks):
                    start = i * chunk_size
                    end = start + chunk_size if i < chunks - 1 else len(results_list)
                    chunk = results_list[start:end]
                    if not chunk:
                        continue
                    chunk_failures = sum(1 for r in chunk if r[1] == "failure")
                    rates.append(chunk_failures / len(chunk))
                rates.sort()
                # Median of an odd-count list is rates[len // 2].
                deploy_monitoring["pre_deploy_failure_rate"] = rates[len(rates) // 2]
                deploy_monitoring["pre_deploy_failure_rate_samples"] = rates
            elif results_list:
                failures = sum(1 for r in results_list if r[1] == "failure")
                deploy_monitoring["pre_deploy_failure_rate"] = failures / len(
                    results_list
                )
                deploy_monitoring["pre_deploy_failure_rate_samples"] = [
                    deploy_monitoring["pre_deploy_failure_rate"]
                ]
            else:
                # No previous data (idle→active). Use generous baseline to avoid false rollbacks.
                deploy_monitoring["pre_deploy_failure_rate"] = 0.15
                deploy_monitoring["pre_deploy_failure_rate_samples"] = [0.15]
            deploy_monitoring["previous_window_results"] = list(results)
            deploy_monitoring["previous_window_start"] = deploy_monitoring.get(
                "window_start"
            )
            deploy_monitoring["task_results"] = deque(maxlen=100)
            deploy_monitoring["window_start"] = _utcnow()
            state.status = DeploymentStatus.MONITORING
        session.add(state)
        await session.commit()
        logger.info(
            f"[DEPLOY] New deploy detected — revision={_current_revision}, status={state.status}, starting monitoring window"
        )


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


def _get_broadcast_limit(design_doc: "dict | None" = None) -> int:
    """Return the broadcast limit for a project, falling back to the global default.

    Per-project override: store ``_broadcast_limit`` in project.design_doc.
    Set to 0 to opt a project out of system-wide broadcast alerts.
    """
    per_project = (design_doc or {}).get("_broadcast_limit")
    if per_project is not None:
        return int(per_project)
    return _runtime_config.get("project_broadcast_limit", 1000)


async def _create_orchestrator_error_alerts(
    session: AsyncSession, consecutive_errors: int
) -> None:
    """Create ERROR alerts on all projects when the orchestrator hits repeated errors (deduplicated)."""
    title = "Orchestrator error loop"
    msg = (
        f"The orchestrator has encountered {consecutive_errors} consecutive errors. "
        "The pipeline may be stalled. Check logs for details."
    )
    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning(
            "_create_orchestrator_error_alerts: project_ids capped at %d",
            global_limit,
        )
    existing_rows = (
        (
            await session.execute(
                select(Alert.project_id).where(
                    Alert.project_id.in_(project_ids),
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.ERROR,
                    Alert.dismissed_at.is_(None),
                    Alert.title == title,
                )
            )
        ).all()
        if project_ids
        else []
    )
    projects_with_alert = {row[0] for row in existing_rows}
    for pid in project_ids:
        if pid in projects_with_alert:
            continue
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.ERROR,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
        )


async def _create_halted_alerts(
    session: AsyncSession,
    rollback_count: int,
    last_known_good: str,
    halted_at: "datetime",
) -> None:
    """Create ERROR alerts on all projects when deployment enters HALTED state."""
    title = f"Deployment HALTED — {rollback_count} consecutive rollbacks, manual intervention required"
    msg = (
        f"Deployment halted after {rollback_count} consecutive rollbacks. "
        f"Last known good revision: {last_known_good or 'unknown'}. "
        f"Halted at: {halted_at.isoformat()}. "
        "Pipeline will auto-recover after 1h cooldown, or manually set max VMs > 0."
    )
    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning("_create_halted_alerts: project_ids capped at %d", global_limit)
    for pid in project_ids:
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.ERROR,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
        )
    await _send_webhook_notification("PIPELINE_HALTED", title, msg)


async def _create_rollback_alerts(
    session: AsyncSession, rolled_back_from: str, rolled_back_to: str
) -> None:
    """Create WARNING alerts on all projects when an automatic rollback is detected.
    Deduplicates: skips if an undismissed rollback alert already exists for a project."""
    title = "Automatic rollback detected — revision reverted to last known good"
    msg = (
        f"Deployment automatically rolled back from {rolled_back_from[:8]} "
        f"to {rolled_back_to[:8]}."
    )
    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning(
            "_create_rollback_alerts: project_ids capped at %d",
            global_limit,
        )
    existing_rows = (
        (
            await session.execute(
                select(Alert.project_id).where(
                    Alert.project_id.in_(project_ids),
                    Alert.task_id.is_(None),
                    Alert.dismissed_at.is_(None),
                    Alert.title.contains("Automatic rollback"),
                )
            )
        ).all()
        if project_ids
        else []
    )
    projects_with_alert = {row[0] for row in existing_rows}
    for pid in project_ids:
        if pid in projects_with_alert:
            continue
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.WARNING,
            title=title,
            message=msg,
        )
        session.add(alert)
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
        )


async def _create_halted_recovery_alerts(session: AsyncSession) -> None:
    """Create INFO alerts and dismiss existing HALTED ERROR alerts when deployment auto-recovers."""
    recovery_title = "Deployment auto-recovered from HALTED state after 1h cooldown"
    now = _utcnow()
    global_limit = _runtime_config.get("project_broadcast_limit", 1000)
    rows = (
        await session.execute(
            select(Project.id, Project.design_doc).limit(global_limit)
        )
    ).all()
    project_ids = [pid for pid, doc in rows if _get_broadcast_limit(doc) > 0]
    if len(rows) == global_limit:
        logger.warning(
            "_create_halted_recovery_alerts: project_ids capped at %d",
            global_limit,
        )
    # Single batched query for all undismissed HALTED ERROR alerts across all projects
    halted_alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id.in_(project_ids),
                    Alert.severity == AlertSeverity.ERROR,
                    Alert.dismissed_at.is_(None),
                    Alert.title.contains("HALTED"),
                )
            )
        )
        .scalars()
        .all()
    )
    halted_by_project: dict = {}
    for h in halted_alerts:
        halted_by_project.setdefault(h.project_id, []).append(h)
    for pid in project_ids:
        # Dismiss existing HALTED ERROR alerts for this project
        for h in halted_by_project.get(pid, []):
            h.dismissed_at = now
            session.add(h)
        # Create INFO recovery alert
        alert = Alert(
            project_id=pid,
            severity=AlertSeverity.INFO,
            title=recovery_title,
            message=recovery_title,
        )
        session.add(alert)
        await broadcast_to_project(
            str(pid),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "INFO", "title": alert.title},
        )


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
        from app.notification_dispatch import dispatch_external_notification
        from app.failure_classification import (
            classify_failure,
            classify_failure_taxonomy,
        )
        import asyncio as _asyncio

        ctx = task.context or {}
        payload = task.first_payload or {}
        direction_text = (
            ctx.get("direction_text") or payload.get("direction_text") or ""
        )
        pr_url = (
            ctx.get("pr_url") or ctx.get("rollup_pr_url") or payload.get("pr_url") or ""
        )
        _asyncio.ensure_future(  # noqa: RUF006  # fire-and-forget; caller does not await
            dispatch_external_notification(
                task.project_id,
                "TASK_FAILED",
                {
                    "task_id": str(task.id),
                    "task_title": task.title or str(task.id)[:8],
                    "task_type": task_type,
                    "error_snippet": error_snippet or "",
                    "failure_class": classify_failure(task),
                    "taxonomy": classify_failure_taxonomy(task),
                    "direction_id": ctx.get("direction_id") or "",
                    "direction_text": direction_text[:200],
                    "scope": ctx.get("scope") or payload.get("scope") or "",
                    "pr_url": pr_url,
                    "attempt_count": int(task.attempt_count or 0),
                },
            )
        )
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
        str(project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": title},
    )


app = FastAPI(
    title="Blueprint Orchestrator",
    description="Secure Orchestration & State Management",
    lifespan=lifespan,
)

# Telemetry must be set up before the app starts (cannot add middleware during lifespan)
setup_telemetry(app, engine)

app.include_router(admin_router)
app.include_router(admin_projects_router)
app.include_router(admin_settings_router)
app.include_router(admin_debug_router)
app.include_router(admin_pool_accounts_router)
app.include_router(admin_project_config_router)
app.include_router(agent_router)
app.include_router(agent_strategic_fetch_router)
app.include_router(agent_warm_continuity_router)
app.include_router(github_router)
app.include_router(projects_router)
app.include_router(directions_router)
app.include_router(deploy_router)
app.include_router(admin_users_router)
app.include_router(admin_notifications_router)
app.include_router(admin_analytics_router)
app.include_router(admin_digest_router)
app.include_router(admin_activity_router)
app.include_router(admin_health_router)
app.include_router(admin_orchestrator_errors_router)
app.include_router(admin_scope_measurement_router)
app.include_router(agent_status_router)
app.include_router(agent_credentials_router)
app.include_router(admin_onboard_router)
app.include_router(admin_agent_ops_router)


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
        from app.notification_dispatch import get_notification_stats

        return get_notification_stats()
    except Exception:
        return {}


@app.get("/api/debug/orchestrator")
async def debug_orchestrator(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Diagnostic endpoint: check if the orchestrator loop is alive."""
    from sqlalchemy import func

    queued = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False))
        )
    ).scalar() or 0
    active = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
        )
    ).scalar() or 0
    ago = None
    if _orch._orchestrator_last_cycle:
        ago = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()
    # Include deployment state
    deploy_state = None
    try:
        state = await session.get(DeploymentState, 1)
        if state:
            monitoring_since = _shared_state.deploy_monitoring.get("window_start")
            recent_results = list(
                _shared_state.deploy_monitoring.get("task_results", [])
            )[-10:]
            deploy_state = {
                "revision": state.current_revision,
                "sha": state.current_sha[:8] if state.current_sha else "",
                "status": state.status,
                "deployed_at": state.deployed_at.isoformat()
                if state.deployed_at
                else None,
                "last_known_good_sha": state.last_known_good_sha[:8]
                if state.last_known_good_sha
                else "",
                "rollback_count": state.rollback_count,
                "rollback_reason": state.rollback_reason,
                "pause_reason": state.pause_reason,
                "monitoring_since": monitoring_since.isoformat()
                if monitoring_since
                else None,
                "recent_outcomes": [
                    {
                        "created_at": r[0],
                        "status": r[1],
                        "failure_class": r[2] if len(r) > 2 else "",
                    }
                    for r in recent_results
                ],
            }
    except Exception as e:
        logger.warning(
            f"[DEBUG] Failed to fetch deployment state for /api/debug/orchestrator: {e}",
            exc_info=True,
        )

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
                    "disabled_at": a["disabled_at"].isoformat()
                    if a.get("disabled_at")
                    else None,
                    "reenable_at": (
                        a["disabled_at"] + timedelta(minutes=30)
                    ).isoformat()
                    if a.get("disabled_at")
                    else None,
                }
                for a in all_accts
            ],
        }

    # Liveness is judged by the in-cycle progress *heartbeat* (the shared
    # evaluate_orchestrator_liveness verdict, same as /health and the watchdog),
    # NOT by full-cycle-completion age. A slow-but-progressing cycle is alive and
    # not hung; only a worker with no heartbeat past ORCHESTRATOR_HANG_SECONDS (or
    # a first cycle that started but never heartbeat) is wedged. `ago` /
    # last_cycle_seconds_ago is preserved purely as an informational slow-cycle
    # signal. See incident 2026-05-28 / orchestrator-operator-health-split.
    liveness = _orch.evaluate_orchestrator_liveness(_utcnow())

    return {
        "orchestrator_alive": liveness.alive,
        "orchestrator_hung": liveness.stalled,
        "orchestrator_progress_age_seconds": liveness.progress_age,
        "orchestrator_cycle_age_seconds": ago,
        "orchestrator_liveness_reason": liveness.reason,
        "last_cycle_seconds_ago": ago,
        "queued_tasks": queued,
        "active_tasks": active,
        "max_concurrent_vms": _runtime_config["max_concurrent_vms"],
        "actual_gcp_vm_count": _last_gcp_vm_count,
        "gcp_vm_count_age_seconds": int(time.time() - _last_gcp_vm_check)
        if _last_gcp_vm_check
        else None,
        "pool_size": pool_snapshot["size"],
        "available_count": pool_snapshot["available"],
        "disabled_count": pool_snapshot["disabled"],
        "pool_accounts": pool_snapshot["accounts"],
        "deploy_state": deploy_state,
        "deploy_monitoring": {
            "window_active": _shared_state.deploy_monitoring["window_start"]
            is not None,
            "task_results_count": len(_shared_state.deploy_monitoring["task_results"]),
            "last_dispatch": _shared_state.deploy_monitoring[
                "last_dispatch_time"
            ].isoformat()
            if _shared_state.deploy_monitoring["last_dispatch_time"]
            else None,
            "last_completion": _shared_state.deploy_monitoring[
                "last_completion_time"
            ].isoformat()
            if _shared_state.deploy_monitoring["last_completion_time"]
            else None,
            "external_health": _shared_state.external_health,
        },
        "zone_health": _get_zone_health_summary(),
        "notification_stats": _get_notification_stats(),
    }


@app.get("/api/health/detailed")
async def health_detailed(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Authenticated endpoint returning structured health status for all system components."""
    import time
    from sqlalchemy import text
    from sqlalchemy import func

    components: Dict[str, Any] = {}

    # --- DB ---
    db_status = "ok"
    db_latency_ms: float | None = None
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
        "last_check": last_check.isoformat()
        if isinstance(last_check, datetime)
        else last_check,
    }

    provider_health = _shared_state.external_health.get("provider_health", {}) or {}
    dispatch_health = _shared_state.external_health.get("dispatch_health", {}) or {}
    components["provider_apis"] = {
        "status": dispatch_health.get("status", "unknown"),
        "fallback_enabled": dispatch_health.get("fallback_enabled", False),
        "required_providers": dispatch_health.get("required_providers", []),
        "available_providers": dispatch_health.get("available_providers", []),
        "degraded_providers": dispatch_health.get("degraded_providers", []),
        "down_providers": dispatch_health.get("down_providers", []),
        "providers": provider_health,
    }

    # --- Orchestrator ---
    # Primary verdict comes from the shared progress-heartbeat liveness predicate
    # (same as /health and the watchdog), NOT from full-cycle-completion age:
    #   ok        -> healthy (fresh heartbeat)
    #   degraded  -> alive + progressing but the last *completed* cycle is stale
    #                (slow cycle), an informational soft signal only
    #   down      -> genuinely wedged: no heartbeat past ORCHESTRATOR_HANG_SECONDS,
    #                or a first cycle that started but never made progress.
    # cycle_age is preserved as the informational slow-cycle marker.
    liveness = _orch.evaluate_orchestrator_liveness(_utcnow())
    cycle_age: float | None = None
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()
    slow_cycle = cycle_age is not None and cycle_age >= 120
    if liveness.healthy:
        orch_status = "degraded" if slow_cycle else "ok"
    elif liveness.stalled or not liveness.alive:
        orch_status = "down"
    else:
        orch_status = "degraded"
    try:
        queued = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.status == TaskStatus.QUEUED, Task.requires_approval.is_(False)
                )
            )
        ).scalar() or 0
        active = (
            await session.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS])
                )
            )
        ).scalar() or 0
    except Exception:
        queued = active = 0
    components["orchestrator"] = {
        "status": orch_status,
        "cycle_age_seconds": cycle_age,
        "progress_age_seconds": liveness.progress_age,
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
    available_accounts = sum(
        1 for a in _account_by_id.values() if a.get("enabled", True)
    )
    if total_accounts == 0:
        pool_status = "ok"  # not using Max Plan accounts
    elif available_accounts > 0:
        pool_status = "ok"
    else:
        pool_status = "down"
    by_provider: Dict[str, Dict[str, int]] = {}
    for account in _account_by_id.values():
        provider_name = account.get("provider", "anthropic")
        bucket = by_provider.setdefault(provider_name, {"total": 0, "available": 0})
        bucket["total"] += 1
        if account.get("enabled", True):
            bucket["available"] += 1
    components["max_plan_pool"] = {
        "status": pool_status,
        "total": total_accounts,
        "available": available_accounts,
        "by_provider": by_provider,
    }

    # --- Circuit Breakers ---
    breaker_states: Dict[str, Any] = {}
    for project_id, breaker in _shared_state.circuit_breakers.items():
        breaker_states[project_id] = {
            "state": breaker.get("state", "CLOSED"),
            "failure_count": breaker.get("failure_count", 0),
            "opened_at": breaker["opened_at"].isoformat()
            if breaker.get("opened_at")
            else None,
        }
    components["circuit_breaker"] = {"status": "ok", "projects": breaker_states}

    # --- Autonomous ---
    autonomous_enabled = _runtime_config.get("max_concurrent_vms", 0) > 0
    consecutive_failures = (
        sum(_shared_state.autonomous_failure_counts.values())
        if _shared_state.autonomous_failure_counts
        else 0
    )
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

    # --- Viewport Debt ---
    _vp_open = [
        RegressionCaseStatus.ACTIVE,
        RegressionCaseStatus.REMEDIATING,
        RegressionCaseStatus.MERGED,
    ]
    try:
        _vp_total = (
            await session.execute(
                select(func.count())
                .select_from(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.regression_class.like("VIEWPORT:%"),
                    RegressionRemediationCase.status.in_(_vp_open),
                )
            )
        ).scalar() or 0
        _vp_attributed = (
            await session.execute(
                select(func.count())
                .select_from(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.regression_class.like("VIEWPORT:%"),
                    RegressionRemediationCase.status.in_(_vp_open),
                    RegressionRemediationCase.introducing_pr_number.isnot(None),
                )
            )
        ).scalar() or 0
    except Exception:
        _vp_total = _vp_attributed = 0
    components["viewport_debt"] = {
        "status": "ok" if _vp_total == 0 else "degraded",
        "open_cases": _vp_total,
        "attributed_cases": _vp_attributed,
    }

    # --- Overall Status ---
    statuses = [
        c["status"]
        for c in components.values()
        if isinstance(c, dict) and "status" in c
    ]
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


@app.get("/api/admin/viewport-failures/download-url")
async def get_viewport_failure_screenshot_download_url(
    project_id: uuid.UUID,
    component: str,
    width: int,
    current_user: dict = Depends(get_current_user),
):
    """Generate a 1-hour signed download URL for a viewport failure screenshot.

    The GCS object path is reconstructed to exactly mirror what the agent
    uploads via POST /api/agent/viewport/failure-screenshot-upload-url
    (viewport-failures/{project_id}/{component}-{width}.png), so the regression
    panel can display screenshots from the stable per-project/component/width
    path without a per-task DB lookup. The component is sanitized with the same
    rule the upload endpoint applies so the two paths always agree.
    """
    import datetime as _dt
    import re as _re

    from google.cloud import storage as _gcs

    from app.storage import ARTIFACT_BUCKET_NAME, GCP_PROJECT_ID

    safe_component = _re.sub(r"[^A-Za-z0-9._-]", "_", component)[:100]
    object_name = f"viewport-failures/{project_id}/{safe_component}-{width}.png"

    def _generate() -> str:
        blob = (
            _gcs.Client(project=GCP_PROJECT_ID)
            .bucket(ARTIFACT_BUCKET_NAME)
            .blob(object_name)
        )
        return blob.generate_signed_url(
            version="v4", expiration=_dt.timedelta(hours=1), method="GET"
        )

    try:
        url = await asyncio.to_thread(_generate)
    except Exception as exc:
        logger.warning(
            "[VIEWPORT] signed download URL failed for %s: %s", object_name, exc
        )
        raise HTTPException(status_code=503, detail="Artifact storage unavailable")
    return {"url": url}


@app.get("/health")
async def health_check():
    """Unauthenticated health endpoint for external monitoring (Cloud Function, load balancer)."""
    from app.shared_state import external_health

    # Non-leader instances don't run the orchestrator — they're healthy HTTP servers
    # but should be clearly distinguished from a leader with a stale/missing cycle.
    if not _shared_state.is_leader:
        return JSONResponse(
            status_code=200,
            content={
                "status": "healthy",
                "is_leader": False,
                "revision": _current_revision,
            },
        )

    # Liveness is gated on the worker's in-cycle progress *heartbeat*, NOT on
    # full-cycle completion. A cycle's wall time scales with the EVALUATING
    # backlog (O(N) CI polling); gating liveness on a fixed 60s full-cycle
    # window killed a slow-but-healthy worker, discarding its progress and
    # spiralling into a Cloud Run restart loop that froze the whole pipeline
    # (incident 2026-05-28). Now only a genuinely wedged worker — no heartbeat
    # for ORCHESTRATOR_HANG_SECONDS — is reported unhealthy (→ liveness restart).
    liveness = _orch.evaluate_orchestrator_liveness(_utcnow())
    orchestrator_alive = liveness.alive
    orchestrator_healthy = liveness.healthy
    progress_age = liveness.progress_age

    # cycle_age = time since the last *completed* cycle. Still surfaced and used
    # as a soft "slow cycle" degraded signal for alerting, but it no longer
    # removes the instance from the LB or triggers a liveness kill.
    cycle_age = None
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()
    slow_cycle = cycle_age is not None and cycle_age >= 120

    # Detect hung orchestrator (cycle started but not completed in 5+ minutes)
    orchestrator_hung = False
    if (
        _orch._orchestrator_cycle_started
        and _orch._orchestrator_last_cycle
        and _orch._orchestrator_cycle_started > _orch._orchestrator_last_cycle
    ):
        hung_seconds = (_utcnow() - _orch._orchestrator_cycle_started).total_seconds()
        orchestrator_hung = hung_seconds > 300

    # Only DB is critical for 503 — provider or external API outages shouldn't remove us from LB.
    critical_healthy = external_health.get("db", True)
    degraded_deps = [
        dep for dep in (external_health.get("failed_deps", []) or []) if dep != "db"
    ]
    if orchestrator_hung:
        degraded_deps.append("orchestrator_hung")
    if slow_cycle:
        degraded_deps.append("orchestrator_slow_cycle")

    healthy = orchestrator_healthy and critical_healthy
    status = (
        "healthy"
        if healthy and not degraded_deps
        else "degraded"
        if healthy
        else "unhealthy"
    )
    status_code = 200 if healthy else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": status,
            "is_leader": True,
            "revision": _current_revision,
            "orchestrator_alive": orchestrator_alive,
            "orchestrator_cycle_age_seconds": cycle_age,
            "orchestrator_progress_age_seconds": progress_age,
            "external_deps": {
                k: v
                for k, v in external_health.items()
                if k
                not in (
                    "last_check",
                    "failed_deps",
                    "errors",
                    "provider_health",
                    "dispatch_health",
                )
            },
            "degraded_deps": degraded_deps,
        },
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

    cycle_age: float | None = None
    if _orch._orchestrator_last_cycle:
        cycle_age = (_utcnow() - _orch._orchestrator_last_cycle).total_seconds()

    active_vms = sum(a.get("active_tasks", 0) for a in _account_by_id.values())
    consecutive_failures = (
        sum(_shared_state.autonomous_failure_counts.values())
        if _shared_state.autonomous_failure_counts
        else 0
    )

    lines: list[str] = []

    def gauge(
        name: str, help_text: str, value: float, labels: Dict[str, str] | None = None
    ) -> None:
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

    gauge(
        "blueprint_tasks_queued",
        "Number of tasks currently queued",
        _shared_state.tasks_queued_gauge,
    )
    gauge(
        "blueprint_tasks_active",
        "Number of tasks currently provisioning or in progress",
        _shared_state.tasks_active_gauge,
    )
    counter(
        "blueprint_tasks_completed_total",
        "Cumulative number of completed tasks",
        _shared_state.tasks_completed_total,
    )
    counter(
        "blueprint_tasks_failed_total",
        "Cumulative number of failed tasks",
        _shared_state.tasks_failed_total,
    )
    gauge(
        "blueprint_orchestrator_cycle_age_seconds",
        "Seconds since last orchestrator cycle",
        cycle_age if cycle_age is not None else -1,
    )
    gauge(
        "blueprint_active_vms", "Current active VM count from Max Plan pool", active_vms
    )

    if _shared_state.circuit_breakers:
        lines.append(
            "# HELP blueprint_circuit_breaker_open 1 if circuit breaker is OPEN, 0 otherwise"
        )
        lines.append("# TYPE blueprint_circuit_breaker_open gauge")
        for project_id, breaker in _shared_state.circuit_breakers.items():
            is_open = 1 if breaker.get("state") == "OPEN" else 0
            lines.append(
                f'blueprint_circuit_breaker_open{{project_id="{project_id}"}} {is_open}'
            )

    gauge(
        "blueprint_autonomous_consecutive_failures",
        "Consecutive autonomous task failures",
        consecutive_failures,
    )

    # DB pool invalidation telemetry (handle_error event listener in
    # database.py). Gives us a direct signal whether the cutover-safe
    # connection-eject pattern is firing — lets a future Cloud Run rollout
    # be measured against the 2026-05-04 cascade baseline.
    import contextlib

    with contextlib.suppress(Exception):
        from app.database import get_db_pool_invalidation_stats

        _db_stats = get_db_pool_invalidation_stats()
        counter(
            "blueprint_db_pool_invalidations_total",
            "Cumulative pool connection invalidations from handle_error listener",
            _db_stats.get("invalidate_total", 0),
        )
        gauge(
            "blueprint_db_pool_invalidate_consecutive_failures",
            "Consecutive failures of connection.invalidate() calls (escalates to pool.dispose at threshold)",
            _db_stats.get("invalidate_consecutive_failures", 0),
        )

    # _orchestrator_deleted_vms tracker size — observability into the in-memory
    # delete-tracker that drives reaper preemption-classification correctness.
    with contextlib.suppress(Exception):
        gauge(
            "blueprint_orchestrator_deleted_vms_size",
            "Number of entries in the orchestrator-initiated VM delete tracker",
            len(_orchestrator_deleted_vms),
        )

    # Reaper liveness — counter increments only on a fully-successful cycle.
    # Production 2026-05-04: reaper crashed every cycle for 80+ minutes
    # (MissingGreenlet at reaper.py:724) before operator noticed via stuck
    # task heartbeat ages. Alert on ``last_cycle_age_seconds > 300`` to
    # catch any reaper crash-loop within ~5 successful-cycle windows.
    with contextlib.suppress(Exception):
        from app.reaper import get_reaper_stats

        _reaper_stats = get_reaper_stats()
        counter(
            "blueprint_reaper_cycles_completed_total",
            "Cumulative number of fully-successful reaper cycles",
            _reaper_stats.get("cycles_completed_total", 0),
        )
        gauge(
            "blueprint_reaper_last_cycle_age_seconds",
            "Seconds since last fully-successful reaper cycle (-1 if never completed)",
            _reaper_stats.get("last_cycle_age_seconds", -1),
        )

    body = "\n".join(lines) + "\n"
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        content=body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )


# Attach the rate limiter to the app
app.state.limiter = limiter


def _apply_cors_headers(resp: JSONResponse, request: Request) -> JSONResponse:
    """Echo the request Origin onto an error response so the browser surfaces
    the real status code instead of a phantom CORS failure.

    Starlette's CORSMiddleware only adds Access-Control-Allow-Origin to
    responses produced by route handlers — exception-handler responses run
    outside it. Cloud Run's frontend then injects a wildcard `*`, which is
    incompatible with `allow_credentials=true` and the browser rejects it.
    Authenticated callers thus see every 429/5xx as "No 'Access-Control-Allow-
    Origin' header is present" rather than the real status. Echoing the
    specific origin (or `*` when explicitly allowed) restores the actual
    error to the client.

    allowed_origins is defined further down in this module and mutated per
    deployment via ALLOWED_ORIGINS env; resolve at call time.
    """
    origin = request.headers.get("origin")
    if origin and origin in allowed_origins:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Vary"] = "Origin"
    elif "*" in allowed_origins:
        # Non-credentialed wildcard only — browsers forbid Allow-Credentials with *.
        resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _cors_aware_rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """slowapi's default 429 handler + CORS headers via _apply_cors_headers.

    Frontend bursts (parallel initial-load fetches) are the typical trigger.
    """
    resp = JSONResponse(
        status_code=429,
        content={
            "error": {"code": 429, "message": f"Rate limit exceeded: {exc.detail}"}
        },
        headers={"Retry-After": "5"},
    )
    return _apply_cors_headers(resp, request)


app.add_exception_handler(RateLimitExceeded, _cors_aware_rate_limit_handler)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    resp = JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}},
    )
    return _apply_cors_headers(resp, request)


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
    resp = JSONResponse(
        status_code=422,
        content={
            "error": {"code": 422, "message": "Validation Error", "details": errors}
        },
    )
    return _apply_cors_headers(resp, request)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(
        f"[UNHANDLED] {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
        exc_info=True,
    )
    resp = JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {str(exc)[:200]}"},
    )
    return _apply_cors_headers(resp, request)


# Add CORS Middleware so the frontend can call these APIs
# In production, set ALLOWED_ORIGINS to the specific frontend domain (e.g., "https://my-app.com")
# Multiple origins can be comma-separated.
def _resolve_allowed_origins(env_value: str) -> list[str]:
    """Parse ALLOWED_ORIGINS env value and reject any wildcard entry.

    A '*' anywhere in the list (whether ALLOWED_ORIGINS='*' or
    ALLOWED_ORIGINS='*,https://foo.com') combined with our
    allow_credentials=True CORS config would let Starlette's CORSMiddleware
    echo arbitrary origins back with credentials — the bypass this guard
    prevents.
    """
    if not env_value:
        return [
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
        ]
    parsed = [origin.strip() for origin in env_value.split(",") if origin.strip()]
    if any(o == "*" for o in parsed):
        raise ValueError(
            "ALLOWED_ORIGINS may not contain '*' when Firebase auth is configured: "
            "a wildcard origin with allow_credentials=True lets any site send "
            "authenticated requests on behalf of logged-in users. "
            "Set ALLOWED_ORIGINS to a comma-separated list of explicit origins "
            f"(e.g. 'https://my-app.web.app,http://localhost:3000'). Got: {env_value!r}"
        )
    return parsed


allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins = _resolve_allowed_origins(allowed_origins_env)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "X-Requested-With",
    ],
)


@app.get("/")
def read_root():
    return {"status": "online", "service": "Blueprint Orchestrator"}


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
    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
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
