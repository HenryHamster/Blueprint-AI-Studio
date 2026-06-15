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
from typing import Dict

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Artifact,
    ArtifactType,
    Project,
    Task,
    TaskStatus,
    VmContact,
    bank_task_current_attempt,
)
from app.compute import (
    get_provision_error,
    record_zone_failure,
    _capture_serial_for_vm,
    get_vm_zone,
    get_vm_status,
    INFRA_PROVISION_PATTERNS,
    any_zone_available,
    soonest_zone_recovery,
)
from app.branch_resolution import classify_rollup_merge_outcome
from app.landed_signal import apply_merged_landed_signal
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
from app.task_blocked import strip_fresh_attempt_context
from app.time_utils import _utcnow, set_retry_after
from app.reaper_infra_recovery import run_infra_recovery_and_hold_maintenance

logger = logging.getLogger(__name__)


def _normalize_reaper_retry_failure_class(failure_class: str, context: object) -> str:
    """Match clone retry classification for raw lowercase scope-overflow labels."""
    if failure_class != "CODE_ERROR" or not isinstance(context, dict):
        return failure_class
    if str(context.get("failure_category") or "").lower() == "scope_overflow":
        return "SCOPE_OVERFLOW"
    return failure_class


async def _relock_orphan_task(
    session,
    task_id,
    cached_task: Task | None,
    expected_status,
    expected_last_heartbeat,
    *,
    vm_liveness_cutoff: datetime | None = None,
) -> Task | None:
    """Re-acquire FOR UPDATE on an orphan task and validate the pre-IO snapshot.

    Mirrors ``rollup_management._relock_parent`` (canonical reference) and
    ``ci_completion._relock_merge_task``: expire-then-populate_existing-SELECT
    handles the cached-identity-map staleness under async + skip_locked.

    Returns the freshly-loaded Task if:
      - the row is still ours (not held by another writer), AND
      - status is unchanged (still PROVISIONING or IN_PROGRESS, not COMPLETED/FAILED), AND
      - last_heartbeat hasn't advanced (agent didn't revive during the unlocked window), AND
      - no fresh ``vmcontact`` landed during the unlocked window (heartbeat
        false-reap fix: the agent's last_heartbeat write may be lock-starved, so
        a contact within ``vm_liveness_cutoff`` proves the VM is alive even
        though ``last_heartbeat`` is unchanged).

    Returns None on any of those conditions failing — caller should ``continue``
    to the next orphan task.
    """
    if cached_task is not None:
        try:
            session.expire(cached_task)
        except InvalidRequestError:
            session.expire_all()
    relock_stmt = (
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    fresh = (await session.execute(relock_stmt)).scalars().first()
    if fresh is None:
        logger.info(
            f"[REAPER] Orphan re-lock skipped for task {task_id} — held by peer "
            f"(deferring to next reaper cycle)"
        )
        return None
    if fresh.status not in (TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS):
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} status transitioned "
            f"to {fresh.status} during external I/O — skipping reap"
        )
        return None
    if fresh.last_heartbeat != expected_last_heartbeat:
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} heartbeat advanced "
            f"({expected_last_heartbeat} → {fresh.last_heartbeat}) during external I/O "
            f"— agent revived, skipping reap"
        )
        return None
    # Heartbeat false-reap fix (option B): a contact that landed during the
    # unlocked I/O window aborts the reap even when last_heartbeat is unchanged
    # (the agent's contended last_heartbeat write was skipped by the rollup
    # lock). Gated on the same cutoff: a genuinely dead VM has no fresh contact
    # and is still reaped. Best-effort — never let a lookup error block the reap.
    if vm_liveness_cutoff is not None and fresh.vm_instance_id:
        try:
            _contact_at = (
                await session.execute(
                    select(VmContact.last_contact_at).where(
                        VmContact.vm_instance_id == fresh.vm_instance_id
                    )
                )
            ).scalar_one_or_none()
        except Exception as _vc_exc:  # noqa: BLE001
            logger.warning(
                "[REAPER] vm_contact re-lock lookup failed for task %s vm=%s "
                "(non-fatal, proceeding with reap): %s",
                task_id,
                fresh.vm_instance_id,
                _vc_exc,
            )
            _contact_at = None
        if _contact_at is not None and _contact_at >= vm_liveness_cutoff:
            logger.info(
                f"[REAPER] Orphan re-lock found task {task_id} vm "
                f"{fresh.vm_instance_id} contacted the orchestrator at "
                f"{_contact_at} (>= cutoff {vm_liveness_cutoff}) during external "
                f"I/O — agent alive (lock-starved heartbeat), skipping reap"
            )
            return None
    if fresh.status != expected_status:
        # Defensive: status changed within the (PROVISIONING, IN_PROGRESS) set
        # — e.g. claim_task flipped PROVISIONING → IN_PROGRESS. Not the same as
        # a terminal transition, but the reap reason classification was based on
        # the original status, so safer to defer.
        logger.info(
            f"[REAPER] Orphan re-lock found task {task_id} status changed "
            f"({expected_status} → {fresh.status}) during external I/O — skipping reap"
        )
        return None
    return fresh


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


def _mark_reaper_progress() -> None:
    """Heartbeat that the reaper worker is actively progressing within a cycle.

    Liveness (the lifecycle watchdog) is gated on the freshness of this
    timestamp, not on full-cycle completion, so a legitimately long cycle (large
    orphan backlog → many sequential GitHub/VM calls) is not mistaken for a hung
    process and killed. Kept cheap: a single timestamp write, no I/O. Mirrors
    orchestrator._mark_orchestrator_progress (PR #6678)."""
    global _reaper_last_progress
    _reaper_last_progress = _utcnow()


# Cycle-level circuit breaker. After N consecutive whole-cycle exceptions the
# reaper enters HALTED state, stops cycling, and posts an ERROR alert. Resets
# automatically on process restart (in-memory); tunable via runtime_config key
# "reaper_cycle_halt_threshold".
_REAPER_CYCLE_HALT_THRESHOLD: int = 5
# Half-open probe limit: after this many consecutive failed probes the reaper
# escalates to a hard halt that requires operator reset (like the original behavior).
# Tunable via runtime_config key "reaper_half_open_probe_limit".
_REAPER_HALF_OPEN_PROBE_LIMIT: int = 3
_reaper_halt_state: dict = {
    "halted": False,
    "halt_timestamp": None,
    "last_error": None,
    "crash_count": 0,
    # hard_halted=True → terminal; requires operator reset (not auto-recovered).
    # hard_halted=False + halted=True → soft halt; auto-recovers after cooldown.
    "hard_halted": False,
    "half_open_failures": 0,  # consecutive failed half-open probes
}


def get_reaper_stats() -> dict:
    """Snapshot of reaper liveness for /metrics and admin observability.

    ``last_cycle_age_seconds`` is the wall-clock seconds since the last
    fully-successful cycle. -1 means the reaper has never completed a cycle
    (process just started, or every cycle has crashed). Alerts should fire
    if this exceeds the reaper sleep interval by a meaningful margin
    (e.g. >300s — five expected cycles missed).
    """
    age_seconds: float | int = -1
    if _reaper_last_cycle_completed_at is not None:
        age_seconds = max(
            0, (_utcnow() - _reaper_last_cycle_completed_at).total_seconds()
        )
    # In-cycle progress age: seconds since the last _mark_reaper_progress()
    # heartbeat, -1 if never marked. This is the freshest liveness signal under
    # a long cycle (the watchdog judges on it before falling back to cycle age).
    progress_age_seconds: float | int = -1
    if _reaper_last_progress is not None:
        progress_age_seconds = max(
            0, (_utcnow() - _reaper_last_progress).total_seconds()
        )
    return {
        "cycles_completed_total": _reaper_cycles_completed_count,
        "last_cycle_age_seconds": age_seconds,
        "last_progress_age_seconds": progress_age_seconds,
        "halted": _reaper_halt_state["halted"],
        "halt_timestamp": _reaper_halt_state["halt_timestamp"].isoformat()
        if _reaper_halt_state["halt_timestamp"]
        else None,
        "halt_crash_count": _reaper_halt_state["crash_count"],
        "hard_halted": _reaper_halt_state.get("hard_halted", False),
        "half_open_failures": _reaper_halt_state.get("half_open_failures", 0),
    }


def is_reaper_halted() -> bool:
    """Liveness probe: True iff the reaper has self-halted after a crash-loop."""
    return _reaper_halt_state["halted"]


async def _post_reaper_halt_alert(crash_count: int, last_error: str) -> None:
    """Post an ERROR-severity alert to every project when the reaper self-halts."""
    try:
        from app.models import Alert, AlertSeverity

        async with AsyncSessionLocal() as s:
            # Bound the fan-out scan and fan-out insert: a single capped SELECT
            # plus one bulk INSERT instead of an unbounded full-table scan and
            # one ORM add() per project in the error-recovery hot path.
            proj_ids = (await s.execute(select(Project.id).limit(1000))).scalars().all()
            if not proj_ids:
                return
            message = (
                f"Reaper crash-looped {crash_count}x and self-halted. "
                f"Last error: {last_error[:300]}. "
                "No new reap cycles until process restart or operator reset."
            )
            await s.execute(
                insert(Alert),
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": pid,
                        "severity": AlertSeverity.ERROR,
                        "title": "Reaper service HALTED after crash-loop",
                        "message": message,
                    }
                    for pid in proj_ids
                ],
            )
            await s.commit()
    except Exception:
        logger.exception("[REAPER] Failed to post halt alert")


