"""Orchestrator worker - background loop that provisions tasks and monitors health.

Extracted from app.main to reduce module size. The worker polls for QUEUED tasks,
checks dependencies, handles file conflicts, provisions VMs, and monitors
EVALUATING tasks.

The orchestrator loop runs every ~10 seconds and:

1. **Task Polling**: Fetches QUEUED tasks in priority order (high → medium → low)
2. **Dependency Checking**: Validates file conflicts, rollup conflicts, and migration
   serialization before provisioning
3. **VM Provisioning**: Creates GCP VMs for non-dependent tasks (subject to
   MAX_CONCURRENT_VMS and MAX_PROVISION_SLOTS limits)
4. **Autonomous Research**: Spawns new research/plan tasks from strategic directions
5. **EVALUATING Monitoring**: Watches tasks waiting on CI/review and triggers PR rollups
   when ready
6. **Circuit Breaker**: Halts new provisioning if a project exceeds failure threshold

Key Functions:
- orchestrator_worker() - Main polling loop (async, called by lifecycle)
- _check_idle_research() - Spawns autonomous research tasks if pipeline is idle
- provision_and_handle() - Attempts to provision a task, with retry and fallback
  logic (extracted to app.orchestrator_provision; re-exported here)
- _monitor_evaluating_tasks() - Checks CI completion and review status for EVALUATING tasks

The orchestrator is the single source of truth for when work moves from QUEUED →
PROVISIONING. All other state transitions (PROVISIONING → IN_PROGRESS, IN_PROGRESS →
COMPLETED/FAILED, etc.) are driven by agent reports or task lifecycle handlers.
"""

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

from sqlalchemy import func as _func, inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.compute import (
    VM_PROVISIONING_MODEL,
    _capture_serial_for_vm,
    any_zone_available,
    get_vm_zone,
    get_zone_health_summary,
    # Kept as the ``app.orchestrator.<name>`` test-patch surface for
    # ``provision_and_handle`` (in ``app.orchestrator_provision``), which reaches
    # these through ``import app.orchestrator as _orch`` at call time.
    prepare_and_store_agent_secret,  # noqa: F401
    provision_agent_vm,  # noqa: F401
    zone_recovery_wait_seconds,
)
from app.compute_cloud_run import (
    launch_agent_cloud_run_job,  # noqa: F401 — app.orchestrator patch surface (see above)
)
from app.task_capabilities import CLOUD_RUN_CANDIDATE_TASK_TYPES
from app.database import AsyncSessionLocal
from app.models import (
    DeploymentState,
    Project,
    Task,
    TaskStatus,
    bank_task_current_attempt,
)
from app.resource_envelope import (
    get_project_recent_spend_usd,
    resolve_project_pause_on_ceiling_exceeded,
    resolve_project_spend_ceiling,
    resolve_project_spend_window_days,
    resolve_project_vm_cap,
)
from app.orchestrator_filters import (
    filter_file_conflicts,
    filter_rollup_conflicts,
    filter_held_directions,
    filter_pipeline_duplicates,
    reserve_migration_revisions,
    filter_scope_empty,
    cap_pipeline_slots,
    reserve_slots_for_types,
    apply_project_fairness,
    apply_project_concurrency_cap,
    reorder_by_completion_priority,
    _project_fairness_weight,
    collect_dep_uuids,
)
from app.stall_alert_diagnostics import (
    _compute_all_accounts_blocked_action,
    _compute_retry_after_escalation_action,
    _missing_runtime_credentials_stall_reason,
)
from app.state_persistence import (
    _persist_circuit_breakers,  # noqa: F401 — re-exported; callers import via app.orchestrator
    _persist_zone_health,
    _persist_self_healing_state_bundle,
)
from app.storage import upload_json_payload  # noqa: F401 — app.orchestrator patch surface
from app.monthly_budget_alerts import check_monthly_budget  # noqa: F401 — re-export for tests patching app.orchestrator.check_monthly_budget
import app.shared_state as _shared_state
from app.shared_state import (
    runtime_config as _runtime_config,
    sync_runtime_config_from_state as _sync_runtime_config_from_state,
)
from app.task_locking import task_lock
from app.task_blocked import strip_fresh_attempt_context
from app.time_utils import (
    _is_cadence_due,
    _utcnow,
    exponential_backoff,
    rate_limit_retry_minutes,
    set_retry_after,
)

logger = logging.getLogger("blueprint")


async def _reconcile_active_per_account(session: AsyncSession) -> Dict[str, int]:
    """Count active (PROVISIONING/IN_PROGRESS) tasks per LLM account id.

    Drives the Max-Plan reconcile each ~30s dispatch cycle. D2 over-fetch fix
    (d-57436105): projects ONLY the promoted, indexed ``Task.llm_account_id``
    scalar column instead of TOAST-fetching every active task's full multi-MB
    ``Task.context`` JSONB blob each cycle (the previous query was
    ``select(Task.context)``). The backfill migration COALESCEd the legacy
    ``context->>'max_plan_account_id'`` fallback into the column and every
    dispatch/requeue write path keeps the column in sync, so the column is
    authoritative for active tasks — no context read is needed.

    SCALAR COLUMN PROJECTION (returns plain rows) — deliberately NOT
    ``select(Task)`` with ``load_only``: touching a deferred attribute on a
    returned Task in this async context risks a MissingGreenlet crash of the
    dispatch cycle (see reference_load_only_event_listeners.md /
    reference_load_only_lazy_load_trap.md).
    """
    rows = (
        await session.execute(
            select(Task.llm_account_id, _func.count(Task.id))
            .where(
                Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]),
                Task.llm_account_id.is_not(None),
            )
            .group_by(Task.llm_account_id)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _lock_task_for_mutation(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    allowed_statuses: tuple[TaskStatus, ...] | None = None,
    reason: str = "task mutation",
    expected_snapshot: dict | None = None,
    snapshot_fn=None,
) -> Task | None:
    """Lock a task before a hot-path mutation and rollback on skipped rows.

    Commit-release-relock pattern (Phase B3) is used to break lock windows
    around long-running GitHub I/O. When ``expected_snapshot`` +
    ``snapshot_fn`` are passed, the re-acquired row is compared against the
    pre-release content snapshot — if a peer mutated it during the unlocked
    window we defer (return None) so the caller can retry on the next
    sweep or surface a conflict.

    R5-W2-A: this content-based snapshot replaced an earlier
    ``expected_updated_at`` timestamp validator that false-tripped on every
    overlapping heartbeat (heartbeats with ``onupdate=_utcnow`` advance
    ``updated_at`` every ~30s). The snapshot pair lets the caller capture
    the fields it actually cares about (e.g. status + review_loop
    pr_number / ci_passed / review_approved / iteration) and defer only
    when one of those has actually changed. R6-W3-B: dropped the unused
    ``expected_updated_at`` parameter — every CI-polling caller migrated
    to the snapshot pair, leaving the timestamp branch dead.
    """
    async with task_lock(session, task_id) as task:
        if task is None:
            logger.info(
                "[ORCHESTRATOR] %s for task %s deferred: task row locked",
                reason,
                task_id,
            )
            # SKIP LOCKED returned no row — no row lock was acquired by this
            # call, so there's nothing to release. Calling session.rollback()
            # here would silently wipe any uncommitted dirty state from
            # earlier iterations of the caller's loop (observed 2026-05-07:
            # stale-PROVISIONING recovery loop kept logging "marking FAILED"
            # 17 times because each iteration's FAILED commit was reverted
            # by the next iteration's rollback-on-skipped-lock).
            return None
        if allowed_statuses is not None and task.status not in allowed_statuses:
            logger.info(
                "[ORCHESTRATOR] %s for task %s skipped: status=%s",
                reason,
                task_id,
                task.status,
            )
            # We DID acquire a row lock and the status check failed. Release
            # by committing rather than rolling back: this terminates the
            # transaction (releasing the lock as a side effect) while
            # preserving any prior dirty state from earlier iterations of
            # the caller's loop. The current call's only mutation effect is
            # the SELECT FOR UPDATE itself, which has no row-data side
            # effect — committing it is a no-op for the locked row.
            await session.commit()
            return None
        if expected_snapshot is not None and snapshot_fn is not None:
            current_snapshot = snapshot_fn(task)
            if current_snapshot != expected_snapshot:
                logger.info(
                    "[ORCHESTRATOR] %s for task %s deferred: row mutated during unlocked window "
                    "(expected snapshot=%s, found %s)",
                    reason,
                    task_id,
                    expected_snapshot,
                    current_snapshot,
                )
                await session.commit()
                return None
        return task


