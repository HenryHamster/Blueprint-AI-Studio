"""Deploy health monitoring: monitoring windows, external dependency checks, auto-pause/resume.

Extracted from app.main to reduce module size. Contains:
- should_rollback: causation analysis for rollback decisions
- _get_deploy_state_for_update: DB accessor with FOR UPDATE
- _check_monitoring_window: post-deploy monitoring window logic
- _check_external_health: checks DB, GitHub API, GCP, and provider-aware dispatch health
- _create_auto_pause_alerts / _create_auto_resume_alerts: alert helpers
- _check_pause_mode: auto-pause on critical failures, auto-resume on recovery
- broadcast_deploy_state: WebSocket broadcast wrapper
"""

import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.broadcast import broadcast_to_project, manager
from app.database import AsyncSessionLocal
from app.models import Alert, AlertSeverity, DeploymentState, DeploymentStatus, Project
from app.max_plan_pool import recheck_disabled_accounts
from app.health_check_primitives import (
    check_db_health,
    check_github_health,
    check_provider_api_health,
    check_gcp_health,
    get_dispatch_health_snapshot,
    log_zone_health_transitions,
)
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow
from app.deploy_health_alerts import (  # noqa: F401
    _build_rate_limit_details,
    _create_auto_pause_alerts,
    _create_forced_resume_alerts,
    _create_auto_resume_alerts as _create_auto_resume_alerts_impl,
)

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

_pause_failure_streak: int = 0  # consecutive health check cycles with critical failures
_last_quota_probe: "datetime | None" = None  # gate probe_rate_limit to every 5min
# Ring buffer of the last 5 health check results while PAUSED (True == healthy).
# Auto-resume fires once >= 3 of the last 5 are healthy, tolerating a single flaky probe.
_HEALTH_HISTORY_SIZE = 5
_HEALTH_HISTORY_HEALTHY_REQUIRED = 3
_health_recovery_history: "deque[bool]" = deque(maxlen=_HEALTH_HISTORY_SIZE)
_last_exhausted_zones: set = set()  # tracks zone health for transition logging
_last_auto_resume_at: "datetime | None" = (
    None  # timestamp of most recent auto-resume (normal or forced)
)
# True when the most recent auto-resume was forced (max pause duration exceeded).
# Forced resume fires precisely because health checks never stabilized, so ramp-up past
# 2 VMs additionally requires evidence of real task success (not just healthy probes).
_last_resume_was_forced: bool = False
# Timestamps of recent auto-resumes, used to detect pause/resume flapping.
# Pruned to entries within `flap_window_seconds`; escalation kicks in when size
# >= `flap_threshold`.
_recent_auto_resumes: "deque[datetime]" = deque()
_FLAP_ESCALATED_HEALTHY_REQUIRED = _HEALTH_HISTORY_SIZE  # 5-of-5 when flapping
# Counts consecutive forced resumes. Force-resumes happen at max_auto_pause_duration
# intervals (default 2 hours), far outside the 30-min flap window, so flap detection
# misses them. A sustained force-resume loop indicates persistent infrastructure
# failure; escalate the alert once this threshold is crossed.
_consecutive_force_resumes: int = 0
_FORCE_RESUME_ESCALATION_THRESHOLD = 3


def _reset_health_recovery_history() -> None:
    """Clear the recovery ring buffer (called on pause, on resume, and on manual reset)."""
    _health_recovery_history.clear()


def get_recovery_history_snapshot() -> list:
    """Return a copy of the current health-recovery ring buffer (True == healthy probe).

    Other modules use this to substitute health-probe evidence for task-result
    evidence while the pipeline is paused (no tasks run, so task_results stops
    growing). Returns at most ``_HEALTH_HISTORY_SIZE`` entries.
    """
    return list(_health_recovery_history)


def _prune_flap_window(now: datetime) -> None:
    """Drop _recent_auto_resumes entries older than the configured flap window."""
    window = _runtime_config.get("flap_window_seconds", 1800)
    while (
        _recent_auto_resumes
        and (now - _recent_auto_resumes[0]).total_seconds() > window
    ):
        _recent_auto_resumes.popleft()