async def _post_reaper_recovery_alert(probe_failures: int) -> None:
    """Post an INFO alert to every project when the reaper autonomously recovers."""
    try:
        from app.models import Alert, AlertSeverity

        async with AsyncSessionLocal() as s:
            # Bound the fan-out scan and fan-out insert (see _post_reaper_halt_alert).
            proj_ids = (await s.execute(select(Project.id).limit(1000))).scalars().all()
            if not proj_ids:
                return
            message = (
                f"Reaper self-recovered after {probe_failures} failed "
                "probe(s). Normal reap cycles have resumed."
            )
            await s.execute(
                insert(Alert),
                [
                    {
                        "id": uuid.uuid4(),
                        "project_id": pid,
                        "severity": AlertSeverity.INFO,
                        "title": "Reaper service recovered autonomously",
                        "message": message,
                    }
                    for pid in proj_ids
                ],
            )
            await s.commit()
    except Exception:
        logger.exception("[REAPER] Failed to post recovery alert")


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

# C3: per-cycle cap on the Step 3/4 stale-EVALUATING sweep. Each parent costs
# either serial GitHub I/O (_check_and_trigger_rollup, Step 3) or a DB-heavy
# requeue (_requeue_parent_task, Step 4). The previous unbounded SELECT could
# pull every stale EVALUATING parent at once, growing the reaper cycle past
# REAPER_HANG_SECONDS under a backlog (the 2026-05-30 watchdog-kill mechanism).
# Generous default; deferred parents re-qualify next cycle (the sweep runs
# every reaper cycle and selects oldest-stale first).
_REAPER_MAX_EVALUATING_PER_CYCLE = int(
    os.getenv("REAPER_MAX_EVALUATING_PER_CYCLE", "100")
)

# Orphan-priority buckets (lower = processed first). Time-sensitive orphans are
# reaped before the cap can truncate the tail.
_ORPHAN_PRIORITY_RATE_LIMITED = 0  # rate-limited / quota — reroute ASAP
_ORPHAN_PRIORITY_SPOT = 1  # spot VM — likelier to be preempted again
_ORPHAN_PRIORITY_PROVISION = 2  # stuck PROVISIONING — never got a heartbeat
_ORPHAN_PRIORITY_DEFAULT = 3  # everything else


def _orphan_priority_key(task) -> int:
    """Cheap priority bucket for one orphan, computed ONLY from already-loaded
    columns (status, latest_status_message, context, vm_spot) — NO extra
    queries. Lower number = processed first so time-sensitive orphans survive a
    truncating cap. Pure + side-effect-free for unit testing.

    Order: rate-limited (0) < spot (1) < provision-failure (2) < other (3).
    """
    # 0: rate-limited / quota — reroute to a fresh pool account ASAP.
    msg = (getattr(task, "latest_status_message", None) or "").lower()
    ctx = getattr(task, "context", None) or {}
    failure_category = ctx.get("failure_category")
    if (
        "rate" in msg
        or "429" in msg
        or "quota" in msg
        or failure_category == RATE_LIMIT
    ):
        return _ORPHAN_PRIORITY_RATE_LIMITED
    # 1: spot VMs — cheaper but preemptible; recover before they churn again.
    if getattr(task, "vm_spot", None) is True:
        return _ORPHAN_PRIORITY_SPOT
    # 2: provision-failures — VM died before first heartbeat; no agent state lost.
    if getattr(task, "status", None) == TaskStatus.PROVISIONING:
        return _ORPHAN_PRIORITY_PROVISION
    # 3: everything else.
    return _ORPHAN_PRIORITY_DEFAULT


def _select_orphans_for_cycle(tasks: list, cap: int) -> tuple[list, int]:
    """Sort ``tasks`` by ascending priority bucket and return the top ``cap``
    plus the number deferred. Sort is stable (Python's sort), so within a bucket
    the original orphan-detection order is preserved. Pure helper so the
    cap+priority logic is unit-testable without the full reaper loop.

    Returns ``(selected, deferred_count)``. A non-positive cap is treated as
    "no cap" (process everything) to fail safe if misconfigured.
    """
    ordered = sorted(tasks, key=_orphan_priority_key)
    if cap is None or cap <= 0 or len(ordered) <= cap:
        return ordered, 0
    return ordered[:cap], len(ordered) - cap


def _vm_contact_within_cutoff(
    task, hb_cutoff: datetime, vm_contact_lookup: dict | None
) -> bool:
    """True iff the orchestrator heard from this task's VM within ``hb_cutoff``.

    Heartbeat false-reap fix (option B): the heartbeat handler upserts an
    uncontended ``vmcontact`` row keyed on ``vm_instance_id`` BEFORE it touches
    the contended Task row, so a VM that is alive-but-lock-starved (its
    ``Task.last_heartbeat`` write was skipped while the rollup held the task row
    FOR UPDATE) still leaves a fresh ``last_contact_at``. This override is gated
    on the SAME heartbeat-stale cutoff used for ``last_heartbeat``: a genuinely
    dead agent (crashed / preempted / OOM — sends no requests) has a stale or
    absent contact row and is reaped exactly as before. Old / absent rows fall
    back to the existing ``last_heartbeat`` behaviour (no regression).
    """
    if not vm_contact_lookup:
        return False
    vm_id = getattr(task, "vm_instance_id", None)
    if not vm_id:
        return False
    last_contact = vm_contact_lookup.get(vm_id)
    return last_contact is not None and last_contact >= hb_cutoff


def _vm_tool_use_within_cutoff(
    task, progress_cutoff: datetime, vm_tool_use_lookup: dict | None
) -> bool:
    """True iff the orchestrator heard a tool-use from this VM after ``progress_cutoff``.

    Fix 3b: the PROGRESS-stall branch reaps a fresh-heartbeating agent whose
    on-row ``Task.last_tool_use_at`` is stale. But that on-row write shares the
    contended task row with ``last_heartbeat`` — a lock-starved heartbeat (the
    rollup holding the task row FOR UPDATE) drops BOTH, so an agent that IS
    making tool calls can read as stalled. The heartbeat handler upserts the
    most-recent tool-use OFF the task row onto ``vmcontact.last_tool_use_at``
    (keyed on ``vm_instance_id``), so a tool-use within the SAME progress-stale
    cutoff proves the agent is progressing (its on-row stamp was merely
    lock-dropped) and the stall reap is suppressed. A genuinely stalled agent
    (no tool calls anywhere) leaves this stale/absent and is still reaped.
    Unlike the heartbeat-stale ``_vm_contact_within_cutoff`` override, this does
    NOT key on the mere fact of contact (a hung agent still heartbeats) — it
    keys on actual TOOL-USE recency, so genuine stall detection is preserved.
    """
    if not vm_tool_use_lookup:
        return False
    vm_id = getattr(task, "vm_instance_id", None)
    if not vm_id:
        return False
    last_tool_use = vm_tool_use_lookup.get(vm_id)
    return last_tool_use is not None and last_tool_use >= progress_cutoff


