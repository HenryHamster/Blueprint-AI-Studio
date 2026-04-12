"""Deploy health monitoring: monitoring windows, external dependency checks, auto-pause/resume.

Extracted from app.main to reduce module size. Contains:
- should_rollback: causation analysis for rollback decisions
- _get_deploy_state_for_update: DB accessor with FOR UPDATE
- _check_monitoring_window: post-deploy monitoring window logic
- _check_external_health: checks DB, GitHub API, GCP, Claude API health
- _create_auto_pause_alerts / _create_auto_resume_alerts: alert helpers
- _check_pause_mode: auto-pause on critical failures, auto-resume on recovery
- broadcast_deploy_state: WebSocket broadcast wrapper
"""

import logging
import os
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.broadcast import broadcast_to_project, manager
from app.database import AsyncSessionLocal
from app.models import Alert, AlertSeverity, DeploymentState, DeploymentStatus, Project, Task
from app.max_plan_pool import (
    _account_by_id,
    _account_available_for_model,
    recheck_disabled_accounts,
)
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger("blueprint")


def _utcnow() -> datetime:
    """Naive UTC timestamp, compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_zone_health_summary() -> dict:
    """Get zone health summary from compute module, with fallback if not in GCP mode."""
    try:
        from app.compute import get_zone_health_summary
        return get_zone_health_summary()
    except Exception:
        return {"total_zones": 0, "available": 0, "exhausted": []}


# ---------------------------------------------------------------------------
# Module-level state for deploy health monitoring
# ---------------------------------------------------------------------------

_pause_failure_streak: int = 0   # consecutive health check cycles with critical failures
_health_recovery_streak: int = 0  # consecutive healthy cycles while PAUSED (gate for auto-resume)
_last_exhausted_zones: set = set()  # tracks zone health for transition logging


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
    global _last_exhausted_zones
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


async def _create_auto_pause_alerts(session: AsyncSession, failed_deps: list) -> None:
    """Create WARNING alerts on all projects when the pipeline auto-pauses due to external dep failures."""
    from app.main import _send_webhook_notification  # noqa: PLC0415 — deferred to avoid circular import
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


async def broadcast_deploy_state():
    """Assemble and broadcast current deploy state to all connected WebSocket clients."""
    try:
        from app.admin import build_deploy_state_payload
        async with AsyncSessionLocal() as session:
            payload = await build_deploy_state_payload(session)
        await manager.broadcast_to_all("deploy_state", payload)
    except Exception as e:
        logger.warning(f"[WS] Failed to broadcast deploy state: {e}")
