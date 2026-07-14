"""Runner health alert helpers extracted from ``app.alert_dispatch``.

Owns the deduplicated create/resolve helpers for all self-hosted-runner alert
types: runner-deactivated, runner-degraded, runner-low-disk,
fleet-no-healthy-runner, and the compute require-local hold warning.
``app.alert_dispatch`` re-exports every symbol here so existing
``from app.alert_dispatch import ...`` callers keep working unchanged.

Pure leaf module — depends only on ``app.alert_policy``, ``app.broadcast``,
``app.shared_state``, ``app.time_utils``, and ``app.models``. External webhook
delivery is deferred to ``app.notification_dispatch.dispatch_external_notification``
(imported lazily at each call site to keep this a leaf).
"""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.alert_policy import create_project_alert_if_allowed
from app.broadcast import broadcast_to_project
from app.models import Alert, AlertSeverity
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


def _fmt_duration_seconds(seconds: "float | int | None") -> str:
    """Human-readable compact duration ('2h 5m', '45m', '30s', 'n/a').

    Mirrors the copy in ``app.alert_dispatch`` — both are kept in sync
    because runner_health_alerts is a pure leaf that cannot import from
    alert_dispatch without a circular dep.
    """
    if seconds is None:
        return "n/a"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, _sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


# Event type + dedupe-key scheme for the self-hosted fleet-runner death alert.
# The dedupe key encodes the runner id so an active->inactive flip raises exactly
# ONE open alert per runner (the ux_alert_active_dedupe_key unique index enforces
# it), and a genuinely distinct death event — after the previous one was resolved
# on recovery — gets a fresh alert. Kept stable: the resolve-on-recovery path
# rebuilds the SAME key to find the open alert to dismiss.
_RUNNER_DEACTIVATED_EVENT_TYPE = "runner.deactivated"
_RUNNER_DEGRADED_EVENT_TYPE = "runner.degraded"

# Escalation: re-notify at 1h if a runner-deactivated alert stays unresolved,
# then again every 4h. Ephemeral in-memory tracking — clears on restart, which
# is acceptable because the initial notification already has retry semantics.
_RUNNER_ESCALATION_FIRST_SECONDS = 3600    # 1h
_RUNNER_ESCALATION_REPEAT_SECONDS = 14400  # 4h
_runner_escalation_last_notified: dict[str, "datetime"] = {}
# Dedupe keys of alerts escalated from WARNING→ERROR by the singular
# _maybe_escalate_runner_deactivated_alert path (RUNNER_OUTAGE_ESCALATED).
# Used to distinguish "escalated-to-ERROR by sweep" from "born-ERROR
# (had_active_work)" so the periodic re-notification sweep (_maybe_escalate_
# runner_deactivated_alerts) can still page for mid-job deaths.
_runner_outage_escalated_keys: set[str] = set()


def _runner_deactivated_dedupe_key(runner_id: "uuid.UUID | str") -> str:
    return f"{_RUNNER_DEACTIVATED_EVENT_TYPE}:{runner_id}"


def _runner_degraded_dedupe_key(runner_id: "uuid.UUID | str") -> str:
    return f"{_RUNNER_DEGRADED_EVENT_TYPE}:{runner_id}"


