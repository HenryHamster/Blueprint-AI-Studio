"""Background reaper service: detects and recovers orphaned/stale tasks.

The reaper runs every 30 seconds and is responsible for:

1. **Heartbeat Timeout**: Monitors IN_PROGRESS tasks and marks them FAILED if their
   last heartbeat exceeds the timeout window (default 5 minutes)
2. **VM Cleanup**: Deletes VMs for timed-out tasks via GCP Compute Engine API
3. **Preemption Detection**: Uses vm_lifecycle.is_orchestrator_initiated_delete() to
   distinguish between:
   - Orchestrator-initiated cleanup (expected; no metric impact)
   - GCP Spot VM preemption (unexpected; increments spot_preemption_count)
4. **Orphan Cleanup**: Periodically scans for VMs that exist in GCP but have no
   corresponding PROVISIONING task (e.g., VMs left behind by crashed agents)

Key Functions:
- reaper_service() - Main polling loop (async, called by lifecycle)
- _mark_task_failed_from_timeout() - Handles the transition of a timed-out task to FAILED
- _cleanup_orphaned_vms() - Finds and deletes VMs without corresponding tasks

The reaper is the primary safety net for task timeouts and VM resource cleanup. It
prevents long-running agents from blocking the orchestrator's concurrency limits and
ensures GCP VMs don't accumulate unnecessarily."""

import asyncio
import contextlib
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Dict

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
    VmContact,
    TaskContact,
    bank_task_current_attempt,
)
from app.compute import (
    get_provision_error,
    record_zone_failure,
    get_vm_zone,
    INFRA_PROVISION_PATTERNS,
    any_zone_available,
    soonest_zone_recovery,
)
from app.compute_cloud_run import is_cloud_run_task
from app.shared_state import runtime_config as _runtime_config
from app.shared_state import (
    record_pipeline_event,
    external_health as _external_health,
    resolve_retry_budget,
    derive_task_branch as _derive_task_branch,
    get_project_integration_branch as _get_project_integration_branch,
    get_project_task_branch_namespace as _get_project_task_branch_namespace,
)
from app.failure_classification import (
    classify_failure,
    persist_authoritative_verdict,
    persist_failure_category,
    RATE_LIMIT,
    DEAD_AUTH,
    _stuck_llm_call_no_progress,
    _persist_stuck_call_reclassification,
)
from app.retry_budget import append_failure_class_history
from app.task_blocked import (
    mark_task_for_fresh_execution_attempt,
    reset_task_for_fresh_attempt,
)
from app.time_utils import (
    _utcnow,
    clear_retry_after,
    exponential_backoff,
    set_retry_after,
)
from app.reaper_infra_recovery import run_infra_recovery_and_hold_maintenance
from app.reaper_post_orphan_sweep import run_post_orphan_sweep

logger = logging.getLogger(__name__)


def _normalize_reaper_retry_failure_class(failure_class: str, context: object) -> str:
    """Match clone retry classification for raw lowercase scope-overflow labels."""
    if failure_class != "CODE_ERROR" or not isinstance(context, dict):
        return failure_class
    if str(context.get("failure_category") or "").lower() == "scope_overflow":
        return "SCOPE_OVERFLOW"
    return failure_class


from app.reaper_relock import _relock_orphan_task


# Reaper: track tasks skipped by skip_locked to force-reap after repeated skips
_reaper_skip_counts: Dict[uuid.UUID, int] = {}

# Per-task consecutive reap-crash streak tracker. Persists across cycles so a
# poison-pill task accumulates its streak and is quarantined before it crash-loops
# the reaper indefinitely. Mirrors _MAX_SWEEP_CRASH_STREAK in release_qualifier.py.
_orphan_reap_crash_streaks: dict[str, int] = {}
_ORPHAN_REAP_CRASH_STREAK_LIMIT = 3

# Reaper liveness telemetry. Incremented at the END of each fully-successful
# cycle (after the success path resets consecutive_errors). If the cycle body
# raises, the counter does NOT increment and the age gauge keeps growing —
# that's the signal a deploy gate / alert can fire on. Production 2026-05-04:
# the reaper crashed every cycle on a MissingGreenlet at reaper.py:724 for
# 80+ minutes before operator noticed; this counter is the broad backstop
# against any "reaper crash-loops silently" failure mode, not just that bug.
_reaper_cycles_completed_count: int = 0
_reaper_last_cycle_completed_at: datetime | None = None

# In-cycle progress heartbeat. Updated frequently *within* a cycle (cycle start
# + once per orphan-reap / stale-EVALUATING / rescue iteration) so liveness
# reflects "the reaper is making progress", NOT "a full cycle finished within N
# seconds". A cycle's wall time grows O(orphan backlog) — gating liveness on
# full-cycle completion kills a slow-but-healthy reaper, discarding its
# in-flight orphan recovery and spiralling (logs: "no cycle completion in
# 684→787→916s" under backlog → watchdog cancel mid-I/O → failure storm).
# Mirrors orchestrator._orchestrator_last_progress (PR #6678). The lifecycle
# watchdog reads the FRESHER of this and _reaper_last_cycle_completed_at; only a
# genuinely wedged reaper (no heartbeat for REAPER_HANG_SECONDS) is restarted.
_reaper_last_progress: datetime | None = None

# How long the reaper may go without a progress heartbeat before the lifecycle
# watchdog treats it as hung and cancels it for a fresh respawn. Env-overridable
# for ops tuning, mirroring ORCHESTRATOR_HANG_SECONDS. Default 300.0 preserves
# the prior hardcoded _reaper_stall_threshold (5 missed 60s cycles) — behaviour
# is identical unless tuned.
REAPER_HANG_SECONDS = float(os.getenv("REAPER_HANG_SECONDS", "300"))


from app.reaper_halt import (
    _REAPER_CYCLE_HALT_THRESHOLD,
    _REAPER_HALF_OPEN_PROBE_LIMIT,
    _reaper_halt_state,
    _mark_reaper_progress,
    _post_reaper_halt_alert,
    _post_reaper_recovery_alert,
)
from app.reaper_halt import get_reaper_stats, is_reaper_halted  # noqa: F401

# Per-(provider, model) regression scan cadence state is now managed in
# reaper_sweeps.py (sweep_regression_scan).  The constants are kept there.


# Default per-cycle orphan-processing cap. Each orphan costs serial GitHub/VM
# I/O (get_vm_status, disable_account_model, _capture_serial_for_vm,
# get_pull_request — up to ~30s each), so a large backlog can blow past the
# watchdog window. The cap bounds per-cycle work; deferred orphans re-qualify
# automatically next cycle via the status-based Step-1 scan (no separate
# deferral queue needed). Override via runtime_config["reaper_max_orphans_per_cycle"].
_REAPER_MAX_ORPHANS_PER_CYCLE_DEFAULT = 60

# Per-cycle WALL-TIME budget (seconds) for the orphan-processing loop. The count
# cap above bounds the NUMBER of orphans, but each orphan's serial GitHub/VM I/O
# is variable (30-120s+ under API latency / retries), so a cap-sized batch of
# slow orphans can still run the cycle far past the watchdog window. The intra-
# cycle _mark_reaper_progress heartbeat keeps the watchdog from killing the
# running cycle, but a wedged-slow cycle still starves the NEXT sweep (stale-
# EVALUATING, retry_after, infra recovery, etc. all wait behind it — the
# 2026-05-30 leader-loop saturation shape). So we also bound wall time: once the
# loop has spent this long, stop processing new orphans and let the remainder
# re-qualify next cycle via the Step-1 status scan (no separate deferral queue).
# The orphan currently mid-flight always finishes (we check the budget BEFORE
# starting each iteration, never mid-reap). Default 180s is comfortably under
# REAPER_HANG_SECONDS (300s) so a single cycle's orphan loop cannot, on its own,
# consume the whole watchdog window. Override via
# runtime_config["reaper_orphan_loop_time_budget_seconds"] (0/negative = no
# time budget, i.e. process every selected orphan — fail-safe if misconfigured).
_REAPER_ORPHAN_LOOP_TIME_BUDGET_DEFAULT = 180.0


from app.reaper_orphan_classify import (
    _ORPHAN_PRIORITY_RATE_LIMITED,  # noqa: F401
    _ORPHAN_PRIORITY_SPOT,  # noqa: F401
    _ORPHAN_PRIORITY_PROVISION,  # noqa: F401
    _ORPHAN_PRIORITY_DEFAULT,  # noqa: F401
    _orphan_priority_key,  # noqa: F401
    _select_orphans_for_cycle,
    _vm_contact_within_cutoff,  # noqa: F401
    _vm_tool_use_within_cutoff,  # noqa: F401
    _is_multiplexed_vm_id,
    _classify_orphan,
    _HEARTBEAT_STALE_DEFAULTS,  # noqa: F401
    _HEARTBEAT_EFFORT_FLOORS,  # noqa: F401
    _PRECLAIM_STALE_DEFAULTS,  # noqa: F401
    _PROGRESS_STALE_DEFAULTS,  # noqa: F401
    _THINKING_HEAVY_TASK_TYPES,  # noqa: F401
    _THINKING_HEAVY_PROGRESS_STALE_SECONDS,  # noqa: F401
    _progress_stale_cutoff,
    _preclaim_stale_cutoff,  # noqa: F401
    _heartbeat_stale_cutoff,
    _build_runner_report_lookup,
    _extract_tool_trace_tail,
    _task_has_open_pr,  # noqa: F401
)
from app.reaper_orphan_io import _run_orphan_io


_TRANSIENT_5XX_PATTERNS = frozenset(
    {
        "http 502",
        "http 503",
        "http 504",
        "502 bad gateway",
        "503 service",
        "504 gateway",
        "server error",
    }
)


# Audit R3-W2 (H7): per-tick fetch cap for unbounded reaper scans. A project
# with a flood of stuck tasks would otherwise pull megabytes per tick. The
# constant is intentionally generous (5000) — any tick that hits the cap
# emits a WARNING so operators can chunk-paginate as a follow-up.
_BATCH = 5000


def _build_active_scan_stmt():
    """Return the column-projected SELECT for the reaper's 60s active-task scan.

    Excludes ``plan_output`` (heavy JSONB, never read on the reap path).
    Retains ``status_log``/``context``/``payloads`` because the Task event
    listeners and ``_heartbeat_stale_cutoff`` require them (MissingGreenlet
    under async sessions if absent).  ``raiseload=True`` catches future
    accidental full-row loads during development.
    """
    return (
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.last_heartbeat,
                Task.last_tool_use_at,
                Task.started_at,
                Task.created_at,
                Task.vm_instance_id,
                Task.vm_provisioned_at,
                Task.project_id,
                Task.parent_task_id,
                Task.latest_status_message,
                Task.attempt_count,
                Task.context,
                Task.payloads,
                Task.status_log,
                Task.retry_after_until,
                Task.previous_error_fingerprint,
                Task.requeue_in_flight,
                Task.description,
                raiseload=True,
            )
        )
        .where(Task.status.in_([TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]))
        .limit(_BATCH)
    )


from app.retry_prompt import _copy_analysis_artifacts_to_previous  # noqa: E402, F401, RUF100


from app.reaper_evaluating_sweeps import (
    sweep_evaluating_parents,
    sweep_orphaned_direction_provider_tasks,
)
from app.reaper_sweeps import (
    sweep_queued_age_alerts,
    sweep_stalled_strategic_cycles,
    sweep_regression_scan,
    sweep_stale_migration_reservations,
    sweep_stuck_qualifications,
    sweep_evergreen_fill_decision_retention,
    sweep_expired_message_attachment_uploads,
    sweep_strategic_context_retention,
    sweep_routine_message_retention,
    sweep_unblock_blocked_tasks,
    sweep_recover_stranded_transient_blockers,
    sweep_activate_staged_directions,
    sweep_stale_staged_directions,
    sweep_merge_gate_verification_degradation,
    sweep_migration_slot_contention,
    sweep_converge_linked_bug_messages,
    sweep_converge_duplicate_foundations,
    sweep_resolve_open_owned_bug_messages,
    sweep_resolve_stale_open_bug_messages,
    sweep_resolve_linked_messages,
    sweep_decay_stale_open_messages,
    sweep_auto_resolve_answered_messages,
    sweep_decay_stale_open_bug_messages,
    sweep_decay_stale_unlinked_blocker_messages,
    sweep_deliver_task_wakes,
    sweep_wake_delivery_stall_alerts,
    sweep_orphaned_cross_base_children,
    sweep_metrics_integrity,
    sweep_mode_knob_graduation,
    sweep_operator_inbox_liveness,
    sweep_backfill_null_audience_messages,
    sweep_capability_backing_proofs,
    sweep_auto_clear_moot_pending_confirmations,
    sweep_decay_stale_pending_confirmations,
    sweep_closed_handmerge_prs,
    sweep_protect_referenced_compute_image_digests,
    sweep_reconcile_gc_guard_db_pins,
    sweep_refresh_project_compute_images,
    sweep_stranded_runner_subprocess_compute_jobs,
    sweep_proof_earn_loop,
    sweep_silent_runner_compute_jobs,
    sweep_retire_viewable_artifacts,
    sweep_retire_consumed_message_attachments,
    sweep_backfill_merged_named_test_coverage,
    _sweep_strategic_cycle_starvation,
)
from app.acceptance_gate import backfill_acceptance_verdicts


async def _mark_landed_parent_dependents(parent_id, session) -> None:
    """Completion-side warm-reuse trigger for reaper-driven rollup landings.

    The reaper re-triggers ``_check_and_trigger_rollup`` from two places — the
    post-reap loop (a failed child whose surviving siblings complete the parent)
    and the Step-3 stale-EVALUATING sweep. When that lands a parent's rollup PR
    on the integration branch the parent transitions to COMPLETED and its QUEUED
    dependents are unblocked, but — exactly as on the orchestrator's CI-completion
    and the in-process merge paths — nothing here proactively stamps the sticky
    ``_deps_satisfied`` readiness marker on those dependents. So a just-unblocked
    sibling-to-sibling fan-out child stays unmarked until a later capacity-and-
    window-bounded dispatch scan happens to reach it, and the warm VM polling for
    follow-on work inside its teardown-defer grace window skips it and tears down
    cold while the orchestrator cold-provisions a fresh VM for that very dependent.

    Re-fetch the parent on the shared session (full row via ``session.get`` — no
    partial projection, so no MissingGreenlet) and, ONLY if it has actually
    landed (status COMPLETED), delegate to the single authoritative
    ``_mark_dependents_warm_claimable``. That reuses the same GitHub-aware
    dependency resolution the dispatch scan uses, runs in its own sessions, and
    stamps only the ``"eligible"`` result — a dependent with any still-unresolved
    or failed dependency continues to carry no marker and cold-dispatch exactly as
    before. Adds no cold provisioning. Best-effort: a failure here never aborts the
    reaper cycle.
    """
    if parent_id is None:
        return
    try:
        from app.task_lifecycle import _mark_dependents_warm_claimable

        parent = await session.get(Task, parent_id)
        if parent is None or parent.status != TaskStatus.COMPLETED:
            return
        await _mark_dependents_warm_claimable(parent)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[REAPER] Dependent warm-claim marking failed for parent {parent_id}: {e}",
        )


def _record_reap_reason_history(
    session,
    task_id,
    *,
    reap_reason: str,
    reap_reason_code: str,
    vm_instance_id: str | None = None,
) -> None:
    """Append a durable TaskHistory row carrying WHY the reaper acted.

    ``reap_reason`` otherwise lives only in mutable channels
    (``latest_status_message``, context meta) that the NEXT reap or status
    update overwrites — a task reaped three times keeps only the last reason.
    Mirrors ``compute_jobs._record_terminal_reason_history``: add-only
    (``session.add``); the caller owns the commit alongside the reap mutation
    itself, so an aborted iteration rolls the row back with it.
    """
    session.add(
        TaskHistory(
            task_id=task_id,
            action="REAPED",
            details={
                "reap_reason": reap_reason,
                "code": reap_reason_code,
                "vm_instance_id": vm_instance_id,
            },
        )
    )


