"""Housekeeping sweep helpers for the reaper (Steps 6-10).

Extracted from reaper.reaper_service. Each async function covers one
labelled phase and is called sequentially inside the reaper's per-cycle
try/except block.  All helpers preserve the original log prefix strings
and commit/error-handling behaviour.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlmodel import select

from app.depends_on_provenance import remember_structural_depends_on
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
# A parked (escalated) blocker that STILL carries a machine-resolvable need is
# re-examined at this LOW, bounded cadence instead of the multi-year park
# horizon, so that when the foundation it waited on finally lands — a sibling
# task, a merged PR, or shipped code now reachable on integration — the task
# resumes automatically rather than waiting for a manual operator requeue. The
# re-evaluation is cheap (one conservative exact-match pass) and never
# re-escalates or re-alerts, so it does not reintroduce per-cycle churn. Parked
# blockers with NO need keep the multi-year horizon and stay for the operator.
_BLOCKED_PARKED_REEVAL_INTERVAL = timedelta(hours=6)
# Bounded number of parked re-evaluations a still-unsatisfiable need is given
# before the dependent is terminated. A need that never becomes satisfiable —
# e.g. a capability no task ever advertises — would otherwise re-arm the parked
# re-evaluation at the low cadence forever. After this many parked re-evals with
# no resolution and no provably-dead foundation, the dependent reaches a bounded
# TERMINAL (CANCELLED) outcome instead of parking forever. The legitimate
# "foundation might still appear" case keeps its resume behavior within the
# bound — only an exhausted, never-satisfiable need terminates.
_BLOCKED_PARKED_REEVAL_CAP = 4
# A no-dependency agent/lifecycle blocker is often a TRANSIENT infra failure
# (orchestrator ./pr open 5xx, git push stale-lease) that clears on its own, but
# can also be a durable precondition (missing base branch/file). We can't tell
# them apart, so we optimistically requeue such a blocker up to this many times —
# a transient blip recovers on retry; a durable blocker re-blocks and, once the
# budget is spent, parks for the normal backoff + 6h operator escalation. The cap
# is deliberately small: re-dispatch costs a full agent VM run, so this trades a
# few wasted runs for auto-recovery instead of a multi-hour operator wedge.
_BLOCKED_OPTIMISTIC_RETRY_CAP = 3

# Durable context marker stamped on a blocked dependent that the resume system
# terminally CANCELS because it can provably never resume. The terminal
# transition clears all structured blocker metadata, so without this marker the
# give-up is invisible in aggregate (only a free-text status message + one
# deduplicated alert survive). The marker is queryable JSONB — no migration — and
# feeds the fleet-wide unresumable-cancellation health signal
# (``digest_metrics.compute_unresumable_cancellation_summary``). Observability
# only: stamping it changes nothing about WHEN or WHETHER a dependent is
# cancelled.
UNRESUMABLE_CANCEL_CONTEXT_KEY = "_unresumable_cancellation"
# Give-up classes — the two reasons a blocked dependent can provably never resume.
GIVE_UP_FOUNDATION_DIED = "foundation_died"  # needed foundation reached terminal FAILED/CANCELLED
GIVE_UP_NEVER_SATISFIABLE = "never_satisfiable"  # need outlived its bounded parked re-evals


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
      - ``"retry"``   — a no-dependency agent/lifecycle blocker that may be a
                        transient infra failure; the driver optimistically
                        requeues it a bounded number of times before parking.
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
        # AGENT-surfaced "BLOCKER:" with no explicit dependency edge (source set
        # by agent_status.py): these are often TRANSIENT infra failures
        # (orchestrator ./pr open 5xx, git push stale-lease) that clear on their
        # own, but can also be durable preconditions (missing base branch/file).
        # We can't reliably tell them apart from here, so signal a BOUNDED
        # optimistic requeue: the driver retries up to
        # _BLOCKED_OPTIMISTIC_RETRY_CAP times before parking + escalating. A
        # transient blip recovers on retry; a durable blocker re-blocks and is
        # parked once the retry budget is spent. (Previously this returned
        # "waiting" unconditionally, which left a transient infra blip wedging
        # its EVALUATING rollup parent for hours until an operator manually
        # requeued — incident 2026-06-04, tasks 87ddb2a4/18c8c195.) Scoped to
        # source=="agent" so parent-source blockers and metadata-less blocked
        # subtasks keep their existing "wait for explicit recovery" semantics.
        if blocker_source == "agent":
            return "retry"
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
        # The optimistic-retry budget is preserved across an onset-preserving
        # requeue (so a transient agent/lifecycle blocker keeps counting toward
        # its cap), but wiped on a genuine dependency clear so a later, unrelated
        # block starts with a fresh budget.
        keys.append("_blocked_optimistic_retries")
    for k in keys:
        ctx.pop(k, None)
    task.context = ctx


async def _fetch_active_candidates(session, project_id) -> list:
    """Active/in-flight tasks in a project — the foundations a need can resolve to.

    Statuses mirror ``blocked_need_resolution.ACTIVE_CANDIDATE_STATUSES``: a need
    can only resolve onto work that is still going to land (QUEUED/PROVISIONING/
    IN_PROGRESS/EVALUATING), never onto already-terminal rows.
    """
    from app.blocked_need_resolution import ACTIVE_CANDIDATE_STATUSES

    active = [TaskStatus(s) for s in ACTIVE_CANDIDATE_STATUSES]
    return (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(active),
                )
            )
        )
        .scalars()
        .all()
    )


async def _classify_blocker_needs(
    task, session, *, candidate_cache: dict, completed_cache: dict, dead_cache: dict
) -> dict:
    """Classify EVERY recorded need of a blocked task against the live foundations.

    A blocked dependent can wait on MORE THAN ONE unresolved prerequisite (see
    :func:`task_blocked.get_blocker_needs`). This evaluates each one against the
    active / completed / dead candidate sets and returns a disposition dict:

      ``had_needs``       — the task carried ≥1 machine-resolvable need
      ``edges``           — foundation ids to wire (need → active in-flight task)
      ``completed_needs`` — needs satisfied by already-completed/merged work
      ``residual``        — needs not yet accounted for (no foundation matches)
      ``dead``            — ≥1 need names a terminally FAILED/CANCELLED foundation

    Resolution per need is active → completed → dead → residual, mirroring the
    single-need ordering. Reuses the shared conservative predicates in
    :mod:`app.blocked_need_resolution` (no parallel matcher); candidate sets are
    fetched lazily and cached per project across the sweep batch. A foundation
    already present in ``task.depends_on`` is treated as already-wired (it is not
    re-added to ``edges``), so a partially-resolved blocker keeps converging.
    """
    from app.task_blocked import get_blocker_needs
    from app.blocked_need_resolution import (
        resolve_blocked_need,
        completed_foundation_for_need,
        need_matched_dead_foundation,
    )

    needs = get_blocker_needs(task)
    disp = {
        "had_needs": bool(needs),
        "edges": [],
        "completed_needs": [],
        "residual": [],
        "dead": False,
        # The first need that matched a terminally-dead foundation, recorded so the
        # foundation-died cancel can stamp it into the unresumable marker.
        "dead_need": None,
    }
    if not needs:
        return disp

    active = candidate_cache.get(task.project_id)
    if active is None:
        active = await _fetch_active_candidates(session, task.project_id)
        candidate_cache[task.project_id] = active
    completed = completed_cache.get(task.project_id)
    if completed is None:
        completed = await _fetch_completed_candidates(session, task.project_id)
        completed_cache[task.project_id] = completed
    dead = dead_cache.get(task.project_id)
    if dead is None:
        dead = await _fetch_dead_candidates(session, task.project_id)
        dead_cache[task.project_id] = dead

    existing_edges = {str(d) for d in (task.depends_on or [])}
    for need in needs:
        match = resolve_blocked_need(task, need, active)
        if match is not None:
            match_id = str(match.id)
            if match_id not in existing_edges and match_id not in disp["edges"]:
                disp["edges"].append(match_id)
            continue
        if completed_foundation_for_need(task, need, completed) is not None:
            disp["completed_needs"].append(need)
            continue
        if need_matched_dead_foundation(task, need, dead):
            disp["dead"] = True
            if disp["dead_need"] is None:
                disp["dead_need"] = need
            continue
        disp["residual"].append(need)
    return disp


def _wire_edges(task, edge_ids: list) -> bool:
    """Idempotently append ``edge_ids`` to ``task.depends_on``. Returns True if any
    edge was newly added. Full-list reassignment so SQLAlchemy observes the JSONB
    change (``flag_modified`` is applied by callers)."""
    existing = [str(d) for d in (task.depends_on or [])]
    added = False
    for match_id in edge_ids:
        if match_id not in existing:
            existing.append(match_id)
            remember_structural_depends_on(task, match_id)
            added = True
    if added:
        task.depends_on = existing
    return added


def _persist_residual_needs(task, residual: list) -> None:
    """Re-persist only the still-unsatisfied needs back into the blocker metadata.

    Drops needs resolved this round (wired into an edge or satisfied by completed
    work) and keeps only ``residual``, recomputing the legacy single ``need``
    mirror = ``residual[0]``. The blocker record is NOT cleared — the task stays
    BLOCKED on the remaining needs so the next tick re-evaluates only those.
    ``residual`` MUST be non-empty (the all-accounted path clears the blocker
    instead).
    """
    from app.task_blocked import BLOCKER_CONTEXT_KEY

    ctx = dict(task.context or {})
    meta = ctx.get(BLOCKER_CONTEXT_KEY)
    if not isinstance(meta, dict):
        return
    meta = dict(meta)
    meta["needs"] = list(residual)
    meta["need"] = residual[0]
    ctx[BLOCKER_CONTEXT_KEY] = meta
    task.context = ctx


async def sweep_converge_duplicate_foundations(session, now=None) -> int:
    """Step 15: serialize HIGH-CONFIDENCE duplicate foundations behind the canonical.

    The duplicate-foundation observability signal flags cross-parent active
    write-tasks with overlapping related files, but that signal is dashboard-only
    because the pipeline intentionally lets independent tasks edit shared-infra
    files in parallel and reconcile at merge. This sweep converges only the
    genuine duplicates the signal catches that slip past spawn-time dedup — a
    shared finding origin, a shared capability marker, or near-identical file sets
    (see ``digest_metrics.compute_foundation_convergence_actions``). For each, it
    wires a ``depends_on`` edge from the later task onto the earlier (canonical)
    one, so the existing held-lane / cross-base re-cut path rebases the duplicate
    on the canonical's landed foundation and builds-on instead of rebuilding.

    Conservative and observable: below the genuine-duplicate predicate the pair is
    left untouched (still merely surfaced by the signal); a self-edge or an edge
    that would close a ``depends_on`` cycle is refused; every wired edge logs and
    stamps the dependent's context so an operator can see why it was serialized.
    Returns the number of edges wired.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.blocked_need_resolution import (
        ACTIVE_CANDIDATE_STATUSES,
        _would_create_cycle,
    )
    from app.broadcast import broadcast_to_project as _bcast
    from app.digest_metrics import compute_foundation_convergence_actions

    active = [TaskStatus(s) for s in ACTIVE_CANDIDATE_STATUSES]
    rows = (
        (await session.execute(select(Task).where(Task.status.in_(active))))
        .scalars()
        .all()
    )
    by_project: dict = {}
    for t in rows:
        by_project.setdefault(t.project_id, []).append(t)

    wired = 0
    pending_broadcasts: list[tuple[str, dict]] = []
    for pid, tasks in by_project.items():
        if len(tasks) < 2:
            continue
        by_id = {str(t.id): t for t in tasks}
        for action in compute_foundation_convergence_actions(tasks):
            dependent = by_id.get(action["dependent_id"])
            foundation = by_id.get(action["foundation_id"])
            if dependent is None or foundation is None:
                continue
            found_id = str(foundation.id)
            if found_id == str(dependent.id):  # defensive self-edge guard
                continue
            existing = [str(d) for d in (dependent.depends_on or [])]
            if found_id in existing:
                continue
            if _would_create_cycle(dependent, foundation, tasks):
                logger.info(
                    f"[REAPER] Step 15: skipped converging {dependent.id} -> "
                    f"{found_id} — edge would close a depends_on cycle"
                )
                continue
            dependent.depends_on = existing + [found_id]
            remember_structural_depends_on(dependent, found_id)
            ctx = dependent.context or {}
            ctx["_foundation_converged"] = {
                "foundation_id": found_id,
                "reason": action["reason"],
                "shared_files": action["shared_files"],
            }
            dependent.context = ctx
            flag_modified(dependent, "depends_on")
            flag_modified(dependent, "context")
            session.add(dependent)
            wired += 1
            logger.info(
                f"[REAPER] Step 15: converged duplicate foundation — task "
                f"{dependent.id} now depends on {found_id} ({action['reason']})"
            )
            pending_broadcasts.append(
                (
                    str(pid),
                    {
                        "task_id": str(dependent.id),
                        "depends_on": dependent.depends_on,
                        "message": f"Serialized behind duplicate foundation: {action['reason']}",
                    },
                )
            )

    if wired:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            with contextlib.suppress(Exception):
                await _bcast(project_id, "task_updated", payload)
        logger.info(f"[REAPER] Step 15: converged {wired} duplicate foundation(s)")
    return wired


