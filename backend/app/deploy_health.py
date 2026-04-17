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
from collections import deque
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.broadcast import broadcast_to_project, manager
from app.database import AsyncSessionLocal
from app.models import Alert, AlertSeverity, DeploymentState, DeploymentStatus, Project
from app.max_plan_pool import recheck_disabled_accounts
from app.health_check_primitives import (
    check_db_health,
    check_github_health,
    check_claude_api_health,
    check_gcp_health,
    log_zone_health_transitions,
)
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")


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
_last_quota_probe: "datetime | None" = None  # gate probe_rate_limit to every 5min
# Ring buffer of the last 5 health check results while PAUSED (True == healthy).
# Auto-resume fires once >= 3 of the last 5 are healthy, tolerating a single flaky probe.
_HEALTH_HISTORY_SIZE = 5
_HEALTH_HISTORY_HEALTHY_REQUIRED = 3
_health_recovery_history: "deque[bool]" = deque(maxlen=_HEALTH_HISTORY_SIZE)
_last_exhausted_zones: set = set()  # tracks zone health for transition logging
_last_auto_resume_at: "datetime | None" = None  # timestamp of most recent auto-resume (normal or forced)


def _reset_health_recovery_history() -> None:
    """Clear the recovery ring buffer (called on pause, on resume, and on manual reset)."""
    _health_recovery_history.clear()


