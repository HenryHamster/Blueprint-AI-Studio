"""Housekeeping sweep helpers for the reaper (Steps 6-10).

Extracted from reaper.reaper_service. Each async function covers one
labelled phase and is called sequentially inside the reaper's per-cycle
try/except block.  All helpers preserve the original log prefix strings
and commit/error-handling behaviour.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from sqlmodel import select

from app.models import Task, TaskStatus
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


# Prefix written by the dispatcher's precondition-defer filters
# (orchestrator_filters.py) onto Task.latest_status_message when a task is
# kept QUEUED because a precondition isn't yet satisfiable — a file conflict
# with an in-progress task, an overlap with a not-yet-merged rollup, or
# serialization behind a pending migration.  The age-escalation sweep reads
# this prefix to surface the real, already-recorded blocker as the alert's
# likely cause instead of a generic infrastructure guess.
_PRECONDITION_DEFER_PREFIX = "Waiting:"


# Per-(provider, model) regression scan cadence.  Moved here from reaper.py.
_REGRESSION_SCAN_INTERVAL_SECONDS = 15 * 60
_last_regression_scan: datetime | None = None


async def _run_regression_scan_and_notify(session) -> int:
    """Run the (provider, model) regression scan and fan out alerts per fired event.

    Moved from reaper.py.  Returns the number of newly-fired quarantine events.
    """
    from app.max_plan_pool import scan_account_model_regressions
    from app.notification_dispatch import (
        dispatch_external_notification,
        post_account_model_regression_message,
    )
    from app.models import ProjectMember

    fired = await scan_account_model_regressions(session)
    if not fired:
        return 0

    user_projects_cache: dict[str, list[uuid.UUID]] = {}
    for event in fired:
        user_id = event.get("user_id")
        if not user_id:
            logger.warning(
                f"[REAPER] regression-quarantine for {event['account_id']} has no user_id — "
                "skipping notification fan-out"
            )
            continue
        if user_id not in user_projects_cache:
            rows = (
                (
                    await session.execute(
                        select(ProjectMember.project_id).where(
                            ProjectMember.user_id == user_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            user_projects_cache[user_id] = list(rows)
        project_ids = user_projects_cache[user_id]
        if not project_ids:
            logger.info(
                f"[REAPER] regression-quarantine fired for user {user_id} with no projects — "
                "skipping notification fan-out (event recorded in pool state)"
            )
            continue
        payload = {
            "account_id": event["account_id"],
            "account_label": event["account_label"],
            "provider": event["provider"],
            "model": event["model"],
            "account_rate": event["account_rate"],
            "project_median": event["project_median"],
            "sample_size": event["sample_size"],
            "distinct_accounts": event["distinct_accounts"],
            "started_at": event["started_at"],
        }
        for pid in project_ids:
            try:
                await post_account_model_regression_message(
                    session,
                    pid,
                    account_id=event["account_id"],
                    account_label=event["account_label"],
                    provider=event["provider"],
                    model=event["model"],
                    account_rate=event["account_rate"],
                    project_median=event["project_median"],
                    sample_size=event["sample_size"],
                    distinct_accounts=event["distinct_accounts"],
                    started_at=event["started_at"],
                )
            except Exception:
                logger.exception(
                    f"[REAPER] post_account_model_regression_message failed for project {pid}"
                )
            asyncio.ensure_future(  # noqa: RUF006
                dispatch_external_notification(
                    pid,
                    "account_model_regression_quarantined",
                    payload,
                )
            )
    try:
        await session.commit()
    except Exception:
        logger.exception(
            "[REAPER] Failed to commit regression quarantine notifications"
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception("[REAPER] Rollback after regression-notify commit failed")
    return len(fired)


async def _purge_stale_routine_messages(session, retention_days: int = 7) -> int:
    """Delete open system-authored routine messages older than retention_days. Returns count."""
    from app.models import ProjectMessage
    from app.routers.admin_notifications import ROUTINE_EVENT_TYPES
    from sqlalchemy import delete

    cutoff = _utcnow() - timedelta(days=retention_days)
    res = await session.execute(
        delete(ProjectMessage).where(
            ProjectMessage.created_at < cutoff,
            ProjectMessage.from_type == "system",
            ProjectMessage.status == "open",
            ProjectMessage.context["event_type"].astext.in_(list(ROUTINE_EVENT_TYPES)),
        )
    )
    return res.rowcount


async def sweep_queued_age_alerts(
    session,
    now: datetime,
    *,
    runtime_config,
    batch_size: int = 5000,
) -> int:
    """Step 6: QUEUED task age alerts — 3-tier INFO/WARNING/ERROR escalation.

    Returns the number of alerts fired this cycle.
    """
    from app.models import Alert, AlertSeverity
    from app.broadcast import broadcast_to_project as _bcast
    import app.shared_state as _shared_state

    _queued_threshold_hours = runtime_config.get("queued_age_alert_hours", 2)
    _queued_cutoff = now - timedelta(hours=_queued_threshold_hours)
    # Audit R3-W2 (H7): cap per-tick fetch at batch_size rows.
    _stale_queued = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.status == TaskStatus.QUEUED,
                    Task.created_at <= _queued_cutoff,
                )
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(_stale_queued) >= batch_size:
        logger.warning(
            "[REAPER] stale-queued sweep hit cap=%d rows — result truncated; "
            "follow-up: chunk-paginate",
            batch_size,
        )

    _tier_specs = [
        (1, _queued_threshold_hours, AlertSeverity.INFO, "INFO"),
        (2, _queued_threshold_hours * 3, AlertSeverity.WARNING, "WARNING"),
        (3, _queued_threshold_hours * 6, AlertSeverity.ERROR, "ERROR"),
    ]

    _alerted = 0
    for _qt in _stale_queued:
        _qctx = _qt.context or {}
        _prev_tier = _qctx.get("queued_age_alert_tier") or (
            1 if _qctx.get("queued_age_alert_fired") else 0
        )
        _queue_hours = (now - _qt.created_at).total_seconds() / 3600
        _target_tier, _target_severity, _target_label = (0, AlertSeverity.INFO, "INFO")
        for _tier, _hrs, _sev, _sev_label in _tier_specs:
            if _queue_hours >= _hrs:
                _target_tier, _target_severity, _target_label = (
                    _tier,
                    _sev,
                    _sev_label,
                )
        if _target_tier <= _prev_tier:
            continue

        _qt_type = (_qt.first_payload or {}).get("type", "unknown")
        # Audit W1: retry_after_until is a column.
        _ra_dt = _qt.retry_after_until
        _ra_raw = _ra_dt.isoformat() if _ra_dt else None
        _ra_recent_cutoff = now - timedelta(minutes=5)
        if _qt.requires_approval:
            _likely_cause = "awaiting operator approval"
        elif _ra_dt is not None and _ra_dt >= _ra_recent_cutoff:
            _resets = int(_qctx.get("_retry_after_resets") or 0)
            if _ra_dt > now:
                _held_min = (now - _qt.created_at).total_seconds() / 60
                _likely_cause = (
                    f"rate-limit retry delay active (retry_after={_ra_raw}, "
                    f"held {_held_min:.1f}m, reset {_resets} times)"
                )
            else:
                _expired_min = (now - _ra_dt).total_seconds() / 60
                _likely_cause = (
                    f"rate-limit retry delay recently expired ({_expired_min:.1f}m ago, "
                    f"reset {_resets} times)"
                )
        elif (_qt.latest_status_message or "").startswith(
            _PRECONDITION_DEFER_PREFIX
        ):
            # The dispatcher already recorded the authoritative, actionable
            # blocker at defer time (e.g. waiting on a pending rollup, a file
            # conflict with a running task, or a serializing migration). Surface
            # it verbatim — dropping the "Waiting: " lead so it reads naturally
            # after "Likely cause: " — in preference to the generic
            # infrastructure heuristics below.
            _likely_cause = (
                _qt.latest_status_message[len(_PRECONDITION_DEFER_PREFIX) :].strip()
                or _qt.latest_status_message
            )
        else:
            _rate, _sample = _shared_state.get_task_type_failure_rate(
                _qt_type, window_minutes=15
            )
            if _sample >= 3 and _rate >= 0.7:
                _likely_cause = f"type failure gate active ({_qt_type}: {_rate:.0%} failure rate)"
            elif (
                _shared_state.tasks_active_gauge
                >= runtime_config.get("max_concurrent_vms", 8)
            ):
                _likely_cause = "all accounts healthy but VM slot limit reached"
            else:
                from app.max_plan_pool import (
                    describe_pool_blockage,
                    _extract_task_model,
                )

                _likely_cause = describe_pool_blockage(_extract_task_model(_qt))

        _alert = Alert(
            project_id=_qt.project_id,
            severity=_target_severity,
            title=f"Task stalled in queue: {_qt.title or str(_qt.id)[:8]}",
            message=(
                f"Task type: {_qt_type}. "
                f"Queued for {_queue_hours:.1f}h (threshold: {_queued_threshold_hours}h, tier {_target_tier}). "
                f"Likely cause: {_likely_cause}."
            ),
            task_id=_qt.id,
        )
        session.add(_alert)

        _new_qctx = dict(_qctx)
        _new_qctx["queued_age_alert_tier"] = _target_tier
        _new_qctx["queued_age_alert_fired"] = True
        _qt.context = _new_qctx
        session.add(_qt)

        try:
            await _bcast(
                str(_qt.project_id),
                "alert_created",
                {
                    "alert_id": str(_alert.id),
                    "severity": _target_label,
                    "title": _alert.title,
                },
            )
        except Exception as _bcast_err:  # noqa: BLE001
            logger.warning(
                f"[REAPER] Queued age alert broadcast failed for {_qt.id}: {_bcast_err}"
            )

        _alerted += 1
        logger.info(
            f"[REAPER] Queued age alert tier {_target_tier} ({_target_label}) fired for task {_qt.id} "
            f"(type={_qt_type}, queued={_queue_hours:.1f}h, cause={_likely_cause})"
        )

    if _alerted:
        await session.commit()
        logger.info(
            f"[REAPER] Fired queued age alerts for {_alerted} stale task(s)"
        )

    return _alerted


async def sweep_stalled_strategic_cycles(
    session,
    now: datetime,
    *,
    heartbeat_stale_cutoff_fn,
) -> int:
    """Step 7: Stalled strategic-cycle reaper.

    Returns the number of cycles marked stalled.
    """
    from app import strategic_cycle
    from app.models import Alert, AlertSeverity
    from app.broadcast import broadcast_to_project as _bcast

    stalled = await strategic_cycle.find_stalled(session)
    _step7_now = _utcnow()
    actually_stalled: list = []
    for c in stalled:
        if c.outcome in (
            strategic_cycle.OUTCOME_IN_PROGRESS,
            strategic_cycle.OUTCOME_PHASE1_RUNNING,
            strategic_cycle.OUTCOME_PHASE1_DONE,
        ):
            stalled_phase = "phase1"
            linked_task_id = c.phase1_task_id
        else:
            stalled_phase = "phase2"
            linked_task_id = c.phase2_task_id

        if linked_task_id is not None:
            linked_task = await session.get(Task, linked_task_id)
            if linked_task is not None and linked_task.status in (
                TaskStatus.QUEUED,
                TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS,
            ):
                hb = linked_task.last_heartbeat
                hb_cutoff = heartbeat_stale_cutoff_fn(linked_task, _step7_now)
                if hb is not None and hb >= hb_cutoff:
                    logger.info(
                        f"[REAPER] Cycle {c.id} in {stalled_phase} >3h "
                        f"but linked task {linked_task_id} has fresh "
                        f"heartbeat ({hb}) — skipping stall declaration"
                    )
                    continue
                linked_task.status = TaskStatus.FAILED
                linked_task.latest_status_message = (
                    f"Force-terminated by stalled-cycle reaper — "
                    f"cycle {c.id} declared stalled after >3h"
                )
                session.add(linked_task)
                logger.info(
                    f"[REAPER] Force-terminated stale task "
                    f"{linked_task_id} linked to stalled cycle {c.id}"
                )

        if stalled_phase == "phase1":
            c.outcome = strategic_cycle.OUTCOME_STALLED_PHASE1
        else:
            c.outcome = strategic_cycle.OUTCOME_STALLED_PHASE2
        if c.failure_reason is None:
            c.failure_reason = f"reaper_stall: cycle in {stalled_phase} >3h with no completion"
        session.add(c)
        actually_stalled.append(c)
        try:
            alert = Alert(
                project_id=c.project_id,
                severity=AlertSeverity.WARNING,
                title=f"Strategic cycle {c.id} stalled in {stalled_phase}",
                message=(
                    f"Cycle {c.id} has been in {stalled_phase} for >3h with no "
                    f"completion handler update. Marking stalled — next strategic "
                    f"cycle will spawn a fresh attempt. Phase1 task: {c.phase1_task_id}, "
                    f"phase2 task: {c.phase2_task_id}."
                ),
            )
            session.add(alert)
            await _bcast(
                str(c.project_id),
                "alert_created",
                {
                    "alert_id": str(alert.id),
                    "severity": "WARNING",
                    "title": alert.title,
                },
            )
        except Exception:
            logger.exception(
                f"[REAPER] Failed to create stalled-cycle alert for {c.id}"
            )
    if actually_stalled:
        await session.commit()
        logger.info(
            f"[REAPER] Marked {len(actually_stalled)} strategic cycle(s) stalled"
        )

    return len(actually_stalled)


async def sweep_regression_scan(session) -> int:
    """Step 8: Per-(provider, model) regression scan.

    Gated to ~15 min cadence via module-level _last_regression_scan.
    Returns the number of newly-fired quarantine events (0 when cadence gate skips).
    """
    global _last_regression_scan

    now_scan = _utcnow()
    if (
        _last_regression_scan is not None
        and (now_scan - _last_regression_scan).total_seconds()
        < _REGRESSION_SCAN_INTERVAL_SECONDS
    ):
        return 0

    _last_regression_scan = now_scan
    return await _run_regression_scan_and_notify(session)


async def sweep_stuck_qualifications(session, *, session_factory) -> int:
    """Step 9: Finalize release qualifications stuck 'pending'.

    Returns the number of qualifications finalized.
    """
    from app import release_qualifier

    finalized = await release_qualifier.sweep_stuck_qualifications(
        session, session_factory=session_factory
    )
    if finalized:
        logger.info(
            f"[REAPER] Finalized {finalized} stuck release qualification(s)"
        )
    return finalized or 0


async def sweep_routine_message_retention(session, *, retention_days: int = 7) -> int:
    """Step 10: Routine message retention — purge stale routine ops messages.

    Returns the number of messages purged.
    """
    purged = await _purge_stale_routine_messages(session, retention_days=retention_days)
    if purged:
        await session.commit()
        logger.info(f"[REAPER] Purged {purged} stale routine message(s)")
    return purged