async def _fetch_completed_candidates(session, project_id) -> list:
    """Completed/merged tasks in a project — foundations that have already LANDED.

    Statuses mirror ``blocked_need_resolution.COMPLETED_CANDIDATE_STATUSES``: a
    parked blocker's need may now be satisfied by work that has since completed
    (a sibling task done, the PR it named merged), which the active-candidate
    matcher never sees.
    """
    from app.blocked_need_resolution import COMPLETED_CANDIDATE_STATUSES

    completed = [TaskStatus(s) for s in COMPLETED_CANDIDATE_STATUSES]
    return (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(completed),
                )
            )
        )
        .scalars()
        .all()
    )


async def _completed_foundation_base_safe(task, need, completed, session) -> bool:
    """Re-cut a resuming dependent's base when its landed foundation is cross-base.

    A blocked dependent that resumes because its need is satisfied by ALREADY-
    completed work carries NO ``depends_on`` edge, so the dispatch-time re-cut
    (which only inspects edged candidates) can never fire for it. If the completed
    foundation that satisfied the need is a GENUINE cross-base code dependency that
    has landed on integration, the dependent would otherwise dispatch against a
    staging base forked before the foundation landed — exactly the failure the
    re-cut machinery exists to prevent.

    Resolve the actual landed foundation and run it through the SAME shared re-cut
    helper the dispatch/completion paths use (``ensure_dependent_base_recut``):

      * Same-base or ordering-only foundation → no re-cut needed → returns ``True``
        (the dependent resumes exactly as before — no false serialization).
      * Cross-base code foundation, re-cut succeeds → ``True`` (base now contains
        the landed code; safe to resume).
      * Cross-base code foundation, re-cut conflicts or hits a transient error →
        ``False`` — the caller must HOLD the dependent (keep it blocked / re-park
        and retry next cycle), never resume against an un-recut base.
    """
    from app.blocked_need_resolution import completed_foundation_for_need
    from app.orchestrator_health import ensure_dependent_base_recut

    foundation = completed_foundation_for_need(task, need, completed)
    if foundation is None:  # defensive — caller already confirmed satisfaction
        return True
    return await ensure_dependent_base_recut(task, [foundation], session)


