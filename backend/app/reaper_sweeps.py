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
    pending_broadcasts: list[tuple[str, dict]] = []
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

        pending_broadcasts.append(
            (
                str(_qt.project_id),
                {
                    "alert_id": str(_alert.id),
                    "severity": _target_label,
                    "title": _alert.title,
                },
            )
        )

        _alerted += 1
        logger.info(
            f"[REAPER] Queued age alert tier {_target_tier} ({_target_label}) fired for task {_qt.id} "
            f"(type={_qt_type}, queued={_queue_hours:.1f}h, cause={_likely_cause})"
        )

    if _alerted:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "alert_created", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    f"[REAPER] Queued age alert broadcast failed for "
                    f"{payload.get('alert_id')}: {_bcast_err}"
                )
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
    pending_broadcasts: list[tuple[str, dict]] = []
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
            pending_broadcasts.append(
                (
                    str(c.project_id),
                    {
                        "alert_id": str(alert.id),
                        "severity": "WARNING",
                        "title": alert.title,
                    },
                )
            )
        except Exception:
            logger.exception(
                f"[REAPER] Failed to create stalled-cycle alert for {c.id}"
            )
    if actually_stalled:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "alert_created", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    f"[REAPER] Stalled-cycle alert broadcast failed for "
                    f"{payload.get('alert_id')}: {_bcast_err}"
                )
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


# ── Step 12: BLOCKED-task auto-unblock / escalation sweep ──────────────────
#
# A task enters the first-class BLOCKED state when a durable precondition is
# missing — an orchestrator-discovered conflict that persisted past the defer
# threshold (orchestrator_filters._record_defer_and_maybe_escalate) or an
# agent/lifecycle "BLOCKER:" with an upstream dependency. BLOCKED tasks are
# undispatchable on the normal queue path (the candidate query is QUEUED-only)
# and are excluded from stale-task reaping. This sweep is the recovery path:
# it cheaply rechecks each BLOCKED task at/after its recheck_after and either
# moves it back to QUEUED (precondition cleared) or re-arms a later recheck.
# A blocker that is permanently unsatisfiable (its dependency cascade-cancelled)
# or that outlives the escalation deadline gets ONE deduplicated operator alert
# and stops being rechecked.

# Base recheck interval, doubled per consecutive still-blocked recheck up to the
# cap. Keeps the sweep cheap (no per-cycle re-judging) while staying responsive.
_BLOCKED_RECHECK_BASE = timedelta(minutes=5)
_BLOCKED_RECHECK_MAX = timedelta(hours=1)
# A blocker that never clears within this window is escalated to the operator
# as permanently blocked (one ERROR alert) and parked.
_BLOCKED_ESCALATE_AFTER = timedelta(hours=6)
# Parking horizon for terminally-escalated blockers. On escalation we push
# ``recheck_after`` far into the future (rather than leaving it NULL) so the row
# drops out of the due query (``recheck_after IS NULL OR <= now``) and is never
# re-SELECTed / FOR UPDATE-locked on the reaper hot path again. Leaving it NULL
# kept escalated rows permanently "due" (sorted first by nullsfirst), where they
# were re-locked every cycle and could crowd genuinely-due rechecks out of the
# batch-size window. Recovery has given up on these; an operator re-queues them.
_BLOCKED_ESCALATED_PARK = timedelta(days=3650)


def _next_recheck_delay(attempts: int) -> timedelta:
    """Exponential backoff for the next recheck, capped at ``_BLOCKED_RECHECK_MAX``."""
    secs = _BLOCKED_RECHECK_BASE.total_seconds() * (2 ** max(0, attempts))
    return timedelta(seconds=min(secs, _BLOCKED_RECHECK_MAX.total_seconds()))