def _is_flapping(now: datetime) -> bool:
    """Return True if recent auto-resume count within the window meets the threshold."""
    _prune_flap_window(now)
    threshold = _runtime_config.get("flap_threshold", 3)
    return len(_recent_auto_resumes) >= threshold


def get_recovery_threshold_state(now: datetime | None = None) -> tuple[int, bool]:
    """Return the current healthy-probe requirement and whether flap escalation is active."""
    current_time = now or _utcnow()
    flap_escalated = _is_flapping(current_time)
    healthy_required = (
        _FLAP_ESCALATED_HEALTHY_REQUIRED
        if flap_escalated
        else _HEALTH_HISTORY_HEALTHY_REQUIRED
    )
    return healthy_required, flap_escalated


def _restore_health_streaks(snapshot: dict) -> None:
    """Restore module-level pause/recovery state from a persisted zone_health_state snapshot.

    Called once at startup. Tolerates missing keys, legacy int history format, and
    invalid ISO timestamps — corrupt values fall back to safe defaults (0 / empty / None).
    """
    global \
        _pause_failure_streak, \
        _last_auto_resume_at, \
        _last_resume_was_forced, \
        _consecutive_force_resumes
    _pause_failure_streak = int(snapshot.get("_pause_failure_streak") or 0)
    _last_resume_was_forced = bool(snapshot.get("_last_resume_was_forced") or False)
    _consecutive_force_resumes = int(snapshot.get("_consecutive_force_resumes") or 0)
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
            logger.warning(
                f"[DEPLOY] Invalid _last_auto_resume_at in persisted state: {raw!r}"
            )
    _recent_auto_resumes.clear()
    raw_resumes = snapshot.get("_recent_auto_resumes")
    if isinstance(raw_resumes, list):
        for v in raw_resumes:
            if not isinstance(v, str):
                continue
            try:
                parsed = datetime.fromisoformat(v)
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                _recent_auto_resumes.append(parsed)
            except ValueError:
                logger.warning(f"[DEPLOY] Invalid _recent_auto_resumes entry: {v!r}")


def _has_recent_task_success(minutes: int = 5) -> bool:
    """True if deploy_monitoring.task_results has any 'success' entry within the window."""
    cutoff = (_utcnow() - timedelta(minutes=minutes)).isoformat()
    for r in _shared_state.deploy_monitoring.get("task_results", ()):
        if len(r) >= 2 and r[1] == "success" and r[0] >= cutoff:
            return True
    return False


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
        return (
            False,
            f"Insufficient data ({len(results)} results, need 5+)",
            "SELF_HEAL",
        )

    # 1. Are we already on last_known_good? Rollback won't help.
    # (This is checked by the caller against DeploymentState)

    # 2. Were things already broken before this deploy?
    pre_rate = deploy_monitoring.get("pre_deploy_failure_rate", 0.0)
    if pre_rate > 0.3:
        return (
            False,
            f"System was already failing before deploy (pre-deploy rate: {pre_rate:.0%})",
            "SELF_HEAL",
        )

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
        return (
            False,
            "All failures are external (provision/API), not code errors",
            "PAUSE",
        )

    # 5. Check if failure rate is significantly higher than pre-deploy
    if post_failure_rate <= pre_rate + 0.1:
        return (
            False,
            f"Failure rate unchanged ({post_failure_rate:.0%} vs {pre_rate:.0%} pre-deploy)",
            "SELF_HEAL",
        )

    # 6. High code error rate after deploy — likely caused by the deploy
    if len(post_code_errors) >= 3 and post_failure_rate > 0.5:
        return (
            True,
            f"High code error rate after deploy: {post_failure_rate:.0%} ({len(post_code_errors)} code errors)",
            "ROLLBACK",
        )

    # 7. 100% failure rate is a strong signal
    if post_failure_rate == 1.0 and len(results) >= 3:
        return (
            True,
            f"100% failure rate across {len(results)} tasks after deploy",
            "ROLLBACK",
        )

    # Not enough signal to attribute to deploy
    return (
        False,
        f"Failure rate {post_failure_rate:.0%} — monitoring, not yet attributable",
        "SELF_HEAL",
    )