async def _fetch_dead_candidates(session, project_id) -> list:
    """Terminally failed/cancelled tasks — foundations that have definitively DIED.

    Statuses mirror ``blocked_need_resolution.TERMINAL_CANDIDATE_STATUSES``: a
    blocked dependent whose named foundation reached one of these states will
    never be satisfied by it, so the dependent is terminated (CANCELLED) rather
    than parked to re-arm forever.
    """
    from app.blocked_need_resolution import TERMINAL_CANDIDATE_STATUSES

    dead = [TaskStatus(s) for s in TERMINAL_CANDIDATE_STATUSES]
    return (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.in_(dead),
                )
            )
        )
        .scalars()
        .all()
    )


async def _cancel_blocker_as_dead(
    task,
    session,
    *,
    detail: str,
    message: str,
    pending_broadcasts: list,
    give_up_class: str,
    need=None,
) -> None:
    """Terminally CANCEL a blocked dependent that can provably never resume.

    Mirrors the dispatch-time cascade-cancel of an edged dead dependency
    (``orchestrator._cascade_cancel_dependent`` sets CANCELLED): a needed-but-
    never-edged foundation that has definitively FAILED/CANCELLED — or a need that
    has outlived its bounded parked re-evaluations without ever becoming
    satisfiable — makes this work impossible. Raises ONE deduplicated operator
    alert (so the auto-cancel is visible) and clears the blocker metadata.

    Stamps a durable ``UNRESUMABLE_CANCEL_CONTEXT_KEY`` marker recording the
    ``give_up_class`` (foundation-died vs never-satisfiable), the unresolved
    ``need``, and the originating direction, so this terminal give-up is queryable
    and countable fleet-wide (the structured blocker metadata is otherwise cleared
    by ``_clear_blocked_fields`` on the same transition). Observability only.
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.alert_dispatch import _maybe_create_blocked_escalation_alert

    await _maybe_create_blocked_escalation_alert(
        task,
        session,
        blocker_kind=task.blocker_kind or "upstream dependency",
        detail=detail,
    )
    # Capture the originating direction before _clear_blocked_fields runs (it
    # preserves direction_id, but read defensively in case that changes).
    direction_id = (task.context or {}).get("direction_id")
    _clear_blocked_fields(task, preserve_first_blocked=False)
    ctx = task.context or {}
    ctx[UNRESUMABLE_CANCEL_CONTEXT_KEY] = {
        "give_up_class": give_up_class,
        "need": need if isinstance(need, dict) else None,
        "direction_id": direction_id,
        "at": _utcnow().isoformat(),
    }
    task.context = ctx
    task.status = TaskStatus.CANCELLED
    task.latest_status_message = message
    flag_modified(task, "context")
    session.add(task)
    pending_broadcasts.append(
        (
            str(task.project_id),
            {"task_id": str(task.id), "status": "CANCELLED", "message": message},
        )
    )


async def _apply_needs_disposition(
    task,
    ctx,
    session,
    now,
    disp,
    *,
    completed_cache: dict,
    pending_broadcasts: list,
    parked: bool,
) -> str:
    """Apply a multi-need classification (:func:`_classify_blocker_needs`).

    A dependent is resumable to QUEUED ONLY when EVERY recorded need is accounted
    for: an edge for each in-flight foundation, and no edge for a need already
    satisfied by completed/merged work (verified base-safe). When only some needs
    resolve, the resolvable edges are wired, the residual still-unsatisfied needs
    are re-persisted, and the task stays BLOCKED so the next tick re-evaluates only
    the remainder. If ANY need names a terminally dead foundation the dependent is
    cancelled; the parked re-evaluation cap applies per the parked path.

    Returns one of:
      ``"resumed"``   — every need accounted for; task → QUEUED (caller counts it)
      ``"cancelled"`` — a dead foundation, or the parked re-eval cap was exceeded
      ``"held"``      — needs remain (partial / unsatisfiable / re-cut conflict);
                        task stays BLOCKED, residual re-persisted, recheck re-armed
      ``"escalate"``  — non-parked, residual needs remain, an edge is already wired
                        and the escalation deadline has passed; caller escalates
      ``"none"``      — non-parked, residual remains, no edge yet: caller falls
                        through to the legacy precondition state machine
    """
    from sqlalchemy.orm.attributes import flag_modified
    from app.alert_dispatch import _resolve_blocked_escalation_alert

    # 1) ANY need names a terminally-dead foundation → cancel (both paths). A
    #    single provably-dead foundation makes the dependent impossible regardless
    #    of how the other needs resolve, mirroring the dispatch-time cascade.
    if disp["dead"]:
        await _cancel_blocker_as_dead(
            task,
            session,
            detail=(
                "A needed foundation has terminally failed or was cancelled — this "
                "task can never run as-is. Auto-cancelled (re-scope, re-point the "
                "need, or recreate the foundation to retry)."
            ),
            message="Cancelled — needed foundation terminally failed/cancelled",
            pending_broadcasts=pending_broadcasts,
            give_up_class=GIVE_UP_FOUNDATION_DIED,
            need=disp.get("dead_need"),
        )
        return "cancelled"

    residual = list(disp["residual"])
    edges = disp["edges"]
    completed_needs = disp["completed_needs"]

    # 2) Verify each completed foundation is base-safe BEFORE treating it as
    #    accounted-for — on BOTH the partial- and full-resolution paths. A need
    #    satisfied by ALREADY-completed work carries no edge, so the dispatch-time
    #    re-cut (which only inspects edged candidates) can never fire for it; a
    #    cross-base GENUINE code foundation must have the dependent's base re-cut
    #    from integration first. _completed_foundation_base_safe performs that
    #    re-cut as a side effect (idempotent; once per need this tick) and returns
    #    False when a re-cut is required but conflicts / hits a transient error.
    #    A need whose re-cut is not yet safe is held back in ``recut_pending`` so
    #    it is re-checked next tick rather than dropped unverified — closing the
    #    partial-resolution gap where a cross-base completed need silently vanished
    #    and the dependent later resumed against a stale base. Such a need DID
    #    match a foundation (it is mid-recut, not genuinely unprovided), so it must
    #    always HOLD and must NOT fall through to the legacy precondition machine.
    recut_pending: list = []
    if completed_needs:
        completed = completed_cache.get(task.project_id)
        if completed is None:
            completed = await _fetch_completed_candidates(session, task.project_id)
            completed_cache[task.project_id] = completed
        base_safe: list = []
        for need in completed_needs:
            if await _completed_foundation_base_safe(task, need, completed, session):
                base_safe.append(need)
            else:
                recut_pending.append(need)
        completed_needs = base_safe
        # base-safe REPLACES and COMMITS task.context with a fresh dict carrying
        # the _recut_* escalation accounting on its False path; re-read the local
        # ctx so the disposition branches below don't clobber that persisted state.
        ctx = task.context or {}

    progressed = bool(edges) or bool(completed_needs)
    # A still-unsatisfied need is either genuinely unprovided (``residual``) or a
    # completed foundation mid-recut (``recut_pending``). Both are re-persisted as
    # needs so the next tick re-evaluates them; only the all-accounted-for path
    # (both empty) leaves BLOCKED.
    unresolved = residual + recut_pending

    # 3) Unresolved needs remain → cannot fully resume this tick.
    if unresolved:
        if progressed or recut_pending:
            # Partial resolution (or a need mid-recut): wire the resolvable edges,
            # drop the resolved (and base-safe-verified completed) needs, keep ONLY
            # the unresolved set, and stay BLOCKED so the next tick re-evaluates the
            # remainder. A recut_pending need ALWAYS holds here — it matched a
            # foundation, so it must never fall through to the precondition machine.
            # Progress resets the parked re-eval cap.
            edged = _wire_edges(task, edges)
            _persist_residual_needs(task, unresolved)
            ctx = task.context or {}
            if progressed:
                ctx.pop("_blocked_parked_reevals", None)
            attempts = int(ctx.get("_blocked_recheck_attempts", 0)) + 1
            ctx["_blocked_recheck_attempts"] = attempts
            task.context = ctx
            flag_modified(task, "context")
            if edged:
                flag_modified(task, "depends_on")
            task.recheck_after = now + (
                _BLOCKED_PARKED_REEVAL_INTERVAL
                if parked
                else _next_recheck_delay(attempts)
            )
            session.add(task)
            return "held"

        # No need resolved this round (and none mid-recut).
        if parked:
            # Apply the per-need parked re-evaluation cap: cancel rather than
            # re-arm a never-satisfiable parked blocker forever.
            reevals = int(ctx.get("_blocked_parked_reevals", 0)) + 1
            if reevals >= _BLOCKED_PARKED_REEVAL_CAP:
                await _cancel_blocker_as_dead(
                    task,
                    session,
                    detail=(
                        f"Blocked need was never satisfiable after {reevals} parked "
                        "re-evaluations — no task ever provided it. Auto-cancelled "
                        "(re-scope or re-point the need to retry)."
                    ),
                    message="Cancelled — blocked need never became satisfiable",
                    pending_broadcasts=pending_broadcasts,
                    give_up_class=GIVE_UP_NEVER_SATISFIABLE,
                    need=residual[0] if residual else None,
                )
                return "cancelled"
            ctx["_blocked_parked_reevals"] = reevals
            task.context = ctx
            flag_modified(task, "context")
            task.recheck_after = now + _BLOCKED_PARKED_REEVAL_INTERVAL
            session.add(task)
            return "held"

        # Non-parked, no progress. With NO edge wired the task can safely fall
        # through to the legacy precondition machine (optimistic retry / waiting /
        # deadline escalation) exactly as a single unresolved need did before. But
        # if an edge is ALREADY wired (a prerequisite resolved on an earlier tick)
        # the task must NOT be resumed by that edge clearing while other needs are
        # still missing — keep it BLOCKED, escalating only at the deadline.
        if not (task.depends_on or []):
            return "none"
        if (
            task.blocked_at is not None
            and (now - task.blocked_at) >= _BLOCKED_ESCALATE_AFTER
        ):
            return "escalate"
        attempts = int(ctx.get("_blocked_recheck_attempts", 0)) + 1
        ctx["_blocked_recheck_attempts"] = attempts
        task.context = ctx
        flag_modified(task, "context")
        task.recheck_after = now + _next_recheck_delay(attempts)
        session.add(task)
        return "held"

    # 4) Nothing unresolved → every need is accounted for AND every completed
    #    foundation was already verified base-safe in step 2 (a base-unsafe
    #    completed need would have been captured in recut_pending above and held).
    #    Wire any in-flight edges and resume.
    edged = _wire_edges(task, edges)
    _clear_blocked_fields(task, preserve_first_blocked=False)
    task.status = TaskStatus.QUEUED
    if edged:
        message = "Unblocked — all needs resolved into dependency edges, requeued"
    elif completed_needs:
        message = "Unblocked — all needs satisfied by completed work, requeued"
    else:
        message = "Unblocked — all needs resolved, requeued"
    task.latest_status_message = message
    flag_modified(task, "context")
    if edged:
        flag_modified(task, "depends_on")
    session.add(task)
    pending_broadcasts.append(
        (
            str(task.project_id),
            {"task_id": str(task.id), "status": "QUEUED", "message": message},
        )
    )
    # Reconcile the now-stale 'permanently blocked' operator alert when resuming a
    # parked blocker. Best-effort: a reconcile failure must never abort the resume.
    if parked:
        with contextlib.suppress(Exception):
            await _resolve_blocked_escalation_alert(task, session)
    return "resumed"


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
    # Active-candidate lists are fetched at most once per project across the batch
    # (only when a task actually has a resolvable need with no edge). The
    # completed-candidate cache is the parked-resume counterpart: a parked
    # blocker's need may now be satisfied by already-merged/shipped work.
    candidate_cache: dict = {}
    completed_cache: dict = {}
    # Terminally-dead foundations (FAILED/CANCELLED): a needed-but-never-edged
    # foundation that has definitively died makes its dependent impossible — it is
    # cancelled rather than parked to re-arm forever.
    dead_cache: dict = {}

    async def _escalate_blocked_task(
        task,
        ctx,
        *,
        blocker_kind: str,
        detail: str,
    ) -> None:
        from app.task_blocked import get_blocker_metadata, mark_blocker_escalated

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
        # A parked blocker that still carries a machine-resolvable need is parked
        # at the LOW re-evaluation cadence (so it comes back due and resumes when
        # its foundation lands), not the multi-year horizon. A need-less blocker
        # keeps the far-future park and stays for the operator exactly as today.
        has_need = isinstance(get_blocker_metadata(task).get("need"), dict)
        task.recheck_after = now + (
            _BLOCKED_PARKED_REEVAL_INTERVAL if has_need else _BLOCKED_ESCALATED_PARK
        )
        session.add(task)

    for task in due:
        ctx = task.context or {}
        # Multi-need classification (shared by the parked and live paths): a
        # blocked dependent can wait on MORE THAN ONE prerequisite. Classify EVERY
        # recorded need once against the live active / completed / dead foundation
        # sets, then apply the disposition — resume only when ALL needs resolve.
        disp = await _classify_blocker_needs(
            task,
            session,
            candidate_cache=candidate_cache,
            completed_cache=completed_cache,
            dead_cache=dead_cache,
        )

        # Already escalated terminally — leave parked for the operator. On
        # escalation we push recheck_after to a far-future sentinel so these rows
        # drop out of the due set entirely. Backstop: if a legacy/None
        # recheck_after let one resurface here, evict it the same way so it stops
        # being re-SELECTed and FOR UPDATE-locked every reaper cycle.
        if ctx.get("_blocked_escalated"):
            # Parked-blocker auto-resume / cancel / hold: a parked blocker that
            # carries still-satisfiable needs is resumed (edges wired + requeue, or
            # proceed when satisfied by completed work) and its stale alert is
            # reconciled; one whose needs are partially resolved keeps converging;
            # one whose needs are provably dead or have outlived the bounded parked
            # re-evaluations is terminated. Runs at the low parked cadence — never
            # re-escalates.
            if disp["had_needs"]:
                outcome = await _apply_needs_disposition(
                    task,
                    ctx,
                    session,
                    now,
                    disp,
                    completed_cache=completed_cache,
                    pending_broadcasts=pending_broadcasts,
                    parked=True,
                )
                if outcome == "resumed":
                    requeued += 1
                    changed = True
                    continue
                if outcome in ("cancelled", "held"):
                    changed = True
                    continue
            # Need-less parked blocker — re-park without re-alerting, keeping the
            # multi-year horizon so it stays for the operator exactly as today.
            if task.recheck_after is None or task.recheck_after <= now:
                task.recheck_after = now + _BLOCKED_ESCALATED_PARK
                session.add(task)
                changed = True
            continue

        # Cross-task RESOLUTION (runs BEFORE the optimistic-retry / escalate path):
        # wire a real depends_on edge for each need that names an in-flight
        # foundation, resume with no edge for any need already satisfied by
        # completed/merged work, and stay BLOCKED on the residual until EVERY need
        # is accounted for. A dead foundation cancels; a fully-resolved set
        # SHORT-CIRCUITS the bounded optimistic retry below. A task that still has
        # an unresolved need with no edge falls through ("none") to the legacy
        # precondition machine unchanged.
        if disp["had_needs"]:
            outcome = await _apply_needs_disposition(
                task,
                ctx,
                session,
                now,
                disp,
                completed_cache=completed_cache,
                pending_broadcasts=pending_broadcasts,
                parked=False,
            )
            if outcome == "resumed":
                requeued += 1
                changed = True
                continue
            if outcome in ("cancelled", "held"):
                changed = True
                continue
            if outcome == "escalate":
                await _escalate_blocked_task(
                    task,
                    ctx,
                    blocker_kind=task.blocker_kind or "upstream dependency",
                    detail=(
                        "Task has unresolved prerequisites that never appeared "
                        "within the deadline — needs operator intervention "
                        "(re-scope, re-point the need, or recreate the foundation)."
                    ),
                )
                changed = True
                continue
            # outcome == "none": fall through to the precondition state machine.

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
        else:  # "retry" or "waiting"
            past_deadline = (
                task.blocked_at is not None
                and (now - task.blocked_at) >= _BLOCKED_ESCALATE_AFTER
            )
            optimistic_used = int(ctx.get("_blocked_optimistic_retries", 0))
            # "retry": a no-dep agent/lifecycle blocker that may be a transient
            # infra failure — optimistically requeue (bounded) so a passing blip
            # auto-recovers instead of wedging its rollup parent until an operator
            # intervenes. Preserve the first-blocked onset so the 6h deadline
            # still measures from true onset across the requeue episodes.
            if (
                state == "retry"
                and not past_deadline
                and optimistic_used < _BLOCKED_OPTIMISTIC_RETRY_CAP
            ):
                ctx["_blocked_optimistic_retries"] = optimistic_used + 1
                task.context = ctx
                # _clear_blocked_fields preserves _blocked_optimistic_retries on
                # an onset-preserving requeue (it is only wiped on a genuine
                # dependency clear), so the budget keeps counting across re-blocks.
                _clear_blocked_fields(task, preserve_first_blocked=True)
                task.status = TaskStatus.QUEUED
                task.latest_status_message = (
                    f"Unblocked — optimistic infra-precondition retry "
                    f"{optimistic_used + 1}/{_BLOCKED_OPTIMISTIC_RETRY_CAP}"
                )
                flag_modified(task, "context")
                session.add(task)
                requeued += 1
                changed = True
                pending_broadcasts.append(
                    (
                        str(task.project_id),
                        {
                            "task_id": str(task.id),
                            "status": "QUEUED",
                            "message": task.latest_status_message,
                        },
                    )
                )
                continue
            # "waiting" (or a retry that exhausted its budget): re-arm a later
            # recheck with backoff. Deadline escalation stays active for blockers
            # still genuinely unresolved at the 6h mark.
            if past_deadline:
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


# ── Step 13: staged-direction auto-activation backstop ─────────────────────
#
# A "staged" direction (status="staged", staged_for_version=N) is inert work
# marked to begin only after the version that was OPEN when it was staged
# promotes.  The promotion success path activates eligible staged directions at
# the version-open transition, but a single failure there must never strand
# staged work forever.  This periodic sweep mirrors the proven BLOCKED-task
# pattern (apply at the event AND self-heal via a periodic sweep): for every
# project that still has a staged direction, it re-checks whether the target
# version has already promoted and activates via the SAME shared idempotent
# helper.  Idempotent — re-running on an already-active direction is a no-op.
async def sweep_activate_staged_directions(session) -> int:
    """Step 13: activate staged directions whose target version already opened.

    For every project with at least one ``staged`` direction, reads the current
    open version and activates any staged direction whose ``staged_for_version``
    is at or below the most-recently-promoted version (``current_open_version -
    1``) via :func:`directions_repo.activate_eligible_staged_directions`.

    Best-effort per project (a failure on one project never aborts the sweep).
    Returns the total number of directions activated across all projects.
    """
    from app.directions_repo import activate_eligible_staged_directions
    from app.models import Direction
    from app.release_version import current_open_version

    # Prefilter candidate projects via the normalised direction table (dual-
    # written under the same project lock as the authoritative blob).  The
    # helper re-reads the blob under the lock, so a momentarily-stale table only
    # affects which projects are visited, not correctness.
    project_ids = (
        (
            await session.execute(
                select(Direction.project_id)
                .where(Direction.status == "staged")
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if not project_ids:
        return 0

    total_activated = 0
    for pid in project_ids:
        try:
            open_version = await current_open_version(session, pid)
            if open_version is None:
                continue
            # The most-recently-promoted version is one below the open version.
            # A staged_for_version equal to the open version stays staged (its
            # target version has not promoted yet).
            promoted_n = open_version.version_number - 1
            if promoted_n < 1:
                continue  # no version has promoted yet
            activated = await activate_eligible_staged_directions(
                session, pid, promoted_n
            )
            total_activated += len(activated)
        except Exception as e:
            logger.error(
                "[REAPER] Staged-direction activation sweep error for project "
                "%s: %s",
                pid,
                e,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()
    if total_activated:
        logger.info(
            "[REAPER] Step 13: activated %d staged direction(s) across %d "
            "candidate project(s)",
            total_activated,
            len(project_ids),
        )
    return total_activated


# ── Step 14: stale-staged-direction staleness alerting ─────────────────────
#
# Step 13 solves the HAPPY path: a staged direction auto-activates the moment
# its target version promotes.  The UNHAPPY path is that the target version can
# stay open far longer than the ~14-day build-to-stabilize stall gate (deferred
# by repeated qualification failures or operator inaction).  When that happens
# the staged direction waits indefinitely and invisibly — the "perpetually
# deferred" failure mode the staging spine set out to eliminate, merely
# relocated from the strategist's head into a forever-staged row.  This sweep
# makes that stall VISIBLE and ACTIONABLE: for every project that still has a
# staged direction whose target version has NOT promoted, it computes how long
# the work has waited (anchored on ``staged_at``, fail-safe to a config age when
# absent) and, past a configurable threshold, raises ONE deduplicated operator
# alert per direction.  Alerting only — it never changes activation semantics.
async def sweep_stale_staged_directions(session) -> int:
    """Step 14: alert on staged directions stalled past the staleness threshold.

    For every project with at least one ``staged`` direction, reads the current
    open version and, for each staged direction whose target version has NOT yet
    promoted (``staged_for_version >= current_open_version``), computes the days
    waited from ``staged_at`` (fail-safe to ``staged_direction_stale_fallback_days``
    when the timestamp is missing or unparseable). When that age is at or beyond
    ``staged_direction_stale_alert_days`` it raises a deduplicated operator alert
    via :func:`alert_dispatch._maybe_create_stale_staged_direction_alert`.

    Best-effort per project (a failure on one project never aborts the sweep).
    Returns the total number of NEW stale alerts created across all projects.
    """
    from app.alert_dispatch import _maybe_create_stale_staged_direction_alert
    from app.models import Direction
    from app.release_version import current_open_version
    from app.shared_state import runtime_config
    from app.time_utils import _coerce_naive_utc

    stale_days = int(runtime_config.get("staged_direction_stale_alert_days", 21))
    fallback_days = int(
        runtime_config.get("staged_direction_stale_fallback_days", 21)
    )

    # Same prefilter as the activation sweep: candidate projects via the
    # normalised direction table. We read the staged rows' ``data`` blobs here
    # (verbatim ``Project.directions`` shape) rather than the project blob.
    rows = (
        (
            await session.execute(
                select(Direction).where(Direction.status == "staged")
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    # Group staged rows by project so each project reads its open version once.
    by_project: dict = {}
    for r in rows:
        by_project.setdefault(r.project_id, []).append(r)

    now = _utcnow()
    total_alerts = 0
    for pid, staged_rows in by_project.items():
        try:
            open_version = await current_open_version(session, pid)
            open_n = open_version.version_number if open_version else None
            for row in staged_rows:
                d = row.data or {}
                target = d.get("staged_for_version")
                try:
                    target_n = int(target)
                except (TypeError, ValueError):
                    # Unparseable marker is fail-OPEN-activated by Step 13, so it
                    # is never genuinely stranded — don't alert on it here.
                    continue
                # Only directions whose target version has NOT promoted are
                # stalled. The activation condition is target < open_version
                # (i.e. target <= open-1); its complement is target >= open.
                if open_n is not None and target_n < open_n:
                    continue
                # Days waited, anchored on the staging timestamp. A missing or
                # unparseable timestamp is fail-safe: assume the fallback age so
                # malformed/legacy staged rows still surface rather than waiting
                # silently forever.
                staged_at_raw = d.get("staged_at")
                waited_days: int
                if staged_at_raw:
                    try:
                        staged_at = _coerce_naive_utc(
                            datetime.fromisoformat(str(staged_at_raw))
                        )
                        waited_days = max(0, (now - staged_at).days)
                    except (TypeError, ValueError):
                        waited_days = fallback_days
                else:
                    waited_days = fallback_days
                if waited_days < stale_days:
                    continue
                created = await _maybe_create_stale_staged_direction_alert(
                    session,
                    pid,
                    direction_id=str(d.get("id") or row.direction_key),
                    direction_text=d.get("text"),
                    waited_days=waited_days,
                    staged_for_version=target_n,
                    open_version=open_n,
                )
                if created:
                    total_alerts += 1
        except Exception as e:
            logger.error(
                "[REAPER] Stale-staged-direction sweep error for project %s: %s",
                pid,
                e,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()
    if total_alerts:
        logger.info(
            "[REAPER] Step 14: raised %d stale-staged-direction alert(s) across "
            "%d candidate project(s)",
            total_alerts,
            len(by_project),
        )
    return total_alerts


async def sweep_merge_gate_verification_degradation(session) -> int:
    """Alert fleet-wide when the no-CI merge-gate verification rate degrades.

    Computes the fleet's recent-window merge-gate health
    (:func:`digest_metrics.compute_merge_gate_fleet_health`) and, when the
    verification rate has collapsed and/or too many projects are degraded over a
    meaningful sample, broadcasts a deduplicated operator alert. A systemically
    broken gate merges 'done' PRs UNVERIFIED at scale while each project looks
    individually unremarkable; today an operator only catches that by chance on
    the dashboard. This raises the proactive alert every other fleet-level signal
    already has. Alerting only — it never influences a merge decision.

    Trigger (over the recent window, gated on a minimum sample so a single
    fall-open never fires): verification rate below ``merge_gate_degradation_min_rate``
    OR at least ``merge_gate_degradation_min_degraded_projects`` projects degraded.
    Severity escalates to ERROR when the dominant fall-open reason is a degraded
    environment/setup failure (reusing ``_severity_for_unverified_reason``), else
    WARNING. Returns the number of NEW alerts created across the fleet.
    """
    from app.alert_dispatch import (
        _maybe_create_merge_gate_degradation_alerts,
        _severity_for_unverified_reason,
    )
    from app.digest_metrics import compute_merge_gate_fleet_health
    from app.models import AlertSeverity
    from app.shared_state import runtime_config

    window_days = int(runtime_config.get("merge_gate_degradation_window_days", 7))
    min_samples = int(runtime_config.get("merge_gate_degradation_min_samples", 10))
    min_rate = float(runtime_config.get("merge_gate_degradation_min_rate", 0.5))
    min_degraded = int(
        runtime_config.get("merge_gate_degradation_min_degraded_projects", 3)
    )

    window_start = _utcnow() - timedelta(days=window_days)
    health = await compute_merge_gate_fleet_health(session, window_start=window_start)

    total = health["verified_count"] + health["fell_open_count"]
    if total < min_samples:
        return 0
    rate = health["verification_rate"]
    degraded = health["degraded_project_count"]
    rate_is_low = rate is not None and rate < min_rate
    too_many_degraded = degraded >= min_degraded
    if not (rate_is_low or too_many_degraded):
        return 0

    reason = health["most_common_reason"]
    severity = (
        _severity_for_unverified_reason(reason) if reason else AlertSeverity.WARNING
    )
    created = await _maybe_create_merge_gate_degradation_alerts(
        session,
        verification_rate=rate,
        verified_count=health["verified_count"],
        fell_open_count=health["fell_open_count"],
        degraded_project_count=degraded,
        project_count=health["project_count"],
        most_common_reason=reason,
        severity=severity,
    )
    if created:
        await session.commit()
        logger.info(
            "[REAPER] raised fleet merge-gate degradation alert on %d project(s) "
            "(rate=%s, degraded=%d/%d, reason=%s)",
            created,
            rate,
            degraded,
            health["project_count"],
            reason,
        )
    return created


# ── Step 15: linked bug-class message convergence / resurfacing sweep ────────
#
# A BUG_REPORT or BLOCKER message placed in 'linked' state has a durable forward
# link recorded in its JSONB context (see message_convergence.py). This sweep
# closes the loop: it reads the owning outcome's current lifecycle state and
# either terminally resolves the message (CONVERGENCE) or reopens it (RESURFACING).
#
# CONVERGENCE fires when the owning outcome actually *lands*:
#   - Direction: status == 'completed'
#   - Task: is_merged_completion(task) — COMPLETED AND carries the landed
#     signal (app.landed_signal). COMPLETED-but-not-landed is treated as
#     still in-flight, never converged.
#
# RESURFACING fires when the owning outcome ended without landing (failed,
# cancelled, retired, rejected, expired, superseded), went missing, or has been
# in-flight longer than _CONVERGENCE_RESURFACE_AFTER without delivering.
#
# Operator-resolved messages (status == 'resolved', not 'linked') are never
# touched — only 'linked' messages participate.  The sweep is idempotent: both
# the convergence and resurfacing transitions change the message status away from
# 'linked', so re-running on the same message is a no-op.

# Default time bound: a linked message whose owning outcome has not yet landed
# after this many days is resurfaced so the strategist can reassign the fix.
_CONVERGENCE_RESURFACE_AFTER = timedelta(days=7)

# Direction statuses that indicate the direction ended without landing the fix.
_DIRECTION_FAILED_STATUSES: frozenset[str] = frozenset(
    {"retired", "rejected", "expired", "superseded"}
)


def _converge_message(msg, now: datetime, rationale: str) -> None:
    """Stamp msg as resolved — the owning outcome landed.

    Mutates msg.status, msg.resolution, msg.resolved_at, and msg.context in
    place.  Does NOT call session.add() or commit.
    """
    msg.status = "resolved"
    msg.resolution = f"Owned outcome landed: {rationale}"
    msg.resolved_at = now
    ctx = dict(msg.context or {})
    ctx["_convergence_converged_at"] = now.isoformat()
    msg.context = ctx


def _resurface_message(msg, now: datetime, reason: str) -> None:
    """Reopen a linked bug-class message — owning outcome failed/missing/stale.

    Moves the stale forward link to ``_convergence_link_stale`` (audit trail),
    records why it came back, and sets status back to 'open'.
    Does NOT call session.add() or commit.
    """
    msg.status = "open"
    ctx = dict(msg.context or {})
    old_link = ctx.pop("_convergence_link", None)
    if old_link:
        ctx["_convergence_link_stale"] = old_link
    ctx["_convergence_resurfaced_at"] = now.isoformat()
    ctx["_convergence_resurfaced_reason"] = reason
    msg.context = ctx


async def sweep_converge_linked_bug_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
) -> int:
    """Step 15: reconcile linked BUG_REPORT/BLOCKER messages with owned outcomes.

    For each 'linked' bug-class message:
    - CONVERGE to 'resolved' when the owning outcome lands.
    - RESURFACE to 'open' when the owning outcome fails, is cancelled/retired/
      rejected/expired/superseded, goes missing, or has been in-flight for longer
      than _CONVERGENCE_RESURFACE_AFTER without delivering.
    - Leave untouched when the owning outcome is still legitimately in-flight
      within the time bound.

    Returns the number of messages acted on (converged + resurfaced).
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import Direction, ProjectMessage
    from app.message_convergence import BUG_CLASS_TYPES, LINKED_STATUS, get_message_link
    from app.landed_signal import is_merged_completion
    from app.time_utils import _coerce_naive_utc
    from app.shared_state import runtime_config

    resurface_days = int(
        runtime_config.get(
            "convergence_resurface_after_days", _CONVERGENCE_RESURFACE_AFTER.days
        )
    )
    resurface_after = timedelta(days=resurface_days)

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == LINKED_STATUS,
                    ProjectMessage.message_type.in_(list(BUG_CLASS_TYPES)),
                )
                .order_by(ProjectMessage.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) >= batch_size:
        logger.warning(
            "[REAPER] convergence sweep hit cap=%d linked bug-class messages — "
            "result truncated; next cycle continues",
            batch_size,
        )

    if not msgs:
        return 0

    acted = 0
    pending_broadcasts: list[tuple[str, dict]] = []

    for msg in msgs:
        link = get_message_link(msg)
        if not link:
            continue

        kind = link.get("kind")
        outcome_id = link.get("id")
        linked_at_raw = link.get("linked_at")

        if not kind or not outcome_id:
            continue

        linked_at: datetime | None = None
        if linked_at_raw:
            try:
                linked_at = _coerce_naive_utc(
                    datetime.fromisoformat(str(linked_at_raw))
                )
            except (ValueError, TypeError):
                linked_at = None

        if kind == "direction":
            dir_row = (
                (
                    await session.execute(
                        select(Direction).where(
                            Direction.project_id == msg.project_id,
                            Direction.direction_key == outcome_id,
                        )
                    )
                )
                .scalars()
                .first()
            )

            if dir_row is None:
                reason = f"Owning direction '{outcome_id}' no longer exists"
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — direction '%s' missing",
                    msg.id,
                    outcome_id,
                )
                continue

            dir_status = dir_row.status or "active"

            if dir_status == "completed":
                rationale = f"Direction '{outcome_id}' reached completed status"
                _converge_message(msg, now, rationale)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: converged message %s — direction '%s' completed",
                    msg.id,
                    outcome_id,
                )
            elif dir_status in _DIRECTION_FAILED_STATUSES:
                reason = (
                    f"Owning direction '{outcome_id}' ended with status "
                    f"'{dir_status}' without landing"
                )
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — direction '%s' ended '%s'",
                    msg.id,
                    outcome_id,
                    dir_status,
                )
            elif linked_at is not None and (now - linked_at) >= resurface_after:
                reason = (
                    f"Owning direction '{outcome_id}' still in status "
                    f"'{dir_status}' after {(now - linked_at).days} days "
                    f"without delivering the fix"
                )
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — direction '%s' "
                    "still in-flight after %d days",
                    msg.id,
                    outcome_id,
                    (now - linked_at).days,
                )
            # else: in-flight within time bound — leave linked

        elif kind == "task":
            try:
                task_uuid = uuid.UUID(str(outcome_id))
            except (ValueError, AttributeError):
                reason = f"Owning task id '{outcome_id}' is not a valid UUID"
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — task id '%s' "
                    "is not a valid UUID",
                    msg.id,
                    outcome_id,
                )
                continue

            task_row = await session.get(Task, task_uuid)

            if task_row is None:
                reason = f"Owning task '{outcome_id}' no longer exists"
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — task '%s' missing",
                    msg.id,
                    outcome_id,
                )
                continue

            task_status = task_row.status

            # CONVERGENCE requires a genuine *landing*, not merely COMPLETED.
            # COMPLETED != merged is a deliberate invariant in this codebase:
            # a fix task can reach COMPLETED with an open, unmerged PR (whose
            # URL canonically lives in the Artifact table, not task.context), so
            # deriving "landed" from a context pr_url heuristic would silently
            # close a live bug report. Reuse the canonical landed detector.
            if is_merged_completion(task_row):
                rationale = f"Owning task '{outcome_id}' completed and PR merged"
                _converge_message(msg, now, rationale)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: converged message %s — task '%s' landed",
                    msg.id,
                    outcome_id,
                )

            elif task_status == TaskStatus.COMPLETED:
                # COMPLETED but not landed (PR open / unmerged) — treat as still
                # in-flight, subject to the time-bound resurfacing below.
                if linked_at is not None and (now - linked_at) >= resurface_after:
                    reason = (
                        f"Owning task '{outcome_id}' completed but did not land "
                        f"(PR not merged) after {(now - linked_at).days} days"
                    )
                    _resurface_message(msg, now, reason)
                    session.add(msg)
                    pending_broadcasts.append(
                        (str(msg.project_id), {"message_id": str(msg.id)})
                    )
                    acted += 1
                    logger.info(
                        "[REAPER] Step 15: resurfaced message %s — task '%s' "
                        "completed but did not land after %d days",
                        msg.id,
                        outcome_id,
                        (now - linked_at).days,
                    )
                # else: COMPLETED but not landed yet, within time bound — leave linked

            elif task_status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                status_str = getattr(task_status, "value", str(task_status))
                reason = (
                    f"Owning task '{outcome_id}' ended with status "
                    f"'{status_str}' without landing"
                )
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — task '%s' %s",
                    msg.id,
                    outcome_id,
                    status_str,
                )
            elif linked_at is not None and (now - linked_at) >= resurface_after:
                status_str = getattr(task_status, "value", str(task_status))
                reason = (
                    f"Owning task '{outcome_id}' still in status "
                    f"'{status_str}' after {(now - linked_at).days} days"
                )
                _resurface_message(msg, now, reason)
                session.add(msg)
                pending_broadcasts.append(
                    (str(msg.project_id), {"message_id": str(msg.id)})
                )
                acted += 1
                logger.info(
                    "[REAPER] Step 15: resurfaced message %s — task '%s' "
                    "still in-flight after %d days",
                    msg.id,
                    outcome_id,
                    (now - linked_at).days,
                )
            # else: in-flight within time bound — leave linked

    if acted:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] convergence broadcast failed for message %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] Step 15: acted on %d linked bug-class message(s) "
            "(converged or resurfaced)",
            acted,
        )

    return acted


