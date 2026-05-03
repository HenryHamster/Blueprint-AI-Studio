"""Application lifecycle management — startup, shutdown, and background-task monitoring.

Extracted from app.main to reduce module size.
"""

import asyncio
import ctypes
import logging
import os
import warnings
from contextlib import asynccontextmanager
from typing import Dict

from sqlalchemy import func as _func, text as _text
from sqlalchemy.orm import load_only
from sqlmodel import select

import app.shared_state as _shared_state
from app.database import AsyncSessionLocal, init_db
from app.models import AllowedUser, Alert, AlertSeverity, Project
from app.time_utils import _utcnow

# Session-scoped advisory lock ID for background-leader election.
# Stored as signed int64 (PostgreSQL bigint) via two's-complement conversion.
_BACKGROUND_LOCK_ID: int = ctypes.c_int64(0xB1EB1EB1EB1EB1EB).value

logger = logging.getLogger("blueprint")


async def _seed_initial_admin():
    """Seed the AllowedUser table with an initial admin if empty."""
    async with AsyncSessionLocal() as seed_session:
        count = (await seed_session.execute(select(_func.count()).select_from(AllowedUser))).scalar_one()
        if count == 0:
            initial_email = os.getenv("INITIAL_ADMIN_EMAIL")
            if initial_email:
                seed_session.add(AllowedUser(email=initial_email, role="admin"))
                await seed_session.commit()
                logger.info(f"[STARTUP] Seeded initial admin: {initial_email}")
            else:
                logger.warning("[STARTUP] AllowedUser table is empty and INITIAL_ADMIN_EMAIL not set. No users can sign in.")


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan context manager — startup and shutdown hooks."""
    # Root logger was never configured — named loggers propagated to a root with
    # no handlers, so every [ORCHESTRATOR]/[DEPLOY]/[HEALTH]/[MONITOR] line
    # vanished silently. Cloud Run captures stdout+stderr; force a basic
    # formatter+handler here so app logs surface even during startup hangs.
    if not logging.root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(name)s] %(levelname)s %(message)s',
            datefmt='%Y-%m-%dT%H:%M:%S',
            force=True,
        )
    # Route Python warnings (RuntimeWarning, DeprecationWarning, etc.) through the
    # logging system so they get a proper severity in Cloud Logging. Otherwise
    # they're written raw to stderr with no severity tag and end up classified as
    # INFO/DEFAULT, invisible to the orchestrator-errors dashboard which filters
    # severity>=ERROR. RuntimeWarnings in particular almost always indicate real
    # bugs (unawaited coroutines, sync code in async contexts) so they're elevated
    # to ERROR; everything else stays at WARNING.
    _warnings_logger = logging.getLogger("py.warnings")

    def _show_warning_via_logger(message, category, filename, lineno, file=None, line=None):
        formatted = warnings.formatwarning(message, category, filename, lineno, line).rstrip()
        if issubclass(category, RuntimeWarning):
            _warnings_logger.error(formatted)
        else:
            _warnings_logger.warning(formatted)

    warnings.showwarning = _show_warning_via_logger
    logger.info("[STARTUP] Logging configured — app logs now visible in Cloud Logging")

    # Late imports to avoid circular dependency with app.main
    from app.main import (
        _verify_schema_version,
        _init_deployment_state,
        _send_webhook_notification,
    )
    from app.reaper import reaper_service
    from app.services import (
        account_availability_refresh_service,
        auto_archive_service,
        gcp_cleanup_service,
        gcp_image_cleanup_service,
        secret_reconciliation_service,
        stale_branch_cleanup_service,
    )
    from app.max_plan_pool import load_max_plan_pool
    from app.orchestrator import orchestrator_worker
    from app.shared_state import runtime_config_sync_service
    from app.github import close_github_client

    # Startup: Initialize DB tables
    await init_db()

    # Verify schema version matches expected migration
    await _verify_schema_version()

    # Seed initial admin if AllowedUser table is empty
    await _seed_initial_admin()

    # Load persisted Max plan accounts into in-memory pool
    await load_max_plan_pool()

    # Check deployment state (detect new deploy, rollback, or normal restart)
    await _init_deployment_state()

    # --- Leader election via pg_try_advisory_lock (session-scoped) ---
    # Only one Cloud Run instance should run the leader-only background services.
    # We hold a dedicated session open for the entire lifespan; the lock is released
    # automatically when the session/connection closes at shutdown.
    leader_session = AsyncSessionLocal()
    await leader_session.__aenter__()
    try:
        _result = await leader_session.execute(
            _text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": _BACKGROUND_LOCK_ID},
        )
        is_leader = bool(_result.scalar())
    except Exception:
        # Non-Postgres dialect (SQLite in tests) — default to leader so all
        # services start normally; close the dummy session immediately.
        is_leader = True
        await leader_session.aclose()
        leader_session = None

    _shared_state.is_leader = is_leader

    if not is_leader:
        logger.info("[STARTUP] Not background-leader; skipping leader-only services (serving HTTP only)")
        # Per-instance services still run on non-leader instances.
        config_sync_task = asyncio.create_task(runtime_config_sync_service())
        from app.shared_state import background_tasks as _bg_tasks_nl
        _bg_tasks_nl["config_sync"] = config_sync_task

        # Minimal watchdog: restart per-instance services if they crash.
        async def _monitor_non_leader():
            while True:
                try:
                    await asyncio.sleep(30)
                    if config_sync_task.done():
                        exc = config_sync_task.exception() if not config_sync_task.cancelled() else None
                        logger.error(f"[MONITOR] config_sync DIED on non-leader (exc={exc}); restarting")
                        _bg_tasks_nl["config_sync"] = asyncio.create_task(runtime_config_sync_service())
                except asyncio.CancelledError:
                    break
                except Exception as _e:
                    logger.warning(f"[MONITOR] Non-leader watchdog error: {_e}")
                    await asyncio.sleep(10)

        nl_monitor_task = asyncio.create_task(_monitor_non_leader())

        yield

        nl_monitor_task.cancel()
        config_sync_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(nl_monitor_task, config_sync_task, return_exceptions=True),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[SHUTDOWN] Non-leader tasks didn't finish within 10s")
        await close_github_client()
        from app.redis_client import close_redis
        await close_redis()
        return

    # This instance owns the background-leader lock — start all background services.

    # Start the Reaper Service background task
    reaper_task = asyncio.create_task(reaper_service())

    # Start the GCP Cleanup Service background task
    gcp_cleanup_task = asyncio.create_task(gcp_cleanup_service())

    # Start the GCP Image Pruner (keeps agent image family under the per-project quota)
    gcp_image_cleanup_task = asyncio.create_task(gcp_image_cleanup_service())

    # Start Secret Reconciliation Service (orphaned agent-creds-* cleanup)
    secret_recon_task = asyncio.create_task(secret_reconciliation_service())

    # Start Stale Branch Cleanup Service (removes old blueprint/research/plan branches)
    stale_branch_task = asyncio.create_task(stale_branch_cleanup_service())

    # Start Auto-Archive Service (archives terminal tasks older than N days)
    auto_archive_task = asyncio.create_task(auto_archive_service())

    # Start the Orchestrator Worker background task
    orchestrator_task = asyncio.create_task(orchestrator_worker())

    # Per-instance runtime_config sync — propagates admin mutations across
    # Cloud Run instances (each has its own in-memory dict; DB is shared).
    config_sync_task = asyncio.create_task(runtime_config_sync_service())

    # Refresh per-account model-availability caches every 30 min so a
    # provider-side preview-access change doesn't sit in dispatch rotation
    # for hours (audit E13).
    availability_refresh_task = asyncio.create_task(account_availability_refresh_service())

    # Store references for admin restart endpoint
    from app.shared_state import background_tasks as _bg_tasks
    _bg_tasks["orchestrator"] = orchestrator_task
    _bg_tasks["reaper"] = reaper_task
    _bg_tasks["gcp_cleanup"] = gcp_cleanup_task
    _bg_tasks["gcp_image_cleanup"] = gcp_image_cleanup_task
    _bg_tasks["secret_reconciliation"] = secret_recon_task
    _bg_tasks["stale_branch_cleanup"] = stale_branch_task
    _bg_tasks["auto_archive"] = auto_archive_task
    _bg_tasks["config_sync"] = config_sync_task
    _bg_tasks["availability_refresh"] = availability_refresh_task

    # Monitor background tasks for silent crashes (with exponential backoff)
    async def _monitor_background_tasks():
        tasks = _bg_tasks
        restart_state: Dict[str, Dict] = {}  # name -> {attempts, last_restart}
        # When the watchdog cancels a hung task, it stashes the reason here so the
        # death-detection branch below can include it in the alert/log instead of
        # the useless "Exception: None" that a bare cancellation produces.
        cancel_reasons: Dict[str, str] = {}
        task_factories = {
            "orchestrator": orchestrator_worker,
            "reaper": reaper_service,
            "gcp_cleanup": gcp_cleanup_service,
            "gcp_image_cleanup": gcp_image_cleanup_service,
            "secret_reconciliation": secret_reconciliation_service,
            "stale_branch_cleanup": stale_branch_cleanup_service,
            "auto_archive": auto_archive_service,
            "config_sync": runtime_config_sync_service,
            "availability_refresh": account_availability_refresh_service,
        }
        while True:
            try:
                await asyncio.sleep(30)
                # Liveness watchdog for orchestrator: task may be alive (not done())
                # yet stuck inside a cycle — `t.done()` alone misses hangs. If
                # `_orchestrator_last_cycle` hasn't advanced in ~120s while the task
                # is still running, cancel so the normal restart path below kicks in.
                try:
                    from app import orchestrator as _orch  # noqa: PLC0415
                    orch_task = tasks.get("orchestrator")
                    if orch_task is not None and not orch_task.done():
                        last = _orch._orchestrator_last_cycle
                        started = _orch._orchestrator_cycle_started
                        stall_threshold = 120.0
                        now = _utcnow()
                        stalled = False
                        reason = ""
                        if last is None and started is not None:
                            age = (now - started).total_seconds()
                            if age > stall_threshold:
                                stalled = True
                                reason = f"first cycle never completed (started {int(age)}s ago, no completion)"
                        elif last is not None:
                            age = (now - last).total_seconds()
                            if age > stall_threshold:
                                stalled = True
                                reason = f"no cycle completion in {int(age)}s"
                        if stalled:
                            logger.error(
                                f"[MONITOR] Orchestrator appears hung — {reason}. "
                                f"Cancelling task so restart path re-spawns it."
                            )
                            cancel_reasons["orchestrator"] = f"watchdog cancelled: {reason}"
                            orch_task.cancel()
                except Exception as wd_exc:
                    logger.warning(f"[MONITOR] Watchdog check failed: {wd_exc}")
                for name, t in list(tasks.items()):
                    if t.done():
                        exc = t.exception() if not t.cancelled() else None
                        # A bare cancellation (t.cancelled() with no exception) is
                        # almost always our own watchdog firing — surface the reason
                        # we stashed at cancel time instead of the misleading "None".
                        cancel_reason = cancel_reasons.pop(name, None)
                        if exc is not None:
                            cause_str = str(exc)[:500]
                        elif cancel_reason:
                            cause_str = cancel_reason
                        else:
                            cause_str = "task cancelled (no exception)"
                        logger.error(f"[MONITOR] Background task '{name}' DIED! Cause: {cause_str}")
                        asyncio.create_task(_send_webhook_notification("SERVICE_CRASHED", f"Background service crashed: {name}", cause_str))
                        try:
                            async with AsyncSessionLocal() as alert_session:
                                projects = (await alert_session.execute(
                                    select(Project).options(load_only(Project.id))
                                )).scalars().all()
                                alert_title = f"Background service crashed: {name}"
                                for project in projects:
                                    existing = (await alert_session.execute(
                                        select(Alert).where(
                                            Alert.project_id == project.id,
                                            Alert.title == alert_title,
                                            Alert.dismissed_at.is_(None),
                                        )
                                    )).scalars().first()
                                    if existing:
                                        continue
                                    alert = Alert(
                                        project_id=project.id,
                                        severity=AlertSeverity.ERROR,
                                        title=alert_title,
                                        message=f"Background service '{name}' crashed with: {cause_str[:300]}. Auto-restarting with backoff.",
                                    )
                                    alert_session.add(alert)
                                await alert_session.commit()
                        except Exception as alert_exc:
                            logger.warning(f"[MONITOR] Failed to create crash alert: {alert_exc}")
                        factory = task_factories.get(name)
                        # Always restart cancelled tasks too — the watchdog
                        # above intentionally cancels hung tasks expecting
                        # this path to respawn them. Previously the
                        # `not t.cancelled()` guard left cancelled tasks
                        # dead forever, spamming DIED alerts every 30s with
                        # no recovery. Shutdown cancellation is handled
                        # separately (the monitor loop itself is cancelled
                        # during shutdown, exiting before reaching here).
                        if factory:
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
                            try:
                                async with AsyncSessionLocal() as alert_session:
                                    projects = (await alert_session.execute(
                                        select(Project).options(load_only(Project.id))
                                    )).scalars().all()
                                    for project in projects:
                                        alert = Alert(
                                            project_id=project.id,
                                            severity=AlertSeverity.INFO,
                                            title=f"Background service restarted: {name}",
                                            message=f"Service '{name}' was automatically restarted (attempt #{state['attempts']}).",
                                        )
                                        alert_session.add(alert)
                                    await alert_session.commit()
                            except Exception as alert_exc:
                                logger.warning(f"[MONITOR] Failed to create restart alert: {alert_exc}")
            except Exception as mon_exc:
                logger.error(f"[MONITOR] Monitor itself hit exception: {mon_exc}", exc_info=True)
                await asyncio.sleep(10)  # Brief pause before retrying
    monitor_task = asyncio.create_task(_monitor_background_tasks())

    yield
    # Shutdown: graceful cleanup with timeout to let in-flight operations finish
    tasks_to_cancel = [reaper_task, gcp_cleanup_task, gcp_image_cleanup_task, secret_recon_task, stale_branch_task, orchestrator_task, config_sync_task, monitor_task]
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
    from app.redis_client import close_redis
    await close_redis()
    # Release the session-scoped advisory lock by closing the dedicated session.
    if leader_session is not None:
        try:
            await leader_session.aclose()
        except Exception:
            pass