def _classify_orphan(
    task,
    now: datetime,
    has_open_pr: bool,
    vm_contact_lookup: dict | None = None,
    vm_tool_use_lookup: dict | None = None,
) -> bool:
    """Single source of truth for the orphan-detection state machine.

    Returns ``True`` iff ``task`` (a PROVISIONING / IN_PROGRESS row that already
    owns a vm_instance_id, or a degenerate other-status catch-all) is an orphan
    that should be reaped, given the current clock and whether it has an open PR.

    Previously this predicate was DUPLICATED verbatim between the Step-1 lock-free
    scan (which collected ``task.id``) and the Step-2 post-lock re-verification
    (which collected the locked ORM object). Two copies drifting out of sync would
    silently change recovery outcomes, so both call sites now route through here.
    Pure + side-effect-free (no logging, no queries) so it is unit-testable and so
    the caller owns any per-site logging.

    ``vm_contact_lookup`` is an optional ``{vm_instance_id: last_contact_at}`` map
    (batch-fetched ONCE per cycle by the caller, never one query per task). On any
    heartbeat-stale branch that would reap, a contact within the SAME
    ``hb_cutoff`` overrides the reap (the agent is alive; its ``last_heartbeat``
    write was lock-starved). Absent / stale contact → existing behaviour.

    ``vm_tool_use_lookup`` is an optional ``{vm_instance_id: last_tool_use_at}``
    map (batch-fetched from the SAME query, Fix 3b). On the PROGRESS-stall
    branches that would reap (``agent_stalled``), a tool-use within the
    ``_progress_stale_cutoff`` overrides the reap — the agent IS making tool
    calls; its on-row ``last_tool_use_at`` write was lock-starved together with
    the heartbeat. Unlike ``vm_contact_lookup`` it keys on tool-use recency, NOT
    mere contact (a hung agent still heartbeats), so genuine stall detection is
    preserved. Absent / stale tool-use → existing behaviour (reap).

    State machine (unchanged):
      PROVISIONING:
        - started_at > 15min ago                        → absolute provision ceiling
        - last_heartbeat older than hb_cutoff            → claimed-then-silent (rare)
        - vm_provisioned_at past preclaim cutoff + no hb → silent pre-claim death
      IN_PROGRESS:
        - last_heartbeat older than hb_cutoff            → agent crashed mid-run
        - no hb, started_at older than hb_cutoff         → post-claim crash (defensive)
        - no hb, no started_at, created >10min ago       → degenerate catch-all
        - fresh hb but last_tool_use_at stale (+started)  → agent stalled (tool clock)
        - fresh hb, no tool use ever, started stale, anthropic → agent stalled (Codex gate)
      any other status: created >10min ago               → catch-all
    """
    ten_minutes_ago = now - timedelta(minutes=10)
    fifteen_minutes_ago = now - timedelta(minutes=15)
    hb_cutoff = _heartbeat_stale_cutoff(task, now, has_open_pr=has_open_pr)

    if task.status == TaskStatus.PROVISIONING:
        if task.started_at and task.started_at < fifteen_minutes_ago:
            return True  # absolute provisioning ceiling
        if task.last_heartbeat and task.last_heartbeat < hb_cutoff:
            # Rare: agent claimed during PROVISIONING (shouldn't happen since
            # claim_task also flips status to IN_PROGRESS), then went silent.
            # vm_contact override: a fresh contact within hb_cutoff means the VM
            # is alive (lock-starved last_heartbeat write) — don't reap.
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
        if (
            task.vm_provisioned_at
            and task.vm_provisioned_at < _preclaim_stale_cutoff(task, now)
            and task.last_heartbeat is None
        ):
            # Silent pre-claim death: VM was dispatched long enough ago
            # (scope-aware: small 6min, medium 8min, large 10min) and the
            # agent never claimed. Charge provision budget. vm_contact override:
            # if the agent has actually been calling in within hb_cutoff it is
            # NOT a silent death — suppress the reap.
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
        return False

    if task.status == TaskStatus.IN_PROGRESS:
        if task.last_heartbeat and task.last_heartbeat < hb_cutoff:
            # Agent crashed mid-run — UNLESS we heard from the VM within the
            # same cutoff (lock-starved last_heartbeat write); then it's alive.
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
        if (
            task.last_heartbeat is None
            and task.started_at
            and task.started_at < hb_cutoff
        ):
            # Defensive: IN_PROGRESS without any heartbeat shouldn't happen
            # (claim_task sets last_heartbeat at agent.py:312) but if we hit
            # it, treat as a post-claim crash. Use scope-aware cutoff so
            # large/high-effort tasks aren't reaped prematurely. vm_contact
            # override: a fresh contact within hb_cutoff proves the VM is alive
            # (its last_heartbeat write was lock-starved), so don't reap.
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
        if (
            task.last_heartbeat is None
            and task.started_at is None
            and task.created_at < ten_minutes_ago
        ):
            # Fully-degenerate IN_PROGRESS row (no heartbeat, no started_at)
            # that's been alive for >10 min — data corruption or a path that
            # forgot to set started_at. Reap as a catch-all. vm_contact override:
            # if the VM is demonstrably alive (fresh contact within hb_cutoff)
            # this is not a dead-VM orphan — suppress the reap.
            return not _vm_contact_within_cutoff(task, hb_cutoff, vm_contact_lookup)
        if (
            task.last_heartbeat
            and task.last_heartbeat >= hb_cutoff
            and task.last_tool_use_at is not None
            and task.last_tool_use_at < _progress_stale_cutoff(task, now)
            and (
                task.started_at is None
                or task.started_at < _progress_stale_cutoff(task, now)
            )
        ):
            # Stalled-but-heartbeating: agent process is alive (fresh
            # heartbeat) but has made no tool/LLM calls for longer than
            # the scope-aware progress threshold. Indicates a hung LLM
            # call, a spinning retry loop, or a wedged tool invocation.
            #
            # NO vm_contact (last_contact_at) override here: this is a
            # PROGRESS-stale reap, not a heartbeat-stale one — the heartbeat IS
            # fresh (last_heartbeat >= hb_cutoff), so a mere contact would
            # likewise be fresh and suppressing on it would defeat genuine stall
            # detection. The contact override only covers the last_heartbeat
            # false-reap (lock-starved write).
            #
            # Fix 3b: a TOOL-USE override IS valid here. The on-row
            # ``last_tool_use_at`` write shares the contended task row with
            # ``last_heartbeat``, so a lock-starved heartbeat drops it too — an
            # agent that IS calling tools can read as stalled. The off-row
            # ``vmcontact.last_tool_use_at`` carries the real progress signal:
            # if it's within the progress-stale cutoff the agent is progressing,
            # so don't reap. Keyed on tool-use recency (not mere contact), so a
            # genuinely stalled-but-heartbeating agent (no recent tool use
            # anywhere) is still reaped.
            #
            # Cross-attempt stale-tool guard (8c8cf973 incident):
            # last_tool_use_at carries forward across retries — if
            # only this field is checked, every retry is born stale
            # and reaped within seconds (8c8cf973 attempt 3 reaped
            # 22s after starting because tool_use_at was 11min old
            # from attempt 1). The started_at check ensures the
            # current attempt has actually had time to call tools.
            return not _vm_tool_use_within_cutoff(
                task, _progress_stale_cutoff(task, now), vm_tool_use_lookup
            )
        if (
            task.last_heartbeat
            and task.last_heartbeat >= hb_cutoff
            and task.last_tool_use_at is None
            and task.started_at
            and task.started_at < _progress_stale_cutoff(task, now)
            and (task.context or {}).get("llm_provider") == "anthropic"
        ):
            # Stalled-but-heartbeating with zero tool calls ever.
            # Provider-gated to anthropic only because the agent
            # wrapper's outputMode='text' path (Codex/Gemini at
            # agent/src/index.ts:3061,3119) never calls
            # recordToolUse — only the claude-stream-json parser
            # does. So last_tool_use_at IS NULL for every
            # Codex/Gemini task by design, regardless of whether
            # tools were called. Without this gate the watchdog
            # reaps healthy non-anthropic work after 8 min.
            # TODO: parse Codex --json events / Gemini events
            # to populate tool_trace, then drop the gate.
            #
            # Fix 3b: even with on-row last_tool_use_at NULL, an off-row
            # ``vmcontact.last_tool_use_at`` within the progress cutoff means
            # the agent DID record a tool use that the lock-starved heartbeat
            # failed to land on the task row — it is progressing, don't reap.
            return not _vm_tool_use_within_cutoff(
                task, _progress_stale_cutoff(task, now), vm_tool_use_lookup
            )
        return False

    # Any other status (degenerate row left in an active-scan status): catch-all.
    return task.created_at < ten_minutes_ago


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


from app.retry_prompt import _copy_analysis_artifacts_to_previous  # noqa: E402, F401, RUF100

_HEARTBEAT_STALE_DEFAULTS = {
    "small": 300,
    "medium": 420,
    "large": 900,
}


_HEARTBEAT_EFFORT_FLOORS = {
    "low": 300,
    "medium": 300,
    "high": 420,
    "max": 720,
}


_PRECLAIM_STALE_DEFAULTS = {
    "small": 360,
    "medium": 480,
    "large": 600,
}

# Progress-staleness thresholds: how long a fresh-heartbeating task may go
# without any tool/LLM event before being classified as agent_stalled.
# Longer than heartbeat-stale to avoid false positives on normal LLM think time.
_PROGRESS_STALE_DEFAULTS = {
    "small": 480,  # 8 min
    "medium": 720,  # 12 min
    "large": 1080,  # 18 min
}

# Task types where the agent legitimately spends 5-20 min reasoning before
# emitting a tool call (b2410541 incident: Claude Opus on strategic_review
# was reaped after 484s while in the middle of pass-1 thinking — actual
# attempt completed cleanly in 19 min once given a budget). These types
# get a flat longer cutoff regardless of nominal scope.
# Memo (pure-reasoning documentation tasks with no repo) may run 5-10+ min
# producing a reasoning artifact without any tool calls — not a sign of staleness.
_THINKING_HEAVY_TASK_TYPES = frozenset(
    {
        "strategic_review",
        "design_doc_update",
        "research",
        "investigate",
        "plan",
        "memo",
    }
)
_THINKING_HEAVY_PROGRESS_STALE_SECONDS = 1500  # 25 min


def _progress_stale_cutoff(task, now: datetime) -> datetime:
    """Return cutoff time for meaningful-progress (last_tool_use_at) staleness.

    Only fires when last_heartbeat IS fresh (agent alive but wedged). Uses
    longer thresholds than heartbeat-stale to tolerate normal LLM latency.
    Thinking-heavy task types override scope-based defaults — reasoning
    models on strategic/research work spend long stretches between tool
    calls (8-min cutoff misclassifies normal Opus reasoning as stalled).
    """
    ctx = task.context or {}
    payload = task.first_payload or {}
    task_type = payload.get("type") or ""
    if task_type in _THINKING_HEAVY_TASK_TYPES:
        thinking_override = _runtime_config.get(
            "progress_stale_seconds_thinking_heavy",
            _THINKING_HEAVY_PROGRESS_STALE_SECONDS,
        )
        return now - timedelta(seconds=thinking_override)
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "progress_stale_seconds_by_scope", _PROGRESS_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 480))
    return now - timedelta(seconds=seconds)