async def _get_deploy_state_for_update(
    session: AsyncSession,
) -> "DeploymentState | None":
    """Fetch DeploymentState with FOR UPDATE SKIP LOCKED.

    skip_locked=True returns None when the row is already locked by a concurrent
    writer instead of blocking indefinitely. Callers already tolerate None (they
    return early and retry on the next cycle), and an unkillable FOR UPDATE wait
    in the orchestrator cold path was silently hanging entire instances when a
    prior OOM'd transaction left the row locked.
    """
    return (
        (
            await session.execute(
                select(DeploymentState)
                .where(DeploymentState.id == 1)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )


async def _check_monitoring_window():
    """Called each orchestrator cycle. If monitoring window has elapsed without issues, mark STABLE."""
    from app.shared_state import deploy_monitoring

    window_start = deploy_monitoring.get("window_start")
    if not window_start:
        return

    duration = (
        deploy_monitoring["extended_window"]
        if deploy_monitoring["is_extended"]
        else deploy_monitoring["window_duration"]
    )
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
                    logger.info(
                        "[DEPLOY] Monitoring idle timeout — no tasks ran, marking STABLE"
                    )
                    logger.info(
                        "deployment_monitor_state_change",
                        extra={
                            "old_status": str(old_status),
                            "new_status": str(state.status),
                            "rollback_triggered": False,
                        },
                    )
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
                project_ids = (
                    (await session.execute(select(Project.id).limit(1000))).scalars().all()
                )
                for project_id in project_ids:
                    alert = Alert(
                        project_id=project_id,
                        severity=AlertSeverity.ERROR,
                        title=rollback_title,
                        message=(
                            f"Deploy monitoring detected degraded performance. "
                            f"Rollback to previous revision is recommended. Reason: {reason}"
                        ),
                        event_type="deployment.rollback_recommended",
                    )
                    session.add(alert)
                    await broadcast_to_project(
                        str(project_id),
                        "alert_created",
                        {
                            "alert_id": str(alert.id),
                            "severity": "ERROR",
                            "title": rollback_title,
                        },
                    )
                    try:
                        from app.notification_dispatch import (
                            post_rollback_recommendation_message,
                        )  # noqa: PLC0415

                        await post_rollback_recommendation_message(
                            session, project_id, reason
                        )
                    except Exception as _me:
                        logger.warning(
                            f"[NOTIFY] post_rollback_recommendation_message failed: {_me}"
                        )
                await session.commit()
                logger.warning(f"[DEPLOY] ROLLBACK RECOMMENDED: {reason}")
                logger.info(
                    "deployment_monitor_state_change",
                    extra={
                        "old_status": str(state.status),
                        "new_status": str(state.status),
                        "rollback_triggered": True,
                    },
                )
                # Dispatch external webhook notifications (fire-and-forget)
                try:
                    import asyncio as _asyncio  # noqa: PLC0415
                    from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415

                    for project_id in project_ids:
                        _asyncio.ensure_future(
                            dispatch_external_notification(
                                project_id,
                                "ROLLBACK_RECOMMENDED",
                                {"reason": reason, "revision": state.current_revision},
                            )
                        )
                except Exception as _ne:
                    logger.warning(
                        f"[NOTIFY] ROLLBACK_RECOMMENDED dispatch setup failed: {_ne}"
                    )
                # Broadcast updated deploy state (includes rollback_reason)
                await broadcast_deploy_state()
            else:
                # No rollback target available — stay in MONITORING, don't promote to STABLE
                state.rollback_reason = (
                    f"Issues detected but no rollback target: {reason}"
                )
                session.add(state)
                await session.commit()
                logger.warning(
                    "[DEPLOY] Issues detected but no rollback target — staying in MONITORING"
                )
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
            logger.info(
                f"[DEPLOY] Monitoring window passed — marked STABLE (revision={state.current_revision})"
            )
            logger.info(
                "deployment_monitor_state_change",
                extra={
                    "old_status": str(old_status),
                    "new_status": str(state.status),
                    "rollback_triggered": False,
                },
            )

    deploy_monitoring["window_start"] = None


async def _check_external_health():
    """Periodic check of external dependencies. Updates shared_state.external_health."""
    global _last_exhausted_zones
    ext = _shared_state.external_health
    now = _utcnow()

    # Adaptive interval: 15s while the pipeline is auto-paused (max_concurrent_vms == 0),
    # 60s otherwise. During pause no tasks dispatch and the GitHub App JWT budget
    # (5,000 req/hr) is idle, so faster probing cuts the 3-of-5 ring-buffer recovery
    # from ~3 min to ~45 s. Worst-case 240 calls/hr is well within budget.
    interval = 15 if _runtime_config.get("max_concurrent_vms", 0) == 0 else 60
    if ext["last_check"] and (now - ext["last_check"]).total_seconds() < interval:
        return
    ext["last_check"] = now
    failed = []

    result = await check_db_health(ext)
    if result:
        failed.append(result)

    result = await check_github_health(ext, now)
    if result:
        failed.append(result)

    for provider_name in ("anthropic", "openai", "gemini", "zai", "deepseek"):
        result = check_provider_api_health(ext, provider_name)
        if result:
            failed.append(result)

    dispatch_health = get_dispatch_health_snapshot(ext)
    ext["dispatch_health"] = dispatch_health
    ext["dispatch_api"] = dispatch_health["status"] != "down"
    if not ext["dispatch_api"]:
        ext.setdefault("errors", {})["dispatch_api"] = (
            "no dispatchable provider path remains"
        )
        failed.append("dispatch_api")
    else:
        ext.setdefault("errors", {}).pop("dispatch_api", None)

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
    for dep in (
        "github_api",
        "anthropic_api",
        "openai_api",
        "gemini_api",
        "zai_api",
        "deepseek_api",
        "dispatch_api",
        "gcp",
        "db",
    ):
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


async def _create_auto_resume_alerts(
    session: AsyncSession, flapping: bool = False
) -> None:
    """Create INFO or ERROR alerts when the pipeline auto-resumes."""
    await _create_auto_resume_alerts_impl(
        session,
        flapping=flapping,
        recent_resume_count=len(_recent_auto_resumes),
    )


async def _check_pause_mode():
    """Auto-pause when critical external deps fail, auto-resume when they recover.

    Recovery uses a 3-of-5 ring buffer to tolerate a single flaky probe. A max
    auto-pause duration forces resume (with reduced VMs) if healthy probes never
    reach the threshold — preventing indefinite pause under intermittent failures.
    """
    global \
        _pause_failure_streak, \
        _last_auto_resume_at, \
        _last_resume_was_forced, \
        _consecutive_force_resumes
    ext = _shared_state.external_health
    failed = ext.get("failed_deps", [])
    # Auto-pause only when critical infrastructure is down or no dispatchable provider path remains.
    critical_failures = [
        d for d in failed if d in ("db", "github_api", "gcp", "dispatch_api")
    ]
    is_healthy = not critical_failures

    if critical_failures:
        _pause_failure_streak += 1
        # Require 3 consecutive failures before pausing (avoid flaky single checks)
        if _pause_failure_streak < 3:
            logger.warning(
                f"[PAUSE] Health check failure {_pause_failure_streak}/3: {', '.join(critical_failures)} — not pausing yet"
            )
            try:
                await manager.broadcast_to_all(
                    "health_degrading",
                    {
                        "streak": _pause_failure_streak,
                        "threshold": 3,
                        "failed_deps": critical_failures,
                    },
                )
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
                # Double the grace period while flapping to damp rapid re-pauses.
                if _is_flapping(_utcnow()):
                    grace *= 2
                elapsed = (_utcnow() - _last_auto_resume_at).total_seconds()
                if 0 <= elapsed < grace:
                    in_grace_period = True
                    grace_remaining = grace - elapsed

            if (
                critical_failures
                and _pause_failure_streak >= 3
                and _runtime_config["max_concurrent_vms"] > 0
                and in_grace_period
            ):
                logger.warning(
                    f"[PAUSE] Suppressing auto-pause during post-resume grace period "
                    f"({int(grace_remaining)}s remaining); failed={critical_failures}"
                )
                try:
                    await manager.broadcast_to_all(
                        "health_degrading",
                        {
                            "streak": _pause_failure_streak,
                            "threshold": 3,
                            "failed_deps": critical_failures,
                            "in_grace_period": True,
                            "grace_remaining_seconds": int(grace_remaining),
                        },
                    )
                except Exception as _be:
                    logger.warning(f"[WS] health_degrading broadcast failed: {_be}")
            elif (
                critical_failures
                and _pause_failure_streak >= 3
                and _runtime_config["max_concurrent_vms"] > 0
            ):
                if state.status not in (
                    DeploymentStatus.PAUSED,
                    DeploymentStatus.HALTED,
                ):
                    # During ramp-up after a prior auto-resume, _runtime_config holds the
                    # intermediate (small) VM count while state.runtime_max_concurrent_vms
                    # holds the original pre-pause target. Preserve the larger so nested
                    # pause cycles don't ratchet capacity down.
                    # Anomaly throttling may have already reduced both runtime config and
                    # DB state below the true original capacity — consult the anomaly
                    # pre-adjustment stash so restoration doesn't permanently shrink.
                    anomaly_pre = (
                        _shared_state.anomaly_adjustments.get("pre_adjustment_max_vms")
                        or 0
                    )
                    original_capacity = max(
                        _runtime_config["max_concurrent_vms"],
                        state.runtime_max_concurrent_vms or 0,
                        anomaly_pre,
                    )
                    from app import pause_clock  # noqa: PLC0415 — local import to avoid cycle at module load

                    state.status = DeploymentStatus.PAUSED
                    state.pause_reason = (
                        f"External dependency down: {', '.join(critical_failures)}"
                    )
                    pause_clock.mark_paused(state, pause_clock.PAUSED_KIND_AUTO)
                    state.pre_pause_max_vms = original_capacity
                    state.runtime_max_concurrent_vms = original_capacity
                    session.add(state)
                    await _create_auto_pause_alerts(session, critical_failures)
                    await session.commit()
                    _runtime_config["max_concurrent_vms"] = 0
                    logger.warning(
                        f"[PAUSE] Auto-paused pipeline — {', '.join(critical_failures)} down"
                    )
                    await broadcast_deploy_state()
            elif (
                not critical_failures
                and state.status
                in (DeploymentStatus.MONITORING, DeploymentStatus.STABLE)
                and state.pre_pause_max_vms is None
                and state.runtime_max_concurrent_vms is not None
                and 0
                < _runtime_config["max_concurrent_vms"]
                < state.runtime_max_concurrent_vms
            ):
                # Gradual ramp-up after auto-resume: DB holds the target, runtime starts low.
                target = state.runtime_max_concurrent_vms
                current = _runtime_config["max_concurrent_vms"]
                # After a forced resume, health probes alone are weak signal — they're what
                # failed to stabilize in the first place. Gate ramp-up past 2 VMs on evidence
                # of a real task succeeding in the last 5 minutes.
                if (
                    _last_resume_was_forced
                    and current >= 2
                    and not _has_recent_task_success(minutes=5)
                ):
                    logger.info(
                        f"[RAMP] forced-resume gate: holding max_concurrent_vms at {current} "
                        f"(target: {target}) — no task success in last 5 minutes"
                    )
                    return
                step = min(2, target - current)
                _runtime_config["max_concurrent_vms"] = current + step
                logger.info(
                    f"[RAMP] max_concurrent_vms {current} → {current + step} (target: {target})"
                )
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

        # When flapping, tighten the recovery bar so we don't resume on marginal signal.
        healthy_required, flapping_now = get_recovery_threshold_state()

        should_force_resume = pause_duration >= max_duration > 0
        should_normal_resume = (
            len(history_snapshot) >= healthy_required
            and healthy_count >= healthy_required
        )

        if not (should_force_resume or should_normal_resume):
            tracked_deps = (
                "db",
                "github_api",
                "gcp",
                "dispatch_api",
                "anthropic_api",
                "openai_api",
                "gemini_api",
                "zai_api",
                "deepseek_api",
            )
            consec_map = ext.get("consecutive_failures", {}) or {}
            failed_set = set(failed)
            still_failing = {
                d: consec_map.get(d, 0) for d in tracked_deps if d in failed_set
            }
            recovered = [d for d in tracked_deps if d not in failed_set]
            logger.info(
                f"[PAUSE] Recovery progress: {healthy_count}/{len(history_snapshot)} healthy "
                f"(need {healthy_required}-of-{_HEALTH_HISTORY_SIZE}"
                f"{' FLAPPING' if flapping_now else ''}); "
                f"paused {int(pause_duration)}s/{max_duration}s; "
                f"healthy_deps={recovered or 'none'}; "
                f"still_failing={still_failing or 'none'}"
            )
            try:
                await manager.broadcast_to_all(
                    "health_recovering",
                    {
                        "healthy_count": healthy_count,
                        "history_len": len(history_snapshot),
                        "healthy_required": healthy_required,
                        "history_size": _HEALTH_HISTORY_SIZE,
                        "flapping": flapping_now,
                        "pause_duration_seconds": int(pause_duration),
                        "max_auto_pause_duration_seconds": max_duration,
                        "healthy_deps": recovered,
                        "still_failing_deps": still_failing,
                    },
                )
            except Exception as _be:
                logger.warning(f"[WS] health_recovering broadcast failed: {_be}")
            return

        restore = state.pre_pause_max_vms or 1
        if should_force_resume:
            initial_vms = 1
        else:
            initial_vms = min(2, restore)

        from app import pause_clock  # noqa: PLC0415 — local import to avoid cycle at module load

        state.status = (
            DeploymentStatus.MONITORING
            if _shared_state.deploy_monitoring["window_start"]
            else DeploymentStatus.STABLE
        )
        state.pause_reason = None
        pause_clock.accumulate_pause_window(state)
        state.pre_pause_max_vms = None
        # Always persist the ORIGINAL target as the ramp-up target, even on forced
        # resume. Previously forced resume stored initial_vms (=1), which caused
        # nested pause cycles to ratchet capacity down permanently.
        state.runtime_max_concurrent_vms = restore
        session.add(state)
        now = _utcnow()
        _recent_auto_resumes.append(now)
        _prune_flap_window(now)
        _, flap_escalated = get_recovery_threshold_state(now)

        _last_resume_was_forced = bool(should_force_resume)

        if should_force_resume:
            _consecutive_force_resumes += 1
            await _create_forced_resume_alerts(
                session, pause_duration, critical_failures, _consecutive_force_resumes
            )
        else:
            _consecutive_force_resumes = 0
            await _create_auto_resume_alerts(session, flapping=flap_escalated)
        await session.commit()
        _runtime_config["max_concurrent_vms"] = initial_vms
        _reset_health_recovery_history()
        _last_auto_resume_at = now

        if should_force_resume:
            logger.error(
                f"[PAUSE] FORCE-RESUMED after {int(pause_duration)}s paused (max {max_duration}s) "
                f"with {initial_vms} VM(s). Health history: {history_snapshot}. "
                f"Still-failing deps: {critical_failures or 'none'}. "
                f"Consecutive force-resumes: {_consecutive_force_resumes}",
                extra={
                    "alert": True,
                    "type": "forced_resume",
                    "pause_duration_seconds": int(pause_duration),
                    "max_auto_pause_duration_seconds": max_duration,
                    "health_history": history_snapshot,
                    "failed_deps": critical_failures,
                    "consecutive_force_resumes": _consecutive_force_resumes,
                },
            )
        elif flap_escalated:
            logger.error(
                f"[PAUSE] FLAPPING auto-resume (#{len(_recent_auto_resumes)} in window) "
                f"with {initial_vms} VMs (target: {restore}). "
                f"Recovery bar raised to {_FLAP_ESCALATED_HEALTHY_REQUIRED}-of-{_HEALTH_HISTORY_SIZE}.",
                extra={
                    "alert": True,
                    "type": "flapping_resume",
                    "recent_resume_count": len(_recent_auto_resumes),
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
