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
from typing import Any

from sqlalchemy import and_, or_
from sqlmodel import select
from app.task_capabilities import BACKEND_MANAGED_TASK_TYPES

from app.blocked_task_unblock_sweeps import (
    _BLOCKED_ESCALATE_AFTER,
    _BLOCKED_ESCALATED_PARK,
    _BLOCKED_OPTIMISTIC_RETRY_CAP,
    _BLOCKED_PARKED_REEVAL_CAP,
    _BLOCKED_PARKED_REEVAL_INTERVAL,
    _TRANSIENT_BLOCKER_PARK_SENTINEL,
    GIVE_UP_FOUNDATION_DIED,
    GIVE_UP_NEVER_SATISFIABLE,
    MISSING_FOUNDATION_MARKER_KEY,
    RESUME_MARKER_CONTEXT_KEY,
    UNRESUMABLE_CANCEL_CONTEXT_KEY,
    _apply_needs_disposition,
    _blocked_precondition_state,
    _cancel_blocker_as_dead,
    _classify_blocker_needs,
    _clear_blocked_fields,
    _fetch_active_candidates,
    _fetch_completed_candidates,
    _fetch_dead_candidates,
    _fetch_knowledge_substance_index,
    _fetch_working_capability_names,
    _next_recheck_delay,
    _partition_completed_needs_base_safe,
    _persist_residual_needs,
    _wire_edges,
    sweep_unblock_blocked_tasks,
    _event_driven_unblock_project,
    event_driven_unblock_dependents,
    event_driven_unblock_on_knowledge,
    event_driven_unblock_on_capability_proof,
)
from app.compute_backends import (
    GCP_BACKEND_NAME,
    RUNNER_SUBPROCESS_BACKEND_NAME,
    resolve_project_default_container_image,
)
from app.compute_jobs import (
    RUNNER_SILENT_TIMEOUT_SECONDS,
    finalize_silent_compute_job,
    finalize_runner_subprocess_job_for_dead_runner,
)
from app.database import AsyncSessionLocal
from app.models import (
    ArtifactRefPromotionIntent,
    ComputeImageBuildStatus,
    ComputeJob,
    ComputeJobFile,
    ComputeJobStatus,
    LocalRunner,
    ProjectComputeImageBuild,
    ProjectMessageAttachmentUpload,
    Task,
    TaskStatus,
)
from app.storage import (
    VIEWABLE_ARTIFACT_RETENTION_DAYS,
    delete_storage_uri_best_effort,
)
from app.runner_health_monitors import _RUNNER_LIVENESS_SECONDS
from app.time_utils import _is_cadence_due, _utcnow
from app.reaper_fleet_health_sweeps import (
    sweep_merge_gate_verification_degradation,
    sweep_migration_slot_contention,
)

__all__ = [
    "sweep_merge_gate_verification_degradation",
    "sweep_migration_slot_contention",
]

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