async def _maybe_create_runner_deactivated_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
    had_active_work: bool = False,
    active_work_count: int = 0,
    seconds_since_last_seen: "int | None" = None,
) -> bool:
    """Create ONE deduplicated operator Alert when a fleet runner is deactivated.

    Fired from the orchestrator's stale-runner sweep at the moment it flips a
    registered self-hosted runner ``active -> inactive`` (missed heartbeat
    window). Surfaces the death in the same Alerts banner/popover as
    PIPELINE_HALTED / CIRCUIT_BREAKER_OPEN and dispatches a ``RUNNER_DEACTIVATED``
    external notification through the retry-capable
    :func:`~app.notification_dispatch.dispatch_external_notification` path (30s /
    2 min / 10 min backoff) so a misconfigured or transiently-unreachable webhook
    does not silently drop the alert.

    Severity mirrors impact: **ERROR** when the runner had active work in flight
    at deactivation (a job may have been interrupted), **WARNING** when it was
    idle. Uses the always-on ``core`` alert domain (runner death is a portable
    operator incident), so it is never suppressed by per-project domain config.

    Deduplicated on ``dedupe_key = runner.deactivated:{runner_id}`` via the shared
    :func:`create_project_alert_if_allowed` upsert: a repeated sweep (or any
    re-entry while the runner stays inactive) bumps the existing open alert's
    occurrence count instead of creating a second row. The notification fires ONLY
    on the first (newly-created) alert, not on dedup hits. Commits its own row so
    the caller's transaction boundary is independent. Returns ``True`` when a NEW
    alert was created (and the notification dispatched), else ``False``.
    """
    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    severity = AlertSeverity.ERROR if had_active_work else AlertSeverity.WARNING
    if had_active_work:
        title = f"Fleet runner went offline mid-job: {runner_label}"
        work_desc = (
            f" It had {active_work_count} active "
            f"task{'s' if active_work_count != 1 else ''} in flight, which may "
            f"have been interrupted."
        )
    else:
        title = f"Fleet runner went offline: {runner_label}"
        work_desc = " It had no active work at deactivation."
    if seconds_since_last_seen is not None:
        last_seen_desc = (
            f" Last heartbeat {_fmt_duration_seconds(seconds_since_last_seen)} ago."
        )
    else:
        last_seen_desc = " It never sent a heartbeat after registering."
    message = (
        f"Self-hosted fleet runner {runner_label} ({str(runner_id)[:8]}) missed "
        f"its heartbeat window and was deactivated.{work_desc}{last_seen_desc} "
        f"Bring the runner back online — it re-arms automatically on its next "
        f"heartbeat (the alert clears then) — or retarget its work to another "
        f"runner or the cloud fallback."
    )[:1000]

    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_RUNNER_DEACTIVATED_EVENT_TYPE,
        severity=severity,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_runner_deactivated_dedupe_key(runner_id),
        broadcast=False,
    )
    if alert is None:
        # Policy suppressed the durable alert (should not happen for the core
        # domain, but keep the contract honest): nothing staged, nothing to push.
        return False
    await session.commit()
    if not created:
        # Dedup hit: the open alert's occurrence count was bumped and committed;
        # do not re-broadcast or re-push the webhook while the runner stays dead.
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": severity.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_DEACTIVATED",
                {
                    "runner_id": str(runner_id),
                    "runner_label": runner_label,
                    "had_active_work": had_active_work,
                    "active_work_count": active_work_count,
                    "seconds_since_last_seen": seconds_since_last_seen,
                    "title": alert.title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_DEACTIVATED dispatch setup failed: %s", _ne)
    return True


# Escalation age threshold: a runner inactive past this duration triggers a
# second, ERROR-severity escalation notification. Configurable at runtime via
# ``runner_escalation_age_seconds`` (default 4h). The flap-suppression window is
# implicit: a runner that re-arms before the threshold never gets escalated
# because recovery dismisses the alert before the sweep reaches it.
_RUNNER_ESCALATION_AGE_SECONDS_DEFAULT = 4 * 3600  # 4 hours


async def _maybe_escalate_runner_deactivated_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
) -> bool:
    """Escalate a long-running runner outage from WARNING to ERROR and re-page.

    Called from the orchestrator's stale-runner escalation sweep for inactive
    runners whose open deactivation alert is older than the configured escalation
    threshold (default 4h). Produces a second, escalated notification distinct
    from the initial RUNNER_DEACTIVATED page. Skipped if:

    * No open deactivation alert exists (runner already recovered or never paged).
    * The alert has been acknowledged by the operator (``acknowledged_at`` set).
    * The alert's severity is already ERROR (already escalated this cycle).
    * The alert's age (``created_at`` to now) is below the threshold.

    Flap suppression is natural: a runner that recovers before the threshold
    dismisses its alert (via ``_resolve_runner_deactivated_alert``), so the sweep
    finds nothing to escalate. Commits its own row; caller wraps best-effort.
    Returns True when an escalation notification was fired.
    """
    threshold = int(
        _runtime_config.get(
            "runner_escalation_age_seconds", _RUNNER_ESCALATION_AGE_SECONDS_DEFAULT
        )
    )
    dedupe_key = _runner_deactivated_dedupe_key(runner_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    alert = result.scalars().first()
    if alert is None:
        return False
    if alert.acknowledged_at is not None:
        return False
    if alert.severity == AlertSeverity.ERROR:
        return False
    now = _utcnow()
    age_seconds = (now - alert.created_at).total_seconds()
    if age_seconds < threshold:
        return False

    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    age_str = _fmt_duration_seconds(int(age_seconds))
    hours = threshold // 3600
    alert.severity = AlertSeverity.ERROR
    alert.title = f"Fleet runner offline — escalated outage: {runner_label}"
    alert.occurrence_count = int(alert.occurrence_count or 1) + 1
    alert.last_seen_at = now
    session.add(alert)
    await session.commit()
    await broadcast_to_project(
        str(project_id),
        "alert_updated",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.ERROR.value,
            "title": alert.title,
        },
    )
    # Re-page through the operator-configured, retry-capable, SSRF-guarded
    # webhook (dispatch_external_notification) rather than the env-var-only
    # PIPELINE_WEBHOOK_URL path (_send_webhook_notification), which has no retry
    # semantics and points at a different endpoint. Fire-and-forget so an
    # escalation-dispatch failure never aborts the stale-runner sweep.
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_OUTAGE_ESCALATED",
                {
                    "runner_id": str(runner_id),
                    "runner_label": runner_label,
                    "offline_duration": age_str,
                    "offline_duration_seconds": int(age_seconds),
                    "threshold_hours": hours,
                    "title": alert.title,
                    "detail": (
                        f"Runner {runner_label} has been offline for {age_str} "
                        f"(threshold: {hours}h). Immediate action required — bring "
                        f"the runner back online or retarget its work."
                    ),
                },
            )
        )
    except Exception as _ne:
        logger.warning(
            "[NOTIFY] RUNNER_OUTAGE_ESCALATED dispatch setup failed: %s", _ne
        )
    logger.warning(
        "[RUNNERS] Escalated runner-deactivated alert for %s (%s): offline %s",
        runner_label,
        runner_id,
        age_str,
    )
    # Record that this alert was escalated via the singular path so the periodic
    # re-notification sweep (_maybe_escalate_runner_deactivated_alerts) knows to
    # skip it and not double-page with a RUNNER_DEACTIVATED on top of the
    # RUNNER_OUTAGE_ESCALATED already fired here.
    _runner_outage_escalated_keys.add(dedupe_key)
    return True


