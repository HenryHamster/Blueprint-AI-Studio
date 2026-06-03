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
- _provision_and_handle() - Attempts to provision a task, with retry and fallback logic
- _monitor_evaluating_tasks() - Checks CI completion and review status for EVALUATING tasks

The orchestrator is the single source of truth for when work moves from QUEUED →
PROVISIONING. All other state transitions (PROVISIONING → IN_PROGRESS, IN_PROGRESS →
COMPLETED/FAILED, etc.) are driven by agent reports or task lifecycle handlers.
"""

import asyncio
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, NamedTuple, Tuple

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.compute import (
    INFRA_PROVISION_PATTERNS,
    VM_PROVISIONING_MODEL,
    _capture_serial_for_vm,
    any_zone_available,
    get_provision_error,
    get_vm_zone,
    get_zone_health_summary,
    prepare_and_store_agent_secret,
    provision_agent_vm,
    record_zone_failure,
    zone_recovery_wait_seconds,
)
from app.database import AsyncSessionLocal, resolve_dialect_name
from app.models import (
    DeploymentState,
    Project,
    Task,
    TaskStatus,
    bank_task_current_attempt,
    with_task_attempt_banking,
)
from app.orchestrator_filters import (
    filter_file_conflicts,
    filter_rollup_conflicts,
    filter_held_directions,
    filter_pipeline_duplicates,
    filter_migration_serialization,
    filter_scope_empty,
    cap_pipeline_slots,
    reserve_slots_for_types,
    apply_project_fairness,
    _project_fairness_weight,
    collect_dep_uuids,
)
from app.stall_alert_diagnostics import (
    _STALL_TITLE_PREFIX,
    _compute_all_accounts_blocked_action,
    _compute_retry_after_escalation_action,
    _format_stall_alert,
    _missing_runtime_credentials_stall_reason,
)
from app.state_persistence import (
    _persist_circuit_breakers,  # noqa: F401 — re-exported; callers import via app.orchestrator
    _persist_zone_health,
    _persist_self_healing_state_bundle,
)
from app.storage import upload_json_payload
import app.shared_state as _shared_state
from app.shared_state import (
    runtime_config as _runtime_config,
    sync_runtime_config_from_state as _sync_runtime_config_from_state,
)
from app.task_locking import task_lock
from app.time_utils import (
    _utcnow,
    clear_retry_after,
    rate_limit_retry_minutes,
    set_retry_after,
)

logger = logging.getLogger("blueprint")


def _clear_retry_after_state(task_or_ctx) -> None:
    """Clear retry_after hold and its reset counter on a Task or context dict.

    Why: the counter feeds an escalation alert; leaving it across recovery
    episodes causes false escalations on the next rate-limit episode.

    Audit 2026-04-25 W1: ``task.retry_after_until`` is now a real column
    so callers should pass the Task; the dict-only form is kept for legacy
    callsites that operate on bare context dicts (e.g. spawn paths).
    """
    clear_retry_after(task_or_ctx)


def _capture_ci_polling_snapshot(task: Task) -> dict:
    """Snapshot the CI-polling fields a lock-window break must protect.

    R5-W2-A: ``updated_at`` advances every ~30s via the heartbeat
    ``onupdate=_utcnow`` and would false-defer the CI-polling re-acquire
    on every overlapping heartbeat. We validate against content fields
    instead — the ones whose mutation indicates a peer took over CI
    handling for this task:

      - ``status``: the most load-bearing; if a peer transitioned the
        task out of EVALUATING (e.g. to COMPLETED on PR merge or FAILED
        on PR delete), we must not re-apply CI-polling mutations on top.
      - ``review_loop.{pr_number, ci_passed, review_approved, iteration}``:
        if any of these moved, the review pipeline advanced under us.

    Heartbeat-only updates touch none of these.
    """
    rl = (task.context or {}).get("review_loop") or {}
    return {
        "status": task.status,
        "pr_number": rl.get("pr_number"),
        "ci_passed": rl.get("ci_passed"),
        "review_approved": rl.get("review_approved"),
        "iteration": rl.get("iteration"),
    }


def _ci_waiting_sql_filter(session):
    """Dialect-portable WHERE clause selecting only EVALUATING rows whose
    ``context.review_loop.ci_waiting`` is truthy.

    C3: the CI-poll loop previously fetched ALL EVALUATING rows (up to 200) and
    then ``_lock_task_for_mutation``'d each one only to discover most are not
    CI-waiting and immediately ``rollback()`` — an O(N) lock-and-rollback cost
    on the leader's hot cycle proportional to the whole EVALUATING backlog, not
    to the (usually small) CI-waiting subset. Pushing the filter into SQL drops
    that cost: only genuinely CI-waiting rows are locked.

    This matches EXACTLY the loop's own gate (``ci_loop.get("ci_waiting")`` where
    ``ci_loop = context["review_loop"]``), so it is a pure prefilter — no
    behavioural change to which tasks the loop acts on. The Python-side guard
    inside the loop remains as cheap defense-in-depth.

    Postgres: ``->'review_loop'->>'ci_waiting'`` compared to the string
    ``'true'`` (JSONB booleans render as ``true``/``false`` via ``->>``).
    SQLite (standalone tests): ``json_extract`` returns the integer 1 for a JSON
    ``true``, so compare against 1.
    """
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        return _func.json_extract(Task.context, "$.review_loop.ci_waiting") == 1
    return Task.context["review_loop"]["ci_waiting"].astext == "true"


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
        .with_for_update(skip_locked=True)
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


# ---------------------------------------------------------------------------
# Module-level globals managed by orchestrator_worker
# ---------------------------------------------------------------------------

_last_pool_refresh: datetime = _utcnow()
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
_last_deadlock_check: float = 0.0
_last_zone_exhaustion_log: datetime | None = None
_last_concurrency_broadcast: float = 0.0
_last_type_throttle_broadcast: dict[str, float] = {}
_last_type_unthrottle_broadcast: dict[str, float] = {}
_previously_failing_types: set[str] = set()

# Debounce: fire the all-accounts-blocked alert once per stall episode.
# Reset when any task dispatches successfully (stall resolved), then re-arm
# if every eligible task is again blocked by account rate limits.
_all_accounts_blocked_alerted: bool = False


async def _emit_all_accounts_blocked_stall(
    session: AsyncSession,
    earliest_recovery: datetime | None,
    blockage_reasons: set[str] | None = None,
) -> None:
    """Broadcast, alert, and notify operators that the pipeline is stalled on
    Max-plan account exhaustion. Intended to be called once per stall episode."""
    from app.broadcast import broadcast_to_project, manager as ws_manager
    from app.models import Alert, AlertSeverity
    from app.notification_dispatch import dispatch_external_notification

    recovery_iso = earliest_recovery.isoformat() if earliest_recovery else None
    title, msg = _format_stall_alert(blockage_reasons, earliest_recovery)
    try:
        await ws_manager.broadcast_to_all(
            "pipeline_throttle",
            {
                "reason": "all_accounts_blocked",
                "detail": msg,
                "earliest_recovery": recovery_iso,
                "blockage_reasons": sorted(blockage_reasons)
                if blockage_reasons
                else [],
            },
        )
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] Failed to broadcast all_accounts_blocked throttle"
        )
    try:
        _stall_batch = 0
        _stall_batch_size = 500
        while True:
            project_ids = list(
                (
                    await session.execute(
                        select(Project.id)
                        .order_by(Project.id)
                        .offset(_stall_batch * _stall_batch_size)
                        .limit(_stall_batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not project_ids:
                break
            # Dedup across the "Pipeline stalled — ..." family, not the exact
            # title: with dynamic copy a new stall episode could otherwise stack
            # a second alert if its blockage shape (rate-limit vs capacity) flips.
            existing_rows = (
                await session.execute(
                    select(Alert.project_id).where(
                        Alert.project_id.in_(project_ids),
                        Alert.task_id.is_(None),
                        Alert.severity == AlertSeverity.ERROR,
                        Alert.dismissed_at.is_(None),
                        Alert.title.like(f"{_STALL_TITLE_PREFIX}%"),
                    )
                )
            ).all()
            projects_with_alert = {row[0] for row in existing_rows}
            for proj_id in project_ids:
                if proj_id not in projects_with_alert:
                    alert = Alert(
                        project_id=proj_id,
                        severity=AlertSeverity.ERROR,
                        title=title,
                        message=msg,
                    )
                    session.add(alert)
                    await broadcast_to_project(
                        str(proj_id),
                        "alert_created",
                        {
                            "alert_id": str(alert.id),
                            "severity": "ERROR",
                            "title": title,
                        },
                    )
                asyncio.ensure_future(
                    dispatch_external_notification(
                        proj_id,
                        "PIPELINE_STALLED",
                        {
                            "reason": "all_accounts_blocked",
                            "earliest_recovery": recovery_iso or "",
                            "detail": msg,
                        },
                    )
                )
            _stall_batch += 1
    except Exception:
        logger.exception(
            "[ORCHESTRATOR] Failed to create all-accounts-blocked stall alert"
        )
    logger.warning(f"[ORCHESTRATOR] {title}: {msg}")


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

# How long the worker may go without a progress heartbeat before /health
# reports it as wedged (→ Cloud Run liveness restart). Must comfortably exceed
# the longest single awaited step in a cycle (a CI GitHub call + its retries,
# the 30s session-acquire timeout, etc.) so a slow-but-progressing worker is
# never killed — only a truly hung one. Env-overridable for ops tuning.
ORCHESTRATOR_HANG_SECONDS = int(os.getenv("ORCHESTRATOR_HANG_SECONDS", "180"))


def _mark_orchestrator_progress() -> None:
    """Heartbeat that the orchestrator worker is actively progressing within a
    cycle. Liveness (`/health`) is gated on the freshness of this timestamp, not
    on full-cycle completion, so a legitimately long cycle (large EVALUATING
    backlog → many sequential CI polls) is not mistaken for a hung process and
    killed — which would discard the cycle's progress and spiral. See
    [[reference-orchestrator-liveness-deathspiral]] / incident 2026-05-28."""
    global _orchestrator_last_progress
    _orchestrator_last_progress = _utcnow()


class OrchestratorLiveness(NamedTuple):
    """Result of :func:`evaluate_orchestrator_liveness`.

    - ``alive``:        a liveness signal (progress heartbeat or completed
                        cycle) has been recorded at least once.
    - ``healthy``:      ``alive`` AND the freshest signal is within
                        ``ORCHESTRATOR_HANG_SECONDS`` (the ``/health`` verdict).
    - ``stalled``:      the in-process watchdog should cancel-and-restart — a
                        definite hang, including a first cycle that started but
                        never made progress past the threshold.
    - ``progress_age``: age in seconds of the freshest (progress|cycle) signal,
                        or ``None`` if neither has been recorded yet.
    - ``reason``:       human-readable cause when ``stalled`` (empty otherwise).
    """

    alive: bool
    healthy: bool
    stalled: bool
    progress_age: float | None
    reason: str


def evaluate_orchestrator_liveness(
    now: datetime | None = None,
) -> "OrchestratorLiveness":
    """Single source of truth for orchestrator liveness.

    Shared by the HTTP ``/health`` endpoint and the in-process background-task
    watchdog so the two judgments can never drift apart again. Liveness is gated
    on the in-cycle progress *heartbeat* (``_orchestrator_last_progress``),
    falling back to the last completed cycle (``_orchestrator_last_cycle``),
    against ``ORCHESTRATOR_HANG_SECONDS``. A legitimately long-but-progressing
    cycle is therefore healthy; only a worker that has emitted no progress for
    longer than the threshold is hung.

    Both call sites previously duplicated the signal + threshold logic — the
    watchdog drifted to full-cycle-completion + a hardcoded 120s and started
    killing healthy-but-slow cycles in a restart loop (incident 2026-05-28; only
    ``/health`` was fixed in PR #6678). This helper is the de-duplicated fix.
    """
    if now is None:
        now = _utcnow()

    last = _orchestrator_last_progress or _orchestrator_last_cycle
    if last is not None:
        age = (now - last).total_seconds()
        if age < ORCHESTRATOR_HANG_SECONDS:
            return OrchestratorLiveness(True, True, False, age, "")
        return OrchestratorLiveness(
            True, False, True, age, f"no progress heartbeat in {int(age)}s"
        )

    # Cold start: no progress/cycle recorded yet. If a cycle has *started* but
    # never reached its first heartbeat within the threshold, that is a wedged
    # first cycle the watchdog must recover.
    if _orchestrator_cycle_started is not None:
        age = (now - _orchestrator_cycle_started).total_seconds()
        if age >= ORCHESTRATOR_HANG_SECONDS:
            return OrchestratorLiveness(
                False,
                False,
                True,
                None,
                f"first cycle made no progress in {int(age)}s "
                "(started, never heartbeat)",
            )

    # Nothing recorded yet, or still inside the startup window — not alive yet,
    # but not a hang the watchdog should cancel.
    return OrchestratorLiveness(False, False, False, None, "")


def _resolve_ci_fix_branch(
    review_loop: dict | None, payload: dict | None, pr_data: dict | None
) -> str:
    """Resolve the head branch for a CI/conflict fix-task spawn.

    Prefers our stored branch (``review_loop`` then ``payload``) but falls back
    to the PR's *live* head ref when both are empty/stale, so a missing stored
    branch can't reach ``_spawn_pipeline_task`` as an empty ``FixPayload.branch``
    — which raises a ValueError every cycle and wedges the task as a poison pill
    (incident 2026-05-28: task cf1d2a25 / 7f0e7ec5)."""
    branch = (review_loop or {}).get("branch", "") or (payload or {}).get("branch", "")
    if not branch:
        branch = ((pr_data or {}).get("head", {}) or {}).get("ref", "")
    return branch or ""


async def _apply_ci_merged_landed_signal(
    ci_task: Task,
    session: AsyncSession,
    *,
    base_branch: str,
    review_loop: dict,
) -> bool:
    """Stage the pr_merged landed signal only when the merged PR landed on
    the project's integration branch.

    Returns True when the merged PR landed on the integration branch. When the
    base branch resolves to a distinct promotion target, the helper withholds
    pr_merged and stages the same branch-safety alert the rollup boundary uses.
    """
    from app.landed_signal import apply_merged_landed_signal

    project = (
        await session.get(Project, ci_task.project_id) if ci_task.project_id else None
    )
    hotfix_lane = bool(
        (ci_task.first_payload or {}).get("hotfix")
        or (ci_task.context or {}).get("hotfix")
        or review_loop.get("hotfix")
    )
    return apply_merged_landed_signal(
        ci_task,
        project,
        base_branch=base_branch,
        hotfix_lane=hotfix_lane,
    )


# Track in-flight provision coroutines so stale recovery can cancel them.
# Without cancellation, zombie provisions hold semaphore slots for up to 360s,
# starving new provisions and creating a cascading failure where no VM can
# claim its task (the reaper assigns a new vm_instance_id before the VM boots).
_provision_coroutines: Dict[str, asyncio.Task] = {}

# Columns the dispatch loop loads for active (PROVISIONING/IN_PROGRESS) tasks.
# Heavy JSONB (status_log, depends_on, history) stays deferred. Anything a
# downstream filter touches (e.g. filter_file_conflicts -> context/title,
# filter_migration_serialization -> payloads via first_payload) MUST be here,
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
)


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


async def _persist_deps_satisfied(task: Task) -> None:
    """Durably stamp the sticky ``_deps_satisfied`` readiness marker on a
    dependency-bearing task whose dependencies the orchestrator has just
    resolved as fully satisfied.

    This marker is the contract between the orchestrator's authoritative,
    GitHub-aware dependency / PR-merge resolution (``_check_candidate_task_deps``
    returning ``"eligible"``) and the two warm-reuse hot paths — the
    ``/next-role`` warm-backlog scan and the dispatch first-refusal hold. Both
    treat a task carrying this marker as warm-claimable WITHOUT re-running dep
    resolution at high frequency. It is stamped only after deps resolve eligible
    (a failed dep cascade-cancels the dependent instead of stamping), so the
    marker can never outlive an unsatisfied dependency.

    Persisted in its own session (mirroring the dep-remap write) so the Step 3
    dispatch re-read and the next-role scan both observe it, and mirrored onto
    the in-memory ``task`` so the same-cycle dispatch sees it without a re-read.
    Idempotent: a no-op when the marker is already present.
    """
    if (task.context or {}).get("_deps_satisfied"):
        return
    async with AsyncSessionLocal() as deps_session:
        deps_task = await deps_session.get(Task, task.id)
        if deps_task is not None:
            deps_task.context = {
                **(deps_task.context or {}),
                "_deps_satisfied": True,
            }
            deps_session.add(deps_task)
            await deps_session.commit()
    task.context = {**(task.context or {}), "_deps_satisfied": True}


from app.orchestrator_health import (
    _check_candidate_task_deps,
    get_confirmed_down_critical_deps,
)


async def _cascade_cancel_dependent(task_id, project_id, broadcast_to_project) -> int:
    """Cancel a dependent whose prerequisite genuinely failed, cascading to its
    non-terminal children, releasing any held account slot, and tearing down its VM.

    Single source of truth shared by the main dispatch dependency check and the
    deadlock-breaker sweep so both apply identical dependency-death policy — a task
    whose real prerequisite failed is recognized once and cancelled, never
    dispatched as impossible work.

    Returns the number of tasks cancelled (the dependent itself plus its cascaded
    children) so callers can accumulate a single deduplicated operator alert per
    failed prerequisite. Returns 0 if the dependent row no longer exists.
    """
    from app.max_plan_pool import release_max_plan_account, get_task_llm_account_id
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.alert_dispatch import _maybe_create_failure_alert

    vm_id = None
    cancelled_count = 0
    async with AsyncSessionLocal() as cancel_session:
        cancel_task = await cancel_session.get(Task, task_id)
        if cancel_task is None:
            return 0
        vm_id = cancel_task.vm_instance_id
        cancel_task.status = TaskStatus.CANCELLED
        if cancel_task.vm_provisioned_at:
            cancel_task.vm_stopped_at = _utcnow()
        cancel_task.latest_status_message = "Cancelled: dependency task failed"
        # Release account slot if held
        cancel_acct = get_task_llm_account_id(cancel_task)
        if cancel_acct and not (cancel_task.context or {}).get("_slot_released"):
            await release_max_plan_account(cancel_acct)
            cancel_task.context = {
                **(cancel_task.context or {}),
                "_slot_released": True,
            }
        cancel_session.add(cancel_task)
        # Cascade cancel to children in non-terminal states
        children = (
            (
                await cancel_session.execute(
                    select(Task).where(
                        Task.parent_task_id == task_id,
                        Task.status.not_in(
                            [
                                TaskStatus.COMPLETED,
                                TaskStatus.FAILED,
                                TaskStatus.CANCELLED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for child in children:
            child.status = TaskStatus.CANCELLED
            if child.vm_provisioned_at:
                child.vm_stopped_at = _utcnow()
            child.latest_status_message = "Cancelled: parent dependency failed"
            child_acct = get_task_llm_account_id(child)
            if child_acct and not (child.context or {}).get("_slot_released"):
                await release_max_plan_account(child_acct)
                child.context = {**(child.context or {}), "_slot_released": True}
            cancel_session.add(child)
        await _maybe_create_failure_alert(
            cancel_task,
            cancel_session,
            "Cascade cancelled: dependency task failed",
        )
        cancelled_count = 1 + len(children)
        await cancel_session.commit()
    await broadcast_to_project(
        str(project_id),
        "task_updated",
        {"task_id": str(task_id), "status": TaskStatus.CANCELLED},
    )
    await _ensure_vm_deleted(vm_id, reason="dependency_cancelled")
    logger.error(f"[ORCHESTRATOR] Cancelled task {task_id} — dependency failed")
    return cancelled_count


async def _apply_deadlock_classification(
    classified, broadcast_to_project, eligible_ids, scan_label=""
) -> int:
    """Apply a :func:`_classify_stuck_tasks` result.

    True cycles and satisfied-dead deps get ``depends_on`` cleared and become
    dispatchable; a dependent whose real prerequisite genuinely failed is
    cascade-cancelled (NOT dispatched) via the same policy the main check uses.
    Returns the number of tasks acted on.
    """
    if not classified:
        return 0
    cancels: list[tuple] = []
    acted = 0
    all_ids = [tid for tid, _ in classified]
    async with AsyncSessionLocal() as fix_session:
        rows = await fix_session.execute(select(Task).where(Task.id.in_(all_ids)))
        tasks_by_id = {t.id: t for t in rows.scalars().all()}
        for task_id, reason in classified:
            fix_task = tasks_by_id.get(task_id)
            if reason == "cascade_cancel":
                if fix_task:
                    cancels.append((task_id, fix_task.project_id))
                    acted += 1
                continue
            if fix_task:
                if reason == "cycle":
                    msg = f"Dependencies cleared: dependency cycle detected{scan_label}"
                    tag = "cycle broken"
                else:
                    msg = (
                        "Dependencies cleared: all deps satisfied/cancelled"
                        f"{scan_label}"
                    )
                    tag = "dead deps cleared"
                logger.warning(
                    f"[ORCHESTRATOR] Breaking deadlock ({tag}{scan_label}): "
                    f"clearing depends_on for task {task_id}"
                )
                fix_task.depends_on = []
                fix_task.latest_status_message = msg
                fix_session.add(fix_task)
                await broadcast_to_project(
                    str(fix_task.project_id),
                    "task_updated",
                    {"task_id": str(task_id), "status": "QUEUED", "message": msg},
                )
            eligible_ids.append(task_id)
            acted += 1
        await fix_session.commit()
    # Cascade-cancel after the clear-commit so each cancel manages its own session.
    for task_id, project_id in cancels:
        await _cascade_cancel_dependent(task_id, project_id, broadcast_to_project)
    return acted


async def _mark_unblocked_dependents(completed_task: Task, verify_pr_merged) -> int:
    """Completion-side trigger: when a task finishes and its PR has landed on the
    integration branch, proactively stamp the sticky ``_deps_satisfied`` readiness
    marker on any QUEUED dependent whose dependencies now authoritatively resolve
    as satisfied.

    Without this, a just-unblocked dependent stays unmarked until a later
    capacity-and-window-bounded dispatch scan happens to reach it, so the
    just-completed task's warm VM — polling for follow-on work inside its
    teardown-defer grace window — skips it and tears down cold, and the
    orchestrator then cold-provisions a fresh VM for the very dependent the
    completion just unblocked. Marking it here makes the obvious sibling-to-sibling
    fan-out reusable in the saturated-fleet regime where reuse matters most.

    The readiness signal is NOT loosened: this reuses the same authoritative,
    GitHub-aware ``_check_candidate_task_deps`` resolution the dispatch scan uses,
    and stamps only the ``"eligible"`` result — a dependent with any still-unresolved
    or failed dependency continues to carry no marker and continue to cold-dispatch
    and cascade-cancel exactly as before. Adds no cold provisioning.

    Async hot-loop safety: dependents and their dependency rows are loaded with
    full ``select(Task)`` (every column present, no partial projection) so the
    downstream attribute reads inside ``_check_candidate_task_deps`` cannot trip a
    MissingGreenlet. The eligibility pass performs no commits, so the attached rows
    are never expired mid-iteration; the marker write happens afterwards in its own
    session.
    """
    completed_id_str = str(completed_task.id)
    project_id = completed_task.project_id
    eligible_ids: list[uuid.UUID] = []
    # Bound the candidate scan. ``depends_on`` is a JSON column filtered in Python
    # below, so the cap is on the raw QUEUED-dependent pool, not the matched subset.
    # Order deterministically (oldest first) so the bounded window is stable rather
    # than nondeterministic, and surface a warning when it is saturated so the
    # silent-drop of surplus dependents for a >cap fan-out is observable.
    _DEPENDENT_SCAN_CAP = 100
    async with AsyncSessionLocal() as dep_session:
        dependents = (
            (
                await dep_session.execute(
                    select(Task)
                    .where(
                        Task.status == TaskStatus.QUEUED,
                        Task.requires_approval.is_(False),
                        Task.project_id == project_id,
                        Task.depends_on.isnot(None),
                    )
                    .order_by(Task.created_at)
                    .limit(_DEPENDENT_SCAN_CAP)
                )
            )
            .scalars()
            .all()
        )
        if len(dependents) >= _DEPENDENT_SCAN_CAP:
            logger.warning(
                "[warm-reuse] _mark_unblocked_dependents hit the %d-dependent scan "
                "cap for project %s after task %s; surplus QUEUED dependents are not "
                "marked this pass and will fall back to the dispatch scan",
                _DEPENDENT_SCAN_CAP,
                project_id,
                completed_id_str,
            )
        dependents = [
            d
            for d in dependents
            if completed_id_str in (d.depends_on or [])
            and not (d.context or {}).get("_deps_satisfied")
        ]
        if not dependents:
            return 0
        # Batch every dependency row across all dependents into one query.
        per_dep_uuids: dict[uuid.UUID, list[uuid.UUID]] = {}
        all_dep_uuids: set[uuid.UUID] = set()
        for d in dependents:
            du: list[uuid.UUID] = []
            for raw in d.depends_on or []:
                try:
                    du.append(uuid.UUID(raw))
                except (ValueError, AttributeError):
                    continue
            per_dep_uuids[d.id] = du
            all_dep_uuids.update(du)
        dep_by_id: dict[uuid.UUID, Task] = {}
        if all_dep_uuids:
            dep_by_id = {
                dt.id: dt
                for dt in (
                    await dep_session.execute(
                        select(Task).where(Task.id.in_(all_dep_uuids))
                    )
                )
                .scalars()
                .all()
            }
        for d in dependents:
            du = per_dep_uuids[d.id]
            dep_tasks = [dep_by_id[u] for u in du if u in dep_by_id]
            result_str, _ = await _check_candidate_task_deps(
                d, dep_tasks, du, dep_session, verify_pr_merged
            )
            if result_str == "eligible":
                eligible_ids.append(d.id)
    if not eligible_ids:
        return 0
    async with AsyncSessionLocal() as stamp_session:
        for r in (
            (
                await stamp_session.execute(
                    select(Task).where(Task.id.in_(eligible_ids))
                )
            )
            .scalars()
            .all()
        ):
            if not (r.context or {}).get("_deps_satisfied"):
                r.context = {**(r.context or {}), "_deps_satisfied": True}
                stamp_session.add(r)
        await stamp_session.commit()
    # Surface the completion-side trigger's lift on the reuse funnel so operators
    # can confirm the widening change is converting just-unblocked dependents into
    # warm-claimable work. Best-effort: telemetry must never abort the completion
    # cycle. Recorder no-ops on a non-positive count, but the early ``return 0``
    # paths above carry no marked dependents, so this single call at the marked-some
    # landing is the authoritative record for all three completion callers.
    from app.warm_backlog_reuse import record_completion_dependents_marked

    try:
        record_completion_dependents_marked(len(eligible_ids))
    except Exception:
        logger.exception("[warm-reuse] completion_dependents_marked telemetry failed")
    return len(eligible_ids)


async def orchestrator_worker():
    """
    Background worker that polls for QUEUED tasks and provisions VMs.
    This ensures tasks are not lost if the server restarts before provisioning.
    """
    # Late imports to avoid circular dependency with app.main
    from app.max_plan_pool import (
        load_max_plan_pool,
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
    from app.rollup_management import _verify_pr_merged
    from app.review_pipeline import _spawn_pipeline_task
    from app.broadcast import broadcast_to_project, manager as ws_manager
    from app.services import (
        _recover_orphaned_tasks,
        _sweep_post_commit_pending,
    )
    from app.routers.agent import get_github_token_for_task
    from app.github import (
        close_pull_request,
        get_check_runs_cached,
        get_pull_request,
        pr_is_not_behind_for,
        reopen_pull_request,
        update_branch_with_sha_retry,
    )

    global _last_pool_refresh, _orchestrator_last_cycle, _orchestrator_cycle_started
    global _orchestrator_consecutive_errors, _orchestrator_consecutive_monitoring_errors
    global _last_orphan_check, _last_post_commit_sweep
    while True:
        queued_tasks = []
        session = None  # defined before try so except handler can reference it
        try:
            _orchestrator_cycle_started = _utcnow()
            cycle_start = time.monotonic()
            _mark_orchestrator_progress()
            tasks_recovered = 0

            # Periodic pool refresh — pick up admin changes without restart
            if (_utcnow() - _last_pool_refresh).total_seconds() >= 300:
                await load_max_plan_pool()
                _last_pool_refresh = _utcnow()

            # Periodic zone health persist — prevents retry storms after restart
            global _last_zone_health_persist
            if (_utcnow() - _last_zone_health_persist).total_seconds() >= 60:
                await _persist_zone_health()
                _last_zone_health_persist = _utcnow()

            # Periodic self-healing state persist — anomaly counts, direction holds, pipeline events.
            # All three live on the same DeploymentState row, so coalesce into a
            # single session/commit (M-P6) instead of three round-trips.
            global _last_anomaly_state_persist
            if (_utcnow() - _last_anomaly_state_persist).total_seconds() >= 60:
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
            if (_utcnow() - _last_anomaly_sweep).total_seconds() >= 600:
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
                # payloads is needed by filter_migration_serialization (Step 2.72)
                # via _is_migration_task -> task.first_payload.
                # Without them, attribute access triggers a sync lazy-load from an
                # async context and crashes the whole dispatch cycle.
                _active_light_cols = load_only(*ACTIVE_TASKS_LIGHT_COLUMNS)
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
                                    acct_id, revert_daily_task_count=True
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
                                acct_id, revert_daily_task_count=True
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
                # Cross-check with actual GCP VM count (cached, refreshed every 2 min)
                actual_vm_count = await _get_actual_gcp_vm_count()
                # Warm-pool slot accounting: a pre-provisioned idle pool VM is a
                # real agent-vm-* instance, so _get_actual_gcp_vm_count() and the
                # GCP cleanup count BOTH include it. Charging it against the
                # dispatch budget would starve real queued tasks. Exclude the
                # non-task-bound pool members (PROVISIONING/IDLE) from the GCP
                # cross-check — they live in a dedicated pool budget
                # (warm_pool_max_vms, enforced by the maintainer), NOT the dispatch
                # budget. LEASED pool members run a task and are already counted in
                # active_count, so they must NOT be subtracted.
                from app.warm_pool import count_idle_pool_vms, is_warm_pool_enabled

                # Only subtract idle pool inventory when the pool is enabled. When
                # disabled the maintainer/idle-sweep early-return and never clear
                # PoolVm rows, but the generic GCP cleanup still deletes the
                # underlying VMs — so stale IDLE rows would over-subtract here,
                # inflating slots_available past max_concurrent_vms. Gate it off.
                idle_pool_count = (
                    await count_idle_pool_vms(session) if is_warm_pool_enabled() else 0
                )
                gcp_dispatch_vms = max(0, actual_vm_count - idle_pool_count)
                effective_count = max(active_count, gcp_dispatch_vms)
                if gcp_dispatch_vms > active_count + 2:
                    logger.warning(
                        f"[ORCHESTRATOR] GCP VM count ({actual_vm_count}, "
                        f"{idle_pool_count} idle-pool) > DB active count ({active_count}) "
                        f"— possible orphans, using GCP count for slot calculation"
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
                    if (time.monotonic() - _last_concurrency_broadcast) > 60:
                        _last_concurrency_broadcast = time.monotonic()
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
                _now_mono = time.monotonic()
                if _failing_types:
                    for _ftype in _failing_types:
                        if (
                            _now_mono - _last_type_throttle_broadcast.get(_ftype, 0.0)
                        ) > 60:
                            _last_type_throttle_broadcast[_ftype] = _now_mono
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
                    if (
                        _now_mono - _last_type_unthrottle_broadcast.get(_rtype, 0.0)
                    ) > 60:
                        _last_type_unthrottle_broadcast[_rtype] = _now_mono
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
                    # Sleep before retrying so we don't tight-loop the gate check
                    # at ~50 cycles/sec, which spams logs and trips the cycle-hung
                    # monitor (no cycle ever "completes" because we keep continuing).
                    # ORCHESTRATOR_THROTTLE_SECONDS matches the failure-rate gate
                    # at line 877 — both back off the same way.
                    await asyncio.sleep(ORCHESTRATOR_THROTTLE_SECONDS)
                    continue

                # Reconcile Max Plan active_tasks counters from DB each cycle.
                # Fetch the JSON path raw and group in Python — avoids Postgres
                # GROUP BY quirks with JSONB path expressions (SQLAlchemy was
                # emitting a SELECT that Postgres saw as referencing task.context
                # without a matching GROUP BY token).
                active_per_account: Dict[str, int] = {}
                _acct_id_rows = (
                    await session.execute(
                        select(Task.context).where(
                            Task.status.in_(
                                [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                            ),
                        )
                    )
                ).all()
                for row in _acct_id_rows:
                    ctx = row[0] or {}
                    acct_id = ctx.get("llm_account_id") or ctx.get(
                        "max_plan_account_id"
                    )
                    if acct_id:
                        active_per_account[acct_id] = (
                            active_per_account.get(acct_id, 0) + 1
                        )
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
                candidate_stmt = (
                    select(Task)
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
                            # Round 5 (2026-05-03): include parent_task_id — downstream
                            # filter_rollup_conflicts at orchestrator_filters.py:184 reads
                            # candidate.parent_task_id; deferring it triggers MissingGreenlet
                            # under async and crashes the entire dispatch cycle, freezing
                            # all QUEUED tasks. Same gotcha class as status_log per memory
                            # `reference_load_only_event_listeners.md` — load_only that
                            # excludes columns downstream code reads is fragile.
                            Task.parent_task_id,
                        )
                    )
                    .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
                    .limit(max(slots_available * 2, 50))
                )  # Fetch extra; NULL priority = lowest
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
                # ``_check_candidate_task_deps`` reads — a full ``select(Task)``
                # (no load_only) loads every column, so reading them on a detached
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
                    async with AsyncSessionLocal() as _dep_batch_session:
                        _dep_rows = (
                            (
                                await _dep_batch_session.execute(
                                    select(Task).where(Task.id.in_(_all_dep_uuids))
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
                from app.failure_classification import (
                    SCOPE_EMPTY as _SCOPE_EMPTY,
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
                    dep_tasks = [
                        _dep_by_id[du] for du in dep_uuids if du in _dep_by_id
                    ]

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
                        new_deps = [
                            deps_remapped.get(d, d) for d in (task.depends_on or [])
                        ]
                        if new_deps != list(task.depends_on or []):
                            async with AsyncSessionLocal() as remap_session:
                                remap_task = await remap_session.get(Task, task.id)
                                if remap_task:
                                    remap_task.depends_on = new_deps
                                    remap_session.add(remap_task)
                                    await remap_session.commit()
                            task.depends_on = new_deps
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
                        # cancel decision: first terminal-failed dep that is not a
                        # deliberate SCOPE_EMPTY no-op exit) so the heavily-tested
                        # helper signature is untouched.
                        _failed_prereq = next(
                            (
                                dt
                                for dt in dep_tasks
                                if dt.status
                                in (
                                    TaskStatus.FAILED,
                                    TaskStatus.CANCELLED,
                                )
                                and (dt.context or {}).get("failure_category")
                                != _SCOPE_EMPTY
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
                            try:
                                async with AsyncSessionLocal() as _dep_session:
                                    _dep_task = await _dep_session.get(Task, task.id)
                                    if (
                                        _dep_task
                                        and _dep_task.latest_status_message != dep_msg
                                    ):
                                        _dep_task.latest_status_message = dep_msg
                                        _dep_session.add(_dep_task)
                                        await _dep_session.commit()
                            except Exception:
                                logger.exception(
                                    f"[ORCHESTRATOR] Failed to update dep wait message for task {task.id}"
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
                global _last_deadlock_check
                now = time.time()
                if now - _last_deadlock_check > 60:
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

                    # Broader scan: check QUEUED tasks older than 10 min with deps (not just candidate batch)
                    ten_min_ago = _utcnow() - timedelta(minutes=10)
                    # Full rows (no load_only): the shared dependency-death policy
                    # reads first_payload / context on both the stuck task and its
                    # deps, so a partial projection would trip an async lazy-load.
                    broad_stmt = (
                        select(Task)
                        .where(
                            Task.status == TaskStatus.QUEUED,
                            Task.created_at < ten_min_ago,
                            Task.requires_approval.is_(False),
                            Task.depends_on.isnot(None),
                        )
                        .limit(100)
                    )
                    broad_stuck = (await session.execute(broad_stmt)).scalars().all()
                    candidate_task_ids = {t.id for t in candidate_tasks}
                    broad_stuck = [
                        t
                        for t in broad_stuck
                        if (t.depends_on or [])
                        and t.id not in eligible_ids
                        and t.id not in candidate_task_ids
                    ]
                    broad_broken = 0
                    if broad_stuck:
                        broad_stuck_ids = {t.id for t in broad_stuck}

                        # Batch all dependency UUIDs for broad scan into one query
                        all_dep_ids_broad: set[uuid.UUID] = set()
                        broad_task_dep_uuids: dict = {}
                        for stask in broad_stuck:
                            dep_uuids_b: set[uuid.UUID] = set()
                            for d in stask.depends_on or []:
                                try:
                                    dep_uuids_b.add(uuid.UUID(d))
                                except (ValueError, AttributeError):
                                    continue
                            broad_task_dep_uuids[stask.id] = dep_uuids_b
                            all_dep_ids_broad.update(dep_uuids_b)

                        broad_dep_tasks_by_id: dict = {}
                        if all_dep_ids_broad:
                            broad_dep_rows = await session.execute(
                                select(Task).where(Task.id.in_(all_dep_ids_broad))
                            )
                            broad_dep_tasks_by_id = {
                                dt.id: dt for dt in broad_dep_rows.scalars().all()
                            }

                        broad_classified = await _classify_stuck_tasks(
                            broad_stuck,
                            broad_task_dep_uuids,
                            broad_dep_tasks_by_id,
                            broad_stuck_ids,
                            session,
                            _verify_pr_merged,
                        )
                        broad_broken += await _apply_deadlock_classification(
                            broad_classified,
                            broadcast_to_project,
                            eligible_ids,
                            scan_label=" (broad scan)",
                        )

                    total_broken = narrow_broken + broad_broken
                    if total_broken > 0:
                        logger.warning(
                            f"[ORCHESTRATOR] Deadlock resolution cycle complete: "
                            f"{narrow_broken} narrow + {broad_broken} broad = {total_broken} deadlock(s) broken"
                        )

                # Step 2.5: File-conflict check — defer tasks that would conflict with in-progress work
                eligible_ids = await filter_file_conflicts(
                    eligible_ids,
                    candidate_tasks,
                    active_tasks_result,
                    session,
                    broadcast_to_project,
                )

                # Step 2.6: Defer tasks that overlap with pending-rollup files
                eligible_ids = await filter_rollup_conflicts(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.7: Defer tasks whose direction is held due to consecutive code failures
                eligible_ids = await filter_held_directions(
                    eligible_ids, candidate_tasks, session, broadcast_to_project
                )

                # Step 2.72: Serialize migration-producing tasks per project to avoid
                # duplicate alembic revisions racing on the same down_revision.
                eligible_ids = await filter_migration_serialization(
                    eligible_ids,
                    candidate_tasks,
                    active_tasks_result,
                    session,
                    broadcast_to_project,
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
                _project_by_id = {
                    t.id: t.project_id for t in candidate_tasks
                }
                _fairness_weight = {
                    t.id: _project_fairness_weight(t.priority)
                    for t in candidate_tasks
                }
                eligible_ids = apply_project_fairness(
                    eligible_ids, _project_by_id, _fairness_weight
                )

                # Slot reservation: ensure high-priority task types (e.g. create,
                # research) get a minimum share of slots when they're queued behind
                # self-perpetuating review/fix chains. Only activates when queue
                # pressure exists (more eligible tasks than open slots) and there
                # are reserved-type tasks waiting beyond the dispatch cutoff.
                eligible_ids = reserve_slots_for_types(
                    eligible_ids, candidate_tasks, _runtime_config, slots_available
                )

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
                                Task.parent_task_id,  # see candidate_stmt comment above
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
                    if (
                        _last_zone_exhaustion_log is None
                        or (now_check - _last_zone_exhaustion_log).total_seconds() >= 60
                    ):
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

                    task = await _lock_dispatchable_task_for_mutation(session, _qt_id)
                    if task is None:
                        continue
                    if task.requires_approval:
                        await session.rollback()
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
                        if (
                            not _warm_expired
                            and not _cb_suppressed
                            and _shared_state.is_vm_alive(_warm_target_vm_id)
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
                        _new_ctx = {
                            k: v
                            for k, v in _warm_ctx.items()
                            if k
                            not in (
                                "warm_target_vm_id",
                                "warm_target_pr",
                                "warm_handoff_deadline",
                                "warm_pool_pin",
                            )
                        }
                        task.context = _new_ctx
                        from sqlalchemy.orm.attributes import flag_modified

                        flag_modified(task, "context")
                        if _cb_suppressed:
                            _clear_reason = "circuit_breaker"
                        elif _warm_expired:
                            _clear_reason = "deadline_expired"
                        else:
                            _clear_reason = "vm_unresponsive"
                        # A pool pin we give up on releases its leased PoolVm row
                        # (retired so it is never re-leased — it failed to serve
                        # the pinned task in time / is unresponsive). Pool clears
                        # feed the pool breaker, NOT the warm-continuity pin-clear
                        # metric, so the two breakers stay independent.
                        if _is_pool_pin:
                            await mark_pool_vm_retiring(session, _warm_target_vm_id)
                            if _clear_reason != "circuit_breaker":
                                _shared_state.record_pool_claim_failure(_clear_reason)
                            # _pin_task_to_pool_vm stamped this retiring member's
                            # vm_instance_id onto the task. Reset it so the cold
                            # fall-through below mints a FRESH agent-vm- id instead
                            # of re-creating a GCP instance named after the VM we
                            # just scheduled for teardown (name collision racing the
                            # reaper's delete). Chain pins leave vm_instance_id None
                            # so they are unaffected; only pool pins set it.
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
                        logger.info(
                            f"[ORCHESTRATOR] Warm-pin cleared for task {task.id}: "
                            f"target_vm={_warm_target_vm_id} reason={_clear_reason}"
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
                            task_project = await session.get(Project, task.project_id)
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
                    _effective_blacklist = list({*(_blacklist or []), *_sticky}) or None
                    runtime_selection = await select_llm_runtime_for_task(
                        user_id=dispatch_user_id,
                        task=task,
                        runtime_config=_runtime_config,
                        blacklist=_effective_blacklist,
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
                    if (
                        not _warm_target_vm_id
                        and not task.depends_on
                        and is_warm_pool_enabled()
                        and runtime_selection is not None
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
                            try:
                                await session.commit()
                            except Exception:
                                logger.exception(
                                    "[ORCHESTRATOR] Failed to commit warm-pool lease "
                                    f"for task {task.id}; falling back to cold provision"
                                )
                                _shared_state.record_pool_claim_failure("commit_error")
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
                                    )
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
                                record_pool_lease_hit()
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
                            )
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
                    account = runtime_selection.account if runtime_selection else None
                    if account:
                        account_id = account["id"]
                        runtime_secret = account.get("runtime_secret") or account.get(
                            "oauth_token"
                        )
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
                                _resets = int(stall_ctx.get("_retry_after_resets") or 0)
                                _reset_threshold = _runtime_config.get(
                                    "retry_after_reset_alert_threshold", 8
                                )
                                _action = _compute_retry_after_escalation_action(
                                    _resets, _reset_threshold, 0
                                )
                                if _action == "alert":
                                    from app.models import Alert, AlertSeverity
                                    from app.max_plan_pool import describe_pool_blockage

                                    _blockage = describe_pool_blockage(
                                        task_model, user_id=dispatch_user_id
                                    )
                                    _hold_min = (
                                        (_utcnow() - task.created_at).total_seconds()
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
                    _shared_state.deploy_monitoring["last_dispatch_time"] = _utcnow()
                    session.add(task)
                    try:
                        await session.commit()
                        # active_tasks already pre-incremented in select_max_plan_account()
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
                            record_pool_cold_dispatch()
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
                                account_id, revert_daily_task_count=True
                            )
                        task.context = clear_task_llm_runtime_context(task.context)
                        await session.rollback()
                        continue

                    # Trigger GCP Compute Engine provisioning in the background
                    # All values passed as args to avoid stale closure captures across loop iterations
                    async def _provision_and_handle(
                        vm_id: str,
                        t_id: uuid.UUID,
                        p_id: uuid.UUID,
                        payloads: list,
                        secret_value: str | None,
                        config_json: str | None,
                        acct_id_for_release: str,
                        llm_provider: str | None,
                        llm_auth_mode: str | None,
                        llm_model: str | None,
                        llm_execution_mode: str | None,
                        llm_provider_metadata: dict | None,
                        llm_provider_settings: dict | None,
                        force_standard: bool = False,
                    ):
                        success = False
                        try:
                            t_type = (
                                (payloads[0] or {}).get("type", "") if payloads else ""
                            )

                            # Upload payload to GCS and store agent credentials in Secret Manager
                            # concurrently — both are independent I/O operations (~500ms each)
                            # that produce inputs for the VM startup script.
                            async def _upload_payload_safe() -> str | None:
                                if not payloads:
                                    return None
                                try:
                                    return await upload_json_payload(
                                        str(t_id), payloads
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"[ORCHESTRATOR] Failed to upload payload for task {t_id}: {e}",
                                        exc_info=True,
                                    )
                                    return None

                            payload_uri, secret_version_name = await asyncio.gather(
                                _upload_payload_safe(),
                                prepare_and_store_agent_secret(
                                    vm_id,
                                    t_id,
                                    secret_value,
                                    config_json,
                                    t_type,
                                    provider=llm_provider,
                                    auth_mode=llm_auth_mode,
                                    model=llm_model,
                                    execution_mode=llm_execution_mode,
                                    provider_metadata=llm_provider_metadata,
                                    provider_settings=llm_provider_settings,
                                    project_id=str(task.project_id),  # audit W3/E10
                                ),
                            )

                            success = await provision_agent_vm(
                                vm_id,
                                t_id,
                                payload_uri,
                                oauth_token=secret_value,
                                claude_config_json=config_json,
                                task_type=t_type,
                                secret_version_name=secret_version_name,
                                provider=llm_provider,
                                auth_mode=llm_auth_mode,
                                model=llm_model,
                                execution_mode=llm_execution_mode,
                                provider_metadata=llm_provider_metadata,
                                provider_settings=llm_provider_settings,
                                provisioning_model="STANDARD"
                                if force_standard
                                else None,
                            )
                            if success:
                                # Verify the task still wants this VM — stale recovery may have
                                # requeued the task with a new vm_id while we were provisioning.
                                # If so, this VM is an orphan: delete it immediately.
                                try:
                                    async with AsyncSessionLocal() as hb_session:
                                        hb_task = await _lock_provisioning_task_for_vm(
                                            hb_session, t_id, vm_id
                                        )
                                        if hb_task is None:
                                            current_task = await hb_session.get(
                                                Task, t_id
                                            )
                                            if (
                                                not current_task
                                                or current_task.vm_instance_id != vm_id
                                            ):
                                                current_vm = (
                                                    current_task.vm_instance_id
                                                    if current_task
                                                    else "gone"
                                                )
                                                logger.warning(
                                                    f"[ORCHESTRATOR] VM {vm_id} created but task {t_id} now has vm_id={current_vm} — deleting orphan"
                                                )
                                                await _ensure_vm_deleted(
                                                    vm_id, reason="vm_id_mismatch"
                                                )
                                                success = False
                                                return
                                        else:
                                            # Refresh vm_provisioned_at to the point the GCE API
                                            # confirmed the VM is running. This gives the agent a
                                            # fresh window to claim before the reaper considers it
                                            # a silent startup failure. last_heartbeat is left alone
                                            # so only real agent activity writes it.
                                            hb_task.vm_provisioned_at = _utcnow()
                                            # vm_spot mirrors orchestrator-mode at provision-confirm time;
                                            # see comment at the dispatch-site write above.
                                            hb_task.vm_spot = (
                                                VM_PROVISIONING_MODEL == "SPOT"
                                            ) and not force_standard
                                            # Save provision zone for diagnostics
                                            prov_zone = get_vm_zone(vm_id)
                                            hb_task.context = {
                                                **(hb_task.context or {}),
                                                "provision_zone": prov_zone,
                                            }
                                            hb_session.add(hb_task)
                                            await hb_session.commit()
                                except Exception as e:
                                    logger.warning(
                                        f"[ORCHESTRATOR] Failed to verify/touch heartbeat after provision for {t_id}: {e}"
                                    )
                        except asyncio.CancelledError:
                            # Stale recovery cancelled us, but the underlying GCP thread may
                            # still create the VM. Schedule a delayed cleanup to catch it.
                            logger.warning(
                                f"[ORCHESTRATOR] Provision for {vm_id} cancelled — scheduling orphan cleanup"
                            )

                            async def _delayed_orphan_cleanup(orphan_vm_id: str):
                                await asyncio.sleep(
                                    120
                                )  # Wait for GCP thread to finish
                                await _ensure_vm_deleted(
                                    orphan_vm_id, reason="cancelled_provision_orphan"
                                )

                            _cleanup_key = f"orphan_cleanup_{vm_id}"
                            _cleanup_task = asyncio.create_task(
                                _delayed_orphan_cleanup(vm_id)
                            )
                            _shared_state.background_tasks[_cleanup_key] = _cleanup_task
                            # Drop the dict entry once the task finishes so we don't retain
                            # completed asyncio.Task objects indefinitely (seen ~100s of these
                            # accumulating per day of provision churn — direct memory growth).
                            _cleanup_task.add_done_callback(
                                lambda _t, _k=_cleanup_key: (
                                    _shared_state.background_tasks.pop(_k, None)
                                )
                            )
                            raise
                        except Exception as e:
                            logger.error(
                                f"[ORCHESTRATOR] Provision crashed for task {t_id}: {e}",
                                exc_info=True,
                            )
                            success = False
                        finally:
                            # Always release the Max Plan account slot on failure, even if an
                            # exception escapes the try/except above (e.g. from record_task_result)
                            if not success and acct_id_for_release:
                                await release_max_plan_account(
                                    acct_id_for_release,
                                    revert_daily_task_count=True,
                                )
                        if not success:
                            provision_err = get_provision_error(vm_id)
                            logger.error(
                                f"[ORCHESTRATOR] Failed to provision VM {vm_id} for task {t_id}: {provision_err}"
                            )
                            try:
                                record_task_result(
                                    success=False,
                                    failure_class="PROVISION_FAILURE",
                                    task_id=str(t_id),
                                    task_type=t_type,
                                )
                                # Hoist zone lookup so it can be saved to context and used for zone failure recording
                                prov_zone = get_vm_zone(vm_id)
                                # Capture serial port logs before VM deletion (best-effort)
                                serial_log = None
                                try:
                                    if prov_zone and prov_zone != "unknown":
                                        serial_log = await asyncio.to_thread(
                                            _capture_serial_for_vm, vm_id, prov_zone
                                        )
                                        if serial_log:
                                            logger.error(
                                                f"[ORCHESTRATOR] Serial log for failed VM {vm_id}:\n{serial_log}"
                                            )
                                except Exception:
                                    logger.exception(
                                        f"[ORCHESTRATOR] Failed to capture serial log for VM {vm_id}"
                                    )
                                # Requeue with retry tracking — zone exhaustion is transient
                                try:
                                    async with AsyncSessionLocal() as inner_session:
                                        failed_task = (
                                            await _lock_provisioning_task_for_vm(
                                                inner_session, t_id, vm_id
                                            )
                                        )
                                        if failed_task:
                                            ctx = dict(failed_task.context or {})
                                            if prov_zone:
                                                ctx["provision_zone"] = prov_zone
                                            if serial_log:
                                                ctx["last_serial_log"] = serial_log

                                            # Classify: infra-wide (zone/quota exhaustion) vs task-specific
                                            err_lower = (provision_err or "").lower()
                                            is_infra = (
                                                any(
                                                    p in err_lower
                                                    for p in INFRA_PROVISION_PATTERNS
                                                )
                                                or not any_zone_available()
                                            )

                                            if is_infra:
                                                # Infrastructure-wide failure — don't burn retry budget
                                                infra_count = (
                                                    ctx.get(
                                                        "provision_failures_infra", 0
                                                    )
                                                    + 1
                                                )
                                                ctx["provision_failures_infra"] = (
                                                    infra_count
                                                )
                                                ctx["_infra_wait"] = True
                                                bank_task_current_attempt(
                                                    failed_task, stopped_at=_utcnow()
                                                )
                                                ctx = with_task_attempt_banking(
                                                    failed_task, ctx
                                                )
                                                failed_task.context = ctx
                                                failed_task.status = TaskStatus.QUEUED
                                                failed_task.vm_instance_id = (
                                                    f"agent-vm-{uuid.uuid4().hex}"
                                                )
                                                failed_task.last_heartbeat = None
                                                failed_task.started_at = None
                                                # Align retry with zone recovery; cap at 30 min.
                                                # Audit C10: zone_recovery_wait_seconds() does
                                                # the tz-AWARE subtraction internally (matches
                                                # the reaper's infra-requeue path). The previous
                                                # naive _utcnow() subtraction here raised
                                                # TypeError, rolling back this QUEUED status +
                                                # retry_after_until.
                                                recovery_wait = zone_recovery_wait_seconds()
                                                if recovery_wait is not None:
                                                    wait_sec = max(60, recovery_wait + 60)
                                                    delay_min = min(wait_sec / 60, 30)
                                                else:
                                                    delay_min = 10
                                                set_retry_after(failed_task, delay_min)
                                                ctx = dict(failed_task.context or {})
                                                failed_task.latest_status_message = f"Infra-wide provision failure (#{infra_count}), requeued — waiting for zone recovery: {provision_err or 'unknown'}"[
                                                    :500
                                                ]
                                                logger.warning(
                                                    f"[ORCHESTRATOR] Task {t_id} infra provision failure — requeuing without burning retry budget (delay {delay_min:.0f}min)"
                                                )
                                            else:
                                                # Task-specific failure — burn retry budget
                                                prov_failures = (
                                                    ctx.get("provision_failures", 0) + 1
                                                )
                                                ctx["provision_failures"] = (
                                                    prov_failures
                                                )
                                                ctx.pop("_infra_wait", None)
                                                max_prov_retries = _runtime_config.get(
                                                    "max_provision_retries", 3
                                                )
                                                if prov_failures >= max_prov_retries:
                                                    # Exhausted retries — permanent failure
                                                    failed_task.status = (
                                                        TaskStatus.FAILED
                                                    )
                                                    failed_task.latest_status_message = f"VM provisioning failed {prov_failures} times — giving up: {provision_err or 'unknown error'}"
                                                    ctx["failure_category"] = (
                                                        "PROVISION_FAILURE"
                                                    )
                                                    await _maybe_create_failure_alert(
                                                        failed_task,
                                                        inner_session,
                                                        (
                                                            provision_err
                                                            or "unknown error"
                                                        )[:200],
                                                    )
                                                    try:
                                                        record_task_result(
                                                            success=False,
                                                            failure_class="PROVISION_FAILURE",
                                                            task_id=str(failed_task.id),
                                                            task_type=(
                                                                failed_task.first_payload
                                                                or {}
                                                            ).get("type", ""),
                                                        )
                                                        _direction_id = str(
                                                            (
                                                                failed_task.context
                                                                or {}
                                                            ).get("direction_id")
                                                            or ""
                                                        )
                                                        if _direction_id:
                                                            from app.shared_state import (
                                                                record_direction_result as _rdr,
                                                            )

                                                            _rdr(
                                                                _direction_id,
                                                                success=False,
                                                                failure_class="PROVISION_FAILURE",
                                                                project_id=str(
                                                                    failed_task.project_id
                                                                )
                                                                if failed_task.project_id
                                                                else "",
                                                            )
                                                    except Exception:
                                                        logger.exception(
                                                            f"[ORCHESTRATOR] Failed to record provision-exhaustion result for task {t_id}"
                                                        )
                                                    logger.error(
                                                        f"[ORCHESTRATOR] Task {t_id} provision failed {prov_failures} times — permanent failure"
                                                    )
                                                else:
                                                    # Requeue for retry
                                                    bank_task_current_attempt(
                                                        failed_task,
                                                        stopped_at=_utcnow(),
                                                    )
                                                    ctx = with_task_attempt_banking(
                                                        failed_task, ctx
                                                    )
                                                    failed_task.context = ctx
                                                    failed_task.status = (
                                                        TaskStatus.QUEUED
                                                    )
                                                    failed_task.vm_instance_id = (
                                                        f"agent-vm-{uuid.uuid4().hex}"
                                                    )
                                                    failed_task.last_heartbeat = None
                                                    failed_task.started_at = None
                                                    # Back off 2 min per failure to let zones recover
                                                    set_retry_after(
                                                        failed_task, 2 * prov_failures
                                                    )
                                                    ctx = dict(
                                                        failed_task.context or {}
                                                    )
                                                    failed_task.latest_status_message = f"Provision failed (attempt {prov_failures}/{max_prov_retries}), requeued: {provision_err or 'unknown'}"
                                                    logger.warning(
                                                        f"[ORCHESTRATOR] Task {t_id} provision failed (attempt {prov_failures}) — requeuing"
                                                    )

                                            if failed_task.status == TaskStatus.QUEUED:
                                                ctx = clear_task_llm_runtime_context(
                                                    ctx
                                                )
                                            failed_task.context = ctx
                                            inner_session.add(failed_task)
                                            await inner_session.commit()
                                            await broadcast_to_project(
                                                str(p_id),
                                                "task_updated",
                                                {
                                                    "task_id": str(t_id),
                                                    "status": failed_task.status,
                                                },
                                            )
                                            # Record zone failure so the zone health system avoids this zone
                                            if prov_zone and prov_zone != "unknown":
                                                record_zone_failure(
                                                    prov_zone,
                                                    reason="PROVISION_INLINE_FAILURE",
                                                )
                                except Exception as e:
                                    logger.error(
                                        f"[ORCHESTRATOR] Error handling provision failure for task {t_id}: {e}",
                                        exc_info=True,
                                    )
                            finally:
                                # ALWAYS delete partially-created VM, even if requeue logic crashes
                                await _ensure_vm_deleted(
                                    vm_id, reason="provision_failure"
                                )

                    provision_coro = asyncio.create_task(
                        _provision_and_handle(
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
                        lambda _, tid=str(task.id): _provision_coroutines.pop(tid, None)
                    )

                    await broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {"task_id": str(task.id), "status": TaskStatus.PROVISIONING},
                    )
                    _cycle_dispatched += 1

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

                # Check tasks waiting on CI (review approved, CI pending)
                CI_WAIT_TIMEOUT_MINUTES = 20
                ci_waiting_task_ids = (
                    (
                        await session.execute(
                            select(Task.id)
                            .where(
                                Task.status == TaskStatus.EVALUATING,
                                # C3: only CI-waiting rows — see
                                # _ci_waiting_sql_filter. Drops the per-row
                                # lock-and-rollback cost on the (typically large)
                                # non-CI-waiting EVALUATING backlog.
                                _ci_waiting_sql_filter(session),
                            )
                            .limit(200)
                        )
                    )
                    .scalars()
                    .all()
                )
                for ci_task_id in ci_waiting_task_ids:
                    # Heartbeat per task: this loop is O(EVALUATING backlog) and
                    # is the dominant contributor to cycle wall-time. Marking
                    # progress each iteration keeps liveness fresh across an
                    # arbitrarily long poll pass (incident 2026-05-28).
                    _mark_orchestrator_progress()
                    ci_task = await _lock_task_for_mutation(
                        session,
                        ci_task_id,
                        allowed_statuses=(TaskStatus.EVALUATING,),
                        reason="CI polling",
                    )
                    if ci_task is None:
                        continue
                    ci_loop = dict((ci_task.context or {}).get("review_loop", {}) or {})
                    if not ci_loop.get("ci_waiting"):
                        await session.rollback()
                        continue
                    if ci_loop.get("conflict_fix_spawned"):
                        await session.rollback()
                        continue  # Already handled — fix task spawned for merge conflicts
                    if ci_loop.get("ci_fix_spawned"):
                        await session.rollback()
                        continue  # Already handled — fix task spawned for CI failure
                    if ci_loop.get("merge_rule_blocked"):
                        await session.rollback()
                        continue  # Merge blocked by branch protection — requires manual intervention
                    # C5: skip approval/safeguard-gated rows while the gate is set
                    # (and gates are enabled). handle_ci_completion ->
                    # _attempt_merge_with_retry_and_escalation now re-checks
                    # requires_approval and refuses to merge, so polling a gated
                    # row would just churn GitHub I/O every cycle for no progress.
                    # The merge resumes once the safeguard/operator clears the flag
                    # (which also re-arms ci_waiting). With gates OFF the flag is
                    # advisory only, so do not skip. This is the orchestrator-side
                    # half of the merge-call invariant (cluster C5 fix #2).
                    if ci_task.requires_approval and _runtime_config.get(
                        "require_approval_gates", True
                    ):
                        await session.rollback()
                        continue
                    ci_pr_number = ci_loop.get("pr_number")
                    if not ci_pr_number:
                        await session.rollback()
                        continue

                    # Ensure ci_wait_started is always set — some paths set ci_waiting=True
                    # without setting ci_wait_started, which silently disables the timeout.
                    # Commit immediately, then reacquire task_lock before more mutations below.
                    if not ci_loop.get("ci_wait_started"):
                        from app import pause_clock

                        ci_loop["ci_wait_started"] = _utcnow().isoformat()
                        ci_loop[
                            "ci_wait_pause_baseline"
                        ] = await pause_clock.current_baseline(session)
                        ci_task.context = {
                            **(ci_task.context or {}),
                            "review_loop": ci_loop,
                        }
                        # Without flag_modified, JSONB nested-dict mutations
                        # may not be detected by SQLAlchemy when the new outer
                        # dict shares the same review_loop reference as the
                        # old. Drives the 2026-04-24 "CI wait exceeded 20min"
                        # flap where escalated/ci_last_polled never persisted.
                        from sqlalchemy.orm.attributes import flag_modified

                        flag_modified(ci_task, "context")
                        session.add(ci_task)
                        await session.commit()
                        ci_task = await _lock_task_for_mutation(
                            session,
                            ci_task_id,
                            allowed_statuses=(TaskStatus.EVALUATING,),
                            reason="CI polling after wait-start persist",
                        )
                        if ci_task is None:
                            continue
                        ci_loop = dict(
                            (ci_task.context or {}).get("review_loop", {}) or {}
                        )

                    # Exponential backoff: skip if not enough time has elapsed since last poll.
                    # Webhooks handle the fast path — polling is a fallback.
                    ci_poll_interval = ci_loop.get("ci_poll_interval", 30)
                    ci_last_polled = ci_loop.get("ci_last_polled")
                    if ci_last_polled:
                        try:
                            _last_polled_dt = datetime.fromisoformat(ci_last_polled)
                            if (
                                _utcnow() - _last_polled_dt
                            ).total_seconds() < ci_poll_interval:
                                await session.rollback()
                                continue
                        except (ValueError, TypeError):
                            pass

                    try:
                        # Timeout: if CI has been pending too long, proceed to merge anyway
                        # Use ci_wait_started if available, otherwise fall back to started_at/created_at
                        ci_wait_started = ci_loop.get("ci_wait_started")
                        try:
                            started = (
                                datetime.fromisoformat(ci_wait_started)
                                if ci_wait_started
                                else None
                            )
                        except (ValueError, TypeError):
                            started = None
                        if started is None:
                            started = ci_task.started_at or ci_task.created_at
                        if (
                            started
                            and (_utcnow() - started).total_seconds()
                            > CI_WAIT_TIMEOUT_MINUTES * 60
                        ):
                            # Escalate: alert the user but keep retrying.
                            # The 30-min backstop in services.py is the true hard-kill.
                            if not ci_loop.get("escalated"):
                                ci_loop["escalated"] = True
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                # See note at the ci_wait_started persist above
                                # — without flag_modified the escalated flag
                                # silently fails to persist, the next cycle
                                # reads escalated=False, and this whole block
                                # re-fires every 2-4s, which is exactly the
                                # "CI wait exceeded 20min" flap pattern.
                                from sqlalchemy.orm.attributes import flag_modified

                                flag_modified(ci_task, "context")
                                ci_task.latest_status_message = f"CI wait exceeded {CI_WAIT_TIMEOUT_MINUTES}min on PR #{ci_pr_number} — retrying, alert sent"
                                session.add(ci_task)
                                await _maybe_create_failure_alert(
                                    ci_task,
                                    session,
                                    f"Rollup PR #{ci_pr_number} stuck in CI-wait for >{CI_WAIT_TIMEOUT_MINUTES}min — still retrying",
                                )
                                await session.commit()
                                await broadcast_to_project(
                                    str(ci_task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": str(ci_task.id),
                                        "status": ci_task.status,
                                    },
                                )
                                ci_task = await _lock_task_for_mutation(
                                    session,
                                    ci_task_id,
                                    allowed_statuses=(TaskStatus.EVALUATING,),
                                    reason="CI polling after escalation persist",
                                )
                                if ci_task is None:
                                    continue
                                ci_loop = dict(
                                    (ci_task.context or {}).get("review_loop", {}) or {}
                                )
                            # Fall through — continue polling CI even after escalation

                        (
                            ci_gh_token,
                            ci_owner,
                            ci_repo,
                        ) = await get_github_token_for_task(ci_task.id, session)

                        # Use cached head SHA when available to skip the PR fetch (saves 1 API call per poll).
                        # Re-fetch PR every 5th poll to detect force-pushes and merge state changes.
                        cached_head_sha = ci_loop.get("head_sha", "")
                        polls_since_fetch = (
                            ci_loop.get("polls_since_pr_fetch", 5) + 1
                        )  # default 5 to force first fetch
                        need_pr_fetch = not cached_head_sha or polls_since_fetch >= 5
                        pr_data = None

                        if not need_pr_fetch:
                            # Fast path: skip PR fetch, use cached SHA, just check CI
                            pr_data = {
                                "state": "open",
                                "head": {"sha": cached_head_sha},
                                "mergeable": True,
                            }
                            ci_loop["polls_since_pr_fetch"] = polls_since_fetch

                        if need_pr_fetch:
                            ci_loop["polls_since_pr_fetch"] = 0
                            # ── Phase B3: release lock around get_pull_request ──
                            # GitHub rate-limit can hold this call for minutes;
                            # holding the lock starves concurrent heartbeats on
                            # the same task. Release, fetch, re-acquire-validate.
                            # R5-W2-A: content snapshot, not updated_at —
                            # heartbeat onupdate would false-defer every poll.
                            ci_pre_pr_snapshot = _capture_ci_polling_snapshot(ci_task)
                            await session.commit()
                            try:
                                pr_data = await get_pull_request(
                                    ci_gh_token, ci_owner, ci_repo, ci_pr_number
                                )
                            except HTTPException as exc:
                                # Re-acquire to apply error-path mutations
                                ci_task = await _lock_task_for_mutation(
                                    session,
                                    ci_task_id,
                                    allowed_statuses=(TaskStatus.EVALUATING,),
                                    reason="CI polling after PR fetch error",
                                    expected_snapshot=ci_pre_pr_snapshot,
                                    snapshot_fn=_capture_ci_polling_snapshot,
                                )
                                if ci_task is None:
                                    continue
                                ci_loop = dict(
                                    (ci_task.context or {}).get("review_loop", {}) or {}
                                )
                                if exc.status_code in (404, 410):
                                    _pr_gone_msg = f"PR #{ci_pr_number} no longer exists (deleted or repo archived)"
                                    logger.exception(
                                        f"[CI] {_pr_gone_msg} for task {ci_task.id}"
                                    )
                                    ci_task.status = TaskStatus.FAILED
                                    if ci_task.vm_provisioned_at:
                                        ci_task.vm_stopped_at = _utcnow()
                                    ci_task.latest_status_message = _pr_gone_msg
                                    from app.failure_classification import (
                                        persist_failure_category,
                                    )
                                    from app.routers.agent_status import (
                                        record_task_result,
                                    )

                                    persist_failure_category(ci_task, "EXTERNAL_API")
                                    record_task_result(
                                        success=False,
                                        failure_class="EXTERNAL_API",
                                        task_id=str(ci_task.id),
                                        task_type=(ci_task.first_payload).get(
                                            "type", ""
                                        ),
                                    )
                                    session.add(ci_task)
                                    await _maybe_create_failure_alert(
                                        ci_task, session, _pr_gone_msg
                                    )
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(ci_task.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(ci_task.id),
                                            "status": ci_task.status,
                                        },
                                    )
                                    continue
                                # Transient GitHub API error — backoff and retry
                                _fetch_failures = (
                                    ci_loop.get("pr_fetch_failures", 0) + 1
                                )
                                ci_loop["pr_fetch_failures"] = _fetch_failures
                                ci_loop["ci_last_polled"] = _utcnow().isoformat()
                                ci_loop["ci_poll_interval"] = min(
                                    (ci_loop.get("ci_poll_interval", 30)) * 2, 300
                                )
                                if _fetch_failures >= 10 and not ci_loop.get(
                                    "fetch_escalated"
                                ):
                                    ci_loop["fetch_escalated"] = True
                                    await _maybe_create_failure_alert(
                                        ci_task,
                                        session,
                                        f"PR #{ci_pr_number} GitHub API failures ({_fetch_failures}x) — still retrying",
                                    )
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                from sqlalchemy.orm.attributes import flag_modified

                                flag_modified(ci_task, "context")
                                logger.warning(
                                    f"[CI] Transient error fetching PR #{ci_pr_number} for task {ci_task.id} "
                                    f"(attempt {_fetch_failures}): {exc.detail}"
                                )
                                session.add(ci_task)
                                await session.commit()
                                continue

                            if not pr_data:
                                await session.rollback()
                                continue

                            # Re-acquire lock after successful PR fetch.
                            ci_task = await _lock_task_for_mutation(
                                session,
                                ci_task_id,
                                allowed_statuses=(TaskStatus.EVALUATING,),
                                reason="CI polling after PR fetch",
                                expected_snapshot=ci_pre_pr_snapshot,
                                snapshot_fn=_capture_ci_polling_snapshot,
                            )
                            if ci_task is None:
                                continue
                            ci_loop = dict(
                                (ci_task.context or {}).get("review_loop", {}) or {}
                            )

                            if ci_loop.get("pr_fetch_failures"):
                                ci_loop["pr_fetch_failures"] = 0

                        pr_state = pr_data.get("state", "open")

                        # Check if PR was already merged (prevents double merge).
                        # If we self-closed for a mergeability reset and reopen failed,
                        # retry the reopen instead of marking COMPLETED — otherwise a
                        # transient reopen failure would silently discard the work.
                        if pr_state == "closed" or pr_data.get("merged"):
                            if (
                                pr_state == "closed"
                                and not pr_data.get("merged")
                                and ci_loop.get("pending_reopen")
                            ):
                                from sqlalchemy.orm.attributes import flag_modified

                                _reopen_attempts = ci_loop.get("reopen_attempts", 0) + 1
                                ci_loop["reopen_attempts"] = _reopen_attempts
                                _reopen_ok = False
                                try:
                                    _reopen_ok = bool(
                                        await reopen_pull_request(
                                            ci_gh_token,
                                            ci_owner,
                                            ci_repo,
                                            ci_pr_number,
                                            project_id=str(ci_task.project_id),
                                        )
                                    )
                                except Exception as _reopen_exc:
                                    logger.warning(
                                        f"[CI] retry reopen of PR #{ci_pr_number} raised: {_reopen_exc}"
                                    )
                                if _reopen_ok:
                                    ci_loop["pending_reopen"] = False
                                    ci_loop["reopen_attempts"] = 0
                                    ci_loop.pop("reopen_alert_fired", None)
                                    ci_loop["polls_since_pr_fetch"] = 999
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} reopened after prior reset failure "
                                        f"(after {_reopen_attempts} attempt{'s' if _reopen_attempts != 1 else ''})"
                                    )
                                    ci_task.context = {
                                        **(ci_task.context or {}),
                                        "review_loop": ci_loop,
                                    }
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    continue
                                _reopen_alert_threshold = _runtime_config.get(
                                    "ci_reopen_alert_threshold", 5
                                )
                                _reopen_approval_cap = _runtime_config.get(
                                    "ci_reopen_approval_cap", 10
                                )
                                ci_task.latest_status_message = (
                                    f"PR #{ci_pr_number} reopen failed (attempt {_reopen_attempts}) — "
                                    f"will retry on next poll"
                                )
                                if (
                                    _reopen_attempts >= _reopen_alert_threshold
                                    and not ci_loop.get("reopen_alert_fired")
                                ):
                                    ci_loop["reopen_alert_fired"] = True
                                    await _maybe_create_failure_alert(
                                        ci_task,
                                        session,
                                        f"PR #{ci_pr_number} reopen failed {_reopen_attempts} times — "
                                        f"GitHub may be rejecting the reopen, still retrying",
                                    )
                                if (
                                    _reopen_attempts >= _reopen_approval_cap
                                    and not ci_task.requires_approval
                                ):
                                    ci_task.status = TaskStatus.FAILED
                                    if ci_task.vm_provisioned_at:
                                        ci_task.vm_stopped_at = _utcnow()
                                    # Gate only the approval flag — FAILED is the
                                    # terminalization and stays. Gates off → task FAILs
                                    # without approval-gating (no 2h timeout strand).
                                    if _runtime_config.get(
                                        "require_approval_gates", True
                                    ):
                                        ci_task.requires_approval = True
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} reopen failed {_reopen_attempts} times — "
                                        f"manual intervention required"
                                    )
                                    from app.failure_classification import (
                                        persist_failure_category as _pfc_reopen,
                                    )

                                    _pfc_reopen(ci_task, "EXTERNAL_API")
                                    record_task_result(
                                        success=False,
                                        failure_class="EXTERNAL_API",
                                        task_id=str(ci_task.id),
                                        task_type=(ci_task.first_payload).get(
                                            "type", ""
                                        ),
                                    )
                                    await _maybe_create_failure_alert(
                                        ci_task,
                                        session,
                                        f"PR #{ci_pr_number} reopen cap exhausted ({_reopen_attempts} attempts) — "
                                        f"task FAILED, manual intervention required",
                                    )
                                    logger.warning(
                                        f"[CI] PR #{ci_pr_number} reopen attempts ({_reopen_attempts}) "
                                        f"hit cap — marking task {ci_task.id} FAILED (requires_approval)"
                                    )
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                flag_modified(ci_task, "context")
                                session.add(ci_task)
                                await session.commit()
                                if ci_task.status == TaskStatus.FAILED:
                                    await broadcast_to_project(
                                        str(ci_task.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(ci_task.id),
                                            "status": ci_task.status,
                                        },
                                    )
                                continue
                            if pr_data.get("merged"):
                                _orch_ci_project = (
                                    await session.get(Project, ci_task.project_id)
                                    if ci_task.project_id
                                    else None
                                )
                                _ci_base = (
                                    (pr_data.get("base") or {}).get("ref", "")
                                    or ci_loop.get("base_branch", "")
                                    or (ci_task.first_payload or {}).get(
                                        "base_branch", ""
                                    )
                                    or _shared_state.get_project_integration_branch(
                                        _orch_ci_project
                                    )
                                )
                                _landed_on_integration = (
                                    await _apply_ci_merged_landed_signal(
                                        ci_task,
                                        session,
                                        base_branch=_ci_base,
                                        review_loop=ci_loop,
                                    )
                                )
                                ci_task.status = TaskStatus.COMPLETED
                                ci_task.latest_status_message = (
                                    f"PR #{ci_pr_number} merged"
                                )
                                session.add(ci_task)
                                await session.commit()
                                await broadcast_to_project(
                                    str(ci_task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": str(ci_task.id),
                                        "status": ci_task.status,
                                    },
                                )
                                logger.info(
                                    f"[CI] PR #{ci_pr_number} merged, marking task COMPLETED"
                                )
                                # Completion-side warm-reuse trigger: now that this
                                # PR has landed on integration, proactively mark any
                                # dependents it just unblocked as warm-claimable so
                                # this task's warm VM can claim follow-on work inside
                                # its teardown-defer grace window instead of tearing
                                # down cold. Best-effort: a failure here must never
                                # abort the CI completion cycle.
                                if _landed_on_integration:
                                    try:
                                        _unblocked = await _mark_unblocked_dependents(
                                            ci_task, _verify_pr_merged
                                        )
                                        if _unblocked:
                                            logger.info(
                                                f"[CI] PR #{ci_pr_number} unblocked "
                                                f"{_unblocked} dependent(s) — marked warm-claimable"
                                            )
                                    except Exception:
                                        logger.exception(
                                            "[CI] dependent warm-claim marking failed "
                                            f"for task {ci_task.id}"
                                        )
                            else:
                                # Closed without merge (human/bot intervention, not a self-close
                                # reset — pending_reopen case handled above). Do NOT mark
                                # COMPLETED — the code never landed. Fail the task so rollup
                                # parents don't silently report success.
                                _closed_msg = f"PR #{ci_pr_number} was closed without being merged"
                                ci_task.status = TaskStatus.FAILED
                                if ci_task.vm_provisioned_at:
                                    ci_task.vm_stopped_at = _utcnow()
                                ci_task.latest_status_message = _closed_msg
                                from app.failure_classification import (
                                    persist_failure_category as _pfc_closed,
                                )

                                _pfc_closed(ci_task, "EXTERNAL_API")
                                record_task_result(
                                    success=False,
                                    failure_class="EXTERNAL_API",
                                    task_id=str(ci_task.id),
                                    task_type=(ci_task.first_payload).get("type", ""),
                                )
                                session.add(ci_task)
                                await _maybe_create_failure_alert(
                                    ci_task, session, _closed_msg
                                )
                                await session.commit()
                                await broadcast_to_project(
                                    str(ci_task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": str(ci_task.id),
                                        "status": ci_task.status,
                                    },
                                )
                                logger.warning(
                                    f"[CI] {_closed_msg} — marking task {ci_task.id} FAILED"
                                )
                            continue

                        # Check for merge conflicts — CI won't trigger on conflicting PRs.
                        # GitHub returns None while computing mergeability; usually transient
                        # but can get stuck after force-pushes or branch-protection changes.
                        # Track consecutive nulls and force a recompute via close+reopen
                        # before the 30-min CI backstop kills the task and discards all work.
                        if pr_data.get("mergeable") is None:
                            _null_count = ci_loop.get("mergeable_null_count", 0) + 1
                            ci_loop["mergeable_null_count"] = _null_count
                            _null_first_ts = (
                                ci_loop.get("mergeable_null_first_ts")
                                or _utcnow().isoformat()
                            )
                            ci_loop["mergeable_null_first_ts"] = _null_first_ts
                            try:
                                _null_elapsed = (
                                    _utcnow() - datetime.fromisoformat(_null_first_ts)
                                ).total_seconds()
                            except (ValueError, TypeError):
                                _null_elapsed = 0.0
                            if _null_count >= 5 and _null_elapsed >= 180:
                                _reopen_cap = 3
                                _reopen_tries = ci_loop.get(
                                    "mergeable_null_reopen_attempts", 0
                                )
                                if _reopen_tries >= _reopen_cap:
                                    if not ci_task.requires_approval:
                                        # Discretionary gate: keep the status message +
                                        # alert (visibility) but only approval-gate when
                                        # require_approval_gates is on, so a non-terminal
                                        # ci_task isn't stranded on the 2h timeout.
                                        if _runtime_config.get(
                                            "require_approval_gates", True
                                        ):
                                            ci_task.requires_approval = True
                                        ci_task.latest_status_message = (
                                            f"PR #{ci_pr_number} mergeability stuck after {_reopen_tries} "
                                            f"close/reopen attempts — manual intervention required"
                                        )
                                        await _maybe_create_failure_alert(
                                            ci_task,
                                            session,
                                            f"PR #{ci_pr_number} mergeability stuck after {_reopen_tries} "
                                            f"close/reopen attempts — escalating for manual intervention",
                                        )
                                    ci_loop["mergeable_null_count"] = 0
                                    ci_loop["mergeable_null_first_ts"] = None
                                    ci_task.context = {
                                        **(ci_task.context or {}),
                                        "review_loop": ci_loop,
                                    }
                                    session.add(ci_task)
                                    await session.commit()
                                    continue
                                logger.warning(
                                    f"[CI] PR #{ci_pr_number} mergeable=None for {_null_count} polls over "
                                    f"{_null_elapsed:.0f}s — closing/reopening to force GitHub to recompute "
                                    f"(attempt {_reopen_tries + 1}/{_reopen_cap})"
                                )
                                _reset_ok = False
                                _did_close = False
                                try:
                                    if await close_pull_request(
                                        ci_gh_token,
                                        ci_owner,
                                        ci_repo,
                                        ci_pr_number,
                                        project_id=str(ci_task.project_id),
                                    ):
                                        _did_close = True
                                        _reset_ok = await reopen_pull_request(
                                            ci_gh_token,
                                            ci_owner,
                                            ci_repo,
                                            ci_pr_number,
                                            project_id=str(ci_task.project_id),
                                        )
                                except Exception as _reset_exc:
                                    logger.warning(
                                        f"[CI] close/reopen of PR #{ci_pr_number} raised: {_reset_exc}"
                                    )
                                ci_loop["mergeable_null_count"] = 0
                                ci_loop["mergeable_null_first_ts"] = None
                                ci_loop["mergeable_null_reopen_attempts"] = (
                                    _reopen_tries + 1
                                )
                                if _reset_ok:
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} mergeability stuck — closed/reopened to force recompute"
                                    ci_loop["polls_since_pr_fetch"] = 999
                                    ci_loop["pending_reopen"] = False
                                else:
                                    if _did_close:
                                        ci_loop["pending_reopen"] = True
                                        ci_loop["polls_since_pr_fetch"] = 999
                                    await _maybe_create_failure_alert(
                                        ci_task,
                                        session,
                                        f"PR #{ci_pr_number} mergeability stuck ({_null_count} null polls) — close/reopen failed, still polling",
                                    )
                            ci_task.context = {
                                **(ci_task.context or {}),
                                "review_loop": ci_loop,
                            }
                            session.add(ci_task)
                            await session.commit()
                            continue
                        if ci_loop.get("mergeable_null_count"):
                            ci_loop["mergeable_null_count"] = 0
                            ci_loop["mergeable_null_first_ts"] = None
                        if pr_data.get("mergeable") is False:
                            # GitHub's mergeable_state has several values that don't
                            # indicate a real merge conflict — only 'dirty' (and
                            # 'behind' as a fall-through fallback) should burn a VM
                            # fix cycle. Other states are recoverable by waiting:
                            # 'blocked' clears when required checks/reviews land,
                            # 'unstable' means CI is failing but the PR is mergeable,
                            # 'unknown' means GitHub is still computing.
                            _ms = (pr_data.get("mergeable_state") or "").lower()
                            _behind_fallthrough = False
                            if _ms == "behind":
                                # ── Phase B3: release lock around update_branch_with_sha_retry ──
                                # The retry helper internally retries on 422; cumulative
                                # wait can be 30s+. Drop the lock so concurrent heartbeats
                                # on this same task (rollup parents share a row) don't queue.
                                # R5-W2-A: content snapshot, not updated_at —
                                # heartbeat onupdate would false-defer every poll.
                                ci_pre_update_branch_snapshot = (
                                    _capture_ci_polling_snapshot(ci_task)
                                )
                                ci_pid_str = str(ci_task.project_id)
                                _expected_head_sha = (
                                    pr_data.get("head", {}).get("sha") or None
                                )
                                # Short-circuit when GitHub's 'behind' state is stale —
                                # if the base tip is already an ancestor of the PR head,
                                # update_branch_with_sha_retry would just churn CI for
                                # no real change. Mirrors ci_completion.py's pattern.
                                _orch_ci_project_pre = (
                                    await session.get(Project, ci_task.project_id)
                                    if ci_task.project_id
                                    else None
                                )
                                _ci_payload_pre = ci_task.first_payload
                                _ci_base_pre = (
                                    ci_loop.get("base_branch", "")
                                    or _ci_payload_pre.get("base_branch", "")
                                    or _shared_state.get_project_integration_branch(
                                        _orch_ci_project_pre
                                    )
                                )
                                _not_behind_pre = await pr_is_not_behind_for(
                                    ci_gh_token,
                                    ci_owner,
                                    ci_repo,
                                    pr_detail=pr_data,
                                    base_branch=_ci_base_pre,
                                    project_id=ci_pid_str,
                                    call_site="orchestrator_ci",
                                )
                                if _not_behind_pre is True:
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} mergeable_state='behind' but "
                                        f"pr_is_not_behind=True — skipping update_branch_with_sha_retry"
                                    )
                                    await session.rollback()
                                    continue
                                await session.commit()
                                # Trivially resolvable via GitHub's update-branch API.
                                # Update the branch in-place; the merge-commit
                                # triggers fresh CI and the next poll re-evaluates.
                                _upd = await update_branch_with_sha_retry(
                                    ci_gh_token,
                                    ci_owner,
                                    ci_repo,
                                    ci_pr_number,
                                    expected_head_sha=_expected_head_sha,
                                    project_id=ci_pid_str,
                                )
                                # Re-acquire to apply downstream mutations.
                                ci_task = await _lock_task_for_mutation(
                                    session,
                                    ci_task_id,
                                    allowed_statuses=(TaskStatus.EVALUATING,),
                                    reason="CI polling after update_branch",
                                    expected_snapshot=ci_pre_update_branch_snapshot,
                                    snapshot_fn=_capture_ci_polling_snapshot,
                                )
                                if ci_task is None:
                                    continue
                                ci_loop = dict(
                                    (ci_task.context or {}).get("review_loop", {}) or {}
                                )
                                if _upd.get("updated"):
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} was behind base — updated branch via GitHub API, awaiting fresh CI"
                                    # Force a refetch next cycle so mergeable/head_sha refresh.
                                    ci_loop["polls_since_pr_fetch"] = 999
                                    ci_loop["head_sha"] = ""
                                    # Reset behind-retry counter — any prior transient failures resolved.
                                    ci_loop.pop("_behind_update_retries", None)
                                    from sqlalchemy.orm.attributes import flag_modified

                                    ci_task.context = {
                                        **(ci_task.context or {}),
                                        "review_loop": ci_loop,
                                    }
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(ci_task.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(ci_task.id),
                                            "status": ci_task.status,
                                        },
                                    )
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} behind base — update_branch succeeded, skipped fix spawn"
                                    )
                                    continue
                                if _upd.get("not_behind"):
                                    # GitHub's cached mergeable_state was stale — the
                                    # PR is not actually behind (no new commits on
                                    # base). Treat exactly like the pr_is_not_behind
                                    # short-circuit above: clear any prior retry
                                    # counter and defer to the next sweep without
                                    # escalating to fix-spawn.
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} not behind base "
                                        f"(no new commits on base) — cached "
                                        f"mergeable_state was stale, deferring to next CI sweep"
                                    )
                                    ci_loop["polls_since_pr_fetch"] = 999
                                    ci_loop["head_sha"] = ""
                                    ci_loop.pop("_behind_update_retries", None)
                                    from sqlalchemy.orm.attributes import flag_modified

                                    ci_task.context = {
                                        **(ci_task.context or {}),
                                        "review_loop": ci_loop,
                                    }
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} behind base — "
                                        f"update_branch reports no new commits on base, deferring to next sweep"
                                    )
                                    continue
                                # update_branch failed — likely a transient GitHub API error,
                                # not a real merge conflict. Defer to the next CI sweep with a
                                # bounded retry budget instead of immediately burning a fix
                                # iteration. Mirrors the rollup_management behind-update pattern.
                                _behind_retries = (
                                    ci_loop.get("_behind_update_retries", 0) + 1
                                )
                                _behind_budget = _runtime_config.get(
                                    "ci_behind_update_retries", 3
                                )
                                _upd_msg = _upd.get("message") or "unknown"
                                if _behind_retries < _behind_budget:
                                    ci_loop["_behind_update_retries"] = _behind_retries
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} behind base — update_branch failed "
                                        f"({_upd_msg}); retrying next sweep ({_behind_retries}/{_behind_budget})"
                                    )
                                    from sqlalchemy.orm.attributes import flag_modified

                                    ci_task.context = {
                                        **(ci_task.context or {}),
                                        "review_loop": ci_loop,
                                    }
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} behind base — update_branch failed "
                                        f"(attempt {_behind_retries}/{_behind_budget}): {_upd_msg} — deferring to next sweep"
                                    )
                                    continue
                                # Budget exhausted — alert, clear the counter, and fall through
                                # to fix spawn so a real engineer-style fix can resolve whatever
                                # GitHub can't auto-update.
                                await _maybe_create_failure_alert(
                                    ci_task,
                                    session,
                                    f"PR #{ci_pr_number} behind base — update_branch failed "
                                    f"{_behind_retries} consecutive sweeps ({_upd_msg}); falling through to fix spawn",
                                )
                                ci_loop.pop("_behind_update_retries", None)
                                _behind_fallthrough = True
                                logger.warning(
                                    f"[CI] PR #{ci_pr_number} behind base — update_branch failed "
                                    f"{_behind_retries} times ({_upd_msg}); falling through to fix spawn"
                                )
                            elif _ms != "dirty":
                                # Non-dirty non-behind mergeable_state values (blocked,
                                # unstable, unknown, empty, or anything unrecognized) are
                                # all recoverable by waiting rather than spawning a fix VM.
                                # Track consecutive same-state defers so silent stalls
                                # surface an alert before the 30-min CI backstop kills the
                                # task with no diagnostic breadcrumbs.
                                _ms_key = _ms or "empty"
                                _is_unrecognized = _ms_key not in (
                                    "blocked",
                                    "unstable",
                                    "unknown",
                                )
                                prev_state = ci_loop.get("_conflict_deferred_state")
                                deferred_count = (
                                    ci_loop.get("_conflict_deferred_count", 0) + 1
                                    if prev_state == _ms_key
                                    else 1
                                )
                                ci_loop["_conflict_deferred_count"] = deferred_count
                                ci_loop["_conflict_deferred_state"] = _ms_key
                                alert_threshold = (
                                    10 if _ms_key in ("blocked", "unstable") else 5
                                )
                                ci_task.latest_status_message = (
                                    f"PR #{ci_pr_number} merge deferred "
                                    f"(mergeable_state={_ms_key}, deferral {deferred_count}) — CI polling continues"
                                )
                                if deferred_count == alert_threshold:
                                    await _maybe_create_failure_alert(
                                        ci_task,
                                        session,
                                        f"PR #{ci_pr_number} stuck in mergeable_state={_ms_key} "
                                        f"for {deferred_count} consecutive CI polls",
                                    )
                                from sqlalchemy.orm.attributes import flag_modified

                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                flag_modified(ci_task, "context")
                                session.add(ci_task)
                                await session.commit()
                                _log = (
                                    logger.warning if _is_unrecognized else logger.debug
                                )
                                _log(
                                    f"[CI] PR #{ci_pr_number} mergeable=False, mergeable_state={_ms_key!r} "
                                    f"(deferral {deferred_count}/alert@{alert_threshold}) — continuing CI polling, not spawning fix"
                                )
                                continue
                            ci_payload = ci_task.first_payload
                            # Resolve the fix-spawn head branch with a live-PR
                            # fallback so a missing/stale stored branch can't
                            # produce an empty FixPayload.branch (incident
                            # 2026-05-28). See _resolve_ci_fix_branch.
                            ci_branch = _resolve_ci_fix_branch(
                                ci_loop, ci_payload, pr_data
                            )
                            _orch_ci_project = (
                                await session.get(Project, ci_task.project_id)
                                if ci_task.project_id
                                else None
                            )
                            ci_base = (
                                ci_loop.get("base_branch", "")
                                or ci_payload.get("base_branch", "")
                                or _shared_state.get_project_integration_branch(
                                    _orch_ci_project
                                )
                            )
                            ci_orig_id = ci_loop.get(
                                "original_task_id", ""
                            ) or ci_payload.get("original_task_id", "")
                            # Peek at CI state for this PR's head SHA so the fix task's prompt
                            # can describe BOTH blockers (conflicts + CI) if they coexist.
                            # The mergeable state can flip between now and when the fix agent
                            # runs — so we also tell the agent to re-verify before acting.
                            ci_preview_section = ""
                            try:
                                _conflict_head_sha = pr_data.get("head", {}).get(
                                    "sha", ""
                                )
                                if _conflict_head_sha:
                                    _conflict_ci_result = await get_check_runs_cached(
                                        ci_gh_token,
                                        ci_owner,
                                        ci_repo,
                                        _conflict_head_sha,
                                    )
                                    if _conflict_ci_result.get("status") == "completed":
                                        _conflict_failed_logs = _conflict_ci_result.get(
                                            "failed_logs", ""
                                        )
                                        if _conflict_failed_logs:
                                            ci_preview_section = (
                                                f"\n\nAdditionally, CI checks are currently failing on this PR head "
                                                f"(the conflict is blocking a re-run, but these failures pre-date the conflict "
                                                f"or exist independently):\n```\n{_conflict_failed_logs[:1500]}\n```\n"
                                                f"After resolving conflicts, fix the CI failures above in the same push."
                                            )
                            except Exception as _ci_preview_exc:
                                logger.debug(
                                    f"[CI] conflict-fix CI preview fetch failed for PR #{ci_pr_number}: {_ci_preview_exc}"
                                )
                            # Pre-spawn ancestor recheck: the PR head may have advanced
                            # (e.g. a sibling rollup) while update_branch retries were
                            # running. If it's now not-behind, skip the fix spawn entirely
                            # and clear the retry counter so the next sweep re-evaluates.
                            if _behind_fallthrough:
                                _late_not_behind = await pr_is_not_behind_for(
                                    ci_gh_token,
                                    ci_owner,
                                    ci_repo,
                                    pr_detail=pr_data,
                                    base_branch=ci_base,
                                    project_id=ci_pid_str,
                                    call_site="orchestrator_ci",
                                )
                                if _late_not_behind is True:
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} pr_is_not_behind=True at spawn time "
                                        f"after update_branch retries exhausted — skipping fix spawn"
                                    )
                                    ci_loop.pop("_behind_update_retries", None)
                                    from sqlalchemy.orm.attributes import flag_modified

                                    ci_task.context = {
                                        **(ci_task.context or {}),
                                        "review_loop": ci_loop,
                                    }
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    continue
                            if _behind_fallthrough:
                                # The PR is merely BEHIND base, not conflicting — but the
                                # GitHub update-branch API failed across the retry budget,
                                # so a manual rebase is needed. Describe the actual state
                                # (behind, not conflicting) so the agent does not chase
                                # nonexistent conflict markers.
                                fix_prompt = (
                                    f"PR #{ci_pr_number} is currently blocking the pipeline.\n\n"
                                    f"Orchestrator observation at spawn time: GitHub reported the PR as BEHIND `{ci_base}` "
                                    f"(no conflicts — just out-of-date). The orchestrator attempted to update the branch "
                                    f"via the GitHub update-branch API across the retry budget but every attempt failed; "
                                    f"a manual rebase is needed to unblock the merge.\n\n"
                                    f"IMPORTANT — verify current state before acting. The `mergeable` flag can flip "
                                    f"between spawn and execution. After cloning:\n"
                                    f"  1. Run `git fetch origin && git rebase origin/{ci_base}`.\n"
                                    f"  2. If no conflicts and no commits were replayed (git says 'Current branch "
                                    f"is up to date'), the PR is already up-to-date — GitHub's mergeability flag "
                                    f"is just stale. Report no changes needed (don't force-push); retries will "
                                    f"clear it.\n"
                                    f"  3. If the rebase surfaces unexpected conflicts (it shouldn't, given the "
                                    f"behind state), check `git status` and `git diff --name-only --diff-filter=U` "
                                    f"and resolve them.\n"
                                    f"  4. If the branch state changed while you were working, inspect the latest "
                                    f"failing checks via the orchestrator's read-only proxy "
                                    f'(`curl -sS -H "X-Agent-Token: $AGENT_API_TOKEN" '
                                    f'"$ORCHESTRATOR_URL/api/agent/github/check-runs?commit_sha=$(git rev-parse HEAD)" '
                                    f"| jq '.data.details[]'`) so you only act on current CI failures.\n\n"
                                    f"Primary action: rebase onto `{ci_base}` and force-push so the PR is no longer behind."
                                    f"{ci_preview_section}\n\n"
                                    f"Before declaring done: ensure (a) no conflict markers remain (if any surfaced), "
                                    f"(b) local lint/type/test suites pass on changed files, and (c) you've committed "
                                    f"every fix — do not leave uncommitted changes."
                                )
                            else:
                                fix_prompt = (
                                    f"PR #{ci_pr_number} is currently blocking the pipeline.\n\n"
                                    f"Orchestrator observation at spawn time: GitHub reported the PR as UNMERGEABLE "
                                    f"(merge conflicts against `{ci_base}`).\n\n"
                                    f"IMPORTANT — verify current state before acting. The `mergeable` flag can flip "
                                    f"between spawn and execution (a concurrent rollup may have resolved the conflict, "
                                    f"or new commits may have reintroduced one). After cloning:\n"
                                    f"  1. Attempt `git rebase origin/{ci_base}` — note whether it succeeds cleanly.\n"
                                    f"  2. Check `git status` and `git diff --name-only --diff-filter=U` for unmerged paths.\n"
                                    f"  3. If neither shows conflicts, the premise is stale — inspect the latest "
                                    f"failing checks via the orchestrator's read-only proxy "
                                    f'(`curl -sS -H "X-Agent-Token: $AGENT_API_TOKEN" '
                                    f'"$ORCHESTRATOR_URL/api/agent/github/check-runs?commit_sha=$(git rev-parse HEAD)" '
                                    f"| jq '.data.details[]'`) and fix whatever is actually blocking the merge now.\n\n"
                                    f"Primary action: rebase onto `{ci_base}` and resolve any conflicts."
                                    f"{ci_preview_section}\n\n"
                                    f"Before declaring done: ensure (a) no conflict markers remain in any file, "
                                    f"(b) local lint/type/test suites pass on changed files, and (c) you've committed "
                                    f"every fix — do not leave uncommitted changes."
                                )
                            if not ci_branch:
                                # Could not resolve a head branch from review_loop,
                                # payload, or the live PR. Skip the spawn (it would
                                # raise a FixPayload ValueError) and re-evaluate next
                                # cycle rather than wedge on a poison task.
                                logger.error(
                                    f"[CI] PR #{ci_pr_number}: cannot resolve head branch "
                                    f"for conflict-fix spawn (task {ci_task_id}) — skipping this cycle"
                                )
                                await session.rollback()
                                continue
                            fix_task, fix_was_created = await _spawn_pipeline_task(
                                ci_task,
                                "fix",
                                session,
                                ci_pr_number,
                                ci_branch,
                                ci_base,
                                fix_prompt=fix_prompt,
                                original_task_id=ci_orig_id,
                            )
                            ci_task = await _lock_task_for_mutation(
                                session,
                                ci_task_id,
                                allowed_statuses=(TaskStatus.EVALUATING,),
                                reason="CI conflict result",
                            )
                            if ci_task is None:
                                continue
                            ci_loop = dict(
                                (ci_task.context or {}).get("review_loop", {}) or {}
                            )
                            if fix_task:
                                from app.rollup_state import (
                                    _is_rollup_parent_awaiting_merge,
                                )

                                ci_loop_ctx = dict(ci_loop)
                                ci_loop_ctx["ci_waiting"] = False
                                ci_loop_ctx["conflict_fix_spawned"] = str(fix_task.id)
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop_ctx,
                                }
                                # Rollup parents must stay in EVALUATING until the rollup PR
                                # actually merges — ci_completion will promote them via
                                # pr_merged=True. Marking COMPLETED here produces a ghost
                                # success while the fix task is still resolving conflicts.
                                if not _is_rollup_parent_awaiting_merge(
                                    ci_task.context
                                ):
                                    ci_task.status = TaskStatus.COMPLETED
                                if fix_was_created:
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} has merge conflicts, spawned fix → {fix_task.id}"
                                    )
                                else:
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts, existing fix continues → {fix_task.id}"
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} has merge conflicts, existing fix continues → {fix_task.id}"
                                    )
                            else:
                                # Fix not spawned — either iteration cap exhausted, or a
                                # different-type task (e.g. a review) is still running on
                                # the same chain and must finish first. In both cases the
                                # CI-wait sweep on the next tick will re-evaluate; only
                                # escalate to FAILED + requires_approval when we actually
                                # know the cap is exhausted.
                                msg = (ci_task.latest_status_message or "").lower()
                                deferred = "deferred" in msg
                                if deferred:
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts — waiting on in-flight pipeline task"
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} conflict-fix deferred — a different-type pipeline task is still running, will retry on next sweep"
                                    )
                                else:
                                    ci_task.status = TaskStatus.FAILED
                                    if ci_task.vm_provisioned_at:
                                        ci_task.vm_stopped_at = _utcnow()
                                    # Gate only the approval flag — FAILED is the
                                    # terminalization and stays. Gates off → no strand.
                                    if _runtime_config.get(
                                        "require_approval_gates", True
                                    ):
                                        ci_task.requires_approval = True
                                    ci_task.latest_status_message = f"PR #{ci_pr_number} has merge conflicts — fix iterations exhausted, requires manual intervention"
                                    logger.warning(
                                        f"[CI] PR #{ci_pr_number} merge conflicts, fix not spawned — marking FAILED (requires_approval)"
                                    )
                                    await _maybe_create_failure_alert(
                                        ci_task,
                                        session,
                                        f"PR #{ci_pr_number} merge conflicts — fix iteration cap exhausted, manual intervention required",
                                    )
                                    from app.failure_classification import (
                                        classify_failure,
                                        persist_failure_category,
                                    )

                                    _fc_mc = classify_failure(ci_task)
                                    persist_failure_category(ci_task, _fc_mc)
                                    record_task_result(
                                        success=False,
                                        failure_class=_fc_mc,
                                        task_id=str(ci_task.id),
                                        task_type=(ci_task.first_payload).get(
                                            "type", ""
                                        ),
                                    )
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(
                                str(ci_task.project_id),
                                "task_updated",
                                {"task_id": str(ci_task.id), "status": ci_task.status},
                            )
                            continue

                        ci_head_sha = pr_data.get("head", {}).get("sha", "")
                        if not ci_head_sha:
                            ci_task.latest_status_message = f"CI waiting on PR #{ci_pr_number} — HEAD SHA unavailable"
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(
                                str(ci_task.project_id),
                                "task_updated",
                                {"task_id": str(ci_task.id), "status": ci_task.status},
                            )
                            logger.info(
                                f"[CI] PR #{ci_pr_number} has no HEAD SHA — skipping CI check this cycle"
                            )
                            continue

                        # ── Phase B3: release lock around get_check_runs_cached ──
                        # Even with TTL caching, a cache miss makes a real GitHub
                        # API call that can hang during rate-limit. Drop the lock
                        # so concurrent heartbeats proceed.
                        # R5-W2-A: content snapshot, not updated_at —
                        # heartbeat onupdate would false-defer every poll.
                        ci_pre_check_snapshot = _capture_ci_polling_snapshot(ci_task)
                        ci_project_id_str = str(ci_task.project_id)
                        await session.commit()
                        # Dedup cached — siblings on the same head_sha collapse
                        # into one underlying GitHub call within the 30s TTL.
                        #
                        # Strict mode (default): route through
                        # ``ci_resolution.resolve_pr_ci_status`` so a bare branch
                        # HEAD with 0 check-runs no longer fail-opens to
                        # ``ci_passed=True``.  When the HEAD has no check-runs
                        # the resolver falls back to the PR-event merge SHA
                        # where the ``pull_request``-triggered build actually
                        # ran; when neither path yields a completed build it
                        # reports unresolved/pending so we keep polling rather
                        # than try to merge against the absence of a signal.
                        # ``ci_resolution_strict_mode=False`` is the operator
                        # rollback handle to the legacy lenient path.
                        from app.shared_state import (
                            runtime_config as _rt_cfg,
                        )

                        # No-CI test-suite merge gate state for this poll. Set in
                        # the no_checks branch when an EXTERNAL no-CI repo should
                        # run its detected suite (verify_tests) before merging;
                        # consumed in the locked block after the re-acquire below.
                        _no_ci_verify_gate = False
                        _no_ci_verify_base = ""

                        if _rt_cfg.get("ci_resolution_strict_mode", True):
                            from app import ci_resolution

                            _resolved = await ci_resolution.resolve_pr_ci_status(
                                pr_number=ci_pr_number,
                                branch_sha=ci_head_sha,
                                token=ci_gh_token,
                                owner=ci_owner,
                                repo=ci_repo,
                                project_id=ci_project_id_str,
                            )
                            _r_state = _resolved["state"]
                            _r_source = _resolved["source"]
                            if _r_state == "green":
                                ci_result = {
                                    "status": "completed",
                                    "conclusion": "success",
                                    "details": _resolved.get("details", []),
                                    "failed_logs": "",
                                }
                            elif _r_state == "red":
                                ci_result = {
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "details": _resolved.get("details", []),
                                    "failed_logs": "",
                                }
                            elif _r_state == "no_checks":
                                # CI is *definitively* absent: the branch HEAD
                                # and every reachable PR-event SHA were queried
                                # cleanly and carry zero check-runs. For PRs
                                # targeting an internal staging branch
                                # (research/*, plan/*), CI is path-filtered out
                                # by design and will NEVER run — polling forever
                                # strands the rollup (incident 2026-05-28:
                                # 0-check PR wedged parent c4d3402e). Treat
                                # 0-checks on a staging base as CI-not-applicable
                                # → green so the orchestrator proceeds to the
                                # mergeability-gated merge (the merge attempt
                                # itself still enforces no-conflicts). CI-gated
                                # bases (main/automain) without checks keep
                                # polling — never merge on absence of signal.
                                _ci_base_ref = (pr_data.get("base") or {}).get(
                                    "ref", ""
                                )
                                _ci_proj = (
                                    await session.get(Project, ci_task.project_id)
                                    if ci_task.project_id
                                    else None
                                )
                                # A *definitive* no_checks proceeds to the
                                # mergeability-gated merge when the PR targets an
                                # internal staging branch (research/plan, CI
                                # path-filtered by design) OR the project declares
                                # it has no CI (build_config.requires_ci_checks=
                                # False) — otherwise (CI-gated repo, checks merely
                                # not registered yet) keep polling. See
                                # Project.proceed_on_no_checks.
                                if _ci_proj is not None and ci_resolution.proceed_on_no_checks(
                                    _ci_proj, _ci_base_ref
                                ):
                                    _is_staging = _ci_proj.is_staging_branch(
                                        _ci_base_ref
                                    )
                                    _gate_on = _rt_cfg.get(
                                        "no_ci_test_gate_enabled", True
                                    )
                                    if ci_resolution.should_run_no_ci_test_gate(
                                        _ci_proj,
                                        _ci_base_ref,
                                        gate_enabled=_gate_on,
                                    ):
                                        # EXTERNAL no-CI repo (requires_ci_checks=
                                        # False) on a real base WITH a runnable
                                        # suite: gate the merge on a verify_tests
                                        # verdict instead of merging blind. Defer
                                        # the spawn / verdict-await to the locked
                                        # block after the re-acquire below; keep
                                        # ci_result PENDING so the loop keeps
                                        # polling until the verdict lands.
                                        _no_ci_verify_gate = True
                                        _no_ci_verify_base = _ci_base_ref
                                        logger.info(
                                            f"[CI] PR #{ci_pr_number} has 0 check-runs "
                                            f"and the project declares no CI — gating "
                                            f"the merge on the repo's detected test "
                                            f"suite (verify_tests) before proceeding"
                                        )
                                        ci_result = {
                                            "status": "pending",
                                            "conclusion": None,
                                            "details": _resolved.get("details", []),
                                            "failed_logs": "",
                                        }
                                    else:
                                        # Immediate-pass (unchanged legacy behavior).
                                        # Internal staging base (CI path-filtered by
                                        # design), kill-switch off, or NO detectable
                                        # test command — an absent-tests repo must
                                        # still progress, so it merges UNVERIFIED.
                                        if _is_staging:
                                            _ci_reason = (
                                                f"targets internal staging branch "
                                                f"'{_ci_base_ref}'"
                                            )
                                        elif not _gate_on:
                                            _ci_reason = (
                                                "the project declares no CI "
                                                "(requires_ci_checks=False) and the "
                                                "no_ci_test_gate kill-switch is off"
                                            )
                                        else:
                                            _ci_reason = (
                                                "the project declares no CI "
                                                "(requires_ci_checks=False) and no "
                                                "test command is detectable — merging "
                                                "UNVERIFIED"
                                            )
                                        logger.info(
                                            f"[CI] PR #{ci_pr_number} has 0 check-runs and "
                                            f"{_ci_reason} — treating as passed so the "
                                            f"merge can proceed"
                                        )
                                        ci_result = {
                                            "status": "completed",
                                            "conclusion": "success",
                                            "details": _resolved.get("details", []),
                                            "failed_logs": "",
                                        }
                                else:
                                    ci_result = {
                                        "status": "pending",
                                        "conclusion": None,
                                        "details": _resolved.get("details", []),
                                        "failed_logs": "",
                                    }
                            elif _r_source == "unresolved":
                                # Transient: couldn't resolve a verdict (GitHub
                                # I/O error, or checks not yet visible). Keep
                                # polling — never merge on the absence of a
                                # signal.
                                ci_result = {
                                    "status": "pending",
                                    "conclusion": None,
                                    "details": _resolved.get("details", []),
                                    "failed_logs": "",
                                }
                            else:
                                ci_result = {
                                    "status": "in_progress",
                                    "conclusion": None,
                                    "details": _resolved.get("details", []),
                                    "failed_logs": "",
                                }
                        else:
                            ci_result = await get_check_runs_cached(
                                ci_gh_token,
                                ci_owner,
                                ci_repo,
                                ci_head_sha,
                                project_id=ci_project_id_str,
                            )
                        # Re-acquire to apply downstream mutations.
                        ci_task = await _lock_task_for_mutation(
                            session,
                            ci_task_id,
                            allowed_statuses=(TaskStatus.EVALUATING,),
                            reason="CI polling after check_runs fetch",
                            expected_snapshot=ci_pre_check_snapshot,
                            snapshot_fn=_capture_ci_polling_snapshot,
                        )
                        if ci_task is None:
                            continue
                        ci_loop = dict(
                            (ci_task.context or {}).get("review_loop", {}) or {}
                        )

                        # ── No-CI test-suite merge gate (verify_tests) ──────────
                        # Reached when the no_checks branch flagged an external
                        # no-CI repo with a runnable suite. We hold the task lock
                        # and a fresh ci_loop here. First poll: spawn verify_tests
                        # on the PR head, record its id, keep polling. Subsequent
                        # polls: once that task is terminal, read its latest
                        # MERGE_TEST_RESULT verdict and set ci_result —
                        #   pass          → completed/success (merge proceeds,
                        #                   still subject to the review gate);
                        #   fail          → completed/failure so the existing
                        #                   handle_ci_completion fix path fires
                        #                   carrying the failing-test output;
                        #   detected=false / no usable verdict → completed/success
                        #                   UNVERIFIED (absent-tests repos must
                        #                   still progress — logged as such).
                        # Kill-switch (no_ci_test_gate_enabled) was checked in the
                        # no_checks branch; when off, _no_ci_verify_gate is never
                        # set and today's immediate-pass applies.
                        if _no_ci_verify_gate:
                            from sqlalchemy.orm.attributes import flag_modified
                            from app.review_pipeline import _spawn_pipeline_task
                            from app.release_qualifier_audit import (
                                _latest_artifact_logs,
                            )
                            from app.models import ArtifactType

                            # Mark active so the "no CI checks appeared" stall
                            # escalation below is bypassed while we verify.
                            ci_loop["ci_checks_seen"] = True
                            _v_recorded = ci_loop.get("verify_tests_verdict")
                            _v_task_id = ci_loop.get("verify_tests_task_id")
                            _v_poll_interval = min(ci_poll_interval * 2, 90)

                            if _v_recorded in ("pass", "undetected"):
                                # Verdict already obtained on a prior poll —
                                # proceed (idempotent until the merge lands).
                                ci_result = {
                                    "status": "completed",
                                    "conclusion": "success",
                                    "details": [],
                                    "failed_logs": "",
                                }
                            elif _v_recorded == "fail":
                                # Already classified failed — belt-and-suspenders
                                # if we re-enter before ci_fix_spawned is set.
                                ci_result = {
                                    "status": "completed",
                                    "conclusion": "failure",
                                    "details": [],
                                    "failed_logs": ci_loop.get(
                                        "verify_tests_output",
                                        "Detected test suite failed.",
                                    ),
                                }
                            elif not _v_task_id:
                                # First encounter: spawn verify_tests on PR head.
                                _v_branch = _resolve_ci_fix_branch(
                                    ci_loop, ci_task.first_payload, pr_data
                                )
                                if not _v_branch:
                                    logger.error(
                                        f"[CI] PR #{ci_pr_number}: cannot resolve "
                                        f"head branch for verify_tests spawn — "
                                        f"retrying next cycle"
                                    )
                                    ci_loop["ci_poll_interval"] = _v_poll_interval
                                    ci_loop["ci_last_polled"] = _utcnow().isoformat()
                                    ci_task.context = {
                                        **(ci_task.context or {}),
                                        "review_loop": ci_loop,
                                    }
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    continue
                                _v_orig = ci_loop.get(
                                    "original_task_id", ""
                                ) or ci_task.first_payload.get("original_task_id", "")
                                _verify_task, _ = await _spawn_pipeline_task(
                                    ci_task,
                                    "verify_tests",
                                    session,
                                    ci_pr_number,
                                    _v_branch,
                                    _no_ci_verify_base,
                                    original_task_id=_v_orig,
                                    suppress_warm_handoff=True,
                                )
                                # _spawn_pipeline_task commits internally — re-lock
                                # the parent before mutating it.
                                ci_task = await _lock_task_for_mutation(
                                    session,
                                    ci_task_id,
                                    allowed_statuses=(TaskStatus.EVALUATING,),
                                    reason="CI polling after verify_tests spawn",
                                )
                                if ci_task is None:
                                    continue
                                ci_loop = dict(
                                    (ci_task.context or {}).get("review_loop", {})
                                    or {}
                                )
                                ci_loop["ci_checks_seen"] = True
                                if _verify_task is not None:
                                    ci_loop["verify_tests_task_id"] = str(
                                        _verify_task.id
                                    )
                                    ci_task.latest_status_message = (
                                        f"No CI on PR #{ci_pr_number} — running "
                                        f"detected test suite to gate merge → "
                                        f"{_verify_task.id}"
                                    )
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                                        f"spawned verify_tests → {_verify_task.id}"
                                    )
                                else:
                                    ci_task.latest_status_message = (
                                        f"No CI on PR #{ci_pr_number} — verify_tests "
                                        f"spawn deferred, will retry"
                                    )
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                                        f"verify_tests spawn suppressed/deferred — "
                                        f"retrying"
                                    )
                                ci_loop["ci_poll_interval"] = _v_poll_interval
                                ci_loop["ci_last_polled"] = _utcnow().isoformat()
                                ci_loop["head_sha"] = ci_head_sha
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                flag_modified(ci_task, "context")
                                session.add(ci_task)
                                await session.commit()
                                await broadcast_to_project(
                                    str(ci_task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": str(ci_task.id),
                                        "status": ci_task.status,
                                    },
                                )
                                continue
                            else:
                                # Await: verify task spawned on a prior poll.
                                _verify_task = None
                                try:
                                    _verify_task = await session.get(
                                        Task, uuid.UUID(_v_task_id)
                                    )
                                except (ValueError, TypeError):
                                    _verify_task = None
                                _terminal = {
                                    TaskStatus.COMPLETED,
                                    TaskStatus.FAILED,
                                    TaskStatus.CANCELLED,
                                }
                                if (
                                    _verify_task is None
                                    or _verify_task.status not in _terminal
                                ):
                                    # Still running — keep polling.
                                    ci_loop["ci_poll_interval"] = _v_poll_interval
                                    ci_loop["ci_last_polled"] = _utcnow().isoformat()
                                    ci_loop["head_sha"] = ci_head_sha
                                    ci_task.latest_status_message = (
                                        f"PR #{ci_pr_number} — awaiting detected-test "
                                        f"verdict (verify_tests {_v_task_id[:8]})"
                                    )
                                    ci_task.context = {
                                        **(ci_task.context or {}),
                                        "review_loop": ci_loop,
                                    }
                                    flag_modified(ci_task, "context")
                                    session.add(ci_task)
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(ci_task.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(ci_task.id),
                                            "status": ci_task.status,
                                        },
                                    )
                                    continue
                                # Terminal — read the latest verdict artifact.
                                _verdict = None
                                if _verify_task.status == TaskStatus.COMPLETED:
                                    _verdict = await _latest_artifact_logs(
                                        session,
                                        _verify_task.id,
                                        ArtifactType.MERGE_TEST_RESULT,
                                    )
                                _outcome, _detail = (
                                    ci_resolution.classify_merge_test_verdict(
                                        _verdict
                                    )
                                )
                                ci_loop["verify_tests_verdict"] = _outcome
                                if _outcome == "pass":
                                    logger.info(
                                        f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                                        f"detected test suite PASSED — proceeding to "
                                        f"merge (subject to review gate)"
                                    )
                                    ci_result = {
                                        "status": "completed",
                                        "conclusion": "success",
                                        "details": [],
                                        "failed_logs": "",
                                    }
                                elif _outcome == "fail":
                                    logger.warning(
                                        f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                                        f"detected test suite FAILED — blocking "
                                        f"merge, spawning fix with failing output"
                                    )
                                    ci_loop["verify_tests_output"] = _detail[:4000]
                                    ci_result = {
                                        "status": "completed",
                                        "conclusion": "failure",
                                        "details": [],
                                        "failed_logs": _detail,
                                    }
                                else:
                                    # detected=false or no usable verdict (verify
                                    # task crashed / produced no artifact): fall
                                    # back to proceed-without-checks. Absent-tests
                                    # repos must still progress — merge UNVERIFIED.
                                    logger.warning(
                                        f"[CI] PR #{ci_pr_number} no-CI merge gate: "
                                        f"no test command detectable (verify_tests "
                                        f"{_v_task_id[:8]} status="
                                        f"{_verify_task.status}) — merge proceeding "
                                        f"UNVERIFIED"
                                    )
                                    ci_result = {
                                        "status": "completed",
                                        "conclusion": "success",
                                        "details": [],
                                        "failed_logs": "",
                                    }
                                # Persist the recorded verdict before
                                # handle_ci_completion re-reads task.context.
                                ci_task.context = {
                                    **(ci_task.context or {}),
                                    "review_loop": ci_loop,
                                }
                                flag_modified(ci_task, "context")
                                session.add(ci_task)
                                # Fall through: ci_result is "completed" so the
                                # pending-skip below is bypassed and
                                # handle_ci_completion runs.

                        # Track whether CI checks have ever been observed for this PR
                        ci_details = ci_result.get("details", [])
                        if ci_details and not ci_loop.get("ci_checks_seen"):
                            ci_loop["ci_checks_seen"] = True
                            ci_loop["ci_first_check_seen_at"] = _utcnow().isoformat()

                        # Detect stalled CI: no checks appeared after reasonable wait
                        if not ci_loop.get("ci_checks_seen") and not ci_details:
                            _cws = ci_loop.get("ci_wait_started")
                            if _cws:
                                try:
                                    _no_checks_elapsed = (
                                        _utcnow() - datetime.fromisoformat(_cws)
                                    ).total_seconds()
                                    if _no_checks_elapsed > 600 and not ci_loop.get(
                                        "no_checks_escalated"
                                    ):
                                        ci_loop["no_checks_escalated"] = True
                                        ci_task.latest_status_message = (
                                            f"No CI checks appeared on PR #{ci_pr_number} after "
                                            f"{int(_no_checks_elapsed / 60)}min — may need manual trigger"
                                        )
                                        ci_task.context = {
                                            **(ci_task.context or {}),
                                            "review_loop": ci_loop,
                                        }
                                        session.add(ci_task)
                                        await _maybe_create_failure_alert(
                                            ci_task,
                                            session,
                                            f"PR #{ci_pr_number}: no CI checks after {int(_no_checks_elapsed / 60)}min",
                                        )
                                        await session.commit()
                                        ci_task = await _lock_task_for_mutation(
                                            session,
                                            ci_task_id,
                                            allowed_statuses=(TaskStatus.EVALUATING,),
                                            reason="CI polling after no-checks alert",
                                        )
                                        if ci_task is None:
                                            continue
                                        ci_loop = dict(
                                            (ci_task.context or {}).get(
                                                "review_loop", {}
                                            )
                                            or {}
                                        )
                                except (ValueError, TypeError):
                                    pass

                        if ci_result["status"] != "completed":
                            details = [
                                d["name"]
                                for d in ci_details
                                if d.get("status") in ("queued", "in_progress")
                            ]
                            ci_task.latest_status_message = f"CI in progress on PR #{ci_pr_number}: {', '.join(details) or 'waiting for checks'}..."
                            # Smarter backoff: faster when CI is actively running
                            if ci_loop.get("escalated"):
                                max_interval = 60  # Post-escalation: poll frequently
                            elif ci_loop.get("ci_checks_seen"):
                                max_interval = 90  # Checks running: moderate backoff
                            else:
                                max_interval = 120  # No checks yet: cautious backoff
                            ci_loop["ci_poll_interval"] = min(
                                ci_poll_interval * 2, max_interval
                            )
                            ci_loop["ci_last_polled"] = _utcnow().isoformat()
                            ci_loop["head_sha"] = ci_head_sha
                            ci_task.context = {
                                **(ci_task.context or {}),
                                "review_loop": ci_loop,
                            }
                            # JSONB nested-dict mutation needs flag_modified —
                            # otherwise ci_last_polled/poll_interval don't
                            # persist and the per-task 30s skip never engages,
                            # so the orchestrator hammers GitHub every 2s.
                            from sqlalchemy.orm.attributes import flag_modified

                            flag_modified(ci_task, "context")
                            session.add(ci_task)
                            await session.commit()
                            await broadcast_to_project(
                                str(ci_task.project_id),
                                "task_updated",
                                {"task_id": str(ci_task.id), "status": ci_task.status},
                            )
                            continue

                        # CI completed — delegate to shared handler
                        from app.ci_completion import handle_ci_completion

                        logger.info(
                            f"[CI] Poll fallback: CI completed for PR #{ci_pr_number}, processing"
                        )
                        await handle_ci_completion(
                            ci_task,
                            ci_result,
                            ci_gh_token,
                            ci_owner,
                            ci_repo,
                            session,
                            source="poll",
                        )
                    except Exception as e:
                        # ci_task may be expired after handle_ci_completion's
                        # internal commit path; accessing ci_task.id would fire
                        # a sync lazy-load on the async session and raise
                        # MissingGreenlet. Use the loop-variable id (already a
                        # plain UUID/str from the SELECT projection) instead.
                        logger.exception(
                            f"[CI] Error checking CI for task {ci_task_id}"
                        )
                        try:
                            # Re-fetch ci_task fresh inside the recovery: if the
                            # outer exception was a connection-class failure, the
                            # session was rolled back and every loaded attribute
                            # is expired — accessing ci_task.context / .id below
                            # would lazy-load on a poisoned connection and crash
                            # this handler too. ``_lock_task_for_mutation`` uses
                            # ``populate_existing=True`` which gives us a live
                            # row; a None return means another worker took the
                            # task or the row is gone, in which case we have
                            # nothing to mutate. (Production 2026-05-08:
                            # orchestrator.py:4750 + models.py:700 cascade.)
                            try:
                                await session.rollback()
                            except Exception:
                                logger.debug(
                                    "[CI] Pre-recovery rollback raised, continuing"
                                )
                            ci_task = await _lock_task_for_mutation(
                                session,
                                ci_task_id,
                                reason="CI polling error recovery",
                            )
                            if ci_task is None:
                                logger.warning(
                                    f"[CI] Skipping error recovery for task {ci_task_id}: row not lockable"
                                )
                                continue
                            status_code = getattr(e, "status_code", None)
                            if status_code == 404:
                                ci_task.status = TaskStatus.FAILED
                                if ci_task.vm_provisioned_at:
                                    ci_task.vm_stopped_at = _utcnow()
                                ci_task.latest_status_message = "CI polling failed: PR not found (404) — PR may have been deleted"
                                _pr_num_404 = (
                                    (ci_task.context or {})
                                    .get("review_loop", {})
                                    .get("pr_number")
                                )
                                logger.exception(
                                    f"[CI] PR #{_pr_num_404} returned 404 — marking task {ci_task_id} FAILED"
                                )
                                await _maybe_create_failure_alert(
                                    ci_task,
                                    session,
                                    "CI polling failed: PR returned 404 — PR may have been deleted or archived",
                                )
                                from app.failure_classification import (
                                    persist_failure_category as _pfc404,
                                )

                                _pfc404(ci_task, "EXTERNAL_API")
                                record_task_result(
                                    success=False,
                                    failure_class="EXTERNAL_API",
                                    task_id=str(ci_task_id),
                                    task_type=(ci_task.first_payload).get("type", ""),
                                )
                            else:
                                ci_task.latest_status_message = (
                                    f"CI check error: {type(e).__name__}: {e!r}"[:500]
                                )
                            session.add(ci_task)
                            await session.commit()
                        except Exception:
                            logger.exception(
                                f"[CI] Failed to update ci_task status after error for task {ci_task_id}"
                            )
                            try:
                                await session.rollback()
                            except Exception:
                                logger.exception(
                                    f"[CI] Rollback also failed for task {ci_task_id}"
                                )

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
            backoff = min(
                0.5 * (2 ** (_orchestrator_consecutive_errors - 1)), 60
            ) * random.uniform(0.8, 1.2)
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

        # Monitoring checks run every cycle (not just when idle)
        try:
            await _check_monitoring_window()
            await _check_external_health()
            await _check_pause_mode()
            # Orphaned task recovery — every 120s. Stays on the leader cycle but
            # is now bounded (per-cycle row cap + per-GitHub-call asyncio.wait_for
            # + per-row heartbeat in services._recover_orphaned_tasks, C3) so it
            # can no longer saturate the cycle on a large orphan backlog.
            if (_utcnow() - _last_orphan_check).total_seconds() >= 120:
                _last_orphan_check = _utcnow()
                await _recover_orphaned_tasks()
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
            if (_utcnow() - _last_post_commit_sweep).total_seconds() >= 60:
                _last_post_commit_sweep = _utcnow()
                await _sweep_post_commit_pending()
            # Drain the deferred missing-branch alert queue into durable,
            # idempotent operator Alerts. The synchronous branch resolvers
            # record intent off the hot path (no DB I/O); the durable persist
            # runs here in an async-safe context so the signal survives
            # restarts without re-spamming (one undismissed alert per project
            # column). Guarded so a flush failure never stalls monitoring.
            try:
                async with AsyncSessionLocal() as _branch_alert_session:
                    if await _shared_state.flush_pending_branch_alerts(
                        _branch_alert_session
                    ):
                        await _branch_alert_session.commit()
            except Exception:
                logger.exception(
                    "[ORCHESTRATOR] Failed to flush pending branch alerts"
                )
            _orchestrator_consecutive_monitoring_errors = 0
        except Exception as e:
            _orchestrator_consecutive_monitoring_errors += 1
            monitoring_backoff = min(
                0.5 * (2 ** (_orchestrator_consecutive_monitoring_errors - 1)), 60
            ) * random.uniform(0.8, 1.2)
            logger.error(
                f"[ORCHESTRATOR] Monitoring check error (consecutive={_orchestrator_consecutive_monitoring_errors}, backoff={monitoring_backoff:.1f}s): {e}",
                exc_info=True,
            )

        # Sleep based on whether we found tasks
        try:
            if _orchestrator_consecutive_errors > 0:
                backoff = min(
                    0.5 * (2 ** (_orchestrator_consecutive_errors - 1)), 60
                ) * random.uniform(0.8, 1.2)
                await asyncio.sleep(backoff)
            elif _orchestrator_consecutive_monitoring_errors > 0:
                monitoring_backoff = min(
                    0.5 * (2 ** (_orchestrator_consecutive_monitoring_errors - 1)), 60
                ) * random.uniform(0.8, 1.2)
                await asyncio.sleep(monitoring_backoff)
            elif queued_tasks:
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(5)
            # Check autonomous research every cycle — interval gate inside handles throttling.
            # Spawns only when VM slots are available (not tied to queue being empty).
            await _check_idle_research()
        except asyncio.CancelledError:
            logger.info("[ORCHESTRATOR] Worker shutting down.")
            break
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Sleep/idle check error: {e}", exc_info=True)