# ── Step 17: auto-resolve OPEN owned bug-class messages on owner success ──────
#
# A BUG_REPORT/BLOCKER created in the 'open' state carries machine-readable
# ownership context stamped at CREATION time (NOT the strategist's
# _convergence_link, which the Step-15 linked sweep owns):
#   - Direction-owned: context['direction_id']  (pause/escalation posters in
#     notification_dispatch.py; the dedup-deadlock BUG_REPORT in
#     research_direction_state.py).
#   - PR/task-owned:    context['pr_number'] + context['source_task_id']
#     (the repeat-fingerprint halt BLOCKER in review_pipeline.py).
#
# Nothing on the integration branch ever resolves these open owned messages:
# the routine-message retention sweep deliberately skips BUG_REPORT/BLOCKER, and
# the Step-15 linked sweep only acts on messages the strategist has explicitly
# moved to the 'linked' state. So once the owner genuinely LANDS — the owning
# direction reaches 'completed', or the owning PR's source task is a merged
# completion — the open message lingers forever in the strategist's inbox as
# noise. This sweep closes only that loop, and ONLY on genuine SUCCESS.
#
# SUCCESS-ONLY, by construction: a message is resolved ONLY when the owning
# direction's status == 'completed' OR the owning PR's source task satisfies the
# canonical landed detector ``landed_signal.is_merged_completion`` (COMPLETED AND
# carries the integration-branch landed signal). An owner that ended WITHOUT
# delivering the fix — a retired/rejected/expired/superseded direction; a
# failed/cancelled/missing task; a COMPLETED-but-unmerged task; an unknown,
# malformed, or still-in-flight owner — leaves the message OPEN. Reaching some
# terminal status is NOT enough; only landing resolves.
#
# Fail-safe: per-message owner resolution is wrapped so one bad row can never
# abort the batch, and any error or ambiguity leaves the message open.
#
# Idempotent: resolution moves status off 'open', so the next pass (which filters
# on status == 'open') skips already-resolved rows — re-running is a no-op.