async def reaper_service():
    """
    Background worker that scans for orphaned VMs.
    Marks them as FAILED and issues a delete command to GCP if they haven't sent a heartbeat in 5 minutes.
    """
    # Deferred imports to avoid circular dependency with app.main
    from app.max_plan_pool import (
        clear_task_llm_runtime_context,
        get_task_llm_account_id,
        release_max_plan_account,
    )
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.routers.agent_status import record_task_result
    from app.broadcast import broadcast_to_project

    consecutive_errors = 0
    while True:
        if _reaper_halt_state["halted"]:
            if _reaper_halt_state.get("hard_halted"):
                # Hard halt: persistent bug, requires operator reset.
                try:
                    await asyncio.sleep(300)
                except asyncio.CancelledError:
                    logger.info("[REAPER] Service shutting down (hard-halted).")
                    break
                continue
            # Soft halt: check whether the cooldown has elapsed for a half-open probe.
            halt_ts = _reaper_halt_state.get("halt_timestamp")
            cooldown_base = int(
                _runtime_config.get("reaper_cycle_halt_cooldown_seconds", 300)
            )
            half_open_failures = _reaper_halt_state.get("half_open_failures", 0)
            cooldown = exponential_backoff(
                half_open_failures + 1, base=float(cooldown_base), cap=3600.0
            )
            elapsed = (
                (_utcnow() - halt_ts).total_seconds() if halt_ts else (cooldown + 1)
            )
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                try:
                    await asyncio.sleep(min(60.0, max(5.0, remaining)))
                except asyncio.CancelledError:
                    logger.info("[REAPER] Service shutting down (soft-halted).")
                    break
                continue
            logger.info(
                "[REAPER] Halt cooldown elapsed (%.0fs, half_open_failures=%d) "
                "— attempting half-open probe cycle",
                elapsed,
                half_open_failures,
            )
            _reaper_halt_state["_in_probe"] = True
            # Fall through to run one probe cycle (no continue).
        session = None
        try:
            async with AsyncSessionLocal() as session:
                # Intra-cycle liveness heartbeat at the very start of the cycle
                # body. The lifecycle watchdog judges reaper liveness on the
                # FRESHER of this and the last completed cycle, so a long cycle
                # under orphan backlog is not killed mid-I/O. See
                # _mark_reaper_progress + the reaper watchdog in lifecycle.py.
                _mark_reaper_progress()
                # Step 1: Read active tasks WITHOUT locks (avoids blocking heartbeat writes)
                # Phase B4 (H-P1): drop heavy ``plan_output`` JSONB from the projection.
                # ``status_log`` MUST stay loaded because the Task.status / Task.
                # latest_status_message event listeners (models.py ~360) read it on
                # every assignment; under async sessions an unloaded status_log
                # triggers MissingGreenlet. ``context`` and ``payloads`` stay loaded
                # because _heartbeat_stale_cutoff / _preclaim_stale_cutoff read both
                # via task.first_payload (L-P2). The reaper mutates status,
                # latest_status_message, vm_instance_id, last_heartbeat, started_at,
                # context, payloads, attempt_count downstream.
                # Audit R3-W2 (H7): cap per-tick fetch at _BATCH rows.
                stmt = _build_active_scan_stmt()
                active_tasks = (await session.execute(stmt)).scalars().all()
                if len(active_tasks) >= _BATCH:
                    logger.warning(
                        "[REAPER] active sweep hit cap=%d rows — result truncated; "
                        "follow-up: chunk-paginate",
                        _BATCH,
                    )

                now = _utcnow()
                ten_minutes_ago = now - timedelta(minutes=10)
                fifteen_minutes_ago = now - timedelta(minutes=15)

                # Batch query for PULL_REQUEST artifacts to support PR-based heartbeat
                # cutoff reduction. Tasks with open PRs use a 90s cutoff instead of
                # scope-aware defaults to detect post-PR silent deaths faster.
                task_ids_to_check = {t.id for t in active_tasks if t.vm_instance_id}
                pr_artifacts = (
                    (
                        await session.execute(
                            select(Artifact.task_id).where(
                                Artifact.task_id.in_(task_ids_to_check),
                                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            )
                        )
                    )
                    .scalars()
                    .all()
                    if task_ids_to_check
                    else []
                )
                tasks_with_pr = set(pr_artifacts)

                # Heartbeat false-reap fix (option B): batch-fetch the
                # uncontended ``vmcontact`` liveness rows for THIS sweep's
                # candidate VMs ONCE (not one query per task) and build a
                # {vm_instance_id: last_contact_at} lookup. _classify_orphan
                # consults it to suppress a heartbeat-stale reap when the
                # orchestrator demonstrably heard from the VM within the same
                # cutoff — covering the case where the agent's last_heartbeat
                # write was lock-starved by the rollup's task-row lock. A
                # genuinely dead VM (no requests) has a stale/absent row and is
                # still reaped on the existing schedule. Both the Step-1 scan
                # and the Step-2 post-lock re-verify pass this SAME lookup.
                candidate_vm_ids = {
                    t.vm_instance_id for t in active_tasks if t.vm_instance_id
                }
                vm_contact_lookup: dict[str, datetime] = {}
                # Fix 3b: also build a {vm_instance_id: last_tool_use_at} map from
                # the SAME query. The progress-stall branch consults it so a
                # lock-starved-but-PROGRESSING agent (its on-row last_tool_use_at
                # write was skipped by the rollup task-row lock) is spared, while
                # a genuinely stalled agent (no recent tool use anywhere) is still
                # reaped. Distinct from the heartbeat-stale override above.
                vm_tool_use_lookup: dict[str, datetime] = {}
                if candidate_vm_ids:
                    contact_rows = (
                        await session.execute(
                            select(
                                VmContact.vm_instance_id,
                                VmContact.last_contact_at,
                                VmContact.last_tool_use_at,
                            ).where(VmContact.vm_instance_id.in_(candidate_vm_ids))
                        )
                    ).all()
                    vm_contact_lookup = {
                        vm_id: last_contact
                        for vm_id, last_contact, _last_tool in contact_rows
                        if last_contact is not None
                    }
                    vm_tool_use_lookup = {
                        vm_id: last_tool
                        for vm_id, _last_contact, last_tool in contact_rows
                        if last_tool is not None
                    }
                # Multiplexed-runner fix: the two maps above are keyed on
                # ``vm_instance_id``, which is only one-row-per-TASK on GCE. A fleet
                # runner multiplexes many tasks onto one ``runner:<id>`` vm id, so
                # ANY agent's heartbeat advances the shared row and a hung agent
                # beside a working sibling never reads as stalled (2026-07-12 zombie
                # class). Batch-fetch the TASK-scoped ``taskcontact`` rows for the
                # SAME candidates; _classify_orphan reads these instead of the
                # vm-keyed maps for ``runner:`` vm ids and keeps using the vm-keyed
                # maps for GCE. ``last_contact_at`` is NOT NULL, so presence in
                # task_contact_lookup is the "row exists" oracle that lets an ABSENT
                # row (pre-rollout) fall back to the old behaviour while a PRESENT
                # row with a NULL/stale tool stamp correctly reaps.
                task_contact_lookup: dict[str, datetime] = {}
                task_tool_use_lookup: dict[str, datetime] = {}
                # Only ``runner:`` tasks: _classify_orphan consults the task-scoped
                # row solely when _is_multiplexed_vm_id(task) is True, so fetching
                # rows for GCE tasks (1 task per VM — their vmcontact row already IS
                # the task's row) would load rows nothing ever reads.
                candidate_task_ids = [
                    t.id
                    for t in active_tasks
                    if t.vm_instance_id and _is_multiplexed_vm_id(t)
                ]
                if candidate_task_ids:
                    task_contact_rows = (
                        await session.execute(
                            select(
                                TaskContact.task_id,
                                TaskContact.last_contact_at,
                                TaskContact.last_tool_use_at,
                            ).where(TaskContact.task_id.in_(candidate_task_ids))
                        )
                    ).all()
                    task_contact_lookup = {
                        str(task_id): last_contact
                        for task_id, last_contact, _last_tool in task_contact_rows
                        if last_contact is not None
                    }
                    task_tool_use_lookup = {
                        str(task_id): last_tool
                        for task_id, _last_contact, last_tool in task_contact_rows
                        if last_tool is not None
                    }
                # Fleet-runner self-reports: defeats the shared-VmContact
                # suppression for a task whose OWN runner freshly reports it is
                # no longer running it (zombie tasks on busy runners; see
                # _runner_report_excludes_task). Batch-fetched once per sweep.
                runner_report_lookup = await _build_runner_report_lookup(
                    session, candidate_vm_ids
                )

                # State-machine invariants after the clock-separation fix:
                #   - vm_provisioned_at: written by orchestrator when the VM is dispatched
                #     and when GCE confirms it's running. Never written by the agent.
                #   - last_heartbeat: written only by the agent (claim + heartbeat interval).
                #     None on a PROVISIONING task means the agent has not contacted us yet.
                #
                # Thresholds:
                #   - IN_PROGRESS + stale heartbeat → agent crashed mid-run (attempt budget).
                #     Stale cutoff is scope-aware: 5min (small) / 7min (medium) / 10min (large).
                #     Exception: tasks with open PRs use 90s cutoff to speed handoff to review.
                #   - PROVISIONING + vm_provisioned_at > 6min ago + no heartbeat → silent pre-claim
                #     death (provision budget). 6min gives margin over the ~90-150s nominal cold
                #     boot + auth validation + claim sequence.
                #   - PROVISIONING + started_at > 15min ago → absolute catch-all.
                orphaned_ids = []
                for task in active_tasks:
                    if not task.vm_instance_id:
                        continue
                    has_pr = task.id in tasks_with_pr
                    # Single source of truth: _classify_orphan owns the full
                    # state machine (see helper). Step 2's post-lock
                    # re-verification calls the identical predicate, so the two
                    # passes can never drift.
                    if not _classify_orphan(
                        task,
                        now,
                        has_open_pr=has_pr,
                        vm_contact_lookup=vm_contact_lookup,
                        vm_tool_use_lookup=vm_tool_use_lookup,
                        runner_report_lookup=runner_report_lookup,
                        task_contact_lookup=task_contact_lookup,
                        task_tool_use_lookup=task_tool_use_lookup,
                    ):
                        continue
                    orphaned_ids.append(task.id)
                    # Step-1-only operator breadcrumb for the most common stuck
                    # case (kept here so the helper stays side-effect-free).
                    if (
                        task.status == TaskStatus.PROVISIONING
                        and task.started_at
                        and task.started_at < fifteen_minutes_ago
                    ):
                        logger.warning(
                            f"[REAPER] Task {task.id} stuck in PROVISIONING for >15min (started_at={task.started_at})"
                        )

                # Step 2: Lock ONLY orphaned rows (heartbeats on healthy tasks are never blocked)
                # expire_all() nukes the identity map so step 2 MUST read fresh from DB.
                # populate_existing=True was tried but doesn't reliably refresh with
                # with_for_update(skip_locked=True) on async sessions.
                orphaned_tasks = []
                if orphaned_ids:
                    session.expire_all()
                    lock_stmt = (
                        select(Task)
                        .where(Task.id.in_(orphaned_ids))
                        .with_for_update(skip_locked=True)
                    )
                    orphaned_tasks = list(
                        (await session.execute(lock_stmt)).scalars().all()
                    )

                    # Track tasks skipped by skip_locked — attempt force-reap after 5 consecutive skips.
                    # The threshold is 5 (not 3) to give more time before force-lock attempts, since
                    # transient lock contention (e.g. a heartbeat write) often resolves within a few cycles.
                    locked_ids = set(orphaned_ids) - {t.id for t in orphaned_tasks}
                    for lid in locked_ids:
                        _reaper_skip_counts[lid] = _reaper_skip_counts.get(lid, 0) + 1
                    for t in orphaned_tasks:
                        _reaper_skip_counts.pop(t.id, None)

                    # Force-lock persistently skipped tasks using skip_locked=True (non-blocking).
                    # A task skipped here stays in _reaper_skip_counts and will be retried next cycle,
                    # so the reaper never stalls waiting on a contested row.
                    force_lock_ids = [
                        lid for lid, count in _reaper_skip_counts.items() if count >= 5
                    ]
                    if force_lock_ids:
                        try:
                            session.expire_all()
                            # Audit C4 (M-C3): the SET LOCAL lock_timeout=60s
                            # below is a defensive no-op as long as the
                            # acquisition uses skip_locked=True — Postgres'
                            # SKIP LOCKED returns immediately for any row
                            # held by another transaction, never blocking, so
                            # lock_timeout never fires on this path. The
                            # statement is kept (a) to document intent for
                            # operators reading logs, and (b) so a future
                            # change that replaces skip_locked with a NOWAIT
                            # or blocking variant inherits the bounded wait
                            # automatically. Reverts on commit/rollback.
                            from sqlalchemy import text as _text

                            await session.execute(
                                _text("SET LOCAL lock_timeout = '60000'")
                            )
                            force_stmt = (
                                select(Task)
                                .where(Task.id.in_(force_lock_ids))
                                .with_for_update(skip_locked=True)
                            )
                            force_locked = list(
                                (await session.execute(force_stmt)).scalars().all()
                            )
                            for t in force_locked:
                                if t.status not in (
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                ):
                                    continue
                                has_pr = t.id in tasks_with_pr
                                if (
                                    t.last_heartbeat
                                    and t.last_heartbeat
                                    >= _heartbeat_stale_cutoff(
                                        t, now, has_open_pr=has_pr
                                    )
                                ):
                                    continue
                                _reaper_skip_counts.pop(t.id, None)
                                orphaned_tasks.append(t)
                                logger.warning(
                                    f"[REAPER] Force-locked persistently skipped task {t.id}"
                                )
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                f"[REAPER] Force-lock failed (lock timeout?): {e}"
                            )
                            # Reset skip counts so force-lock is retried next cycle
                            for lid in force_lock_ids:
                                _reaper_skip_counts.pop(lid, None)

                    # Clean stale entries from skip counts
                    orphaned_id_set = set(orphaned_ids)
                    for tid in list(_reaper_skip_counts.keys()):
                        if tid not in orphaned_id_set:
                            del _reaper_skip_counts[tid]

                    # Re-verify after acquiring lock (heartbeat may have arrived between step 1 and 2)
                    # expire_all() above ensures these objects have fresh DB values.
                    # Uses the SAME _classify_orphan predicate as the Step-1 scan —
                    # the previous verbatim in-code mirror is gone, so the two passes
                    # cannot drift. ``now`` is refreshed because time passed during
                    # lock acquisition (a heartbeat that arrived in that window makes
                    # the task no longer stale).
                    now = _utcnow()  # Refresh now — time passed during lock acquisition
                    # Keep the derived cutoffs consistent with the refreshed clock;
                    # the downstream reap-reason classifier reads fifteen_minutes_ago.
                    ten_minutes_ago = now - timedelta(minutes=10)
                    fifteen_minutes_ago = now - timedelta(minutes=15)
                    # Re-fetch the vm_contact lookup with the REFRESHED clock so a
                    # contact that landed during the lock-acquisition window
                    # (between Step 1 and Step 2) also aborts the reap — exactly
                    # the lock-starved-heartbeat case this fix targets. Scoped to
                    # the verified-candidate VMs only (small set).
                    step2_vm_ids = {
                        t.vm_instance_id for t in orphaned_tasks if t.vm_instance_id
                    }
                    step2_contact_lookup: dict[str, datetime] = {}
                    step2_tool_use_lookup: dict[str, datetime] = {}
                    if step2_vm_ids:
                        step2_contact_rows = (
                            await session.execute(
                                select(
                                    VmContact.vm_instance_id,
                                    VmContact.last_contact_at,
                                    VmContact.last_tool_use_at,
                                ).where(VmContact.vm_instance_id.in_(step2_vm_ids))
                            )
                        ).all()
                        step2_contact_lookup = {
                            vm_id: last_contact
                            for vm_id, last_contact, _last_tool in step2_contact_rows
                            if last_contact is not None
                        }
                        step2_tool_use_lookup = {
                            vm_id: last_tool
                            for vm_id, _last_contact, last_tool in step2_contact_rows
                            if last_tool is not None
                        }
                    # Re-fetch the TASK-scoped rows with the refreshed clock too:
                    # a heartbeat from THIS task that landed during lock acquisition
                    # must abort the reap, mirroring the vm_contact re-fetch above.
                    # Without this the Step-2 re-verify would see no task-scoped row
                    # and fall back to the shared (sibling-fed) vm row.
                    # Same ``runner:``-only filter as the Step-1 fetch above.
                    step2_task_ids = [
                        t.id
                        for t in orphaned_tasks
                        if t.vm_instance_id and _is_multiplexed_vm_id(t)
                    ]
                    step2_task_contact_lookup: dict[str, datetime] = {}
                    step2_task_tool_use_lookup: dict[str, datetime] = {}
                    if step2_task_ids:
                        step2_task_rows = (
                            await session.execute(
                                select(
                                    TaskContact.task_id,
                                    TaskContact.last_contact_at,
                                    TaskContact.last_tool_use_at,
                                ).where(TaskContact.task_id.in_(step2_task_ids))
                            )
                        ).all()
                        step2_task_contact_lookup = {
                            str(task_id): last_contact
                            for task_id, last_contact, _last_tool in step2_task_rows
                            if last_contact is not None
                        }
                        step2_task_tool_use_lookup = {
                            str(task_id): last_tool
                            for task_id, _last_contact, last_tool in step2_task_rows
                            if last_tool is not None
                        }
                    # Re-fetch the runner self-reports with the refreshed clock
                    # too: a runner heartbeat that landed during lock
                    # acquisition and now INCLUDES the task (e.g. the claim
                    # raced Step 1) must abort the reap, mirroring the
                    # vm_contact re-fetch above.
                    step2_runner_report_lookup = await _build_runner_report_lookup(
                        session, step2_vm_ids
                    )
                    verified = [
                        t
                        for t in orphaned_tasks
                        if _classify_orphan(
                            t,
                            now,
                            has_open_pr=(t.id in tasks_with_pr),
                            vm_contact_lookup=step2_contact_lookup,
                            vm_tool_use_lookup=step2_tool_use_lookup,
                            runner_report_lookup=step2_runner_report_lookup,
                            task_contact_lookup=step2_task_contact_lookup,
                            task_tool_use_lookup=step2_task_tool_use_lookup,
                        )
                    ]
                    orphaned_tasks = verified

                # ── PER-CYCLE ORPHAN CAP + PRIORITY ORDERING ──────────────
                # Each orphan below costs serial GitHub/VM I/O (~30-120s);
                # a large backlog can blow past REAPER_HANG_SECONDS even with
                # intra-cycle heartbeats keeping the watchdog at bay (a wedged
                # cycle still starves the *next* sweep). Cap per-cycle work and
                # process the most time-sensitive orphans first so the cap never
                # strands a rate-limited/spot orphan behind a long tail. Sort
                # MUST run here — before the ``expire_all`` below — because the
                # priority key reads vm_spot/context/latest_status_message off
                # the live ORM objects (the Step-2 lock SELECT loads full rows;
                # after expire_all those attrs would trigger MissingGreenlet).
                # Deferred orphans re-qualify automatically next cycle via the
                # status-based Step-1 scan, so no separate deferral queue.
                if orphaned_tasks:
                    _orphan_cap = int(
                        _runtime_config.get(
                            "reaper_max_orphans_per_cycle",
                            _REAPER_MAX_ORPHANS_PER_CYCLE_DEFAULT,
                        )
                    )
                    orphaned_tasks, _deferred_orphans = _select_orphans_for_cycle(
                        orphaned_tasks, _orphan_cap
                    )
                    if _deferred_orphans:
                        logger.warning(
                            "[REAPER] Orphan backlog %d exceeds per-cycle cap %d "
                            "— processing top %d by priority, deferring %d to next "
                            "cycle (they re-qualify via the Step-1 status scan).",
                            len(orphaned_tasks) + _deferred_orphans,
                            _orphan_cap,
                            len(orphaned_tasks),
                            _deferred_orphans,
                        )

                # ── RELEASE MULTI-ROW FOR UPDATE BEFORE EXTERNAL I/O LOOP ──
                # Production 2026-05-04 cascade shape: the orphan-loop body
                # below makes 4 external API calls per task — get_vm_status,
                # disable_account_model (with retry+sleep), _capture_serial_for_vm,
                # get_pull_request — each up to 30s, with combined per-task
                # worst-case 30-120s. The multi-row FOR UPDATE acquired at
                # lock_stmt/force_stmt above was previously held until the
                # batch commit at the end of the for loop, blocking heartbeats
                # on EVERY orphan row across the entire batch and tripping
                # the 60s idle_in_transaction_session_timeout under load.
                # Release it now: classification was already done against the
                # ``verified`` list, and each per-task iteration below does its
                # own release-around-IO + skip_locked re-acquire dance for the
                # mutation phase. expire_all() ensures subsequent reads come
                # from fresh DB state.
                #
                # CRITICAL: snapshot IDs BEFORE expire_all. After expire_all,
                # every attribute on every instance is expired — including
                # ``task.id``. Accessing ``task.id`` then triggers a sync
                # attribute __get__ that drives a lazy refresh; on AsyncSession
                # the underlying pool ping uses ``await_()`` which raises
                # MissingGreenlet because the sync attribute access is not
                # inside the AsyncSession's greenlet bridge. Result: the
                # orphan loop crashes mid-iteration on the very first task.
                # Iterate by id instead so no expired-attribute access happens.
                _orphaned_task_ids: list[str] = []
                _orphaned_project_ids: list[uuid.UUID] = []
                if orphaned_tasks:
                    _orphaned_task_ids = [t.id for t in orphaned_tasks]
                    _orphaned_project_ids = list(
                        {t.project_id for t in orphaned_tasks}
                    )
                    await session.commit()
                    session.expire_all()

                # Batch-load all projects referenced by orphaned tasks to
                # avoid N+1 SELECTs in the orphan-processing loop below (each
                # of the 3 sites that read task.project_id previously issued a
                # separate await session.get(Project, ...) query).
                _projects_batch: dict[uuid.UUID, Project] = {}
                if _orphaned_project_ids:
                    _proj_stmt = select(Project).where(
                        Project.id.in_(_orphaned_project_ids)
                    )
                    _proj_result = await session.execute(_proj_stmt)
                    _projects_batch = {
                        _p.id: _p for _p in _proj_result.scalars().all()
                    }

                # Collect old VM IDs for deletion after commit (can't set attrs on SQLModel)
                _reaper_old_vms: Dict[str, str] = {}
                # Audit 2026-04-25 A8: defer parent re-queue to AFTER session.commit()
                # so the orphan-row FOR UPDATE lock is released first. The previous
                # version called _requeue_parent_task (which clones children + remaps
                # deps + does its own queries) WHILE holding force-locked rows, which
                # starved agent heartbeats with 429s and triggered cascading false
                # orphan detection on the next reaper cycle.
                _deferred_parent_requeues: list[Task] = []
                # Gates the post-commit loops (parent-requeue / VM-delete / rollup).
                # Orphans now commit PER ROW inside the loop, so a single bad row no
                # longer rolls back the whole batch; this flag therefore stays True
                # in the normal path. It is retained as the post-loop gate so a
                # future failure mode that must skip the post-commit loops has a
                # single switch, and so the post-loop trailing-commit failure path is
                # explicit. The post-commit loops re-fetch fresh per-row state and are
                # FAILED-gated, so deferred / uncommitted rows self-skip there.
                _orphan_commit_ok = True

                # ── PER-CYCLE ORPHAN-LOOP WALL-TIME BUDGET ────────────────
                # The count cap (_select_orphans_for_cycle) bounds how MANY orphans
                # we take, but each orphan's serial GitHub/VM I/O is variable, so a
                # cap-sized batch of slow orphans can still run this loop far past
                # the watchdog window — starving the NEXT sweep even though the
                # intra-cycle heartbeat keeps THIS cycle from being killed. Bound the
                # wall time too: once the loop has spent its budget, stop taking new
                # orphans and let the remainder re-qualify next cycle via the Step-1
                # status scan. The orphan in flight always finishes (the check runs
                # BEFORE each iteration, never mid-reap). A non-positive budget means
                # "no time budget" (fail-safe if misconfigured). monotonic() is used
                # so the deadline is immune to wall-clock jumps.
                _orphan_loop_time_budget = float(
                    _runtime_config.get(
                        "reaper_orphan_loop_time_budget_seconds",
                        _REAPER_ORPHAN_LOOP_TIME_BUDGET_DEFAULT,
                    )
                )
                _orphan_loop_started_monotonic = time.monotonic()
                _orphan_loop_processed = 0
                _orphan_loop_time_deferred = 0

                # Batch-fetch all children for all orphaned task IDs (N+1 guard).
                # Build a parent_task_id → [Task] mapping so the per-iteration loop
                # below never issues an individual SELECT per research/plan parent.
                _children_by_parent: dict[int, list[Task]] = defaultdict(list)
                if _orphaned_task_ids:
                    _all_children = (
                        (
                            await session.execute(
                                select(Task).where(
                                    Task.parent_task_id.in_(_orphaned_task_ids)
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for _child in _all_children:
                        _children_by_parent[_child.parent_task_id].append(_child)

                # Batch-fetch all orphan tasks before the loop to
                # avoid N+1 session.get calls.  After a per-row commit
                # (line ~2209) all cached instances are expired — fall
                # back to session.get for the affected row.
                _orphan_task_map: dict[uuid.UUID, Task] = {}
                if _orphaned_task_ids:
                    _orphan_rows = (
                        (
                            await session.execute(
                                select(Task).where(Task.id.in_(_orphaned_task_ids))
                            )
                        )
                        .scalars()
                        .all()
                    )
                    _orphan_task_map = {t.id: t for t in _orphan_rows}

                for _orphan_loop_idx, task_id in enumerate(_orphaned_task_ids):
                    # Stop taking NEW orphans once the wall-time budget is spent.
                    # The remainder re-qualify next cycle via the Step-1 scan, so no
                    # work is lost — only deferred. Skipped here BEFORE any I/O so an
                    # in-flight reap is never interrupted.
                    if (
                        _orphan_loop_time_budget > 0
                        and _orphan_loop_processed > 0
                        and (time.monotonic() - _orphan_loop_started_monotonic)
                        >= _orphan_loop_time_budget
                    ):
                        _orphan_loop_time_deferred = (
                            len(_orphaned_task_ids) - _orphan_loop_idx
                        )
                        logger.warning(
                            "[REAPER] Orphan-loop wall-time budget %.0fs reached "
                            "after %d orphan(s) — deferring %d remaining to next "
                            "cycle (they re-qualify via the Step-1 status scan).",
                            _orphan_loop_time_budget,
                            _orphan_loop_processed,
                            _orphan_loop_time_deferred,
                        )
                        break
                    # Heartbeat per orphan: this loop is the dominant per-cycle
                    # cost (serial GitHub/VM I/O ~30-120s each), so a backlog can
                    # exceed REAPER_HANG_SECONDS. Marking progress here keeps the
                    # watchdog from killing a slow-but-progressing reap.
                    _mark_reaper_progress()
                    _orphan_loop_processed += 1
                    # Snapshot the deferred-requeue list length BEFORE this row's
                    # mutations so a per-row rollback can discard only THIS row's
                    # appended parent (truncating by index — never reading p.id on a
                    # post-rollback-expired ORM object, which would MissingGreenlet).
                    _dq_len_at_row_start = len(_deferred_parent_requeues)
                    # Set when this orphan's recovery records a real integration
                    # landing (out-of-band merge), so the post-(per-row)-commit
                    # direction-outcome refresh runs for it. Re-fetched in a fresh
                    # session below — the per-row commit can expire ``task``.
                    _orphan_merge_refresh_needed = False
                    try:
                        # Re-validate status before acting — task may have completed/been cancelled
                        # between the initial query and now.
                        # Use batch-cached task; re-fetch only when the cached
                        # instance was expired by a prior iteration's commit/rollback.
                        fresh_task = _orphan_task_map.get(task_id)
                        if fresh_task is None or inspect(fresh_task).expired:
                            fresh_task = await session.get(Task, task_id)
                        if fresh_task is None or fresh_task.status not in (
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                        ):
                            logger.info(
                                f"[REAPER] Skipping task {task_id} — status transitioned to {fresh_task.status if fresh_task else 'gone'} before reap"
                            )
                            # Task resolved itself — drop any prior crash streak so the
                            # dict doesn't accumulate stale entries (mirrors the
                            # _reaper_skip_counts prune ~940-942).
                            _orphan_reap_crash_streaks.pop(str(task_id), None)
                            continue
                        task = fresh_task  # Use fresh data for all subsequent reads and writes

                        # ── C2: per-orphan idempotency-key pre-query guard ──
                        # The reap below can flip this task to QUEUED (rate-limit /
                        # retry requeue) or EVALUATING (reaped-with-open-PR handoff),
                        # both of which enter the partial unique index
                        # ux_task_active_idempotency_key. If a peer already holds
                        # this key in an active status, the BATCH commit at the end
                        # of this loop raises IntegrityError and — because the commit
                        # covers EVERY orphan's mutations — rolls back the entire
                        # batch's recovery work, not just this one collision. Pre-query
                        # the collision here and skip this orphan before mutating, so
                        # the collision is prevented per-task rather than handled by an
                        # all-batch rollback. The active duplicate already represents
                        # the work, so reaping (which would requeue a redundant copy)
                        # is moot. Mirrors the proven pre-query guard in
                        # reaper_infra_recovery.py (~263-338).
                        if task.idempotency_key:
                            _dup_active = await session.scalar(
                                select(Task.id).where(
                                    Task.idempotency_key == task.idempotency_key,
                                    Task.id != task.id,
                                    # Must mirror the ux_task_active_idempotency_key
                                    # partial-index predicate EXACTLY (migration 0199:
                                    # QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING/BLOCKED/
                                    # PARKED). A status the index counts but this guard
                                    # omits lets a colliding requeue slip past → the
                                    # unique-violation IntegrityError on commit that
                                    # rolls back the WHOLE reaper batch — exactly what
                                    # this pre-query exists to prevent.
                                    Task.status.in_(
                                        [
                                            TaskStatus.QUEUED,
                                            TaskStatus.PROVISIONING,
                                            TaskStatus.IN_PROGRESS,
                                            TaskStatus.EVALUATING,
                                            TaskStatus.BLOCKED,
                                            TaskStatus.PARKED,
                                        ]
                                    ),
                                )
                            )
                            if _dup_active is not None:
                                logger.info(
                                    "[REAPER] Skipping orphan reap of %s — "
                                    "idempotency_key already held by active task %s; "
                                    "the active duplicate represents the work, reap is "
                                    "moot (re-qualifies next cycle if it doesn't)",
                                    task_id,
                                    _dup_active,
                                )
                                # VM-GC: the VM-collection step (~1248) lives in the
                                # reap body AFTER this guard, so a bare ``continue``
                                # would strand this orphan's dead VM until the active
                                # duplicate frees the idempotency key. The orphan is a
                                # dead/stuck task (its agent died or stalled) and the
                                # active duplicate owns a SEPARATE VM, so this VM is
                                # collectible now. Schedule it for deletion in the
                                # post-commit VM sweep (~2493). _ensure_vm_deleted is
                                # idempotent, so re-collecting on a later cycle (if the
                                # orphan re-qualifies before its row is cleaned up) is
                                # harmless.
                                if task.vm_instance_id and not is_cloud_run_task(
                                    task.vm_machine_type
                                ):
                                    _reaper_old_vms[str(task.id)] = task.vm_instance_id
                                _orphan_reap_crash_streaks.pop(str(task_id), None)
                                continue

                        # Determine why the reaper flagged this task. The classification slices
                        # into three cases based on status + which clock expired first.
                        if (
                            fresh_task.status == TaskStatus.PROVISIONING
                            and fresh_task.started_at
                            and fresh_task.started_at < fifteen_minutes_ago
                        ):
                            age = int((now - fresh_task.started_at).total_seconds())
                            reap_reason = (
                                f"Absolute provision timeout — {age}s since dispatch"
                            )
                            reap_reason_code = "absolute_timeout"
                        elif (
                            fresh_task.status == TaskStatus.PROVISIONING
                            and fresh_task.last_heartbeat is None
                        ):
                            # VM created, agent never contacted us. Use vm_provisioned_at if available,
                            # else fall back to started_at for the age display.
                            clock = (
                                fresh_task.vm_provisioned_at or fresh_task.started_at
                            )
                            age = int((now - clock).total_seconds()) if clock else 0
                            reap_reason = f"Silent pre-claim death — {age}s since VM provisioned, no heartbeat"
                            reap_reason_code = "provision_timeout_pre_claim"
                        elif (
                            fresh_task.last_heartbeat
                            and fresh_task.last_tool_use_at is not None
                            and fresh_task.last_tool_use_at
                            < _progress_stale_cutoff(fresh_task, now)
                            and (
                                fresh_task.started_at is None
                                or fresh_task.started_at
                                < _progress_stale_cutoff(fresh_task, now)
                            )
                            and fresh_task.last_heartbeat
                            >= _heartbeat_stale_cutoff(fresh_task, now)
                        ):
                            # Stalled-but-heartbeating: process alive, no tool progress.
                            # The started_at clause guards against cross-attempt
                            # stale-tool reaps — last_tool_use_at carries forward
                            # across retries; without this, a fresh attempt can be
                            # killed within seconds of starting (8c8cf973 incident).
                            age = int(
                                (now - fresh_task.last_tool_use_at).total_seconds()
                            )
                            reap_reason = (
                                f"Agent stalled — heartbeat fresh but no tool calls for {age}s "
                                f"(last_tool_use_at={fresh_task.last_tool_use_at.isoformat()})"
                            )
                            reap_reason_code = "agent_stalled"
                        elif (
                            fresh_task.last_heartbeat
                            and fresh_task.last_tool_use_at is None
                            and fresh_task.started_at
                            and fresh_task.started_at
                            < _progress_stale_cutoff(fresh_task, now)
                            and fresh_task.last_heartbeat
                            >= _heartbeat_stale_cutoff(fresh_task, now)
                        ):
                            # Stalled-but-heartbeating with zero tool calls ever
                            # (Codex text-mode pattern). Same classification as
                            # last_tool_use_at-stale; uses started_at as the clock.
                            age = int((now - fresh_task.started_at).total_seconds())
                            reap_reason = (
                                f"Agent stalled — heartbeat fresh but no tool call has occurred in {age}s "
                                f"since started ({fresh_task.started_at.isoformat()})"
                            )
                            reap_reason_code = "agent_stalled"
                        elif fresh_task.last_heartbeat:
                            age = int((now - fresh_task.last_heartbeat).total_seconds())
                            reap_reason = f"No heartbeat for {age}s (last: {fresh_task.last_heartbeat.isoformat()})"
                            # Enrich with tool trace tail for post-mortem analysis
                            tool_trace_tail = _extract_tool_trace_tail(fresh_task)
                            if tool_trace_tail:
                                reap_reason += f" | last tools: {tool_trace_tail}"
                            reap_reason_code = "heartbeat_timeout_post_claim"
                        elif fresh_task.started_at:
                            # IN_PROGRESS without a heartbeat (defensive branch — should be rare).
                            age = int((now - fresh_task.started_at).total_seconds())
                            reap_reason = f"IN_PROGRESS without heartbeat for {age}s since dispatch"
                            # Enrich with tool trace tail for post-mortem analysis
                            tool_trace_tail = _extract_tool_trace_tail(fresh_task)
                            if tool_trace_tail:
                                reap_reason += f" | last tools: {tool_trace_tail}"
                            reap_reason_code = "heartbeat_timeout_post_claim"
                        else:
                            age = int((now - fresh_task.created_at).total_seconds())
                            reap_reason = (
                                f"Never started — {age}s since creation, no heartbeat"
                            )
                            reap_reason_code = "never_started"

                        task_type = (fresh_task.first_payload).get("type", "unknown")
                        logger.info(
                            "reaper_task_reaped",
                            extra={
                                "task_id": str(task.id),
                                "task_type": task_type,
                                "reap_reason": reap_reason_code,
                                "vm_instance_id": fresh_task.vm_instance_id,
                            },
                        )

                        record_pipeline_event(
                            reap_reason_code,
                            task_id=str(task.id),
                            vm_id=fresh_task.vm_instance_id,
                            zone=(task.context or {}).get("provision_zone"),
                        )

                        # Capture old VM ID before any overwrite — deletion happens after commit
                        if fresh_task.vm_instance_id and not is_cloud_run_task(
                            fresh_task.vm_machine_type
                        ):
                            _reaper_old_vms[str(task.id)] = fresh_task.vm_instance_id

                        # Read-only inputs to the I/O block + branch decisions below.
                        # No mutations happen until AFTER the relock further down.
                        acct_id = get_task_llm_account_id(fresh_task)
                        _slot_already_released = bool(
                            (fresh_task.context or {}).get("_slot_released")
                        )

                        # Separate provision failures from agent failures for retry budgeting
                        is_provision_failure = (
                            fresh_task.status == TaskStatus.PROVISIONING
                        )
                        provision_failures = (task.context or {}).get(
                            "provision_failures", 0
                        )
                        max_provision_retries = _runtime_config["max_provision_retries"]
                        can_retry = False

                        # Rate-limited / dead-auth tasks: requeue without burning retry
                        # budget. Delegate to classify_failure so the reaper path applies
                        # the same Anthropic-529/GitHub-429 guards as the agent-status
                        # path, and matches real Claude rate-limit strings only.
                        combined_logs = (
                            ((task.context or {}).get("last_error") or "")
                            + "\n"
                            + (
                                (task.error_logs or "")
                                if hasattr(task, "error_logs")
                                else ""
                            )
                        )
                        _failure_class_for_reaper = classify_failure(
                            task,
                            logs=combined_logs,
                            at_time=task.last_heartbeat,
                        )
                        is_rate_limited = _failure_class_for_reaper == "RATE_LIMIT"
                        is_dead_auth = _failure_class_for_reaper == DEAD_AUTH

                        # ── EXTERNAL I/O BLOCK (no row lock held) ─────────────────
                        # Production 2026-05-04: this block previously ran with the
                        # multi-row FOR UPDATE on this task held, blocking heartbeats
                        # for 30-120s per task across get_vm_status (GCP) +
                        # disable_account_model (with retry+sleep) + _capture_serial_for_vm
                        # (GCP serial console) + get_pull_request (GitHub). The
                        # multi-row lock is released above, so this whole block runs
                        # unlocked. We re-acquire FOR UPDATE briefly after this block
                        # to apply the resulting mutations.
                        # ──────────────────────────────────────────────────────────
                        _io = await _run_orphan_io(
                            task=task,
                            fresh_task=fresh_task,
                            reap_reason_code=reap_reason_code,
                            is_provision_failure=is_provision_failure,
                            is_rate_limited=is_rate_limited,
                            is_dead_auth=is_dead_auth,
                            session=session,
                            runtime_config=_runtime_config,
                        )
                        pre_io_status = _io.pre_io_status
                        pre_io_last_hb = _io.pre_io_last_hb
                        is_spot_preemption = _io.is_spot_preemption
                        is_rate_limited = _io.is_rate_limited
                        rl_acct_id = _io.rl_acct_id
                        rl_model = _io.rl_model
                        da_acct_id = _io.da_acct_id
                        io_serial_log = _io.io_serial_log
                        io_prov_zone = _io.io_prov_zone
                        io_provision_err = _io.io_provision_err
                        _has_pr_artifact = _io.has_pr_artifact
                        _pr_num = _io.pr_num
                        io_gh_pr_state = _io.gh_pr_state
                        io_gh_pr_merged = _io.gh_pr_merged
                        io_gh_pr_base_branch = _io.gh_pr_base_branch

                        # ── RE-ACQUIRE FOR UPDATE FOR MUTATIONS ──────────────────
                        # All external I/O above is done. Re-acquire the FOR UPDATE
                        # lock briefly to apply state mutations atomically. If the
                        # row is held by a peer (e.g. concurrent reaper, finalize
                        # path) or status changed during the unlocked window,
                        # _relock_orphan_task returns None and we skip this iter.
                        # Heartbeat false-reap fix (option B): also pass the
                        # heartbeat-stale cutoff so a vmcontact that landed during
                        # the (potentially 30-120s) unlocked external-I/O window
                        # aborts the reap even though last_heartbeat is unchanged
                        # (the agent's contended last_heartbeat write was skipped
                        # by the rollup's task-row lock). Computed at relock time
                        # with the current clock + the same scope/effort/PR
                        # awareness _classify_orphan used.
                        #
                        # Fix 3a: the vm_contact override is ONLY valid for
                        # heartbeat-stale reap codes. A PROGRESS-stall reap
                        # (``agent_stalled``: fresh heartbeat but no tool calls)
                        # is a different signal — a hung-but-heartbeating agent
                        # ALWAYS has a fresh vm_contact (it's still calling the
                        # heartbeat endpoint), so passing the override here would
                        # defeat genuine stall detection entirely. Mirror
                        # _classify_orphan, which deliberately withholds the
                        # override on its progress-stall branches. Pass None for
                        # ``agent_stalled``; the lock-starved-but-progressing case
                        # is handled separately off-row in _classify_orphan via
                        # vmcontact.last_tool_use_at (Fix 3b).
                        _HEARTBEAT_STALE_REAP_CODES = frozenset(
                            {
                                "heartbeat_timeout_post_claim",
                                "provision_timeout_pre_claim",
                                "absolute_timeout",
                                "never_started",
                            }
                        )
                        _relock_liveness_cutoff = (
                            _heartbeat_stale_cutoff(
                                fresh_task,
                                _utcnow(),
                                has_open_pr=_has_pr_artifact,
                            )
                            if reap_reason_code in _HEARTBEAT_STALE_REAP_CODES
                            else None
                        )
                        # Shared-VmContact zombie mask at the relock stage: every
                        # task on a fleet runner shares vm_instance_id
                        # 'runner:<id>' and therefore ONE vmcontact row, so a live
                        # sibling's heartbeat keeps last_contact_at fresh and the
                        # relock's contact check un-reaps a dead task on EVERY
                        # cycle — silently undoing the Step-1/Step-2
                        # classification that #14413 taught to see through the
                        # mask. Give the relock the same runner self-report
                        # override. Re-fetched HERE (not reused from Step 2)
                        # because the unlocked external-I/O window above can span
                        # 30-120s: the report's own freshness is gated against the
                        # liveness cutoff, so a Step-2 snapshot could read as
                        # "stale runner" purely from I/O latency and fail-safe
                        # back into the mask. Cheap: _build_runner_report_lookup
                        # issues NO query for a non-'runner:' vm id.
                        _relock_runner_report_lookup: dict = {}
                        if (
                            _relock_liveness_cutoff is not None
                            and fresh_task.vm_instance_id
                        ):
                            try:
                                _relock_runner_report_lookup = (
                                    await _build_runner_report_lookup(
                                        session, {fresh_task.vm_instance_id}
                                    )
                                )
                            except Exception as _rr_exc:  # noqa: BLE001
                                # The pre-try `{}` binding still stands (the
                                # assignment above only binds on success), so the
                                # relock falls back to vm_contact suppression.
                                logger.warning(
                                    "[REAPER] runner self-report fetch failed for "
                                    "task %s vm=%s (non-fatal, relock falls back "
                                    "to vm_contact suppression): %s",
                                    task.id,
                                    fresh_task.vm_instance_id,
                                    _rr_exc,
                                )
                        relocked = await _relock_orphan_task(
                            session,
                            task.id,
                            fresh_task,
                            pre_io_status,
                            pre_io_last_hb,
                            vm_liveness_cutoff=_relock_liveness_cutoff,
                            runner_report_lookup=_relock_runner_report_lookup,
                        )
                        if relocked is None:
                            # Peer is finalizing this task or agent revived; skip safely.
                            # Don't add to _reaper_old_vms-driven cleanup either — let
                            # the peer (or next reaper cycle) handle it.
                            _reaper_old_vms.pop(str(task.id), None)
                            continue
                        task = relocked
                        fresh_task = relocked

                        # Durable reason trail: the reap is now committed-to (the
                        # relock confirmed no peer/revival), so record WHY before
                        # any branch below (retry, requeue, PR handoff, terminal
                        # fail) overwrites the mutable status message. Rides the
                        # same commit as the reap mutation itself.
                        _record_reap_reason_history(
                            session,
                            task.id,
                            reap_reason=reap_reason,
                            reap_reason_code=reap_reason_code,
                            vm_instance_id=fresh_task.vm_instance_id,
                        )

                        # Apply the in-memory account-slot release marker. Re-check
                        # the flag from the FRESH (post-relock) context to avoid a
                        # double-release on the rare case where a peer reaper iteration
                        # set the flag between our pre-IO capture and the relock (would
                        # require last_heartbeat unchanged, since the relock validates
                        # that). The release op itself is also idempotent inside the
                        # in-memory pool, so this is belt-and-suspenders.
                        _slot_now_released = bool(
                            (task.context or {}).get("_slot_released")
                        )
                        if (
                            acct_id
                            and not _slot_already_released
                            and not _slot_now_released
                        ):
                            await release_max_plan_account(
                                acct_id,
                                project_id=str(task.project_id) if task.project_id else None,
                            )
                            task.context = {
                                **(task.context or {}),
                                "_slot_released": True,
                            }

                        if is_spot_preemption:
                            # Skip attempt_count++ and provision_failures++ — preemption
                            # isn't a code defect or account problem. Requeue cleanly.
                            logger.info(
                                f"[REAPER] Spot preemption — requeuing task {task.id} without burning retry budget"
                            )
                            new_ctx = dict(task.context or {})
                            spot_count = (new_ctx.get("spot_preemption_count") or 0) + 1
                            new_ctx["spot_preemption_count"] = spot_count
                            if is_provision_failure:
                                # Undo the provision_failures increment from the pre-claim path
                                provision_failures -= 1
                                new_ctx["provision_failures"] = provision_failures
                            task.context = new_ctx
                            can_retry = True
                            record_pipeline_event(
                                "spot_preemption",
                                task_id=str(task.id),
                                vm_id=fresh_task.vm_instance_id,
                                zone=(task.context or {}).get("provision_zone")
                                or get_vm_zone(fresh_task.vm_instance_id),
                            )
                        elif is_dead_auth:
                            # disable_account_for_auth (permanent hard-disable) was already
                            # invoked in the unlocked I/O block above; da_acct_id captured
                            # there. Requeue once onto a different account — no retry budget
                            # consumed (DEAD_AUTH is an INFRA class).
                            bank_task_current_attempt(task, stopped_at=_utcnow())
                            task.status = TaskStatus.QUEUED
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                            task.last_heartbeat = None
                            task.started_at = None
                            task.latest_status_message = (
                                "Dead OAuth session — account hard-disabled; "
                                "requeued onto different account"
                            )
                            da_ctx = dict(task.context or {})
                            if da_acct_id:
                                blacklisted = list(
                                    da_ctx.get("blacklisted_accounts") or []
                                )
                                if da_acct_id not in blacklisted:
                                    blacklisted.append(da_acct_id)
                                    record_pipeline_event(
                                        "account_blacklisted",
                                        task_id=str(task.id),
                                        vm_id=fresh_task.vm_instance_id,
                                        zone=(task.context or {}).get("provision_zone"),
                                        account_id=da_acct_id,
                                    )
                                da_ctx["blacklisted_accounts"] = blacklisted
                            _copy_analysis_artifacts_to_previous(da_ctx)
                            da_ctx = clear_task_llm_runtime_context(da_ctx)
                            da_ctx.pop("_slot_released", None)
                            da_ctx.pop("provision_failures", None)
                            # Clear the stale DEAD_AUTH verdict and matched pattern so
                            # the next attempt is classified fresh for its own provider.
                            # Without this, the short-circuit in _classify_failure_class
                            # (task.context["failure_class"]) fires on the new attempt and
                            # disables whatever account that attempt runs on — including
                            # accounts from a different provider — with the stale pattern
                            # as the reason. (Incident 2026-06-14: Codex dead-auth
                            # disabled all Anthropic accounts via this leak.)
                            da_ctx.pop("failure_class", None)
                            da_ctx.pop("dead_auth_matched_pattern", None)
                            task.context = da_ctx
                            mark_task_for_fresh_execution_attempt(task)
                            # Keep the promoted column in sync (D2 over-fetch fix).
                            task.llm_account_id = None
                            clear_retry_after(task)
                            session.add(task)
                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {"task_id": str(task.id), "status": TaskStatus.QUEUED},
                            )
                            continue
                        elif is_rate_limited:
                            # disable_account_model was already invoked (with retry+sleep)
                            # in the unlocked I/O block above; rl_acct_id and rl_model
                            # captured there. Apply the resulting state mutations here.
                            bank_task_current_attempt(task, stopped_at=_utcnow())
                            task.status = TaskStatus.QUEUED
                            task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                            task.last_heartbeat = None
                            task.started_at = None
                            task.latest_status_message = "Rate limited — requeued with exhausted account blacklisted"
                            # Blacklist the exhausted account; let the orchestrator pick another
                            # from the pool on the next cycle. If all accounts are rate-limited,
                            # the orchestrator handles that case by skipping the task and retrying
                            # next cycle — no need for a task-level retry_after delay.
                            rl_ctx = dict(task.context or {})
                            if rl_acct_id:
                                blacklisted = list(
                                    rl_ctx.get("blacklisted_accounts") or []
                                )
                                if rl_acct_id not in blacklisted:
                                    blacklisted.append(rl_acct_id)
                                    record_pipeline_event(
                                        "account_blacklisted",
                                        task_id=str(task.id),
                                        vm_id=fresh_task.vm_instance_id,
                                        zone=(task.context or {}).get("provision_zone"),
                                        account_id=rl_acct_id,
                                    )
                                rl_ctx["blacklisted_accounts"] = blacklisted
                            _copy_analysis_artifacts_to_previous(rl_ctx)
                            rl_ctx = clear_task_llm_runtime_context(rl_ctx)
                            rl_ctx.pop("_slot_released", None)
                            # Rate-limit is an account-level issue, not a provision failure —
                            # don't carry provision budget into the next dispatch.
                            rl_ctx.pop("provision_failures", None)
                            task.context = rl_ctx
                            mark_task_for_fresh_execution_attempt(task)
                            # Keep the promoted column in sync (D2 over-fetch fix).
                            task.llm_account_id = None
                            clear_retry_after(task)
                            persist_authoritative_verdict(task, RATE_LIMIT)
                            session.add(task)
                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {"task_id": str(task.id), "status": TaskStatus.QUEUED},
                            )
                            continue

                        # Track per-class budget/failure-class for non-provision retries.
                        # Initialized here so both retry and fail paths can reference them.
                        reap_failure_class: str | None = None
                        class_budget: int = _runtime_config["max_retries"]

                        if is_provision_failure:
                            provision_failures += 1
                            # Reuse zone + provision_err + serial_log captured by the
                            # unlocked I/O block above. Read-only locals fall back to
                            # the same fallback chain when the I/O block didn't fire
                            # (e.g. is_provision_failure was True but the row was rate-
                            # limited or spot-preempted, in which case those branches
                            # already returned via `continue` and we never reach here).
                            prov_zone = io_prov_zone
                            if prov_zone is None and fresh_task.vm_instance_id:
                                cached = get_vm_zone(fresh_task.vm_instance_id)
                                if cached and cached != "unknown":
                                    prov_zone = cached
                            if not prov_zone:
                                prov_zone = (task.context or {}).get("provision_zone")
                            failed_acct = get_task_llm_account_id(task)

                            # Classify: infra-wide (all zones/quota exhausted) vs task-specific
                            provision_err = (
                                io_provision_err
                                if io_provision_err is not None
                                else get_provision_error(
                                    fresh_task.vm_instance_id or ""
                                )
                            )
                            err_lower = (
                                (provision_err or "")
                                + " "
                                + (task.latest_status_message or "")
                            ).lower()
                            is_infra = any(
                                p in err_lower for p in INFRA_PROVISION_PATTERNS
                            ) or (not prov_zone and not any_zone_available())

                            # Serial log was captured by the unlocked I/O block above.
                            # We intentionally do NOT re-capture here on failure: doing so
                            # would call _capture_serial_for_vm (5-30s GCP API) while holding
                            # the FOR UPDATE lock, re-introducing the production-2026-05-04
                            # cascade. If the I/O block couldn't capture (zone unresolvable
                            # or transient GCP error), the next reaper cycle will retry from
                            # the unlocked I/O path.
                            serial_log = io_serial_log
                            if prov_zone:
                                # VM was created (GCP API succeeded) but agent never claimed.

                                # Blacklist the account so retry uses a different one.
                                # We maintain two lists:
                                #   - blacklisted_accounts: pruned when model cooldown expires
                                #     (correct for rate-limit blacklists)
                                #   - provision_failed_accounts: sticky for this task's lifetime
                                #     (silent pre-claim death isn't model-cooldown-driven, so
                                #     pruning by _account_available_for_model wrongly reselects
                                #     the broken account — observed on task 812fdbb9 retrying
                                #     account 017d51cb 3x in a row in us-central1-a).
                                blacklisted = list(
                                    (task.context or {}).get("blacklisted_accounts")
                                    or []
                                )
                                sticky = list(
                                    (task.context or {}).get(
                                        "provision_failed_accounts"
                                    )
                                    or []
                                )
                                if failed_acct and failed_acct not in blacklisted:
                                    blacklisted.append(failed_acct)
                                    logger.warning(
                                        f"[REAPER] Agent startup failure on {prov_zone} acct={failed_acct[:8]} "
                                        f"— blacklisting account for task {task.id}"
                                    )
                                    record_pipeline_event(
                                        "account_blacklisted",
                                        task_id=str(task.id),
                                        vm_id=fresh_task.vm_instance_id,
                                        zone=prov_zone,
                                        account_id=failed_acct,
                                    )
                                if failed_acct and failed_acct not in sticky:
                                    sticky.append(failed_acct)
                                new_ctx = {
                                    **(task.context or {}),
                                    "blacklisted_accounts": blacklisted or None,
                                    "provision_failed_accounts": sticky or None,
                                }
                                if serial_log:
                                    new_ctx["last_serial_log"] = serial_log
                                record_zone_failure(
                                    prov_zone, reason="AGENT_STARTUP_FAILURE"
                                )
                                # Feed startup failure into per-account health/breaker.
                                # Only when not is_infra (global outage already handled
                                # upstream); multi-zone check is inside the function.
                                if not is_infra and failed_acct:
                                    from app.max_plan_pool import (
                                        record_account_startup_failure,
                                    )

                                    await record_account_startup_failure(
                                        failed_acct, prov_zone
                                    )
                            else:
                                # No provision_zone — VM may never have been created, or the
                                # fallback chain couldn't resolve one.
                                logger.warning(
                                    f"[REAPER] Skipping serial capture for task {task.id}: "
                                    f"zone unknown (vm_id={fresh_task.vm_instance_id})"
                                )
                                new_ctx = dict(task.context or {})

                            if is_infra:
                                # Infrastructure-wide failure — don't burn task retry budget
                                provision_failures -= 1  # Undo the increment above
                                infra_count = (
                                    new_ctx.get("provision_failures_infra", 0) + 1
                                )
                                new_ctx["provision_failures_infra"] = infra_count
                                new_ctx["provision_failures"] = provision_failures
                                new_ctx["_infra_wait"] = True
                                task.context = new_ctx
                                can_retry = True
                                logger.warning(
                                    f"[REAPER] Task {task.id} infra-wide provision failure (#{infra_count}) — requeuing without burning retry budget"
                                )
                            else:
                                new_ctx["provision_failures"] = provision_failures
                                new_ctx.pop("_infra_wait", None)
                                task.context = new_ctx
                                can_retry = provision_failures < max_provision_retries
                                if not can_retry:
                                    logger.error(
                                        f"[REAPER] Task {task.id} provision failed {provision_failures} times — giving up"
                                    )
                        else:
                            # Classify failure to select per-class retry budget.
                            # Use agent-reported error logs as the primary signal. If the agent
                            # left no error context (pure heartbeat timeout), fall back to
                            # AGENT_TIMEOUT rather than the default CODE_ERROR — a silent death
                            # is not a code defect. Skip the override when an authoritative
                            # failure_class is already persisted in context: that verdict was
                            # written at the failure instant and must not be demoted to
                            # AGENT_TIMEOUT just because logs are empty in this reaper cycle.
                            _ctx_for_classification = task.context or {}
                            _ctx_persisted_verdict = _ctx_for_classification.get(
                                "failure_class"
                            )
                            reap_failure_class = classify_failure(
                                task, logs=combined_logs, at_time=task.last_heartbeat
                            )
                            if (
                                reap_failure_class == "CODE_ERROR"
                                and not combined_logs.strip()
                                and not _ctx_persisted_verdict
                            ):
                                reap_failure_class = "AGENT_TIMEOUT"
                            reap_failure_class = _normalize_reaper_retry_failure_class(
                                reap_failure_class, _ctx_for_classification
                            )
                            persist_authoritative_verdict(task, reap_failure_class)
                            task_scope = (task.context or {}).get("scope")
                            class_budget = resolve_retry_budget(
                                reap_failure_class, task_scope
                            )
                            # Detect EXTERNAL_API fast-path: transient 5xx where the dependency
                            # health check shows recovery. These failures skip the attempt_count
                            # increment below (mirrors the RATE_LIMIT fast-path) since the retry
                            # is near-instant and not indicative of a code defect. Applies whether
                            # or not the task already has a PR — a transient 502 after pushing
                            # work should not burn a retry attempt if the dependency recovered.
                            _err_blob = (
                                combined_logs + " " + (task.latest_status_message or "")
                            ).lower()
                            _transient_5xx = any(
                                p in _err_blob for p in _TRANSIENT_5XX_PATTERNS
                            )
                            _gh_consec = _external_health.get(
                                "consecutive_failures", {}
                            ).get("github_api", 0)
                            ext_api_fast_path = (
                                reap_failure_class == "EXTERNAL_API"
                                and _transient_5xx
                                and _gh_consec == 0
                            )
                            can_retry = task.attempt_count < class_budget

                        # Open-PR handoff: if this attempt already pushed a branch
                        # and opened a PR, retrying from scratch would collide with
                        # the existing branch (see agent/src/index.ts:706 stale-
                        # branch guard) and orphan the PR. Hand off to the review
                        # pipeline instead — review/fix iteration + the existing
                        # 6h EVALUATING hard timeout (services.py:807) + the 16-iter
                        # review-fix cap guarantee termination.
                        #
                        # PR state was already fetched in the unlocked I/O block above
                        # (io_gh_pr_state, io_gh_pr_merged) so the GitHub round-trip
                        # doesn't run with FOR UPDATE held. Reuse those values here.
                        _gh_pr_state = io_gh_pr_state
                        _gh_pr_merged = io_gh_pr_merged
                        _gh_pr_base_branch = io_gh_pr_base_branch
                        _pr_actually_open = (
                            _has_pr_artifact and _pr_num and _gh_pr_state == "open"
                        )

                        if (
                            _has_pr_artifact
                            and _pr_num
                            and _gh_pr_state == "closed"
                            and _gh_pr_merged
                        ):
                            # PR merged out-of-band: completion-processing didn't run
                            # (agent died before reporting). Mark the task COMPLETED
                            # so the rollup parent and downstream review/fix tasks
                            # see the merge.
                            logger.info(
                                f"[REAPER] Task {task.id} reaped mid-run; PR #{_pr_num} already merged — "
                                f"classifying landing"
                            )
                            _proj = _projects_batch.get(task.project_id)
                            if _proj is None or inspect(_proj).expired:
                                _proj = await session.get(Project, task.project_id)
                            from app.recovery_evaluating_helpers import (
                                _apply_rollup_merge_classification,
                            )

                            (
                                _record_pr_merged,
                                _terminal_rollup_merge,
                                _landed_branch,
                            ) = await _apply_rollup_merge_classification(
                                task,
                                _proj,
                                session,
                                pr_number=_pr_num,
                                merged_base_branch=_gh_pr_base_branch,
                                source="reaper.orphan_recovery",
                                msg_terminal=f"Reaped mid-run; PR #{_pr_num} was merged out-of-band",
                                msg_non_terminal=f"Reaped mid-run; PR #{_pr_num} merged into {{landed}}; awaiting downstream landing",
                                hotfix_lane=bool(
                                    (task.first_payload or {}).get("hotfix")
                                    or (task.context or {}).get("hotfix")
                                ),
                                non_merge_reason="merged_pr_non_integration_branch",
                            )
                            try:
                                from app.task_completion import (
                                    _finalize_migration_reservation_after_merge,
                                )

                                await _finalize_migration_reservation_after_merge(
                                    task,
                                    session,
                                    terminal_merge=_terminal_rollup_merge,
                                )
                            except Exception as _mr_exc:  # noqa: BLE001
                                logger.warning(
                                    "[MIGRATION] Failed to finalize reservation for "
                                    "reaped merged task %s: %r",
                                    task.id,
                                    _mr_exc,
                                )
                            # Defer the direction-outcome refresh until after the
                            # per-row commit persists this landing (a fresh
                            # session re-fetches the task — the reaper's per-row
                            # commit can expire ``task``, so reading its context
                            # here would risk a sync lazy-load / MissingGreenlet).
                            _orphan_merge_refresh_needed = True
                            session.add(task)
                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {"task_id": str(task.id), "status": task.status},
                            )
                        elif (
                            _has_pr_artifact
                            and _pr_num
                            and _gh_pr_state == "closed"
                            and not _gh_pr_merged
                        ):
                            # PR closed without merging (operator rejection / empty
                            # rollup short-circuit / review-pipeline rejection).
                            # Don't hand off to review — review can't act on a closed
                            # PR. Mark FAILED with the closure marker so Step 5's
                            # resurrection sweep skips it.
                            logger.info(
                                f"[REAPER] Task {task.id} reaped mid-run; PR #{_pr_num} closed without merge — "
                                f"marking FAILED"
                            )
                            task.status = TaskStatus.FAILED
                            if task.vm_provisioned_at:
                                task.vm_stopped_at = _utcnow()
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            task.latest_status_message = f"Reaped mid-run; PR #{_pr_num} was closed without merging"
                            task.context = {
                                **(task.context or {}),
                                "pr_closed_unmerged": True,
                            }
                            session.add(task)
                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {"task_id": str(task.id), "status": task.status},
                            )
                        elif _pr_actually_open:
                            # Provision failures can't publish a PR (agent never ran),
                            # so class_budget/reap_failure_class are only bound when
                            # the agent-error branch above ran. Guard the format args.
                            _display_class = (
                                "PROVISION_FAILURE"
                                if is_provision_failure
                                else locals().get("reap_failure_class", "UNKNOWN")
                            )
                            _display_budget = locals().get("class_budget", "?")
                            logger.info(
                                f"[REAPER] Task {task.id} reaped mid-run with open PR #{_pr_num}; "
                                f"handing off to review pipeline (was attempt {task.attempt_count}/{_display_budget})"
                            )
                            task.status = TaskStatus.EVALUATING
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            task.latest_status_message = (
                                f"Reaped mid-run with open PR #{_pr_num}; "
                                f"handed off to review pipeline ({_display_class})"
                            )
                            # C6 fix: write-ahead a recovery marker BEFORE the
                            # spawn. The EVALUATING transition is persisted by the
                            # batched orphan-reap commit regardless of whether the
                            # spawn below succeeds; previously a spawn failure (or
                            # a death between this commit and the spawn) left a
                            # childless EVALUATING parent that reaper Step 3 (needs
                            # a COMPLETED child) and Step 4 (skips childless
                            # parents) both ignore — stranded until the 6h/48h
                            # backstop. Setting `_pr_state_check_pending` here makes
                            # the services `_check_evaluating_timeouts` consumer
                            # re-check the PR and re-spawn the review. Cleared only
                            # on confirmed spawn success below. Mirrors the
                            # agent_status FAILED-with-open-PR handoff path.
                            _ho_ctx = dict(task.context or {})
                            _ho_ctx["_pr_state_check_pending"] = True
                            task.context = _ho_ctx
                            flag_modified(task, "context")
                            session.add(task)
                            # Spawn the review task that _process_post_commit would
                            # have spawned if the agent had reported COMPLETED.
                            _ho_spawn_ok = False
                            try:
                                from app.review_pipeline import _spawn_pipeline_task

                                _task_type = (task.first_payload or {}).get(
                                    "type", ""
                                ) or ""
                                # Prefer the source task's existing branch (fix tasks
                                # carry the PR's HEAD ref; create/refactor tasks carry
                                # the branch the agent already pushed). Only fall back
                                # to constructing a fresh blueprint/* name when no
                                # branch is recorded — without this, reaped fix tasks
                                # spawn review tasks pointing at non-existent branches
                                # and the review fails on `git clone --branch`.
                                _proj = _projects_batch.get(task.project_id)
                                if _proj is None or inspect(_proj).expired:
                                    _proj = await session.get(Project, task.project_id)
                                _branch = (task.first_payload or {}).get(
                                    "branch"
                                ) or _derive_task_branch(
                                    task.id,
                                    _task_type,
                                    _get_project_task_branch_namespace(_proj),
                                )
                                _base_branch = (task.first_payload or {}).get(
                                    "base_branch"
                                ) or _get_project_integration_branch(_proj)
                                _original_prompt = (task.first_payload or {}).get(
                                    "prompt", task.description or ""
                                )
                                await _spawn_pipeline_task(
                                    task,
                                    "review",
                                    session,
                                    _pr_num,
                                    _branch,
                                    _base_branch,
                                    original_prompt=_original_prompt,
                                    original_task_id=str(task.id),
                                )
                                _ho_spawn_ok = True
                            except Exception as _spawn_exc:  # noqa: BLE001
                                # Review spawn is best-effort from the reaper — if it
                                # fails, the `_pr_state_check_pending` marker set
                                # above (left in place since we don't clear it on
                                # this path) routes recovery to the services
                                # evaluating-sweep consumer; the 6h hard timeout
                                # still bounds the EVALUATING state. Don't let this
                                # crash the reaper cycle.
                                logger.warning(
                                    f"[REAPER] Review spawn failed for reaped-with-PR task {task.id}: "
                                    f"{type(_spawn_exc).__name__}: {_spawn_exc} — leaving "
                                    f"_pr_state_check_pending set for the evaluating-sweep consumer",
                                    exc_info=True,
                                )
                                # _spawn_pipeline_task commits/rolls back internally;
                                # a rollback expires `task` and discards our marker
                                # mutation. Re-stage the EVALUATING handoff WITH the
                                # recovery marker so the parent stays recoverable
                                # rather than stranded childless.
                                with contextlib.suppress(Exception):
                                    task.status = TaskStatus.EVALUATING
                                    task.vm_instance_id = None
                                    task.last_heartbeat = None
                                    _recover_ctx = dict(task.context or {})
                                    _recover_ctx["_pr_state_check_pending"] = True
                                    task.context = _recover_ctx
                                    flag_modified(task, "context")
                                    session.add(task)
                            if _ho_spawn_ok:
                                # Spawn confirmed — drop the write-ahead marker so
                                # the consumer doesn't spawn a SECOND review on a
                                # parent that already has its review child.
                                _ok_ctx = dict(task.context or {})
                                _ok_ctx.pop("_pr_state_check_pending", None)
                                task.context = _ok_ctx
                                flag_modified(task, "context")
                                session.add(task)
                            # Status was set to EVALUATING above; _spawn_pipeline_task
                            # can roll back internally and expire `task` (even with
                            # expire_on_commit=False), so a task.status read here would
                            # sync lazy-load in async ctx → MissingGreenlet. Use literal.
                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {
                                    "task_id": str(task.id),
                                    "status": TaskStatus.EVALUATING,
                                },
                            )
                        elif can_retry:
                            # Check if this is a research/plan parent with work children —
                            # re-evaluate instead of re-run.
                            task_type = (task.first_payload).get("type", "")
                            is_parent_type = task_type in ("research", "plan")
                            # Use pre-batched children mapping (N+1 guard above the loop).
                            children = _children_by_parent.get(task.id, [])
                            if not is_parent_type:
                                # Non-parent types have no children; ensure empty list.
                                children = []
                            if children:
                                # Audit A8: queue for post-commit processing; don't
                                # block heartbeats on this task while we clone children.
                                # Mark requeue_in_flight so the next reaper cycle (and
                                # the orchestrator dispatch) skips this task while we work.
                                logger.info(
                                    f"[REAPER] Deferring re-evaluation of parent task {task.id} until after lock release"
                                )
                                task.requeue_in_flight = True
                                session.add(task)
                                _deferred_parent_requeues.append(task)
                            else:
                                if not is_provision_failure and not ext_api_fast_path:
                                    task.attempt_count += 1
                                if is_provision_failure:
                                    retry_label = f"provision {provision_failures}/{max_provision_retries}"
                                else:
                                    retry_label = f"attempt {task.attempt_count}/{class_budget} [{reap_failure_class}]"
                                logger.info(
                                    f"[REAPER] Retrying task {task.id} ({retry_label})"
                                )
                                reap_ctx = task.context or {}
                                reap_zone = reap_ctx.get("provision_zone", "?")
                                reap_acct = (get_task_llm_account_id(reap_ctx) or "?")[
                                    :8
                                ]
                                bank_task_current_attempt(task, stopped_at=_utcnow())
                                task.latest_status_message = f"Reaped ({retry_label}): {reap_reason} [zone={reap_zone}, acct={reap_acct}]"
                                task.status = TaskStatus.QUEUED
                                _pre_strip_reaper_ctx = task.context or {}
                                reset_task_for_fresh_attempt(task)
                                task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                                reaper_ctx = dict(task.context or {})
                                # provision_failures is a task-lifetime counter
                                # that must survive ALL reap retries — only
                                # admin/clone resurrects should get a clean slate.
                                if "provision_failures" in _pre_strip_reaper_ctx:
                                    reaper_ctx["provision_failures"] = (
                                        _pre_strip_reaper_ctx["provision_failures"]
                                    )
                                if is_provision_failure:
                                    # Provision retry counters and account
                                    # tracking survive into the retry so repeated
                                    # infrastructure failures still converge and
                                    # the account blacklist is enforced.
                                    for _provision_key in (
                                        "provision_failures_infra",
                                        "_infra_wait",
                                        "blacklisted_accounts",
                                        "provision_failed_accounts",
                                    ):
                                        if _provision_key in _pre_strip_reaper_ctx:
                                            reaper_ctx[_provision_key] = (
                                                _pre_strip_reaper_ctx[_provision_key]
                                            )
                                # Enrich with failure diagnosis for retry guidance
                                try:
                                    from app.anomaly_detection import (
                                        _error_fingerprint,
                                        _is_recurring_failure_class,
                                    )
                                    from app.task_lifecycle import _derive_failure_stage
                                    import app.shared_state as _anomaly_state

                                    _reap_failure_class = (
                                        "PROVISION_FAILURE"
                                        if is_provision_failure
                                        else reap_failure_class
                                    )
                                    _fingerprint = _error_fingerprint(reap_reason)
                                    # Audit W1: previous_error_fingerprint is a column.
                                    task.previous_error_fingerprint = _fingerprint
                                    _stage = _derive_failure_stage(
                                        _reap_failure_class, reap_reason
                                    )
                                    if _stage:
                                        reaper_ctx["_previous_failure_stage"] = _stage
                                    _proj_anomalies = (
                                        _anomaly_state.persistent_anomalies.get(
                                            str(task.project_id), {}
                                        )
                                    )
                                    if _is_recurring_failure_class(
                                        _proj_anomalies, _reap_failure_class
                                    ):
                                        reaper_ctx["_previous_is_recurring"] = True
                                    # Set base fields so buildRetryContext renders the retry section
                                    # (only for agent failures — provision failures ran no agent code)
                                    if not is_provision_failure:
                                        reaper_ctx["_attempt_number"] = (
                                            task.attempt_count
                                        )
                                        reaper_ctx["_max_attempts"] = class_budget
                                        reaper_ctx["_previous_error"] = (
                                            task.latest_status_message or ""
                                        )[:1500]
                                        reaper_ctx["_previous_failure_class"] = (
                                            _reap_failure_class
                                        )
                                        reaper_ctx["_failure_class_history"] = (
                                            append_failure_class_history(
                                                reaper_ctx.get(
                                                    "_failure_class_history"
                                                ),
                                                _reap_failure_class,
                                            )
                                        )
                                        if _reap_failure_class == "CODE_ERROR":
                                            reaper_ctx["_previous_error_summary"] = (
                                                task.latest_status_message or ""
                                            )[:500]
                                        _copy_analysis_artifacts_to_previous(reaper_ctx)
                                except Exception:
                                    logger.exception(
                                        "[REAPER] Failed to enrich retry context for task %s",
                                        task.id,
                                    )
                                if not is_provision_failure:
                                    try:
                                        from app.retry_prompt import (
                                            _build_retry_prompt_suffix,
                                            merge_retry_prompt_suffix,
                                        )

                                        _task_scope = (
                                            reaper_ctx.get("scope")
                                            or (task.first_payload or {}).get("scope")
                                            or "small"
                                        )
                                        _prior_error = (
                                            (task.context or {}).get("last_error")
                                            or reaper_ctx.get("_previous_error")
                                            or ""
                                        )
                                        _extra_guidance, _scope_override = (
                                            _build_retry_prompt_suffix(
                                                reap_failure_class,
                                                _prior_error,
                                                task.attempt_count,
                                                _task_scope,
                                                reaper_ctx,
                                            )
                                        )
                                        if _scope_override:
                                            reaper_ctx["scope"] = _scope_override
                                            reaper_ctx["_scope_reduced"] = True
                                        if _extra_guidance and task.payloads:
                                            _new_payloads = list(task.payloads)
                                            if isinstance(_new_payloads[0], dict):
                                                _p0 = dict(_new_payloads[0])
                                                _existing_suffix = (
                                                    _p0.get("prompt_suffix", "") or ""
                                                )
                                                _p0["prompt_suffix"] = (
                                                    merge_retry_prompt_suffix(
                                                        _extra_guidance,
                                                        _existing_suffix,
                                                        reaper_ctx,
                                                    )
                                                )
                                                _new_payloads[0] = _p0
                                                task.payloads = _new_payloads
                                                flag_modified(task, "payloads")
                                    except Exception:
                                        logger.exception(
                                            "[REAPER] Failed to inject retry guidance for task %s",
                                            task.id,
                                        )
                                # Escalate effort one tier for a pure wall-clock
                                # execution timeout so the retry gets a larger
                                # budget (and a stronger model where effort drives
                                # model selection) instead of repeating the same
                                # doomed budget. Gated to the execution_timeout
                                # subtype only — oom/crash/heartbeat-loss/rate-limit
                                # never escalate. Persisted on reaper_ctx["effort"]
                                # (Layer-1 routing wins) before task.context is set.
                                try:
                                    from app.failure_classification import (
                                        escalated_effort_for_execution_timeout,
                                    )

                                    _escalated_effort = (
                                        escalated_effort_for_execution_timeout(task)
                                    )
                                    if _escalated_effort:
                                        reaper_ctx["_effort_escalated_from"] = (
                                            reaper_ctx.get("effort")
                                        )
                                        reaper_ctx["effort"] = _escalated_effort
                                except Exception:
                                    logger.exception(
                                        "[REAPER] Failed to escalate effort for task %s",
                                        task.id,
                                    )
                                if is_provision_failure and (task.context or {}).get(
                                    "_infra_wait"
                                ):
                                    # Infra-wide failure: align with zone recovery
                                    recovery = soonest_zone_recovery()
                                    if recovery:
                                        wait_sec = max(
                                            60,
                                            (
                                                recovery - datetime.now(timezone.utc)
                                            ).total_seconds()
                                            + 60,
                                        )
                                        delay_minutes = min(wait_sec / 60, 30)
                                    else:
                                        delay_minutes = 10 * random.uniform(0.75, 1.25)
                                elif is_provision_failure:
                                    delay_minutes = 5 * random.uniform(
                                        0.75, 1.25
                                    )  # 3.75-6.25 minutes
                                elif reap_failure_class == "EXTERNAL_API":
                                    # Adaptive delay: transient HTTP 5xx typically resolves in 1-3
                                    # minutes, so don't wait 5+ minutes on first retry. Sustained
                                    # outages fall through to [5,15,30,60]. Non-transient errors
                                    # (DNS, TLS, context_length_exceeded) indicate deeper problems
                                    # and always use the longer sequence.
                                    _err_blob = (
                                        combined_logs
                                        + " "
                                        + (task.latest_status_message or "")
                                    ).lower()
                                    _transient_5xx = any(
                                        p in _err_blob for p in _TRANSIENT_5XX_PATTERNS
                                    )
                                    _gh_consec = _external_health.get(
                                        "consecutive_failures", {}
                                    ).get("github_api", 0)
                                    if _transient_5xx and _gh_consec == 0:
                                        # Dependency already recovered — requeue immediately.
                                        delay_minutes = 0
                                    elif _transient_5xx and task.attempt_count == 1:
                                        # First-attempt short delay for transient 5xx.
                                        delay_minutes = 1.5 * random.uniform(0.75, 1.25)
                                    elif _transient_5xx:
                                        # Subsequent retries for transient 5xx: [5, 15, 30].
                                        _t5xx_delays = [5, 15, 30]
                                        _idx = min(
                                            task.attempt_count - 2,
                                            len(_t5xx_delays) - 1,
                                        )
                                        delay_minutes = _t5xx_delays[
                                            _idx
                                        ] * random.uniform(0.75, 1.25)
                                    else:
                                        # Non-transient (DNS, TLS, context_length_exceeded) — use
                                        # the longer sequence since these indicate deeper problems.
                                        _ext_api_delays = [5, 15, 30, 60]
                                        _attempt_idx = min(
                                            task.attempt_count - 1,
                                            len(_ext_api_delays) - 1,
                                        )
                                        delay_minutes = _ext_api_delays[
                                            _attempt_idx
                                        ] * random.uniform(0.75, 1.25)
                                else:
                                    # Exponential backoff for agent failures: 1, 2, 4, 8 min capped at 15
                                    delay_minutes = (
                                        exponential_backoff(
                                            task.attempt_count,
                                            base=60,
                                            cap=900,
                                            jitter=0.25,
                                        )
                                        / 60
                                    )
                                if task_type == "research":
                                    try:
                                        from app.autonomous_research import (
                                            refresh_research_task_verification,
                                        )

                                        _res_proj = _projects_batch.get(
                                            task.project_id
                                        )
                                        if _res_proj is not None and inspect(_res_proj).expired:
                                            _res_proj = await session.get(Project, task.project_id)
                                        if _res_proj is not None:
                                            await refresh_research_task_verification(
                                                task, _res_proj, session
                                            )
                                    except Exception:
                                        logger.exception(
                                            "[REAPER] Failed to refresh research task verification for task %s",
                                            task.id,
                                        )
                                task.context = reaper_ctx
                                set_retry_after(task, delay_minutes)
                                session.add(task)

                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {
                                    "task_id": str(task.id),
                                    "status": task.status,
                                    "attempt_count": task.attempt_count,
                                },
                            )
                        else:
                            if is_provision_failure:
                                fail_reason = (
                                    f"provision failed {provision_failures} times"
                                )
                            else:
                                fail_reason = f"failed after {class_budget} retries ({reap_failure_class})"
                            logger.error(f"[REAPER] Task {task.id} {fail_reason}.")
                            task.status = TaskStatus.FAILED
                            if task.vm_provisioned_at:
                                task.vm_stopped_at = _utcnow()
                            if is_provision_failure:
                                ctx = task.context or {}
                                provision_zone = ctx.get(
                                    "provision_zone", "unknown zone"
                                )
                                blacklisted_accounts = (
                                    ctx.get("blacklisted_accounts", []) or []
                                )
                                sticky_accounts = (
                                    ctx.get("provision_failed_accounts", []) or []
                                )
                                all_blocked = list(
                                    {*blacklisted_accounts, *sticky_accounts}
                                )
                                acct_id = get_task_llm_account_id(ctx) or "no account"
                                gcp_error = provision_err or "no GCP error captured"
                                gcp_error_short = gcp_error[:200]
                                # Distinguish: VM created but agent died vs. GCP API failure
                                if provision_zone and provision_zone != "unknown zone":
                                    label = "Agent startup failed"
                                    detail = f"VM created in {provision_zone} but agent never claimed task"
                                    if all_blocked:
                                        detail += f" (blacklisted {len(all_blocked)} account(s))"
                                else:
                                    label = "Provision failed"
                                    detail = f"GCP error: {gcp_error_short}"
                                task.latest_status_message = (
                                    f"{label} {provision_failures}x — "
                                    f"zone: {provision_zone}, acct: {acct_id}. "
                                    f"{detail}. "
                                    f"Last reap: {reap_reason}"
                                )
                            else:
                                task.latest_status_message = f"{fail_reason.capitalize()} — last reap: {reap_reason}"
                            extra_log_parts = [
                                (task.context or {}).get("last_error", ""),
                                (task.context or {}).get("last_serial_log", ""),
                            ]
                            if is_provision_failure:
                                extra_log_parts.append(provision_err or "")
                            extra_logs = " ".join(filter(None, extra_log_parts))
                            from app.failure_classification import (
                                classify_failure_with_confidence,
                            )

                            classification = classify_failure_with_confidence(
                                task,
                                logs=extra_logs or None,
                                # Fall back to started_at when last_heartbeat is None
                                # (never-claimed provision deaths) so the cooldown
                                # correlation still evaluates at the failure instant
                                # rather than silently defaulting to sweep time.
                                # Matches the agent_status.py call sites.
                                at_time=task.last_heartbeat or task.started_at,
                            )
                            failure_class = classification.failure_class
                            if reap_reason_code == "agent_stalled":
                                # A "stall" with an LLM call started but 0 output
                                # tokens is a silent provider hang (429/529 the
                                # Claude Code SDK absorbed via internal retry,
                                # surfacing no rate-limit text), NOT a code stall.
                                # Classify as RATE_LIMIT so it holds/reroutes
                                # instead of burning the retry budget and
                                # terminal-failing a healthy task (incident
                                # 2026-05-29). The reap-reason text itself never
                                # matches an AGENT_TIMEOUT keyword, so this is
                                # checked directly rather than via the classifier.
                                if _stuck_llm_call_no_progress(task):
                                    failure_class = RATE_LIMIT
                                    _persist_stuck_call_reclassification(task)
                                else:
                                    failure_class = "agent_stalled"
                            persist_failure_category(
                                task,
                                failure_class,
                                logs=extra_logs or None,
                                confidence=classification.confidence,
                            )
                            record_task_result(
                                success=False,
                                failure_class=failure_class,
                                task_id=str(task.id),
                                task_type=task.first_payload.get("type", ""),
                            )
                            session.add(task)
                            await _maybe_create_failure_alert(task, session)

                            await broadcast_to_project(
                                str(task.project_id),
                                "task_updated",
                                {"task_id": str(task.id), "status": TaskStatus.FAILED},
                            )

                        # ── PER-ROW COMMIT ──────────────────────────────────
                        # Previously the loop accumulated EVERY orphan's mutations
                        # and flushed them in ONE batched commit after the loop. A
                        # single IntegrityError there (e.g. a peer racing into
                        # ux_task_active_idempotency_key) rolled back the WHOLE
                        # batch — every other orphan's recovery work was discarded
                        # and had to re-qualify next cycle. Committing per row makes
                        # each orphan's recovery durable independently: one bad row
                        # rolls back only itself (handled below), the rest stay
                        # committed. The orphan-row FOR UPDATE lock was already
                        # released before this loop (commit + expire_all above), so a
                        # per-row commit here holds no extra locks across the I/O.
                        await session.commit()
                        # Heal the direction's research-history merge counts for an
                        # orphan whose PR merged out-of-band. Done in a FRESH session
                        # re-fetching the task by id (the per-row commit above can
                        # expire ``task``, and the refresh takes the directions
                        # FOR-NO-KEY-UPDATE lock + commits, so it must not share the
                        # reaper's batch session). No-op unless the task carries
                        # direction/research lineage; best-effort.
                        if _orphan_merge_refresh_needed:
                            from app.direction_outcomes import (
                                refresh_direction_outcomes_after_merge,
                            )

                            try:
                                async with AsyncSessionLocal() as _dir_session:
                                    _dir_task = await _dir_session.get(Task, task_id)
                                    if _dir_task is not None:
                                        await refresh_direction_outcomes_after_merge(
                                            _dir_task, _dir_session
                                        )
                            except Exception as _dir_exc:  # noqa: BLE001
                                logger.warning(
                                    "[REAPER] direction-outcome refresh failed for "
                                    "orphan-recovered merge of task %s: %r",
                                    task_id,
                                    _dir_exc,
                                )
                    except Exception as _reap_exc:
                        # Roll back partial mutations for THIS row only — earlier rows
                        # were already committed per-row above, so they survive. The
                        # next cycle re-selects this row (its mutations rolled back)
                        # via the Step-1 scan. Pattern mirrors the per-project scan in
                        # lifecycle.py (~479-496).
                        with contextlib.suppress(Exception):
                            await session.rollback()
                        # Discard ONLY this row's deferred entries; earlier committed
                        # rows keep their post-commit VM-delete / parent-requeue work.
                        # task_id-keyed pop + index truncation avoid reading any
                        # post-rollback-expired ORM attribute.
                        _reaper_old_vms.pop(str(task_id), None)
                        del _deferred_parent_requeues[_dq_len_at_row_start:]
                        # Re-batch-fetch remaining orphan tasks after rollback to
                        # avoid N+1 per-row session.get() on the next iterations.
                        # The rollback expires all cached ORM objects; a single
                        # IN-query here is faster than one SELECT per remaining row.
                        _remaining_ids = _orphaned_task_ids[_orphan_loop_idx + 1:]
                        if _remaining_ids:
                            try:
                                _refreshed = (
                                    (
                                        await session.execute(
                                            select(Task).where(Task.id.in_(_remaining_ids))
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
                                for _rt in _refreshed:
                                    _orphan_task_map[_rt.id] = _rt
                            except Exception as _refresh_exc:  # noqa: BLE001
                                logger.debug(
                                    "[REAPER] Remaining-orphan batch refresh failed; "
                                    "falling back to per-row fetch: %r",
                                    _refresh_exc,
                                )

                        # Benign per-row idempotency collision: the requeue→QUEUED /
                        # →EVALUATING mutation tried to enter
                        # ux_task_active_idempotency_key while a peer already held the
                        # key in an active status. The pre-query guard above catches
                        # the common case; this is the narrow race window between that
                        # SELECT and this per-row COMMIT. The active duplicate already
                        # represents the work, so the reap is moot — skip THIS row
                        # cleanly (no streak / quarantine) and let it re-qualify next
                        # cycle if it doesn't resolve. This is NOT a poison-pill crash.
                        if isinstance(
                            _reap_exc, IntegrityError
                        ) and "ux_task_active_idempotency_key" in str(_reap_exc):
                            logger.warning(
                                "[REAPER] Orphan reap of %s hit "
                                "ux_task_active_idempotency_key on commit (a peer "
                                "holds the key in an active status) — rolled back this "
                                "row only; earlier rows stay committed; it re-qualifies "
                                "next cycle via the Step-1 scan. Detail: %s",
                                task_id,
                                str(_reap_exc)[:300],
                            )
                            _orphan_reap_crash_streaks.pop(str(task_id), None)
                            continue

                        logger.error(
                            "[REAPER] orphan reap raised for task %s — skipping: %s",
                            task_id,
                            _reap_exc,
                            exc_info=True,
                        )
                        _reap_key = str(task_id)
                        _orphan_reap_crash_streaks[_reap_key] = (
                            _orphan_reap_crash_streaks.get(_reap_key, 0) + 1
                        )
                        if (
                            _orphan_reap_crash_streaks[_reap_key]
                            >= _ORPHAN_REAP_CRASH_STREAK_LIMIT
                        ):
                            try:
                                async with AsyncSessionLocal() as _qsession:
                                    _qtask = await _qsession.get(Task, task_id)
                                    if _qtask is not None and _qtask.status in (
                                        TaskStatus.PROVISIONING,
                                        TaskStatus.IN_PROGRESS,
                                    ):
                                        _streak = _orphan_reap_crash_streaks[_reap_key]
                                        _qtask.status = TaskStatus.FAILED
                                        _qtask.latest_status_message = (
                                            f"Quarantined: reaper crashed {_streak}x"
                                            f" — last error: {str(_reap_exc)[:200]}"
                                        )
                                        from app.models import Alert, AlertSeverity

                                        _qsession.add(
                                            Alert(
                                                project_id=_qtask.project_id,
                                                severity=AlertSeverity.WARNING,
                                                title=(
                                                    "Reaper quarantine: "
                                                    + (_qtask.title or str(task_id)[:8])
                                                ),
                                                message=(
                                                    f"Task {task_id} failed to reap "
                                                    f"{_streak} consecutive time(s) and "
                                                    f"has been quarantined (FAILED). "
                                                    f"Last error: {str(_reap_exc)[:300]}"
                                                ),
                                                task_id=task_id,
                                                event_type="task.failed",
                                            )
                                        )
                                        _qsession.add(_qtask)
                                        await _qsession.commit()
                                        logger.exception(
                                            "[REAPER] Task %s quarantined after %d "
                                            "consecutive reap crashes",
                                            task_id,
                                            _streak,
                                        )
                                        del _orphan_reap_crash_streaks[_reap_key]
                            except Exception:
                                logger.exception(
                                    "[REAPER] Failed to quarantine task %s", task_id
                                )
                    else:
                        # Reap completed without raising — clear any prior crash streak
                        # so a task that crashed once or twice then reaped cleanly does
                        # not leave a permanent entry (unbounded dict growth keyed by
                        # task id).
                        _orphan_reap_crash_streaks.pop(str(task_id), None)
                if orphaned_tasks:
                    # Each orphan above already committed its OWN mutations per-row
                    # (see the PER-ROW COMMIT block), so the batch-wide
                    # ux_task_active_idempotency_key collision that used to roll back
                    # the WHOLE loop here is gone — it's now isolated per row. This
                    # trailing commit is a defensive flush of any residual session
                    # state (it commits nothing if the last row already committed
                    # cleanly). A residual IntegrityError can no longer originate from
                    # the orphan reaps themselves, so let it surface (the outer
                    # per-cycle handler classifies / circuit-breaks).
                    await session.commit()
                    _orphan_commit_ok = True

                await run_post_orphan_sweep(
                    session,
                    orphaned_tasks,
                    orphan_commit_ok=_orphan_commit_ok,
                    orphaned_task_ids=_orphaned_task_ids,
                    deferred_parent_requeues=_deferred_parent_requeues,
                    reaper_old_vms=_reaper_old_vms,
                    progress_cb=_mark_reaper_progress,
                    landed_cb=_mark_landed_parent_dependents,
                )

                # Steps 3+4: Sweep stale EVALUATING parents (rollup re-trigger)
                # and rescue all-children-failed EVALUATING parents. Extracted to
                # reaper_evaluating_sweeps.sweep_evaluating_parents().
                await sweep_evaluating_parents(
                    session,
                    now,
                    _mark_reaper_progress,
                    landed_cb=_mark_landed_parent_dependents,
                )

                # Step 3.25: discovery review convergence. Immediate completion
                # hooks are latency optimizations; this bounded leader-owned
                # sweep is the correctness path for expired envelopes and
                # terminal cohorts whose request process died between commits.
                try:
                    from app.discovery_review import sweep_discovery_reviews

                    await sweep_discovery_reviews(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Discovery-review sweep error: {e}",
                        exc_info=True,
                    )

                # Step 3.5: Detach EVALUATING provider tasks whose direction closed.
                # direction_id is provenance, not a kill-tether — a retired
                # direction must NEVER cancel in-flight/completed work (cancelling
                # cascaded to FAIL parents and strand spawned fix tasks). Clear the
                # stale direction_id so the generic EVALUATING sweep can resolve the
                # task via its own children/PR.
                _mark_reaper_progress()
                await sweep_orphaned_direction_provider_tasks(session)

                # Step 5: Infrastructure recovery sweep + hold maintenance.
                # Delegates to reaper_infra_recovery for resurrection (5),
                # infra hold clearance (5a), orphan hold sweep (5c),
                # in-memory state pruning (5d), stale retry_after sweep (5b),
                # and stale residual case closure (5e).
                # C3: Steps 5-10 previously shared one REAPER_HANG_SECONDS budget
                # with no intra-step heartbeat, so a slow Step 5 sub-step (serial
                # resurrection DB work, broadcast fan-out) could exhaust the
                # window before Step 6 even started. Heartbeat at the start of
                # each step, and pass the heartbeat into Step 5 so its sub-steps
                # mark progress too — liveness now tracks step-level progress.
                _mark_reaper_progress()
                await run_infra_recovery_and_hold_maintenance(
                    session,
                    now,
                    just_reaped_ids=_orphaned_task_ids,
                    progress_cb=_mark_reaper_progress,
                )
                try:
                    await sweep_stale_migration_reservations(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Migration-reservation sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 6: QUEUED task age alerts — 3-tier INFO/WARNING/ERROR escalation.
                _mark_reaper_progress()
                try:
                    await sweep_queued_age_alerts(
                        session,
                        now,
                        runtime_config=_runtime_config,
                        batch_size=_BATCH,
                    )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Queued age alert sweep error: {e}", exc_info=True
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 7: Stalled strategic-cycle reaper. Also detects zero-cycle
                # starvation (autonomous project with no recent StrategicCycle row
                # at all) and emits the strategic_cycle_stall Alert here — no
                # separate emitter (operator decision).
                _mark_reaper_progress()
                try:
                    await sweep_stalled_strategic_cycles(
                        session,
                        now,
                        heartbeat_stale_cutoff_fn=_heartbeat_stale_cutoff,
                    )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stalled-cycle sweep error: {e}", exc_info=True
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()
                try:
                    await _sweep_strategic_cycle_starvation(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Strategic-cycle starvation sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 8: Per-(provider, model) regression scan (~15min cadence).
                _mark_reaper_progress()
                try:
                    await sweep_regression_scan(session)
                except Exception as e:
                    logger.error(f"[REAPER] Regression scan error: {e}", exc_info=True)
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 9: Finalize release qualifications stuck "pending".
                # ``session_factory=AsyncSessionLocal`` is passed explicitly so the
                # per-version finalize session uses the same engine as this scan session
                # (important under the test harness where the reaper patches AsyncSessionLocal).
                _mark_reaper_progress()
                try:
                    await sweep_stuck_qualifications(
                        session, session_factory=AsyncSessionLocal
                    )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stuck qualification sweep error: {e}",
                        exc_info=True,
                    )
                    # Re-raise so the outer consecutive_errors counter engages
                    # and the liveness timestamp stops advancing.  Per-version
                    # crashes are swallowed inside the sweep; this re-raise covers
                    # scan-level failures (e.g. DB error on the initial query).
                    raise

                # Step 10: Routine message retention — purge stale routine ops messages.
                _mark_reaper_progress()
                try:
                    await sweep_routine_message_retention(session)
                except Exception as e:
                    logger.error(f"[REAPER] Routine message purge: {e}", exc_info=True)
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 10a: Global Strategic envelope/private-packet retention.
                # This is intentionally independent of a project receiving its
                # next message, so expired prompt copies cannot become dormant
                # data-retention leaks.
                _mark_reaper_progress()
                try:
                    await sweep_strategic_context_retention(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Strategic context retention purge: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 10b: Global expired message-attachment cleanup. This is
                # independent of any actor returning to upload again.
                _mark_reaper_progress()
                try:
                    await sweep_expired_message_attachment_uploads(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Expired message attachment cleanup: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 10b: Evergreen fill-decision log retention (30-day TTL).
                _mark_reaper_progress()
                try:
                    await sweep_evergreen_fill_decision_retention(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Evergreen fill-decision log purge: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 10c: Resolve preserved hand-merge PRs that have been merged/closed.
                _mark_reaper_progress()
                try:
                    _hm_resolved = await sweep_closed_handmerge_prs(session)
                    if _hm_resolved:
                        logger.info(
                            f"[REAPER] Step 10c: cleared {_hm_resolved} closed hand-merge PR(s)"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Handmerge PR sweep error: {e}", exc_info=True
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 11: Idle warm-pool VM reaper — retire over-TTL / zombie /
                # stale-image / stuck-RETIRING pool members (Task-row reaper is
                # blind to taskless pool VMs). Inert when warm_pool_enabled=False.
                _mark_reaper_progress()
                try:
                    from app.warm_pool_maintainer import sweep_idle_pool_vms

                    pool_retired = await sweep_idle_pool_vms(
                        session, now, runtime_config=_runtime_config
                    )
                    if pool_retired:
                        logger.info(
                            f"[REAPER] Step 11: retired {pool_retired} idle pool VM(s)"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Idle pool VM sweep error: {e}", exc_info=True
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 12: Auto-unblock / escalate BLOCKED tasks — recheck each
                # parked precondition blocker, requeue cleared ones to QUEUED,
                # and escalate permanently-stuck / deadline-expired ones once.
                _mark_reaper_progress()
                try:
                    await sweep_unblock_blocked_tasks(session, now)
                except Exception as e:
                    logger.error(f"[REAPER] Unblock sweep error: {e}", exc_info=True)
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 12a: Runner-death compute recovery — a runner-subprocess
                # job has no backend-side liveness signal once its claiming runner
                # stops reporting. Finalize only jobs tied to a confirmed-dead
                # claiming runner, then let the normal wake step deliver its task.
                _mark_reaper_progress()
                try:
                    await sweep_stranded_runner_subprocess_compute_jobs(
                        session, now, session_factory=AsyncSessionLocal
                    )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stranded runner compute-job sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 12b: Deliver external task wakes — advance PARKED tasks
                # whose registered wake source has fired (exactly once), expire
                # timed-out registrations, and strand-guard orphaned parked tasks.
                _mark_reaper_progress()
                _wake_result = None
                try:
                    _wake_result = await sweep_deliver_task_wakes(session, now)
                    # Per-cycle wake-delivery health: this return value used to be
                    # discarded, leaving a wedged-but-not-crashed delivery path
                    # invisible. Log the full breakdown whenever the cycle did
                    # anything (or is stalled) so delivered / expired / stranded /
                    # delivery-failed counts and the cross-cycle delivery-stall
                    # signal are observable per cycle.
                    if _wake_result.is_noteworthy():
                        _wake_log = (
                            logger.warning
                            if _wake_result.delivery_stalled
                            else logger.info
                        )
                        _wake_log(
                            "[REAPER] Task-wake delivery: delivered=%d expired=%d "
                            "stranded=%d delivery_failed=%d fired_undelivered=%d "
                            "stalled=%s (stall_cycles=%d)",
                            _wake_result.delivered,
                            _wake_result.expired,
                            _wake_result.stranded,
                            _wake_result.delivery_failed,
                            _wake_result.fired_undelivered,
                            _wake_result.delivery_stalled,
                            _wake_result.stall_cycles,
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Task-wake delivery sweep error: {e}", exc_info=True
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 12b: Alert — raise or clear per-project wake-delivery-stall
                # operator alerts based on the cycle counter from the sweep above.
                # Runs in its own try/except so an alert failure never aborts the
                # reaper cycle or masks the underlying delivery signal.
                _mark_reaper_progress()
                try:
                    await sweep_wake_delivery_stall_alerts(
                        session,
                        delivery_stalled=(
                            _wake_result.delivery_stalled if _wake_result else False
                        ),
                        stall_cycles=(_wake_result.stall_cycles if _wake_result else 0),
                    )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Wake-delivery stall alert sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 12b: Recovery — un-strand transient blockers previously
                # hard-parked at the +10y sentinel so they self-heal when the
                # overlapping in-flight task merges.
                _mark_reaper_progress()
                try:
                    await sweep_recover_stranded_transient_blockers(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Transient-blocker recovery sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 13: Staged-direction auto-activation backstop — self-heal
                # any staged direction whose target version already promoted/opened
                # in case the promotion-path activation hook failed once.
                _mark_reaper_progress()
                try:
                    await sweep_activate_staged_directions(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Staged-direction activation sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 14: Stale-staged-direction alerting — surface staged
                # work whose target version has stalled (not promoted) past the
                # configurable staleness threshold so an operator can retarget,
                # force-activate, or cancel it. Alerting only.
                _mark_reaper_progress()
                try:
                    await sweep_stale_staged_directions(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stale-staged-direction sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Fleet merge-gate verification degradation alerting — surface
                # systemic claim-verification erosion (PRs landing UNVERIFIED at
                # scale) proactively instead of only on the dashboard. Alerting
                # only — never influences a merge decision.
                _mark_reaper_progress()
                try:
                    await sweep_merge_gate_verification_degradation(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Merge-gate degradation sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Migration-serialization-slot contention alerting — surface a
                # prolonged per-project migration slot hold that is parking
                # schema-touching tasks (invisible to the throughput-only
                # migration-health summary and the escalated-only parked-blocker
                # signal). Alerting only — never influences serialization.
                _mark_reaper_progress()
                try:
                    await sweep_migration_slot_contention(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Migration-slot-contention sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 15: Linked bug-class message convergence / resurfacing —
                # reconcile each 'linked' BUG_REPORT/BLOCKER with its owning
                # outcome: resolve when landed, reopen when failed/cancelled/
                # retired/missing, or when in-flight beyond the time bound.
                _mark_reaper_progress()
                try:
                    await sweep_converge_linked_bug_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Linked-message convergence sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 16: Converge duplicate foundations — serialize a high-
                # confidence cross-parent duplicate code-change task behind its
                # canonical sibling by wiring a depends_on edge, so the second
                # task rebases on the first's landed foundation instead of
                # rebuilding it. Conservative + cycle-guarded; below the action
                # threshold the pair is left untouched (signal-only).
                _mark_reaper_progress()
                try:
                    await sweep_converge_duplicate_foundations(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Duplicate-foundation convergence sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 16b: Structural-health audit — recurring resource/scalability
                # sweep that detects unbounded JSONB fat-row growth in task.context
                # (the structural class that caused a Postgres saturation outage) via
                # a BOUNDED DB-side byte-size aggregate, and enrolls a deduped finding
                # into the existing evergreen ledger. Mode- and cadence-gated; INERT
                # until an operator flips structural_health_audit_mode off→observe/
                # enforce. Wrapped so it can never crash the cycle.
                _mark_reaper_progress()
                try:
                    from app.structural_health_audit import run_structural_health_audit

                    await run_structural_health_audit(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Structural-health audit sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 16c: Metrics-integrity sweep — recurring leader-only self-audit
                # that recomputes selected self-reported counters (landed-vs-reported
                # merges, playbook confirmation integrity, non-code outcome window
                # accounting) from ground truth and FLAGS drift WITHOUT mutating the
                # audited source counter. Bounded per check; each check isolated.
                # Mode- and cadence-gated; INERT in observe (records to observability)
                # until an operator flips metrics_integrity_sweep_mode to enforce.
                # Wrapped so it can never crash the cycle.
                _mark_reaper_progress()
                try:
                    await sweep_metrics_integrity(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Metrics-integrity sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 16c2: Shared mode-knob graduation harness — disposes
                # dormant runtime-config mode-knobs with MEASURED evidence
                # (graduate off→observe→enforce on measured lift, emit a legible
                # CUT recommendation on measured no-lift, or HOLD). Runs
                # INDEPENDENTLY of metrics_integrity_sweep_mode (a harness that
                # disposes dormant knobs must not itself hinge on a dormant knob
                # being enforced); gated only by its own mode_knob_graduation_enabled
                # flag (default off). Wrapped so it can never crash the cycle.
                _mark_reaper_progress()
                try:
                    await sweep_mode_knob_graduation(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Mode-knob graduation sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 16d: Operator-inbox liveness — always-on signal that
                # measures forward-leaked non-actionable messages and
                # badge-invisible linked BLOCKER/QUESTION messages per project.
                # Runs regardless of metrics_integrity_sweep_mode; survives if
                # that sweep is cut.  Cadence- and error-isolated.
                _mark_reaper_progress()
                try:
                    await sweep_operator_inbox_liveness(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Operator-inbox liveness sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 16e: Proactive NULL-audience backfill — drain the FULL
                # population of open/linked non-user messages whose audience was
                # never stamped (legacy pre-registry rows) by re-stamping each
                # with its canonical audience. Complements Step 16d, which only
                # re-stamps the narrow forward-leaked subset. Independent hourly
                # cadence; idempotent (stamped rows stop matching); error-isolated.
                _mark_reaper_progress()
                try:
                    await sweep_backfill_null_audience_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] NULL-audience backfill sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 17: Auto-resolve OPEN owned bug-class messages — resolve an
                # open system/agent BUG_REPORT/BLOCKER once the outcome that owns
                # its fix (a direction or a PR, read from creation-time context)
                # has genuinely LANDED: direction 'completed' or owning PR merged.
                # Success-only + fail-safe; an owner that ended without landing
                # leaves the message open. Disjoint from Step 15 (linked sweep).
                _mark_reaper_progress()
                try:
                    await sweep_resolve_open_owned_bug_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Open-owned-message resolve sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 18: Negatively resolve OPEN bug-class messages confirmed
                # stale on HEAD — resolve an open system/agent BUG_REPORT/BLOCKER
                # whose cited code is unambiguously ABSENT on the current
                # integration HEAD (re-grounded fresh, conservative tri-state;
                # indeterminate/present leaves it open). Runs AFTER Step 17 so a
                # landed owner is positively resolved first. Fail-safe per message.
                _mark_reaper_progress()
                try:
                    await sweep_resolve_stale_open_bug_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stale-bug-message resolve sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 18b: Generalized linked-message auto-resolve — resolve any
                # NON-bug-class 'linked' message (NOTE/QUESTION/FEEDBACK) once its
                # recorded _convergence_link target genuinely lands, clearing its
                # needs-action derivation without operator involvement. Disjoint
                # from Step 15 by message_type (bug-class stays owned there).
                _mark_reaper_progress()
                try:
                    await sweep_resolve_linked_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Linked-message resolve sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 18c: Stale-open conversational decay — auto-resolve any OPEN
                # NOTE/FEEDBACK item with no activity for the decay window and no
                # live linked condition (resolution='auto-decayed (stale)'), so dead
                # conversation ages out. Genuinely-live QUESTION/BLOCKER untouched.
                _mark_reaper_progress()
                try:
                    await sweep_decay_stale_open_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stale-open decay sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 18d: Stale-open bug-class decay — age out OPEN unowned
                # BUG_REPORT/BLOCKER messages after the long decay window (default
                # 30 days) so machine-generated findings that are never owned or
                # linked don't accumulate forever. Disjoint from Steps 17/18 (which
                # cover owned and HEAD-absent messages). Fail-safe per message.
                _mark_reaper_progress()
                try:
                    await sweep_decay_stale_open_bug_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stale-open-bug decay sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 18f: Answered-question auto-resolve — close OPEN QUESTION and
                # operator-action-needed messages that have received a genuine
                # strategic/agent/system reply so they stop inflating the
                # needs-action badge. BLOCKERs are never auto-resolved here.
                _mark_reaper_progress()
                try:
                    await sweep_auto_resolve_answered_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Answered-message auto-resolve sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 18e: Unlinked BLOCKER decay — age out OPEN BLOCKER messages
                # (all authors, including user) that have no convergence link and
                # no creation-time ownership context, after a conservative 60-day
                # window. Complements Step 18d (which handles system/agent authors
                # at 30 days). Fail-safe per message.
                _mark_reaper_progress()
                try:
                    await sweep_decay_stale_unlinked_blocker_messages(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stale-unlinked-blocker decay sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 19: Lane-pilot KILL verdict sweep — evaluate pilot safety
                # conditions for all projects with active lane work; apply KILL
                # (flag flip + force-expire reservations + kill lanes + alert) when
                # warranted. Owns the KILL safety guarantee independently of digest.
                _mark_reaper_progress()
                try:
                    from app.review_fix_lane_pilot import sweep_lane_pilot_kill_verdict

                    _pilot_kills = await sweep_lane_pilot_kill_verdict(session)
                    if _pilot_kills:
                        logger.info(
                            f"[REAPER] Step 19: applied lane-pilot KILL to {_pilot_kills} project(s)"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Lane-pilot kill sweep error: {e}", exc_info=True
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 20: Orphaned cross-base children converge guard — cancel
                # QUEUED children whose parent direction is COMPLETED and whose
                # cross-base genuine code-dependency is permanently stranded (dep
                # COMPLETED but confirmed NOT to be an ancestor of integration).
                # Conservative: only fires when STRANDED_LANDING_REASON_KEY is
                # explicitly set on the dep; legitimately-waiting children
                # (dep not yet stranded) are left untouched.
                _mark_reaper_progress()
                try:
                    _cross_base_cancelled = await sweep_orphaned_cross_base_children(
                        session
                    )
                    if _cross_base_cancelled:
                        logger.info(
                            f"[REAPER] Step 20: cancelled {_cross_base_cancelled} "
                            "orphaned cross-base child task(s)"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Orphaned cross-base children sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 21: Backfill acceptance verdicts for pre-fix false-negative
                # named_test evaluations — re-evaluate with the current fixed logic
                # and record CriterionEvidence when the new verdict is proven=True.
                # One-time bounded sweep (200 tasks/cycle); idempotent after all
                # candidates are processed (each upgraded task carries proven=True).
                _mark_reaper_progress()
                try:
                    await backfill_acceptance_verdicts(session)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Backfill-acceptance-verdicts sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 21a: Auto-backfill acceptance evidence from pre-existing test
                # coverage.  Finds merged tasks whose declared named_test matches
                # test files that shipped in the same PR, and records backfilled
                # criterion evidence — upgrading from 'unproven' to 'backfilled' on
                # the coverage ladder.  Hourly cadence-gated; independent timer.
                _mark_reaper_progress()
                try:
                    await sweep_backfill_merged_named_test_coverage(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Backfill-merged-named-test-coverage sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 22: Capability backing-proof sweep — record backing_path
                # proof for working capabilities whose declared backing_paths all
                # resolve at HEAD. Hourly cadence-gated. This is a PRODUCER (writes
                # proof records), NOT an auditor — it creates evidence for capabilities
                # that already meet the bar rather than flagging those that don't.
                # Wrapped so it can never crash the cycle.
                _mark_reaper_progress()
                try:
                    await sweep_capability_backing_proofs(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Capability backing-proof sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 22b: Proof-earn loop — commission proof for demanded unproven
                # working capabilities.  Computes the intersection of demanded
                # capabilities (from the missing-foundation demand signal) and unproven
                # working capabilities, then attempts to earn proof via backing-path
                # verification.  Shares the hourly cadence of Step 22.
                _mark_reaper_progress()
                try:
                    await sweep_proof_earn_loop(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Proof-earn loop error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 22c: Compute-image GC guard — stamp durable ``inuse-`` tags
                # on registry digests a live/recent compute job still depends on
                # so purely time/count-based registry retention can't reclaim an
                # image out from under a running experiment or a recent result's
                # provenance. Hourly cadence-gated on an independent timer.
                # Wrapped so it can never crash the cycle; registry I/O fails open.
                _mark_reaper_progress()
                try:
                    await sweep_protect_referenced_compute_image_digests(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Compute-image GC guard sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 22c-ii: DB-pin reconciliation — verify every DB-pinned
                # compute-image digest has its ``inuse-`` tag in the registry,
                # surfacing the case where GC would delete an in-use image.
                # Hourly cadence-gated on an independent timer. Fail-open.
                _mark_reaper_progress()
                try:
                    await sweep_reconcile_gc_guard_db_pins(session, now=now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] GC-guard DB-pin reconciliation error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 22d: Compute-image auto-refresh — rebuild a project's baked
                # compute image when its declared dependency manifest changes, so
                # the image can't silently drift from the code's real deps until an
                # operator remembers to click the manual rebuild. Cheap no-op (no
                # clone, no build) when the manifest is unchanged; hourly cadence-
                # gated on an independent timer. Wrapped so it can never crash the
                # cycle; the per-project driver is itself best-effort.
                # Pass _mark_reaper_progress as progress_cb so each multi-minute
                # build keeps the lifecycle watchdog alive via periodic heartbeats.
                _mark_reaper_progress()
                try:
                    await sweep_refresh_project_compute_images(
                        session, now, progress_cb=_mark_reaper_progress
                    )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Compute-image auto-refresh sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 22e (was inflight Cloud Build reconciliation — removed;
                # the ProjectComputeImageBuild table was dead code).

                # Step 22f: Auto-clear moot pending_user_confirmation items —
                # remove credentials_needed/access_needed asks the runtime can
                # already prove are resolved (owner LLM-account credential ready,
                # live cached project repo token) so they drop off the operator's
                # needs-action surface. Strictly conservative/fail-closed; never
                # touches intent_change/major_decision. Hourly cadence-gated on an
                # independent timer. Wrapped so it can never crash the cycle.
                _mark_reaper_progress()
                try:
                    await sweep_auto_clear_moot_pending_confirmations(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Moot pending-confirmation sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 22e-ii: Age-decay stale pending_user_confirmation items —
                # removes ANY item (all types including intent_change/major_decision)
                # whose created_at is older than PENDING_CONFIRMATION_STALE_DECAY_DAYS.
                # Complements Step 22e which is signal-based and credential/access-only.
                # 6-hourly cadence-gated on an independent timer.
                _mark_reaper_progress()
                try:
                    await sweep_decay_stale_pending_confirmations(session, now)
                except Exception as e:
                    logger.error(
                        f"[REAPER] Stale pending-confirmation decay error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 22f: Silent runner compute-job sweep — reap runner-subprocess
                # jobs whose runner process died (SIGKILL / host reboot) without self-
                # reporting, freeing the project's concurrency slot and spend ceiling
                # instead of letting them burn the full wall-clock deadline doing zero
                # work. Wrapped so it can never crash the cycle.
                _mark_reaper_progress()
                try:
                    _silent_reaped = await sweep_silent_runner_compute_jobs(session, now)
                    if _silent_reaped:
                        logger.info(
                            f"[REAPER] Step 22f: finalized {_silent_reaped} silent "
                            "runner compute job(s)"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] Silent runner compute-job sweep error: {e}",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 23: Retire viewable GCS artifacts and their DB rows for
                # compute jobs that completed more than 180 days ago.  Viewable
                # media is exempt from the GCS bucket lifecycle rules and relies
                # on this sweep for cleanup.
                _mark_reaper_progress()
                try:
                    _viewable_retired = await sweep_retire_viewable_artifacts(session)
                    if _viewable_retired:
                        logger.info(
                            "[REAPER] Step 23: retired %d viewable artifact(s)",
                            _viewable_retired,
                        )
                except Exception as e:
                    logger.error(
                        "[REAPER] Viewable artifact retirement sweep error: %s",
                        e,
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 24: Retire consumed operator-chat message attachments and
                # their DB rows for receipts consumed more than 180 days ago.
                # message-attachments/ is exempt from the GCS bucket lifecycle
                # rules and relies on this sweep for cleanup.
                _mark_reaper_progress()
                try:
                    _attachments_retired = await sweep_retire_consumed_message_attachments(session)
                    if _attachments_retired:
                        logger.info(
                            "[REAPER] Step 24: retired %d consumed message attachment(s)",
                            _attachments_retired,
                        )
                except Exception as e:
                    logger.error(
                        "[REAPER] Consumed message attachment retirement sweep error: %s",
                        e,
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await session.rollback()

            consecutive_errors = 0
            if _reaper_halt_state.get("_in_probe"):
                _probe_failures = _reaper_halt_state.get("half_open_failures", 0)
                _reaper_halt_state.update(
                    {
                        "halted": False,
                        "hard_halted": False,
                        "halt_timestamp": None,
                        "last_error": None,
                        "crash_count": 0,
                        "half_open_failures": 0,
                        "_in_probe": False,
                    }
                )
                logger.info(
                    "[REAPER] Autonomous recovery: half-open probe succeeded after %d failure(s)",
                    _probe_failures,
                )
                with contextlib.suppress(Exception):
                    asyncio.get_running_loop().create_task(
                        _post_reaper_recovery_alert(_probe_failures)
                    )
            # Liveness telemetry: only bump on the success path. If the
            # cycle body raised above, neither this counter nor the
            # last-completed timestamp advances — the /metrics gauge will
            # report a growing age and the monitoring alert fires.
            global _reaper_cycles_completed_count, _reaper_last_cycle_completed_at
            _reaper_cycles_completed_count += 1
            _reaper_last_cycle_completed_at = _utcnow()
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"[REAPER] Error: {e}", exc_info=True)
            if session is not None:
                try:
                    await session.rollback()
                except Exception:
                    logger.exception("[REAPER] Rollback after reaper error failed")
            _halt_threshold = _runtime_config.get(
                "reaper_cycle_halt_threshold", _REAPER_CYCLE_HALT_THRESHOLD
            )
            if _reaper_halt_state.get("_in_probe"):
                # Probe cycle failed; escalate or re-halt.
                _reaper_halt_state["_in_probe"] = False
                _ho_failures = _reaper_halt_state.get("half_open_failures", 0) + 1
                _probe_limit = int(
                    _runtime_config.get(
                        "reaper_half_open_probe_limit", _REAPER_HALF_OPEN_PROBE_LIMIT
                    )
                )
                if _ho_failures >= _probe_limit:
                    _reaper_halt_state.update(
                        {
                            "halted": True,
                            "hard_halted": True,
                            "halt_timestamp": _utcnow(),
                            "last_error": str(e)[:500],
                            "crash_count": consecutive_errors,
                            "half_open_failures": _ho_failures,
                        }
                    )
                    logger.exception(
                        "[REAPER] HARD HALT after %d consecutive failed probe(s) "
                        "— operator reset required; last error: %s",
                        _ho_failures,
                        e,
                    )
                    with contextlib.suppress(Exception):
                        asyncio.get_running_loop().create_task(
                            _post_reaper_halt_alert(consecutive_errors, str(e)[:500])
                        )
                else:
                    _reaper_halt_state.update(
                        {
                            "halted": True,
                            "hard_halted": False,
                            "halt_timestamp": _utcnow(),
                            "last_error": str(e)[:500],
                            "crash_count": consecutive_errors,
                            "half_open_failures": _ho_failures,
                        }
                    )
                    logger.warning(
                        "[REAPER] Half-open probe failed (%d/%d) — re-halting with extended cooldown",
                        _ho_failures,
                        _probe_limit,
                    )
            elif (
                consecutive_errors >= _halt_threshold
                and not _reaper_halt_state["halted"]
            ):
                _reaper_halt_state.update(
                    {
                        "halted": True,
                        "halt_timestamp": _utcnow(),
                        "last_error": str(e)[:500],
                        "crash_count": consecutive_errors,
                    }
                )
                logger.exception(
                    "[REAPER] HALTED after %d consecutive cycle crashes — last error: %s",
                    consecutive_errors,
                    e,
                )
                with contextlib.suppress(Exception):
                    asyncio.get_running_loop().create_task(
                        _post_reaper_halt_alert(consecutive_errors, str(e)[:500])
                    )

        # Sleep before next cycle; use exponential backoff on consecutive errors
        sleep_seconds = exponential_backoff(
            consecutive_errors + 1, base=60.0, cap=300.0, jitter=0.0
        )
        try:
            await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