async def _blocked_precondition_state(task, session, verify_pr_merged) -> str:
    """Classify whether a BLOCKED task's recorded precondition is now satisfied.

    Returns one of:
      - ``"clear"``   — the precondition is satisfied (or is a transient conflict
                        with no dependency edge); requeue and let dispatch run/
                        re-judge it.
      - ``"waiting"`` — an upstream dependency is still in flight; stay BLOCKED.
      - ``"dead"``    — an upstream dependency genuinely failed / was cancelled,
                        so this work is impossible; escalate terminally.

    Reuses the single source of truth for dependency resolution
    (``orchestrator_health._check_candidate_task_deps``) rather than
    reimplementing merge/cancel/scope-empty semantics here.
    """
    deps = task.depends_on or []
    blocker_meta = (task.context or {}).get("blocker") or {}
    blocker_source = blocker_meta.get("source") if isinstance(blocker_meta, dict) else None
    if not deps:
        if task.blocker_kind in {"file_conflict", "pending_rollup"}:
            # Orchestrator-owned blockers with no dependency edges are still
            # safe to requeue optimistically: dispatch re-judges and re-blocks
            # them immediately if the conflict is still live.
            return "clear"
        if blocker_source == "parent":
            children = (
                (
                    await session.execute(
                        select(Task).where(Task.parent_task_id == task.id)
                    )
                )
                .scalars()
                .all()
            )
            for child in children:
                child_ctx = child.context or {}
                if child.status == TaskStatus.BLOCKED:
                    return "waiting"
                if child.status == TaskStatus.FAILED and (
                    child_ctx.get("failure_category") == "BLOCKED_PRECONDITION"
                    or child_ctx.get("failure_class") == "BLOCKED_PRECONDITION"
                    or (child.latest_status_message or "").lstrip().lower().startswith(
                        "blocker:"
                    )
                ):
                    return "waiting"
            # Child blockers have cleared; move the parent back into the
            # rollup/re-evaluation path instead of normal dispatch.
            return "re_evaluate"
        if verify_pr_merged is None:
            return "waiting"
        # Agent/lifecycle blockers with no explicit dependency edge are durable
        # preconditions (missing base branch/file, upstream PR not yet landed).
        # Keep them parked and back off; the deadline escalation path surfaces
        # long-lived blockers for operator intervention.
        return "waiting"
    if verify_pr_merged is None:
        return "waiting"

    from app.orchestrator_health import _check_candidate_task_deps

    dep_uuids = []
    for d in deps:
        try:
            dep_uuids.append(uuid.UUID(str(d)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not dep_uuids:
        return "clear"
    dep_tasks = (
        (await session.execute(select(Task).where(Task.id.in_(dep_uuids))))
        .scalars()
        .all()
    )
    result, _ = await _check_candidate_task_deps(
        task, dep_tasks, dep_uuids, session, verify_pr_merged
    )
    if result == "eligible":
        return "clear"
    if result == "cascade_cancel":
        return "dead"
    return "waiting"


def _clear_blocked_fields(task, *, preserve_first_blocked: bool = False) -> None:
    """Reset a task's durable blocker metadata and defer bookkeeping on unblock.

    Delegates the structured ``blocker`` record and the BLOCKED_PRECONDITION
    compat-tag clearing to ``task_blocked.clear_blocker_metadata`` — the single
    source of truth for "what a clean out-transition drops" — so the reaper's
    recovery path and the canonical out-transition cannot drift. This sweep
    then clears its own recheck/escalation bookkeeping keys on top.

    ``preserve_first_blocked`` keeps the ``_blocked_first_at`` onset stamp so a
    blocker that is only *optimistically* requeued — a conflict/rollup blocker
    with no dependency edge, which dispatch may immediately re-block — still
    measures its escalation deadline from the true onset rather than restarting
    each requeue episode. A genuine dependency clear wipes everything.
    """
    from app.task_blocked import clear_blocker_metadata

    # Drops blocker columns, the structured blocker record, and the stale
    # BLOCKED_PRECONDITION failure_class/failure_category tags.
    clear_blocker_metadata(task)
    ctx = task.context or {}
    keys = [
        "_precondition_defer",
        "_precondition_escalated_key",
        "_blocked_escalated",
        "_blocked_recheck_attempts",
    ]
    if not preserve_first_blocked:
        keys.append("_blocked_first_at")
    for k in keys:
        ctx.pop(k, None)
    task.context = ctx


async def sweep_unblock_blocked_tasks(
    session,
    now: datetime,
    *,
    verify_pr_merged=None,
    batch_size: int = 200,
) -> int:
    """Step 12: re-evaluate due BLOCKED tasks; requeue or escalate them.

    Idempotent and concurrency-safe alongside the orchestrator/reaper: due rows
    are claimed with ``FOR UPDATE SKIP LOCKED`` so two sweeps (or a sweep racing
    the orchestrator) never double-process the same task.

    Returns the number of tasks moved back to QUEUED.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.alert_dispatch import _maybe_create_blocked_escalation_alert
    from app.broadcast import broadcast_to_project as _bcast

    if verify_pr_merged is None:
        from app.rollup_pr_verification import _verify_pr_merged
        verify_pr_merged = _verify_pr_merged

    due_stmt = (
        select(Task)
        .where(
            Task.status == TaskStatus.BLOCKED,
            (Task.recheck_after.is_(None)) | (Task.recheck_after <= now),
        )
        .order_by(Task.recheck_after.asc().nullsfirst())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    due = (await session.execute(due_stmt)).scalars().all()
    if not due:
        return 0

    requeued = 0
    changed = False
    # task_updated broadcasts are deferred until after the batch commit so the
    # admin UI (which refetches task/digest data on these events) never observes
    # a stale BLOCKED row — or, on rollback, a transition that never persisted.
    pending_broadcasts: list[tuple[str, dict]] = []

    async def _escalate_blocked_task(
        task,
        ctx,
        *,
        blocker_kind: str,
        detail: str,
    ) -> None:
        from app.task_blocked import mark_blocker_escalated

        await _maybe_create_blocked_escalation_alert(
            task,
            session,
            blocker_kind=blocker_kind,
            detail=detail,
        )
        ctx["_blocked_escalated"] = True
        task.context = ctx
        # Terminal escalation keeps the task parked in BLOCKED for the operator,
        # so reflect that in the structured record (escalated -> True) rather
        # than leaving the in-transition's escalated=False stale.
        mark_blocker_escalated(task, detail=detail, now=now)
        flag_modified(task, "context")
        task.recheck_after = now + _BLOCKED_ESCALATED_PARK
        session.add(task)

    for task in due:
        ctx = task.context or {}
        # Already escalated terminally — leave parked for the operator. On
        # escalation we push recheck_after to a far-future sentinel so these
        # rows drop out of the due set entirely. Backstop: if a legacy/None
        # recheck_after let one resurface here, evict it the same way so it
        # stops being re-SELECTed and FOR UPDATE-locked every reaper cycle.
        if ctx.get("_blocked_escalated"):
            if task.recheck_after is None or task.recheck_after <= now:
                task.recheck_after = now + _BLOCKED_ESCALATED_PARK
                session.add(task)
                changed = True
            continue

        state = await _blocked_precondition_state(task, session, verify_pr_merged)
        if state == "clear":
            blocker_meta = (task.context or {}).get("blocker") or {}
            blocker_source = (
                blocker_meta.get("source") if isinstance(blocker_meta, dict) else None
            )
            # Conflict/rollup blockers are transient-but-self-re-blocking:
            # dispatch requeues them every recheck interval and re-blocks if
            # the precondition still holds. Their deadline must measure from
            # the TRUE first-blocked onset, so they are escalation-eligible and
            # onset-preserving even when they carry stale, already-satisfied
            # dependency edges (a successfully-completed dep id is never
            # stripped from depends_on). Key the discriminator off the actual
            # blocker identity, NOT the mere presence of dependency edges —
            # otherwise a conflict-blocked task with a satisfied dep would skip
            # escalation and reset its onset every cycle, looping forever.
            is_optimistic_conflict = task.blocker_kind in {
                "file_conflict",
                "pending_rollup",
            }
            is_genuine_clear = (not is_optimistic_conflict) and (
                bool(task.depends_on) or blocker_source == "parent"
            )
            if (
                not is_genuine_clear
                and task.blocked_at is not None
                and (now - task.blocked_at) >= _BLOCKED_ESCALATE_AFTER
            ):
                await _escalate_blocked_task(
                    task,
                    ctx,
                    blocker_kind=task.blocker_kind or "unknown",
                    detail=(
                        f"Task has been blocked since {task.blocked_at:%Y-%m-%d %H:%M} "
                        f"without clearing: {task.blocked_reason or task.latest_status_message or ''}"
                    ),
                )
                changed = True
                continue
            # A conflict/rollup blocker is requeued optimistically and must
            # preserve its first-blocked onset across the requeue (so the next
            # block episode keeps counting toward the deadline) — unless it has
            # already crossed the deadline and was escalated above. A genuine
            # dependency/parent clear resets all, including the onset.
            _clear_blocked_fields(
                task, preserve_first_blocked=is_optimistic_conflict
            )
            task.status = TaskStatus.QUEUED
            task.latest_status_message = "Unblocked — requeued for dispatch"
            flag_modified(task, "context")
            session.add(task)
            requeued += 1
            changed = True
            pending_broadcasts.append(
                (
                    str(task.project_id),
                    {"task_id": str(task.id), "status": "QUEUED",
                     "message": task.latest_status_message},
                )
            )
        elif state == "re_evaluate":
            _clear_blocked_fields(task, preserve_first_blocked=False)
            task.status = TaskStatus.EVALUATING
            task.latest_status_message = (
                "Unblocked — awaiting rollup re-evaluation"
            )
            flag_modified(task, "context")
            session.add(task)
            changed = True
            pending_broadcasts.append(
                (
                    str(task.project_id),
                    {
                        "task_id": str(task.id),
                        "status": "EVALUATING",
                        "message": task.latest_status_message,
                    },
                )
            )
        elif state == "dead":
            await _escalate_blocked_task(
                task,
                ctx,
                blocker_kind=task.blocker_kind or "upstream dependency",
                detail=(
                    "Upstream dependency failed or was cancelled — this task can "
                    "never run as-is and needs operator intervention "
                    "(re-scope, re-point dependency, or cancel)."
                ),
            )
            changed = True
        else:  # "waiting" — re-arm a later recheck with backoff.
            # Deadline escalation stays active for blockers that are still
            # genuinely unresolved at the 6h mark.
            if (
                task.blocked_at is not None
                and (now - task.blocked_at) >= _BLOCKED_ESCALATE_AFTER
            ):
                await _escalate_blocked_task(
                    task,
                    ctx,
                    blocker_kind=task.blocker_kind or "unknown",
                    detail=(
                        f"Task has been blocked since {task.blocked_at:%Y-%m-%d %H:%M} "
                        f"without clearing: {task.blocked_reason or task.latest_status_message or ''}"
                    ),
                )
                changed = True
                continue
            attempts = int(ctx.get("_blocked_recheck_attempts", 0)) + 1
            ctx["_blocked_recheck_attempts"] = attempts
            task.context = ctx
            flag_modified(task, "context")
            task.recheck_after = now + _next_recheck_delay(attempts)
            session.add(task)
            changed = True

    if changed:
        await session.commit()
    # Emit task_updated only after the batch commit succeeds, so a rollback
    # never publishes a transition the DB didn't keep. Per-broadcast failures
    # are swallowed so one bad emit can't break the sweep or skip the rest.
    for project_id, payload in pending_broadcasts:
        try:
            await _bcast(project_id, "task_updated", payload)
        except Exception:
            logger.debug(
                f"[REAPER] Failed to broadcast {payload.get('status')} "
                f"for {payload.get('task_id')}",
                exc_info=True,
            )
    if requeued:
        logger.info(f"[REAPER] Step 12: unblocked {requeued} task(s) → QUEUED")
    return requeued