# Context audit keys stamped on an auto-resolution (traceability + idempotency).
_OWNED_AUTO_RESOLVED_AT_KEY = "_owned_auto_resolved_at"
_OWNED_AUTO_RESOLUTION_KEY = "_owned_auto_resolution"


async def _owned_message_resolution(session, msg, *, verify_pr_merged) -> str | None:
    """High-confidence audit string when ``msg``'s owner genuinely LANDED, else None.

    Reads ownership context stamped at message creation (``direction_id`` first,
    then ``pr_number`` + ``source_task_id``). Direction ownership wins when both
    are present. Returns ``None`` — fail-safe, leaving the message open — for any
    missing/unknown/malformed/still-in-flight owner or any owner that ended
    without landing the fix.
    """
    from app.landed_signal import is_merged_completion
    from app.models import Direction

    ctx = msg.context if isinstance(msg.context, dict) else {}

    # 1) Direction-owned: resolve ONLY on status == 'completed'. A direction that
    #    was retired/rejected/expired/superseded ended WITHOUT landing the fix and
    #    must leave the message open.
    direction_id = ctx.get("direction_id")
    if direction_id:
        dir_row = (
            (
                await session.execute(
                    select(Direction).where(
                        Direction.project_id == msg.project_id,
                        Direction.direction_key == str(direction_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        if dir_row is not None and (dir_row.status or "active") == "completed":
            return f"Auto-resolved: owning direction '{direction_id}' completed"
        # Direction owner present but not completed (or missing) — fail-safe open.
        # Fall through only when there is NO direction owner at all.
        if dir_row is not None:
            return None

    # 2) PR/task-owned: resolve ONLY when the source task is a merged completion.
    #    Never infer 'merged' from a pr_url heuristic; never treat a
    #    COMPLETED-but-unmerged task as success.
    pr_number = ctx.get("pr_number")
    source_task_id = ctx.get("source_task_id")
    if pr_number is None or not source_task_id:
        return None
    try:
        task_uuid = uuid.UUID(str(source_task_id))
    except (ValueError, AttributeError, TypeError):
        return None
    task = await session.get(Task, task_uuid)
    if task is None:
        return None
    # Primary detector: context-based, side-effect-free, identical to the Step-15
    # linked sweep ("COMPLETED AND carries the landed signal"). A merged PR's
    # source task is stamped landed at merge time.
    if is_merged_completion(task):
        return f"Auto-resolved: owning PR #{pr_number} merged"
    # Secondary confirmation via the GitHub-backed verifier ONLY for a task that
    # is already COMPLETED (so a still-active task is never probed and a
    # COMPLETED-but-unmerged task is left open unless the verifier confirms the
    # merge). Any verifier error is fail-safe (leaves the message open).
    if task.status == TaskStatus.COMPLETED and verify_pr_merged is not None:
        try:
            if await verify_pr_merged(task, session):
                return f"Auto-resolved: owning PR #{pr_number} merged"
        except Exception:
            return None
    return None


async def sweep_resolve_open_owned_bug_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
    verify_pr_merged=None,
) -> int:
    """Step 17: auto-resolve OPEN owned BUG_REPORT/BLOCKER messages on owner success.

    Selects up to ``batch_size`` OPEN, system/agent-authored bug-class messages
    (DISJOINT from the Step-15 'linked' sweep) and resolves each one whose owning
    outcome — read from creation-time context — has genuinely landed: the owning
    direction reached 'completed', or the owning PR's source task is a merged
    completion. Everything else (owner ended without landing, COMPLETED-but-
    unmerged, missing/unknown/malformed/still-active, or any lookup error) leaves
    the message OPEN.

    Returns the number of messages auto-resolved.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import ProjectMessage
    from app.message_convergence import BUG_CLASS_TYPES, OPEN_STATUS, SYSTEM_AUTHORED_FROM_TYPES

    if verify_pr_merged is None:
        from app.rollup_pr_verification import _verify_pr_merged
        verify_pr_merged = _verify_pr_merged

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.from_type.in_(list(SYSTEM_AUTHORED_FROM_TYPES)),
                    ProjectMessage.message_type.in_(list(BUG_CLASS_TYPES)),
                )
                .order_by(ProjectMessage.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if len(msgs) >= batch_size:
        logger.warning(
            "[REAPER] open-owned-message sweep hit cap=%d open bug-class messages — "
            "result truncated; next cycle continues",
            batch_size,
        )

    if not msgs:
        return 0

    resolved = 0
    pending_broadcasts: list[tuple[str, dict]] = []
    for msg in msgs:
        # Fail-safe: one malformed/unreadable owner must never abort the batch.
        try:
            audit = await _owned_message_resolution(
                session, msg, verify_pr_merged=verify_pr_merged
            )
        except Exception:
            logger.warning(
                "[REAPER] Step 17: owner resolution failed for message %s — "
                "leaving open",
                msg.id,
                exc_info=True,
            )
            continue
        if audit is None:
            continue
        msg.status = "resolved"
        msg.resolution = audit
        msg.resolved_at = now
        ctx = dict(msg.context or {})
        ctx[_OWNED_AUTO_RESOLVED_AT_KEY] = now.isoformat()
        ctx[_OWNED_AUTO_RESOLUTION_KEY] = audit
        msg.context = ctx
        session.add(msg)
        resolved += 1
        pending_broadcasts.append(
            (str(msg.project_id), {"message_id": str(msg.id)})
        )
        logger.info(
            "[REAPER] Step 17: auto-resolved owned message %s — %s",
            msg.id,
            audit,
        )

    if resolved:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] Step 17: broadcast failed for message %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] Step 17: auto-resolved %d open owned bug-class message(s)",
            resolved,
        )

    return resolved