def _extract_tool_trace_tail(task) -> str:
    """Extract and format the tail of _tool_trace from task.context.

    Returns a compact formatted string of recent tool calls (timestamp + tool name
    + first ~80 chars of input preview), limited to ~400 chars total. If _tool_trace
    is missing or empty, returns empty string.
    """
    ctx = task.context or {}
    tool_trace = ctx.get("_tool_trace") or []
    if not tool_trace:
        return ""

    # Take the last 5 entries to stay within 400-char budget
    entries = tool_trace[-5:] if len(tool_trace) > 5 else tool_trace
    parts = []
    total_len = 0
    max_total = 400

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        t_str = entry.get("t", "").split("T")[-1][:8]  # HH:MM:SS from ISO-8601
        tool_name = entry.get("tool", "?")[:20]
        input_preview = entry.get("input_preview", "")[:80]
        # Compact format: timestamp + tool + input
        part = f"{t_str} {tool_name}: {input_preview}"
        if total_len + len(part) + 3 > max_total:  # +3 for " | "
            break
        parts.append(part)
        total_len += len(part) + 3

    if not parts:
        return ""
    return " | ".join(parts)


def _preclaim_stale_cutoff(task, now: datetime) -> datetime:
    ctx = task.context or {}
    payload = task.first_payload or {}
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "preclaim_stale_seconds_by_scope", _PRECLAIM_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 360))
    return now - timedelta(seconds=seconds)


def _heartbeat_stale_cutoff(task, now: datetime, has_open_pr: bool = False) -> datetime:
    # When a task has an open PR, reduce the heartbeat-stale cutoff to 90s
    # to detect post-PR silent deaths faster and hand off to the review
    # pipeline sooner (normally 5-15 min delay; with PR cutoff ~90-150s).
    # The 90s gives one missed heartbeat (60s) plus one reaper cycle (~30s).
    if has_open_pr:
        return now - timedelta(seconds=90)

    ctx = task.context or {}
    payload = task.first_payload or {}
    scope = ctx.get("scope") or payload.get("scope")
    thresholds = _runtime_config.get(
        "heartbeat_stale_seconds_by_scope", _HEARTBEAT_STALE_DEFAULTS
    )
    seconds = thresholds.get(scope, thresholds.get("small", 300))

    effort = ctx.get("effort") or payload.get("effort")
    if not effort:
        task_type = ctx.get("type") or payload.get("type")
        if task_type:
            # Resolve effort from the provider settings bag: merge Anthropic
            # defaults with any per-task-type override. Reaper needs a scalar
            # effort to size the heartbeat-stale floor; other providers'
            # effort-like knobs (reasoning_effort, thinking_budget) aren't
            # normalized so we look at Anthropic only, which is the dispatch
            # default.
            defaults = _runtime_config.get("provider_settings_defaults", {}).get(
                "anthropic", {}
            )
            overrides = (
                _runtime_config.get("task_type_provider_overrides", {})
                .get(task_type, {})
                .get("anthropic", {})
            )
            effort = overrides.get("effort") or defaults.get("effort")
    floor = _HEARTBEAT_EFFORT_FLOORS.get(effort, 0)
    seconds = max(seconds, floor)
    return now - timedelta(seconds=seconds)