async def _resolve_runner_deactivated_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
) -> int:
    """Dismiss any open runner-deactivated alert for a runner that recovered.

    The companion to :func:`_maybe_create_runner_deactivated_alert`. Called from
    the runner heartbeat handler when a previously-inactive runner re-arms
    (status flips back to ``active``): the WARNING/ERROR alert that flagged the
    death no longer describes a real condition, so it is resolved — mirroring how
    :func:`_resolve_wake_delivery_stall_alert` dismisses its alert on recovery.

    Looks the open alert up by the deterministic ``dedupe_key`` (rebuilt with the
    SAME construction the create path used), stamps ``dismissed_at``, commits its
    own rows, and broadcasts one ``alert_dismissed`` per dismissed alert so the
    operator UI drops it without waiting for a poll. Only open
    (``dismissed_at IS NULL``) alerts are touched, so this is a safe idempotent
    no-op when none exists. Returns the number of alerts dismissed (0 or 1 in
    practice). Best-effort contract: the caller wraps this so a resolve failure
    never aborts the heartbeat that triggered it.
    """
    dedupe_key = _runner_deactivated_dedupe_key(runner_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
        # Clear escalation state so the next outage starts fresh.
        if alert.dedupe_key:
            _runner_escalation_last_notified.pop(alert.dedupe_key, None)
            _runner_outage_escalated_keys.discard(alert.dedupe_key)
    return len(open_alerts)


async def _maybe_escalate_runner_deactivated_alerts(session: AsyncSession) -> int:
    """Re-notify for runner-deactivated alerts that have been open too long.

    The initial notification fires on alert creation (with retry semantics). This
    sweep function is called periodically (every 300s) and fires a follow-up
    ``RUNNER_DEACTIVATED`` external notification when an alert has been unresolved
    for more than :data:`_RUNNER_ESCALATION_FIRST_SECONDS` (1h), then re-escalates
    every :data:`_RUNNER_ESCALATION_REPEAT_SECONDS` (4h).

    Escalation cadence is persisted in :data:`Alert.last_escalated_at` so the cadence
    survives orchestrator restart. Born-ERROR alerts (had_active_work=True) and
    escalated-to-ERROR alerts (had_active_work=False) are both re-notified if
    unacknowledged, unless the alert was escalated by the singular
    _maybe_escalate_runner_deactivated_alert path. Returns the count of escalation
    notifications dispatched.
    """
    now = _utcnow()
    open_alerts = (
        await session.execute(
            select(Alert).where(
                Alert.event_type == _RUNNER_DEACTIVATED_EVENT_TYPE,
                Alert.dismissed_at.is_(None),
            )
        )
    ).scalars().all()

    dispatched = 0
    for alert in open_alerts:
        if alert.dedupe_key is None:
            continue
        # Honour the acknowledged_at contract: operator acknowledgement suppresses
        # further re-pages until the runner recovers (alert auto-resolves).  This
        # mirrors the check in the singular _maybe_escalate_runner_deactivated_alert
        # (alert_dispatch.py:1884) and is especially important for born-ERROR alerts
        # (had_active_work=True) which are never added to _runner_outage_escalated_keys
        # and therefore reach this sweep exclusively.  Also fixes the restart edge-case
        # where an acknowledged WARNING→ERROR alert re-pages because the in-memory set
        # is empty after a process restart.
        if alert.acknowledged_at is not None:
            continue
        # Skip alerts that were escalated from WARNING→ERROR by the singular
        # _maybe_escalate_runner_deactivated_alert path (RUNNER_OUTAGE_ESCALATED
        # was already fired there). Keying off the dedicated set — not raw severity
        # — lets born-ERROR alerts (had_active_work=True, created at ERROR severity)
        # still receive periodic RUNNER_DEACTIVATED re-notifications here.
        if alert.dedupe_key in _runner_outage_escalated_keys:
            continue
        age_s = (now - alert.created_at).total_seconds()
        # Use alert.last_escalated_at for escalation cadence tracking (persisted;
        # survives restart). None means the sweep has never paged for this alert —
        # fire the first re-page after _RUNNER_ESCALATION_FIRST_SECONDS (1h).
        # last_seen_at is NOT used here: alert_policy sets it on creation and bumps
        # it on every dedup re-fire, so it is never None for real alerts.
        should_escalate = False
        if alert.last_escalated_at is None:
            if age_s >= _RUNNER_ESCALATION_FIRST_SECONDS:
                should_escalate = True
        else:
            if (now - alert.last_escalated_at).total_seconds() >= _RUNNER_ESCALATION_REPEAT_SECONDS:
                should_escalate = True

        if not should_escalate:
            continue

        # Persist escalation cadence in alert.last_escalated_at (survives restart)
        alert.last_escalated_at = now
        session.add(alert)
        # Update in-memory dict for same-process, same-tick re-entry (best-effort)
        _runner_escalation_last_notified[alert.dedupe_key] = now
        try:
            import asyncio as _asyncio
            from app.notification_dispatch import dispatch_external_notification

            _asyncio.ensure_future(  # noqa: RUF006
                dispatch_external_notification(
                    alert.project_id,
                    "RUNNER_DEACTIVATED",
                    {
                        "runner_id": alert.dedupe_key.split(":", 1)[-1],
                        "title": alert.title,
                        "message": alert.message,
                        "age_seconds": int(age_s),
                        "occurrence_count": alert.occurrence_count or 1,
                        "escalation": True,
                    },
                )
            )
            dispatched += 1
        except Exception as _ne:
            logger.warning("[NOTIFY] Runner escalation dispatch failed: %s", _ne)

    if dispatched > 0:
        await session.commit()

    return dispatched


async def _maybe_create_runner_degraded_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
    consecutive_failures: int = 0,
    seconds_since_last_success: "int | None" = None,
) -> bool:
    """Create a deduplicated WARNING alert when a runner is degraded.

    A degraded runner has a recent heartbeat but is failing every job it claims
    (``consecutive_failures >= threshold``). Distinct from the deactivated alert:
    the runner is still CONNECTED and heartbeating, but its jobs are silently
    failing — a false-green that the operator panel's ``device_status="degraded"``
    flag exposes but that previously raised no push notification.

    Deduplicated on ``runner.degraded:{runner_id}`` so repeated sweep passes
    bump the occurrence count rather than creating duplicate rows. The alert
    clears automatically when the runner completes a successful job
    (``consecutive_failures`` drops to 0) and the degraded-runner sweep calls
    :func:`_resolve_runner_degraded_alert`. Returns ``True`` when a NEW alert
    was created (and the notification dispatched), else ``False``.
    """
    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    title = f"Fleet runner degraded: {runner_label}"
    if seconds_since_last_success is not None:
        success_desc = (
            f" Last successful job "
            f"{_fmt_duration_seconds(seconds_since_last_success)} ago."
        )
    else:
        success_desc = " No successful job recorded."
    message = (
        f"Self-hosted fleet runner {runner_label} ({str(runner_id)[:8]}) is "
        f"heartbeating normally but has failed {consecutive_failures} consecutive "
        f"job{'s' if consecutive_failures != 1 else ''}.{success_desc} "
        f"Check the runner's logs for errors. The alert clears automatically when "
        f"the runner completes a job successfully."
    )[:1000]

    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_RUNNER_DEGRADED_EVENT_TYPE,
        severity=AlertSeverity.WARNING,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_runner_degraded_dedupe_key(runner_id),
        broadcast=False,
    )
    if alert is None:
        return False
    await session.commit()
    if not created:
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.WARNING.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_DEGRADED",
                {
                    "runner_id": str(runner_id),
                    "runner_label": runner_label,
                    "consecutive_failures": consecutive_failures,
                    "seconds_since_last_success": seconds_since_last_success,
                    "title": alert.title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_DEGRADED dispatch setup failed: %s", _ne)
    return True


async def _resolve_runner_degraded_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
) -> int:
    """Dismiss any open runner-degraded alert for a runner that recovered.

    The companion to :func:`_maybe_create_runner_degraded_alert`. Called from the
    degraded-runner sweep when ``consecutive_job_failures`` drops below the
    threshold (the runner completed a successful job). Mirrors
    :func:`_resolve_runner_deactivated_alert`. Returns the number of alerts
    dismissed (0 or 1 in practice).
    """
    dedupe_key = _runner_degraded_dedupe_key(runner_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


async def _maybe_escalate_runner_degraded_alerts(session: AsyncSession) -> int:
    """Re-page and escalate degraded-runner alerts that have been open too long.

    The initial WARNING notification fires once on creation (via
    :func:`_maybe_create_runner_degraded_alert`, with retry semantics). Without
    this sweep a runner that keeps silently failing every job for hours produces
    exactly ONE push and is never surfaced again — a false-green that ages
    indefinitely, unlike a fully-down runner which the deactivated escalation
    pair re-pages and raises to ERROR. This function gives degraded alerts the
    same aging treatment: called periodically (every 300s from
    :func:`~app.orchestrator_runner_dispatch._check_degraded_runners`), for each
    open, unacknowledged ``runner.degraded:*`` alert it

    * **re-notifies** via a follow-up ``RUNNER_DEGRADED_ESCALATED`` external
      notification (distinct from the initial ``RUNNER_DEGRADED`` page, mirroring
      the ``RUNNER_OUTAGE_ESCALATED`` / ``RUNNER_DEACTIVATED`` pair for outages)
      once the alert has been open past
      :data:`_RUNNER_ESCALATION_FIRST_SECONDS` (1h), then repeats every
      :data:`_RUNNER_ESCALATION_REPEAT_SECONDS` (4h); and
    * **escalates severity** WARNING → ERROR (bumping the occurrence count and
      updating the title) once the alert has aged past the configurable
      escalation threshold (``runner_escalation_age_seconds``, default 4h).

    Re-page cadence is persisted in :data:`Alert.last_escalated_at` so it survives
    an orchestrator restart, and :data:`Alert.acknowledged_at` suppresses further
    re-pages exactly as the deactivated sweep documents. Flap suppression is
    natural: a runner that recovers has its degraded alert dismissed by
    :func:`_resolve_runner_degraded_alert`, so this sweep finds nothing to
    escalate. Distinct from the deactivated escalation path — separate event type,
    separate dedupe key, separate rows — the two failure modes stay separate.
    Commits once per pass; best-effort dispatch never crashes the sweep. Returns
    the count of escalation notifications dispatched.
    """
    now = _utcnow()
    threshold = int(
        _runtime_config.get(
            "runner_escalation_age_seconds", _RUNNER_ESCALATION_AGE_SECONDS_DEFAULT
        )
    )
    open_alerts = (
        await session.execute(
            select(Alert).where(
                Alert.event_type == _RUNNER_DEGRADED_EVENT_TYPE,
                Alert.dismissed_at.is_(None),
            )
        )
    ).scalars().all()

    dispatched = 0
    updated = False
    for alert in open_alerts:
        if alert.dedupe_key is None:
            continue
        # Operator acknowledgement suppresses further re-pages/escalation until the
        # runner recovers (the alert auto-resolves then). Mirrors the deactivated
        # sweep (alert_dispatch.py) and is restart-safe: acknowledged_at is
        # persisted, so a process restart cannot cause an acknowledged alert to
        # re-page.
        if alert.acknowledged_at is not None:
            continue
        age_s = (now - alert.created_at).total_seconds()
        runner_id_str = alert.dedupe_key.split(":", 1)[-1]

        changed = False
        # Severity flip WARNING → ERROR once the alert has aged past the threshold.
        # Evaluated independently of the re-page cadence so the runner is raised to
        # ERROR at the threshold even if the next re-page tick is not yet due. The
        # severity guard makes this fire exactly once per alert.
        if alert.severity == AlertSeverity.WARNING and age_s >= threshold:
            existing_label = (
                alert.title.split(":", 1)[-1].strip()
                if ":" in alert.title
                else f"runner {runner_id_str[:8]}"
            )
            alert.severity = AlertSeverity.ERROR
            alert.title = f"Fleet runner degraded — escalated: {existing_label}"
            changed = True

        # Re-page cadence, persisted in last_escalated_at (survives restart). None
        # means the sweep has never paged this alert — fire the first re-page after
        # _RUNNER_ESCALATION_FIRST_SECONDS (1h), then every
        # _RUNNER_ESCALATION_REPEAT_SECONDS (4h) thereafter.
        should_repage = False
        if alert.last_escalated_at is None:
            if age_s >= _RUNNER_ESCALATION_FIRST_SECONDS:
                should_repage = True
        elif (now - alert.last_escalated_at).total_seconds() >= _RUNNER_ESCALATION_REPEAT_SECONDS:
            should_repage = True

        if should_repage:
            alert.last_escalated_at = now
            alert.occurrence_count = int(alert.occurrence_count or 1) + 1
            alert.last_seen_at = now
            changed = True
            try:
                import asyncio as _asyncio
                from app.notification_dispatch import dispatch_external_notification

                _asyncio.ensure_future(  # noqa: RUF006
                    dispatch_external_notification(
                        alert.project_id,
                        "RUNNER_DEGRADED_ESCALATED",
                        {
                            "runner_id": runner_id_str,
                            "title": alert.title,
                            "message": alert.message,
                            "age_seconds": int(age_s),
                            "occurrence_count": alert.occurrence_count or 1,
                            "escalation": True,
                        },
                    )
                )
                dispatched += 1
            except Exception as _ne:
                logger.warning(
                    "[NOTIFY] Runner degraded escalation dispatch failed: %s", _ne
                )

        if changed:
            session.add(alert)
            updated = True
            await broadcast_to_project(
                str(alert.project_id),
                "alert_updated",
                {
                    "alert_id": str(alert.id),
                    "severity": getattr(alert.severity, "value", alert.severity),
                    "title": alert.title,
                },
            )

    if updated:
        await session.commit()

    return dispatched


_LOCAL_RUNNER_HOLD_ALERT_TITLE = "Compute hold: require-local runner offline"


async def _maybe_create_local_runner_hold_alert(
    task: "object",
    session: AsyncSession,
    *,
    held_seconds: int,
    timeout_seconds: int,
) -> bool:
    """Create a deduplicated WARNING alert for an aging require-local compute hold.

    Fires when a ``compute_experiment`` task has been QUEUED waiting for a
    self-hosted runner longer than the early-warning threshold, but before the
    6h hard-fail timeout. Surfaces the stranded hold so the operator learns the
    hardware is offline in minutes rather than waiting for the terminal failure.

    Deduplicated on ``(task_id, title, dismissed_at IS NULL)`` so repeated sweep
    passes raise exactly ONE alert per hold episode. The caller commits after this
    returns ``True``. Returns ``False`` when the alert already exists (dedup hit).
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.dismissed_at.is_(None),
            Alert.title == _LOCAL_RUNNER_HOLD_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return False
    held_str = _fmt_duration_seconds(held_seconds)
    remaining_str = _fmt_duration_seconds(timeout_seconds)
    msg = (
        f"A require-local compute experiment has been waiting {held_str} for a "
        f"self-hosted runner with none online — the task will hard-fail in "
        f"~{remaining_str} if no runner comes online. Bring the runner back "
        f"online or requeue the task without require_local_runner to use cloud "
        f"compute."
    )[:1000]
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=_LOCAL_RUNNER_HOLD_ALERT_TITLE,
        message=msg,
        task_id=task.id,
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                task.project_id,
                "RUNNER_LOCAL_HOLD",
                {
                    "task_id": str(task.id),
                    "task_title": task.title or str(task.id)[:8],
                    "held_seconds": held_seconds,
                    "held_duration": held_str,
                    "timeout_seconds": timeout_seconds,
                    "remaining_duration": remaining_str,
                    "title": alert.title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_LOCAL_HOLD dispatch setup failed: %s", _ne)
    return True


async def _resolve_local_runner_hold_alert(
    session: AsyncSession,
    task_id: "uuid.UUID",
) -> int:
    """Dismiss any open compute-hold alert when a held task finally dispatches.

    The companion to :func:`_maybe_create_local_runner_hold_alert`. Called from
    the runner-dispatch path when a ``require-local`` compute task that was held
    >15 min is successfully pinned to a runner: the WARNING alert that flagged
    the stranded hold no longer describes a real condition, so it is resolved —
    mirroring how :func:`_resolve_runner_deactivated_alert` and
    :func:`_resolve_runner_degraded_alert` dismiss their alerts on recovery.

    Looks the open alert up by ``(task_id, title, dismissed_at IS NULL)`` (the
    same compound key the create path uses for dedup), stamps ``dismissed_at``,
    commits its own row, and broadcasts ``alert_dismissed``. Only open
    (``dismissed_at IS NULL``) alerts are touched, so this is a safe idempotent
    no-op when none exists. Returns the number of alerts dismissed (0 or 1 in
    practice). Best-effort: a resolve failure must never abort the dispatch that
    triggered it.
    """
    result = await session.execute(
        select(Alert).where(
            Alert.task_id == task_id,
            Alert.title == _LOCAL_RUNNER_HOLD_ALERT_TITLE,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(alert.project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


_CONTAINER_RUNNER_HOLD_ALERT_TITLE = "Compute hold: no container-capable runner"


async def _maybe_create_container_runner_hold_alert(
    task: "object",
    session: AsyncSession,
    *,
    held_seconds: int,
    timeout_seconds: int,
) -> bool:
    """Create a deduplicated WARNING alert for an aging container-runner compute hold.

    Sibling of :func:`_maybe_create_local_runner_hold_alert`. Fires when a
    ``compute_experiment`` declaring a ``container_image`` has been QUEUED longer
    than the early-warning threshold because no live runner advertises the
    ``docker_usable`` capability — the exact stall the project-baked-image
    authoring default triggers on any GPU box without Docker. Distinct title so
    the operator sees the accurate cause (missing container runtime, not an
    offline require-local host).

    Deduplicated on ``(task_id, title, dismissed_at IS NULL)`` so repeated sweep
    passes raise exactly ONE alert per hold episode. The caller commits after this
    returns ``True``. Returns ``False`` when the alert already exists (dedup hit).
    """
    existing = await session.execute(
        select(Alert).where(
            Alert.task_id == task.id,
            Alert.dismissed_at.is_(None),
            Alert.title == _CONTAINER_RUNNER_HOLD_ALERT_TITLE,
        )
    )
    if existing.scalars().first() is not None:
        return False
    held_str = _fmt_duration_seconds(held_seconds)
    remaining_str = _fmt_duration_seconds(timeout_seconds)
    msg = (
        f"A container-image compute experiment has been waiting {held_str} for a "
        f"container-capable runner (docker_usable) with none online — the task "
        f"will hard-fail in ~{remaining_str} if no such runner comes online. "
        f"Enroll a Docker-capable runner or requeue the task without a "
        f"container_image to use cloud compute."
    )[:1000]
    alert = Alert(
        project_id=task.project_id,
        severity=AlertSeverity.WARNING,
        title=_CONTAINER_RUNNER_HOLD_ALERT_TITLE,
        message=msg,
        task_id=task.id,
    )
    session.add(alert)
    await broadcast_to_project(
        str(task.project_id),
        "alert_created",
        {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                task.project_id,
                "RUNNER_CONTAINER_HOLD",
                {
                    "task_id": str(task.id),
                    "task_title": task.title or str(task.id)[:8],
                    "held_seconds": held_seconds,
                    "held_duration": held_str,
                    "timeout_seconds": timeout_seconds,
                    "remaining_duration": remaining_str,
                    "title": alert.title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_CONTAINER_HOLD dispatch setup failed: %s", _ne)
    return True


async def _resolve_container_runner_hold_alert(
    session: AsyncSession,
    task_id: "uuid.UUID",
) -> int:
    """Dismiss any open container-runner-hold alert when the held task dispatches.

    Companion to :func:`_maybe_create_container_runner_hold_alert`, mirroring
    :func:`_resolve_local_runner_hold_alert`. Idempotent no-op when no open alert
    exists; best-effort — a resolve failure must never abort the dispatch that
    triggered it. Returns the number of alerts dismissed.
    """
    result = await session.execute(
        select(Alert).where(
            Alert.task_id == task_id,
            Alert.title == _CONTAINER_RUNNER_HOLD_ALERT_TITLE,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(alert.project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


_FLEET_NO_HEALTHY_RUNNER_EVENT_TYPE = "runner.fleet_no_healthy"


def _fleet_no_healthy_runner_dedupe_key(project_id: "uuid.UUID | str") -> str:
    return f"{_FLEET_NO_HEALTHY_RUNNER_EVENT_TYPE}:{project_id}"


async def _maybe_create_fleet_no_healthy_runner_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    offline_count: int,
    degraded_count: int,
) -> bool:
    """Create a deduplicated ERROR alert when ALL runners for a project are unhealthy.

    Fires when a project has registered runners but none are currently healthy
    (active + heartbeat within liveness window + consecutive failures below
    threshold). This is distinct from individual runner-deactivated /
    runner-degraded alerts: it signals the catastrophic case where the entire
    self-hosted fleet is down, not just one runner.

    Deduplicated on ``runner.fleet_no_healthy:{project_id}`` via
    :func:`create_project_alert_if_allowed` so repeated sweep passes bump the
    occurrence count rather than creating duplicate rows. Returns ``True`` when a
    NEW alert was created (and the notification dispatched), else ``False``.
    The alert auto-resolves when :func:`_resolve_fleet_no_healthy_runner_alert`
    is called once a healthy runner is detected.
    """
    total = offline_count + degraded_count
    title = "All fleet runners offline or degraded"
    parts = []
    if offline_count:
        parts.append(f"{offline_count} offline")
    if degraded_count:
        parts.append(f"{degraded_count} degraded")
    health_desc = f" ({', '.join(parts)})" if parts else ""
    message = (
        f"All {total} registered runner{'s' if total != 1 else ''} for this "
        f"project are unhealthy{health_desc}. No self-hosted capacity is "
        f"available — tasks will fall back to cloud dispatch or be held. Bring "
        f"at least one runner back online to restore self-hosted capacity. The "
        f"alert clears automatically when a healthy runner is detected."
    )[:1000]

    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_FLEET_NO_HEALTHY_RUNNER_EVENT_TYPE,
        severity=AlertSeverity.ERROR,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_fleet_no_healthy_runner_dedupe_key(project_id),
        broadcast=False,
    )
    if alert is None:
        return False
    await session.commit()
    if not created:
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.ERROR.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_FLEET_OFFLINE",
                {
                    "offline_count": offline_count,
                    "degraded_count": degraded_count,
                    "title": title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_FLEET_OFFLINE dispatch setup failed: %s", _ne)
    return True


async def _resolve_fleet_no_healthy_runner_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
) -> int:
    """Dismiss any open fleet-no-healthy-runner alert once a healthy runner recovers.

    The companion to :func:`_maybe_create_fleet_no_healthy_runner_alert`. Called
    when a project is found to have at least one healthy runner (active + recent
    heartbeat + not degraded). Mirrors :func:`_resolve_runner_deactivated_alert`.
    Returns the number of alerts dismissed (0 or 1 in practice).
    """
    dedupe_key = _fleet_no_healthy_runner_dedupe_key(project_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


_RUNNER_LOW_DISK_EVENT_TYPE = "runner.low_disk"


def _runner_low_disk_dedupe_key(runner_id: "uuid.UUID | str") -> str:
    return f"{_RUNNER_LOW_DISK_EVENT_TYPE}:{runner_id}"


async def _maybe_create_runner_low_disk_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
    disk_free_bytes: int,
    threshold_bytes: int,
) -> bool:
    """Create a deduplicated WARNING alert when a runner's free disk is low.

    Fires when a live runner reports ``disk_free_bytes`` below ``threshold_bytes``
    during the orchestrator's degraded-runner sweep. Surfaces the capacity risk
    before jobs start failing due to disk exhaustion.

    Deduplicated on ``runner.low_disk:{runner_id}`` so repeated sweep passes
    bump the occurrence count rather than creating duplicate rows. The alert
    clears automatically when :func:`_resolve_runner_low_disk_alert` is called
    once disk recovers above the threshold. Returns ``True`` when a NEW alert
    was created, else ``False``.
    """
    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    free_gb = disk_free_bytes / (1024 ** 3)
    threshold_gb = threshold_bytes / (1024 ** 3)
    title = f"Fleet runner low disk: {runner_label}"
    message = (
        f"Self-hosted fleet runner {runner_label} ({str(runner_id)[:8]}) has "
        f"only {free_gb:.1f} GB of free disk space, below the "
        f"{threshold_gb:.1f} GB threshold. Low disk may cause job failures. "
        f"Free up disk space to restore healthy operation. The alert clears "
        f"automatically when free disk recovers above the threshold."
    )[:1000]

    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_RUNNER_LOW_DISK_EVENT_TYPE,
        severity=AlertSeverity.WARNING,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_runner_low_disk_dedupe_key(runner_id),
        broadcast=False,
    )
    if alert is None:
        return False
    await session.commit()
    if not created:
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.WARNING.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    # Mirror the deactivated/degraded/fleet helpers: fire-and-forget the external
    # webhook dispatch on the newly-created path only (not dedup hits) so the
    # operator's configured, retry-capable webhook receives the capacity warning.
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_LOW_DISK",
                {
                    "runner_id": str(runner_id),
                    "runner_label": runner_label,
                    "disk_free_bytes": disk_free_bytes,
                    "threshold_bytes": threshold_bytes,
                    "title": alert.title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_LOW_DISK dispatch setup failed: %s", _ne)
    return True


async def _resolve_runner_low_disk_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
) -> int:
    """Dismiss any open runner-low-disk alert for a runner whose disk has recovered.

    The companion to :func:`_maybe_create_runner_low_disk_alert`. Called from
    the degraded-runner sweep when a runner's ``disk_free_bytes`` rises back
    above the threshold (or it stops reporting disk). Mirrors
    :func:`_resolve_runner_degraded_alert`. Returns the number of alerts
    dismissed (0 or 1 in practice).
    """
    dedupe_key = _runner_low_disk_dedupe_key(runner_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


_RUNNER_POST_UPDATE_SILENT_EVENT_TYPE = "runner.post_update_silent"


def _runner_post_update_silent_dedupe_key(runner_id: "uuid.UUID | str") -> str:
    return f"{_RUNNER_POST_UPDATE_SILENT_EVENT_TYPE}:{runner_id}"


async def _maybe_create_runner_post_update_silent_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
    old_version: "str | None" = None,
    new_version: "str | None" = None,
    seconds_silent: "int | None" = None,
) -> bool:
    """Create a deduplicated WARNING alert when a runner self-updated then went silent.

    Fires from the degraded-runner sweep when a runner reported a NEW version and
    then stopped heartbeating within the post-update window — the tmux/nohup
    auto-update failure mode where ``os._exit`` killed the process and nothing
    restarted it (operator-observed dgx-1 ~45min outage). Deduplicated on
    ``runner.post_update_silent:{runner_id}`` so repeated sweeps bump the
    occurrence count. Clears via :func:`_resolve_runner_post_update_silent_alert`
    once the runner heartbeats again. Returns ``True`` when a NEW alert was
    created, else ``False``.
    """
    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    silent_for = (
        f"{seconds_silent}s" if seconds_silent is not None else "an unknown interval"
    )
    version_note = (
        f"from {old_version} to {new_version}"
        if old_version and new_version
        else f"to {new_version}" if new_version else ""
    )
    title = f"Fleet runner silent after self-update: {runner_label}"
    message = (
        f"Self-hosted fleet runner {runner_label} ({str(runner_id)[:8]}) reported a "
        f"version change {version_note} and then went silent for {silent_for}. This "
        f"is the auto-update-without-supervisor failure mode: the runner replaced "
        f"itself and no supervisor restarted it. Check the host — if launched via "
        f"tmux/nohup, wrap it in a restart loop or systemd service. The alert clears "
        f"automatically when the runner heartbeats again."
    )[:1000]

    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_RUNNER_POST_UPDATE_SILENT_EVENT_TYPE,
        severity=AlertSeverity.WARNING,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_runner_post_update_silent_dedupe_key(runner_id),
        broadcast=False,
    )
    if alert is None:
        return False
    await session.commit()
    if not created:
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.WARNING.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    return True


async def _resolve_runner_post_update_silent_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
) -> int:
    """Dismiss any open post-update-silent alert once the runner is live again.

    Companion to :func:`_maybe_create_runner_post_update_silent_alert`. Called
    from the sweep when the runner has heartbeated within the liveness window.
    Returns the number of alerts dismissed (0 or 1 in practice).
    """
    dedupe_key = _runner_post_update_silent_dedupe_key(runner_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


# ── Runner idle-GPU alert ──────────────────────────────────────────────────
# Fires when a live GPU runner reports GPU utilization below the idle threshold,
# indicating expensive GPU capacity may be wasting. Mirrors the low-disk pattern:
# deduplicated per runner, WARNING severity, auto-resolves on recovery.

_RUNNER_IDLE_GPU_EVENT_TYPE = "runner.idle_gpu"
_RUNNER_IDLE_GPU_THRESHOLD_PCT_DEFAULT = 10  # 10% utilization = idle


def _runner_idle_gpu_dedupe_key(runner_id: "uuid.UUID | str") -> str:
    return f"{_RUNNER_IDLE_GPU_EVENT_TYPE}:{runner_id}"


async def _maybe_create_runner_idle_gpu_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
    gpu_utilization_pct: float,
    threshold_pct: int,
) -> bool:
    """Create a deduplicated WARNING alert when a GPU runner is persistently idle.

    Fires when a live runner reports GPU utilization below the idle threshold,
    indicating expensive GPU capacity is being wasted. The alert auto-resolves
    when GPU utilization recovers above the threshold.

    Deduplicated on ``runner.idle_gpu:{runner_id}`` so repeated sweep passes
    bump the occurrence count rather than creating duplicate rows. Returns
    ``True`` when a NEW alert was created, else ``False``.
    """
    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    title = f"Fleet runner GPU idle: {runner_label}"
    message = (
        f"Self-hosted fleet runner {runner_label} ({str(runner_id)[:8]}) "
        f"GPU utilization is {gpu_utilization_pct:.0f}%, below the "
        f"{threshold_pct}% idle threshold. Expensive GPU capacity may be "
        f"wasting. The alert clears automatically when GPU utilization "
        f"recovers above the threshold."
    )[:1000]

    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_RUNNER_IDLE_GPU_EVENT_TYPE,
        severity=AlertSeverity.WARNING,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_runner_idle_gpu_dedupe_key(runner_id),
        broadcast=False,
    )
    if alert is None:
        return False
    await session.commit()
    if not created:
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.WARNING.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_IDLE_GPU",
                {
                    "runner_id": str(runner_id),
                    "runner_label": runner_label,
                    "gpu_utilization_pct": gpu_utilization_pct,
                    "threshold_pct": threshold_pct,
                    "title": alert.title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_IDLE_GPU dispatch setup failed: %s", _ne)
    return True


async def _resolve_runner_idle_gpu_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
) -> int:
    """Dismiss any open runner-idle-gpu alert for a runner whose GPU recovered.

    The companion to :func:`_maybe_create_runner_idle_gpu_alert`. Called from
    the idle-GPU sweep when a runner's GPU utilization rises above the threshold.
    Returns the number of alerts dismissed (0 or 1 in practice).
    """
    dedupe_key = _runner_idle_gpu_dedupe_key(runner_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


# ── Runner token-stale alert ──────────────────────────────────────────────────
# Fires when a runner's token is older than the recommended rotation window
# (90 days). Tokens issued months or years ago carry increased risk if
# compromised. The alert auto-resolves when the token is rotated.
# Mirrors the low-disk/idle-GPU pattern: deduplicated per runner, WARNING severity.

_RUNNER_TOKEN_STALE_EVENT_TYPE = "runner.token_stale"
_RUNNER_TOKEN_STALE_DEDUPE_KEY_PREFIX = "runner.token_stale:"


def _runner_token_stale_dedupe_key(runner_id: "uuid.UUID | str") -> str:
    return f"{_RUNNER_TOKEN_STALE_DEDUPE_KEY_PREFIX}{runner_id}"


async def _maybe_create_runner_token_stale_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
    token_age_days: int,
    threshold_days: int = 90,
) -> bool:
    """Create a deduplicated WARNING alert when a runner's token is stale.

    Fires when a runner's ``token_created_at`` is older than ``threshold_days``,
    indicating the token has not been rotated within the recommended window.
    The alert auto-resolves when the token is rotated (``token_created_at``
    is updated by the rotate-token endpoint).

    Deduplicated on ``runner.token_stale:{runner_id}`` so repeated sweep passes
    bump the occurrence count rather than creating duplicate rows. Returns
    ``True`` when a NEW alert was created, else ``False``.
    """
    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    title = f"Fleet runner token stale: {runner_label}"
    message = (
        f"Self-hosted fleet runner {runner_label} ({str(runner_id)[:8]}) "
        f"token is {token_age_days} days old, exceeding the {threshold_days}-day "
        f"rotation threshold. Rotate the runner token via the admin API to "
        f"reduce the risk of a compromised credential persisting. The alert "
        f"clears automatically when the token is rotated."
    )[:1000]

    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_RUNNER_TOKEN_STALE_EVENT_TYPE,
        severity=AlertSeverity.WARNING,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_runner_token_stale_dedupe_key(runner_id),
        broadcast=False,
    )
    if alert is None:
        return False
    await session.commit()
    if not created:
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.WARNING.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_TOKEN_STALE",
                {
                    "runner_id": str(runner_id),
                    "runner_label": runner_label,
                    "token_age_days": token_age_days,
                    "threshold_days": threshold_days,
                    "title": alert.title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_TOKEN_STALE dispatch setup failed: %s", _ne)
    return True


async def _resolve_runner_token_stale_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
) -> int:
    """Dismiss any open runner-token-stale alert for a runner whose token was rotated.

    The companion to :func:`_maybe_create_runner_token_stale_alert`. Called from
    the stale-token sweep when a runner's ``token_created_at`` is updated (token
    was rotated). Returns the number of alerts dismissed (0 or 1 in practice).
    """
    dedupe_key = _runner_token_stale_dedupe_key(runner_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


# ── Runner low memory ─────────────────────────────────────────────────────────

_RUNNER_LOW_MEMORY_EVENT_TYPE = "runner.low_memory"


def _runner_low_memory_dedupe_key(runner_id: "uuid.UUID | str") -> str:
    return f"{_RUNNER_LOW_MEMORY_EVENT_TYPE}:{runner_id}"


async def _maybe_create_runner_low_memory_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
    memory_free_bytes: int,
    threshold_bytes: int,
) -> bool:
    """Create a deduplicated WARNING alert when a runner's free memory is low.

    Fires when a live runner reports ``memory_free_bytes`` below
    ``threshold_bytes`` during the orchestrator's degraded-runner sweep.
    Surfaces the capacity risk before jobs start failing due to OOM.

    Deduplicated on ``runner.low_memory:{runner_id}`` so repeated sweep passes
    bump the occurrence count rather than creating duplicate rows. The alert
    clears automatically when :func:`_resolve_runner_low_memory_alert` is called
    once memory recovers above the threshold. Returns ``True`` when a NEW alert
    was created, else ``False``.
    """
    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    free_mb = memory_free_bytes / (1024 ** 2)
    threshold_mb = threshold_bytes / (1024 ** 2)
    title = f"Fleet runner low memory: {runner_label}"
    message = (
        f"Self-hosted fleet runner {runner_label} ({str(runner_id)[:8]}) has "
        f"only {free_mb:.0f} MB of free memory, below the "
        f"{threshold_mb:.0f} MB threshold. Low memory may cause OOM job "
        f"failures. Free up memory to restore healthy operation. The alert "
        f"clears automatically when free memory recovers above the threshold."
    )[:1000]

    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_RUNNER_LOW_MEMORY_EVENT_TYPE,
        severity=AlertSeverity.WARNING,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_runner_low_memory_dedupe_key(runner_id),
        broadcast=False,
    )
    if alert is None:
        return False
    await session.commit()
    if not created:
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.WARNING.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_LOW_MEMORY",
                {
                    "runner_id": str(runner_id),
                    "runner_label": runner_label,
                    "memory_free_bytes": memory_free_bytes,
                    "threshold_bytes": threshold_bytes,
                    "title": alert.title,
                },
            )
        )
    except Exception as _ne:
        logger.warning("[NOTIFY] RUNNER_LOW_MEMORY dispatch setup failed: %s", _ne)
    return True


async def _resolve_runner_low_memory_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
) -> int:
    """Dismiss any open runner-low-memory alert for a runner whose memory has recovered.

    The companion to :func:`_maybe_create_runner_low_memory_alert`. Called from
    the degraded-runner sweep when a runner's ``memory_free_bytes`` rises back
    above the threshold (or it stops reporting memory). Mirrors
    :func:`_resolve_runner_low_disk_alert`. Returns the number of alerts
    dismissed (0 or 1 in practice).
    """
    dedupe_key = _runner_low_memory_dedupe_key(runner_id)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == dedupe_key,
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)


# Cross-project access alert. The small process-local window deliberately
# suppresses isolated stale-job races. It resets on restart and is not shared
# across workers; the durable open alert itself remains in the database.
_RUNNER_CROSS_PROJECT_ACCESS_EVENT_TYPE = "runner.cross_project_access"
_RUNNER_CROSS_PROJECT_ACCESS_THRESHOLD = 3
_RUNNER_CROSS_PROJECT_ACCESS_WINDOW_SECONDS = 300
_runner_cross_project_mismatch_timestamps: dict[str, list[datetime]] = {}


def _runner_cross_project_access_dedupe_key(runner_id: "uuid.UUID | str") -> str:
    return f"{_RUNNER_CROSS_PROJECT_ACCESS_EVENT_TYPE}:{runner_id}"


def _record_runner_cross_project_mismatch(
    runner_id: "uuid.UUID | str",
    *,
    now: "datetime | None" = None,
) -> tuple[bool, int]:
    """Record a mismatch and return whether the sliding-window threshold fired."""
    key = str(runner_id)
    ts_now = now if now is not None else _utcnow()
    cutoff = ts_now - timedelta(seconds=_RUNNER_CROSS_PROJECT_ACCESS_WINDOW_SECONDS)
    timestamps = [
        timestamp
        for timestamp in _runner_cross_project_mismatch_timestamps.get(key, [])
        if timestamp > cutoff
    ]
    timestamps.append(ts_now)
    _runner_cross_project_mismatch_timestamps[key] = timestamps
    return len(timestamps) >= _RUNNER_CROSS_PROJECT_ACCESS_THRESHOLD, len(timestamps)


async def _maybe_create_runner_cross_project_access_alert(
    session: AsyncSession,
    *,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
    hostname: "str | None" = None,
    mismatch_count: int = 0,
    attempt_project_id: "uuid.UUID | str | None" = None,
) -> bool:
    """Create one ERROR alert when a runner repeatedly crosses project boundaries."""
    runner_label = hostname or f"runner {str(runner_id)[:8]}"
    target_desc = (
        f" targeting project {str(attempt_project_id)[:8]}" if attempt_project_id else ""
    )
    title = f"Runner cross-project access attempt: {runner_label}"
    message = (
        f"Self-hosted runner {runner_label} ({str(runner_id)[:8]}) has attempted "
        f"to access jobs belonging to a different project{target_desc} "
        f"{mismatch_count} time(s) in the last "
        f"{_RUNNER_CROSS_PROJECT_ACCESS_WINDOW_SECONDS // 60} minutes. "
        "This may indicate a misconfigured or compromised runner. Verify the "
        "runner's project assignment and rotate its credentials if compromise "
        "is suspected."
    )[:1000]
    alert, created, _decision = await create_project_alert_if_allowed(
        session,
        project_id=project_id,
        event_type=_RUNNER_CROSS_PROJECT_ACCESS_EVENT_TYPE,
        severity=AlertSeverity.ERROR,
        title=title,
        message=message,
        source="orchestrator",
        dedupe_key=_runner_cross_project_access_dedupe_key(runner_id),
        broadcast=False,
    )
    if alert is None:
        return False
    await session.commit()
    if not created:
        return False
    await broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": AlertSeverity.ERROR.value,
            "title": alert.title,
            "domain": alert.domain,
            "event_type": alert.event_type,
        },
    )
    try:
        import asyncio as _asyncio
        from app.notification_dispatch import dispatch_external_notification

        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                project_id,
                "RUNNER_CROSS_PROJECT_ACCESS",
                {
                    "runner_id": str(runner_id),
                    "runner_label": runner_label,
                    "mismatch_count": mismatch_count,
                    "attempt_project_id": (
                        str(attempt_project_id) if attempt_project_id else None
                    ),
                    "title": alert.title,
                },
            )
        )
    except Exception as exc:
        logger.warning(
            "[NOTIFY] RUNNER_CROSS_PROJECT_ACCESS dispatch setup failed: %s", exc
        )
    return True


async def _resolve_runner_cross_project_access_alert(
    session: AsyncSession,
    project_id: "uuid.UUID",
    runner_id: "uuid.UUID | str",
) -> int:
    """Dismiss the runner alert and reset its process-local mismatch window."""
    _runner_cross_project_mismatch_timestamps.pop(str(runner_id), None)
    result = await session.execute(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.dedupe_key == _runner_cross_project_access_dedupe_key(runner_id),
            Alert.dismissed_at.is_(None),
        )
    )
    open_alerts = result.scalars().all()
    if not open_alerts:
        return 0
    now = _utcnow()
    for alert in open_alerts:
        alert.dismissed_at = now
        session.add(alert)
    await session.commit()
    for alert in open_alerts:
        await broadcast_to_project(
            str(project_id),
            "alert_dismissed",
            {"alert_id": str(alert.id), "title": alert.title},
        )
    return len(open_alerts)