async def _lock_dispatchable_task_for_mutation(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> Task | None:
    """Lock a QUEUED task only if its project is still unpaused and unarchived.

    The candidate scan and queued-task refresh both prefilter paused/archived
    projects, but an operator can pause or archive a project in the narrow
    window before the final dispatch claim. Re-check both flags in the same
    ``SELECT .. FOR UPDATE SKIP LOCKED`` that acquires the task row so the
    task cannot leave QUEUED after pause/archive lands.
    """
    stmt = (
        select(Task)
        .join(Project, Task.project_id == Project.id)
        .where(
            Task.id == task_id,
            Task.status == TaskStatus.QUEUED,
            Project.paused.is_(False),
            Project.not_archived(),
        )
        # of=Task: lock ONLY the Task row. The Project join is a pause/archive
        # filter, never mutated here — a plain FOR UPDATE would also lock the
        # Project row, which deadlocks against the FK child-inserts that take
        # FOR KEY SHARE on the same Project (project_lock FK deadlock) and lets
        # skip_locked silently drop a claimable task whenever a sibling claim in
        # the same project holds the shared Project row. Mirrors
        # agent_warm_continuity.py:750.
        .with_for_update(skip_locked=True, of=Task)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(stmt)
    task = result.scalar_one_or_none()
    if task is None:
        logger.info(
            "[ORCHESTRATOR] dispatch for task %s deferred: row locked, status changed, or project paused",
            task_id,
        )
    return task


async def _lock_provisioning_task_for_vm(
    session: AsyncSession,
    task_id: uuid.UUID,
    vm_id: str,
) -> Task | None:
    """Lock a PROVISIONING task only if it still owns the provisioning VM.

    Provisioning completes on a background coroutine. By the time that coroutine
    reports success/failure, the reaper may already have requeued the task with
    a new ``vm_instance_id``. Mutating without this fresh locked check can
    overwrite the reaper's QUEUED state with stale provision-failure handling.
    """
    task = await _lock_task_for_mutation(
        session,
        task_id,
        allowed_statuses=None,
        reason="provision result",
    )
    if task is None:
        return None
    if task.status != TaskStatus.PROVISIONING or task.vm_instance_id != vm_id:
        logger.info(
            "[ORCHESTRATOR] Ignoring stale provision result for task %s "
            "(status=%s, vm=%s, result_vm=%s)",
            task_id,
            task.status,
            task.vm_instance_id,
            vm_id,
        )
        await session.rollback()
        return None
    return task


def _clear_dispatch_prep_failures(task: Task) -> None:
    """Drop the contained dispatch-crash counter ahead of a dispatch commit.

    Called on the dispatch paths whose commit moves the task out of the
    crash-prone prep window (warm-pool lease, PROVISIONING). The cleared
    counter is persisted by that same commit — if the commit (or anything
    before it) still crashes, the rollback restores the counter so quarantine
    progress is never lost to a partially-successful attempt.
    """
    _dispatch_prep_contention_streak.pop(str(task.id), None)
    if "_dispatch_prep_failures" in (task.context or {}):
        ctx = dict(task.context)
        ctx.pop("_dispatch_prep_failures", None)
        task.context = ctx


def _warm_pin_still_honorable(
    *,
    warm_expired: bool,
    breaker_suppressed: bool,
    vm_doomed: bool,
    vm_alive: bool,
) -> bool:
    """WI-5: the composed skip-provision honor predicate for a warm-pinned task.

    A warm pin is honored (task left QUEUED for the target VM's /next-role
    poll, no fresh provision) only when ALL of the following hold:

    - the warm handoff deadline has not passed (``not warm_expired``),
    - the relevant circuit breaker is not suppressing warm reuse
      (``not breaker_suppressed``),
    - the orchestrator has NOT already scheduled the target VM for teardown
      (``not vm_doomed``) — a torn-down VM's <90s completion heartbeat is a
      corpse beat, not proof of life, so honoring it busy-loops
      skip-provision until the liveness window lapses (WI-5 primary fix), and
    - the target VM is heartbeating within the liveness window (``vm_alive``).

    Pure by construction: callers pass ``is_vm_alive(...)`` and
    ``is_orchestrator_initiated_delete(...)[0]`` in as plain booleans so this
    predicate does no ORM/session/clock/registry work and is directly unit
    testable — the inline dispatch-loop gate has no other extractable seam.
    """
    return (
        (not warm_expired)
        and (not breaker_suppressed)
        and (not vm_doomed)
        and vm_alive
    )


# ---------------------------------------------------------------------------
# Module-level globals managed by orchestrator_worker
# ---------------------------------------------------------------------------

_last_pool_refresh: datetime = _utcnow()
_last_usage_meter_refresh: datetime = _utcnow()
_last_zone_health_persist: datetime = _utcnow()
_last_anomaly_state_persist: datetime = _utcnow()
_last_anomaly_sweep: datetime = _utcnow()
# C3: regression dispatch + EVALUATING-timeout recovery now run as dedicated
# background tasks (lifecycle.py) on their own schedules. These two module
# attributes are retained for back-compat (tests / metrics) but are no longer
# the cadence gate for those sweeps.
_last_regression_dispatch: datetime = _utcnow()
_last_orphan_check: datetime = _utcnow()
_last_evaluating_timeout_check: datetime = _utcnow()
_last_post_commit_sweep: datetime = _utcnow()
_last_sidecar_sweep: datetime = _utcnow()
_last_bootstrap_recovery_sweep: datetime = _utcnow()
_last_lane_baseline_backfill: datetime = _utcnow()
_last_merge_credit_backfill: datetime = _utcnow()
_last_v22_finding_backfill: datetime = _utcnow()
_last_runner_deactivation_sweep: datetime = _utcnow()
_last_runner_degraded_sweep: datetime = _utcnow()
_last_runner_escalation_sweep: datetime = _utcnow()
_last_local_runner_hold_sweep: datetime = _utcnow()
_last_deadlock_check: datetime | None = None
_last_zone_exhaustion_log: datetime | None = None
_last_budget_gate_log: datetime | None = None
_last_concurrency_broadcast: datetime | None = None
_last_type_throttle_broadcast: dict[str, datetime | None] = {}
_last_type_unthrottle_broadcast: dict[str, datetime | None] = {}
_previously_failing_types: set[str] = set()

# Debounce: fire the all-accounts-blocked alert once per stall episode.
# Reset when any task dispatches successfully (stall resolved), then re-arm
# if every eligible task is again blocked by account rate limits.
_all_accounts_blocked_alerted: bool = False

# ---------------------------------------------------------------------------
# The local-runner + compute-experiment dispatch cluster now lives in
# ``app.orchestrator_runner_dispatch``. Symbols below are re-exported so
# ``from app.orchestrator import ...`` (and ``orch.X`` attribute access) keep
# working for existing callers and tests; the leader cycle calls these helpers
# in the dispatch loop body. ``_all_accounts_blocked_alerted`` stays defined
# above because ``orchestrator_worker`` mutates it via ``global``.
# ---------------------------------------------------------------------------
from app.orchestrator_runner_dispatch import (  # noqa: F401
    _LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS,
    _RUNNER_LIVENESS_SECONDS,
    RunnerDispatchSnapshot,
    _build_compute_launch_kwargs,
    _check_degraded_runners,
    _create_strategic_local_cloud_fallback_alert,
    _deactivate_stale_runners,
    _dispatch_compute_experiment_directly,
    _emit_all_accounts_blocked_stall,
    _fail_compute_experiment_task,
    _fail_stale_local_runner_holds,
    _hold_strategic_local_runner_task,
    _load_active_runners_cache,
    _load_runner_compute_job_assignments,
    _num_or_none,
    _parse_local_runner_hold_timestamp,
    _runner_active_compute_job_count,
    _runner_active_task_count,
    _runner_active_work_count,
    _runner_agent_available,
    _runner_at_capacity,
    _runner_gpu_capable,
    _runner_handles_task_type,
    _select_eligible_runner,
    _strategic_local_hold_timeout_seconds,
    _sweep_runner_deactivated_escalations,
    _task_requires_strategic_local_runner,
    _try_runner_dispatch,
    _try_strategic_local_required_runner_path,
)
# Timestamp updated each orchestrator cycle — used by /debug endpoint
_orchestrator_last_cycle: datetime | None = None
_orchestrator_cycle_started: datetime | None = None
# In-cycle progress heartbeat. Updated frequently *within* a cycle (cycle
# start + once per CI-poll task) so liveness reflects "the worker is making
# progress", NOT "a full cycle finished within N seconds". A cycle's wall
# time grows O(EVALUATING backlog) — gating liveness on full-cycle completion
# kills a slow-but-healthy worker, discarding its progress and spiralling
# (incident 2026-05-28: O(N) CI-poll cycle outgrew the fixed 60s /health
# window → Cloud Run liveness death-loop → whole pipeline froze). The /health
# liveness verdict reads this; only a genuinely wedged worker (no heartbeat
# for ORCHESTRATOR_HANG_SECONDS) is reported unhealthy and restarted.
_orchestrator_last_progress: datetime | None = None

# Consecutive error counters for exponential backoff
_orchestrator_consecutive_errors: int = 0
_orchestrator_consecutive_monitoring_errors: int = 0

# Configurable throttle sleep when high failure rate detected
ORCHESTRATOR_THROTTLE_SECONDS = int(os.getenv("ORCHESTRATOR_THROTTLE_SECONDS", "30"))

# A task whose dispatch preparation crashes this many consecutive times
# (contained per-task failures, counted in context["_dispatch_prep_failures"])
# is quarantined: FAILED + PROVISION_FAILURE classification + operator alert.
# Without the bound, a deterministic prep crash (malformed payload/context)
# re-occupies the same ranked dispatch slot every cycle and the per-type
# failure gate never engages because the task never reaches FAILED.
DISPATCH_PREP_FAILURE_QUARANTINE_THRESHOLD = int(
    os.getenv("DISPATCH_PREP_FAILURE_QUARANTINE_THRESHOLD", "3")
)

# A task whose dispatch preparation keeps hitting DB *contention* (lock timeout
# / serialization failure — see is_db_contention_error) is normally left QUEUED
# and retried next cycle WITHOUT counting toward the poison quarantine above,
# because genuine contention self-heals in a cycle or two (prod saw ~489
# lock_timeout events/24h falsely FAILing healthy tasks). But a PERSISTENT
# non-transient error that keeps surfacing as a contention SQLSTATE (raw 25P02,
# or an error raised while handling a contention error) would otherwise retry
# EVERY cycle forever. This far-higher bound is the safety valve: a task that
# hits contention this many CONSECUTIVE cycles is quarantined like a poison
# task. The streak is tracked in leader-process memory (not the DB) so the
# common transient path adds no write during the very contention burst it
# handles; it resets on any successful dispatch (via
# _clear_dispatch_prep_failures) so only a genuinely stuck task reaches the cap.
DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD = int(
    os.getenv("DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD", "40")
)
# task_id -> consecutive contention-classified dispatch-prep failures (leader
# memory only; a leader restart harmlessly resets streaks to 0).
_dispatch_prep_contention_streak: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Liveness heartbeat + hard-watchdog machinery — extracted to
# ``app.orchestrator_liveness``. The liveness gauges (_orchestrator_last_cycle /
# _orchestrator_cycle_started / _orchestrator_last_progress, declared above)
# remain owned HERE — orchestrator_worker stamps them in-place under ``global``
# and sibling modules/tests read them as ``app.orchestrator.<name>``; the
# extracted logic reaches them through a lazy import. Symbols below are
# re-exported so ``from app.orchestrator import ...`` keeps working for existing
# callers and tests.
# ---------------------------------------------------------------------------
from app.orchestrator_liveness import (  # noqa: F401
    LEADER_STEP_BUDGET_SECONDS,
    ORCHESTRATOR_HANG_SECONDS,
    ORCHESTRATOR_HARD_WATCHDOG_POLL_SECONDS,
    ORCHESTRATOR_HARD_WATCHDOG_SECONDS,
    OrchestratorLiveness,
    _bounded_leader_step,
    _default_hard_abort,
    _hard_watchdog_loop,
    _hard_watchdog_step,
    _mark_orchestrator_progress,
    evaluate_orchestrator_liveness,
    hard_watchdog_should_abort,
    start_orchestrator_hard_watchdog,
    stop_orchestrator_hard_watchdog,
)

# ---------------------------------------------------------------------------
# The EVALUATING / CI-waiting monitor sweep now lives in
# ``app.orchestrator_ci_monitor``. Symbols below are re-exported so
# ``from app.orchestrator import ...`` keeps working for existing callers and
# tests; the leader cycle calls ``_monitor_evaluating_tasks`` in the loop body.
# ---------------------------------------------------------------------------
from app.orchestrator_ci_monitor import (  # noqa: F401
    _VERIFY_TESTS_MAX_RETRIES,
    _VERIFY_TESTS_SPAWN_DEFERRAL_CAP,
    _apply_ci_merged_landed_signal,
    _capture_ci_polling_snapshot,
    _ci_result_from_resolved_pr_ci_status,
    _ci_waiting_sql_filter,
    _clear_retry_after_state,
    _monitor_evaluating_tasks,
    _resolve_ci_fix_branch,
)


# Track in-flight provision coroutines so stale recovery can cancel them.
# Without cancellation, zombie provisions hold semaphore slots for up to 360s,
# starving new provisions and creating a cascading failure where no VM can
# claim its task (the reaper assigns a new vm_instance_id before the VM boots).
_provision_coroutines: Dict[str, asyncio.Task] = {}

# ---------------------------------------------------------------------------
# VM provisioning + provision-failure handling now lives in
# ``app.orchestrator_provision``. ``provision_and_handle`` is re-exported so
# ``from app.orchestrator import provision_and_handle`` keeps working and the
# dispatch loop can fire it via ``asyncio.create_task``. The extracted coroutine
# reaches ``provision_agent_vm`` / ``launch_agent_cloud_run_job`` /
# ``prepare_and_store_agent_secret`` / ``upload_json_payload`` /
# ``_capture_serial_for_vm`` / ``_lock_provisioning_task_for_vm`` through
# ``import app.orchestrator as _orch`` at call time, so those names stay imported
# above purely as the test-patch surface. ``provision_and_handle`` itself is
# invoked by the dispatch loop below (not just re-exported), so it needs no F401
# exemption; E402 is globally exempted for this module in pyproject.
# ---------------------------------------------------------------------------
from app.orchestrator_provision import provision_and_handle

# Columns the dispatch loop loads for active (PROVISIONING/IN_PROGRESS) tasks.
# Heavy JSONB (status_log, depends_on, history) stays deferred. Anything a
# downstream filter touches (e.g. filter_file_conflicts -> context/title,
# reserve_migration_revisions -> payloads via first_payload) MUST be here,
# or attribute access triggers a sync lazy-load from async context and crashes
# the whole dispatch cycle. Module-level so tests can mirror the exact set.
ACTIVE_TASKS_LIGHT_COLUMNS = (
    Task.id,
    Task.status,
    Task.last_heartbeat,
    Task.started_at,
    Task.vm_instance_id,
    Task.project_id,
    Task.context,
    Task.title,
    Task.payloads,
    # filter_file_conflicts (Step 2.5) reads ``t.parent_task_id`` on the
    # in-progress blockers for its cross-parent looseness check
    # (orchestrator_filters.py, added in PR #7431). Deferring the column makes
    # that attribute access a sync lazy-load from this async context →
    # MissingGreenlet that kills the ENTIRE dispatch cycle (10x in prod
    # 2026-06-03 07:0x-07:48). Same gotcha class as ``status_log`` per
    # reference_load_only_event_listeners.md — any column a downstream filter
    # touches on active tasks MUST be loaded here.
    Task.parent_task_id,
)


# Light column set for the QUEUED candidate scan (Step 2.4 candidate_stmt). Drops
# heavy JSONB (status_log_data / history blobs) but MUST carry every column a
# downstream Step-2.5 filter reads on a candidate — including ``title``, because a
# QUEUED candidate that becomes a senior file-conflict reservation holder is used
# as ``blocking_task`` and its ``.title`` is read for the deferral message
# (orchestrator_filters.py). Deferring such a column makes that access a sync
# lazy-load from async context → MissingGreenlet that crashes the whole cycle.
CANDIDATE_LIGHT_COLUMNS = (
    Task.id,
    Task.status,
    Task.priority,
    Task.created_at,
    Task.requires_approval,
    Task.retry_after_until,
    Task.context,
    Task.depends_on,
    Task.launched_by_user_id,
    Task.project_id,
    Task.payloads,
    Task.status_log,
    Task.latest_status_message,
    Task.parent_task_id,
    Task.title,
)


# Light column set for parked-BLOCKED file-conflict holders (Step 2.5
# blocked_conflict_tasks). They are absent from the QUEUED candidate scan but
# still hold a seniority reservation; ``title`` is required because a parked
# senior can be the ``blocking_task`` whose ``.title`` is read for the deferral
# message (orchestrator_filters.py) — deferring it raises MissingGreenlet.
BLOCKED_CONFLICT_LIGHT_COLUMNS = (
    Task.id,
    Task.status,
    Task.created_at,
    Task.context,
    Task.project_id,
    Task.blocker_kind,
    Task.parent_task_id,
    Task.title,
    # filter_file_conflicts now reads ``first_payload`` (payloads) on these
    # parked holders via _is_read_only_task when building the seniority set, so
    # payloads MUST be loaded — deferring it makes that access a sync lazy-load
    # from async context → MissingGreenlet that crashes the whole dispatch cycle
    # (same trap as parent_task_id above; see reference_load_only_lazy_load_trap).
    Task.payloads,
)

# PROJECTION CONTRACT: all load_only() calls in backend/app pass raiseload=True so
# that any code path accessing an unprojected column raises InvalidRequestError
# synchronously at test time instead of MissingGreenlet at runtime.  When a new
# column access fails with InvalidRequestError, fix it by REMOVING the unauthorized
# column access from the code path — never by widening the projection tuple above.


async def _project_resource_dispatch_defer_message(
    session: AsyncSession,
    task: Task,
    project_active_counts: dict[uuid.UUID, int],
    *,
    project_cache: dict[uuid.UUID, Project | None] | None = None,
    spend_cache: dict[uuid.UUID, tuple[float, float, int]] | None = None,
    budget_exhausted: bool = False,
) -> str | None:
    """Return an operator-readable resource-envelope deferral reason, if any."""
    if project_cache is not None and task.project_id in project_cache:
        project = project_cache[task.project_id]
        # A per-task rollback expires every ORM instance in the session.  The
        # dispatch cache intentionally survives those contained rollbacks, so a
        # cached Project may no longer have build_config loaded.  Reading it
        # synchronously through resolve_project_* would then attempt async I/O
        # outside greenlet_spawn and raise MissingGreenlet.  Refresh the one
        # resource-envelope column explicitly before any synchronous helper
        # touches it; healthy cache hits remain query-free.
        if project is not None:
            state = sa_inspect(project)
            if state.expired_attributes:
                # A per-task rollback expires ALL scalar columns on every cached
                # ORM instance.  Refresh the entire row so any column accessed by
                # resolve_project_* helpers (build_config, paused, etc.) is live.
                # Healthy cache hits remain query-free (no expired attributes).
                await session.refresh(project)
    else:
        project = await session.get(Project, task.project_id)
        if project_cache is not None:
            project_cache[task.project_id] = project
    if project is None:
        return (
            "Waiting for project resource envelope check — project record unavailable"
        )

    vm_cap = resolve_project_vm_cap(project)
    if vm_cap is not None:
        active_for_project = project_active_counts.get(task.project_id, 0)
        if active_for_project >= vm_cap:
            return (
                "Waiting for project VM capacity — "
                f"{active_for_project}/{vm_cap} project VM slots are already in use"
            )

    spend_ceiling = resolve_project_spend_ceiling(project)

    # Per-project spend ceiling check — only runs when a ceiling is configured.
    if spend_ceiling is not None:
        spend_window_days = resolve_project_spend_window_days(project)
        if spend_cache is not None and task.project_id in spend_cache:
            recent_spend, spend_ceiling, spend_window_days = spend_cache[task.project_id]
        else:
            spend_breakdown = await get_project_recent_spend_usd(
                session,
                project_id=task.project_id,
                window_days=spend_window_days,
            )
            recent_spend = spend_breakdown.get(
                "ceiling_gate_total", spend_breakdown["total"]
            )
            if spend_cache is not None:
                spend_cache[task.project_id] = (
                    recent_spend,
                    spend_ceiling,
                    spend_window_days,
                )
        if recent_spend >= spend_ceiling:
            return (
                "Waiting for project spend budget — "
                f"${recent_spend:.2f}/${spend_ceiling:.2f} spent in the last "
                f"{spend_window_days} day(s)"
            )

    # Monthly budget gate: when fleet-wide month-to-date spend has reached 100%
    # of monthly_budget_target_usd, defer all projects with queued tasks until
    # the budget resets or the target is raised. Operates independently of the
    # per-project spend ceiling above so projects without a spend_ceiling_usd
    # (the default) are still gated when the fleet-wide budget is exhausted.
    if budget_exhausted:

        return (
            "Waiting for monthly budget — "
            "fleet-wide spend has exceeded the monthly budget target"
        )
    return None


async def _filter_project_resource_dispatch_eligible(
    session: AsyncSession,
    eligible_ids: list[uuid.UUID],
    candidate_tasks: list[Task],
    project_active_counts: dict[uuid.UUID, int],
    broadcast_to_project,
    *,
    budget_exhausted: bool = False,
) -> list[uuid.UUID]:
    """Drop resource-deferred tasks before the global slot slice."""
    task_by_id = {task.id: task for task in candidate_tasks}
    projected_counts = dict(project_active_counts)
    project_cache: dict[uuid.UUID, Project | None] = {}
    spend_cache: dict[uuid.UUID, tuple[float, float, int]] = {}
    filtered: list[uuid.UUID] = []

    for task_id in eligible_ids:
        task = task_by_id.get(task_id)
        if task is None:
            continue
        if _task_requires_strategic_local_runner(task):
            filtered.append(task_id)
            continue
        try:
            defer_msg = await _project_resource_dispatch_defer_message(
                session,
                task,
                projected_counts,
                project_cache=project_cache,
                spend_cache=spend_cache,
                budget_exhausted=budget_exhausted,
            )
        except Exception as exc:
            logger.exception(
                "[ORCHESTRATOR] Error checking resource eligibility for task %s (project %s): %s",
                task.id,
                task.project_id,
                exc,
            )
            defer_msg = f"Deferred due to resource check error: {type(exc).__name__}"
        if defer_msg:
            logger.info(
                "[ORCHESTRATOR] Deferring task %s for project %s before slot slice: %s",
                task.id,
                task.project_id,
                defer_msg,
            )
            if task.latest_status_message != defer_msg:
                task.latest_status_message = defer_msg
                session.add(task)
                await broadcast_to_project(
                    str(task.project_id),
                    "task_updated",
                    {
                        "task_id": str(task.id),
                        "status": task.status,
                        "message": defer_msg,
                    },
                )
            continue
        filtered.append(task_id)
        projected_counts[task.project_id] = projected_counts.get(task.project_id, 0) + 1

    # Broadcast per-project spend-ceiling state transitions for caller
    # (admin-ui) to surface ceiling-deferred indicators without waiting
    # for the next resource-envelope API fetch.
    _current_ceiling_deferred: set[str] = set()
    for _pid, (_recent, _ceiling, _) in spend_cache.items():
        if _recent >= _ceiling:
            _current_ceiling_deferred.add(str(_pid))
            # HARD-pause enforcement: when pause_on_ceiling_exceeded is
            # configured AND the spend ceiling is breached, pause the
            # project until an operator manually unpauses.  Guard on
            # project.paused to avoid re-firing every cycle.
            _project = project_cache.get(_pid)
            if _project is not None and resolve_project_pause_on_ceiling_exceeded(_project):
                if not _project.paused:
                    _project.paused = True
                    session.add(_project)
                    logger.info(
                        "[ORCHESTRATOR] Hard-pausing project %s — "
                        "spend ceiling exceeded and pause_on_ceiling_exceeded is set",
                        _pid,
                    )
                    try:
                        await broadcast_to_project(
                            str(_pid),
                            "project_spend_ceiling_exceeded",
                            {"project_id": str(_pid)},
                        )
                    except Exception:
                        logger.exception(
                            "[ORCHESTRATOR] Failed to broadcast spend_ceiling_exceeded for %s",
                            _pid,
                        )
    _prev = _shared_state.project_ceiling_deferred_ids
    for _pid in _current_ceiling_deferred - _prev:
        try:
            await broadcast_to_project(_pid, "project_spend_ceiling_deferred", {"project_id": _pid})
        except Exception:
            logger.exception("[ORCHESTRATOR] Failed to broadcast ceiling_deferred for %s", _pid)
    for _pid in _prev - _current_ceiling_deferred:
        try:
            await broadcast_to_project(_pid, "project_spend_ceiling_restored", {"project_id": _pid})
        except Exception:
            logger.exception("[ORCHESTRATOR] Failed to broadcast ceiling_restored for %s", _pid)
    _shared_state.project_ceiling_deferred_ids = _current_ceiling_deferred

    # Broadcast per-project monthly-budget state transitions.  When the
    # fleet-wide monthly budget is exhausted, every project that has queued
    # tasks is deferred.  Transitions mirror the spend_ceiling pattern above
    # so the admin-ui surfaces the same throttle indicators.
    _current_monthly_deferred: set[str] = set()
    if budget_exhausted:
        _current_monthly_deferred = {
            str(t.project_id) for t in task_by_id.values() if t is not None
        }
    _prev_monthly = _shared_state.monthly_budget_deferred_project_ids
    for _pid in _current_monthly_deferred - _prev_monthly:
        try:
            await broadcast_to_project(
                _pid, "project_monthly_budget_deferred", {"project_id": _pid}
            )
        except Exception:
            logger.exception(
                "[ORCHESTRATOR] Failed to broadcast monthly_budget_deferred for %s", _pid
            )
    for _pid in _prev_monthly - _current_monthly_deferred:
        try:
            await broadcast_to_project(
                _pid, "project_monthly_budget_restored", {"project_id": _pid}
            )
        except Exception:
            logger.exception(
                "[ORCHESTRATOR] Failed to broadcast monthly_budget_restored for %s", _pid
            )
    _shared_state.monthly_budget_deferred_project_ids = _current_monthly_deferred

    if session.dirty:
        await session.commit()
    return filtered


# ---------------------------------------------------------------------------
# Cycle / health classification — extracted to ``app.orchestrator_health``.
# Symbols below are re-exported so ``from app.orchestrator import ...`` keeps
# working for existing callers and tests.
# ---------------------------------------------------------------------------
from app.orchestrator_health import (  # noqa: F401
    _CRITICAL_EXTERNAL_DEPS,
    _classify_stuck_tasks,
    _find_cycle_members,
    _health_score_task,
    reorder_eligible_by_health,
)


# ---------------------------------------------------------------------------
# Env-backed runtime-credential fallback — extracted to
# ``app.orchestrator_runtime_env``. Symbols below are re-exported so
# ``from app.orchestrator import ...`` keeps working for existing callers and
# tests.
# ---------------------------------------------------------------------------
from app.orchestrator_runtime_env import (  # noqa: F401
    _ENV_FALLBACK_DEFAULT_MODELS,
    _provider_env_credential_hint,
    _resolve_env_runtime_fallback,
)


def _sync_task_payload_runtime_fields(
    task: Task,
    *,
    model: str | None,
    execution_mode: str | None,
) -> bool:
    """Keep payload runtime hints aligned with the resolved task runtime identity."""
    if not task.payloads:
        return False
    payload = task.payloads[0]
    if payload is None:
        payload = {}
        task.payloads[0] = payload
    changed = False
    if model and payload.get("model") != model:
        payload["model"] = model
        changed = True
    if execution_mode and payload.get("execution_mode") != execution_mode:
        payload["execution_mode"] = execution_mode
        changed = True
    return changed


# ---------------------------------------------------------------------------
# Dependency-lifecycle cluster — extracted to ``app.dep_lifecycle``.
# Symbols below are re-exported so ``from app.orchestrator import ...`` keeps
# working for existing callers and tests.
# ---------------------------------------------------------------------------
from app.dep_lifecycle import (  # noqa: F401
    _apply_deadlock_classification,
    _cascade_cancel_dependent,
    _escalate_stranded_dependent,
    _mark_unblocked_dependents,
    _persist_deps_satisfied,
    _persist_requeue_dep_remap,
    _recover_stale_dependency_deadlocks,
)
from app.orchestrator_health import (
    _check_candidate_task_deps,
    _effective_dep_task,
    ensure_dependent_base_recut,
    get_confirmed_down_critical_deps,
    recut_landed_cross_base_dependents,
)


def _select_cycle_sleep(
    *,
    consecutive_errors: int,
    consecutive_monitoring_errors: int,
    cycle_dispatched: int,
    error_backoff: float,
    monitoring_backoff: float,
    fast_interval: float = 0.5,
    idle_interval: float = 5.0,
) -> float:
    """Pick the post-cycle sleep duration for the orchestrator main loop.

    The fast path (``fast_interval``) is gated on ACTUAL dispatch progress this
    cycle (``cycle_dispatched > 0``), NOT on queue-presence. Tasks can sit
    QUEUED-but-undispatchable for many cycles — warm-pin skip-provision,
    strategic-local-runner hold, project resource/spend defer, no-credentials,
    backlog-reuse first-refusal — without ever incrementing ``cycle_dispatched``.
    Gating the fast path on ``queued_tasks`` being non-empty (the old behavior)
    made the loop spin at 2 Hz whenever any such held task existed, re-running the
    whole dispatch + review/PR + monitoring pass to no effect (~10x load
    amplifier, WI-4).

    ``cycle_dispatched`` increments only where a task actually moves OUT of
    QUEUED, so if a task were genuinely dispatchable the loop would have
    dispatched it and bumped the counter. ``cycle_dispatched == 0`` therefore
    provably means nothing was dispatchable this cycle — back off to
    ``idle_interval``. The extra idle wait only delays noticing external state
    changes (a slot freeing, an account recovering, a runner registering), all
    second-to-minute-scale; CI polling is already 30s-gated + webhook-driven, and
    per-cycle health checks still run at the 5s idle cadence when slots are full.
    """
    if consecutive_errors > 0:
        return error_backoff
    if consecutive_monitoring_errors > 0:
        return monitoring_backoff
    if cycle_dispatched > 0:
        return fast_interval
    return idle_interval


async def orchestrator_worker():
    """
    Background worker that polls for QUEUED tasks and provisions VMs.
    This ensures tasks are not lost if the server restarts before provisioning.
    """
    # Late imports to avoid circular dependency with app.main
    from app.max_plan_pool import (
        load_max_plan_pool,
        refresh_usage_meter_snapshots,
        release_max_plan_account,
        recheck_disabled_accounts,
        _extract_task_model,
        _account_available_for_model,
        _account_by_id,
        _max_plan_pools,
        _max_plan_pool_lock,
        apply_task_llm_runtime_context,
        clear_task_llm_runtime_context,
        get_task_llm_account_id,
        reconcile_model_for_provider,
        resolve_task_quality_tier,
        select_llm_runtime_for_task,
    )
    from app.providers.registry import get_provider
    from app.vm_lifecycle import (
        _get_actual_gcp_vm_count,
        _ensure_vm_deleted,
    )
    from app.warm_pool import (
        is_warm_pool_enabled,
        mark_pool_vm_retiring,
        try_pool_dispatch,
    )

    # Pool dispatch funnel: record into the warm_backlog_reuse counters that the
    # operator panel (admin_health._get_pool_metrics → get_pool_funnel_snapshot)
    # actually displays, so the surfaced pool_metrics.funnel reflects real
    # dispatch activity instead of staying permanently zero.
    from app.warm_backlog_reuse import (
        record_pool_cold_dispatch,
        record_pool_lease_hit,
    )
    from app.alert_dispatch import (
        _maybe_create_failure_alert,
        _maybe_create_cascade_cancel_alert,
        _create_orchestrator_error_alerts,
    )
    from app.routers.agent_status import record_task_result
    from app.deploy_health import (
        _check_monitoring_window,
        _check_external_health,
        _check_pause_mode,
    )
    from app.autonomous_research import _check_idle_research
    from app.evergreen_backlog import check_evergreen_fill as _check_evergreen_fill
    from app.rollup_management import _verify_pr_merged
    from app.broadcast import broadcast_to_project, manager as ws_manager
    from app.services import (
        _recover_orphaned_tasks,
        _sweep_post_commit_pending,
        _sweep_stranded_sidecar_sources,
        _sweep_bootstrap_recovery,
        _sweep_lane_baseline_backfill,
    )

    global _last_pool_refresh, _last_usage_meter_refresh, _orchestrator_last_cycle, _orchestrator_cycle_started
    global _orchestrator_consecutive_errors, _orchestrator_consecutive_monitoring_errors
    global \
        _last_orphan_check, \
        _last_post_commit_sweep, \
        _last_sidecar_sweep, \
        _last_bootstrap_recovery_sweep, \
        _last_lane_baseline_backfill, \
        _last_merge_credit_backfill, \
        _last_v22_finding_backfill, \
        _last_runner_deactivation_sweep, \
        _last_runner_degraded_sweep, \
        _last_runner_escalation_sweep, \
        _last_local_runner_hold_sweep, \
        _last_budget_gate_log
    while True:
        queued_tasks = []
        # Reset at the very top of the loop body (before the try) — REQUIRED for
        # correctness, not merely defensive. The end-of-cycle sleep decision reads
        # _cycle_dispatched from a SEPARATE try-block (the _select_cycle_sleep call)
        # that runs even when the main dispatch try raises before reaching its own
        # authoritative per-cycle reset (with the sibling counters, in the dispatch
        # block below). Without this top-of-loop init, a first-cycle early crash
        # leaves _cycle_dispatched unbound → NameError on that read → swallowed by
        # the sleep block's except → no sleep runs → the loop spins at zero delay
        # (the exact busy-loop WI-4 fixes). It also keeps a stale prior-cycle count
        # from leaking into the sleep decision.
        _cycle_dispatched = 0
        session = None  # defined before try so except handler can reference it
        try:
            _orchestrator_cycle_started = _utcnow()
            cycle_start = time.monotonic()
            _mark_orchestrator_progress()
            tasks_recovered = 0

            # Periodic pool refresh — pick up admin changes without restart
            if _is_cadence_due(_last_pool_refresh, 300):
                await load_max_plan_pool()
                _last_pool_refresh = _utcnow()

            # Periodic usage-meter refresh — keeps the admin usage meter
            # (5h/7d plan utilization) populated independent of the
            # default-off usage_headroom selection gate. Fire-and-forget
            # probes; never blocks dispatch.
            if _is_cadence_due(_last_usage_meter_refresh, 300):
                await refresh_usage_meter_snapshots()
                _last_usage_meter_refresh = _utcnow()

            # Periodic zone health persist — prevents retry storms after restart
            global _last_zone_health_persist
            if _is_cadence_due(_last_zone_health_persist, 60):
                await _persist_zone_health()
                _last_zone_health_persist = _utcnow()

            # Periodic self-healing state persist — anomaly counts, direction holds, pipeline events.
            # All three live on the same DeploymentState row, so coalesce into a
            # single session/commit (M-P6) instead of three round-trips.
            global _last_anomaly_state_persist
            if _is_cadence_due(_last_anomaly_state_persist, 60):
                await _persist_self_healing_state_bundle()
                _last_anomaly_state_persist = _utcnow()

            # C3: the pipeline-regression dispatcher + watchdog alarms used to
            # run inline here on the leader's hot dispatch cycle (iterating every
            # autonomous project). It now runs as a dedicated leader-only
            # background task (``_regression_dispatch_service`` in lifecycle.py)
            # with its own 60s schedule and a per-tick asyncio.timeout, so the
            # dispatch cycle's liveness is no longer coupled to per-project
            # regression I/O. ``_last_regression_dispatch`` is retained only as a
            # back-compat module attribute (referenced by tests/metrics).

            # Independent anomaly maintenance — expires stale entries and restores
            # pipeline adjustments without depending on autonomous research cycles.
            global _last_anomaly_sweep
            if _is_cadence_due(_last_anomaly_sweep, 600):
                from app.anomaly_detection import anomaly_maintenance_sweep

                try:
                    await anomaly_maintenance_sweep()
                except Exception:
                    logger.exception("anomaly_maintenance_sweep failed")
                _last_anomaly_sweep = _utcnow()

            # First-cycle awaits are the most common silent-hang site: connection
            # pool exhaustion, row-level locks from a previously OOM'd instance,
            # or VPC/Cloud-SQL proxy hiccups all manifest as an indefinite wait
            # here with no logs. Bound the whole session-acquire + config-sync
            # block so a stuck cycle becomes a loud TimeoutError instead.
            try:
                async with asyncio.timeout(30):
                    async with AsyncSessionLocal() as session:
                        # Sync runtime_config with DB each cycle. Cloud Run runs up to
                        # max_instance_count instances; admin mutations (pause, settings)
                        # only update the receiving instance's in-memory runtime_config,
                        # so other instances would otherwise dispatch past a pause or
                        # stale cap. DeploymentState is the authoritative source.
                        _deploy_state = await session.get(DeploymentState, 1)
                        _changed_keys = await _sync_runtime_config_from_state(
                            _deploy_state
                        )
                        if _changed_keys:
                            logger.info(
                                f"[ORCHESTRATOR] Runtime config drift corrected: {_changed_keys}"
                            )
            except TimeoutError:
                logger.exception(
                    "[ORCHESTRATOR] Timed out (30s) acquiring session / reading DeploymentState — "
                    "likely DB pool contention or row-level lock from a stale transaction. "
                    "Skipping this cycle."
                )
                _orchestrator_last_cycle = _utcnow()
                await asyncio.sleep(2)
                continue
            async with AsyncSessionLocal() as session:
                # Fetch all active tasks once — used for both concurrency limit and counter reconciliation
                # Light query: only load columns needed for the stale-provision scan
                # and concurrency count. Heavy JSONB (context, status_log) is
                # deferred — the few stale tasks are re-read through task_lock()
                # before mutation below.
                # context + title are needed by filter_file_conflicts (Step 2.5).
                # payloads is needed by reserve_migration_revisions (Step 2.72)
                # via _is_migration_task -> task.first_payload.
                # Without them, attribute access triggers a sync lazy-load from an
                # async context and crashes the whole dispatch cycle.
                _active_light_cols = load_only(
                    *ACTIVE_TASKS_LIGHT_COLUMNS, raiseload=True
                )
                active_tasks_result = (
                    (
                        await session.execute(
                            select(Task)
                            .where(
                                Task.status.in_(
                                    [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                                )
                            )
                            .options(_active_light_cols)
                        )
                    )
                    .scalars()
                    .all()
                )

                # Recover stale PROVISIONING tasks (orchestrator crashed mid-dispatch)
                ten_min_ago = _utcnow() - timedelta(minutes=10)
                stale_vm_ids = []
                cancelled_provision_ids = []
                for t in active_tasks_result:
                    if t.status != TaskStatus.PROVISIONING:
                        continue
                    # Skip if provision coroutine is still running — it hasn't
                    # failed yet, just let it finish. Only requeue truly abandoned tasks.
                    active_coro = _provision_coroutines.get(str(t.id))
                    if active_coro and not active_coro.done():
                        continue
                    if (
                        (t.last_heartbeat is None or t.last_heartbeat < ten_min_ago)
                        and t.started_at
                        and t.started_at < ten_min_ago
                    ):
                        # Re-read with task_lock to prevent races with the reaper
                        # and provisioning callback on provision_failures/requeue state.
                        fresh_t = await _lock_task_for_mutation(
                            session,
                            t.id,
                            allowed_statuses=(TaskStatus.PROVISIONING,),
                            reason="stale provisioning recovery",
                        )
                        if not fresh_t:
                            continue  # Reaper already handled it

                        # Capture serial console BEFORE any state mutation. This
                        # is the only forensic source for silent post-creds-fetch
                        # deaths — the in-script ERR-trap + failure-beacon path
                        # can be bypassed (SIGKILL, kernel hang, lost-after-trap
                        # poweroff). reaper.py:534 already does this for the
                        # agent-startup-failure reaper; the stale-PROVISIONING
                        # path was the silent gap.
                        old_vm_for_serial = fresh_t.vm_instance_id
                        serial_log: str | None = None
                        if old_vm_for_serial:
                            prov_zone = (fresh_t.context or {}).get("provision_zone")
                            if not prov_zone:
                                cached_zone = get_vm_zone(old_vm_for_serial)
                                if cached_zone and cached_zone != "unknown":
                                    prov_zone = cached_zone
                            if prov_zone:
                                try:
                                    serial_log = await asyncio.to_thread(
                                        _capture_serial_for_vm,
                                        old_vm_for_serial,
                                        prov_zone,
                                    )
                                    if serial_log:
                                        logger.warning(
                                            f"[ORCHESTRATOR] Serial log for stale "
                                            f"provisioning VM {old_vm_for_serial} "
                                            f"(task {t.id}):\n{serial_log}"
                                        )
                                except Exception:
                                    logger.exception(
                                        f"[ORCHESTRATOR] Serial capture failed for "
                                        f"stale provisioning VM {old_vm_for_serial}"
                                    )
                            else:
                                logger.warning(
                                    f"[ORCHESTRATOR] Serial capture skipped for task "
                                    f"{t.id}: zone unknown (vm_id={old_vm_for_serial})"
                                )

                        # Check provision failure limit before requeueing
                        provision_failures = (fresh_t.context or {}).get(
                            "provision_failures", 0
                        ) + 1
                        if provision_failures >= _runtime_config.get(
                            "max_provision_retries", 3
                        ):
                            fresh_t.status = TaskStatus.FAILED
                            if fresh_t.vm_provisioned_at:
                                fresh_t.vm_stopped_at = _utcnow()
                            fresh_t.latest_status_message = (
                                f"Failed after {provision_failures} provision attempts"
                            )
                            failed_ctx = {
                                **(fresh_t.context or {}),
                                "failure_category": "PROVISION_FAILURE",
                                "provision_failures": provision_failures,
                            }
                            if serial_log:
                                failed_ctx["last_serial_log"] = serial_log
                            fresh_t.context = failed_ctx
                            logger.error(
                                f"[ORCHESTRATOR] Task {t.id} exceeded provision retry limit ({provision_failures}) — marking FAILED"
                            )
                            acct_id = get_task_llm_account_id(fresh_t)
                            if acct_id:
                                await release_max_plan_account(
                                    acct_id,
                                    revert_daily_task_count=True,
                                    project_id=str(fresh_t.project_id) if fresh_t.project_id else None,
                                )
                            session.add(fresh_t)
                            await _maybe_create_failure_alert(
                                fresh_t,
                                session,
                                f"Provision failed {provision_failures} times — exceeded retry limit",
                            )
                            record_task_result(
                                success=False,
                                failure_class="PROVISION_FAILURE",
                                task_id=str(fresh_t.id),
                                task_type=(fresh_t.first_payload).get("type", ""),
                            )
                            continue
                        stale_vm_ids.append(fresh_t.vm_instance_id)
                        # Release Max Plan account slot before requeueing
                        acct_id = get_task_llm_account_id(fresh_t)
                        if acct_id:
                            await release_max_plan_account(
                                acct_id,
                                revert_daily_task_count=True,
                                project_id=str(fresh_t.project_id) if fresh_t.project_id else None,
                            )
                        # Cancel any lingering coroutine reference
                        zombie = _provision_coroutines.pop(str(t.id), None)
                        if zombie and not zombie.done():
                            zombie.cancel()
                            cancelled_provision_ids.append(str(t.id))
                        bank_task_current_attempt(fresh_t, stopped_at=_utcnow())
                        fresh_t.status = TaskStatus.QUEUED
                        fresh_t.started_at = None
                        fresh_t.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        requeue_ctx = {
                            **(fresh_t.context or {}),
                            "provision_failures": provision_failures,
                        }
                        if serial_log:
                            requeue_ctx["last_serial_log"] = serial_log
                        fresh_t.context = clear_task_llm_runtime_context(requeue_ctx)
                        # Keep the promoted column in sync (D2 over-fetch fix).
                        fresh_t.llm_account_id = None
                        session.add(fresh_t)
                        tasks_recovered += 1
                        if fresh_t.last_heartbeat is None:
                            logger.warning(
                                f"[ORCHESTRATOR] Recovered stale PROVISIONING task {t.id} → QUEUED (no heartbeat, started_at={fresh_t.started_at})"
                            )
                        else:
                            logger.warning(
                                f"[ORCHESTRATOR] Recovered stale PROVISIONING task {t.id} → QUEUED (stale heartbeat={fresh_t.last_heartbeat}, started_at={fresh_t.started_at})"
                            )
                if cancelled_provision_ids:
                    logger.warning(
                        f"[ORCHESTRATOR] Cancelled {len(cancelled_provision_ids)} zombie provision coroutine(s): {cancelled_provision_ids}"
                    )
                if session.dirty:
                    await session.commit()
                    # Delete old VMs from stale provisions (after commit)
                    for old_vm in stale_vm_ids:
                        if old_vm:
                            await _ensure_vm_deleted(
                                old_vm, reason="stale_provisioning"
                            )
                    # Re-fetch after recovery (light columns only — just need count)
                    active_tasks_result = (
                        (
                            await session.execute(
                                select(Task)
                                .where(
                                    Task.status.in_(
                                        [
                                            TaskStatus.PROVISIONING,
                                            TaskStatus.IN_PROGRESS,
                                        ]
                                    )
                                )
                                .options(_active_light_cols)
                            )
                        )
                        .scalars()
                        .all()
                    )

                # Enforce concurrency limit
                active_count = len(active_tasks_result)
                project_active_counts: dict[uuid.UUID, int] = {}
                for _active_task in active_tasks_result:
                    project_active_counts[_active_task.project_id] = (
                        project_active_counts.get(_active_task.project_id, 0) + 1
                    )

                # Capacity partition: Cloud Run tasks run in a container and never
                # consume a GCE VM slot. Compute the effective CR type set (candidate
                # set ∩ runtime allowlist) and exclude those tasks from the GCE
                # dispatch budget. `payloads` is loaded by ACTIVE_TASKS_LIGHT_COLUMNS.
                _cr_capacity_allowlist = frozenset(
                    _runtime_config.get("cloud_run_task_type_allowlist", [])
                )
                _effective_cr_types_capacity = (
                    CLOUD_RUN_CANDIDATE_TASK_TYPES & _cr_capacity_allowlist
                )
                if _effective_cr_types_capacity:
                    _cr_active_count = sum(
                        1
                        for _t in active_tasks_result
                        if ((_t.first_payload or {}).get("type", ""))
                        in _effective_cr_types_capacity
                    )
                else:
                    _cr_active_count = 0
                # GCE-only active count for the VM slot budget.
                _gce_active_count = active_count - _cr_active_count

                # Cross-check with actual GCP VM count (cached, refreshed every 2 min)
                actual_vm_count = await _get_actual_gcp_vm_count()
                # Warm-pool slot accounting: a pre-provisioned idle pool VM is a
                # real agent-vm-* instance, so _get_actual_gcp_vm_count() and the
                # GCP cleanup count BOTH include it. Charging it against the
                # dispatch budget would starve real queued tasks. Exclude the
                # non-task-bound pool members (PROVISIONING/IDLE) from the GCP
                # cross-check — they live in a dedicated pool budget
                # (warm_pool_max_vms, enforced by the maintainer), NOT the dispatch
                # budget. A LEASED member running an ACTIVE task is already counted
                # in active_count, so it must NOT be subtracted — but a LEASED member
                # whose task is terminal/gone (an ORPHANED lease) is in neither
                # active_count nor IDLE_POOL_STATES and IS subtracted below.
                from app.warm_pool import (
                    count_idle_pool_vms,
                    count_orphaned_leased_pool_vms,
                    is_warm_pool_enabled,
                )

                # Only subtract idle pool inventory when the pool is enabled. When
                # disabled the maintainer/idle-sweep early-return and never clear
                # PoolVm rows, but the generic GCP cleanup still deletes the
                # underlying VMs — so stale IDLE rows would over-subtract here,
                # inflating slots_available past max_concurrent_vms. Gate it off.
                _pool_enabled = is_warm_pool_enabled()
                idle_pool_count = (
                    await count_idle_pool_vms(session) if _pool_enabled else 0
                )
                # Also exclude ORPHANED leases: LEASED pool rows whose task already
                # reached a terminal state (or vanished) without the lease being
                # released — e.g. a leader crash / Cloud-Run cutover cancelled the
                # release mid-flight. Like idle inventory these are not in-flight
                # work, but unlike a healthy LEASED member they are NOT in
                # active_count (their task is gone), so without subtracting them they
                # phantom-consume the dispatch budget and latch slots_full forever
                # (prod 2026-06-13: a leader-flap deploy left ~16 stale leases that
                # pinned slots_available at 0 with 0 active tasks — total dispatch
                # freeze). Gated on the pool being enabled, same as idle_pool_count.
                orphaned_leased_count = (
                    await count_orphaned_leased_pool_vms(session)
                    if _pool_enabled
                    else 0
                )
                gcp_dispatch_vms = max(
                    0, actual_vm_count - idle_pool_count - orphaned_leased_count
                )
                # Use _gce_active_count (CR tasks excluded) for the GCE VM budget.
                effective_count = max(_gce_active_count, gcp_dispatch_vms)
                if gcp_dispatch_vms > _gce_active_count + 2:
                    logger.warning(
                        f"[ORCHESTRATOR] GCP VM count ({actual_vm_count}, "
                        f"{idle_pool_count} idle-pool, {orphaned_leased_count} orphaned-lease) "
                        f"> DB active count ({_gce_active_count}) — possible orphans, "
                        f"using GCP count for slot calculation"
                    )
                slots_available = max(
                    0, _runtime_config["max_concurrent_vms"] - effective_count
                )
                _shared_state.tasks_active_gauge = active_count
                if slots_available == 0:
                    # Update queued gauge even when we skip dispatching
                    _shared_state.tasks_queued_gauge = (
                        await session.execute(
                            select(_func.count())
                            .select_from(Task)
                            .where(
                                Task.status == TaskStatus.QUEUED,
                                Task.requires_approval.is_(False),
                            )
                        )
                    ).scalar() or 0
                    global _last_concurrency_broadcast
                    now_broadcast = _utcnow()
                    if _is_cadence_due(_last_concurrency_broadcast, 60, now_broadcast):
                        _last_concurrency_broadcast = now_broadcast
                        try:
                            await ws_manager.broadcast_to_all(
                                "pipeline_throttle",
                                {
                                    "reason": "concurrency_limit",
                                    "detail": f"All {_runtime_config['max_concurrent_vms']} VM slots in use",
                                    "recovery_seconds": None,
                                },
                            )
                        except Exception:
                            logger.exception(
                                "[ORCHESTRATOR] Failed to broadcast concurrency_limit throttle"
                            )
                    logger.info(
                        "orchestrator_slots_full",
                        extra={
                            "slots_in_use": effective_count,
                            "total_slots": _runtime_config["max_concurrent_vms"],
                        },
                    )
                    logger.info(
                        "orchestrator_cycle_complete",
                        extra={
                            "cycle_duration_seconds": round(
                                time.monotonic() - cycle_start, 3
                            ),
                            "tasks_dispatched": 0,
                            "tasks_recovered": tasks_recovered,
                            "slots_in_use": effective_count,
                            "slots_available": 0,
                            "queue_depth": _shared_state.tasks_queued_gauge,
                        },
                    )
                    _orchestrator_last_cycle = _utcnow()
                    # Monitoring must still run when the dispatch path is idle —
                    # otherwise an auto-pause (max_concurrent_vms=0) makes
                    # slots_available=0 every cycle, which `continue`s past the
                    # monitoring block at the bottom of the loop and freezes the
                    # 3-of-5 recovery ring buffer. Without this, pause is sticky
                    # forever because _check_external_health / _check_pause_mode
                    # never run while paused.
                    try:
                        await _check_monitoring_window()
                        await _check_external_health()
                        await _check_pause_mode()
                    except Exception:
                        logger.exception(
                            "[ORCHESTRATOR] Monitoring checks failed during idle cycle"
                        )
                    # Dependency-recovery bookkeeping needs no free slot: a failed
                    # foundation's queued dependents must still be cascade-cancelled
                    # and depends_on deadlocks broken while the fleet is saturated or
                    # auto-paused (max_concurrent_vms=0). Without this the work piles
                    # up until a slot frees, which under a long pause may be never.
                    # Honor the same 60s throttle as the in-dispatch deadlock scan.
                    global _last_deadlock_check
                    _idle_now = _utcnow()
                    if _is_cadence_due(_last_deadlock_check, 60, _idle_now):
                        _last_deadlock_check = _idle_now
                        try:
                            _idle_broken = await _recover_stale_dependency_deadlocks(
                                session
                            )
                            if _idle_broken:
                                logger.warning(
                                    "[ORCHESTRATOR] Idle-cycle dependency recovery: "
                                    f"{_idle_broken} dependent(s)/deadlock(s) resolved "
                                    "with no dispatch slots available"
                                )
                        except Exception:
                            logger.exception(
                                "[ORCHESTRATOR] Idle-cycle dependency recovery failed"
                            )
                    await asyncio.sleep(5)
                    continue

                # Failure-rate dispatch gate — throttle when recent tasks are mostly failing
                # Filter to last 15 min so the gate auto-clears when failures age out
                _fr_cutoff = (_utcnow() - timedelta(minutes=15)).isoformat()
                recent_results = [
                    r
                    for r in _shared_state.deploy_monitoring["task_results"]
                    if r[0] >= _fr_cutoff
                ]
                # Types with >70% failure rate (min 3 samples) are skipped in the dispatch loop
                # below without throttling healthy types.
                _failing_types: set = set()
                if len(recent_results) >= 5:
                    recent_window = recent_results[-10:]
                    recent_failures = sum(1 for r in recent_window if r[1] == "failure")
                    recent_rate = recent_failures / len(recent_window)
                    _failure_gate = _shared_state.anomaly_adjustments[
                        "failure_gate_threshold"
                    ]
                    if recent_rate >= _failure_gate:
                        # Global backstop: entire pipeline is degraded — sleep and retry
                        logger.warning(
                            f"[ORCHESTRATOR] High failure rate ({recent_failures}/{len(recent_window)}) — throttling dispatch for {ORCHESTRATOR_THROTTLE_SECONDS}s"
                        )
                        try:
                            await ws_manager.broadcast_to_all(
                                "pipeline_throttle",
                                {
                                    "reason": "high_failure_rate",
                                    "detail": f"{recent_failures}/{len(recent_window)} recent tasks failed",
                                    "recovery_seconds": ORCHESTRATOR_THROTTLE_SECONDS,
                                },
                            )
                        except Exception:
                            logger.exception(
                                "[ORCHESTRATOR] Failed to broadcast high_failure_rate throttle"
                            )
                        # Run account re-enablement before sleeping so rate-limited accounts
                        # can recover even while dispatch is throttled
                        await recheck_disabled_accounts()
                        # Same anti-latch treatment as the external health gate
                        # below: this `continue` skips the bottom-of-loop
                        # monitoring block, freezing consecutive_failures and the
                        # cycle-completion marker while throttled. The failure-rate
                        # input itself self-ages (15-min task_results window), but
                        # external-dep state and the dashboard's last-cycle gauge
                        # must keep refreshing through the throttle.
                        _orchestrator_last_cycle = _utcnow()
                        try:
                            await _check_monitoring_window()
                            await _check_external_health()
                            await _check_pause_mode()
                        except Exception:
                            logger.exception(
                                "[ORCHESTRATOR] Monitoring checks failed during throttled cycle"
                            )
                        await asyncio.sleep(ORCHESTRATOR_THROTTLE_SECONDS)
                        continue
                    else:
                        # Per-type gate: detect individual failing types and skip only them
                        seen_types = {
                            r[3] for r in recent_results if len(r) > 3 and r[3]
                        }
                        for ttype in seen_types:
                            rate, sample_size = (
                                _shared_state.get_task_type_failure_rate(
                                    ttype, window_minutes=15
                                )
                            )
                            if sample_size >= 3 and rate >= 0.7:
                                _failing_types.add(ttype)
                                logger.warning(
                                    f"[ORCHESTRATOR] Per-type failure gate: '{ttype}' "
                                    f"({rate:.0%} failure rate, n={sample_size}) — skipping this type"
                                )

                # Broadcast type_throttle events for each newly-blocked type (debounced 60s per type)
                global \
                    _last_type_throttle_broadcast, \
                    _last_type_unthrottle_broadcast, \
                    _previously_failing_types
                if _failing_types:
                    for _ftype in _failing_types:
                        if _is_cadence_due(
                            _last_type_throttle_broadcast.get(_ftype), 60
                        ):
                            _last_type_throttle_broadcast[_ftype] = _utcnow()
                            _frate, _fsample = _shared_state.get_task_type_failure_rate(
                                _ftype, window_minutes=15
                            )
                            try:
                                await ws_manager.broadcast_to_all(
                                    "type_throttle",
                                    {
                                        "type": _ftype,
                                        "failure_rate": round(_frate, 3),
                                        "sample_size": _fsample,
                                        "detail": f"Task type '{_ftype}' throttled: {_frate:.0%} failure rate (n={_fsample})",
                                    },
                                )
                            except Exception:
                                logger.exception(
                                    f"[ORCHESTRATOR] Failed to broadcast type_throttle for '{_ftype}'"
                                )

                # Broadcast type_unthrottle for types that were gated on the prior cycle but have
                # now recovered (aged out of _failing_types). Debounced 60s per type like throttle.
                _recovered_types = _previously_failing_types - _failing_types
                for _rtype in _recovered_types:
                    if _is_cadence_due(_last_type_unthrottle_broadcast.get(_rtype), 60):
                        _last_type_unthrottle_broadcast[_rtype] = _utcnow()
                        _rrate, _rsample = _shared_state.get_task_type_failure_rate(
                            _rtype, window_minutes=15
                        )
                        logger.info(
                            f"[ORCHESTRATOR] Per-type failure gate cleared: '{_rtype}' recovered ({_rrate:.0%} failure rate, n={_rsample})"
                        )
                        try:
                            await ws_manager.broadcast_to_all(
                                "type_unthrottle",
                                {
                                    "type": _rtype,
                                    "failure_rate": round(_rrate, 3),
                                    "sample_size": _rsample,
                                    "detail": f"Task type '{_rtype}' recovered: {_rrate:.0%} failure rate (n={_rsample})",
                                },
                            )
                        except Exception:
                            logger.exception(
                                f"[ORCHESTRATOR] Failed to broadcast type_unthrottle for '{_rtype}'"
                            )
                _previously_failing_types = set(_failing_types)

                # External health gate — skip dispatch when a critical dependency has been
                # confirmed down for 2+ consecutive health checks (avoids flapping on transient probes).
                # DB is excluded: if DB is down the orchestrator itself cannot function.
                _confirmed_down = get_confirmed_down_critical_deps()
                if _confirmed_down:
                    _dep_str = ", ".join(_confirmed_down)
                    logger.warning(
                        f"[ORCHESTRATOR] External health gate: {_dep_str} confirmed down — skipping dispatch this cycle"
                    )
                    try:
                        await ws_manager.broadcast_to_all(
                            "health_gate_throttle",
                            {
                                "reason": "external_dependency_down",
                                "detail": f"Critical dependencies down: {_dep_str}",
                                "deps": _confirmed_down,
                            },
                        )
                    except Exception:
                        logger.exception(
                            "[ORCHESTRATOR] Failed to broadcast health_gate_throttle"
                        )
                    # The gate decision above reads consecutive_failures, which
                    # ONLY _check_external_health updates — and this `continue`
                    # skips the monitoring block at the bottom of the loop. Without
                    # re-running the checks here the gate latches shut forever on
                    # the last pre-gate snapshot, even after every account cooldown
                    # has expired (prod 2026-06-07: all-accounts rate-limit closed
                    # the gate at 11:42Z and only a coincidental instance restart
                    # at 16:33Z reopened it — 5h of zero dispatch). Same sticky
                    # pattern as the idle-cycle fix above. Stamp the cycle marker
                    # too so a gated-but-healthy loop doesn't render as a dead
                    # orchestrator in the dashboard (it read "last cycle 299min
                    # ago" during that outage).
                    _orchestrator_last_cycle = _utcnow()
                    try:
                        await _check_monitoring_window()
                        await _check_external_health()
                        await _check_pause_mode()
                    except Exception:
                        logger.exception(
                            "[ORCHESTRATOR] Monitoring checks failed during gated cycle"
                        )
                    # Sleep before retrying so we don't tight-loop the gate check
                    # at ~50 cycles/sec, which spams logs and trips the cycle-hung
                    # monitor (no cycle ever "completes" because we keep continuing).
                    # ORCHESTRATOR_THROTTLE_SECONDS matches the failure-rate gate
                    # at line 877 — both back off the same way.
                    await asyncio.sleep(ORCHESTRATOR_THROTTLE_SECONDS)
                    continue

                # Reconcile Max Plan active_tasks counters from DB each cycle.
                # Reads only the promoted Task.llm_account_id column (no longer
                # the full Task.context JSONB blob) — see
                # _reconcile_active_per_account (D2 over-fetch fix, d-57436105).
                active_per_account = await _reconcile_active_per_account(session)
                async with _max_plan_pool_lock:
                    for acct in _account_by_id.values():
                        # Reconcile from DB — the pre-increment in select_max_plan_account() is always
                        # committed as PROVISIONING before the next cycle's reconciliation runs.
                        db_count = active_per_account.get(acct["id"], 0)
                        acct["active_tasks"] = db_count

                # Step 1: Find candidate tasks WITHOUT lock (read-only scan for dep filtering)
                # R4-W2 (H4): drop heavy JSONB (status_log_data, history blob)
                # via load_only so a 50-row candidate pull doesn't TOAST-fetch
                # the death-trace window from every QUEUED task. Include
                # ``status_log`` per memory ``reference_load_only_event_listeners.md``
                # — Task.status / latest_status_message setters trigger event
                # listeners that read status_log, which raises MissingGreenlet
                # if it's a deferred-loaded attribute (no async session bound).
                #
                # Per-project candidate cap: fetch at most `per_project_cap`
                # candidates per project via a ROW_NUMBER() window function
                # subquery. Without this, a single at-cap project that owns
                # many high-priority QUEUED tasks can fill the entire batch and
                # starve tasks from under-cap projects that sit just beyond the
                # limit — the resource-envelope filter then defers every
                # candidate and no other project dispatches that cycle.
                _per_project_cap = max(slots_available * 2, 20)
                _rn_col = (
                    _func.row_number()
                    .over(
                        partition_by=Task.project_id,
                        order_by=[
                            Task.priority.desc().nullslast(),
                            Task.created_at.asc(),
                        ],
                    )
                    .label("row_num")
                )
                _cand_id_sq = (
                    select(Task.id, _rn_col)
                    # Exclude tasks owned by an archived OR paused project — both
                    # halt new dispatch of the project's QUEUED work. Pause is a
                    # first-class persisted switch distinct from terminal status
                    # and from autonomous_enabled: while paused, no new dispatch
                    # (cold OR warm-pool) may select the project's work, though
                    # in-flight PROVISIONING/IN_PROGRESS tasks drain. Filtering via
                    # a project-id subquery (rather than a JOIN) keeps the
                    # load_only/scalars() shape and the MissingGreenlet-safe
                    # Task-only column set untouched.
                    .where(
                        Task.status == TaskStatus.QUEUED,
                        Task.requires_approval.is_(False),
                        Task.project_id.in_(
                            select(Project.id).where(
                                Project.not_archived(),
                                Project.paused.is_(False),
                            )
                        ),
                    )
                    .subquery()
                )
                candidate_stmt = (
                    select(Task)
                    .where(
                        Task.id.in_(
                            select(_cand_id_sq.c.id).where(
                                _cand_id_sq.c.row_num <= _per_project_cap
                            )
                        )
                    )
                    # CANDIDATE_LIGHT_COLUMNS drops heavy JSONB but carries every
                    # column a Step-2.5 filter reads on a candidate (status_log,
                    # parent_task_id, title, ...). Excluding any of them makes the
                    # downstream access a sync lazy-load from async context →
                    # MissingGreenlet that crashes the whole dispatch cycle. See the
                    # constant's docstring and reference_load_only_event_listeners.md.
                    .options(load_only(*CANDIDATE_LIGHT_COLUMNS, raiseload=True))
                    .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
                )
                candidate_tasks = (
                    (await session.execute(candidate_stmt)).scalars().all()
                )

                # Per-user pool availability cache. Rate-limit-origin retry_after
                # holds (set without _infra_wait) can be released early when the
                # dispatching user's own pool recovers. Checking the global pool
                # would release tasks whose user pool is still exhausted, only to
                # re-block them at dispatch.
                # Keyed by (user_id, model) — model=None is conservative and would
                # hold a Sonnet task when only Opus is rate-limited (or vice versa).
                _user_pool_available: Dict[Tuple[str, str | None], bool] = {}
                # R5-W5 MED-5: cache Project→user_id within a single dispatch
                # cycle. When task.launched_by_user_id is NULL, the previous
                # implementation called session.get(Project, ...) per task —
                # 50 NULL-launcher candidates = 50 redundant Project SELECTs
                # every cycle.
                _project_user_cache: Dict[uuid.UUID, str] = {}
                # Pre-warm the project cache for ALL candidate tasks' projects
                # so the eligibility loop and dispatch section never issue per-task
                # Project SELECTs (covers both null-launcher tasks and the fallback
                # path at dispatch-time user-owner resolution).
                _all_candidate_pids = {t.project_id for t in candidate_tasks}
                if _all_candidate_pids:
                    _proj_rows = (
                        await session.execute(
                            select(Project.id, Project.user_id).where(
                                Project.id.in_(_all_candidate_pids)
                            )
                        )
                    ).all()
                    for _pid, _uid in _proj_rows:
                        _project_user_cache[_pid] = _uid

                async def _user_has_available_account(task: Task) -> bool:
                    user_id = task.launched_by_user_id
                    if not user_id:
                        cached = _project_user_cache.get(task.project_id)
                        if cached is not None:
                            user_id = cached
                        else:
                            proj = await session.get(Project, task.project_id)
                            user_id = proj.user_id if proj else ""
                            _project_user_cache[task.project_id] = user_id
                    model = _extract_task_model(task)
                    cache_key = (user_id, model)
                    if cache_key in _user_pool_available:
                        return _user_pool_available[cache_key]
                    pool = _max_plan_pools.get(user_id, [])
                    available = any(
                        _account_available_for_model(a, model) for a in pool
                    )
                    _user_pool_available[cache_key] = available
                    return available

                # Step 2: Filter out tasks with unmet dependencies (no lock needed)
                #
                # Batch-fetch ALL dependency rows across EVERY candidate in ONE
                # IN(...) query (mirrors the EVALUATING sweep at services.py:1006
                # and the deadlock scan below). The previous implementation issued
                # one ``select(Task).where(Task.id.in_(dep_uuids))`` per candidate
                # with deps — 50 tasks x 2-5 deps = 50-250 SELECTs every cycle.
                # Now it's one.
                #
                # The dep rows are read in a SEPARATE read-only session and then
                # DETACHED (expunge_all) before the loop runs. The per-task
                # retry_after / infra_wait blocks below commit on the MAIN
                # ``session``, and a commit expires every instance loaded in that
                # session; loading the dep rows in their own session and detaching
                # them keeps their already-loaded column values (status / context /
                # payloads, read via first_payload) intact for the downstream
                # ``_check_candidate_task_deps`` reads — load_only projects exactly
                # the columns those reads need, so accessing them on the detached
                # instance returns the cached value rather than firing a sync
                # lazy-load → MissingGreenlet (the rollback-expiry / detached-read
                # trap in reference_load_only_lazy_load_trap.md). The
                # replacement-clone lookup inside _check_candidate_task_deps still
                # uses the main ``session`` (unchanged) — it's rare, not part of
                # the N+1, and returns a fresh attached row.
                # Per-candidate validation (with error logging) is still re-run
                # in the loop below; collect_dep_uuids silently skips malformed
                # ids here so one bad row can't poison the shared batch.
                _all_dep_uuids = collect_dep_uuids(candidate_tasks)

                _dep_by_id: dict[uuid.UUID, Task] = {}
                if _all_dep_uuids:
                    # Column projection for the dep batch: _check_candidate_task_deps
                    # reads status, context (_replaced_by_task_id / has_landed_signal /
                    # is_genuine_code_dependency / verify_pr_merged), parent_task_id
                    # (lineage-parent landed fallback), and payloads (first_payload for
                    # _same_base_branch + task-type checks).
                    # title is included so the cascade-cancel alert (raised from a
                    # separate session against the DETACHED prereq row) can read
                    # prereq_task.title without a raiseload DetachedInstanceError.
                    # Drops plan_output / status_log / depends_on and other heavy JSONB.
                    # Rows are detached (expunge_all) so loaded columns remain accessible
                    # without triggering async lazy-loads (MissingGreenlet). raiseload=True
                    # catches accidental projection-widening during development.
                    _dep_light = load_only(
                        Task.id,
                        Task.status,
                        Task.context,
                        Task.payloads,
                        Task.project_id,
                        Task.parent_task_id,
                        Task.title,
                        raiseload=True,
                    )
                    async with AsyncSessionLocal() as _dep_batch_session:
                        _dep_rows = (
                            (
                                await _dep_batch_session.execute(
                                    select(Task)
                                    .where(Task.id.in_(_all_dep_uuids))
                                    .options(_dep_light)
                                )
                            )
                            .scalars()
                            .all()
                        )
                        _dep_by_id = {dt.id: dt for dt in _dep_rows}
                        # Detach so the rows survive the main session's per-task
                        # commits below with their loaded columns intact.
                        _dep_batch_session.expunge_all()

                eligible_ids = []
                # Accumulate cascade-cancel counts per failed prerequisite across
                # this single dependency-resolution pass so a root failure that
                # cascades through A→B→C raises exactly ONE operator alert per
                # distinct failed prerequisite (flushed after the loop), rather
                # than one alert per cancelled dependent.
                _cascade_cancel_by_prereq: dict[uuid.UUID, dict] = {}
                # Batch collect status-message updates for pending-dep tasks so
                # they can be flushed in one query after the loop instead of N
                # individual _dep_session.get() calls (one per pending-dep task).
                _pending_dep_msg_updates: list[tuple[uuid.UUID, str]] = []
                from app.failure_classification import (
                    is_non_death_cancellation as _is_non_death_cancellation,
                )

                for task in candidate_tasks:
                    # Skip tasks held back due to retry_after hold (audit W1:
                    # ``retry_after_until`` is a column now, so a stale
                    # in-memory copy can't shadow a fresh DB value).
                    if task.retry_after_until and task.retry_after_until > _utcnow():
                        # Release rate-limit holds early when the dispatching
                        # user's pool has recovered. _infra_wait holds wait
                        # for zone recovery.
                        if not (task.context or {}).get(
                            "_infra_wait"
                        ) and await _user_has_available_account(task):
                            locked_task = await _lock_task_for_mutation(
                                session,
                                task.id,
                                allowed_statuses=(TaskStatus.QUEUED,),
                                reason="retry_after release",
                            )
                            if locked_task is None:
                                continue
                            locked_task.context = strip_fresh_attempt_context(
                                locked_task.context or {}
                            )
                            _clear_retry_after_state(locked_task)
                            session.add(locked_task)
                            await session.commit()
                            task = locked_task
                            logger.info(
                                f"[ORCHESTRATOR] Task {task.id} retry_after cleared early — account pool recovered"
                            )
                        else:
                            logger.info(
                                f"[ORCHESTRATOR] Task {task.id} held back — retry_after={task.retry_after_until.isoformat()}"
                            )
                            continue
                    # Clear infra-wait flag when task becomes eligible again
                    if (task.context or {}).get("_infra_wait"):
                        locked_task = await _lock_task_for_mutation(
                            session,
                            task.id,
                            allowed_statuses=(TaskStatus.QUEUED,),
                            reason="infra_wait clear",
                        )
                        if locked_task is None:
                            continue
                        ctx = dict(locked_task.context or {})
                        ctx.pop("_infra_wait", None)
                        ctx = strip_fresh_attempt_context(ctx)
                        locked_task.context = ctx
                        _clear_retry_after_state(locked_task)
                        session.add(locked_task)
                        await session.commit()
                        task = locked_task

                    deps = task.depends_on or []
                    if not deps:
                        eligible_ids.append(task.id)
                        continue

                    # Check all dependencies are COMPLETED with PR merged
                    try:
                        dep_uuids = [uuid.UUID(d) for d in deps]
                    except (ValueError, AttributeError) as e:
                        logger.error(
                            f"[ORCHESTRATOR] Task {task.id} has invalid dependency UUID: {e}",
                            exc_info=True,
                        )
                        continue  # Don't dispatch — deps can't be validated
                    # Look the dep rows up from the single batch fetched above
                    # (was one SELECT per candidate — the N+1 this fix removes).
                    dep_tasks = [_dep_by_id[du] for du in dep_uuids if du in _dep_by_id]

                    # Detect deleted dependencies — treat as failed to prevent premature dispatch
                    if len(dep_tasks) < len(dep_uuids):
                        found_ids = {str(dt.id) for dt in dep_tasks}
                        missing = [d for d in deps if d not in found_ids]
                        logger.error(
                            f"[ORCHESTRATOR] Task {task.id} has {len(missing)} deleted dependencies: {missing}"
                        )

                    dep_result_str, deps_remapped = await _check_candidate_task_deps(
                        task, dep_tasks, dep_uuids, session, _verify_pr_merged
                    )
                    all_met = dep_result_str == "eligible"
                    any_failed = dep_result_str == "cascade_cancel"

                    # Persist remap of requeue-cancelled dep IDs → replacement clone IDs
                    # so future orchestrator cycles skip the _replaced_by_task_id lookup.
                    if deps_remapped and not any_failed:
                        if await _persist_requeue_dep_remap(task, deps_remapped):
                            logger.info(
                                f"[ORCHESTRATOR] Remapped {len(deps_remapped)} requeue-cancelled "
                                f"dep(s) for task {task.id}"
                            )

                    if any_failed:
                        # Single source of truth: same cascade-cancel policy the
                        # deadlock-breaker sweep applies (its own session, child
                        # cascade, slot release, VM teardown). Returns the number
                        # of tasks cancelled (dependent + cascaded children).
                        _cancelled_count = await _cascade_cancel_dependent(
                            task.id, task.project_id, broadcast_to_project
                        )
                        # Accumulate the cascade against the failed prerequisite
                        # so a single deduplicated operator alert is raised once
                        # after this dispatch pass. Re-derive the failed
                        # prerequisite locally (mirrors _check_candidate_task_deps'
                        # cancel decision: first terminal-failed dep that is a
                        # genuine death — not SCOPE_EMPTY, co-deferred, or
                        # requeue-replaced).
                        _failed_prereq = next(
                            (
                                dt
                                for dt in dep_tasks
                                if dt.status
                                in (
                                    TaskStatus.FAILED,
                                    TaskStatus.CANCELLED,
                                )
                                and not _is_non_death_cancellation(dt)
                            ),
                            None,
                        )
                        if _failed_prereq is not None:
                            _entry = _cascade_cancel_by_prereq.setdefault(
                                _failed_prereq.id,
                                {
                                    "prereq_task": _failed_prereq,
                                    "count": 0,
                                },
                            )
                            _entry["count"] += _cancelled_count
                    elif all_met:
                        # A cross-base GENUINE code-dependent must run against a
                        # base that provably contains its landed dependency's
                        # code, so re-cut its staging/rollup base from current
                        # integration BEFORE stamping the warm-claim marker.
                        # Stamping _deps_satisfied makes the task warm-claimable
                        # via the /next-role backlog scan; if we stamped first and
                        # the re-cut then conflicted/errored, a warm VM could claim
                        # and dispatch it against a base MISSING the landed code
                        # (the exact invariant this guards). Resolve effective
                        # (remap-following) dep rows so a requeue-replaced
                        # dependency is recognized as landed. No-op for tasks with
                        # no landed cross-base code-dep (returns True immediately).
                        effective_dep_tasks = [
                            await _effective_dep_task(dt, session) for dt in dep_tasks
                        ]
                        if not await ensure_dependent_base_recut(
                            task, effective_dep_tasks, session
                        ):
                            # Re-cut required but incomplete (merge conflict /
                            # transient GitHub error) — keep HOLDING: do not stamp
                            # the marker and do not dispatch. The task stays QUEUED
                            # and is retried next cycle (a persistent conflict
                            # surfaces for a fix task / operator).
                            logger.warning(
                                "[CROSS-BASE] Holding dependent %s — base re-cut "
                                "incomplete; not marking warm-claimable",
                                task.id,
                            )
                            continue
                        # Durably stamp the sticky readiness marker so the cheap
                        # /next-role warm-backlog scan and the dispatch
                        # first-refusal hold can treat this dependency-bearing
                        # task as warm-claimable WITHOUT re-running the
                        # (GitHub-aware) dependency / PR-merge resolution on the
                        # hot path. Only reached on the all_met path — a failed
                        # dependency cascade-cancels the dependent above, so the
                        # marker can never outlive an unsatisfied dependency.
                        await _persist_deps_satisfied(task)
                        eligible_ids.append(task.id)
                    else:
                        # Surface dependency-wait reason to UI
                        pending = [
                            str(dt.id)[:8]
                            for dt in dep_tasks
                            if dt.status
                            not in (
                                TaskStatus.COMPLETED,
                                TaskStatus.FAILED,
                                TaskStatus.CANCELLED,
                            )
                        ]
                        dep_msg = f"Waiting on {len(pending)} dep{'s' if len(pending) != 1 else ''}: {', '.join(pending[:3])}"
                        if pending and task.latest_status_message != dep_msg:
                            # Defer the DB write — batch-flush below in one
                            # session/query instead of N individual queries.
                            _pending_dep_msg_updates.append((task.id, dep_msg))

                # Batch flush pending-dep status-message updates: one query for
                # all tasks instead of N individual _dep_session.get() calls.
                if _pending_dep_msg_updates:
                    try:
                        async with AsyncSessionLocal() as _dep_batch_session:
                            dep_ids = [tid for tid, _ in _pending_dep_msg_updates]
                            _dep_rows = (
                                (
                                    await _dep_batch_session.execute(
                                        select(Task).where(Task.id.in_(dep_ids))
                                    )
                                )
                                .scalars()
                                .all()
                            )
                            _dep_task_by_id = {t.id: t for t in _dep_rows}
                            _to_add = []
                            for tid, msg in _pending_dep_msg_updates:
                                _t = _dep_task_by_id.get(tid)
                                if _t is not None and _t.latest_status_message != msg:
                                    _t.latest_status_message = msg
                                    _to_add.append(_t)
                            if _to_add:
                                for _t in _to_add:
                                    _dep_batch_session.add(_t)
                                await _dep_batch_session.commit()
                    except Exception:
                        logger.exception(
                            "[ORCHESTRATOR] Failed to batch flush dep wait messages "
                            "for %d tasks",
                            len(_pending_dep_msg_updates),
                        )

                # Flush accumulated cascade-cancels: raise ONE deduplicated
                # operator alert per distinct failed prerequisite that cancelled
                # dependents during this dependency-resolution pass.
                if _cascade_cancel_by_prereq:
                    async with AsyncSessionLocal() as _cascade_alert_session:
                        for _entry in _cascade_cancel_by_prereq.values():
                            try:
                                await _maybe_create_cascade_cancel_alert(
                                    _cascade_alert_session,
                                    _entry["prereq_task"].project_id,
                                    _entry["prereq_task"],
                                    _entry["count"],
                                )
                            except Exception:
                                logger.exception(
                                    "[ORCHESTRATOR] Failed to raise cascade-cancel "
                                    "alert for prereq %s",
                                    _entry["prereq_task"].id,
                                )

                # Step 2.4: Deadlock detection — break circular deps among stuck QUEUED tasks
                # (_last_deadlock_check declared global in the idle-cycle branch above)
                now = _utcnow()
                if _is_cadence_due(_last_deadlock_check, 60, now):
                    _last_deadlock_check = now
                    stuck_tasks = [
                        t
                        for t in candidate_tasks
                        if t.id not in eligible_ids
                        and (t.depends_on or [])
                        and t.status == TaskStatus.QUEUED
                    ]
                    narrow_broken = 0
                    if stuck_tasks:
                        stuck_ids = {t.id for t in stuck_tasks}

                        # Batch all dependency UUIDs across all stuck tasks into one query
                        all_dep_ids: set[uuid.UUID] = set()
                        task_dep_uuids: dict = {}
                        for stask in stuck_tasks:
                            dep_uuids: set[uuid.UUID] = set()
                            for d in stask.depends_on or []:
                                try:
                                    dep_uuids.add(uuid.UUID(d))
                                except (ValueError, AttributeError):
                                    continue
                            task_dep_uuids[stask.id] = dep_uuids
                            all_dep_ids.update(dep_uuids)

                        dep_tasks_by_id: dict = {}
                        if all_dep_ids:
                            dep_rows = await session.execute(
                                select(Task).where(Task.id.in_(all_dep_ids))
                            )
                            dep_tasks_by_id = {
                                dt.id: dt for dt in dep_rows.scalars().all()
                            }

                        # Cycle detection + shared dependency-death policy: a true
                        # cycle clears depends_on and dispatches; a genuinely failed
                        # prerequisite cascade-cancels the dependent instead of being
                        # dispatched as impossible work (single source of truth with
                        # the main per-candidate check via _check_candidate_task_deps).
                        classified = await _classify_stuck_tasks(
                            stuck_tasks,
                            task_dep_uuids,
                            dep_tasks_by_id,
                            stuck_ids,
                            session,
                            _verify_pr_merged,
                        )
                        narrow_broken += await _apply_deadlock_classification(
                            classified, broadcast_to_project, eligible_ids
                        )

                    # Broader scan: check QUEUED tasks older than 10 min with
                    # deps (not just candidate batch). Slot-independent — shared
                    # with the idle/paused-cycle recovery pass above.
                    broad_broken = await _recover_stale_dependency_deadlocks(
                        session,
                        exclude_task_ids={t.id for t in candidate_tasks},
                        eligible_ids=eligible_ids,
                    )

                    total_broken = narrow_broken + broad_broken
                    if total_broken > 0:
                        logger.warning(
                            f"[ORCHESTRATOR] Deadlock resolution cycle complete: "
                            f"{narrow_broken} narrow + {broad_broken} broad = {total_broken} deadlock(s) broken"
                        )

                # Step 2.45: Re-cut held cross-rollup code-dependents' bases from
                # current integration. A dependent held until its genuine
                # cross-base code-dependency landed must run against a base that
                # provably contains that landed code; this merges integration
                # into the dependent's staging/rollup base (preserving the rollup
                # model). A dependent whose re-cut conflicts or errors is dropped
                # from this cycle's eligible set so it never dispatches against a
                # base missing its dependency's code — it stays QUEUED and retries.
                eligible_ids = await recut_landed_cross_base_dependents(
                    eligible_ids, candidate_tasks, _dep_by_id, session
                )

                # Step 2.5: File-conflict check — defer tasks that would conflict with in-progress work
                # Parked BLOCKED file-conflict tasks are absent from the QUEUED
                # candidate scan, but they still hold a seniority reservation on
                # their contested files (so a stream of newer same-file tasks
                # cannot grab the lock while a long-waiting task is parked). Load
                # them light — only the columns filter_file_conflicts reads
                # (context/parent_task_id/created_at/blocker_kind) — to keep this
                # async-safe (no MissingGreenlet lazy-load) and cheap.
                blocked_conflict_tasks = []
                if eligible_ids:
                    blocked_conflict_tasks = (
                        (
                            await session.execute(
                                select(Task)
                                .where(
                                    Task.status == TaskStatus.BLOCKED,
                                    Task.blocker_kind == "file_conflict",
                                )
                                .options(
                                    load_only(
                                        *BLOCKED_CONFLICT_LIGHT_COLUMNS, raiseload=True
                                    )
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                eligible_ids = await filter_file_conflicts(
                    eligible_ids,
                    candidate_tasks,
                    active_tasks_result,
                    session,
                    broadcast_to_project,
                    blocked_conflict_tasks,
                )

                # Step 2.6: Defer tasks that overlap with pending-rollup files
                eligible_ids = await filter_rollup_conflicts(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.7: Defer tasks whose direction is held due to consecutive code failures
                eligible_ids = await filter_held_directions(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.72: Reserve a provisional alembic revision id for each
                # migration-producing candidate instead of serializing them. The
                # allocator locks distinct monotonic ids per project, so siblings
                # no longer wait behind a holder's full execution/test lifecycle —
                # the slot releases the moment numbering is locked. Returns
                # eligible_ids unchanged (no deferral); the reserved id rides the
                # agent claim and is consumed verbatim.
                eligible_ids = await reserve_migration_revisions(
                    eligible_ids,
                    candidate_tasks,
                    session,
                )

                # Step 2.75: Pre-dispatch dedup safety net for review/fix pipeline
                # tasks. Catches duplicates that raced past _spawn_pipeline_task's
                # spawn-time dedup (TOCTOU on active-task query, or different
                # original_task_id resolutions for the same effective chain).
                # Never cancels the sole survivor — see filter docstring.
                eligible_ids = await filter_pipeline_duplicates(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.76: SCOPE_EMPTY pre-flight — pre-empt auto-decomposed
                # cluster siblings whose related_files are fully covered by a
                # completed sibling. Transitions directly to FAILED/SCOPE_EMPTY
                # (an INFRA_CLASS) so the retry budget is NOT consumed.
                eligible_ids = await filter_scope_empty(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.8: Health-aware reordering — boost small/low-risk tasks when degraded.
                # Only activates under degradation so normal priority ordering is preserved.
                # Degradation signals: any zone in cooldown, failure rate > 30%, or slots < 50% of max.
                # Extracted to orchestrator_health.reorder_eligible_by_health; predict_spawn_success
                # is injected to keep that seam free of a spawn_calibration import.
                from app.spawn_calibration import predict_spawn_success

                eligible_ids = await reorder_eligible_by_health(
                    eligible_ids,
                    candidate_tasks,
                    recent_results,
                    slots_available,
                    _runtime_config,
                    get_zone_health_summary(),
                    session,
                    predict_spawn_success,
                )

                # Step 2.9: Pipeline-slot cap — review/fix chains are self-perpetuating
                # (review spawns fix spawns review, up to 16 iterations per PR), so
                # without a ceiling they can fill every VM slot and starve create/
                # research tasks. Defer (keep QUEUED) review/fix tasks once the
                # configured pipeline budget is exhausted.
                eligible_ids = await cap_pipeline_slots(
                    eligible_ids, candidate_tasks, _runtime_config, session
                )

                # Step 2.92: Per-project concurrency cap — optional operator
                # envelope that bounds how many CONCURRENT VMs (PROVISIONING or
                # IN_PROGRESS tasks) a single project may hold. Candidates from a
                # project at-or-over its cap are deferred (stay QUEUED, never
                # failed) so another project's candidate can take the free slot.
                # Projects with no configured cap are never filtered — behavior
                # is byte-identical for existing unconfigured projects. Applied
                # AFTER the pipeline-slot cap and BEFORE fairness so the fairness
                # reorder never re-admits an already-capped project.
                _active_by_project: dict = {}
                for _t in active_tasks_result:
                    _pid = _t.project_id
                    _active_by_project[_pid] = _active_by_project.get(_pid, 0) + 1
                _cap_by_project_raw = (
                    _runtime_config.get("max_concurrent_tasks_by_project") or {}
                )
                _cap_by_project: dict = {}
                for _pid_str, _cap_val in _cap_by_project_raw.items():
                    try:
                        _cap_int = int(_cap_val)
                    except (TypeError, ValueError):
                        continue
                    if _cap_int > 0:
                        _cap_by_project[_pid_str] = _cap_int
                if _cap_by_project:
                    _str_project_by_id = {
                        t.id: str(t.project_id) for t in candidate_tasks
                    }
                    _str_active_by_project = {
                        str(pid): cnt for pid, cnt in _active_by_project.items()
                    }
                    _before_cap = len(eligible_ids)
                    eligible_ids = apply_project_concurrency_cap(
                        eligible_ids,
                        _str_project_by_id,
                        _str_active_by_project,
                        _cap_by_project,
                    )
                    _deferred_cap = _before_cap - len(eligible_ids)
                    if _deferred_cap:
                        logger.info(
                            "[ORCHESTRATOR] Per-project cap: deferred %d candidate(s) "
                            "from at-cap projects",
                            _deferred_cap,
                        )

                # Step 2.95: Per-project fairness — round-robin the eligible
                # tasks across their owning projects BEFORE the slot cap below.
                # The candidate scan orders globally by (priority desc,
                # created_at asc); without fairness a single autonomous project
                # that fanned out dozens of tasks fills the entire dispatch
                # window (eligible_ids[:slots_available]) and starves every
                # other project for multiple cycles. Weighted round-robin keeps
                # each project's internal order intact (per-project priority/age
                # is untouched) and is a pure permutation — every eligible task
                # stays dispatchable, so this is behavior-preserving for valid
                # dispatch and only changes *whose* task wins a contended slot.
                # Weight is derived from each project's highest queued priority
                # (no dedicated tier column exists) so urgent projects still get
                # proportionally more turns without monopolizing the window.
                _project_by_id = {t.id: t.project_id for t in candidate_tasks}
                _fairness_weight = {
                    t.id: _project_fairness_weight(t.priority) for t in candidate_tasks
                }
                eligible_ids = apply_project_fairness(
                    eligible_ids, _project_by_id, _fairness_weight
                )

                # Step 2.97: Finish-before-start — reorder eligible tasks so
                # tree-completing work wins scarce slots over brand-new work.
                # Tier order: (1) rollup-PR reviews/fixes (whole-tree release),
                # (2) ordinary review/fix, (3) children of already-running plans,
                # (4) new research / top-level work. Pure permutation — no tasks
                # are dropped. The downstream slot reservation still provides the
                # anti-starvation floor for create/research.
                _active_task_ids = {t.id for t in active_tasks_result}
                eligible_ids = await reorder_by_completion_priority(
                    eligible_ids,
                    candidate_tasks,
                    _active_task_ids,
                    session,
                )

                # Slot reservation: ensure high-priority task types (e.g. create,
                # research) get a minimum share of slots when they're queued behind
                # self-perpetuating review/fix chains. Only activates when queue
                # pressure exists (more eligible tasks than open slots) and there
                # are reserved-type tasks waiting beyond the dispatch cutoff.
                eligible_ids = reserve_slots_for_types(
                    eligible_ids, candidate_tasks, _runtime_config, slots_available
                )

                # Monthly budget gate: check BEFORE the resource dispatch filter
                # so budget_exhausted is available for per-project deferral
                # decisions. Fail-open (any exception silently passes through).
                try:
                    budget_exhausted, usage_ratio = await check_monthly_budget(session)
                except Exception:
                    budget_exhausted = False
                    logger.exception(
                        "[ORCHESTRATOR] Monthly budget check failed — treating as not exhausted"
                    )

                eligible_ids = await _filter_project_resource_dispatch_eligible(
                    session,
                    eligible_ids,
                    candidate_tasks,
                    project_active_counts,
                    broadcast_to_project,
                    budget_exhausted=budget_exhausted,
                )

                # Fleet-wide monthly-budget informational broadcast (follows
                # the zone-exhaustion pattern). Per-project deferral messaging
                # and state transitions are handled inside
                # _filter_project_resource_dispatch_eligible above.
                if budget_exhausted:
                    _shared_state.monthly_budget_gate_engaged = True
                    if _is_cadence_due(_last_budget_gate_log, 60, _utcnow()):
                        _last_budget_gate_log = _utcnow()
                        logger.info(
                            "[ORCHESTRATOR] Monthly budget exhausted (%.1f%% of target) — deferring eligible tasks",
                            usage_ratio * 100 if usage_ratio else 100.0,
                        )
                        try:
                            await ws_manager.broadcast_to_all(
                                "pipeline_throttle",
                                {
                                    "reason": "monthly_budget",
                                    "detail": "Monthly budget target exhausted",
                                },
                            )
                        except Exception:
                            logger.exception(
                                "[ORCHESTRATOR] Failed to broadcast monthly_budget throttle"
                            )
                elif not budget_exhausted:
                    _shared_state.monthly_budget_gate_engaged = False

                # Respect slot limit after conflict filtering
                eligible_ids = eligible_ids[:slots_available]

                # Step 3: Re-read ONLY the tasks we're about to dispatch. This
                # scan is read-only; the row is locked with task_lock() at the
                # top of the per-task mutation block below.
                # R4-W2 (H4): same load_only filter as the candidate fetch
                # — heavy JSONB columns aren't needed for the dispatch gate
                # work either. ``status_log`` retained for event-listener
                # reads on Task.status mutations downstream.
                queued_tasks = []
                if eligible_ids:
                    dispatch_stmt = (
                        select(Task)
                        .join(Project, Task.project_id == Project.id)
                        .where(
                            Task.id.in_(eligible_ids),
                            Task.status == TaskStatus.QUEUED,
                            Project.paused.is_(False),
                            Project.not_archived(),
                        )
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.priority,
                                Task.created_at,
                                Task.requires_approval,
                                Task.retry_after_until,
                                Task.context,
                                Task.depends_on,
                                Task.launched_by_user_id,
                                Task.project_id,
                                Task.payloads,
                                Task.status_log,
                                Task.latest_status_message,
                                Task.parent_task_id,  # see candidate_stmt comment above,
                                raiseload=True,
                            )
                        )
                    )
                    queued_tasks = list(
                        (await session.execute(dispatch_stmt)).scalars().all()
                    )

                logger.info(
                    f"[ORCHESTRATOR] Cycle: {len(queued_tasks)} queued, {active_count} active, {slots_available} slots, pool={len(_account_by_id)} accounts"
                )
                # Snapshot per-task gate fields BEFORE entering the dispatch loop.
                # Inside the loop, _lock_task_for_mutation calls session.rollback()
                # on contention/status mismatch, which expires every attribute on
                # every object loaded in this session — including queued_task.payloads.
                # The next iteration's queued_task.first_payload would then trigger a
                # sync lazy-load from async context (bug pattern documented at
                # ACTIVE_TASKS_LIGHT_COLUMNS comment above) and crash the cycle.
                _queued_task_gate_info: list[tuple] = [
                    (qt.id, qt.first_payload.get("type", "")) for qt in queued_tasks
                ]
                # Gauge query: skip autoflush so pending UPDATEs from earlier in the
                # cycle don't get pushed and contend on a row another transaction holds
                # (LockNotAvailableError). The count is a snapshot for metrics, not a
                # transactional read — pending state being absent is the right answer.
                # session.no_autoflush is a sync context manager (toggles a flag,
                # no I/O), so use plain ``with`` even on an AsyncSession.
                with session.no_autoflush:
                    _shared_state.tasks_queued_gauge = (
                        await session.execute(
                            select(_func.count())
                            .select_from(Task)
                            .where(
                                Task.status == TaskStatus.QUEUED,
                                Task.requires_approval.is_(False),
                            )
                        )
                    ).scalar() or 0
                # tasks_active_gauge already set above after active_count is computed

                # Zone pre-check: skip dispatch entirely if all GCP zones are in cooldown
                if queued_tasks and not any_zone_available():
                    global _last_zone_exhaustion_log
                    # _last_zone_exhaustion_log is a module-level gauge written
                    # only here; keep it naive (_utcnow) for a consistent
                    # self-comparison. The recovery-wait subtraction itself lives
                    # in zone_recovery_wait_seconds() which does the aware math
                    # against the tz-aware soonest_zone_recovery() (Audit C10 —
                    # previously this subtracted a naive _utcnow() and raised
                    # TypeError, firing this 60s-gated error path repeatedly).
                    now_check = _utcnow()
                    if _is_cadence_due(_last_zone_exhaustion_log, 60, now_check):
                        _last_zone_exhaustion_log = now_check
                        wait_sec = zone_recovery_wait_seconds()
                        if wait_sec is not None:
                            logger.info(
                                f"[ORCHESTRATOR] All zones in cooldown — skipping dispatch (recovery in {int(wait_sec)}s)"
                            )
                        else:
                            logger.info(
                                "[ORCHESTRATOR] All zones in cooldown — skipping dispatch"
                            )
                        try:
                            await ws_manager.broadcast_to_all(
                                "pipeline_throttle",
                                {
                                    "reason": "zone_exhaustion",
                                    "detail": "All GCP zones in cooldown",
                                    "recovery_seconds": int(wait_sec)
                                    if wait_sec is not None
                                    else None,
                                },
                            )
                        except Exception:
                            logger.exception(
                                "[ORCHESTRATOR] Failed to broadcast zone_exhaustion throttle"
                            )
                    queued_tasks = []  # Clear so the loop below is skipped

                # All-types-blocked detection: if every queued task's type is in _failing_types,
                # the pipeline is silently stalled — create an operator alert.
                if _failing_types and queued_tasks:
                    _queued_types = {
                        t.first_payload.get("type", "")
                        for t in queued_tasks
                        if t.first_payload.get("type", "")
                    }
                    if _queued_types and _queued_types.issubset(_failing_types):
                        _blocked_summary = ", ".join(sorted(_queued_types))
                        logger.warning(
                            f"[ORCHESTRATOR] All queued task types blocked by per-type failure gate: {_blocked_summary}"
                        )
                        try:
                            from app.models import Alert, AlertSeverity

                            _all_blocked_title = (
                                "Pipeline stalled — all task types throttled"
                            )
                            _all_blocked_msg = (
                                f"All queued task types are blocked by the per-type failure gate: {_blocked_summary}. "
                                "Each type has >70% failure rate in the last 15 minutes. The pipeline will not dispatch "
                                "new tasks until failure rates drop."
                            )
                            # Load already-alerted project IDs once so the batched
                            # scan below avoids an N+1 existence check per project.
                            _alerted_project_ids = set(
                                (
                                    await session.execute(
                                        select(Alert.project_id).where(
                                            Alert.task_id.is_(None),
                                            Alert.severity == AlertSeverity.ERROR,
                                            Alert.dismissed_at.is_(None),
                                            Alert.title == _all_blocked_title,
                                        )
                                    )
                                )
                                .scalars()
                                .all()
                            )
                            # Batch the project scan in chunks of 500 with a stable
                            # ORDER BY so memory/lock duration stay bounded and no
                            # project is skipped or duplicated across batches.
                            _ab_batch = 0
                            _ab_batch_size = 500
                            while True:
                                _all_project_ids = list(
                                    (
                                        await session.execute(
                                            select(Project.id)
                                            .order_by(Project.id)
                                            .offset(_ab_batch * _ab_batch_size)
                                            .limit(_ab_batch_size)
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
                                if not _all_project_ids:
                                    break
                                for _proj_id in _all_project_ids:
                                    if _proj_id in _alerted_project_ids:
                                        continue
                                    _alert = Alert(
                                        project_id=_proj_id,
                                        severity=AlertSeverity.ERROR,
                                        title=_all_blocked_title,
                                        message=_all_blocked_msg,
                                    )
                                    session.add(_alert)
                                    await broadcast_to_project(
                                        str(_proj_id),
                                        "alert_created",
                                        {
                                            "alert_id": str(_alert.id),
                                            "severity": "ERROR",
                                            "title": _all_blocked_title,
                                        },
                                    )
                                _ab_batch += 1
                        except Exception:
                            logger.exception(
                                "[ORCHESTRATOR] Failed to create all-types-blocked project alerts"
                            )

                # Track per-cycle counts so a full stall on Max-plan account exhaustion
                # fires an alert+broadcast once, rather than only per-task retry_after.
                _cycle_accounts_blocked = 0
                _cycle_dispatched = 0
                _cycle_earliest_recovery: datetime | None = None
                _cycle_blockage_reasons: set[str] = set()

                # Pull-based fleet runners: load live registered runners ONCE per
                # cycle (keyed by project_id) so the per-task runner-dispatch check
                # never issues a DB query. Empty dict when none are registered —
                # the common case, in which all dispatch paths run unchanged.
                _active_runners_cache = await _load_active_runners_cache(session)
                # Per-cycle pin counter ({runner_id: pins_made_this_pass}).
                # Declared inside the cycle body so it RESETS every dispatch pass
                # — it must count only pins made in THIS cycle, since the runner's
                # heartbeat-reported active count catches up between cycles.
                # _try_runner_dispatch reads it (capacity check) and increments it
                # on each committed pin so one idle runner is never over-pinned
                # beyond its max_concurrent in a single burst.
                _runner_pins_this_cycle: dict[uuid.UUID, int] = {}
                # Per-cycle project/spend caches for the resource-envelope check
                # in the per-task dispatch loop below.  Avoids redundant DB queries
                # (get_project_recent_spend_usd, session.get(Project)) when multiple
                # tasks from the same project are dispatched in one cycle — mirrors
                # the caching pattern in _filter_project_resource_dispatch_eligible.
                # Populate full Project rows in one round trip before the task
                # loop. The earlier Project->user_id cache is only a projection
                # and cannot serve the resource-envelope checks below.
                # Pre-seeding absent ids with None also prevents a concurrently
                # deleted project from falling back to a point lookup.
                _project_cache_for_dispatch: dict[uuid.UUID, Project | None] = {
                    project_id: None for project_id in _all_candidate_pids
                }
                if _all_candidate_pids:
                    _dispatch_projects = (
                        (
                            await session.execute(
                                select(Project).where(
                                    Project.id.in_(_all_candidate_pids)
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    _project_cache_for_dispatch.update(
                        {project.id: project for project in _dispatch_projects}
                    )
                _spend_cache_for_dispatch: dict[uuid.UUID, tuple[float, float, int]] = {}

                for _qt_id, _task_type_for_gate in _queued_task_gate_info:
                    # Per-type failure gate: skip tasks whose type is currently failing
                    # (global 80% backstop already handled above; this skips only the bad type).
                    # Use the snapshot tuple — never read .first_payload off a session-bound
                    # object inside this loop, since _lock_task_for_mutation's rollback would
                    # expire it and trigger a sync lazy-load that crashes the cycle.
                    if _task_type_for_gate and _task_type_for_gate in _failing_types:
                        logger.info(
                            f"[ORCHESTRATOR] Skipping task {_qt_id} "
                            f"(type={_task_type_for_gate}) — per-type failure gate active"
                        )
                        continue

                    # Per-task dispatch containment: everything from the
                    # dispatch claim to the provision launch runs inside this
                    # boundary. A crash preparing ONE task is logged, the
                    # iteration's pre-claimed LLM-account slot is released, the
                    # session is rolled back, and the loop continues — so
                    # later-ranked tasks and the post-loop stages (CI polling,
                    # stall detection) still run this cycle instead of starving
                    # behind a deterministic poison task. Repeated crashes are
                    # bounded by the quarantine in the handler below. The
                    # cycle-level handler still owns genuinely cycle-wide
                    # failures (this `except Exception` lets CancelledError —
                    # a BaseException — propagate for clean shutdown).
                    #
                    # _iter_preclaimed_account_id tracks the slot
                    # select_max_plan_account pre-increments during
                    # select_llm_runtime_for_task. It is cleared at every point
                    # ownership transfers (warm-pool lease commit, PROVISIONING
                    # commit) or an existing early-exit path already released
                    # the slot, so the handler can never double-release.
                    _iter_preclaimed_account_id: str | None = None
                    _iter_preclaimed_project_id: str | None = None
                    try:
                        task = await _lock_dispatchable_task_for_mutation(
                            session, _qt_id
                        )
                        if task is None:
                            continue
                        if task.requires_approval:
                            await session.rollback()
                            continue
                        # vm_instance_id read via __dict__ after the strategic
                        # path call. _hold_strategic_local_runner_task's
                        # no-change branch calls session.rollback(), which
                        # expires ALL session-bound ORM instances even under
                        # expire_on_commit=False. Under asyncpg reading the
                        # expired descriptor triggers a sync lazy-load →
                        # MissingGreenlet (the same hazard documented in the
                        # dispatch-prep handler comments above). Reading via
                        # __dict__ avoids the descriptor's lazy-load trap.
                        if await _try_strategic_local_required_runner_path(
                            session,
                            task,
                            _active_runners_cache,
                            _runner_pins_this_cycle,
                        ):
                            # expire_on_commit=False preserves in-memory values
                            # through a commit, so the runner-dispatch path has
                            # vm_instance_id="runner:{id}" accessible. Only the
                            # rollback path expires the instance, and that path
                            # never writes "runner:" — so reading through __dict__
                            # (which returns None for expired/missing keys) is
                            # safe and avoids the lazy-load → MissingGreenlet.
                            if str(
                                task.__dict__.get("vm_instance_id")
                                or ""
                            ).startswith("runner:"):
                                _cycle_dispatched += 1
                            continue
                        resource_defer_msg = (
                            await _project_resource_dispatch_defer_message(
                                session, task, project_active_counts,
                                project_cache=_project_cache_for_dispatch,
                                spend_cache=_spend_cache_for_dispatch,
                            )
                        )
                        if resource_defer_msg:
                            logger.info(
                                "[ORCHESTRATOR] Deferring task %s for project %s: %s",
                                task.id,
                                task.project_id,
                                resource_defer_msg,
                            )
                            if task.latest_status_message != resource_defer_msg:
                                task.latest_status_message = resource_defer_msg
                                session.add(task)
                                await broadcast_to_project(
                                    str(task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": str(task.id),
                                        "status": task.status,
                                        "message": resource_defer_msg,
                                    },
                                )
                            await session.commit()
                            continue

                        # Direct compute_experiment launch (no agent half). A
                        # compute_experiment's agent leg is purely mechanical —
                        # create the ComputeJob + park on its terminal wake, no LLM
                        # and no repo checkout — so the orchestrator does that work
                        # itself here and the task never enters agent dispatch (which
                        # would pin the mechanical leg to a runner that may not run
                        # the agent at all — a silent wedge — and couple the GPU job's
                        # runner pin to the agent task). This branch fully owns
                        # compute_experiment dispatch: runner selection, the
                        # enforce-local hold, and cloud fallback all happen inside it,
                        # so a compute_experiment never reaches _try_runner_dispatch or
                        # the legacy hold path below.
                        if (
                            str((task.first_payload or {}).get("type") or "")
                            == "compute_experiment"
                        ):
                            _ce_outcome = (
                                await _dispatch_compute_experiment_directly(
                                    session,
                                    task,
                                    _active_runners_cache,
                                    _runner_pins_this_cycle,
                                )
                            )
                            if _ce_outcome in ("launched", "failed"):
                                _cycle_dispatched += 1
                            continue

                        # Pull-based fleet runner routing. Checked BEFORE the
                        # warm-pool / GCP dispatch paths: if an eligible live
                        # runner is registered for this project, pin the task to
                        # it (PROVISIONING + vm_instance_id='runner:{id}' +
                        # context['runner_target_id']) and let the runner pull it
                        # via POST /api/runner/next-task. Purely additive — when
                        # no runner is registered this returns False and every
                        # existing dispatch path runs exactly as before. No
                        # LOCAL_MODE flag is involved.
                        if await _try_runner_dispatch(
                            session,
                            task,
                            _active_runners_cache,
                            _runner_pins_this_cycle,
                        ):
                            _cycle_dispatched += 1
                            continue

                        if await _hold_strategic_local_runner_task(session, task):
                            continue

                        # Phase 2c: warm-VM continuity skip-provision branch.
                        # Three outcomes when warm_target_vm_id is set on the task:
                        #   (a) deadline expired → clear warm fields, fall through
                        #       to provision (cold path).
                        #   (b) target VM heartbeated within 90s → skip provision
                        #       entirely. Task stays QUEUED and the agent on the
                        #       target VM will pick it up via /api/agent/next-role.
                        #   (c) target VM unresponsive → clear warm fields, fall
                        #       through to provision (cold path).
                        _warm_ctx = task.context or {}
                        _warm_target_vm_id = _warm_ctx.get("warm_target_vm_id")
                        # A pool pin (warm_pool dispatch hook) reuses the warm_target_vm_id
                        # machinery but is gated on the warm-pool flag/breaker, not
                        # warm-continuity, and releases its leased PoolVm row on clear.
                        _is_pool_pin = bool(_warm_ctx.get("warm_pool_pin"))
                        # A parked-for-retarget task holds the parked VM id in
                        # vm_instance_id (agent_status stamps it there so the reaper
                        # can tear it down if the park budget expires). When the
                        # orchestrator gives up on the warm pin it must reset
                        # vm_instance_id just like pool pins do, so the cold
                        # fall-through mints a FRESH id instead of re-creating a GCP
                        # instance with a stale parked-VM name (name-collision hazard).
                        _is_parked_retarget = bool(_warm_ctx.get("parked_for_retarget"))
                        if _warm_target_vm_id:
                            _warm_deadline_iso = _warm_ctx.get("warm_handoff_deadline")
                            _warm_expired = False
                            if _warm_deadline_iso:
                                try:
                                    _warm_deadline = datetime.fromisoformat(
                                        str(_warm_deadline_iso)
                                    )
                                    if _warm_deadline.tzinfo is not None:
                                        _warm_deadline = _warm_deadline.astimezone(
                                            timezone.utc
                                        ).replace(tzinfo=None)
                                    _warm_expired = _utcnow() > _warm_deadline
                                except (ValueError, TypeError):
                                    _warm_expired = True
                            else:
                                _warm_expired = True

                            if _is_pool_pin:
                                _cb_suppressed = (
                                    not is_warm_pool_enabled()
                                ) or _shared_state.is_warm_pool_suppressed()
                            else:
                                _cb_suppressed = (
                                    _shared_state.is_warm_continuity_suppressed()
                                )
                            # Liveness proxy (WI-5): a <90s heartbeat is NOT proof
                            # of life when the orchestrator has already scheduled
                            # this VM for teardown. A VM that just finished a task
                            # still reads "alive" off its completion POST's
                            # heartbeat while its delete is in flight, so honouring
                            # the pin loops skip-provision ~30x until the 90s
                            # window lapses (observed in prod). Treat an
                            # orchestrator-doomed target as dead so the task falls
                            # through to cold provision immediately.
                            # _warm_target_vm_id is a local snapshot (see above) so
                            # this read is safe even after a session.rollback().
                            from app.vm_lifecycle import (
                                is_orchestrator_initiated_delete,
                            )

                            _vm_doomed, _doom_reason = is_orchestrator_initiated_delete(
                                _warm_target_vm_id
                            )
                            if _warm_pin_still_honorable(
                                warm_expired=_warm_expired,
                                breaker_suppressed=_cb_suppressed,
                                vm_doomed=_vm_doomed,
                                vm_alive=_shared_state.is_vm_alive(_warm_target_vm_id),
                            ):
                                # (b) Skip provision: the warm VM is alive and the
                                # circuit breaker is not tripped. Leave the task
                                # QUEUED — the agent's /next-role poll will claim it
                                # without a fresh provision.
                                logger.info(
                                    f"[ORCHESTRATOR] Warm-pin skip-provision for task {task.id}: "
                                    f"target_vm={_warm_target_vm_id} (heartbeat fresh, deadline {_warm_deadline_iso})"
                                )
                                await session.rollback()
                                continue

                            # (a), (c), or (d): clear warm fields and fall through.
                            _park_keys = (
                                {"parked_for_retarget", "park_deadline"}
                                if _is_parked_retarget
                                else set()
                            )
                            _new_ctx = {
                                k: v
                                for k, v in _warm_ctx.items()
                                if k
                                not in (
                                    "warm_target_vm_id",
                                    "warm_target_pr",
                                    "warm_handoff_deadline",
                                    "warm_pool_pin",
                                    *_park_keys,
                                )
                            }
                            task.context = _new_ctx
                            from sqlalchemy.orm.attributes import flag_modified

                            flag_modified(task, "context")
                            if _cb_suppressed:
                                _clear_reason = "circuit_breaker"
                            elif _warm_expired:
                                _clear_reason = "deadline_expired"
                            elif _vm_doomed:
                                # Orchestrator already tore the target VM down; its
                                # <90s heartbeat is a corpse beat, not liveness.
                                _clear_reason = "vm_doomed"
                            else:
                                _clear_reason = "vm_unresponsive"
                            # A pool pin we give up on releases its leased PoolVm row
                            # (retired so it is never re-leased — it failed to serve
                            # the pinned task in time / is unresponsive). Pool clears
                            # feed the pool breaker, NOT the warm-continuity pin-clear
                            # metric, so the two breakers stay independent.
                            if _is_pool_pin:
                                from app.models import PoolVmState as _PoolVmState

                                await mark_pool_vm_retiring(
                                    session,
                                    _warm_target_vm_id,
                                    source_states=(_PoolVmState.LEASED,),
                                )
                                if _clear_reason != "circuit_breaker":
                                    _shared_state.record_pool_claim_failure(
                                        _clear_reason
                                    )
                                # _pin_task_to_pool_vm stamped this retiring member's
                                # vm_instance_id onto the task. Reset it so the cold
                                # fall-through below mints a FRESH agent-vm- id instead
                                # of re-creating a GCP instance named after the VM we
                                # just scheduled for teardown (name collision racing the
                                # reaper's delete). Chain pins leave vm_instance_id None
                                # so they are unaffected; only pool pins set it.
                                task.vm_instance_id = None
                            elif _is_parked_retarget:
                                # Park-and-retarget stamped the held VM id onto
                                # vm_instance_id (agent_status.py) so the reaper can
                                # track it. When the warm pin is given up (VM
                                # unresponsive, deadline expired, or circuit-breaker
                                # suppressed), reset vm_instance_id so the cold
                                # fall-through below mints a FRESH id — otherwise the
                                # stale parked-VM name races the reaper's delete on GCP.
                                task.vm_instance_id = None
                            # Phase 2e observability: track clear-out rate so we
                            # can detect warm-VM instability. Sustained >5/hour
                            # means the warm chain is failing more than expected.
                            # Defer record_warm_pin_clear until AFTER the commit
                            # that persists the warm-field clear lands — if the
                            # commit fails and we retry next cycle we'd otherwise
                            # double-count the metric (Issue 11).
                            # Circuit-breaker suppressions are not counted as pin-clear
                            # events to avoid a feedback loop that deepens suppression.
                            _pending_warm_pin_clear: tuple[str, str] | None = (
                                None
                                if (_clear_reason == "circuit_breaker" or _is_pool_pin)
                                else (_warm_target_vm_id, _clear_reason)
                            )
                            # Surface the orchestrator's own teardown reason when the
                            # clear was triggered by a doomed target (WI-5) — otherwise
                            # the "vm_doomed" reason alone hides WHY the VM was torn down
                            # (e.g. agent_completion corpse-beat vs an explicit reap).
                            _doom_detail = (
                                f" doom_reason={_doom_reason}" if _vm_doomed else ""
                            )
                            logger.info(
                                f"[ORCHESTRATOR] Warm-pin cleared for task {task.id}: "
                                f"target_vm={_warm_target_vm_id} reason={_clear_reason}{_doom_detail}"
                            )
                        else:
                            _pending_warm_pin_clear = None

                        # Resolve the task's LLM runtime identity and select an eligible
                        # account BEFORE consulting the warm pool, so the pool lease gate
                        # enforces the SAME compatibility the cold path would (provider
                        # preference, category/quality routing, account availability, and
                        # the rate-limit blacklist). Two sources feed the effective
                        # blacklist:
                        #  - blacklisted_accounts: pruneable (rate-limit recovers → reusable)
                        #  - provision_failed_accounts: sticky (silent pre-claim deaths stay
                        #    unavailable for this task — prune logic below would otherwise
                        #    reselect the same broken account every attempt).
                        _blacklist = (task.context or {}).get("blacklisted_accounts")
                        _sticky = (task.context or {}).get(
                            "provision_failed_accounts"
                        ) or []
                        # Launcher pays; fall back to project owner for autonomous tasks.
                        # R6-W3-D: reuse the cycle-local _project_user_cache built during
                        # eligibility filtering so we don't re-issue a Project SELECT per
                        # dispatched task (extends R5-W5 MED-5 from the eligibility scan
                        # to the dispatch site).
                        if task.launched_by_user_id:
                            dispatch_user_id = task.launched_by_user_id
                        else:
                            cached_owner = _project_user_cache.get(task.project_id)
                            if cached_owner is not None:
                                dispatch_user_id = cached_owner
                            else:
                                task_project = await session.get(
                                    Project, task.project_id
                                )
                                dispatch_user_id = (
                                    task_project.user_id if task_project else ""
                                )
                                _project_user_cache[task.project_id] = dispatch_user_id
                        task_model = _extract_task_model(task)
                        # Prune stale blacklist entries — accounts whose model cooldown has expired
                        # should be eligible again, otherwise rate-limited tasks get stuck forever
                        if _blacklist:
                            async with _max_plan_pool_lock:
                                _blacklist = [
                                    aid
                                    for aid in _blacklist
                                    if aid in _account_by_id
                                    and not _account_available_for_model(
                                        _account_by_id[aid], task_model
                                    )
                                ]
                            if _blacklist != (task.context or {}).get(
                                "blacklisted_accounts"
                            ):
                                task.context = {
                                    **(task.context or {}),
                                    "blacklisted_accounts": _blacklist or None,
                                }
                                if not _blacklist:
                                    task.context.pop("blacklisted_accounts", None)
                                session.add(task)
                        _effective_blacklist = (
                            list({*(_blacklist or []), *_sticky}) or None
                        )
                        runtime_selection = await select_llm_runtime_for_task(
                            user_id=dispatch_user_id,
                            task=task,
                            runtime_config=_runtime_config,
                            blacklist=_effective_blacklist,
                            project_id=str(task.project_id) if task.project_id else None,
                            # No runner_available_clis here: this is the cloud
                            # pre-provision path where no concrete runner is known
                            # yet. Cloud VMs are provisioned with all LLM CLIs
                            # pre-installed, so CLI-based filtering is unnecessary.
                        )
                        # From here until ownership transfers, the containment
                        # handler owns releasing the slot this selection
                        # pre-incremented (select_max_plan_account).
                        if runtime_selection and runtime_selection.account:
                            _iter_preclaimed_account_id = runtime_selection.account.get(
                                "id"
                            )
                            _iter_preclaimed_project_id = (
                                str(task.project_id) if task.project_id else None
                            )

                        # Warm-pool dispatch hook (efficiency direction d-5fd69fe1):
                        # BEFORE cold-provisioning, consult the pre-provisioned idle
                        # pool. If a compatible, same-tenant IDLE member is free, lease
                        # it atomically (FOR UPDATE SKIP LOCKED) and pin this task to
                        # it, then skip cold provisioning entirely — the member's agent
                        # claims the task warm via /next-role (origin "warm_pool"),
                        # converting a cold dispatch into a warm-start claim. Inert when
                        # warm_pool_enabled is off. Scoped to fresh, non-pinned,
                        # no-dependency tasks (a task already carrying a warm pin is
                        # handled by the skip-provision branch above). The required
                        # identity passed in is the RESOLVED selection (runtime_selection)
                        # — honoring provider preference, category/quality routing AND the
                        # rate-limit blacklist — NOT the task's raw context, so a member
                        # pinned to a blacklisted account or a non-routed model is never
                        # leased. Skipped entirely when no runtime identity resolves
                        # (runtime_selection is None — the cold path's no-account handling
                        # takes over). On no lease, fall through to the existing cold path
                        # and count it as a cold dispatch.
                        # Cloud Run types bypass the warm VM pool — they run in
                        # a Cloud Run Job container, not on a pool VM. Compute
                        # the effective CR set here from the runtime allowlist.
                        _cr_dispatch_allowlist = frozenset(
                            _runtime_config.get("cloud_run_task_type_allowlist", [])
                        )
                        _effective_cr_types_dispatch = (
                            CLOUD_RUN_CANDIDATE_TASK_TYPES & _cr_dispatch_allowlist
                        )
                        _task_type_dispatch = (
                            (task.first_payload or {}).get("type", "")
                            if task.payloads
                            else ""
                        )
                        _is_cloud_run_dispatch = (
                            _task_type_dispatch in _effective_cr_types_dispatch
                        )
                        if (
                            not _warm_target_vm_id
                            and not task.depends_on
                            and is_warm_pool_enabled()
                            and runtime_selection is not None
                            and not _is_cloud_run_dispatch
                        ):
                            _leased_member = await try_pool_dispatch(
                                session,
                                task,
                                required_provider=runtime_selection.provider,
                                required_model=runtime_selection.model or task_model,
                                required_account=(
                                    runtime_selection.account.get("id")
                                    if runtime_selection.account
                                    else None
                                ),
                                required_auth_mode=runtime_selection.auth_mode,
                                excluded_account_ids=_effective_blacklist,
                            )
                            if _leased_member is not None:
                                # A dispatch that reaches a lease commit resets
                                # the contained-crash counter (persisted by that
                                # commit; rolled back with it on failure).
                                _clear_dispatch_prep_failures(task)
                                try:
                                    await session.commit()
                                except Exception:
                                    logger.exception(
                                        "[ORCHESTRATOR] Failed to commit warm-pool lease "
                                        f"for task {task.id}; falling back to cold provision"
                                    )
                                    _shared_state.record_pool_claim_failure(
                                        "commit_error"
                                    )
                                    # Release the slot pre-reserved during runtime
                                    # selection (select_max_plan_account pre-increments
                                    # active_tasks under lock). This early-exit abandons
                                    # the dispatch without launching, so the reservation
                                    # would otherwise leak until the next per-cycle
                                    # reconciliation re-bases the counter from the DB —
                                    # mirroring the cold-provision commit-failure release.
                                    # Guarded so it is a no-op when selection resolved to
                                    # an env-credential fallback with no pooled account;
                                    # the release helper also floors at zero. Use the
                                    # account carried on the resolved selection, not the
                                    # loop-local account_id (only assigned later, below).
                                    if runtime_selection and runtime_selection.account:
                                        await release_max_plan_account(
                                            runtime_selection.account["id"],
                                            revert_daily_task_count=True,
                                            project_id=str(task.project_id) if task.project_id else None,
                                        )
                                    _iter_preclaimed_account_id = None
                                    _iter_preclaimed_project_id = None
                                    await session.rollback()
                                    # session.rollback() expires every session-bound
                                    # instance (independent of expire_on_commit=False),
                                    # so the fall-through reads of this task below
                                    # (task.depends_on/project_id/vm_instance_id) would
                                    # trigger a sync lazy-load -> MissingGreenlet and
                                    # abort the whole cycle (the hazard documented at
                                    # the top of this loop). The lease was rolled back,
                                    # so the task is still QUEUED — retry it cleanly
                                    # next cycle, exactly like every other rollback
                                    # site in this loop.
                                    continue
                                else:
                                    # Slot ownership transferred to the leased
                                    # warm task — the containment handler must
                                    # not release it past this point.
                                    _iter_preclaimed_account_id = None
                                    _iter_preclaimed_project_id = None
                                    record_pool_lease_hit(
                                        account=(
                                            runtime_selection.account.get("id")
                                            if runtime_selection.account
                                            else None
                                        ),
                                        project_id=task.project_id,
                                    )
                                    logger.info(
                                        f"[ORCHESTRATOR] Warm-pool dispatch: task {task.id} "
                                        f"leased pool VM {_leased_member.vm_instance_id} "
                                        "(skipping cold provision)"
                                    )
                                    continue

                        # Backlog-reuse first-refusal (efficiency direction d-91d8dede):
                        # if an idle warm VM is actively polling /next-role for this
                        # project, hold off cold-provisioning this eligible backlog task
                        # for a bounded window so the warm VM can claim it first. Scoped
                        # to non-chain-pinned tasks that the /next-role backlog branch
                        # will actually claim: either no dependencies, or dependencies
                        # the orchestrator already resolved and stamped
                        # ``_deps_satisfied`` on above — exactly the warm-eligible set.
                        # A task with still-unresolved deps carries no marker and is
                        # never held (it cold-dispatches as before). The atomic
                        # SKIP-LOCKED claim in /next-role still guarantees no
                        # double-provision if both race; this only biases the race
                        # toward reuse. The window auto-expires, so a VM that dies
                        # mid-poll delays this task by at most that bound.
                        if (
                            _runtime_config.get("warm_backlog_reuse_enabled", False)
                            and not _warm_target_vm_id
                            and (
                                not task.depends_on
                                or (task.context or {}).get("_deps_satisfied")
                            )
                            and _shared_state.is_backlog_first_refusal_active(
                                str(task.project_id)
                            )
                        ):
                            logger.info(
                                f"[ORCHESTRATOR] Backlog-reuse first-refusal: holding off "
                                f"cold-provision of task {task.id} (project {task.project_id}) "
                                f"— idle warm VM has first claim"
                            )
                            # Release the slot pre-reserved during runtime selection
                            # (select_max_plan_account pre-increments active_tasks). This
                            # hold abandons the dispatch so the warm VM can claim the task
                            # via /next-role; without releasing, the reservation leaks for
                            # the rest of the cycle and repeated holds compound the drift.
                            # Mirrors the cold-provision commit-failure release; guarded for
                            # the env-credential fallback (no pooled account) and floored at
                            # zero by the helper. Targets the resolved selection's account,
                            # not the loop-local account_id (assigned later, below).
                            if runtime_selection and runtime_selection.account:
                                await release_max_plan_account(
                                    runtime_selection.account["id"],
                                    revert_daily_task_count=True,
                                    project_id=str(task.project_id) if task.project_id else None,
                                )
                            _iter_preclaimed_account_id = None
                            _iter_preclaimed_project_id = None
                            await session.rollback()
                            continue

                        # Ensure VM ID exists (cleared during stale PROVISIONING recovery
                        # OR set to None for warm-pinned tasks whose pin was just cleared).
                        if not task.vm_instance_id:
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                        logger.info(
                            f"[ORCHESTRATOR] Picking up queued task {task.id} (VM: {task.vm_instance_id})"
                        )

                        runtime_secret = None
                        claude_config_json = None
                        account_id = None
                        runtime_provider = None
                        runtime_auth_mode = None
                        runtime_model = task_model
                        runtime_execution_mode = None
                        runtime_provider_metadata = None
                        runtime_provider_settings: dict | None = None
                        if runtime_selection:
                            runtime_provider = runtime_selection.provider
                            runtime_auth_mode = runtime_selection.auth_mode
                            runtime_model = runtime_selection.model or runtime_model
                            runtime_execution_mode = runtime_selection.execution_mode
                            runtime_provider_settings = runtime_selection.settings
                        account = (
                            runtime_selection.account if runtime_selection else None
                        )
                        if account:
                            account_id = account["id"]
                            runtime_secret = account.get(
                                "runtime_secret"
                            ) or account.get("oauth_token")
                            claude_config_json = account.get("claude_config_json")
                            runtime_provider_metadata = (
                                account.get("provider_metadata") or {}
                            )
                            logger.info(
                                f"[ORCHESTRATOR] Using LLM account '{account['name']}' "
                                f"(provider={runtime_provider}, auth_mode={runtime_auth_mode}, model={runtime_model})"
                            )
                        else:
                            # No available account — either all disabled/blacklisted or none configured
                            user_pool = _max_plan_pools.get(dispatch_user_id, [])
                            provider_accounts = user_pool
                            if user_pool and runtime_provider:
                                provider_handler = get_provider(runtime_provider)
                                provider_accounts = [
                                    acct
                                    for acct in user_pool
                                    if str(acct.get("provider") or "anthropic")
                                    == runtime_provider
                                ]
                                ready_provider_accounts = [
                                    acct
                                    for acct in provider_accounts
                                    if acct.get("enabled", True)
                                    and provider_handler.account_has_credentials(acct)
                                ]
                            else:
                                ready_provider_accounts = list(provider_accounts)
                            if user_pool and ready_provider_accounts:
                                # Accounts exist but all blocked — notify user and skip
                                logger.info(
                                    f"[ORCHESTRATOR] All accounts blocked for task {task.id} — skipping (will retry next cycle)"
                                )
                                from app.max_plan_pool import (
                                    summarize_pool_state,
                                    describe_pool_blockage,
                                )

                                _pool_summary = summarize_pool_state(
                                    task_model, user_id=dispatch_user_id
                                )
                                earliest_recovery = _pool_summary["earliest_recovery"]
                                stall_msg = describe_pool_blockage(
                                    task_model, user_id=dispatch_user_id
                                )
                                _cycle_blockage_reasons.add(stall_msg)
                                if earliest_recovery:
                                    stall_delay_minutes = rate_limit_retry_minutes(
                                        earliest_recovery
                                    )
                                else:
                                    stall_delay_minutes = 2
                                logger.info(
                                    "[ORCHESTRATOR] pool_exhausted task=%s model=%s %s",
                                    str(task.id)[:8],
                                    task_model,
                                    _pool_summary,
                                )
                                # Retry delay based on earliest known recovery to avoid
                                # hot-looping every 2 min when recovery is hours away.
                                # set_retry_after clamps to RETRY_AFTER_MAX_MINUTES.
                                stall_ctx = dict(task.context or {})
                                set_retry_after(stall_ctx, stall_delay_minutes)
                                task.context = stall_ctx
                                # Escalate visibility when a task is stuck cycling through retry_after holds.
                                # Each reset is up to RETRY_AFTER_MAX_MINUTES (30m). At the alert
                                # threshold (default 8 resets ≈ 4h) we fire a WARNING so the situation
                                # is visible, but pool exhaustion is a wait condition — we never
                                # auto-fail the task; it stays in retry_after until the pool recovers.
                                try:
                                    _resets = int(
                                        stall_ctx.get("_retry_after_resets") or 0
                                    )
                                    _reset_threshold = _runtime_config.get(
                                        "retry_after_reset_alert_threshold", 8
                                    )
                                    _action = _compute_retry_after_escalation_action(
                                        _resets, _reset_threshold, 0
                                    )
                                    if _action == "alert":
                                        from app.models import Alert, AlertSeverity
                                        from app.max_plan_pool import (
                                            describe_pool_blockage,
                                        )

                                        _blockage = describe_pool_blockage(
                                            task_model, user_id=dispatch_user_id
                                        )
                                        _hold_min = (
                                            (
                                                _utcnow() - task.created_at
                                            ).total_seconds()
                                            / 60
                                            if task.created_at
                                            else 0.0
                                        )
                                        _stuck_title = f"Task stuck in retry_after cycle: {task.title or str(task.id)[:8]}"
                                        _existing_stuck = (
                                            (
                                                await session.execute(
                                                    select(Alert).where(
                                                        Alert.task_id == task.id,
                                                        Alert.title == _stuck_title,
                                                        Alert.dismissed_at.is_(None),
                                                    )
                                                )
                                            )
                                            .scalars()
                                            .first()
                                        )
                                        if _existing_stuck is None:
                                            _stuck_msg = (
                                                f"Task {task.id} has been cycling through retry_after holds "
                                                f"{_resets} times (threshold {_reset_threshold}); held for "
                                                f"{_hold_min:.0f}m total. Pool state: {_blockage}."
                                            )
                                            _stuck_alert = Alert(
                                                project_id=task.project_id,
                                                severity=AlertSeverity.WARNING,
                                                title=_stuck_title,
                                                message=_stuck_msg,
                                                task_id=task.id,
                                            )
                                            session.add(_stuck_alert)
                                            await broadcast_to_project(
                                                str(task.project_id),
                                                "alert_created",
                                                {
                                                    "alert_id": str(_stuck_alert.id),
                                                    "severity": "WARNING",
                                                    "title": _stuck_title,
                                                },
                                            )
                                            logger.warning(
                                                f"[ORCHESTRATOR] Retry-after escalation alert for task {task.id} "
                                                f"(resets={_resets}, blockage={_blockage})"
                                            )
                                except Exception:
                                    logger.exception(
                                        f"[ORCHESTRATOR] Failed to evaluate retry_after escalation for task {task.id}"
                                    )
                                if task.latest_status_message != stall_msg:
                                    # Update directly on the locked task object (separate session would deadlock)
                                    task.latest_status_message = stall_msg
                                    await broadcast_to_project(
                                        str(task.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(task.id),
                                            "status": task.status,
                                            "message": stall_msg,
                                        },
                                    )
                                session.add(task)
                                _cycle_accounts_blocked += 1
                                if earliest_recovery and (
                                    _cycle_earliest_recovery is None
                                    or earliest_recovery < _cycle_earliest_recovery
                                ):
                                    _cycle_earliest_recovery = earliest_recovery
                                await session.commit()
                                if _pending_warm_pin_clear is not None:
                                    _shared_state.record_warm_pin_clear(
                                        *_pending_warm_pin_clear
                                    )
                                    _pending_warm_pin_clear = None
                                continue
                            # No accounts at all — fall back to raw provider credentials when present
                            (
                                runtime_secret,
                                runtime_provider,
                                runtime_auth_mode,
                                runtime_model,
                                runtime_execution_mode,
                                runtime_provider_metadata,
                            ) = _resolve_env_runtime_fallback(
                                provider=runtime_provider,
                                model=runtime_model or task_model,
                                execution_mode=runtime_execution_mode,
                                task=task,
                                runtime_config=_runtime_config,
                            )
                            if runtime_secret:
                                logger.info(
                                    f"[ORCHESTRATOR] No LLM pool account available, using env credential fallback "
                                    f"(provider={runtime_provider}, auth_mode={runtime_auth_mode}, model={runtime_model})"
                                )
                            else:
                                logger.warning(
                                    f"[ORCHESTRATOR] No credentials available — skipping task {task.id}"
                                )
                                stall_msg = _missing_runtime_credentials_stall_reason(
                                    user_pool_count=len(user_pool),
                                    provider_account_count=len(provider_accounts),
                                    ready_provider_account_count=len(
                                        ready_provider_accounts
                                    ),
                                    runtime_provider=runtime_provider,
                                )
                                _cycle_blockage_reasons.add(stall_msg)
                                _cycle_accounts_blocked += 1
                                # Update directly on the locked task object (separate session would deadlock)
                                task.latest_status_message = (
                                    "Waiting for credentials — add an LLM account or set "
                                    f"{_provider_env_credential_hint(runtime_provider)}"
                                )
                                session.add(task)
                                await session.commit()
                                if _pending_warm_pin_clear is not None:
                                    _shared_state.record_warm_pin_clear(
                                        *_pending_warm_pin_clear
                                    )
                                    _pending_warm_pin_clear = None
                                continue

                        # Cross-provider fallback model guard (EXTERNAL_API mispair
                        # fix) — applied at the FINAL choke point where
                        # runtime_provider / runtime_model have their settled values
                        # from EVERY path (pool selection, account, env-credential
                        # fallback), just before they are written to the payload,
                        # context, and agent manifest. A task pinned to provider A's
                        # model can be dispatched to provider B when A is unavailable;
                        # if the model id isn't re-derived for B the runtime is handed
                        # e.g. provider=anthropic + model=deepseek-v4-pro (selection.
                        # model was None, so `or runtime_model` kept the task's stale
                        # context.llm_model), and the anthropic CLI rejects the foreign
                        # id → EXTERNAL_API failure → 3 retries burned → task FAILED
                        # (and, in a rollup, review-fix iterations wasted). Re-derive a
                        # model valid for the resolved provider; no-op when already
                        # consistent or the model is operator-custom (infers to None).
                        _reconciled_model = reconcile_model_for_provider(
                            runtime_provider,
                            runtime_model,
                            (
                                runtime_selection.quality_tier
                                if runtime_selection
                                else None
                            )
                            or resolve_task_quality_tier(task, _runtime_config),
                            _runtime_config,
                        )
                        if _reconciled_model != runtime_model:
                            logger.warning(
                                "[ORCHESTRATOR] Cross-provider model mismatch for task "
                                "%s: provider=%s but model=%s is foreign; re-derived "
                                "to %s",
                                task.id,
                                runtime_provider,
                                runtime_model,
                                _reconciled_model,
                            )
                            runtime_model = _reconciled_model

                        if _sync_task_payload_runtime_fields(
                            task,
                            model=runtime_model,
                            execution_mode=runtime_execution_mode,
                        ):
                            from sqlalchemy.orm.attributes import flag_modified

                            flag_modified(task, "payloads")
                            session.add(task)

                        task.context = apply_task_llm_runtime_context(
                            task.context,
                            account_id=account_id,
                            provider=runtime_provider,
                            auth_mode=runtime_auth_mode,
                            model=runtime_model,
                            execution_mode=runtime_execution_mode,
                        )
                        # Keep the promoted column in sync with context
                        # (D2 over-fetch fix) — context keys are still written
                        # above so in-flight rows / rollbacks stay safe.
                        task.llm_account_id = account_id
                        # A dispatch that reaches the PROVISIONING commit resets
                        # the contained-crash counter (persisted by that commit;
                        # rolled back with everything else if prep still crashes
                        # between here and the commit).
                        _clear_dispatch_prep_failures(task)

                        # Launch-time staleness recompute: for finding-derived tasks
                        # that have been QUEUED while the integration branch advanced,
                        # re-check whether any cited file changed since the finding was
                        # generated — against the CURRENT branch tip, not the inert
                        # ingestion HEAD. Best-effort: the helper never raises and only
                        # an affirmative change verdict annotates the context, so it can
                        # never block or delay dispatch. The mutation is persisted by
                        # the PROVISIONING commit below.
                        from app.research_completion import recompute_launch_staleness

                        if await recompute_launch_staleness(session, task):
                            from sqlalchemy.orm.attributes import flag_modified

                            flag_modified(task, "context")

                        # Mark as PROVISIONING to prevent other workers from picking it up.
                        # last_heartbeat is intentionally NOT set here — it must reflect real
                        # agent activity only. The reaper uses vm_provisioned_at as the clock
                        # for the pre-claim window so it can distinguish "VM created but agent
                        # never claimed" from "agent claimed and crashed".
                        task.status = TaskStatus.PROVISIONING
                        task.started_at = _utcnow()
                        # Write-once dispatch timestamp — anchors vm_cold_start_ms
                        # to the full dispatch->agent-ready interval (includes GCE
                        # boot latency). Never re-stamped on provision-confirm.
                        if task.vm_dispatched_at is None:
                            task.vm_dispatched_at = _utcnow()
                        task.vm_provisioned_at = _utcnow()
                        # Cold dispatch: this is a fresh provision, so vm_warm_start
                        # is False. The warm-claim / backlog-reuse path stamps True
                        # instead (routers/agent_warm_continuity._finalize_warm_claim),
                        # which is also where the vm_reuse_rate signal
                        # (context['_timing']['warm_start']) is written. See
                        # compute.provision_agent_vm for the corresponding marker.
                        task.vm_warm_start = False
                        # vm_spot must be written here — it's read by ~30 cost-rate sites
                        # (admin_digest, ci_completion, task_completion, etc.) gating
                        # SPOT_RATE vs ON_DEMAND_RATE. Without this write every cost
                        # dashboard overstates by ~3.3x when the orchestrator runs in
                        # SPOT mode. compute.VM_PROVISIONING_MODEL is the source of truth
                        # (env-driven; STANDARD by default).
                        # Tasks with >=2 prior spot preemptions are forced to STANDARD
                        # to avoid a third preemption on the same task.
                        _spot_preemption_count = (task.context or {}).get(
                            "spot_preemption_count", 0
                        )
                        _force_standard = (VM_PROVISIONING_MODEL == "SPOT") and (
                            _spot_preemption_count >= 2
                        )
                        if _force_standard:
                            logger.info(
                                f"[ORCHESTRATOR] Task {task.id} forced to STANDARD VM after {_spot_preemption_count} spot preemptions"
                            )
                        task.vm_spot = (
                            VM_PROVISIONING_MODEL == "SPOT"
                        ) and not _force_standard
                        task.last_heartbeat = None
                        _shared_state.deploy_monitoring["last_dispatch_time"] = (
                            _utcnow()
                        )
                        session.add(task)
                        try:
                            await session.commit()
                            # Slot ownership transfers to provision_and_handle
                            # (acct_id_for_release) once the PROVISIONING commit
                            # lands — the containment handler must not release it.
                            _iter_preclaimed_account_id = None
                            _iter_preclaimed_project_id = None
                            # active_tasks already pre-incremented in select_max_plan_account()
                            project_active_counts[task.project_id] = (
                                project_active_counts.get(task.project_id, 0) + 1
                            )
                            logger.info(
                                f"[ORCHESTRATOR] Task {task.id} committed as PROVISIONING — launching VM"
                            )
                            # Issue 11: emit warm-pin-clear metric ONLY after the
                            # commit that persists the warm-field clear succeeds.
                            # On commit failure → rollback path below, the metric
                            # stays unrecorded so the next dispatch cycle can fire
                            # it cleanly without double-counting.
                            if _pending_warm_pin_clear is not None:
                                _shared_state.record_warm_pin_clear(
                                    *_pending_warm_pin_clear
                                )
                                _pending_warm_pin_clear = None
                            # Pool-hit-vs-cold-dispatch funnel: this task was cold-
                            # provisioned (no compatible pool member was leased above).
                            # Recorded only when the pool feature is on so the funnel
                            # denominator reflects the period the pool was actually live.
                            if is_warm_pool_enabled():
                                # Attribute the cold dispatch to the same resolved
                                # account as the lease hit/miss recorded earlier in
                                # this iteration so the per-account funnel
                                # denominator reconciles with fleet totals. When
                                # selection fell back to env credentials account_id
                                # is None and the cold dispatch buckets under the
                                # unattributed account, exactly as before.
                                record_pool_cold_dispatch(
                                    account_id,
                                    project_id=task.project_id,
                                )
                        except Exception as e:
                            import traceback

                            logger.error(
                                f"[ORCHESTRATOR] FAILED to commit PROVISIONING for task {task.id}: {e}",
                                exc_info=True,
                            )
                            traceback.print_exc()
                            # Release the pre-incremented slot on commit failure
                            if account_id:
                                await release_max_plan_account(
                                    account_id,
                                    revert_daily_task_count=True,
                                    project_id=str(task.project_id) if task.project_id else None,
                                )
                            _iter_preclaimed_account_id = None
                            _iter_preclaimed_project_id = None
                            task.context = clear_task_llm_runtime_context(task.context)
                            # Keep the promoted column in sync (D2 over-fetch fix).
                            task.llm_account_id = None
                            await session.rollback()
                            continue

                        # Trigger GCP Compute Engine provisioning in the background.
                        # ``provision_and_handle`` lives in ``app.orchestrator_provision``
                        # (re-exported below); every value is passed as an explicit arg
                        # so there are no stale closure captures across loop iterations.
                        provision_coro = asyncio.create_task(
                            provision_and_handle(
                                task.vm_instance_id,
                                task.id,
                                task.project_id,
                                task.payloads,
                                runtime_secret,
                                claude_config_json,
                                account_id,
                                runtime_provider,
                                runtime_auth_mode,
                                runtime_model,
                                runtime_execution_mode,
                                runtime_provider_metadata,
                                runtime_provider_settings,
                                force_standard=_force_standard,
                            )
                        )
                        _provision_coroutines[str(task.id)] = provision_coro
                        provision_coro.add_done_callback(
                            lambda _, tid=str(task.id): _provision_coroutines.pop(
                                tid, None
                            )
                        )

                        await broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {
                                "task_id": str(task.id),
                                "status": TaskStatus.PROVISIONING,
                            },
                        )
                        _cycle_dispatched += 1
                    except Exception as _dispatch_prep_exc:
                        # NEVER touch the loop's `task` ORM object in here — the
                        # rollback below expires every session-bound instance
                        # and attribute access would fire a sync lazy-load
                        # (MissingGreenlet, the hazard documented at the
                        # gate-info snapshot above). Use the snapshot tuple
                        # (_qt_id, _task_type_for_gate) instead.
                        # Transient DB row/lock contention (the global 30s
                        # lock_timeout expiring on a FOR UPDATE, a broken
                        # deadlock, or a statement hitting an already-aborted
                        # txn) is NOT a poison task — it self-heals once the lock
                        # releases. Detect it (walking the __cause__/.orig chain:
                        # the surfaced error is usually a PendingRollbackError /
                        # DBAPIError wrapping the asyncpg LockNotAvailableError)
                        # so it does NOT accrue toward the 3-strike quarantine
                        # below, which in prod was FAILing healthy
                        # review/fix/design_doc_update tasks during contention
                        # bursts (~489 lock_timeout events/24h).
                        from app.db_contention import is_db_contention_error

                        _prep_is_contention = is_db_contention_error(
                            _dispatch_prep_exc
                        )
                        # Set when a contention-classified failure has recurred so
                        # many consecutive cycles it is no longer plausibly
                        # transient — forces the quarantine block below to fire
                        # even though the poison counter was never incremented.
                        _force_quarantine = False
                        if _prep_is_contention:
                            logger.warning(
                                "[ORCHESTRATOR] Dispatch prep hit transient DB "
                                f"contention for task {_qt_id} "
                                f"(type={_task_type_for_gate or '?'}): "
                                f"{_dispatch_prep_exc!r} — left QUEUED, will retry "
                                "next cycle (not counted toward quarantine)"
                            )
                        else:
                            logger.error(
                                f"[ORCHESTRATOR] Dispatch prep crashed for task {_qt_id} "
                                f"(type={_task_type_for_gate or '?'}): {_dispatch_prep_exc!r} "
                                "— contained; continuing with next task",
                                exc_info=True,
                            )
                        try:
                            await session.rollback()
                        except Exception:
                            logger.exception(
                                f"[ORCHESTRATOR] Containment rollback failed for task {_qt_id}"
                            )
                        if _iter_preclaimed_account_id:
                            try:
                                await release_max_plan_account(
                                    _iter_preclaimed_account_id,
                                    revert_daily_task_count=True,
                                    project_id=_iter_preclaimed_project_id,
                                )
                            except Exception:
                                logger.exception(
                                    "[ORCHESTRATOR] Failed to release pre-claimed account "
                                    f"{_iter_preclaimed_account_id} after dispatch crash "
                                    f"for task {_qt_id}"
                                )
                        if _prep_is_contention:
                            # Transient DB contention self-heals in a cycle or two,
                            # so the common case leaves the task QUEUED untouched:
                            # the rollback above discarded any partial in-txn
                            # mutation and the pre-claimed account was released, so
                            # the next cycle re-dispatches cleanly once the lock
                            # frees. Do NOT touch _dispatch_prep_failures and do
                            # NOT quarantine — transient contention must never
                            # transition the task to FAILED.
                            #
                            # Safety valve: a PERSISTENT non-transient error that
                            # keeps surfacing as a contention SQLSTATE would
                            # otherwise retry every cycle forever. Track the
                            # consecutive-contention streak in leader memory (no DB
                            # write on the hot contention path); only once it
                            # exceeds the far-higher contention bound treat the
                            # task as stuck and fall through to quarantine.
                            _cont_key = str(_qt_id)
                            _cont_streak = (
                                _dispatch_prep_contention_streak.get(_cont_key, 0)
                                + 1
                            )
                            if (
                                _cont_streak
                                < DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD
                            ):
                                _dispatch_prep_contention_streak[_cont_key] = (
                                    _cont_streak
                                )
                                continue
                            # Streak exhausted — no longer plausibly transient.
                            _dispatch_prep_contention_streak.pop(_cont_key, None)
                            _force_quarantine = True
                            logger.error(
                                "[ORCHESTRATOR] Task %s hit DB contention on "
                                "dispatch prep %d consecutive cycles (threshold "
                                "%d) — no longer treating as transient; "
                                "quarantining as a stuck task. Last error: %r",
                                _qt_id,
                                _cont_streak,
                                DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD,
                                _dispatch_prep_exc,
                            )
                        try:
                            _crashed_task = await _lock_dispatchable_task_for_mutation(
                                session, _qt_id
                            )
                            if _crashed_task is not None:
                                _crash_ctx = dict(_crashed_task.context or {})
                                _prep_failures = (
                                    int(_crash_ctx.get("_dispatch_prep_failures") or 0)
                                    + 1
                                )
                                if (
                                    _force_quarantine
                                    or _prep_failures
                                    >= DISPATCH_PREP_FAILURE_QUARANTINE_THRESHOLD
                                ):
                                    # Quarantine: take the poison task out of the
                                    # dispatch path. FAILED is operator-recoverable
                                    # (retry) and record_task_result engages the
                                    # per-type failure gate — the gate that never
                                    # fired while the task crashed pre-FAILED.
                                    # Dependents are reclaimed by the existing
                                    # failed-foundation cascade sweep.
                                    _crash_ctx["_dispatch_prep_failures"] = (
                                        _prep_failures
                                    )
                                    _crashed_task.context = _crash_ctx
                                    _crashed_task.status = TaskStatus.FAILED
                                    if _crashed_task.vm_provisioned_at:
                                        _crashed_task.vm_stopped_at = _utcnow()
                                    _crashed_task.latest_status_message = (
                                        (
                                            "Dispatch preparation hit persistent DB "
                                            "contention "
                                            f"{DISPATCH_PREP_CONTENTION_QUARANTINE_THRESHOLD} "
                                            "consecutive cycles — quarantined: "
                                            f"{_dispatch_prep_exc!r}"
                                        )
                                        if _force_quarantine
                                        else (
                                            f"Dispatch preparation crashed {_prep_failures} "
                                            "consecutive cycles — quarantined: "
                                            f"{_dispatch_prep_exc!r}"
                                        )
                                    )[:500]
                                    from app.failure_classification import (
                                        persist_failure_category as _pfc_dispatch,
                                    )

                                    _pfc_dispatch(_crashed_task, "PROVISION_FAILURE")
                                    record_task_result(
                                        success=False,
                                        failure_class="PROVISION_FAILURE",
                                        task_id=str(_qt_id),
                                        task_type=_task_type_for_gate or "",
                                    )
                                    await _maybe_create_failure_alert(
                                        _crashed_task,
                                        session,
                                        (
                                            f"Dispatch preparation crashed {_prep_failures} "
                                            f"times — task quarantined. Last error: "
                                            f"{_dispatch_prep_exc!r}"
                                        )[:200],
                                    )
                                    logger.exception(
                                        f"[ORCHESTRATOR] Task {_qt_id} quarantined after "
                                        f"{_prep_failures} dispatch prep crashes"
                                    )
                                else:
                                    _crash_ctx["_dispatch_prep_failures"] = (
                                        _prep_failures
                                    )
                                    _crashed_task.context = _crash_ctx
                                    _crashed_task.latest_status_message = (
                                        "Dispatch preparation crashed (attempt "
                                        f"{_prep_failures}/"
                                        f"{DISPATCH_PREP_FAILURE_QUARANTINE_THRESHOLD}"
                                        f"), will retry: {_dispatch_prep_exc!r}"
                                    )[:500]
                                session.add(_crashed_task)
                                _crash_proj_id = str(_crashed_task.project_id)
                                _crash_status = _crashed_task.status
                                await session.commit()
                                await broadcast_to_project(
                                    _crash_proj_id,
                                    "task_updated",
                                    {
                                        "task_id": str(_qt_id),
                                        "status": _crash_status,
                                    },
                                )
                        except Exception:
                            # Containment must never re-crash the cycle: if
                            # persisting the counter/quarantine fails the task
                            # simply stays QUEUED for the next cycle (the
                            # pre-containment behavior). Mirrors the CI-poll
                            # error recovery's nested handler below.
                            logger.exception(
                                "[ORCHESTRATOR] Failed to persist dispatch-crash "
                                f"state for task {_qt_id}"
                            )
                            try:
                                await session.rollback()
                            except Exception:
                                logger.exception(
                                    "[ORCHESTRATOR] Containment rollback also "
                                    f"failed for task {_qt_id}"
                                )
                        continue

                # All-accounts-blocked stall detection: if at least one task was
                # skipped for rate-limited accounts and nothing dispatched, the
                # pipeline is silently stalled. Emit one alert/broadcast/notification
                # per episode (debounced until any task dispatches again).
                global _all_accounts_blocked_alerted
                _stall_action = _compute_all_accounts_blocked_action(
                    _cycle_accounts_blocked,
                    _cycle_dispatched,
                    _all_accounts_blocked_alerted,
                )
                if _stall_action == "reset":
                    if _all_accounts_blocked_alerted:
                        _all_accounts_blocked_alerted = False
                        logger.info(
                            "[ORCHESTRATOR] Rate-limit stall resolved — "
                            f"{_cycle_dispatched} task(s) dispatched after prior all-accounts-blocked episode"
                        )
                        try:
                            from app.broadcast import manager as _ws_manager

                            await _ws_manager.broadcast_to_all(
                                "rate_limit_stall_resolved",
                                {
                                    "dispatched_count": _cycle_dispatched,
                                    "detail": "Pipeline recovered: tasks dispatching after all-accounts-blocked stall",
                                },
                            )
                        except Exception:
                            logger.exception(
                                "[ORCHESTRATOR] Failed to broadcast rate_limit_stall_resolved"
                            )
                elif _stall_action == "fire":
                    _all_accounts_blocked_alerted = True
                    await _emit_all_accounts_blocked_stall(
                        session,
                        _cycle_earliest_recovery,
                        _cycle_blockage_reasons or None,
                    )

                # Per-cycle broadcast so operators can see held-task counts in
                # the admin UI without tailing logs. Distinct from the debounced
                # pipeline_throttle alert: this fires every cycle any tasks are held.
                if _cycle_accounts_blocked > 0:
                    try:
                        from app.broadcast import manager as _ws_manager

                        await _ws_manager.broadcast_to_all(
                            "tasks_held_by_rate_limit",
                            {
                                "count": _cycle_accounts_blocked,
                                "earliest_recovery": (
                                    _cycle_earliest_recovery.isoformat()
                                    if _cycle_earliest_recovery
                                    else None
                                ),
                            },
                        )
                    except Exception:
                        logger.exception(
                            "[ORCHESTRATOR] Failed to broadcast tasks_held_by_rate_limit"
                        )

                # Flush any status message updates from skipped tasks (e.g., "All accounts rate-limited")
                if session.dirty:
                    await session.commit()

                # CI/EVALUATING monitor sweep — extracted to
                # app.orchestrator_ci_monitor._monitor_evaluating_tasks
                # (re-exported above). Behaviour-preserving.
                await _monitor_evaluating_tasks(session)
                # Post-commit recovery is handled by `_sweep_post_commit_pending`
                # in services.py — it calls `_process_post_commit` (which knows
                # how to spawn the right follow-up for every task type, including
                # fix/review/plan/research/strategic/etc). The hand-rolled
                # spawn-review loop that used to live here whitelisted only
                # create/refactor/docs/qa/deps/migrate and silently CLEARED the
                # post_commit_pending column for everything else, eating the
                # recovery marker before the sweeper could pick it up. Removed.

            _orchestrator_consecutive_errors = 0
            logger.info(
                "orchestrator_cycle_complete",
                extra={
                    "cycle_duration_seconds": round(time.monotonic() - cycle_start, 3),
                    "tasks_dispatched": len(queued_tasks),
                    "tasks_recovered": tasks_recovered,
                    "slots_in_use": effective_count,
                    "slots_available": slots_available,
                    "queue_depth": _shared_state.tasks_queued_gauge,
                },
            )
            _orchestrator_last_cycle = _utcnow()
        except asyncio.CancelledError:
            logger.info("[ORCHESTRATOR] Worker shutting down.")
            break
        except Exception:
            _orchestrator_consecutive_errors += 1
            backoff = exponential_backoff(_orchestrator_consecutive_errors)
            logger.error(
                "orchestrator_cycle_error",
                extra={
                    "cycle_duration_seconds": round(time.monotonic() - cycle_start, 3),
                    "consecutive_errors": _orchestrator_consecutive_errors,
                    "backoff_seconds": backoff,
                },
                exc_info=True,
            )
            _orchestrator_last_cycle = _utcnow()
            try:
                if session:
                    await session.rollback()
            except Exception:
                logger.exception("[ORCHESTRATOR] Rollback after cycle error failed")
            # After 3 consecutive errors, alert all projects (deduplicated)
            if _orchestrator_consecutive_errors >= 3:
                try:
                    async with AsyncSessionLocal() as alert_session:
                        await _create_orchestrator_error_alerts(
                            alert_session, _orchestrator_consecutive_errors
                        )
                        await alert_session.commit()
                except Exception:
                    logger.exception("[ORCHESTRATOR] Failed to create error alerts")

        # Monitoring checks run every cycle (not just when idle).
        # Every step below runs through _bounded_leader_step so the tail keeps its
        # own heartbeat fresh and no single hung await can wedge leader liveness
        # (incident 2026-06-15). Budgets are safety nets, comfortably under
        # ORCHESTRATOR_HANG_SECONDS — they should not fire in healthy operation.
        try:
            await _bounded_leader_step(
                "monitoring_window", _check_monitoring_window(), budget=30
            )
            await _bounded_leader_step(
                "external_health", _check_external_health(), budget=45
            )
            await _bounded_leader_step("pause_mode", _check_pause_mode(), budget=30)
            # Orphaned task recovery — every 120s. Stays on the leader cycle but
            # is now bounded (per-cycle row cap + per-GitHub-call asyncio.wait_for
            # + per-row heartbeat in services._recover_orphaned_tasks, C3) so it
            # can no longer saturate the cycle on a large orphan backlog.
            if _is_cadence_due(_last_orphan_check, 120):
                _last_orphan_check = _utcnow()
                await _bounded_leader_step(
                    "recover_orphaned_tasks", _recover_orphaned_tasks(), budget=120
                )
            # C3: EVALUATING-timeout recovery moved OFF the leader cycle into the
            # dedicated _evaluating_timeout_service background task (lifecycle.py)
            # with its own 300s schedule + per-tick timeout. The dispatch cycle no
            # longer awaits its serial GitHub I/O, so leader liveness is decoupled
            # from EVALUATING-backlog size. (_last_evaluating_timeout_check is kept
            # as a module attribute for back-compat with tests/metrics.)
            # _post_commit_pending sweeper — every 60s. Retries post-commit
            # processing for terminal tasks whose prior attempt timed out
            # (GitHub quota exhaustion, transient failures, etc.) without
            # waiting for the agent to retry (the VM has usually terminated).
            if _is_cadence_due(_last_post_commit_sweep, 60):
                _last_post_commit_sweep = _utcnow()
                # Self-heartbeats per task internally; the 120s outer bound only
                # trips if it stops making progress (e.g. a single relock-starved
                # hot row — the 2026-06-15 trigger), abandoning the tick instead
                # of letting it pin the tail.
                await _bounded_leader_step(
                    "post_commit_sweep", _sweep_post_commit_pending(), budget=120
                )
            # Stranded-sidecar sweep — every 120s. Resolves EVALUATING source
            # tasks whose side-car challenger died without posting status (reaper
            # kill, VM death with retries exhausted, cancellation). Calls the
            # lane resolver with a synthetic FAILED payload for fail-closed semantics.
            if _is_cadence_due(_last_sidecar_sweep, 120):
                _last_sidecar_sweep = _utcnow()
                await _bounded_leader_step(
                    "stranded_sidecar_sweep",
                    _sweep_stranded_sidecar_sources(),
                    budget=90,
                )
            # Bootstrap-recovery sweep — every 300s. Finds external projects with
            # no active bootstrap task and no bootstrap_completed_at, and enqueues
            # one recovery repo_bootstrap each. Idempotent via the spawn helper.
            if _is_cadence_due(_last_bootstrap_recovery_sweep, 300):
                _last_bootstrap_recovery_sweep = _utcnow()
                try:
                    await _bounded_leader_step(
                        "bootstrap_recovery", _sweep_bootstrap_recovery(), budget=90
                    )
                except Exception:
                    logger.exception("[ORCHESTRATOR] Bootstrap recovery sweep error")
            # Lane-baseline backfill sweep — every 600s. Finds closed versions
            # lacking a stored baseline and persists one retroactively.
            # First-write-wins idempotent; best-effort/non-fatal.
            if _is_cadence_due(_last_lane_baseline_backfill, 600):
                _last_lane_baseline_backfill = _utcnow()
                try:
                    await _bounded_leader_step(
                        "lane_baseline_backfill",
                        _sweep_lane_baseline_backfill(),
                        budget=90,
                    )
                except Exception:
                    logger.exception(
                        "[ORCHESTRATOR] Lane-baseline backfill sweep error"
                    )
            # Merge-credit backfill sweep — every 600s. Retroactively re-stamps
            # under-credited children of landed rollups (rollups that landed
            # BEFORE PR #12971's forward-propagation fix left their children
            # stamped landed_on_integration=False) and recomputes each affected
            # direction's outcomes. Idempotent — a cheap no-op once converged;
            # also self-heals any future stranding the forward path misses.
            if _is_cadence_due(_last_merge_credit_backfill, 600):
                _last_merge_credit_backfill = _utcnow()
                try:
                    from app.merge_credit_backfill import (
                        _sweep_merge_credit_backfill,
                    )

                    await _bounded_leader_step(
                        "merge_credit_backfill",
                        _sweep_merge_credit_backfill(),
                        budget=90,
                    )
                except Exception:
                    logger.exception(
                        "[ORCHESTRATOR] Merge-credit backfill sweep error"
                    )
            # v22 carried finding backfill — every 600s. Upserts "resolved"
            # AuditFindingResolution rows for the 13 v22 low-severity carried
            # audit findings (fixed in d2cf1ebb8 / PR #13245) and attaches
            # a CriterionEvidence row to c-1a3ec09d. Idempotent no-op once
            # all findings are resolved.
            if _is_cadence_due(_last_v22_finding_backfill, 600):
                _last_v22_finding_backfill = _utcnow()
                try:
                    from app.release_readiness import (  # noqa: PLC0415
                        _sweep_v22_finding_backfill,
                    )

                    await _bounded_leader_step(
                        "v22_finding_backfill",
                        _sweep_v22_finding_backfill(),
                        budget=90,
                    )
                except Exception:
                    logger.exception(
                        "[ORCHESTRATOR] v22 finding backfill sweep error"
                    )
            # Stale-runner deactivation — every 300s. Marks registered fleet
            # runners that missed the heartbeat window as inactive so they drop
            # out of the dispatch cache and the admin listing. Tasks already
            # pinned to a now-dead runner are recovered by the orphaned-task
            # sweep above (no new recovery code needed).
            if _is_cadence_due(_last_runner_deactivation_sweep, 300):
                _last_runner_deactivation_sweep = _utcnow()
                try:
                    async with AsyncSessionLocal() as _runner_sweep_session:
                        await _bounded_leader_step(
                            "runner_deactivation",
                            _deactivate_stale_runners(_runner_sweep_session),
                            budget=30,
                        )
                except Exception:
                    logger.exception(
                        "[ORCHESTRATOR] Stale-runner deactivation sweep error"
                    )
            # Degraded-runner alert sweep — every 300s. Alerts the operator when a
            # runner is heartbeating normally but failing every job it claims (a
            # false-green that the deactivated-runner alert never catches). Also
            # resolves degraded alerts when the runner completes a successful job.
            if _is_cadence_due(_last_runner_degraded_sweep, 300):
                _last_runner_degraded_sweep = _utcnow()
                try:
                    async with AsyncSessionLocal() as _degraded_sweep_session:
                        await _bounded_leader_step(
                            "runner_degraded_check",
                            _check_degraded_runners(_degraded_sweep_session),
                            budget=30,
                        )
                except Exception:
                    logger.exception(
                        "[ORCHESTRATOR] Degraded-runner alert sweep error"
                    )
            # Runner-deactivated escalation sweep — every 300s. Re-notifies for
            # runner-deactivated alerts that have been open for > 1h (then every 4h)
            # so a long outage does not stay silent after the initial notification.
            if _is_cadence_due(_last_runner_escalation_sweep, 300):
                _last_runner_escalation_sweep = _utcnow()
                try:
                    from app.alert_dispatch import _maybe_escalate_runner_deactivated_alerts

                    async with AsyncSessionLocal() as _esc_session:
                        await _bounded_leader_step(
                            "runner_escalation",
                            _maybe_escalate_runner_deactivated_alerts(_esc_session),
                            budget=30,
                        )
                except Exception:
                    logger.exception(
                        "[ORCHESTRATOR] Runner-deactivated escalation sweep error"
                    )
            # Enforce-local hold timeout — every 300s. Terminally fails any
            # compute_experiment that has been held QUEUED awaiting a self-hosted
            # runner past _LOCAL_RUNNER_WAIT_TIMEOUT_SECONDS, so a require-local
            # project can never wedge forever when the operator's hardware stays
            # offline.
            if _is_cadence_due(_last_local_runner_hold_sweep, 300):
                _last_local_runner_hold_sweep = _utcnow()
                try:
                    async with AsyncSessionLocal() as _hold_sweep_session:
                        await _bounded_leader_step(
                            "local_runner_hold_timeout",
                            _fail_stale_local_runner_holds(_hold_sweep_session),
                            budget=30,
                        )
                except Exception:
                    logger.exception(
                        "[ORCHESTRATOR] Local-runner hold-timeout sweep error"
                    )
            # Drain the deferred missing-branch alert queue into durable,
            # idempotent operator Alerts. The synchronous branch resolvers
            # record intent off the hot path (no DB I/O); the durable persist
            # runs here in an async-safe context so the signal survives
            # restarts without re-spamming (one undismissed alert per project
            # column). Guarded so a flush failure never stalls monitoring.
            try:
                async with asyncio.timeout(30):
                    async with AsyncSessionLocal() as _branch_alert_session:
                        if await _shared_state.flush_pending_branch_alerts(
                            _branch_alert_session
                        ):
                            await _branch_alert_session.commit()
            except TimeoutError:
                logger.warning(
                    "[ORCHESTRATOR] branch-alert flush exceeded 30s — abandoned this tick"
                )
            except Exception:
                logger.exception("[ORCHESTRATOR] Failed to flush pending branch alerts")
            finally:
                _mark_orchestrator_progress()
            _orchestrator_consecutive_monitoring_errors = 0
        except Exception as e:
            _orchestrator_consecutive_monitoring_errors += 1
            monitoring_backoff = exponential_backoff(
                _orchestrator_consecutive_monitoring_errors
            )
            logger.error(
                f"[ORCHESTRATOR] Monitoring check error (consecutive={_orchestrator_consecutive_monitoring_errors}, backoff={monitoring_backoff:.1f}s): {e}",
                exc_info=True,
            )

        # Sleep based on whether the cycle made real dispatch progress.
        # Fast path is gated on _cycle_dispatched (a task actually left QUEUED),
        # NOT on queued_tasks being non-empty — a held/deferred but undispatchable
        # task kept the queue non-empty and spun the loop at 2 Hz (WI-4). See
        # _select_cycle_sleep for the full rationale.
        try:
            _cycle_error_backoff = (
                exponential_backoff(_orchestrator_consecutive_errors)
                if _orchestrator_consecutive_errors > 0
                else 0.0
            )
            _cycle_monitoring_backoff = (
                exponential_backoff(_orchestrator_consecutive_monitoring_errors)
                if _orchestrator_consecutive_monitoring_errors > 0
                else 0.0
            )
            await asyncio.sleep(
                _select_cycle_sleep(
                    consecutive_errors=_orchestrator_consecutive_errors,
                    consecutive_monitoring_errors=_orchestrator_consecutive_monitoring_errors,
                    cycle_dispatched=_cycle_dispatched,
                    error_backoff=_cycle_error_backoff,
                    monitoring_backoff=_cycle_monitoring_backoff,
                )
            )
            # Check autonomous research every cycle — interval gate inside handles throttling.
            # Spawns only when VM slots are available (not tied to queue being empty).
            await _bounded_leader_step(
                "idle_research", _check_idle_research(), budget=60
            )
            # Utilization-triggered evergreen backlog auto-fill (d-2877ddc7).
            # Defaulted OFF; interval gate + mode gate handled inside. Additive
            # idle-fill parallelism — never competes with the release-critical path.
            await _bounded_leader_step(
                "evergreen_fill", _check_evergreen_fill(), budget=60
            )
        except asyncio.CancelledError:
            logger.info("[ORCHESTRATOR] Worker shutting down.")
            break
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Sleep/idle check error: {e}", exc_info=True)