def _restore_health_streaks(snapshot: dict) -> None:
    """Restore module-level pause/recovery state from a persisted zone_health_state snapshot.

    Called once at startup. Tolerates missing keys, legacy int history format, and
    invalid ISO timestamps — corrupt values fall back to safe defaults (0 / empty / None).
    """
    global _pause_failure_streak, _last_auto_resume_at
    _pause_failure_streak = int(snapshot.get("_pause_failure_streak") or 0)
    history = snapshot.get("_health_recovery_history")
    _health_recovery_history.clear()
    if isinstance(history, list):
        for v in history[-_HEALTH_HISTORY_SIZE:]:
            _health_recovery_history.append(bool(v))
    _last_auto_resume_at = None
    raw = snapshot.get("_last_auto_resume_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            _last_auto_resume_at = parsed
        except ValueError:
            logger.warning(f"[DEPLOY] Invalid _last_auto_resume_at in persisted state: {raw!r}")


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
                # Create ERROR alert on all projects
                rollback_title = f"Rollback recommended: {reason}"
                projects = (await session.execute(select(Project))).scalars().all()
                for project in projects:
                    alert = Alert(
                        project_id=project.id,
                        severity=AlertSeverity.ERROR,
                        title=rollback_title,
                        message=(
                            f"Deploy monitoring detected degraded performance. "
                            f"Rollback to previous revision is recommended. Reason: {reason}"
                        ),
                    )
                    session.add(alert)
                    await broadcast_to_project(
                        str(project.id),
                        "alert_created",
                        {"alert_id": str(alert.id), "severity": "ERROR", "title": rollback_title},
                    )
                    try:
                        from app.notification_dispatch import post_rollback_recommendation_message  # noqa: PLC0415
                        await post_rollback_recommendation_message(session, project.id, reason)
                    except Exception as _me:
                        logger.warning(f"[NOTIFY] post_rollback_recommendation_message failed: {_me}")
                await session.commit()
                logger.warning(f"[DEPLOY] ROLLBACK RECOMMENDED: {reason}")
                logger.info('deployment_monitor_state_change', extra={
                    'old_status': str(state.status),
                    'new_status': str(state.status),
                    'rollback_triggered': True,
                })
                # Dispatch external webhook notifications (fire-and-forget)
                try:
                    import asyncio as _asyncio  # noqa: PLC0415
                    from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
                    for project in projects:
                        _asyncio.ensure_future(dispatch_external_notification(
                            project.id,
                            "ROLLBACK_RECOMMENDED",
                            {"reason": reason, "revision": state.current_revision},
                        ))
                except Exception as _ne:
                    logger.warning(f"[NOTIFY] ROLLBACK_RECOMMENDED dispatch setup failed: {_ne}")
                # Broadcast updated deploy state (includes rollback_reason)
                await broadcast_deploy_state()
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

    result = await check_db_health(ext)
    if result:
        failed.append(result)

    result = await check_github_health(ext, now)
    if result:
        failed.append(result)

    result = check_claude_api_health(ext)
    if result:
        failed.append(result)

    result = check_gcp_health(ext)
    if result:
        failed.append(result)

    ext["failed_deps"] = failed

    # Probe /rate_limit to keep github_quota warm for the dashboard pill.
    # Gated to every 5 minutes — the probe itself is free against the
    # install-token 5000/hr bucket, but running it every 60s from the
    # health cycle adds ~60 calls/hr against whatever token we use.
    # 5min cadence = 12 probes/hr, immaterial to the install budget.
    global _last_quota_probe
    if _last_quota_probe is None or (now - _last_quota_probe).total_seconds() >= 300:
        _last_quota_probe = now
        try:
            from app.github import probe_rate_limit  # noqa: PLC0415
            await probe_rate_limit()
        except Exception:
            logger.debug("[HEALTH] GitHub rate_limit probe failed", exc_info=True)

    # Track consecutive failure counts per dependency (used by dispatch gate)
    consec = ext.setdefault("consecutive_failures", {})
    for dep in ("github_api", "claude_api", "gcp", "db"):
        if dep in failed:
            consec[dep] = consec.get(dep, 0) + 1
        else:
            consec[dep] = 0

    # Re-enable rate-limited accounts after cooldown
    await recheck_disabled_accounts()

    # Log zone health transitions (available ↔ exhausted)
    zone_summary = _get_zone_health_summary()
    current_exhausted = set(zone_summary.get("exhausted", []))
    log_zone_health_transitions(current_exhausted, _last_exhausted_zones, zone_summary)
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
    try:
        import asyncio as _asyncio  # noqa: PLC0415
        from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415
        for project in projects:
            _asyncio.ensure_future(dispatch_external_notification(
                project.id,
                "DEPLOY_HEALTH_ALERT",
                {
                    "failed_deps": failed_deps,
                    "title": title,
                },
            ))
    except Exception as _ne:
        logger.warning(f"[NOTIFY] DEPLOY_HEALTH_ALERT dispatch setup failed: {_ne}")


async def _create_forced_resume_alerts(session: AsyncSession, pause_duration_seconds: float, failed_deps: list) -> None:
    """CRITICAL alerts when we force a resume after max_auto_pause_duration_seconds elapsed."""
    pause_title = "Pipeline auto-paused: external dependencies down"
    title = "Pipeline force-resumed: max auto-pause duration exceeded"
    msg = (
        f"Pipeline force-resumed after {int(pause_duration_seconds)}s paused "
        f"(max {_runtime_config['max_auto_pause_duration_seconds']}s). "
        f"Health probes still report failures: {', '.join(failed_deps) if failed_deps else 'none'}. "
        "Resuming with reduced VM count (1) — operators should investigate."
    )
    now = _utcnow()
    projects = (await session.execute(select(Project))).scalars().all()
    for project in projects:
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
    """Auto-pause when critical external deps fail, auto-resume when they recover.

    Recovery uses a 3-of-5 ring buffer to tolerate a single flaky probe. A max
    auto-pause duration forces resume (with reduced VMs) if healthy probes never
    reach the threshold — preventing indefinite pause under intermittent failures.
    """
    global _pause_failure_streak, _last_auto_resume_at
    ext = _shared_state.external_health
    failed = ext.get("failed_deps", [])
    # Auto-pause on critical deps including Claude API (rate limits, not just missing credentials)
    critical_failures = [d for d in failed if d in ("db", "github_api", "gcp", "claude_api")]
    is_healthy = not critical_failures

    if critical_failures:
        _pause_failure_streak += 1
        # Require 3 consecutive failures before pausing (avoid flaky single checks)
        if _pause_failure_streak < 3:
            logger.warning(f"[PAUSE] Health check failure {_pause_failure_streak}/3: {', '.join(critical_failures)} — not pausing yet")
            try:
                await manager.broadcast_to_all("health_degrading", {
                    "streak": _pause_failure_streak,
                    "threshold": 3,
                    "failed_deps": critical_failures,
                })
            except Exception as _be:
                logger.warning(f"[WS] health_degrading broadcast failed: {_be}")
            # Still record into recovery history while PAUSED so a single flake doesn't reset progress
    else:
        _pause_failure_streak = 0

    # If we're not paused from our own auto-pause, handle the pause-trigger path.
    async with AsyncSessionLocal() as session:
        state = await _get_deploy_state_for_update(session)
        if state is None:
            return

        is_auto_paused = (
            state.status == DeploymentStatus.PAUSED
            and state.pre_pause_max_vms is not None
            and (state.pause_reason or "").startswith("External dependency down")
        )

        # --- Pause-trigger path -------------------------------------------------
        if not is_auto_paused:
            _reset_health_recovery_history()
            in_grace_period = False
            grace_remaining = 0.0
            if _last_auto_resume_at is not None:
                grace = _runtime_config.get("auto_resume_grace_period_seconds", 300)
                elapsed = (_utcnow() - _last_auto_resume_at).total_seconds()
                if 0 <= elapsed < grace:
                    in_grace_period = True
                    grace_remaining = grace - elapsed

            if critical_failures and _pause_failure_streak >= 3 and _runtime_config["max_concurrent_vms"] > 0 and in_grace_period:
                logger.warning(
                    f"[PAUSE] Suppressing auto-pause during post-resume grace period "
                    f"({int(grace_remaining)}s remaining); failed={critical_failures}"
                )
                try:
                    await manager.broadcast_to_all("health_degrading", {
                        "streak": _pause_failure_streak,
                        "threshold": 3,
                        "failed_deps": critical_failures,
                        "in_grace_period": True,
                        "grace_remaining_seconds": int(grace_remaining),
                    })
                except Exception as _be:
                    logger.warning(f"[WS] health_degrading broadcast failed: {_be}")
            elif critical_failures and _pause_failure_streak >= 3 and _runtime_config["max_concurrent_vms"] > 0:
                if state.status not in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED):
                    # During ramp-up after a prior auto-resume, _runtime_config holds the
                    # intermediate (small) VM count while state.runtime_max_concurrent_vms
                    # holds the original pre-pause target. Preserve the larger so nested
                    # pause cycles don't ratchet capacity down.
                    original_capacity = max(
                        _runtime_config["max_concurrent_vms"],
                        state.runtime_max_concurrent_vms or 0,
                    )
                    state.status = DeploymentStatus.PAUSED
                    state.pause_reason = f"External dependency down: {', '.join(critical_failures)}"
                    state.paused_at = _utcnow()
                    state.pre_pause_max_vms = original_capacity
                    state.runtime_max_concurrent_vms = original_capacity
                    session.add(state)
                    await _create_auto_pause_alerts(session, critical_failures)
                    await session.commit()
                    _runtime_config["max_concurrent_vms"] = 0
                    logger.warning(f"[PAUSE] Auto-paused pipeline — {', '.join(critical_failures)} down")
                    await broadcast_deploy_state()
            elif (
                not critical_failures
                and state.status in (DeploymentStatus.MONITORING, DeploymentStatus.STABLE)
                and state.pre_pause_max_vms is None
                and state.runtime_max_concurrent_vms is not None
                and 0 < _runtime_config["max_concurrent_vms"] < state.runtime_max_concurrent_vms
            ):
                # Gradual ramp-up after auto-resume: DB holds the target, runtime starts low.
                target = state.runtime_max_concurrent_vms
                current = _runtime_config["max_concurrent_vms"]
                step = min(2, target - current)
                _runtime_config["max_concurrent_vms"] = current + step
                logger.info(f"[RAMP] max_concurrent_vms {current} → {current + step} (target: {target})")
                await broadcast_deploy_state()
            return

        # --- Recovery path (we are in our own auto-pause) ----------------------
        _health_recovery_history.append(is_healthy)
        history_snapshot = list(_health_recovery_history)
        healthy_count = sum(1 for x in history_snapshot if x)

        # Forced-resume after max auto-pause duration
        max_duration = _runtime_config.get("max_auto_pause_duration_seconds", 7200)
        pause_duration = 0.0
        if state.paused_at is not None:
            paused_at = state.paused_at
            now = _utcnow()
            # _utcnow() is naive UTC; normalize both sides to match.
            if paused_at.tzinfo is not None:
                paused_at = paused_at.replace(tzinfo=None)
            pause_duration = (now - paused_at).total_seconds()

        should_force_resume = pause_duration >= max_duration > 0
        should_normal_resume = (
            len(history_snapshot) >= _HEALTH_HISTORY_HEALTHY_REQUIRED
            and healthy_count >= _HEALTH_HISTORY_HEALTHY_REQUIRED
        )

        if not (should_force_resume or should_normal_resume):
            logger.info(
                f"[PAUSE] Recovery progress: {healthy_count}/{len(history_snapshot)} healthy "
                f"(need {_HEALTH_HISTORY_HEALTHY_REQUIRED}-of-{_HEALTH_HISTORY_SIZE}); "
                f"paused {int(pause_duration)}s/{max_duration}s"
            )
            return

        restore = state.pre_pause_max_vms or 1
        if should_force_resume:
            initial_vms = 1
        else:
            initial_vms = min(2, restore)

        state.status = DeploymentStatus.MONITORING if _shared_state.deploy_monitoring["window_start"] else DeploymentStatus.STABLE
        state.pause_reason = None
        state.paused_at = None
        state.pre_pause_max_vms = None
        # Always persist the ORIGINAL target as the ramp-up target, even on forced
        # resume. Previously forced resume stored initial_vms (=1), which caused
        # nested pause cycles to ratchet capacity down permanently.
        state.runtime_max_concurrent_vms = restore
        session.add(state)
        if should_force_resume:
            await _create_forced_resume_alerts(session, pause_duration, critical_failures)
        else:
            await _create_auto_resume_alerts(session)
        await session.commit()
        _runtime_config["max_concurrent_vms"] = initial_vms
        _reset_health_recovery_history()
        _last_auto_resume_at = _utcnow()

        if should_force_resume:
            logger.error(
                f"[PAUSE] FORCE-RESUMED after {int(pause_duration)}s paused (max {max_duration}s) "
                f"with {initial_vms} VM(s). Health history: {history_snapshot}. "
                f"Still-failing deps: {critical_failures or 'none'}",
                extra={
                    "alert": True,
                    "type": "forced_resume",
                    "pause_duration_seconds": int(pause_duration),
                    "max_auto_pause_duration_seconds": max_duration,
                    "health_history": history_snapshot,
                    "failed_deps": critical_failures,
                },
            )
        else:
            logger.info(
                f"[PAUSE] Auto-resumed pipeline with {initial_vms} VMs (target: {restore}) "
                f"after {int(pause_duration)}s paused. Health history: {history_snapshot}"
            )
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
