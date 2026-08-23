"""Alert helpers extracted from deploy health monitoring."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.alert_policy import create_project_alert_if_allowed
from app.broadcast import broadcast_to_project
from app.models import Alert, AlertSeverity, Project
import app.shared_state as _shared_state
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")
_FORCE_RESUME_ESCALATION_THRESHOLD = 3


def _build_rate_limit_details() -> "dict | None":
    """Collect account-pool rate-limit state for inclusion in auto-pause alerts."""
    try:
        from app.max_plan_pool import (  # noqa: PLC0415
            summarize_pool_state,
            _account_by_id,
            _OPUS_MODEL,
        )
    except Exception:
        return None
    try:
        summary = summarize_pool_state(_OPUS_MODEL)
    except Exception:
        return None
    if not summary.get("total"):
        return None
    blocked_models: set = set()
    for account in _account_by_id.values():
        for model in (account.get("disabled_models") or {}).keys():
            blocked_models.add(model)
    recovery = summary.get("earliest_recovery")
    return {
        "total_accounts": summary.get("total", 0),
        "rate_limited_accounts": summary.get("rate_limited", 0),
        "healthy_accounts": summary.get("healthy", 0),
        "probing_accounts": summary.get("probing", 0),
        "blocked_models": sorted(blocked_models),
        "earliest_recovery": recovery.isoformat() if recovery else None,
    }


async def _create_auto_pause_alerts(session: AsyncSession, failed_deps: list) -> None:
    """Create WARNING alerts on all projects when the pipeline auto-pauses."""
    from app.notification import _send_webhook_notification  # noqa: PLC0415

    title = "Pipeline auto-paused: external dependencies down"
    dispatch_health = _shared_state.external_health.get("dispatch_health", {}) or {}
    rl_details = _build_rate_limit_details() if "anthropic_api" in failed_deps else None
    if rl_details:
        suffix_parts = [
            f"{rl_details['rate_limited_accounts']}/{rl_details['total_accounts']} accounts rate-limited"
        ]
        if rl_details["blocked_models"]:
            suffix_parts.append(
                f"models blocked: {', '.join(rl_details['blocked_models'])}"
            )
        if rl_details["earliest_recovery"]:
            suffix_parts.append(f"earliest recovery: {rl_details['earliest_recovery']}")
        rl_suffix = " (" + "; ".join(suffix_parts) + ")"
    else:
        rl_suffix = ""
    dispatch_suffix = ""
    if "dispatch_api" in failed_deps and dispatch_health:
        down_providers = dispatch_health.get("down_providers", [])
        if down_providers:
            dispatch_suffix = (
                f" (no dispatchable providers: {', '.join(down_providers)})"
            )
    msg = (
        f"Pipeline auto-paused: external dependencies down — {', '.join(failed_deps)}{rl_suffix}{dispatch_suffix}. "
        "Will auto-resume when healthy."
    )
    project_ids = (
        await session.execute(select(Project.id).limit(10000))
    ).scalars().all()
    if len(project_ids) >= 10000:
        logger.warning(
            "[HEALTH] Project ID fetch hit cap of 10000 — alert sweep truncated; "
            "unfetched projects will be handled on the next sweep cycle"
        )
    # Batch-check for existing pause alerts across all projects in one query
    already_alerted_pids = set(
        (
            await session.execute(
                select(Alert.project_id).where(
                    Alert.project_id.in_(project_ids),
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == title,
                )
            )
        )
        .scalars()
        .all()
    )
    for project_id in project_ids:
        if project_id in already_alerted_pids:
            continue
        alert, created, _decision = await create_project_alert_if_allowed(
            session,
            project_id=project_id,
            event_type="deployment.auto_pause",
            severity=AlertSeverity.WARNING,
            title=title,
            message=msg,
            source="deploy_health_alerts",
            dedupe_key=f"deployment.auto_pause:{project_id}",
        )
        if alert is None or not created:
            continue
        await broadcast_to_project(
            str(project_id),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "WARNING", "title": alert.title},
        )
    await _send_webhook_notification("AUTO_PAUSED", title, msg)
    try:
        import asyncio as _asyncio  # noqa: PLC0415
        from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415

        for project_id in project_ids:
            _asyncio.ensure_future(
                dispatch_external_notification(
                    project_id,
                    "DEPLOY_HEALTH_ALERT",
                    {
                        "failed_deps": failed_deps,
                        "title": title,
                        **({"rate_limit": rl_details} if rl_details else {}),
                    },
                )
            )
    except Exception as exc:
        logger.warning(f"[NOTIFY] DEPLOY_HEALTH_ALERT dispatch setup failed: {exc}")


async def _create_forced_resume_alerts(
    session: AsyncSession,
    pause_duration_seconds: float,
    failed_deps: list,
    consecutive_force_resumes: int = 1,
) -> None:
    """Create ERROR alerts when the pipeline force-resumes after a long pause."""
    from app.notification import _send_webhook_notification  # noqa: PLC0415

    pause_title = "Pipeline auto-paused: external dependencies down"
    escalated = consecutive_force_resumes >= _FORCE_RESUME_ESCALATION_THRESHOLD
    if escalated:
        title = f"Prolonged pipeline stall: {consecutive_force_resumes} consecutive force-resumes"
        msg = (
            f"Pipeline has force-resumed {consecutive_force_resumes} times in a row without "
            f"health probes ever stabilizing (latest pause {int(pause_duration_seconds)}s, "
            f"max {_runtime_config['max_auto_pause_duration_seconds']}s). "
            f"Still-failing deps: {', '.join(failed_deps) if failed_deps else 'none'}. "
            "This signals persistent infrastructure failure — manual investigation required."
        )
    else:
        title = "Pipeline force-resumed: max auto-pause duration exceeded"
        msg = (
            f"Pipeline force-resumed after {int(pause_duration_seconds)}s paused "
            f"(max {_runtime_config['max_auto_pause_duration_seconds']}s). "
            f"Health probes still report failures: {', '.join(failed_deps) if failed_deps else 'none'}. "
            "Resuming with reduced VM count (1) — operators should investigate."
        )
    now = _utcnow()
    project_ids = (
        await session.execute(select(Project.id).limit(10000))
    ).scalars().all()
    if len(project_ids) >= 10000:
        logger.warning(
            "[HEALTH] Project ID fetch hit cap of 10000 — alert sweep truncated; "
            "unfetched projects will be handled on the next sweep cycle"
        )
    # Batch-dismiss existing pause-warning alerts across all projects in one query
    all_existing_warnings = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id.in_(project_ids),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == pause_title,
                )
            )
        )
        .scalars()
        .all()
    )
    for warning in all_existing_warnings:
        warning.dismissed_at = now
        session.add(warning)
    for project_id in project_ids:
        alert, created, _decision = await create_project_alert_if_allowed(
            session,
            project_id=project_id,
            event_type="deployment.forced_resume",
            severity=AlertSeverity.ERROR,
            title=title,
            message=msg,
            source="deploy_health_alerts",
            dedupe_key=f"deployment.forced_resume:{project_id}",
        )
        if alert is None or not created:
            continue
        await broadcast_to_project(
            str(project_id),
            "alert_created",
            {"alert_id": str(alert.id), "severity": "ERROR", "title": alert.title},
        )
    await _send_webhook_notification("FORCED_RESUME", title, msg)
    try:
        import asyncio as _asyncio  # noqa: PLC0415
        from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415

        for project_id in project_ids:
            _asyncio.ensure_future(
                dispatch_external_notification(
                    project_id,
                    "FORCED_RESUME",
                    {
                        "pause_duration_seconds": int(pause_duration_seconds),
                        "failed_deps": failed_deps,
                        "title": title,
                    },
                )
            )
    except Exception as exc:
        logger.warning(f"[NOTIFY] FORCED_RESUME dispatch setup failed: {exc}")


async def _create_auto_resume_alerts(
    session: AsyncSession,
    flapping: bool = False,
    recent_resume_count: int = 0,
) -> None:
    """Announce an auto-resume, letting alert policy decide what stays durable.

    Both branches used to construct ``Alert(...)`` directly, once per project,
    with no dedupe key and nothing that would ever dismiss them. A resume is a
    point-in-time event rather than a standing condition, so no
    condition-derived sweep could retire them either: measured on the live feed
    2026-08-21, ``deployment.auto_resume`` accounted for 473 of the 1000 most
    recent open alerts -- 77 bursts of one row per project over 16 days, every
    one of them a notice that its own creation had already made obsolete, and
    not one of them dismissed. That 473 is a floor, not a total: the endpoint
    caps at 1000 and returned 1000 open rows. Counting the 30 open
    ``deployment.flapping_resume`` rows with it, this one function is over half
    the visible operator feed.

    Alert policy already said as much. ``deployment.auto_resume`` carries
    ``route=ROUTE_TIMELINE`` -- "timeline notice, not a durable alert row" --
    and the emitter simply never asked. Routing through
    :func:`create_project_alert_if_allowed` makes the policy authoritative and
    splits the two branches on their own merits:

    * a non-flapping resume (INFO, ``route=ROUTE_TIMELINE``) creates nothing
      durable. Dismissing the paired pause WARNING was always the part that
      carried operator signal, and that still happens.
    * a flapping resume (ERROR, default ``route=ROUTE_ALERT``) stays durable
      and action-required, but a per-project ``dedupe_key`` now collapses
      repeats onto one row with a climbing ``occurrence_count`` instead of
      minting a fresh row per event.

    ``_create_halted_recovery_alerts`` in ``alert_dispatch`` already does
    exactly this for the ``deployment.halted`` / ``deployment.halted_recovered``
    pair. This brings the resume path in line with it.
    """
    pause_title = "Pipeline auto-paused: external dependencies down"
    if flapping:
        resume_title = (
            "Pipeline auto-resumed (FLAPPING): rapid pause/resume cycles detected."
        )
        severity = AlertSeverity.ERROR
        event_type = "deployment.flapping_resume"
    else:
        resume_title = "Pipeline auto-resumed: all external dependencies healthy."
        severity = AlertSeverity.INFO
        event_type = "deployment.auto_resume"
    now = _utcnow()
    project_ids = (
        await session.execute(select(Project.id).limit(10000))
    ).scalars().all()
    if len(project_ids) >= 10000:
        logger.warning(
            "[HEALTH] Project ID fetch hit cap of 10000 — alert sweep truncated; "
            "unfetched projects will be handled on the next sweep cycle"
        )
    # Batch-dismiss existing pause-warning alerts across all projects in one query
    all_existing_warnings = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id.in_(project_ids),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == pause_title,
                )
            )
        )
        .scalars()
        .all()
    )
    for warning in all_existing_warnings:
        warning.dismissed_at = now
        session.add(warning)
    if not flapping:
        # A healthy resume ends the force-resume condition too. Retire rows
        # from both the pre-policy emitter and the policy-owned dedupe path.
        forced_resume_alerts = (
            (
                await session.execute(
                    select(Alert).where(
                        Alert.project_id.in_(project_ids),
                        Alert.event_type == "deployment.forced_resume",
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for forced_resume in forced_resume_alerts:
            forced_resume.dismissed_at = now
            session.add(forced_resume)
    # Retire the pre-dedupe backlog of this same family. Rows minted by the old
    # emitter carry no dedupe_key, so the upsert below can neither find nor
    # supersede them and they would stay open forever. Deliberately scoped to
    # the family being emitted: a clean resume must never clear a standing
    # FLAPPING alert, because one healthy resume does not refute the pattern
    # that alert is reporting.
    superseded_notices = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id.in_(project_ids),
                    Alert.event_type == event_type,
                    Alert.dedupe_key.is_(None),
                    Alert.dismissed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for superseded in superseded_notices:
        superseded.dismissed_at = now
        session.add(superseded)
    for project_id in project_ids:
        alert, _created, _decision = await create_project_alert_if_allowed(
            session,
            project_id=project_id,
            event_type=event_type,
            severity=severity,
            title=resume_title,
            message=resume_title,
            source="deploy_health_alerts",
            dedupe_key=f"{event_type}:{project_id}",
        )
        if alert is None:
            continue
        await broadcast_to_project(
            str(project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": severity.name,
                "title": alert.title,
            },
        )
    if flapping:
        from app.notification import _send_webhook_notification  # noqa: PLC0415

        recent_failed = list(_shared_state.external_health.get("failed_deps", []) or [])
        flap_payload = {
            "title": resume_title,
            "recent_resume_count": recent_resume_count,
            "current_failed_deps": recent_failed,
        }
        webhook_msg = resume_title
        if recent_failed:
            webhook_msg = (
                f"{resume_title} (current failed deps: {', '.join(recent_failed)})"
            )
        await _send_webhook_notification("FLAPPING_RESUME", resume_title, webhook_msg)
        try:
            import asyncio as _asyncio  # noqa: PLC0415
            from app.notification_dispatch import dispatch_external_notification  # noqa: PLC0415

            for project_id in project_ids:
                _asyncio.ensure_future(
                    dispatch_external_notification(
                        project_id,
                        "FLAPPING_RESUME",
                        flap_payload,
                    )
                )
        except Exception as exc:
            logger.warning(f"[NOTIFY] FLAPPING_RESUME dispatch setup failed: {exc}")