async def _task_has_open_pr(task, session) -> tuple[bool, int | None]:
    """Check whether a task has a PULL_REQUEST artifact (PR was published).

    NOTE: Despite the name, this only checks the Artifact table — it does
    NOT verify the PR is still open on GitHub. The PR may have been closed
    or merged out-of-band. Callers that act on "open" must re-confirm via
    get_pull_request before parking the task in EVALUATING.

    Used by the reaper: a task that pushed a branch and opened a PR before
    being reaped should hand off to the review pipeline (EVALUATING) rather
    than retry from scratch (QUEUED) — the retry path would collide with the
    existing branch and be refused by the agent's stale-branch guard
    (agent/src/index.ts:706), orphaning the PR.

    The Artifact table is written atomically with PR creation (see
    routers/agent.py:2035). Returns (True, pr_number) on match.
    """
    stmt = select(Artifact).where(
        Artifact.task_id == task.id,
        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
    )
    artifact = (await session.execute(stmt)).scalars().first()
    if artifact is None or not artifact.pr_url:
        return False, None
    # Parse PR number from the URL (e.g. https://github.com/owner/repo/pull/2903)
    try:
        pr_num = int(artifact.pr_url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return False, None
    return True, pr_num


from app.reaper_sweeps import (  # noqa: E402
    sweep_queued_age_alerts,
    sweep_stalled_strategic_cycles,
    sweep_regression_scan,
    sweep_stale_migration_reservations,
    sweep_stuck_qualifications,
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
    sweep_deliver_task_wakes,
    sweep_wake_delivery_stall_alerts,
)

# Zero-cycle starvation: a project that should be running the strategic loop but
# has produced NO StrategicCycle row in this window emits a deduped
# ``strategic_cycle_stall`` WARNING Alert. Distinct from the in-progress-but-stuck
# detection handled by ``sweep_stalled_strategic_cycles`` — this catches the
# strategic engine never spawning a cycle at all (the deleted regression-dispatcher
# ``strategic_cycle_stall`` signal used to surface this; it is now a plain Alert).
_STRATEGIC_CYCLE_STARVATION_DEFAULT_HOURS = 24
_STRATEGIC_CYCLE_STALL_ALERT_TITLE = "Strategic engine starved: no cycle in window"


async def _sweep_strategic_cycle_starvation(session, now: datetime) -> int:
    """Emit a deduped strategic_cycle_stall Alert for autonomous projects with no
    recent StrategicCycle row at all. Returns the number of alerts created."""
    from app.models import Alert, AlertSeverity, Project, StrategicCycle

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


async def reaper_service():
    """
    Background worker that scans for orphaned VMs.
    Marks them as FAILED and issues a delete command to GCP if they haven't sent a heartbeat in 5 minutes.
    """
    # Deferred imports to avoid circular dependency with app.main
    from app.max_plan_pool import (
        clear_task_llm_runtime_context,
        db_account_was_rate_limited,
        db_pool_wide_rate_limit_cooldown,
        disable_account_model,
        disable_account_for_auth,
        get_task_llm_account_id,
        get_task_rate_limit_scope,
        _extract_task_model,
        release_max_plan_account,
    )
    from app.vm_lifecycle import _ensure_vm_deleted
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.routers.agent_status import record_task_result
    from app.broadcast import broadcast_to_project
    from app.task_lifecycle import _requeue_parent_task
    from app.rollup_management import _check_and_trigger_rollup

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
            cooldown = min(cooldown_base * (2**half_open_failures), 3600)
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
                stmt = (
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
                        raiseload=True,)
                    )
                    .where(
                        Task.status.in_(
                            [TaskStatus.PROVISIONING, TaskStatus.IN_PROGRESS]
                        )
                    )
                    .limit(_BATCH)
                )
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
                            select(Artifact).where(
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
                tasks_with_pr = {a.task_id for a in pr_artifacts}

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
                    verified = [
                        t
                        for t in orphaned_tasks
                        if _classify_orphan(
                            t,
                            now,
                            has_open_pr=(t.id in tasks_with_pr),
                            vm_contact_lookup=step2_contact_lookup,
                            vm_tool_use_lookup=step2_tool_use_lookup,
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
                if orphaned_tasks:
                    _orphaned_task_ids = [t.id for t in orphaned_tasks]
                    await session.commit()
                    session.expire_all()

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
                        # between the initial query and now
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
                                if task.vm_instance_id:
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
                        if fresh_task.vm_instance_id:
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

                        # Capture pre-IO snapshot so the relock below can detect peer
                        # mutation during the unlocked window. status + last_heartbeat
                        # are the two fields the heartbeat path writes; an advance in
                        # either signals the agent revived and the reap should be aborted.
                        pre_io_status = fresh_task.status
                        pre_io_last_hb = fresh_task.last_heartbeat

                        # 1. Spot preemption check (GCP API)
                        is_spot_preemption = False
                        prov_zone_for_status: str | None = None
                        io_vm_status: str | None = None
                        io_orch_delete_info: tuple[bool, str] = (False, "")
                        if (
                            getattr(fresh_task, "vm_spot", False)
                            and fresh_task.vm_instance_id
                            and reap_reason_code
                            in (
                                "heartbeat_timeout_post_claim",
                                "provision_timeout_pre_claim",
                                # absolute_timeout: a PROVISIONING task that aged past
                                # the 15-min absolute ceiling before earlier reaper
                                # codes had a chance to classify it (typically because
                                # the reaper itself was lagging — e.g., 2026-05-07
                                # MissingGreenlet crash on last_tool_use_at). Without
                                # this branch, the spot check is skipped and the
                                # account gets blacklisted for what's actually a GCE
                                # capacity issue.
                                "absolute_timeout",
                            )
                        ):
                            prov_zone_for_status = (task.context or {}).get(
                                "provision_zone"
                            ) or get_vm_zone(fresh_task.vm_instance_id)
                            if prov_zone_for_status:
                                try:
                                    io_vm_status = await asyncio.to_thread(
                                        get_vm_status,
                                        fresh_task.vm_instance_id,
                                        prov_zone_for_status,
                                    )
                                    # TERMINATED = preempted by GCP. DELETED = already gone.
                                    # DELETED is ambiguous: GCP delete-on-preempt OR orchestrator-
                                    # initiated cleanup (agent_completion, terminal_transition,
                                    # admin_requeue, etc). Production 2026-05-04: reaper misclassified
                                    # orchestrator-deleted VMs as preemptions, inflating
                                    # spot_preemption_count for tasks that were really completed
                                    # but whose status POST 500'd during a cutover.
                                    # Fix: consult the orchestrator-initiated-delete tracker. If the
                                    # VM was deleted by us within the last 30min, it's NOT a preemption.
                                    from app.vm_lifecycle import (
                                        is_orchestrator_initiated_delete,
                                    )

                                    io_orch_delete_info = (
                                        is_orchestrator_initiated_delete(
                                            fresh_task.vm_instance_id
                                        )
                                    )
                                    if io_orch_delete_info[0]:
                                        logger.info(
                                            f"[REAPER] Task {task.id} on SPOT VM {fresh_task.vm_instance_id} "
                                            f"vm_status={io_vm_status} but orchestrator-initiated delete "
                                            f"(reason={io_orch_delete_info[1]}) — NOT classifying as preemption"
                                        )
                                    elif io_vm_status in ("TERMINATED", "DELETED"):
                                        is_spot_preemption = True
                                        logger.warning(
                                            f"[REAPER] Task {task.id} on SPOT VM {fresh_task.vm_instance_id} "
                                            f"in {prov_zone_for_status} — vm_status={io_vm_status}, treating "
                                            f"as preemption (skipping attempt_count++)"
                                        )
                                except Exception as e:  # noqa: BLE001
                                    logger.debug(
                                        f"[REAPER] get_vm_status failed for {fresh_task.vm_instance_id}: {e}"
                                    )

                        # 1.5. Durable DB rate-limit check (cross-instance fallback).
                        # The in-memory classify_failure above consults only
                        # _account_was_rate_limited (same-process cache). When the
                        # cooldown was recorded on a different Cloud Run instance or
                        # before a restart, the cache is empty and the task would be
                        # silently misclassified CODE_ERROR. Augment the flag with
                        # durable DB state before the account-disable I/O step so all
                        # reaper deaths get the same cross-instance treatment that
                        # agent_status.py already applies on the agent-reported path.
                        # Skip DB rate-limit fallback when dead-auth is already
                        # confirmed — the account needs a hard-disable, not a cooldown.
                        if (
                            not is_rate_limited
                            and not is_dead_auth
                            and not is_spot_preemption
                        ):
                            _db_rl_acct = get_task_llm_account_id(fresh_task)
                            _at_time_rl = (
                                fresh_task.last_heartbeat or fresh_task.started_at
                            )
                            _db_rl_scope = get_task_rate_limit_scope(fresh_task)
                            if _db_rl_acct:
                                if await db_account_was_rate_limited(
                                    _db_rl_acct, at_time=_at_time_rl
                                ):
                                    is_rate_limited = True
                                    logger.info(
                                        "[REAPER] Task %s reclassified RATE_LIMIT via "
                                        "durable DB state (account=%s) — in-memory cache missed",
                                        fresh_task.id,
                                        _db_rl_acct,
                                    )
                            elif await db_pool_wide_rate_limit_cooldown(
                                at_time=_at_time_rl,
                                providers=_db_rl_scope.get("providers"),
                                model=_db_rl_scope.get("model"),
                                user_id=_db_rl_scope.get("user_id"),
                            ):
                                is_rate_limited = True
                                logger.info(
                                    "[REAPER] Task %s (no account) reclassified RATE_LIMIT "
                                    "via pool-wide DB state — pool was fully blocked at failure time",
                                    fresh_task.id,
                                )

                        # 2. Rate-limited account-disable I/O (called only when is_rate_limited
                        #    AND not is_spot_preemption; mirrors the original elif chain where
                        #    the rate-limited branch was skipped if spot preempt fired first).
                        #    Side-effect-only — disables the account/model in the in-memory pool
                        #    + persists to DB via its own internal session.
                        rl_acct_id: str | None = None
                        rl_model: str | None = None
                        if is_rate_limited and not is_spot_preemption:
                            rl_acct_id = get_task_llm_account_id(task)
                            logger.info(
                                f"[REAPER] Task {task.id} rate-limited (account={rl_acct_id}) — "
                                f"requeuing immediately with account blacklisted"
                            )
                            # Disable the exhausted model on the account so the orchestrator's
                            # blacklist pruning preserves this entry on the next dispatch cycle.
                            rl_model = _extract_task_model(task)
                            if not rl_model:
                                _rl_task_type = (task.first_payload or {}).get(
                                    "type", "create"
                                )
                                rl_model = _runtime_config.get(
                                    "task_type_models", {}
                                ).get(_rl_task_type)
                            if rl_acct_id and rl_model:
                                _reaper_ctx_io = task.context or {}
                                _reaper_kwargs_io = {
                                    "retry_after_seconds": _reaper_ctx_io.get(
                                        "rate_limit_retry_after_seconds"
                                    ),
                                    "kind": _reaper_ctx_io.get("rate_limit_kind")
                                    or "rate_limit",
                                }
                                try:
                                    await disable_account_model(
                                        rl_acct_id, rl_model, **_reaper_kwargs_io
                                    )
                                except Exception as _disable_err:  # noqa: BLE001
                                    logger.warning(
                                        f"[REAPER] disable_account_model failed for {rl_acct_id}/{rl_model}: "
                                        f"{_disable_err}; retrying in 1s"
                                    )
                                    await asyncio.sleep(1)
                                    try:
                                        await disable_account_model(
                                            rl_acct_id, rl_model, **_reaper_kwargs_io
                                        )
                                    except Exception as _disable_err2:  # noqa: BLE001
                                        logger.error(  # noqa: TRY400
                                            f"[REAPER] disable_account_model retry also failed for "
                                            f"{rl_acct_id}/{rl_model}: {_disable_err2}. "
                                            "Account remains active in pool — tight re-selection loop risk."
                                        )

                        # 2b. Dead-auth account hard-disable I/O.
                        # Called only when is_dead_auth AND not is_spot_preemption.
                        # Uses disable_account_for_auth (permanent hard-disable, no
                        # auto-recover) rather than disable_account_model (cooldown).
                        da_acct_id: str | None = None
                        if is_dead_auth and not is_spot_preemption:
                            da_acct_id = get_task_llm_account_id(task)
                            if da_acct_id:
                                _da_pattern = (task.context or {}).get(
                                    "dead_auth_matched_pattern", "dead_oauth_session"
                                )
                                _da_reason = f"dead-auth: {_da_pattern}"
                                logger.warning(
                                    "[REAPER] Task %s dead-auth (account=%s pattern=%r) — "
                                    "hard-disabling account fleet-wide; requeuing onto different account",
                                    task.id,
                                    da_acct_id,
                                    _da_pattern,
                                )
                                try:
                                    await disable_account_for_auth(
                                        da_acct_id, _da_reason
                                    )
                                except Exception as _da_err:  # noqa: BLE001
                                    logger.warning(
                                        "[REAPER] disable_account_for_auth failed for %s: %s; retrying in 1s",
                                        da_acct_id,
                                        _da_err,
                                    )
                                    await asyncio.sleep(1)
                                    try:
                                        await disable_account_for_auth(
                                            da_acct_id, _da_reason
                                        )
                                    except Exception as _da_err2:  # noqa: BLE001
                                        logger.error(  # noqa: TRY400
                                            "[REAPER] disable_account_for_auth retry also failed for %s: %s. "
                                            "Account remains active — dead-auth re-selection risk.",
                                            da_acct_id,
                                            _da_err2,
                                        )

                        # 3. Provision-failure serial capture I/O (GCP serial console).
                        # Run unconditionally on provision failures: the spot-preempt
                        # branch falls through to the provision-failure mutation block
                        # below (which uses io_serial_log), so we need the capture
                        # whenever is_provision_failure regardless of spot/rate-limit
                        # branch. Tiny perf cost when spot/rate fast-paths skip the
                        # mutation block, but avoids re-introducing the held-lock-during-IO
                        # bug as a fallback path.
                        io_serial_log: str | None = None
                        io_prov_zone: str | None = None
                        io_provision_err: str | None = None
                        if is_provision_failure:
                            # Resolve zone (in-memory cache + persisted fallback). The two
                            # values prov_zone and provision_err are stable post-fail; reading
                            # them while unlocked is safe — no peer mutates them.
                            if fresh_task.vm_instance_id:
                                cached = get_vm_zone(fresh_task.vm_instance_id)
                                if cached and cached != "unknown":
                                    io_prov_zone = cached
                            if not io_prov_zone:
                                io_prov_zone = (task.context or {}).get(
                                    "provision_zone"
                                )
                            io_provision_err = get_provision_error(
                                fresh_task.vm_instance_id or ""
                            )
                            if io_prov_zone and fresh_task.vm_instance_id:
                                try:
                                    io_serial_log = await asyncio.to_thread(
                                        _capture_serial_for_vm,
                                        fresh_task.vm_instance_id,
                                        io_prov_zone,
                                    )
                                    if io_serial_log:
                                        logger.warning(
                                            f"[REAPER] Serial log for dying agent {fresh_task.vm_instance_id} "
                                            f"(task {task.id}):\n{io_serial_log}"
                                        )
                                except Exception:
                                    logger.exception(
                                        f"[REAPER] Failed to capture serial log for VM {fresh_task.vm_instance_id}"
                                    )

                        # 4. PR-state lookup I/O (GitHub API)
                        _has_pr_artifact, _pr_num = await _task_has_open_pr(
                            task, session
                        )
                        io_gh_pr_state: str | None = None
                        io_gh_pr_merged: bool = False
                        io_gh_pr_base_branch: str | None = None
                        if _has_pr_artifact and _pr_num:
                            try:
                                from app.routers.agent import get_github_token_for_task
                                from app.github import get_pull_request

                                (
                                    _gh_token,
                                    _gh_owner,
                                    _gh_repo,
                                ) = await get_github_token_for_task(task.id, session)
                                _pr_data = await get_pull_request(
                                    _gh_token, _gh_owner, _gh_repo, _pr_num
                                )
                                io_gh_pr_state = _pr_data.get("state")
                                io_gh_pr_merged = bool(_pr_data.get("merged"))
                                io_gh_pr_base_branch = (_pr_data.get("base") or {}).get(
                                    "ref"
                                )
                            except Exception as _gh_exc:  # noqa: BLE001
                                # GH API unreachable: fall back to "open" — handing off
                                # to review pipeline is the safer default.
                                logger.warning(
                                    f"[REAPER] PR-state lookup failed for task {task.id} PR #{_pr_num}: "
                                    f"{type(_gh_exc).__name__}: {_gh_exc} — assuming open",
                                    exc_info=True,
                                )
                                io_gh_pr_state = "open"

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
                        relocked = await _relock_orphan_task(
                            session,
                            task.id,
                            fresh_task,
                            pre_io_status,
                            pre_io_last_hb,
                            vm_liveness_cutoff=_relock_liveness_cutoff,
                        )
                        if relocked is None:
                            # Peer is finalizing this task or agent revived; skip safely.
                            # Don't add to _reaper_old_vms-driven cleanup either — let
                            # the peer (or next reaper cycle) handle it.
                            _reaper_old_vms.pop(str(task.id), None)
                            continue
                        task = relocked
                        fresh_task = relocked

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
                            await release_max_plan_account(acct_id)
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
                                zone=prov_zone_for_status,
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
                            task.retry_after_until = None
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
                            # Audit W1: retry_after_until is a column now.
                            task.retry_after_until = None
                            # Rate-limit is an account-level issue, not a provision failure —
                            # don't carry provision budget into the next dispatch.
                            rl_ctx.pop("provision_failures", None)
                            task.context = rl_ctx
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
                            _proj = await session.get(Project, task.project_id)
                            (
                                _record_pr_merged,
                                _terminal_rollup_merge,
                                _landed_branch,
                            ) = await classify_rollup_merge_outcome(
                                task,
                                _proj,
                                session,
                                pr_number=_pr_num,
                                merged_base_branch=_gh_pr_base_branch,
                                source="reaper.orphan_recovery",
                            )
                            task.status = (
                                TaskStatus.COMPLETED
                                if _terminal_rollup_merge
                                else TaskStatus.EVALUATING
                            )
                            task.vm_instance_id = None
                            task.last_heartbeat = None
                            if _terminal_rollup_merge:
                                task.latest_status_message = f"Reaped mid-run; PR #{_pr_num} was merged out-of-band"
                            else:
                                task.latest_status_message = (
                                    f"Reaped mid-run; PR #{_pr_num} merged into "
                                    f"{_landed_branch}; awaiting downstream landing"
                                )
                            if _record_pr_merged:
                                apply_merged_landed_signal(
                                    task,
                                    _proj,
                                    base_branch=_landed_branch,
                                    hotfix_lane=bool(
                                        (task.first_payload or {}).get("hotfix")
                                        or (task.context or {}).get("hotfix")
                                    ),
                                    non_merge_reason="merged_pr_non_integration_branch",
                                )
                            try:
                                from app.task_completion import (  # noqa: PLC0415
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
                            children = []
                            if is_parent_type:
                                children = (
                                    (
                                        await session.execute(
                                            select(Task).where(
                                                Task.parent_task_id == task.id
                                            )
                                        )
                                    )
                                    .scalars()
                                    .all()
                                )
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
                                task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                                task.last_heartbeat = None
                                task.started_at = None
                                _pre_strip_reaper_ctx = task.context or {}
                                reaper_ctx = strip_fresh_attempt_context(
                                    _pre_strip_reaper_ctx
                                )
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
                                    delay_minutes = min(
                                        2 ** (task.attempt_count - 1), 15
                                    ) * random.uniform(0.75, 1.25)
                                if task_type == "research":
                                    try:
                                        from app.autonomous_research import (
                                            refresh_research_task_verification,
                                        )

                                        _res_proj = await session.get(
                                            Project, task.project_id
                                        )
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
                                        # C5: a quarantined (FAILED) safeguard must
                                        # also release its cross-cutting merge gate,
                                        # else orig.requires_approval is stranded
                                        # forever. Runs on the same freshly-committed
                                        # _qsession; no-ops for non-safeguard tasks.
                                        if (_qtask.first_payload or {}).get(
                                            "type"
                                        ) == "security_audit":
                                            from app.task_completion import (
                                                _maybe_release_safeguard_gate,
                                            )

                                            await _maybe_release_safeguard_gate(
                                                _qtask, None, _qsession
                                            )
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

                if orphaned_tasks and _orphan_commit_ok:
                    # Re-fetch fresh task state by id before the post-commit loops.
                    # A per-task reap crash above triggers session.rollback() (2211),
                    # which expires EVERY orphaned_tasks ORM object (rollback expires
                    # regardless of expire_on_commit). Reading task.status/.context on
                    # an expired object below drives a sync lazy-refresh on the
                    # AsyncSession -> MissingGreenlet (see 1028-1036). Re-fetching by id
                    # keeps every attribute access inside the async greenlet bridge and
                    # reflects the committed (or rolled-back) state of each task.
                    _post_reap_tasks: list[Task] = []
                    for _post_id in _orphaned_task_ids:
                        _refetched = await session.get(Task, _post_id)
                        if _refetched is not None:
                            _post_reap_tasks.append(_refetched)

                    # Audit A8: now that the orphan-row lock is released, do the
                    # slow parent-requeue work (child clone + dep remap). Heartbeats
                    # on the parent task will go through fine since we only set
                    # requeue_in_flight (a marker), not a row lock.
                    for parent in _deferred_parent_requeues:
                        # Heartbeat: parent requeue clones children + remaps deps
                        # (multiple queries) per parent — keep the watchdog happy.
                        _mark_reaper_progress()
                        try:
                            await _requeue_parent_task(parent, session)
                        except (
                            Exception
                        ) as e:  # per-attempt try in deferred-requeue loop
                            logger.error(
                                f"[REAPER] Deferred parent requeue failed for {parent.id}: {e}",
                                exc_info=True,
                            )
                            # C1: _requeue_parent_task commits internally; a
                            # failure (IntegrityError on the cloned child) leaves
                            # the shared session aborted, which would poison the
                            # remaining requeues, the post-reap rollup loop, and
                            # Steps 3-10. Roll back so each subsequent operation
                            # starts on a clean session.
                            with contextlib.suppress(Exception):
                                await session.rollback()
                        finally:
                            # Always clear the in-flight marker so subsequent reaper
                            # cycles can re-evaluate if requeue itself failed.
                            parent.requeue_in_flight = False
                            session.add(parent)
                    if _deferred_parent_requeues:
                        await session.commit()

                    # Delete old VMs after commit (IDs captured before overwrite)
                    for old_vm in _reaper_old_vms.values():
                        # Heartbeat: each VM delete is a GCE API call.
                        _mark_reaper_progress()
                        await _ensure_vm_deleted(old_vm, reason="reaper")

                    # Trigger rollup checks for failed tasks with parents (after commit)
                    for task in _post_reap_tasks:
                        # Heartbeat: _check_and_trigger_rollup hits GitHub per task.
                        _mark_reaper_progress()
                        if task.status == TaskStatus.FAILED and task.parent_task_id:
                            try:
                                # C3: pass the heartbeat so the per-child
                                # _verify_pr_merged loop inside the rollup check
                                # also heartbeats — a fan-heavy parent's PR-verify
                                # round-trips cannot silently exhaust
                                # REAPER_HANG_SECONDS on this loop either (mirrors
                                # the stale-EVALUATING sweep at reaper.py:2789).
                                await _check_and_trigger_rollup(
                                    task, session, progress_cb=_mark_reaper_progress
                                )
                                # Completion-side warm-reuse: if that landing
                                # completed the parent rollup, stamp the
                                # _deps_satisfied marker on the dependents it just
                                # unblocked so a polling warm VM can claim them
                                # before tearing down cold (see helper docstring).
                                await _mark_landed_parent_dependents(
                                    task.parent_task_id, session
                                )
                            except Exception as e:
                                logger.error(
                                    f"[REAPER] Rollup check failed for task {task.id}: {e}",
                                    exc_info=True,
                                )
                                # C1: _check_and_trigger_rollup runs FOR UPDATE
                                # selects + commits that can raise on a DB error,
                                # leaving the shared session aborted. Roll back so
                                # the next task's rollup check (and Steps 3-10) do
                                # not inherit a PendingRollback session.
                                with contextlib.suppress(Exception):
                                    await session.rollback()

                    # Immediately unblock dependents of terminally failed tasks
                    for task in _post_reap_tasks:
                        # Heartbeat: per-task dependent-unblock opens its own session.
                        _mark_reaper_progress()
                        if task.status == TaskStatus.FAILED:
                            try:
                                async with AsyncSessionLocal() as dep_session:
                                    dep_stmt = select(Task).where(
                                        Task.project_id == task.project_id,
                                        Task.status == TaskStatus.QUEUED,
                                        Task.depends_on.contains([str(task.id)]),
                                    )
                                    dep_result = await dep_session.execute(dep_stmt)
                                    waiting_tasks = dep_result.scalars().all()
                                    count = 0
                                    for wt in waiting_tasks:
                                        wt.depends_on = [
                                            d
                                            for d in (wt.depends_on or [])
                                            if d != str(task.id)
                                        ]
                                        dep_session.add(wt)
                                        count += 1
                                    if count:
                                        await dep_session.commit()
                                        logger.info(
                                            f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks"
                                        )
                            except Exception as e:  # noqa: BLE001
                                logger.warning(
                                    f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}",
                                    exc_info=True,
                                )

                    # C5: release the cross-cutting safeguard merge gate when a
                    # reaper-terminated safeguard task FAILS. The gate is otherwise
                    # released only on the security_audit COMPLETED branch, so a
                    # reaped safeguard (orphan-timeout, retries exhausted) would
                    # leave orig.requires_approval=True forever — a permanent
                    # strand that services.py then 2h-timeout-FAILs into a healthy
                    # parent. Each release runs in its OWN session (mirrors the
                    # dependent-unblock loop above and reaper Step 9
                    # sweep_stuck_qualifications) so a failure here cannot poison
                    # the shared reaper session. _maybe_release_safeguard_gate
                    # no-ops for non-safeguard tasks; findings=None → release
                    # (a reaped safeguard produced no blocker).
                    for task in _post_reap_tasks:
                        _mark_reaper_progress()
                        if task.status != TaskStatus.FAILED:
                            continue
                        _t_payload = task.first_payload if task.payloads else {}
                        if _t_payload.get("type") != "security_audit":
                            continue
                        try:
                            from app.task_completion import (
                                _maybe_release_safeguard_gate,
                            )

                            async with AsyncSessionLocal() as _sg_session:
                                _sg_task = await _sg_session.get(Task, task.id)
                                if _sg_task is not None:
                                    await _maybe_release_safeguard_gate(
                                        _sg_task, None, _sg_session
                                    )
                        except Exception as _sg_exc:  # noqa: BLE001
                            logger.warning(
                                f"[SAFEGUARD] Failed to release gate on reaped "
                                f"safeguard {task.id}: {_sg_exc}",
                                exc_info=True,
                            )

                    # Close open PRs owned by terminally-failed fix tasks. When a
                    # fix exhausts retries, its PR stays OPEN on GitHub, and
                    # sibling tasks whose rollup waits on open PRs targeting the
                    # same research branch block forever. Closing the dead PR
                    # removes it from the sibling rollup-wait gate.
                    for task in _post_reap_tasks:
                        # Heartbeat: closing a dead PR is a GitHub API call per task.
                        _mark_reaper_progress()
                        if task.status != TaskStatus.FAILED:
                            continue
                        payload = task.first_payload if task.payloads else {}
                        if payload.get("type") != "fix":
                            continue
                        pr_number = (task.context or {}).get("review_loop", {}).get(
                            "pr_number"
                        ) or payload.get("pr_number")
                        if not pr_number:
                            continue
                        # Skip closing rollup PRs (branch=staging prefix). A
                        # rollup PR represents many tasks' worth of merged work;
                        # closing it discards the whole body. Better to leave the
                        # rollup open for human follow-up than to throw away N
                        # children's successfully-merged contributions for a
                        # single fix task that exhausted retries. Match the FULL
                        # set of per-project staging prefixes, not just research/.
                        branch = payload.get("branch", "")
                        _proj = await session.get(Project, task.project_id)
                        if _proj is not None and _proj.is_staging_branch(branch):
                            logger.warning(
                                f"[REAPER] Skip closing rollup PR #{pr_number} "
                                f"(branch={branch}, fix task {task.id}) — "
                                f"closing would discard the rollup body"
                            )
                            continue
                        # Infra-recovery guard: do NOT close PRs for fix tasks that
                        # failed due to transient infrastructure (AGENT_TIMEOUT, VM
                        # preemption, etc.) while the review-loop still has iteration
                        # budget.  The code on the PR is valid — the agent may never
                        # have run.  Leave the PR open so the eval-sweep reaped-owner
                        # reconcile (recovery_evaluating.py) or the resurrection sweep
                        # can re-drive the chain.  Only close for genuine merit/code
                        # failures or when iteration budget is truly exhausted.
                        # Sibling chokepoints that are failure-class-blind (rollup-wait
                        # gate in rollup_management.py ~2031, _resolve_terminal_parent_
                        # verdict in task_lifecycle_requeue.py, review cap handler) are
                        # tracked as follow-up work — they require broader DB queries
                        # and separate review to fix safely.
                        from app.failure_classification import is_recoverable_infra_terminal

                        if is_recoverable_infra_terminal(task):
                            logger.info(
                                f"[PIPELINE] Preserving PR #{pr_number} for infra-reaped "
                                f"fix task {task.id} — review-loop budget remains, "
                                f"skipping close so eval-sweep can re-drive the chain"
                            )
                            continue
                        try:
                            from app.routers.agent import get_github_token_for_task
                            from app.github import close_pull_request, create_pr_comment

                            gh_token, owner, repo = await get_github_token_for_task(
                                task.id, session
                            )
                            reason = (
                                task.latest_status_message
                                or "fix task reached terminal failure"
                            )
                            try:
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    int(pr_number),
                                    f":warning: Abandoning this PR — fix task `{str(task.id)[:8]}` reached terminal failure.\n\n"
                                    f"{reason[:400]}\n\n"
                                    f"Closing so sibling rollups aren't blocked waiting for this branch.",
                                )
                            except Exception as ce:  # noqa: BLE001
                                logger.warning(
                                    f"[PIPELINE] Failed to comment on dead PR #{pr_number}: {ce}"
                                )
                            closed = await close_pull_request(
                                gh_token,
                                owner,
                                repo,
                                int(pr_number),
                                project_id=str(task.project_id),
                            )
                            if closed:
                                logger.info(
                                    f"[PIPELINE] Closed PR #{pr_number} after fix task {task.id} terminal-failed"
                                )
                            else:
                                logger.warning(
                                    f"[PIPELINE] close_pull_request returned False for PR #{pr_number}"
                                )
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                f"[PIPELINE] Failed to close PR for failed fix task {task.id}: {e}",
                                exc_info=True,
                            )

                # Step 3: Sweep EVALUATING parents with stale unmerged children.
                # When the initial review spawn after a child completes fails silently,
                # no event re-triggers _check_and_trigger_rollup.  This sweep catches
                # those stuck parents and lets the stale-PR recovery path respawn reviews.
                #
                # Phase B4 (H-P1): batch-fetch all children for ALL evaluating parents
                # in a single ``IN (...)`` query (mirrors the Step 4 pattern at the
                # batched fetch below). The previous code fired N queries — one per
                # parent — to find a single COMPLETED child for the rollup trigger.
                # ``load_only`` keeps each row narrow but MUST include ``status_log``
                # because the ``Task.latest_status_message``/``Task.status`` event
                # listeners (models.py ~360) read status_log on every set; under
                # async sessions a deferred-column fetch raises MissingGreenlet.
                # Plain-tuple snapshot of (id, status, context) per child, captured
                # at SELECT time. Steps 3 AND 4 iterate this rather than touching
                # live ORM rows: ``_check_and_trigger_rollup`` in Step 3 may commit
                # or trigger a session rollback (via inner DB error), which under
                # SQLAlchemy expires every loaded instance — a subsequent
                # ``parent.id`` / ``child.status`` read fires a sync lazy-load that
                # can't acquire a connection if the underlying asyncpg socket is
                # dead, crashing the entire reaper cycle. Snapshotting + re-fetching
                # via ``session.get`` decouples both steps from session state.
                # (Production 2026-05-08: 26x crashes at reaper.py:2407; recurred
                # 2026-05-21 at reaper.py:2385/2420 — Step 3/4 still read parent.id
                # and child.status off the now-stale batch.)
                _children_snapshot_by_parent: Dict[uuid.UUID, list[tuple]] = {}
                evaluating_parents = []
                # Plain UUID list — Steps 3 & 4 iterate this, never the live
                # ``evaluating_parents`` ORM objects (see snapshot note above).
                _parent_ids: list = []
                try:
                    stale_cutoff = now - timedelta(minutes=15)
                    evaluating_parents = (
                        (
                            await session.execute(
                                select(Task)
                                .options(
                                    load_only(
                                        Task.id,
                                        Task.status,
                                        Task.updated_at,
                                        Task.context,
                                        Task.payloads,
                                        Task.project_id,
                                        Task.parent_task_id,
                                        Task.latest_status_message,
                                        Task.status_log,
                                        Task.attempt_count,
                                        Task.created_at,
                                    raiseload=True,)
                                )
                                .where(
                                    Task.status == TaskStatus.EVALUATING,
                                    Task.updated_at < stale_cutoff,
                                )
                                # C3: per-cycle row cap. Step 3 calls
                                # _check_and_trigger_rollup (serial GitHub I/O)
                                # per parent and Step 4 calls _requeue_parent_task
                                # (DB-heavy) per parent. Without a cap a large
                                # stale-EVALUATING backlog grows the reaper cycle
                                # past REAPER_HANG_SECONDS mid-I/O — the same O(N)
                                # saturation the orphan cap bounds. Oldest-stale
                                # first (most-stuck parents); deferred parents
                                # re-qualify next cycle (status+age scan, no
                                # deferral queue needed).
                                .order_by(Task.updated_at.asc())
                                .limit(_REAPER_MAX_EVALUATING_PER_CYCLE)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(evaluating_parents) >= _REAPER_MAX_EVALUATING_PER_CYCLE:
                        logger.warning(
                            "[REAPER] Hit per-cycle cap of %d stale EVALUATING "
                            "parents — remainder deferred to next cycle",
                            _REAPER_MAX_EVALUATING_PER_CYCLE,
                        )
                    if evaluating_parents:
                        _parent_ids = [p.id for p in evaluating_parents]
                        _all_children = (
                            (
                                await session.execute(
                                    select(Task)
                                    .options(
                                        load_only(
                                            Task.id,
                                            Task.status,
                                            Task.parent_task_id,
                                            Task.context,
                                            Task.status_log,
                                            Task.latest_status_message,
                                            Task.payloads,
                                        raiseload=True,)
                                    )
                                    .where(Task.parent_task_id.in_(_parent_ids))
                                )
                            )
                            .scalars()
                            .all()
                        )
                        for _c in _all_children:
                            _children_snapshot_by_parent.setdefault(
                                _c.parent_task_id, []
                            ).append((_c.id, _c.status, _c.context or {}))
                    for parent_id in _parent_ids:
                        # Heartbeat: Step 3 calls _check_and_trigger_rollup per
                        # parent (serial GitHub I/O) — the other dominant per-cycle
                        # cost besides the orphan loop. Mark progress every parent.
                        _mark_reaper_progress()
                        # Find a triggering child id from the plain-tuple
                        # snapshot, then re-fetch it fresh. An earlier iteration's
                        # _check_and_trigger_rollup may have committed or rolled
                        # back — reading parent.id / child.status off the now-stale
                        # ORM batch fires a sync lazy-load that crashes the cycle
                        # on an unhealthy connection.
                        #
                        # Prefer a COMPLETED child (the normal stale-spawn case).
                        # C6 rework (B): ALSO accept any child carrying the
                        # ``_rollup_nudge_pending`` write-ahead marker, regardless
                        # of status. ``_propagate_pipeline_failure_to_original``
                        # sets that marker on a child it is CANCELLING just before
                        # nudging the rollup; a death between its terminal commit
                        # and the nudge drops the trigger, and a parent whose only
                        # un-rolled child is that CANCELLED one has NO COMPLETED
                        # child for the path above to find — it would sit
                        # EVALUATING until the backstop. ``_check_and_trigger_rollup``
                        # is keyed only on ``child.parent_task_id`` and re-evaluates
                        # ALL siblings, so re-running it for the marker-carrying
                        # child is correct and idempotent.
                        _nudge_child_id = next(
                            (
                                cid
                                for cid, _c_status, c_ctx in _children_snapshot_by_parent.get(
                                    parent_id, []
                                )
                                if (c_ctx or {}).get("_rollup_nudge_pending")
                            ),
                            None,
                        )
                        completed_child_id = next(
                            (
                                cid
                                for cid, c_status, _ in _children_snapshot_by_parent.get(
                                    parent_id, []
                                )
                                if c_status == TaskStatus.COMPLETED
                            ),
                            None,
                        )
                        trigger_child_id = completed_child_id or _nudge_child_id
                        if trigger_child_id is not None:
                            try:
                                child = await session.get(Task, trigger_child_id)
                                if child is not None:
                                    # C3: pass the heartbeat so _check_and_trigger_rollup's
                                    # per-child PR-verify loop keeps the reaper
                                    # liveness fresh on a fan-heavy parent.
                                    await _check_and_trigger_rollup(
                                        child,
                                        session,
                                        progress_cb=_mark_reaper_progress,
                                    )
                                    # C6 rework (B): the nudge ran without raising —
                                    # drop the write-ahead recovery marker on the
                                    # child that carried it (the rollup has now been
                                    # re-evaluated). Best-effort: a leftover marker
                                    # is self-healing (next sweep re-runs an
                                    # idempotent check).
                                    if _nudge_child_id is not None:
                                        _nudged = await session.get(
                                            Task, _nudge_child_id
                                        )
                                        if _nudged is not None and (
                                            _nudged.context or {}
                                        ).get("_rollup_nudge_pending"):
                                            _nc = dict(_nudged.context or {})
                                            _nc.pop("_rollup_nudge_pending", None)
                                            _nudged.context = _nc
                                            flag_modified(_nudged, "context")
                                            session.add(_nudged)
                                            await session.commit()
                                    # Completion-side warm-reuse: if the sweep's
                                    # rollup trigger landed this parent, stamp the
                                    # _deps_satisfied marker on the dependents it
                                    # just unblocked (see helper docstring) so a
                                    # polling warm VM claims them before cold
                                    # teardown rather than waiting for a later
                                    # window-bounded dispatch scan.
                                    await _mark_landed_parent_dependents(
                                        parent_id, session
                                    )
                            except Exception as e:  # noqa: BLE001
                                logger.warning(
                                    f"[REAPER] Evaluating sweep rollup failed for parent {parent_id}: {e}",
                                    exc_info=True,
                                )
                    if evaluating_parents:
                        logger.info(
                            f"[REAPER] Swept {len(evaluating_parents)} stale EVALUATING parents"
                        )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[REAPER] Evaluating parent sweep error: {e}", exc_info=True
                    )
                    # C1: a sub-helper (e.g. _check_and_trigger_rollup) that
                    # raised may have left the shared ``session`` in
                    # PendingRollback — roll back here so Steps 4-10 run on a
                    # clean session rather than cascading silent failures /
                    # rollback-expiry MissingGreenlet. Mirrors Step 6/7/8/10.
                    with contextlib.suppress(Exception):
                        await session.rollback()

                # Step 4: Rescue EVALUATING parents whose children ALL terminated without
                # any completing successfully.  These parents have no COMPLETED child to
                # trigger _check_and_trigger_rollup, so they stay EVALUATING forever.
                # Iterates the plain-tuple snapshot from Step 3 — see the comment
                # at ``_children_snapshot_by_parent`` for why we don't touch
                # live ORM children here.
                try:
                    _terminal = {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                    }
                    rescued = 0
                    for parent_id in _parent_ids:
                        # Heartbeat: this rescue loop calls _requeue_parent_task
                        # (child clone + dep remap, multiple queries) per parent —
                        # another per-cycle cost that can extend wall time under a
                        # large EVALUATING backlog. Mark progress every parent.
                        _mark_reaper_progress()
                        children_snap = _children_snapshot_by_parent.get(parent_id, [])
                        if not children_snap:
                            continue
                        if any(
                            status not in _terminal for _, status, _ in children_snap
                        ):
                            # At least one child still running — not safe to rescue yet.
                            continue
                        if any(
                            status == TaskStatus.COMPLETED
                            for _, status, _ in children_snap
                        ):
                            # Step 3 handles this; skip.
                            continue
                        # All children are terminal and none completed — attempt re-evaluation
                        # via _requeue_parent_task which clones failed children with retry
                        # guidance (up to MAX_PARENT_REQUEUES attempts).  If attempts are
                        # exhausted it marks the parent FAILED and fires a failure alert.
                        # Re-fetch the parent fresh — Step 3 may have committed or
                        # rolled back, expiring the live ``evaluating_parents`` batch.
                        parent = await session.get(Task, parent_id)
                        if parent is None:
                            continue
                        failed_children = [
                            (cid, ctx)
                            for cid, status, ctx in children_snap
                            if status == TaskStatus.FAILED
                        ]
                        cancelled_children = [
                            cid
                            for cid, status, _ in children_snap
                            if status == TaskStatus.CANCELLED
                        ]
                        # Build a diagnostic message including child failure classes.
                        parts = []
                        if failed_children:
                            classes = [
                                (ctx or {}).get("failure_category") or "unknown"
                                for _, ctx in failed_children
                            ]
                            class_summary = ", ".join(f"{cls}" for cls in classes)
                            parts.append(
                                f"{len(failed_children)} failed ({class_summary})"
                            )
                        if cancelled_children:
                            parts.append(f"{len(cancelled_children)} cancelled")
                        diag = (
                            "; ".join(parts)
                            if parts
                            else f"{len(children_snap)} terminal"
                        )
                        parent.latest_status_message = (
                            f"All child tasks terminated without success: {diag}"
                        )
                        session.add(parent)
                        await _requeue_parent_task(parent, session)
                        rescued += 1
                        logger.warning(
                            f"[REAPER] Rescued stuck EVALUATING parent {parent.id}: {diag}"
                        )
                    if rescued:
                        await session.commit()
                        logger.info(
                            f"[REAPER] Rescued {rescued} all-children-failed EVALUATING parents"
                        )
                except Exception as e:
                    logger.error(
                        f"[REAPER] All-children-failed rescue error: {e}", exc_info=True
                    )
                    # C1: _requeue_parent_task commits internally and an
                    # IntegrityError (e.g. idempotency-key collision on the
                    # cloned retry child) leaves the shared ``session`` aborted.
                    # Roll back so Steps 5-10 don't inherit the poison. Mirrors
                    # the Step 6/7/8/10 handlers and the Step 3 guard above.
                    with contextlib.suppress(Exception):
                        await session.rollback()

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
        sleep_seconds = min(60 * (2**consecutive_errors), 300)
        try:
            await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            logger.info("[REAPER] Service shutting down.")
            break