# Cadence gate for fleet-wide scans that are expensive but need not run every
# reaper tick (60s).  900s = 15 min matches the regression-scan interval.
_FLEET_SCAN_INTERVAL_SECONDS = 15 * 60
_last_converge_duplicate_scan: datetime | None = None
_last_merge_gate_degradation_scan: datetime | None = None


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
            asyncio.ensure_future(
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


async def sweep_stale_migration_reservations(session) -> int:
    """Expire ACTIVE migration reservations whose owning task is gone or terminal-failed."""
    from app.models import MigrationReservation, MigrationReservationStatus

    rows = (
        await session.execute(
            select(MigrationReservation, Task.status)
            .outerjoin(Task, Task.id == MigrationReservation.task_id)
            .where(
                MigrationReservation.status == MigrationReservationStatus.ACTIVE,
            )
        )
    ).all()
    expired = 0
    now = _utcnow()
    for reservation, task_status in rows:
        if reservation.task_id is None or task_status in (
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        ):
            reservation.status = MigrationReservationStatus.EXPIRED
            reservation.updated_at = now
            session.add(reservation)
            expired += 1
    if expired:
        await session.commit()
        logger.info("[REAPER] Expired %d stale migration reservation(s)", expired)
    return expired


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
    from app import pause_clock
    from app.models import Alert, AlertSeverity, DeploymentState
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
                    or_(
                        Task.payloads[0]["type"].as_string().is_(None),
                        Task.payloads[0]["type"].as_string().not_in(
                            sorted(BACKEND_MANAGED_TASK_TYPES)
                        ),
                    ),
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

    # PAUSE-AWARE tier age: while the pipeline is PAUSED nothing dequeues, so
    # every QUEUED row ages past the tiers on wall-clock and spams INFO→WARNING→
    # ERROR alerts for a pause the operator deliberately started. Subtract the
    # open pause window from the tier decision (the row's created_at is immutable
    # so no baseline is stamped; deploy_state=None → raw). The DB pre-filter above
    # still over-selects on raw age, but a paused row's pause-aware tier is 0, so
    # the tier gate below suppresses the alert.
    _deploy_state = await session.get(DeploymentState, 1)
    _alerted = 0
    pending_broadcasts: list[tuple[str, dict]] = []
    for _qt in _stale_queued:
        _qctx = _qt.context or {}
        _prev_tier = _qctx.get("queued_age_alert_tier") or (
            1 if _qctx.get("queued_age_alert_fired") else 0
        )
        _queue_hours = (
            pause_clock.active_elapsed_seconds(_deploy_state, _qt.created_at, None)
            / 3600
        )
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
        elif (_qt.latest_status_message or "").startswith(_PRECONDITION_DEFER_PREFIX):
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
                _likely_cause = (
                    f"type failure gate active ({_qt_type}: {_rate:.0%} failure rate)"
                )
            elif _shared_state.tasks_active_gauge >= runtime_config.get(
                "max_concurrent_vms", 8
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
            except Exception as _bcast_err:
                logger.warning(
                    f"[REAPER] Queued age alert broadcast failed for "
                    f"{payload.get('alert_id')}: {_bcast_err}"
                )
        logger.info(f"[REAPER] Fired queued age alerts for {_alerted} stale task(s)")

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
    from app.models import (
        Alert,
        AlertSeverity,
        Project,
        StrategicCycle,
        StrategicResident,
    )
    from app.broadcast import broadcast_to_project as _bcast
    from app.strategic_runtime_work import task_is_strategic_local_runner_hold

    # Pause-aware: a cycle whose completion handler simply could not run while
    # the pipeline was paused must not be force-terminated as "stalled". Passing
    # the deploy state makes find_stalled's 3h deadline exclude pause time.
    from app.models import DeploymentState  # noqa: PLC0415

    _deploy_state = await session.get(DeploymentState, 1)
    stalled = await strategic_cycle.find_stalled(session, deploy_state=_deploy_state)
    _step7_now = _utcnow()
    if not stalled:
        return 0
    actually_stalled: list = []
    pending_broadcasts: list[tuple[str, dict]] = []

    # Fence the legacy reaper against ownership cutover without locking the
    # broad discovery scan. These bounded statements follow the canonical
    # Project -> Resident -> Cycle -> Task order used by cutover/completion.
    _project_ids = sorted({c.project_id for c in stalled}, key=str)
    _locked_projects = (
        (
            await session.execute(
                select(Project)
                .where(Project.id.in_(_project_ids))
                .order_by(Project.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    _locked_project_ids = {project.id for project in _locked_projects}
    _locked_residents = (
        (
            await session.execute(
                select(StrategicResident)
                .where(StrategicResident.project_id.in_(_project_ids))
                .order_by(StrategicResident.project_id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    _resident_by_project = {
        resident.project_id: resident for resident in _locked_residents
    }
    _legacy_project_ids = {
        project_id
        for project_id in _locked_project_ids
        if (
            _resident_by_project.get(project_id) is None
            or _resident_by_project[project_id].engine_owner == "legacy"
        )
    }
    _candidate_cycle_ids = sorted({c.id for c in stalled})
    _locked_cycles = (
        (
            await session.execute(
                select(StrategicCycle)
                .where(
                    StrategicCycle.id.in_(_candidate_cycle_ids),
                    StrategicCycle.project_id.in_(_legacy_project_ids),
                )
                .order_by(StrategicCycle.project_id.asc(), StrategicCycle.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
        if _legacy_project_ids
        else []
    )
    _linked_task_ids = sorted(
        {
            task_id
            for cycle in _locked_cycles
            for task_id in (
                cycle.phase1_task_id
                if cycle.outcome
                in (
                    strategic_cycle.OUTCOME_IN_PROGRESS,
                    strategic_cycle.OUTCOME_PHASE1_RUNNING,
                    strategic_cycle.OUTCOME_PHASE1_DONE,
                )
                else cycle.phase2_task_id,
            )
            if task_id is not None
        },
        key=str,
    )
    _linked_tasks: dict = {}
    if _linked_task_ids:
        _rows = await session.execute(
            select(Task)
            .where(Task.id.in_(_linked_task_ids))
            .order_by(Task.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        _linked_tasks = {task.id: task for task in _rows.scalars().all()}

    _phase_cutoff = _step7_now - timedelta(hours=3)
    for c in _locked_cycles:
        if c.outcome in (
            strategic_cycle.OUTCOME_IN_PROGRESS,
            strategic_cycle.OUTCOME_PHASE1_RUNNING,
            strategic_cycle.OUTCOME_PHASE1_DONE,
        ):
            stalled_phase = "phase1"
            linked_task_id = c.phase1_task_id
            if (
                c.phase1_started_at is None
                or c.phase1_started_at >= _phase_cutoff
                or c.phase2_started_at is not None
            ):
                continue
        elif c.outcome == strategic_cycle.OUTCOME_PHASE2_RUNNING:
            stalled_phase = "phase2"
            linked_task_id = c.phase2_task_id
            _phase_ref = c.phase2_started_at or c.phase1_started_at
            if _phase_ref is None or _phase_ref >= _phase_cutoff:
                continue
        else:
            continue

        if linked_task_id is not None:
            linked_task = _linked_tasks.get(linked_task_id)
            if linked_task is not None and linked_task.project_id != c.project_id:
                logger.warning(
                    f"[REAPER] Cycle {c.id} references cross-project task "
                    f"{linked_task_id}; skipping stall mutation"
                )
                continue
            if task_is_strategic_local_runner_hold(linked_task):
                logger.info(
                    f"[REAPER] Cycle {c.id} in {stalled_phase} >3h "
                    f"but linked task {linked_task_id} is intentionally "
                    f"waiting for a local Strategic runner — skipping stall declaration"
                )
                continue
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
                from app.failure_classification import (
                    AGENT_STALLED,
                    persist_failure_category,
                )

                persist_failure_category(linked_task, AGENT_STALLED)
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
            c.failure_reason = (
                f"reaper_stall: cycle in {stalled_phase} >3h with no completion"
            )
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
    await session.commit()
    if actually_stalled:
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "alert_created", payload)
            except Exception as _bcast_err:
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
    if not _is_cadence_due(
        _last_regression_scan, _REGRESSION_SCAN_INTERVAL_SECONDS, now_scan
    ):
        return 0

    _last_regression_scan = now_scan
    return await _run_regression_scan_and_notify(session)


async def sweep_metrics_integrity(session, now=None) -> dict | None:
    """Standing metrics-integrity sweep seam — recompute selected self-reported
    counters from ground truth and FLAG drift without mutating the source.

    Thin delegator to :func:`app.metrics_integrity_sweep.run_metrics_integrity_sweep`
    so the sweep lives beside the other reaper housekeeping sweeps. Mode- and
    cadence-gated inside the callee; returns the tick summary dict, or ``None``
    when gated out (mode off / cadence not due). The reaper wraps this call in
    its own try/except+rollback, so a raised error can never crash the cycle.
    """
    from app.metrics_integrity_sweep import run_metrics_integrity_sweep

    return await run_metrics_integrity_sweep(session, now)


# Cadence gate for the capability backing-proof sweep. Hourly to bound overall
# reaper cycle time — the sweep iterates projects and verifies file paths via
# git ls-files, so a slow project or deep file tree should not run every tick.
_BACKING_PROOF_INTERVAL_SECONDS = 3600
_last_backing_proof_sweep: datetime | None = None
# Independent cadence timer for the proof-earn loop (Step 22b). It shares the
# hourly interval above but MUST NOT share the timestamp global: Step 22 stamps
# _last_backing_proof_sweep at the top of the same reaper cycle, so a shared
# timer would report Step 22b as "not due" every cycle and permanently gate it
# out. Splitting the timers lets both run at the same cadence on separate clocks.
_last_proof_earn_loop_sweep: datetime | None = None


async def sweep_capability_backing_proofs(session, now=None) -> int:
    """Step 22: Record backing-path proof for capabilities whose backing files
    all resolve at HEAD.

    Iterates projects (bounded, default 25), finds working capabilities with
    non-empty ``backing_paths``, verifies each path via git ls-files against
    HEAD, and — when ALL paths are definitely present — records a bounded
    ``backing_path`` proof entry on the project via
    :func:`app.capability_proof.record_capability_backing_path_proof`.

    Cadence-gated to hourly. Returns the number of new proof entries recorded.
    Any path returning ``None`` (indeterminate) prevents proof recording for
    that capability — only a definite True on every path triggers a record.
    Wrapped so it can never crash the reaper cycle.
    """
    global _last_backing_proof_sweep
    from app import source_ref_validation as _srv
    from app.capability_proof import record_capability_backing_path_proof
    from app.models import Project
    from sqlalchemy.orm import load_only

    _now = now or _utcnow()
    if not _is_cadence_due(
        _last_backing_proof_sweep, _BACKING_PROOF_INTERVAL_SECONDS, _now
    ):
        return 0
    _last_backing_proof_sweep = _now

    max_projects = 25
    rows = (
        (
            await session.execute(
                select(Project)
                .options(
                    load_only(
                        Project.id,
                        Project.problem_model,
                        Project.capability_proof_records,
                        raiseload=True,
                    )
                )
                .limit(max_projects)
            )
        )
        .scalars()
        .all()
    )

    if not rows:
        return 0

    total_recorded = 0
    for project in rows:
        project_recorded = 0
        try:
            pm = getattr(project, "problem_model", None)
            if not isinstance(pm, dict):
                continue
            capabilities = pm.get("capabilities")
            if not isinstance(capabilities, list):
                continue

            for cap in capabilities:
                if not isinstance(cap, dict):
                    continue
                status = cap.get("status", "").strip().lower()
                if status not in ("working", "partial"):
                    continue
                backing_paths = cap.get("backing_paths")
                if not isinstance(backing_paths, list) or not backing_paths:
                    continue

                cap_name = cap.get("name")
                if not isinstance(cap_name, str) or not cap_name:
                    continue

                # Filter to file-like paths; skip globs, logical ids, etc.
                file_paths = [
                    p
                    for p in backing_paths
                    if isinstance(p, str) and _srv._looks_like_file_path(p)
                ]
                if not file_paths:
                    continue

                # Check every path against HEAD. A single None (indeterminate)
                # prevents proof recording — only definite True on ALL paths
                # is sufficient.
                all_present = True
                for p in file_paths:
                    exists = await _srv._path_exists_at_head(p)
                    if exists is not True:
                        all_present = False
                        break

                if all_present and file_paths:
                    recorded = record_capability_backing_path_proof(
                        project, cap_name
                    )
                    if recorded:
                        project_recorded += 1

            if project_recorded:
                session.add(project)
                await session.commit()
                total_recorded += project_recorded

        except Exception:  # noqa: BLE001
            logger.warning(
                "[REAPER] Capability backing-proof sweep failed for project %s:",
                getattr(project, "id", "?"),
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()

    if total_recorded:
        logger.info(
            "[REAPER] Step 22: recorded %d backing-path proof(s)",
            total_recorded,
        )
    return total_recorded


# Cadence gate for the moot-pending-confirmation auto-clear sweep (Step 22e).
# Hourly, on an independent timer (see the Step 22/22b split note above for why
# each cadence-gated sweep needs its own timestamp global).
_MOOT_CONFIRM_INTERVAL_SECONDS = 3600
_last_moot_confirm_sweep: datetime | None = None


async def _owner_llm_credentials_ready(session, user_id: str | None) -> bool:
    """Whether the project owner has >=1 enabled, runtime-ready LLM account.

    Reuses the DB-side readiness check the pool uses
    (``max_plan_pool._db_account_runtime_ready``) rather than inventing a new
    probe — a positive result means the credential the runtime resolves at
    dispatch is present. Fail-closed: no user / no accounts / any error -> False.
    """
    if not user_id:
        return False
    from app.max_plan_pool import _db_account_runtime_ready
    from app.models import MaxPlanAccount

    accounts = (
        (
            await session.execute(
                select(MaxPlanAccount).where(
                    MaxPlanAccount.user_id == user_id,
                    MaxPlanAccount.enabled == True,  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )
    return any(_db_account_runtime_ready(a) for a in accounts)


async def sweep_auto_clear_moot_pending_confirmations(session, now=None) -> int:
    """Step 22e: Auto-clear moot ``pending_user_confirmation`` items.

    Long-stale operator-action items pile up in ``ProblemModel.pending_user_confirmation``
    because clearing is entirely manual and the strategy-internals surface where
    they live is unvisited. This sweep removes the items whose ask the system can
    already prove is resolved, so the operator's needs-action surface (which
    re-derives from the durable list each poll) naturally drops them.

    Strictly conservative and fail-closed:

    - only ``credentials_needed`` / ``access_needed`` items are eligible;
      ``intent_change`` / ``major_decision`` require a human decision and are
      NEVER auto-removed;
    - an item is removed ONLY when its moot condition is POSITIVELY detected via
      the existing credential/access resolution paths — owner LLM-account
      readiness (``_db_account_runtime_ready``) for credentials, a live cached
      project repo token (``get_cached_project_token``) for access — AND the
      item text references that resource class (see
      :func:`app.problem_model._confirmation_is_moot`);
    - when uncertain, the item is left in place.

    Iterates ALL projects with a non-empty pending list via keyset pagination
    (ordered by ``Project.id``, batches of 100) so every project is covered
    deterministically regardless of fleet size. Mutates the durable model via
    the pure :func:`app.problem_model.auto_clear_resolved_confirmations` helper
    (same revision-log / ``last_updated_by`` contract as the operator dismiss
    endpoint), and persists per project. Hourly cadence-gated, idempotent, and
    wrapped so it can never crash the reaper cycle. Returns the total number of
    items cleared.
    """
    global _last_moot_confirm_sweep
    from app.github_token_resolver import get_cached_project_token
    from app.models import Project
    from app.problem_model import (
        ConfirmationResolutionSignals,
        auto_clear_resolved_confirmations,
        parse_problem_model,
    )
    from sqlalchemy.orm import load_only
    from sqlalchemy.orm.attributes import flag_modified

    _now = now or _utcnow()
    if not _is_cadence_due(
        _last_moot_confirm_sweep, _MOOT_CONFIRM_INTERVAL_SECONDS, _now
    ):
        return 0
    _last_moot_confirm_sweep = _now

    _BATCH = 100
    last_id = None
    total_cleared = 0

    while True:
        query = (
            select(Project)
            .options(
                load_only(
                    Project.id,
                    Project.user_id,
                    Project.problem_model,
                    raiseload=True,
                )
            )
            .where(Project.problem_model.is_not(None))
            .order_by(Project.id)
            .limit(_BATCH)
        )
        if last_id is not None:
            query = query.where(Project.id > last_id)

        rows = (await session.execute(query)).scalars().all()
        if not rows:
            break

        last_id = rows[-1].id

        for project in rows:
            try:
                pm = getattr(project, "problem_model", None)
                if not isinstance(pm, dict):
                    continue
                pending = pm.get("pending_user_confirmation")
                if not isinstance(pending, list) or not pending:
                    continue
                # Skip early unless there is at least one auto-eligible item, so the
                # common case does no credential/token I/O.
                if not any(
                    isinstance(p, dict)
                    and p.get("type") in ("credentials_needed", "access_needed")
                    for p in pending
                ):
                    continue

                try:
                    model = parse_problem_model(pm)
                except Exception:  # noqa: BLE001 — a malformed model must not block the sweep
                    continue

                signals = ConfirmationResolutionSignals(
                    llm_credentials_ready=await _owner_llm_credentials_ready(
                        session, getattr(project, "user_id", None)
                    ),
                    repo_access_ready=bool(get_cached_project_token(str(project.id))),
                )
                cleared = auto_clear_resolved_confirmations(
                    model, signals, _now.isoformat()
                )
                if cleared:
                    project.problem_model = model.model_dump()
                    flag_modified(project, "problem_model")
                    session.add(project)
                    await session.commit()
                    total_cleared += len(cleared)

            except Exception:  # noqa: BLE001
                logger.warning(
                    "[REAPER] Moot pending-confirmation sweep failed for project %s:",
                    getattr(project, "id", "?"),
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()

        if len(rows) < _BATCH:
            break

    if total_cleared:
        logger.info(
            "[REAPER] Step 22e: auto-cleared %d moot pending confirmation(s)",
            total_cleared,
        )
    return total_cleared


# Cadence gate for the stale-decay pending-confirmation sweep (Step 22e-ii).
# 6-hourly, independent timer (age-based decay; much cheaper than moot sweep
# which does credential I/O per project, so longer interval is fine).
_STALE_CONFIRM_DECAY_INTERVAL_SECONDS = 21600
_last_stale_confirm_decay_sweep: datetime | None = None


async def sweep_decay_stale_pending_confirmations(session, now=None) -> int:
    """Step 22e-ii: Age-decay stale ``pending_user_confirmation`` items.

    Complements :func:`sweep_auto_clear_moot_pending_confirmations` (which is
    fast-path / signal-based and only touches ``credentials_needed`` /
    ``access_needed``).  This sweep removes ANY confirmation item whose
    ``created_at`` is older than
    :data:`app.problem_model.PENDING_CONFIRMATION_STALE_DECAY_DAYS`, including
    ``intent_change`` and ``major_decision`` items that the moot sweep can never
    remove.

    Items with a missing or unparseable ``created_at`` are conservatively kept.
    Iterates ALL projects via keyset pagination (batches of 100).  6-hourly
    cadence-gated on an independent timer.  Wrapped so it can never crash the
    reaper cycle.  Returns the total number of items decayed.
    """
    global _last_stale_confirm_decay_sweep
    from app.models import Project
    from app.problem_model import (
        decay_stale_pending_confirmations,
        parse_problem_model,
    )
    from sqlalchemy.orm import load_only
    from sqlalchemy.orm.attributes import flag_modified

    _now = now or _utcnow()
    if not _is_cadence_due(
        _last_stale_confirm_decay_sweep, _STALE_CONFIRM_DECAY_INTERVAL_SECONDS, _now
    ):
        return 0
    _last_stale_confirm_decay_sweep = _now

    now_iso = _now.isoformat()
    _BATCH = 100
    last_id = None
    total_decayed = 0

    while True:
        query = (
            select(Project)
            .options(
                load_only(
                    Project.id,
                    Project.problem_model,
                    raiseload=True,
                )
            )
            .where(Project.problem_model.is_not(None))
            .order_by(Project.id)
            .limit(_BATCH)
        )
        if last_id is not None:
            query = query.where(Project.id > last_id)

        rows = (await session.execute(query)).scalars().all()
        if not rows:
            break

        last_id = rows[-1].id

        for project in rows:
            try:
                pm = getattr(project, "problem_model", None)
                if not isinstance(pm, dict):
                    continue
                pending = pm.get("pending_user_confirmation")
                if not isinstance(pending, list) or not pending:
                    continue

                try:
                    model = parse_problem_model(pm)
                except Exception:  # noqa: BLE001
                    continue

                decayed = decay_stale_pending_confirmations(model, now_iso)
                if decayed:
                    project.problem_model = model.model_dump()
                    flag_modified(project, "problem_model")
                    session.add(project)
                    await session.commit()
                    total_decayed += len(decayed)

            except Exception:  # noqa: BLE001
                logger.warning(
                    "[REAPER] Stale pending-confirmation decay failed for project %s:",
                    getattr(project, "id", "?"),
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()

        if len(rows) < _BATCH:
            break

    if total_decayed:
        logger.info(
            "[REAPER] Step 22e-ii: age-decayed %d stale pending confirmation(s)",
            total_decayed,
        )
    return total_decayed


async def sweep_proof_earn_loop(session, now=None) -> dict[str, Any]:
    """Step 22b: Earn proof for demanded unproven working capabilities.

    Computes the intersection of demanded capabilities and unproven working
    capabilities via :func:`commission_proof_earn_loop` and attempts to earn
    proof by verifying backing_paths at HEAD.  For demanded capabilities that
    have no backing_paths, the gap is surfaced in the return summary so the
    strategist layer can act.

    When there is NO demand (no blocked dependents — common when the proof gate
    is in ``off`` mode), proactively builds a synthetic demand covering ALL
    unproven working capabilities so the earn loop can still prove capabilities
    independently of the demand signal.  This breaks the circularity: demand
    requires blocked dependents, blocked dependents require enforce mode, and
    enforce mode requires proven capabilities.  Without this fallthrough the
    earn loop would never fire on a project with all capabilities unproven and
    no blocked dependents, leaving the gate permanently stuck in ``off``.

    Cadence-gated to the same hourly interval as the backing-path sweep, but on
    an INDEPENDENT timer (``_last_proof_earn_loop_sweep``) so Step 22 stamping
    its own timer earlier in the same cycle cannot suppress this sweep.
    Returns a summary dict (see ``commission_proof_earn_loop`` return doc),
    or ``{}`` when gated.
    """
    global _last_proof_earn_loop_sweep
    from app.capability_demand import collect_missing_capability_demand
    from app.capability_proof import commission_proof_earn_loop
    from app.capability_registry import (
        consumer_gate_proof_status,
    )
    from app.models import Project
    from sqlalchemy.orm import load_only

    _now = now or _utcnow()
    if not _is_cadence_due(
        _last_proof_earn_loop_sweep, _BACKING_PROOF_INTERVAL_SECONDS, _now
    ):
        return {}
    _last_proof_earn_loop_sweep = _now

    max_projects = 25
    rows = (
        (
            await session.execute(
                select(Project)
                .options(
                    load_only(
                        Project.id,
                        Project.problem_model,
                        Project.capability_proof_records,
                        raiseload=True,
                    )
                )
                .limit(max_projects)
            )
        )
        .scalars()
        .all()
    )

    if not rows:
        return {}

    combined: dict[str, Any] = {
        "total_demanded_unproven": 0,
        "earned": 0,
        "no_backing_paths": [],
        "still_unproven": [],
        "path_proven_stuck": [],
        "named_test_executed": [],
        "execution_proven": [],
    }

    for project in rows:
        try:
            pm = getattr(project, "problem_model", None)
            if not isinstance(pm, dict):
                continue
            capabilities = pm.get("capabilities")
            if not isinstance(capabilities, list) or not capabilities:
                continue

            # Fetch demand for this project.
            demand_result = await collect_missing_capability_demand(
                session, project.id, now=_now
            )

            # When there is NO demand (no blocked dependents), proactively attempt
            # to prove ALL unproven working capabilities. This breaks the
            # circularity where demand requires blocked dependents, blocked
            # dependents require enforce mode, and enforce mode requires proven
            # capabilities. Without this fallthrough, the earn loop would never
            # fire on a project with all capabilities unproven and no blocked
            # dependents, leaving the gate permanently stuck in 'off'.
            if not demand_result.get("demands"):
                proof_by_name = consumer_gate_proof_status(
                    pm,
                    getattr(project, "capability_proof_records", None),
                    now=_now,
                )
                unproven_names = [
                    name for name, status in proof_by_name.items()
                    if status != "proven"
                ]
                if not unproven_names:
                    continue
                # Build a synthetic demand covering all unproven working
                # capabilities so commission_proof_earn_loop can process them.
                synthetic_demands = [
                    {
                        "kind": "capability",
                        "ref": name,
                        "label": name,
                        "dependent_count": 1,
                    }
                    for name in unproven_names
                ]
                demand_result = {
                    "total": len(synthetic_demands),
                    "demands": synthetic_demands,
                }

            result = await commission_proof_earn_loop(
                project, demand_result, pm, session=session
            )
            if result.get("total_demanded_unproven", 0) > 0:
                combined["total_demanded_unproven"] += result["total_demanded_unproven"]
                combined["earned"] += result["earned"]
                combined["no_backing_paths"].extend(result.get("no_backing_paths", []))
                combined["still_unproven"].extend(result.get("still_unproven", []))
                combined["path_proven_stuck"].extend(result.get("path_proven_stuck", []))
                combined["named_test_executed"].extend(
                    result.get("named_test_executed", [])
                )
                combined["execution_proven"].extend(
                    result.get("execution_proven", [])
                )
                if result["earned"] > 0:
                    session.add(project)
                    await session.commit()

        except Exception:  # noqa: BLE001
            logger.warning(
                "[REAPER] Proof-earn loop failed for project %s:",
                getattr(project, "id", "?"),
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()

    if (
        combined["earned"]
        or combined["still_unproven"]
        or combined["path_proven_stuck"]
        or combined["named_test_executed"]
    ):
        logger.info(
            "[REAPER] Step 22b: proof-earn loop: total_demanded_unproven=%d "
            "earned=%d named_test_executed=%d no_backing_paths=%d "
            "still_unproven=%d path_proven_stuck=%d",
            combined["total_demanded_unproven"],
            combined["earned"],
            len(combined["named_test_executed"]),
            len(combined["no_backing_paths"]),
            len(combined["still_unproven"]),
            len(combined["path_proven_stuck"]),
        )
    return combined


async def sweep_stuck_qualifications(session, *, session_factory) -> int:
    """Step 9: Finalize release qualifications stuck 'pending'.

    Returns the number of qualifications finalized.
    """
    from app import release_qualifier

    finalized = await release_qualifier.sweep_stuck_qualifications(
        session, session_factory=session_factory
    )
    if finalized:
        logger.info(f"[REAPER] Finalized {finalized} stuck release qualification(s)")
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


async def sweep_expired_message_attachment_uploads(
    session,
    *,
    batch_size: int = 32,
    timeout_seconds: float = 5.0,
) -> int:
    """Delete a global bounded batch of expired, unconsumed upload receipts."""

    receipts = (
        await session.execute(
            select(ProjectMessageAttachmentUpload)
            .where(
                ProjectMessageAttachmentUpload.consumed_at.is_(None),
                ProjectMessageAttachmentUpload.message_id.is_(None),
                ProjectMessageAttachmentUpload.expires_at <= _utcnow(),
                ~select(ArtifactRefPromotionIntent.id)
                .where(
                    ArtifactRefPromotionIntent.receipt_id
                    == ProjectMessageAttachmentUpload.attachment_id,
                    ArtifactRefPromotionIntent.state != "settled",
                )
                .exists(),
            )
            .order_by(ProjectMessageAttachmentUpload.expires_at.asc())
            .limit(max(1, min(int(batch_size), 100)))
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    if not receipts:
        return 0
    try:
        deleted = await asyncio.wait_for(
            asyncio.gather(
                *(
                    delete_storage_uri_best_effort(receipt.storage_uri)
                    for receipt in receipts
                )
            ),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except TimeoutError:
        await session.rollback()
        logger.warning("[REAPER] Expired message attachment cleanup timed out")
        return 0

    removed = 0
    for receipt, object_deleted in zip(receipts, deleted, strict=True):
        if object_deleted:
            await session.delete(receipt)
            removed += 1
    if removed:
        await session.commit()
        logger.info(
            "[REAPER] Deleted %d expired message attachment upload(s)",
            removed,
        )
    else:
        await session.rollback()
    return removed


async def sweep_evergreen_fill_decision_retention(
    session, *, retention_days: int = 30
) -> int:
    """Purge EvergreenFillDecisionLog rows older than retention_days (default 30).

    Returns the number of rows deleted.
    """
    from app.models import EvergreenFillDecisionLog
    from sqlalchemy import delete

    cutoff = _utcnow() - timedelta(days=retention_days)
    res = await session.execute(
        delete(EvergreenFillDecisionLog).where(EvergreenFillDecisionLog.ts < cutoff)
    )
    count = res.rowcount
    if count:
        await session.commit()
        logger.info(f"[REAPER] Purged {count} stale evergreen fill decision log row(s)")
    return count


# ── Step 12: BLOCKED-task auto-unblock / escalation sweep ──────────────────
# (constants and sweep helpers live in app.blocked_task_unblock_sweeps)


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
    from app.blocked_need_resolution import ACTIVE_CANDIDATE_STATUSES
    from app.broadcast import broadcast_to_project as _bcast
    from app.digest_metrics import (
        apply_foundation_convergence_action,
        compute_foundation_convergence_actions,
    )

    global _last_converge_duplicate_scan
    _now = now or _utcnow()
    if not _is_cadence_due(
        _last_converge_duplicate_scan, _FLEET_SCAN_INTERVAL_SECONDS, _now
    ):
        return 0
    _last_converge_duplicate_scan = _now

    active = [TaskStatus(s) for s in ACTIVE_CANDIDATE_STATUSES]
    rows = (
        (await session.execute(select(Task).where(Task.status.in_(active)).limit(500)))
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
            dependent = apply_foundation_convergence_action(
                action, by_id, tasks, logger=logger
            )
            if dependent is None:
                continue
            found_id = action["foundation_id"]
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
                "[REAPER] Staged-direction activation sweep error for project %s: %s",
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
    fallback_days = int(runtime_config.get("staged_direction_stale_fallback_days", 21))

    # Same prefilter as the activation sweep: candidate projects via the
    # normalised direction table. We read the staged rows' ``data`` blobs here
    # (verbatim ``Project.directions`` shape) rather than the project blob.
    rows = (
        (await session.execute(select(Direction).where(Direction.status == "staged")))
        .scalars()
        .all()
    )
    if not rows:
        return 0

    # Group staged rows by project so each project reads its open version once.
    # Snapshot the fields we read (the ``data`` blob + ``direction_key``) into
    # plain values NOW. A per-project failure below calls session.rollback()
    # (rollback expires every Direction row regardless of expire_on_commit), and
    # a later row.data / row.direction_key read on an expired row would fire a
    # sync lazy-load inside the AsyncSession -> MissingGreenlet, spuriously
    # skipping every subsequent project's staged rows. Plain locals never expire.
    by_project: dict = {}
    for r in rows:
        by_project.setdefault(r.project_id, []).append(
            (dict(r.data or {}), r.direction_key)
        )

    now = _utcnow()
    total_alerts = 0
    for pid, staged_rows in by_project.items():
        try:
            open_version = await current_open_version(session, pid)
            open_n = open_version.version_number if open_version else None
            for d, row_direction_key in staged_rows:
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
                    direction_id=str(d.get("id") or row_direction_key),
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


# ── Recovery: stranded transient-blocker self-heal sweep ─────────────────────
#
# A file_conflict or pending_rollup blocker that was escalated before Fix 2
# landed was hard-parked at ``_BLOCKED_ESCALATED_PARK`` (+10 years).  These
# rows never appear in the normal Step-12 due query (``recheck_after <= now``)
# and would stay stranded forever without this targeted recovery pass.  The
# fix: find them, reset ``recheck_after`` to the bounded cadence, and let the
# next Step-12 cycle re-evaluate and requeue them when the conflict lifts.
#
# The recovery touch is the minimal safe change: it preserves ``_blocked_escalated``
# (so the alert is not re-fired) and just gives the row a near-future due date
# so Step 12 picks it up.  Step 12's updated "need-less parked transient" path
# (Fix 2) then actually decides whether to requeue or re-arm.


async def sweep_recover_stranded_transient_blockers(
    session, now: datetime, *, batch_size: int = 200
) -> int:
    """Self-healing recovery for transient blockers previously hard-parked at +10y.

    Finds BLOCKED tasks whose ``blocker_kind`` is a transient kind
    (``file_conflict`` or ``pending_rollup``) and whose ``recheck_after`` is
    more than ``_TRANSIENT_BLOCKER_PARK_SENTINEL`` (365 days) in the future —
    the signature of the old hard-park sentinel — and resets ``recheck_after``
    to ``now + _BLOCKED_PARKED_REEVAL_INTERVAL`` so the Step-12 sweep picks them
    up on the next cycle.  Idempotent and safe to run repeatedly.

    Returns the number of rows recovered.
    """
    sentinel_cutoff = now + _TRANSIENT_BLOCKER_PARK_SENTINEL
    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.status == TaskStatus.BLOCKED,
                    Task.blocker_kind.in_(["file_conflict", "pending_rollup"]),
                    Task.recheck_after > sentinel_cutoff,
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    recovered = 0
    for task in rows:
        ctx = task.context or {}
        if not ctx.get("_blocked_escalated"):
            # Unexpected: far-future recheck on a non-escalated transient blocker
            # (shouldn't happen but guard defensively; don't touch it).
            continue
        task.recheck_after = now + _BLOCKED_PARKED_REEVAL_INTERVAL
        session.add(task)
        recovered += 1
        logger.info(
            "[REAPER] Recovery: reset hard-parked transient blocker %s "
            "(kind=%s) to bounded re-eval",
            task.id,
            task.blocker_kind,
        )

    if recovered:
        await session.commit()
        logger.info(
            "[REAPER] Recovery: un-stranded %d hard-parked transient blocker(s)",
            recovered,
        )
    return recovered


# ── Strategic cycle starvation sweep ─────────────────────────────────────────

# Autonomous projects older than this window with no StrategicCycle row trigger
# a strategic_cycle_stall WARNING Alert.
_STRATEGIC_CYCLE_STARVATION_DEFAULT_HOURS = 24
_STRATEGIC_CYCLE_STALL_ALERT_TITLE = "Strategic engine starved: no cycle in window"


async def _sweep_strategic_cycle_starvation(session, now: datetime) -> int:
    """Emit a deduped strategic_cycle_stall Alert for autonomous projects with no
    recent StrategicCycle row at all. Returns the number of alerts created."""
    from app.models import Alert, AlertSeverity, Project, StrategicCycle
    from app.shared_state import runtime_config as _runtime_config

    starvation_hours = int(
        _runtime_config.get(
            "strategic_cycle_starvation_hours",
            _STRATEGIC_CYCLE_STARVATION_DEFAULT_HOURS,
        )
        or _STRATEGIC_CYCLE_STARVATION_DEFAULT_HOURS
    )
    cutoff = now - timedelta(hours=starvation_hours)

    project_rows = (
        await session.execute(
            select(Project.id, Project.created_at).where(
                Project.autonomous_enabled.is_(True),
                Project.not_archived(),
            )
        )
    ).all()

    # New projects have not had time to establish a cadence — skip until the
    # project itself is older than the starvation window. Filter in Python so
    # the two batched lookups below only run over genuine candidates.
    candidate_ids: list = []
    for project_id, project_created_at in project_rows:
        if project_created_at is not None:
            pc = project_created_at
            if pc.tzinfo is not None:
                pc = pc.replace(tzinfo=None)
            if pc > cutoff:
                continue
        candidate_ids.append(project_id)

    created = 0
    if not candidate_ids:
        return created

    # Batched lookup 1: which candidate projects already have a recent cycle.
    # Replaces a per-project existence SELECT (the old N+1). Equivalent to the
    # prior ``phase1_started_at >= cutoff`` LIMIT 1 existence check, collapsed
    # into one round-trip via DISTINCT.
    projects_with_recent_cycle = set(
        (
            await session.execute(
                select(StrategicCycle.project_id)
                .where(
                    StrategicCycle.project_id.in_(candidate_ids),
                    StrategicCycle.phase1_started_at >= cutoff,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    # Batched lookup 2: which candidate projects already have an open WARNING
    # dedup alert. Replaces the per-project dedup SELECT (the second N+1).
    projects_with_open_alert = set(
        (
            await session.execute(
                select(Alert.project_id)
                .where(
                    Alert.project_id.in_(candidate_ids),
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == _STRATEGIC_CYCLE_STALL_ALERT_TITLE,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    for project_id in candidate_ids:
        if project_id in projects_with_recent_cycle:
            continue
        # Dedup: one open WARNING alert per project for this title.
        if project_id in projects_with_open_alert:
            continue

        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.WARNING,
                title=_STRATEGIC_CYCLE_STALL_ALERT_TITLE,
                message=(
                    f"No strategic cycle has started in the last {starvation_hours}h "
                    "for this autonomous project — the strategic engine appears "
                    "starved (zero cycles, not merely stalled mid-cycle). Review the "
                    "strategic loop scheduler and recent orchestrator logs."
                )[:1000],
            )
        )
        created += 1
    if created:
        await session.commit()
    return created


# ── Step 20: Orphaned cross-base children that can never land ─────────────────
#
# A research/plan parent converges to COMPLETED while a child task stays QUEUED
# forever behind a cross-base genuine code-dependency whose landed-signal gate
# can never be satisfied: the dep is COMPLETED but its rollup commit was
# definitively confirmed NOT to be an ancestor of the project's integration
# branch (``STRANDED_LANDING_REASON_KEY`` is set on the dep by the ancestry check
# that stamped ``is_ancestor_of -> False``). No new rollup will ever land the dep
# on integration, so the child's ``has_landed_signal`` gate can never flip.
#
# Conservative policy: only the ``STRANDED_LANDING_REASON_KEY`` marker triggers
# cancellation. A dep whose ``landed_on_integration`` is merely False without
# that marker may still receive its signal via rollup propagation (the
# in-progress sibling fix that propagates the signal to carried children) and
# MUST be left untouched.


def _has_permanently_stranded_cross_base_dep(child: "Task", dep_tasks: list) -> bool:
    """True when *child* has a permanently-stranded cross-base genuine code-dep.

    A dep is permanently stranded when it is COMPLETED with no landed signal
    AND with ``STRANDED_LANDING_REASON_KEY`` explicitly set — durable proof that
    the dep's rollup commit is not an ancestor of the integration branch. A dep
    that merely has ``landed_on_integration=False`` without the stranded marker
    may still receive its signal via rollup propagation and must not be cancelled.
    """
    from app.orchestrator_health import _same_base_branch, is_genuine_code_dependency
    from app.landed_signal import STRANDED_LANDING_REASON_KEY, has_landed_signal

    for dep in dep_tasks:
        if dep.status != TaskStatus.COMPLETED:
            continue
        if _same_base_branch(child, dep):
            continue
        if not is_genuine_code_dependency(child, dep):
            continue
        if has_landed_signal(dep):
            continue
        if not (dep.context or {}).get(STRANDED_LANDING_REASON_KEY):
            continue
        return True
    return False


async def sweep_orphaned_cross_base_children(session, *, batch_size: int = 500) -> int:
    """Step 20: Cancel QUEUED children orphaned behind a permanently-stranded cross-base dep.

    Scans QUEUED tasks with a parent_task_id whose parent is COMPLETED, then
    for each such candidate checks whether any of its ``depends_on`` deps is a
    cross-base genuine code-dependency that can never land (dep COMPLETED,
    ``has_landed_signal`` False, ``STRANDED_LANDING_REASON_KEY`` set). Matches
    are cancelled and stamped ``_orphaned_cross_base_cancel: True``.

    Returns the number of children cancelled.
    """
    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.status == TaskStatus.QUEUED,
                    Task.parent_task_id.isnot(None),
                )
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    # Batched parent load: only keep candidates whose parent is COMPLETED.
    parent_ids = list({r.parent_task_id for r in rows})
    parent_rows = (
        (await session.execute(select(Task).where(Task.id.in_(parent_ids))))
        .scalars()
        .all()
    )
    parent_by_id = {p.id: p for p in parent_rows}

    candidates = [
        r
        for r in rows
        if parent_by_id.get(r.parent_task_id) is not None
        and parent_by_id[r.parent_task_id].status == TaskStatus.COMPLETED
        and (r.depends_on or [])
    ]
    if not candidates:
        return 0

    # Batched dep load.
    all_dep_ids: set = set()
    for cand in candidates:
        for raw in cand.depends_on or []:
            try:
                all_dep_ids.add(uuid.UUID(str(raw)))
            except (ValueError, AttributeError):
                continue
    dep_rows: list = []
    if all_dep_ids:
        dep_rows = (
            (await session.execute(select(Task).where(Task.id.in_(all_dep_ids))))
            .scalars()
            .all()
        )
    dep_by_id = {str(d.id): d for d in dep_rows}

    cancelled = 0
    for cand in candidates:
        dep_tasks = [
            dep_by_id[str(raw)]
            for raw in (cand.depends_on or [])
            if str(raw) in dep_by_id
        ]
        if not _has_permanently_stranded_cross_base_dep(cand, dep_tasks):
            continue
        cand.status = TaskStatus.CANCELLED
        cand.context = {
            **(cand.context or {}),
            "_orphaned_cross_base_cancel": True,
        }
        session.add(cand)
        cancelled += 1
        logger.info(
            "[REAPER] Step 20: cancelled orphaned cross-base child %s "
            "(parent %s COMPLETED, dep permanently stranded)",
            cand.id,
            cand.parent_task_id,
        )

    if cancelled:
        await session.commit()
        logger.info(
            "[REAPER] Step 20: cancelled %d orphaned cross-base child task(s)",
            cancelled,
        )
    return cancelled


# ── Stage-2 generalized message lifecycle sweeps (design d-7c851b3f) ──────────
#
# Two cadence sweeps that generalize the bug-class-only convergence machinery so
# actionable operator items auto-clear when their underlying condition resolves
# and dead conversation ages out — WITHOUT operator involvement:
#
#   1. sweep_resolve_linked_messages — resolves ANY 'linked' NON-bug-class
#      message (NOTE / QUESTION / FEEDBACK) once its recorded _convergence_link
#      target genuinely lands. Bug-class ('BUG_REPORT'/'BLOCKER') linked messages
#      stay owned by sweep_converge_linked_bug_messages (Step 15), which ALSO
#      resurfaces on failure; the two are disjoint by message_type and together
#      cover every 'linked' message.
#   2. sweep_decay_stale_open_messages — auto-resolves an OPEN conversational
#      (NOTE/FEEDBACK) item with no activity for STALE_OPEN_DECAY_DAYS and no
#      live linked condition, stamping resolution 'auto-decayed (stale)'.
#      Unanswered QUESTION items age out at a longer window (STALE_QUESTION_DECAY_DAYS)
#      and receive a distinct resolution reason 'stale_unanswered' so the operator
#      can distinguish auto-resolved from genuinely-answered questions.
#      Genuinely-live BLOCKER items are never decayed.

# Decay window (days) after which a stale open NOTE/FEEDBACK message is
# auto-resolved. Exposed as a module constant so the one-time backlog migration
# reuses the SAME threshold; overridable at runtime via
# runtime_config['stale_open_message_decay_days'].
STALE_OPEN_DECAY_DAYS = 14

# Longer decay window for unanswered QUESTION messages — questions deserve more
# patience than plain notes. Overridable at runtime via
# runtime_config['stale_question_decay_days'].
STALE_QUESTION_DECAY_DAYS = 30


async def sweep_resolve_linked_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
) -> int:
    """Resolve NON-bug-class 'linked' messages whose owning outcome landed.

    Generalizes the positive-convergence half of Step 15 to NOTE/QUESTION/
    FEEDBACK: a 'linked' message is terminally resolved once its recorded
    ``_convergence_link`` target genuinely LANDS (direction 'completed' or task
    merged-completion), clearing its needs-action derivation without operator
    involvement. Disjoint from Step 15 by message_type (bug-class stays owned by
    ``sweep_converge_linked_bug_messages``, which also resurfaces on failure).

    An outcome that has not landed leaves the message 'linked' — manual resolve
    and the bug-class resurfacing sweep remain the fallback. Fail-safe per
    message (one bad row never aborts the batch), idempotent (resolution moves
    status off 'linked'), and broadcasts a ``message_updated`` envelope per row.

    Returns the number of messages auto-resolved.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.models import ProjectMessage
    from app.message_convergence import (
        BUG_CLASS_TYPES,
        LINKED_STATUS,
        linked_outcome_landed_resolution,
        mark_message_resolved,
    )
    from app.project_message_projection import message_updated_event_payload

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == LINKED_STATUS,
                    ProjectMessage.message_type.notin_(list(BUG_CLASS_TYPES)),
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
            "[REAPER] linked-message resolve sweep hit cap=%d linked messages — "
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
            audit = await linked_outcome_landed_resolution(session, msg, now=now)
        except Exception:
            logger.warning(
                "[REAPER] linked-message resolve failed for message %s — "
                "leaving linked",
                msg.id,
                exc_info=True,
            )
            continue
        if audit is None:
            continue
        mark_message_resolved(msg, now, f"Owned outcome landed: {audit}")
        session.add(msg)
        resolved += 1
        pending_broadcasts.append(
            (str(msg.project_id), message_updated_event_payload(msg))
        )
        logger.info(
            "[REAPER] linked-message resolve: auto-resolved message %s — %s",
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
                    "[REAPER] linked-message resolve broadcast failed for %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] linked-message resolve: auto-resolved %d linked message(s)",
            resolved,
        )
    return resolved


async def sweep_decay_stale_open_messages(
    session,
    now: datetime,
    *,
    batch_size: int = 200,
) -> int:
    """Auto-resolve dead conversational (NOTE/FEEDBACK/QUESTION) open threads.

    NOTE/FEEDBACK: aged out after ``STALE_OPEN_DECAY_DAYS`` (overridable via
    ``runtime_config['stale_open_message_decay_days']``), stamping
    ``resolution='auto-decayed (stale)'``.

    QUESTION: aged out after ``STALE_QUESTION_DECAY_DAYS`` (overridable via
    ``runtime_config['stale_question_decay_days']``), stamping
    ``resolution='stale_unanswered'`` so operators can distinguish auto-resolved
    from genuinely-answered questions.

    Genuinely-live BLOCKER items and any message carrying a live
    ``_convergence_link`` are left untouched.  Fail-safe per message, idempotent
    (resolution moves status off 'open'), and broadcasts a ``message_updated``
    envelope per resolved row.

    Returns the number of messages auto-decayed.
    """
    from app.broadcast import broadcast_to_project as _bcast
    from app.message_audience import OPERATOR_ACTION_NEEDED
    from app.models import ProjectMessage
    from app.message_convergence import (
        CONVERSATIONAL_DECAY_MESSAGE_TYPES,
        OPEN_STATUS,
        get_message_link,
        mark_message_resolved,
    )
    from app.project_message_projection import message_updated_event_payload
    from app.shared_state import runtime_config

    decay_days = int(
        runtime_config.get("stale_open_message_decay_days", STALE_OPEN_DECAY_DAYS)
    )
    question_decay_days = int(
        runtime_config.get("stale_question_decay_days", STALE_QUESTION_DECAY_DAYS)
    )
    # Use the shorter cutoff for the DB fetch so we retrieve all candidates;
    # per-message type logic below applies the correct window per row.
    min_decay_days = min(decay_days, question_decay_days)
    cutoff = now - timedelta(days=min_decay_days)

    msgs = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(
                    ProjectMessage.status == OPEN_STATUS,
                    ProjectMessage.message_type.in_(
                        list(CONVERSATIONAL_DECAY_MESSAGE_TYPES)
                    ),
                    ProjectMessage.created_at <= cutoff,
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
            "[REAPER] stale-open decay sweep hit cap=%d open conversational "
            "messages — result truncated; next cycle continues",
            batch_size,
        )
    if not msgs:
        return 0

    question_cutoff = now - timedelta(days=question_decay_days)
    resolved = 0
    pending_broadcasts: list[tuple[str, dict]] = []
    for msg in msgs:
        # Determine the effective decay window and resolution reason by type.
        msg_type = (
            msg.message_type.value
            if hasattr(msg.message_type, "value")
            else str(msg.message_type)
        )
        if msg_type == "QUESTION":
            if (msg.created_at or now) > question_cutoff:
                # Not yet old enough under the longer QUESTION window.
                continue
            resolution_reason = "stale_unanswered"
            effective_decay_days = question_decay_days
        else:
            resolution_reason = "auto-decayed (stale)"
            effective_decay_days = decay_days
        # Never decay an open item that still carries a live linked condition
        # (defensive — open items are normally unlinked, but a resurfaced link
        # can leave the stale forward pointer behind).
        if get_message_link(msg) is not None:
            continue
        # Operator-action-needed NOTEs survive the normal decay window; they
        # eventually decay after a longer threshold (default 60 days) so a
        # forgotten notice does not accumulate indefinitely.
        ctx = msg.context or {}
        if ctx.get("audience") == OPERATOR_ACTION_NEEDED:
            operator_decay_days = int(
                runtime_config.get(
                    "stale_open_operator_action_needed_decay_days", 60
                )
            )
            if (now - msg.created_at).days < operator_decay_days:
                continue
        mark_message_resolved(msg, now, resolution_reason)
        session.add(msg)
        resolved += 1
        pending_broadcasts.append(
            (str(msg.project_id), message_updated_event_payload(msg))
        )
        logger.info(
            "[REAPER] stale-open decay: auto-resolved %s message %s (no activity "
            "for >=%dd, reason=%s)",
            msg_type,
            msg.id,
            effective_decay_days,
            resolution_reason,
        )

    if resolved:
        await session.commit()
        for project_id, payload in pending_broadcasts:
            try:
                await _bcast(project_id, "message_updated", payload)
            except Exception as _bcast_err:  # noqa: BLE001
                logger.warning(
                    "[REAPER] stale-open decay broadcast failed for %s: %s",
                    payload.get("message_id"),
                    _bcast_err,
                )
        logger.info(
            "[REAPER] stale-open decay: auto-resolved %d stale open message(s)",
            resolved,
        )
    return resolved


# ── Re-exports from extracted sub-module ─────────────────────────────────────
#
# These symbols were extracted to app.reaper_bug_message_sweeps. They are
# re-exported here so every existing import site (reaper.py, digest_metrics.py,
# test_reaper.py, etc.) keeps working without any change.
from app.reaper_bug_message_sweeps import (  # noqa: E402
    STALE_OPEN_BUG_CLASS_DECAY_DAYS,
    STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS,
    WakeSweepResult,
    _WAKE_DELIVERY_STALL_CYCLE_THRESHOLD,
    get_wake_delivery_stall_cycles,
    reset_wake_delivery_stall_state,
    sweep_converge_linked_bug_messages,
    sweep_decay_stale_open_bug_messages,
    sweep_decay_stale_unlinked_blocker_messages,
    sweep_deliver_task_wakes,
    sweep_resolve_open_owned_bug_messages,
    sweep_resolve_stale_open_bug_messages,
    sweep_wake_delivery_stall_alerts,
)

async def sweep_closed_handmerge_prs(session) -> int:
    """Resolve the operator-attention record for preserved hand-merge PRs that have since been merged or closed.

    Finds FAILED tasks with ``pr_preserved_for_handmerge=True``, fetches each
    PR's current state from GitHub, and — when the PR is no longer open —
    clears the flag (removing the ``hand_merge_required`` attention item) and
    resolves the associated ProjectMessage created at preservation time.

    Capped at ``_HANDMERGE_SWEEP_BATCH`` tasks per reaper cycle to bound
    GitHub API call volume (each task needs one round-trip).
    """
    import contextlib as _contextlib

    from app.models import ArtifactType, ProjectMessage, Task, TaskStatus
    from app.models import Artifact as _Artifact
    from sqlalchemy.orm.attributes import flag_modified
    from sqlmodel import select as _select

    _HANDMERGE_SWEEP_BATCH = 10

    # Order by updated_at ASC so that tasks whose context was most recently
    # stamped (open-PR checks bump updated_at) rotate to the back, ensuring
    # every preserved task is eventually checked regardless of batch size.
    preserved_tasks = list(
        (
            await session.execute(
                _select(Task)
                .where(
                    Task.archived.is_(False),
                    Task.status == TaskStatus.FAILED,
                    Task.context["pr_preserved_for_handmerge"].as_boolean().is_(True),
                )
                .order_by(Task.updated_at.asc())
                .limit(_HANDMERGE_SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )
    if not preserved_tasks:
        return 0

    try:
        from app.github import get_pull_request as _get_pull_request
        from app.routers.agent import get_github_token_for_task as _get_token
    except ImportError:
        return 0

    # Snapshot the fields we need as plain Python values NOW.
    # session.commit() and session.rollback() both expire every ORM instance
    # (expire_on_commit=True by default), so a lazy-load in a later iteration
    # fires a sync DB read inside the async session → MissingGreenlet, silently
    # aborting the rest of the batch.  Plain locals never expire.
    task_snapshots = [
        (task, task.id, task.project_id, dict(task.context or {}))
        for task in preserved_tasks
    ]

    # Batch-fetch PULL_REQUEST artifacts for tasks that lack handmerge_pr_number
    # in context, to avoid N+1 queries inside the loop.
    _missing_pr_ids = [
        t_id
        for _, t_id, _, ctx in task_snapshots
        if not isinstance(ctx.get("handmerge_pr_number"), int)
    ]
    _artifact_by_task: dict = {}
    if _missing_pr_ids:
        _art_rows = (
            await session.execute(
                _select(_Artifact).where(
                    _Artifact.task_id.in_(_missing_pr_ids),
                    _Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        ).scalars().all()
        for _art in _art_rows:
            _artifact_by_task[_art.task_id] = _art

    resolved = 0
    for task, t_id, t_project_id, ctx in task_snapshots:
        pr_number = ctx.get("handmerge_pr_number")
        if not isinstance(pr_number, int):
            # Fall back to the PULL_REQUEST artifact for tasks preserved before
            # handmerge_pr_number was stored in context.
            artifact = _artifact_by_task.get(t_id)
            if artifact and artifact.pr_url:
                try:
                    pr_number = int(artifact.pr_url.rstrip("/").split("/")[-1])
                except (ValueError, IndexError):
                    pr_number = None
        if not pr_number:
            continue

        try:
            gh_token, owner, repo = await _get_token(t_id, session)
            pr = await _get_pull_request(
                gh_token, owner, repo, pr_number, project_id=str(t_project_id)
            )
        except Exception as _e:  # noqa: BLE001
            logger.warning(
                "[REAPER] handmerge-sweep: failed to check PR #%s for task %s: %s",
                pr_number,
                t_id,
                _e,
            )
            continue

        if (pr.get("state") or "").lower() == "open":
            # Stamp last-checked time and commit so updated_at advances,
            # rotating this task to the back of the ordered batch next cycle.
            ctx["handmerge_last_checked_at"] = _utcnow().isoformat()
            task.context = ctx
            flag_modified(task, "context")
            session.add(task)
            try:
                await session.commit()
            except Exception as _e:  # noqa: BLE001
                logger.warning(
                    "[REAPER] handmerge-sweep: failed to stamp last-checked for task %s: %s",
                    t_id,
                    _e,
                )
                with _contextlib.suppress(Exception):
                    await session.rollback()
            continue

        # PR is no longer open — clear the flag so the attention item disappears.
        resolution = "merged" if pr.get("merged_at") else "closed"
        ctx.pop("pr_preserved_for_handmerge", None)
        ctx["handmerge_pr_resolved"] = True
        ctx["handmerge_pr_resolution"] = resolution
        task.context = ctx
        flag_modified(task, "context")
        session.add(task)

        # Resolve the associated operator-attention ProjectMessage if open.
        ikey = f"handmerge:{t_id}"
        msg = (
            await session.execute(
                _select(ProjectMessage).where(
                    ProjectMessage.project_id == t_project_id,
                    ProjectMessage.idempotency_key == ikey,
                    ProjectMessage.status == "open",
                )
            )
        ).scalar_one_or_none()
        if msg is not None:
            msg.status = "resolved"
            msg.resolution = f"PR #{pr_number} {resolution}"
            msg.resolved_at = _utcnow()
            session.add(msg)

        try:
            await session.commit()
            resolved += 1
            logger.info(
                "[REAPER] handmerge-sweep: cleared PR #%s (%s) for task %s",
                pr_number,
                resolution,
                t_id,
            )
        except Exception as _e:  # noqa: BLE001
            logger.warning(
                "[REAPER] handmerge-sweep: commit failed for task %s: %s",
                t_id,
                _e,
            )
            with _contextlib.suppress(Exception):
                await session.rollback()

    return resolved


# Independent cadence timer for the compute-image GC guard (below). Hourly, on
# its own clock so it can never be gated out by another sweep stamping a shared
# global at the top of the same reaper cycle.
_COMPUTE_IMAGE_GC_GUARD_INTERVAL_SECONDS = 3600
# How far back a *completed* compute job still counts as depending on its image:
# a recent result's provenance/reproducibility needs the digest to stay pullable.
_COMPUTE_IMAGE_PROTECT_WINDOW_SECONDS = 30 * 24 * 3600
_last_compute_image_gc_guard_sweep: datetime | None = None

# Deduped operator-alert title raised when the GC-protection sweep FAILS to apply
# an ``inuse-`` tag. The registry helper is fail-open (returns None on a missing
# gcloud binary / Artifact Registry API error / non-zero exit), so a failed apply
# used to be entirely silent: not logged, not counted, not alerted. A missing
# ``inuse-`` tag means registry time/count retention — which cannot query the DB —
# can reclaim a digest a live/recent compute job (or the project's DB-pinned
# default image) still depends on. This surfaces that as an actionable alert.
_COMPUTE_IMAGE_PROTECT_FAIL_ALERT_TITLE = (
    "Compute-image GC protection tag failed to apply"
)


async def _alert_compute_image_protection_failures(
    session, project_ids, *, apply_failed: int, now: datetime
) -> int:
    """Raise a deduped WARNING alert per project whose ``inuse-`` tag apply failed.

    ``apply_registry_protection_tag`` is fail-open: on a missing gcloud binary, an
    Artifact Registry API error, or a non-zero exit it returns ``None`` rather than
    raising. Previously the sweep just did not increment its applied counter, so
    the failure vanished. Because the registry's retention policy is purely
    time/count-based and cannot see the DB, a digest that never got its ``inuse-``
    protection tag can be GC'd out from under the very job/pin that still references
    it. One open alert per affected project (deduped on an open row with the same
    title) until an operator dismisses it. Returns the number of NEW alerts created.
    """
    from app.models import Alert, AlertSeverity

    project_ids = [p for p in dict.fromkeys(project_ids) if p is not None]
    if not project_ids:
        return 0

    # Single batched existence query (no per-project N+1): which of these projects
    # already carry an OPEN alert with our title.
    already = set(
        (
            await session.execute(
                select(Alert.project_id)
                .where(
                    Alert.project_id.in_(project_ids),
                    Alert.title == _COMPUTE_IMAGE_PROTECT_FAIL_ALERT_TITLE,
                    Alert.dismissed_at.is_(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    created = 0
    for pid in project_ids:
        if pid in already:
            continue
        session.add(
            Alert(
                project_id=pid,
                severity=AlertSeverity.WARNING,
                title=_COMPUTE_IMAGE_PROTECT_FAIL_ALERT_TITLE,
                message=(
                    "The hourly compute-image GC-protection sweep could not apply an "
                    "inuse- registry protection tag to at least one digest this project "
                    "references (fail-open gcloud / Artifact Registry error). Until the "
                    "tag is applied on a later sweep, registry time/count retention "
                    "could reclaim a digest a live/recent compute job — or this "
                    "project's pinned default image — still depends on, breaking "
                    "reproducibility. Check gcloud availability and Artifact Registry "
                    "API health on the orchestrator host."
                )[:1000],
            )
        )
        created += 1
    if created:
        await session.commit()
    return created


def _split_pinned_ref(ref: str) -> tuple[str, str] | None:
    """Split a digest-pinned image ref into ``(full_repo, digest)``.

    ``us-docker.pkg.dev/proj/compute-images/my-project:v2@sha256:abc``
        -> ``("us-docker.pkg.dev/proj/compute-images/my-project", "sha256:abc")``

    The digest is everything after the last ``@``; the repo is what precedes it,
    minus an optional ``:tag`` on the FINAL path segment (a registry host may
    legitimately carry a ``:port``, which must survive). A tag-only or malformed
    ref yields ``None`` — it has no stable digest to protect.
    """
    if not ref or "@" not in ref:
        return None
    base, _, digest = ref.rpartition("@")
    if not base or not digest.startswith("sha256:"):
        return None
    head, sep, last = base.rpartition("/")
    if ":" in last:
        last = last.split(":", 1)[0]
    full_repo = f"{head}{sep}{last}" if sep else last
    if not full_repo or not last:
        return None
    return full_repo, digest


async def _referenced_compute_image_repo_digests(
    session, *, since: datetime
) -> set[tuple[str, str]]:
    """``(full_repo, digest)`` pairs a live/recent compute job still depends on.

    The digest-only sibling of this query lives in
    ``compute_jobs.referenced_compute_image_digests`` and answers "which digests
    matter". That is enough to decide what NOT to prune, but not enough to
    PROTECT: a registry protection tag can only be applied to the repo that
    actually holds the digest, and a job's ``container_image`` is the only place
    that repo is recorded when the image has no ``ProjectComputeImage`` row (the
    documented CI build path, ``scripts/build_compute_image.py``, writes none).

    Reuses the same live/recent status predicate as the digest query — the status
    constants are imported from ``compute_jobs`` so the two cannot drift — and
    differs only in projecting the FULL ref instead of just its digest.
    """
    from sqlalchemy import and_, or_

    from app.compute_jobs import (
        _COMPUTE_JOB_ACTIVE_STATUSES,
        _COMPUTE_JOB_TERMINAL_STATUSES,
    )
    from app.models import ComputeJob

    conditions = [ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES)]
    if since is not None:
        conditions.append(
            and_(
                ComputeJob.status.in_(list(_COMPUTE_JOB_TERMINAL_STATUSES)),
                ComputeJob.completed_at.is_not(None),
                ComputeJob.completed_at >= since,
            )
        )

    rows = (
        await session.execute(
            select(ComputeJob.polling_metadata).where(or_(*conditions))
        )
    ).scalars().all()

    pairs: set[tuple[str, str]] = set()
    for meta in rows:
        if not isinstance(meta, dict):
            continue
        ref = meta.get("container_image")
        if not isinstance(ref, str):
            continue
        parsed = _split_pinned_ref(ref)
        if parsed is not None:
            pairs.add(parsed)
    return pairs


async def sweep_protect_referenced_compute_image_digests(
    session,
    now=None,
    *,
    _apply_tag=None,
    _list_tags=None,
    _delete_tag=None,
    max_images: int = 500,
) -> dict[str, int]:
    """Protect registry digests a live/recent compute job still depends on.

    Registry retention (Terraform ``cleanup_policy`` on the compute-images repo)
    is purely time/count-based and cannot query the DB, so a digest a *running*
    experiment — or a *recent* result's provenance — depends on can be GC'd out
    from under it, breaking reproducibility. This sweep closes that gap: it
    stamps a durable ``inuse-<shorthash>`` protection tag on every referenced
    digest, and a matching KEEP ``cleanup_policy`` (``keep-inuse-tagged`` in
    ``infra/terraform/registry.tf``) shields anything so tagged from deletion.
    Protection tags whose digest is no longer referenced are pruned so ordinary
    time-based retention can reclaim the image on a later pass.

    A digest is "referenced" when it is the ``container_image`` of a live compute
    job, of a job completed within the protection window, OR a usable
    ``ProjectComputeImage`` that resolves as a project's configured current
    default. The last case protects that manifest-selected row before its first
    job lands. Historical rows are deliberately not standing pins: a
    superseded digest stays protected only while a live/recent job still
    references it.

    Cadence-gated to hourly on an independent timer. Returns a summary dict
    (``applied``/``pruned``/``skipped_cadence``). The caller wraps this so it can
    never crash the reaper cycle; the registry helpers fail open individually.
    """
    global _last_compute_image_gc_guard_sweep
    from app.compute_image_build import (
        apply_registry_protection_tag,
        delete_registry_protection_tag,
        list_registry_protection_tags,
        protection_tag_for_digest,
    )
    from app.compute_jobs import referenced_compute_image_digests
    from app.models import Project, ProjectComputeImage

    _now = now or _utcnow()
    if not _is_cadence_due(
        _last_compute_image_gc_guard_sweep,
        _COMPUTE_IMAGE_GC_GUARD_INTERVAL_SECONDS,
        _now,
    ):
        return {"applied": 0, "pruned": 0, "apply_failed": 0, "skipped_cadence": 1}
    _last_compute_image_gc_guard_sweep = _now

    apply_tag = _apply_tag or apply_registry_protection_tag
    list_tags = _list_tags or list_registry_protection_tags
    delete_tag = _delete_tag or delete_registry_protection_tag

    cutoff = _now - timedelta(seconds=_COMPUTE_IMAGE_PROTECT_WINDOW_SECONDS)
    referenced = await referenced_compute_image_digests(session, since=cutoff)

    # Read every image row in bounded pages. ``max_images`` is retained as the
    # page size for callers, not a global cap: silently omitting rows loses both
    # repositories to prune and old rows needed to map a job-referenced digest
    # back to its repository. The stable keyset avoids offset drift as the table
    # grows while keeping each database fetch bounded.
    page_size = max(1, max_images)
    images: list[ProjectComputeImage] = []
    last_updated = None
    last_id = None
    while True:
        page_where = []
        if last_updated is not None and last_id is not None:
            page_where.append(
                or_(
                    ProjectComputeImage.updated_at < last_updated,
                    and_(
                        ProjectComputeImage.updated_at == last_updated,
                        ProjectComputeImage.id < last_id,
                    ),
                )
            )
        page = (
            (
                await session.execute(
                    select(ProjectComputeImage)
                    .where(*page_where)
                    .order_by(
                        ProjectComputeImage.updated_at.desc(),
                        ProjectComputeImage.id.desc(),
                    )
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        if not page:
            break
        images.extend(page)
        last_updated, last_id = page[-1].updated_at, page[-1].id
        if len(page) < page_size:
            break

    # Mirror default-image resolution. The resolver reads the configured manifest
    # and pins its selected build, including an older explicitly tagged row. Do
    # not retain every per-tag candidate: content-hash build tags are unique, so
    # that would turn all historical builds into permanent retention pins.
    latest_by_project: dict[uuid.UUID, set[uuid.UUID]] = {}
    latest_identities_by_project: dict[uuid.UUID, set[tuple[str, str]]] = {}
    built_image_ids_by_ref: dict[tuple[uuid.UUID, str, str], uuid.UUID] = {}
    protection_tags_by_project: dict[uuid.UUID, set[str]] = {}
    current_ids: set[uuid.UUID] = set()
    preserved_protection_tags: set[str] = set()
    for img in images:
        if not img.built or not img.digest:
            continue
        # Images are ordered newest-first. Keep the existing latest-build
        # fallback only for projects with no declared default image at all.
        identity = (img.registry_repo, img.image_name)
        seen = latest_identities_by_project.setdefault(img.project_id, set())
        if identity not in seen:
            seen.add(identity)
            latest_by_project.setdefault(img.project_id, set()).add(img.id)
        built_image_ids_by_ref.setdefault(
            (img.project_id, f"{img.registry_repo}/{img.image_name}", img.digest), img.id
        )
        protection_tags_by_project.setdefault(img.project_id, set()).add(
            protection_tag_for_digest(img.digest)
        )

    project_ids = list(latest_by_project)
    projects_by_id: dict[uuid.UUID, Project] = {}
    if project_ids:
        projects_by_id = {
            project.id: project
            for project in (
                await session.execute(select(Project).where(Project.id.in_(project_ids)))
            ).scalars()
        }

    for project_id, latest_ids in latest_by_project.items():
        project = projects_by_id.get(project_id)
        default_ref = (
            await resolve_project_default_container_image(session, project)
            if project is not None
            else None
        )
        if default_ref is None:
            # The shared resolver intentionally fails open to ``None`` for both
            # no manifest declaration and transient manifest/registry errors.
            # Re-read the declaration to distinguish them: selecting a newer
            # row after an indeterminate resolution could prune the inuse tag
            # for an older explicit manifest tag. In that case retain prior
            # protection tags for this project's rows until a later sweep can
            # resolve the actual default. Do not stamp all historical rows.
            has_declared_default: bool | None
            if project is None:
                has_declared_default = False
            else:
                try:
                    from app.routers.admin_project_tools import _discover_manifest_tools

                    manifest = await _discover_manifest_tools(project)
                    # ``_discover_manifest_tools`` caches fetch/token failures
                    # as a result rather than raising. Those failures are
                    # indeterminate here, not proof that no default exists.
                    # Falling back to the newest row could otherwise prune an
                    # older explicitly configured image's protection tag.
                    if manifest.get("error"):
                        has_declared_default = None
                    else:
                        compute_image = (manifest.get("compute") or {}).get("image") or {}
                        has_declared_default = bool(
                            compute_image.get("name")
                            if isinstance(compute_image, dict)
                            else None
                        )
                except Exception:  # noqa: BLE001 — preserve prior tags on discovery failure
                    has_declared_default = None
            if has_declared_default is False:
                # Only an affirmative no-image result uses the historical
                # newest-build fallback.
                current_ids.update(latest_ids)
            else:
                preserved_protection_tags.update(
                    protection_tags_by_project.get(project_id, set())
                )
            continue
        selected = _split_pinned_ref(default_ref)
        if selected is None:
            # A configured image without a matching durable build is not this
            # project's default row, so it must not pin an unrelated history.
            continue
        full_repo, digest = selected
        image_id = built_image_ids_by_ref.get((project_id, full_repo, digest))
        if image_id is not None:
            current_ids.add(image_id)

    for img in images:
        if img.id in current_ids and img.digest:
            referenced.add(img.digest)

    protected_tags = {protection_tag_for_digest(d) for d in referenced}

    # (repo, digest) pairs to stamp. A digest can only be tagged in the repo that
    # actually holds it, so protection needs the REPO, not just the digest.
    #
    # Two independent sources — and the second one is why this sweep used to lie:
    #
    #   * ProjectComputeImage rows — the repo comes from the row's own
    #     registry_repo/image_name columns.
    #   * LIVE/RECENT COMPUTE JOBS — the repo is parsed out of the job's
    #     container_image ref. Protection used to be applied by iterating
    #     ProjectComputeImage rows ONLY, so a digest referenced by a running job
    #     but with NO ProjectComputeImage row (e.g. an image baked through the
    #     documented CI path, scripts/build_compute_image.py, which writes no DB
    #     row) was collected into `referenced`, counted as protected when pruning
    #     — and then never actually stamped. It was GC-able out from under the
    #     very job that was still pulling it, which is precisely the failure this
    #     sweep's docstring promises to prevent.
    targets: set[tuple[str, str]] = set()
    # Map each protection target back to the project that pins it, so a FAILED
    # apply can be surfaced as an actionable alert on the affected project. Only
    # ProjectComputeImage-derived targets carry a project_id here; job-only refs
    # (no build row) still count toward apply_failed and log, but have no single
    # owning project to alert.
    target_project: dict[tuple[str, str], uuid.UUID] = {}

    for img in images:
        if img.digest and (img.id in current_ids or img.digest in referenced):
            key = (f"{img.registry_repo}/{img.image_name}", img.digest)
            targets.add(key)
            target_project.setdefault(key, img.project_id)

    job_refs = await _referenced_compute_image_repo_digests(session, since=cutoff)
    for full_repo, digest in job_refs:
        if digest in referenced:
            targets.add((full_repo, digest))

    applied = 0
    apply_failed = 0
    failed_projects: list[uuid.UUID] = []
    pruned = 0
    # Every repo we know about gets its stale protection tags pruned — including
    # repos we only learned about from a job's container_image.
    repos: set[str] = {f"{img.registry_repo}/{img.image_name}" for img in images}
    repos.update(repo for repo, _digest in job_refs)

    for full_repo, digest in sorted(targets):
        # Run blocking subprocess call off the event-loop thread so the
        # sweep cannot stall API responsiveness or heartbeat processing.
        if await asyncio.to_thread(apply_tag, full_repo, digest):
            applied += 1
        else:
            # apply_registry_protection_tag is fail-open (None on missing gcloud /
            # AR API error / non-zero exit). This used to vanish silently — no
            # warning, no count, no alert — while the referenced digest lost its
            # only GC protection. Count it, and remember the owning project so the
            # failure can be alerted below.
            apply_failed += 1
            owner = target_project.get((full_repo, digest))
            if owner is not None:
                failed_projects.append(owner)

    for full_repo in repos:
        try:
            existing = await asyncio.to_thread(list_tags, full_repo)
        except Exception:  # noqa: BLE001 — registry helpers fail open
            continue
        for tag in existing:
            if tag not in (protected_tags | preserved_protection_tags) and await asyncio.to_thread(
                delete_tag, full_repo, tag
            ):
                pruned += 1

    # Surface fail-open apply failures — the whole point is that they are no longer
    # silent. A missing inuse- tag means registry retention can reclaim a digest a
    # live/recent job or DB-pinned default still references.
    if apply_failed:
        logger.warning(
            "sweep_protect_referenced_compute_image_digests: %d of %d protection-tag "
            "apply(s) FAILED fail-open (no inuse- tag written) — referenced compute-"
            "image digest(s) may be GC'd out from under a live/recent job or a DB-"
            "pinned project default. applied=%d",
            apply_failed,
            len(targets),
            applied,
        )
        try:
            await _alert_compute_image_protection_failures(
                session, failed_projects, apply_failed=apply_failed, now=_now
            )
        except Exception:  # noqa: BLE001 — alerting must never crash the sweep
            logger.exception(
                "sweep_protect_referenced_compute_image_digests: failed to raise "
                "protection-failure alert(s)"
            )
            with contextlib.suppress(Exception):
                await session.rollback()

    return {
        "applied": applied,
        "pruned": pruned,
        "apply_failed": apply_failed,
        "skipped_cadence": 0,
    }


# ── Auto-rebuild project compute images on manifest change ────────────────────
#
# A project-baked compute image (the images-first dependency substrate) only ever
# rebuilds today via the manual OWNER-only admin endpoint. Nothing rebuilds it
# when the project's declared dependency manifest changes, so the baked image
# silently drifts from the code's real deps until an operator remembers to click
# rebuild. This sweep closes that gap: hourly, it drives the reusable
# ``compute_backends.refresh_project_compute_image`` driver per connected-repo
# project, which CHEAPLY (no clone) recomputes the current dependency hash and
# rebuilds ONLY when the registry tag is absent (a genuinely changed manifest).
#
# Independent hourly cadence timer so a shared global stamped earlier in the same
# reaper cycle can never gate it out. Bounded work: honors the manifest cache,
# skips repo-less projects, respects the process-wide build concurrency guard
# (never stampedes the host with concurrent docker builds), and builds at most a
# small number of projects per cycle so one pass cannot pin the host for an hour.
_COMPUTE_IMAGE_REFRESH_INTERVAL_SECONDS = 3600
_COMPUTE_IMAGE_REFRESH_MAX_PROJECTS = 25
_COMPUTE_IMAGE_REFRESH_MAX_BUILDS_PER_CYCLE = 3
_last_compute_image_refresh_sweep: datetime | None = None


async def sweep_refresh_project_compute_images(
    session,
    now=None,
    *,
    _refresh_fn=None,
    _build_semaphore=None,
    max_projects: int = _COMPUTE_IMAGE_REFRESH_MAX_PROJECTS,
    max_builds_per_cycle: int = _COMPUTE_IMAGE_REFRESH_MAX_BUILDS_PER_CYCLE,
    progress_cb=None,
) -> dict[str, int]:
    """Rebuild project baked compute images whose dependency manifest changed.

    Iterates projects with a connected GitHub repo (bounded) and calls the
    best-effort per-project driver
    :func:`app.compute_backends.refresh_project_compute_image`. The driver
    recomputes the current dependency hash without a clone and rebuilds ONLY when
    the manifest genuinely changed (the registry tag for the new hash is absent);
    an unchanged manifest is a cheap registry-lookup no-op.

    Bounded so one cycle cannot pin the orchestrator host for an hour: at most
    ``max_builds_per_cycle`` rebuilds are triggered (further changed projects are
    ``deferred`` to the next hourly pass), and the driver is handed the same
    process-wide build concurrency guard the admin endpoint uses so a build
    already in flight defers rather than stampeding.

    When *progress_cb* is provided, each build uses heartbeat-loop progress
    reporting so the reaper's lifecycle watchdog does not cancel the cycle during
    a multi-minute baked-image build.

    Cadence-gated to hourly on an independent timer. Returns a summary dict of
    per-status counts (``rebuilt``/``unchanged``/``no_config``/``deferred``/
    ``errors``/``projects``/``skipped_cadence``). A per-project failure is
    isolated — it never aborts the sweep. The reaper wraps this so a raised error
    can never crash the cycle.
    """
    global _last_compute_image_refresh_sweep
    from app.models import Project

    _now = now or _utcnow()
    if not _is_cadence_due(
        _last_compute_image_refresh_sweep,
        _COMPUTE_IMAGE_REFRESH_INTERVAL_SECONDS,
        _now,
    ):
        return {
            "projects": 0,
            "rebuilt": 0,
            "dispatched": 0,
            "unchanged": 0,
            "no_config": 0,
            "deferred": 0,
            "errors": 0,
            "skipped_cadence": 1,
        }
    _last_compute_image_refresh_sweep = _now

    if _refresh_fn is None:
        from app.compute_backends import refresh_project_compute_image

        refresh_fn = refresh_project_compute_image
    else:
        refresh_fn = _refresh_fn

    if _build_semaphore is None:
        try:
            from app.routers.admin_project_tools import (
                _compute_image_build_semaphore,
            )

            build_semaphore = _compute_image_build_semaphore
        except Exception:  # noqa: BLE001 — guard defensively; run unbounded-by-sem
            build_semaphore = None
    else:
        build_semaphore = _build_semaphore

    _prefetched_projects = (
        (
            await session.execute(
                select(Project)
                .where(
                    Project.github_owner.is_not(None),
                    Project.github_repo.is_not(None),
                )
                .limit(max_projects)
            )
        )
        .scalars()
        .all()
    )
    # Extract IDs as plain scalars so they survive a session rollback below.
    project_ids = [p.id for p in _prefetched_projects]

    summary = {
        "projects": 0,
        "rebuilt": 0,
        "dispatched": 0,
        "unchanged": 0,
        "no_config": 0,
        "deferred": 0,
        "errors": 0,
        "skipped_cadence": 0,
    }
    builds_done = 0
    for project_id in project_ids:
        summary["projects"] += 1
        try:
            # A prior project's failed driver may roll back the shared session,
            # expiring ORM instances. session.get() returns the already-loaded
            # project from the identity map (no extra round-trip) unless a
            # rollback expired it, in which case it re-fetches — preserving
            # the same isolation guarantee as the prior IDs-only approach.
            project = await session.get(Project, project_id)
            if project is None:
                summary["errors"] += 1
                continue
            result = await refresh_fn(
                session,
                project,
                allow_build=(builds_done < max_builds_per_cycle),
                build_semaphore=build_semaphore,
                progress_cb=progress_cb,
            )
        except Exception:  # noqa: BLE001 — driver is best-effort; isolate anyway
            logger.warning(
                "[REAPER] compute-image auto-refresh raised for project %s",
                project_id,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()
            summary["errors"] += 1
            continue

        status = (result or {}).get("status")
        if status == "rebuilt":
            summary["rebuilt"] += 1
            builds_done += 1
            try:
                from app.alert_dispatch import _maybe_create_compute_image_drift_alert

                await _maybe_create_compute_image_drift_alert(
                    session,
                    project_id,
                    dep_hash=(result or {}).get("dep_hash"),
                    pinned_ref=(result or {}).get("pinned_ref"),
                )
            except Exception:  # noqa: BLE001 — alert is best-effort
                logger.warning(
                    "[REAPER] compute-image drift alert failed for project %s",
                    project_id,
                    exc_info=True,
                )
        elif status == "dispatched":
            # Off-orchestrator async build kicked off; it counts against the
            # per-cycle build budget. The reaper poll sweep records the pin (and
            # any drift alert fires once the pinned_ref is known), so nothing to
            # record here.
            summary["dispatched"] += 1
            builds_done += 1
        elif status == "unchanged":
            summary["unchanged"] += 1
        elif status == "no_config":
            summary["no_config"] += 1
        elif status == "deferred":
            summary["deferred"] += 1
        else:  # "error" or an unrecognized status
            summary["errors"] += 1

    if (
        summary["rebuilt"]
        or summary["dispatched"]
        or summary["deferred"]
        or summary["errors"]
    ):
        logger.info(
            "[REAPER] compute-image auto-refresh: rebuilt=%d dispatched=%d "
            "unchanged=%d deferred=%d no_config=%d errors=%d (of %d project(s))",
            summary["rebuilt"],
            summary["dispatched"],
            summary["unchanged"],
            summary["deferred"],
            summary["no_config"],
            summary["errors"],
            summary["projects"],
        )
    return summary


# ── Off-orchestrator image-build poll monitor (DARK — not wired into the loop) ──
#
# Sibling of the compute-job monitor: it drives BUILDING ProjectComputeImageBuild
# rows to a terminal state by polling the throwaway builder VM's serial marker /
# durable terminal.json, then records the pinned image (through the same
# digest-preservation guard the inline path uses) and reaps the VM. Past its
# deadline a build's VM is deleted and the row is marked TIMED_OUT.
#
# This sweep is NOT registered in the reaper cycle and NOT called by the trigger
# endpoint — the whole builder lane ships dark, selectable by config. It exists
# so the machinery is testable and ready to wire up once proven.
_IMAGE_BUILD_POLL_MAX_ROWS = 25


async def sweep_poll_image_builds(
    session,
    now=None,
    *,
    _backend=None,
    _record_fn=None,
    _preserve_fn=None,
    _reap_fn=None,
    max_builds: int = _IMAGE_BUILD_POLL_MAX_ROWS,
) -> dict[str, int]:
    """Poll BUILDING off-orchestrator compute-image builds to a terminal state.

    For each ``BUILDING`` :class:`ProjectComputeImageBuild` row (bounded to
    ``max_builds``):

    * **Past deadline** — delete the builder VM and mark the row ``TIMED_OUT``
      (a build that ran past its wall-clock ceiling never completes on its own).
    * **SUCCEEDED** — reconstruct a ``BuildResult`` from the tool's parsed
      ``RESULT_JSON``, run it through the digest-preservation guard, UPSERT the
      ``ProjectComputeImage`` pin, persist ``result_json``, mark ``SUCCEEDED``,
      and reap the VM.
    * **FAILED** — persist the reason, mark ``FAILED``, and reap the VM.
    * **still BUILDING** — leave the row untouched for the next pass.

    All VM/serial/registry calls go through the injected ``_backend`` /
    ``_record_fn`` / ``_preserve_fn`` / ``_reap_fn`` seams so the state machine is
    testable with no cloud I/O. A per-row failure is isolated — it never aborts
    the sweep. Returns a summary dict of per-outcome counts. The reaper (once this
    is wired) wraps it so a raised error can never crash the cycle.
    """
    from app import compute_backends as _cb

    _now = now or _utcnow()
    backend = _backend if _backend is not None else _cb.get_image_builder_backend()
    record_fn = _record_fn if _record_fn is not None else _cb.record_project_compute_image_build
    preserve_fn = (
        _preserve_fn if _preserve_fn is not None else _cb._preserve_existing_digest_pin_cb
    )

    async def _default_reap(build: ProjectComputeImageBuild) -> bool:
        return await backend.abort(build, reason="reap_after_terminal")

    reap_fn = _reap_fn if _reap_fn is not None else _default_reap

    rows = (
        (
            await session.execute(
                select(ProjectComputeImageBuild)
                .where(
                    ProjectComputeImageBuild.status == ComputeImageBuildStatus.BUILDING
                )
                .order_by(ProjectComputeImageBuild.created_at)
                .limit(max_builds)
            )
        )
        .scalars()
        .all()
    )

    summary = {
        "polled": 0,
        "succeeded": 0,
        "failed": 0,
        "timed_out": 0,
        "still_building": 0,
        "errors": 0,
    }
    for build in rows:
        summary["polled"] += 1
        try:
            # Deadline first: a build past its wall-clock ceiling is reaped and
            # marked TIMED_OUT without trusting a (possibly never-coming) marker.
            if build.deadline_at is not None and _now >= build.deadline_at:
                await reap_fn(build)
                build.status = ComputeImageBuildStatus.TIMED_OUT
                build.error_reason = "build exceeded its wall-clock deadline"
                build.updated_at = _now
                session.add(build)
                await session.commit()
                summary["timed_out"] += 1
                continue

            status = await backend.poll(build)
            if status.status == ComputeImageBuildStatus.BUILDING:
                summary["still_building"] += 1
                continue

            if status.status == ComputeImageBuildStatus.SUCCEEDED:
                result_payload = status.result or {}
                build_result = _cb.build_result_from_serial_payload(
                    result_payload, dep_hash=build.dep_hash
                )
                # Never let a digest-less result NULL an already-stored pin.
                build_result = await preserve_fn(
                    session, build.project_id, build_result
                )
                await record_fn(
                    session,
                    build.project_id,
                    build.registry_repo,
                    build.image_name,
                    build_result,
                )
                build.status = ComputeImageBuildStatus.SUCCEEDED
                build.result_json = build_result.to_json()
                build.error_reason = None
                build.updated_at = _now
                session.add(build)
                await session.commit()
                await reap_fn(build)
                summary["succeeded"] += 1
                continue

            # FAILED (or any non-BUILDING/SUCCEEDED terminal state).
            build.status = ComputeImageBuildStatus.FAILED
            build.error_reason = status.reason or "image build failed"
            build.updated_at = _now
            session.add(build)
            await session.commit()
            await reap_fn(build)
            summary["failed"] += 1
        except Exception:  # noqa: BLE001 — isolate a per-row failure
            logger.warning(
                "[REAPER] image-build poll raised for build %s",
                getattr(build, "id", "?"),
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()
            summary["errors"] += 1
            continue

    if summary["succeeded"] or summary["failed"] or summary["timed_out"] or summary["errors"]:
        logger.info(
            "[REAPER] image-build poll: succeeded=%d failed=%d timed_out=%d "
            "still_building=%d errors=%d (of %d row(s))",
            summary["succeeded"],
            summary["failed"],
            summary["timed_out"],
            summary["still_building"],
            summary["errors"],
            summary["polled"],
        )
    return summary


async def sweep_silent_runner_compute_jobs(session, now=None) -> int:
    """Step 22f: Reap silent runner-subprocess and GCP compute jobs.

    GCP jobs delete their VM before finalization; runner jobs only finalize.
    Best-effort per job so one failure does not block the next.
    Returns the number of jobs finalized.
    """
    _now = now or _utcnow()
    stale_cutoff = _now - timedelta(seconds=RUNNER_SILENT_TIMEOUT_SECONDS)

    stmt = (
        select(ComputeJob)
        .where(
            ComputeJob.status == ComputeJobStatus.RUNNING,
            ComputeJob.backend.in_((RUNNER_SUBPROCESS_BACKEND_NAME, GCP_BACKEND_NAME)),
            (
                (ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME)
                & (
                    (ComputeJob.last_heartbeat_at.is_(None)
                     & (ComputeJob.started_at < stale_cutoff))
                    | (ComputeJob.last_heartbeat_at < stale_cutoff)
                )
            )
            | (
                (ComputeJob.backend == GCP_BACKEND_NAME)
                & (ComputeJob.last_heartbeat_at < stale_cutoff)
            ),
        )
        .limit(50)
    )
    rows = (await session.execute(stmt)).scalars().all()

    finalized = 0
    for job in rows:
        try:
            ok = await finalize_silent_compute_job(session, job, now=_now)
            if ok:
                finalized += 1
        except Exception:
            logger.exception(
                f"[REAPER] Failed to finalize silent compute job {job.id}"
            )
            await session.rollback()
    if finalized:
        logger.info(
            f"[REAPER] Finalized {finalized} silent compute job(s)"
        )
    return finalized


async def sweep_stranded_runner_subprocess_compute_jobs(
    session,
    now: datetime,
    *,
    session_factory=AsyncSessionLocal,
) -> int:
    """Finalize claimed runner-subprocess jobs after a runner dies.

    A runner's last report is stored only in ``polling_metadata``.  If the
    runner crashes or becomes partitioned, the normal monitor therefore sees
    STARTING/RUNNING forever.  ``ran_on_runner_id`` is the authoritative claim
    record; never use the dispatch-only ``runner_target_id`` pin here.
    """
    liveness_cutoff = now - timedelta(seconds=_RUNNER_LIVENESS_SECONDS)
    candidates = (
        await session.execute(
            select(ComputeJob.id, ComputeJob.ran_on_runner_id)
            .join(LocalRunner, LocalRunner.id == ComputeJob.ran_on_runner_id)
            .where(
                ComputeJob.backend == RUNNER_SUBPROCESS_BACKEND_NAME,
                ComputeJob.status.in_(
                    [ComputeJobStatus.STARTING, ComputeJobStatus.RUNNING]
                ),
                or_(
                    LocalRunner.last_seen_at.is_(None),
                    LocalRunner.last_seen_at < liveness_cutoff,
                ),
            )
        )
    ).all()

    finalized = 0
    for job_id, runner_id in candidates:
        if runner_id is not None and await finalize_runner_subprocess_job_for_dead_runner(
            session_factory,
            job_id,
            runner_id=runner_id,
            liveness_cutoff=liveness_cutoff,
            now=now,
        ):
            finalized += 1
    if finalized:
        logger.warning(
            "[REAPER] finalized %d stranded runner-subprocess compute job(s)",
            finalized,
        )
    return finalized


async def sweep_retire_viewable_artifacts(
    session,
    *,
    retention_days: int = VIEWABLE_ARTIFACT_RETENTION_DAYS,
    batch_size: int = 100,
) -> int:
    """Step 23: Delete viewable GCS objects and their ComputeJobFile rows for
    completed jobs older than ``retention_days`` (default
    ``storage.VIEWABLE_ARTIFACT_RETENTION_DAYS`` = 180).

    Viewable objects at ``compute-jobs/<uuid>/artifacts/viewable/`` are
    intentionally excluded from the GCS bucket lifecycle rules (which only
    cover ``.tar.gz`` bundles and ``tasks/`` payloads).  This sweep is their
    sole reaper: once the owning ComputeJob has been terminal for the full
    retention window the object is deleted best-effort and the row removed.

    At most ``batch_size`` files are processed per call to bound GCS call
    volume.  Returns the number of ComputeJobFile rows deleted.
    """
    cutoff = _utcnow() - timedelta(days=max(1, int(retention_days)))

    old_job_ids = (
        select(ComputeJob.id)
        .where(
            ComputeJob.completed_at.is_not(None),
            ComputeJob.completed_at < cutoff,
        )
        .scalar_subquery()
    )

    rows = (
        await session.execute(
            select(ComputeJobFile)
            .where(ComputeJobFile.compute_job_id.in_(old_job_ids))
            .order_by(ComputeJobFile.id.asc())
            .limit(max(1, min(int(batch_size), 500)))
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()

    if not rows:
        return 0

    deleted_flags = await asyncio.gather(
        *(delete_storage_uri_best_effort(row.storage_uri) for row in rows)
    )

    removed = 0
    for row, ok in zip(rows, deleted_flags, strict=True):
        if ok:
            await session.delete(row)
            removed += 1
    if removed:
        await session.commit()
        logger.info(
            "[REAPER] Retired %d viewable artifact file(s) older than %d days",
            removed,
            retention_days,
        )
    else:
        await session.rollback()
    return removed


__all__ = [
    # Re-exported from app.blocked_task_unblock_sweeps (constants)
    "_BLOCKED_ESCALATE_AFTER",
    "_BLOCKED_ESCALATED_PARK",
    "_BLOCKED_OPTIMISTIC_RETRY_CAP",
    "_BLOCKED_PARKED_REEVAL_CAP",
    "GIVE_UP_FOUNDATION_DIED",
    "GIVE_UP_NEVER_SATISFIABLE",
    "MISSING_FOUNDATION_MARKER_KEY",
    "RESUME_MARKER_CONTEXT_KEY",
    "UNRESUMABLE_CANCEL_CONTEXT_KEY",
    "_WAKE_DELIVERY_STALL_CYCLE_THRESHOLD",
    "WakeSweepResult",
    # Re-exported from app.blocked_task_unblock_sweeps (sweep entry points)
    "sweep_unblock_blocked_tasks",
    "_event_driven_unblock_project",
    "event_driven_unblock_dependents",
    "event_driven_unblock_on_knowledge",
    "event_driven_unblock_on_capability_proof",
    # Re-exported from app.blocked_task_unblock_sweeps (helpers)
    "_apply_needs_disposition",
    "_blocked_precondition_state",
    "_cancel_blocker_as_dead",
    "_classify_blocker_needs",
    "_clear_blocked_fields",
    "_fetch_active_candidates",
    "_fetch_completed_candidates",
    "_fetch_dead_candidates",
    "_fetch_knowledge_substance_index",
    "_fetch_working_capability_names",
    "_next_recheck_delay",
    "_partition_completed_needs_base_safe",
    "_persist_residual_needs",
    "_wire_edges",
    "get_wake_delivery_stall_cycles",
    "reset_wake_delivery_stall_state",
    "sweep_converge_linked_bug_messages",
    "sweep_decay_stale_open_bug_messages",
    "sweep_decay_stale_unlinked_blocker_messages",
    "STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS",
    "sweep_deliver_task_wakes",
    "sweep_resolve_open_owned_bug_messages",
    "sweep_resolve_stale_open_bug_messages",
    "sweep_resolve_linked_messages",
    "sweep_decay_stale_open_messages",
    "sweep_decay_stale_pending_confirmations",
    "STALE_OPEN_DECAY_DAYS",
    "STALE_OPEN_BUG_CLASS_DECAY_DAYS",
    "STALE_QUESTION_DECAY_DAYS",
    "sweep_wake_delivery_stall_alerts",
    "sweep_closed_handmerge_prs",
    "sweep_protect_referenced_compute_image_digests",
    "sweep_refresh_project_compute_images",
    "sweep_silent_runner_compute_jobs",
    "sweep_stranded_runner_subprocess_compute_jobs",
    "VIEWABLE_ARTIFACT_RETENTION_DAYS",
    "sweep_retire_viewable_artifacts",
]
