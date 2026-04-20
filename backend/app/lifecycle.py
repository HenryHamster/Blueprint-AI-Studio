"""Application lifecycle management — startup, shutdown, and background-task monitoring.

Extracted from app.main to reduce module size.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict

from sqlalchemy import func as _func
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.database import AsyncSessionLocal, init_db
from app.models import AllowedUser, Alert, AlertSeverity, Project
from app.time_utils import _utcnow

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
    # Late imports to avoid circular dependency with app.main
    from app.main import (
        _verify_schema_version,
        _init_deployment_state,
        _send_webhook_notification,
    )
    from app.reaper import reaper_service
    from app.services import gcp_cleanup_service, secret_reconciliation_service, stale_branch_cleanup_service, auto_archive_service
    from app.max_plan_pool import load_max_plan_pool
    from app.orchestrator import orchestrator_worker
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

    # Start the Reaper Service background task
    reaper_task = asyncio.create_task(reaper_service())

    # Start the GCP Cleanup Service background task
    gcp_cleanup_task = asyncio.create_task(gcp_cleanup_service())

    # Start Secret Reconciliation Service (orphaned agent-creds-* cleanup)
    secret_recon_task = asyncio.create_task(secret_reconciliation_service())

    # Start Stale Branch Cleanup Service (removes old blueprint/research/plan branches)
    stale_branch_task = asyncio.create_task(stale_branch_cleanup_service())

    # Start Auto-Archive Service (archives terminal tasks older than N days)
    auto_archive_task = asyncio.create_task(auto_archive_service())

    # Start the Orchestrator Worker background task
    orchestrator_task = asyncio.create_task(orchestrator_worker())

    # Store references for admin restart endpoint
    from app.shared_state import background_tasks as _bg_tasks
    _bg_tasks["orchestrator"] = orchestrator_task
    _bg_tasks["reaper"] = reaper_task
    _bg_tasks["gcp_cleanup"] = gcp_cleanup_task
    _bg_tasks["secret_reconciliation"] = secret_recon_task
    _bg_tasks["stale_branch_cleanup"] = stale_branch_task
    _bg_tasks["auto_archive"] = auto_archive_task

    # Monitor background tasks for silent crashes (with exponential backoff)
    async def _monitor_background_tasks():
        tasks = _bg_tasks
        restart_state: Dict[str, Dict] = {}  # name -> {attempts, last_restart}
        task_factories = {
            "orchestrator": orchestrator_worker,
            "reaper": reaper_service,
            "gcp_cleanup": gcp_cleanup_service,
            "secret_reconciliation": secret_reconciliation_service,
            "stale_branch_cleanup": stale_branch_cleanup_service,
            "auto_archive": auto_archive_service,
        }
        while True:
            try:
                await asyncio.sleep(30)
                for name, t in list(tasks.items()):
                    if t.done():
                        exc = t.exception() if not t.cancelled() else None
                        logger.error(f"[MONITOR] Background task '{name}' DIED! Exception: {exc}")
                        asyncio.create_task(_send_webhook_notification("SERVICE_CRASHED", f"Background service crashed: {name}", str(exc)[:500]))
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
                                        message=f"Background service '{name}' crashed with: {str(exc)[:300]}. Auto-restarting with backoff.",
                                    )
                                    alert_session.add(alert)
                                await alert_session.commit()
                        except Exception as alert_exc:
                            logger.warning(f"[MONITOR] Failed to create crash alert: {alert_exc}")
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
    tasks_to_cancel = [reaper_task, gcp_cleanup_task, secret_recon_task, stale_branch_task, orchestrator_task, monitor_task]
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
