"""Warm-VM backlog reuse (efficiency direction d-91d8dede).

Extends the existing warm-VM continuity mechanism so an idle agent VM can
claim the next eligible QUEUED task from its project's GENERAL backlog --
not only a PR-chain follow-on pinned to its VM. Gated by the runtime config
flag ``warm_backlog_reuse_enabled`` (default ON, guarded by the
is_warm_backlog_reuse_suppressed() circuit breaker), independent from
``warm_continuity_enabled``.

This module owns the *eligibility* and *atomic selection* for a backlog
claim. The actual QUEUED->IN_PROGRESS flip, telemetry stamping, and JWT mint
live in the /api/agent/next-role handler so the chain and backlog paths share
one claim-finalize routine.
"""

import logging
import uuid
from collections import deque
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_, select

from app.models import Task, TaskStatus
from app.orchestrator_filters import _prod_files
from app.time_utils import _utcnow

logger = logging.getLogger("blueprint")

# Bounded window (seconds) for which a COMPLETED task's VM teardown is held so
# the agent can poll /api/agent/next-role and claim backlog work before the VM
# is torn down. Sized to comfortably exceed the agent's poll cadence (5s) plus
# one round-trip. If nothing is claimed within the window, teardown proceeds
# exactly as before.
WARM_BACKLOG_TEARDOWN_DEFER_SECS = 20

# How many QUEUED candidates to scan per poll. A small cap keeps the endpoint
# cheap (it can be hit by many idle VMs at up to 600/min) while still giving
# the file-conflict / skip-locked filters enough rows to find a claimable one.
_BACKLOG_SCAN_LIMIT = 10


async def _project_in_progress_files(session, project_id) -> set[str]:
    """Production files currently being modified by IN_PROGRESS tasks in a project.

    Mirrors the orchestrator's ``filter_file_conflicts`` isolation guarantee:
    a warm backlog claim must never run a task that overlaps files an
    IN_PROGRESS task is already editing, or the two diverge and collide at
    merge time. Test files are additive and excluded (same as the filter).
    """
    rows = (
        await session.execute(
            select(Task.context).where(
                Task.project_id == project_id,
                Task.status == TaskStatus.IN_PROGRESS,
            )
        )
    ).all()
    files: set[str] = set()
    for (ctx,) in rows:
        files |= _prod_files((ctx or {}).get("related_files", []))
    return files


async def _eligible_backlog_ids(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: Optional[set[str]] = None,
) -> list[uuid.UUID]:
    """QUEUED task ids in *project_id* an idle VM may claim, priority-ordered.

    Conservative subset of the orchestrator's dispatch gate (kept simple
    rather than replicating the full async filter stack):
      - status == QUEUED, same project_id
      - not requires_approval (orchestrator skips these too)
      - retry_after_until null or already elapsed
      - no ``depends_on`` (avoids re-implementing dependency / PR-merge
        resolution; dependency-bearing tasks simply cold-dispatch as before)
      - not pinned to a *different* VM via ``warm_target_vm_id`` (a pin to
        this VM is the chain path's job; a pin elsewhere must not be stolen)
      - no related_files overlap with an IN_PROGRESS task in the same project

    Any task type is runnable by any agent VM (the workspace is re-cloned per
    role via resetForNextRole), so no per-VM task-type capability gate is
    needed — same-project scoping is the capability boundary.

    ``in_progress_files`` lets a caller that already computed the project's
    IN_PROGRESS file set (e.g. an eligibility gate immediately followed by a
    claim) pass it in so the file-conflict scan runs once instead of once per
    call. When omitted it is computed lazily — and only if the QUEUED scan
    returned rows, so an empty backlog never pays for the IN_PROGRESS scan.
    """
    now = _utcnow()
    rows = (
        await session.execute(
            select(
                Task.id,
                Task.depends_on,
                Task.context,
            )
            .where(
                Task.status == TaskStatus.QUEUED,
                Task.project_id == project_id,
                Task.requires_approval.is_(False),
                or_(
                    Task.retry_after_until.is_(None),
                    Task.retry_after_until <= now,
                ),
            )
            .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
            .limit(_BACKLOG_SCAN_LIMIT)
        )
    ).all()

    rows_fetched = len(rows)
    saturated = rows_fetched >= _BACKLOG_SCAN_LIMIT

    if not rows:
        logger.debug(
            "[BACKLOG-REUSE] scan project_id=%s vm_instance_id=%s "
            "rows_fetched=%d rows_eligible=0 saturated=%s",
            project_id,
            vm_instance_id,
            rows_fetched,
            saturated,
        )
        return []

    if in_progress_files is None:
        in_progress_files = await _project_in_progress_files(session, project_id)

    eligible: list[uuid.UUID] = []
    skipped_depends_on = 0
    skipped_pinned_vm = 0
    skipped_file_conflict = 0

    for task_id, depends_on, ctx in rows:
        if depends_on:
            skipped_depends_on += 1
            continue
        ctx = ctx or {}
        pinned_vm = ctx.get("warm_target_vm_id")
        if pinned_vm and pinned_vm != vm_instance_id:
            skipped_pinned_vm += 1
            continue
        if in_progress_files:
            cfiles = _prod_files(ctx.get("related_files", []))
            if cfiles & in_progress_files:
                skipped_file_conflict += 1
                continue
        eligible.append(task_id)

    logger.debug(
        "[BACKLOG-REUSE] scan project_id=%s vm_instance_id=%s "
        "rows_fetched=%d rows_eligible=%d saturated=%s "
        "skipped_depends_on=%d skipped_pinned_vm=%d skipped_file_conflict=%d",
        project_id,
        vm_instance_id,
        rows_fetched,
        len(eligible),
        saturated,
        skipped_depends_on,
        skipped_pinned_vm,
        skipped_file_conflict,
    )

    return eligible


async def has_eligible_backlog_task(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: Optional[set[str]] = None,
) -> bool:
    """True iff *project_id* has at least one backlog task this VM could claim.

    Used by the teardown-deferral decision: only worth keeping an idle VM
    alive if there is genuine pending work it can run warm.

    Pass ``in_progress_files`` to reuse an already-computed IN_PROGRESS file
    set (see ``_eligible_backlog_ids``) when this gate is paired with a claim.
    """
    return bool(
        await _eligible_backlog_ids(
            session, project_id, vm_instance_id, in_progress_files=in_progress_files
        )
    )


async def select_backlog_reuse_candidate(
    session,
    project_id,
    vm_instance_id: str,
    *,
    in_progress_files: Optional[set[str]] = None,
) -> Optional[Task]:
    """Atomically lock the next claimable backlog task for an idle VM, or None.

    Walks the eligible ids in priority order and locks the first one still
    QUEUED with ``SELECT ... FOR UPDATE SKIP LOCKED`` — so a concurrent
    orchestrator cold-dispatch or a peer VM's next-role poll can never
    double-claim the same row (whoever wins the lock flips it; the other
    path's lock attempt skips it and tries the next candidate).

    The returned Task is locked and still QUEUED; the caller finalizes the
    claim (status flip, telemetry, commit).

    Pass ``in_progress_files`` to reuse an already-computed IN_PROGRESS file
    set (see ``_eligible_backlog_ids``) when this claim follows an eligibility
    gate in the same flow, so the file-conflict scan is not run twice.

    Records the funnel skip reason for the two None-returning cases, which the
    caller cannot distinguish on its own: ``no_eligible`` when no backlog task
    qualifies, ``lost_claim_race`` when one or more qualified but a concurrent
    claimer won the SKIP-LOCKED row.
    """
    eligible = await _eligible_backlog_ids(
        session, project_id, vm_instance_id, in_progress_files=in_progress_files
    )
    if not eligible:
        record_claim_skipped("no_eligible")
        return None
    result = await session.execute(
        select(Task)
        .where(Task.id.in_(eligible), Task.status == TaskStatus.QUEUED)
        .order_by(Task.priority.desc().nullslast(), Task.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    claimed = result.scalars().first()
    logger.debug(
        "[BACKLOG-REUSE] claim project_id=%s vm_instance_id=%s "
        "task_id=%s rows_eligible=%d",
        project_id,
        vm_instance_id,
        claimed.id if claimed is not None else None,
        len(eligible),
    )
    if claimed is None:
        record_claim_skipped("lost_claim_race")
    return claimed


# ---------------------------------------------------------------------------
# Funnel observability counters
# ---------------------------------------------------------------------------
# Rolling deques for each funnel stage. Each entry is a UTC datetime (or a
# (datetime, reason) tuple for claim_skipped). Bounded by maxlen so they
# never grow without bound on a long-lived instance.

_vms_with_eligible_backlog_events: deque = deque(maxlen=500)
_teardown_defers_events: deque = deque(maxlen=500)
_next_role_polls_during_defer_events: deque = deque(maxlen=500)
_warm_claims_succeeded_events: deque = deque(maxlen=500)
_claim_skipped_events: deque = deque(maxlen=500)  # (datetime, reason)


def record_vm_with_eligible_backlog() -> None:
    """Stage 1: a COMPLETION event found ≥1 eligible QUEUED backlog task."""
    _vms_with_eligible_backlog_events.append(_utcnow())


def record_teardown_defer() -> None:
    """Stage 2: VM teardown was deferred for the warm-backlog reuse window."""
    _teardown_defers_events.append(_utcnow())


def record_next_role_poll_during_defer() -> None:
    """Stage 3: a /next-role poll arrived inside the teardown-defer window."""
    _next_role_polls_during_defer_events.append(_utcnow())


def record_warm_claim_succeeded() -> None:
    """Stage 4: a /next-role poll claimed a backlog task (vm_warm_start=True)."""
    _warm_claims_succeeded_events.append(_utcnow())


def record_claim_skipped(reason: str) -> None:
    """Stage 4 miss: poll arrived but did not claim.

    reason should be one of: "no_eligible", "lost_claim_race",
    "circuit_breaker_tripped".
    """
    _claim_skipped_events.append((_utcnow(), reason))


def get_reuse_funnel_snapshot(window_hours: int = 24) -> dict:
    """Return rolling funnel counts for the warm-reuse claim pipeline.

    All counts cover the trailing ``window_hours`` (default 24h).
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    skipped_by_reason: dict = {}
    for ts, reason in _claim_skipped_events:
        if ts >= cutoff:
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
    return {
        "window_hours": window_hours,
        "completed_vms_with_eligible_backlog": sum(
            1 for ts in _vms_with_eligible_backlog_events if ts >= cutoff
        ),
        "teardown_defers_scheduled": sum(
            1 for ts in _teardown_defers_events if ts >= cutoff
        ),
        "next_role_polls_during_defer": sum(
            1 for ts in _next_role_polls_during_defer_events if ts >= cutoff
        ),
        "warm_claims_succeeded": sum(
            1 for ts in _warm_claims_succeeded_events if ts >= cutoff
        ),
        "claim_skipped_by_reason": skipped_by_reason,
        "claim_skipped_total": sum(skipped_by_reason.values()),
    }


# ---------------------------------------------------------------------------
# Chain-continuity funnel observability counters
# ---------------------------------------------------------------------------
# Mirrors the backlog-reuse funnel above for the chain-continuity branch of
# /next-role. Each entry is a UTC datetime (or (datetime, reason) tuple for
# the multi-reason skipped stage). Bounded by maxlen=500 to prevent
# unbounded growth on a long-lived process.

_chain_poll_events: deque = deque(maxlen=500)
_chain_claimed_events: deque = deque(maxlen=500)
_chain_no_pinned_events: deque = deque(maxlen=500)
_chain_other_vm_pr_events: deque = deque(maxlen=500)
_chain_doomed_events: deque = deque(maxlen=500)
_chain_skipped_events: deque = deque(maxlen=500)  # (datetime, reason)


def record_chain_poll() -> None:
    """Total poll arrivals: every /next-role call that enters the chain branch."""
    _chain_poll_events.append(_utcnow())


def record_chain_claimed() -> None:
    """Poll successfully claimed a warm-pinned chain task via _finalize_warm_claim."""
    _chain_claimed_events.append(_utcnow())


def record_chain_no_pinned() -> None:
    """Poll found no pinned task (chain was active) and returned 204."""
    _chain_no_pinned_events.append(_utcnow())


def record_chain_other_vm_pr() -> None:
    """Poll exited because another VM owns an IN_PROGRESS task for this PR."""
    _chain_other_vm_pr_events.append(_utcnow())


def record_chain_doomed() -> None:
    """Poll returned 204 because the VM is scheduled for orchestrator-initiated deletion."""
    _chain_doomed_events.append(_utcnow())


def record_chain_skipped(reason: str) -> None:
    """Chain lookup was skipped; reason is 'disabled' or 'breaker_tripped'."""
    _chain_skipped_events.append((_utcnow(), reason))


def get_chain_continuity_funnel_snapshot(window_hours: int = 24) -> dict:
    """Return rolling funnel counts for the chain-continuity claim path.

    All counts cover the trailing ``window_hours`` (default 24h).
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    skipped_by_reason: dict = {}
    for ts, reason in _chain_skipped_events:
        if ts >= cutoff:
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
    return {
        "window_hours": window_hours,
        "poll_arrivals": sum(1 for ts in _chain_poll_events if ts >= cutoff),
        "chain_claimed": sum(1 for ts in _chain_claimed_events if ts >= cutoff),
        "no_pinned_task": sum(1 for ts in _chain_no_pinned_events if ts >= cutoff),
        "another_vm_owns_pr": sum(
            1 for ts in _chain_other_vm_pr_events if ts >= cutoff
        ),
        "doomed_vm": sum(1 for ts in _chain_doomed_events if ts >= cutoff),
        "skipped_by_reason": skipped_by_reason,
        "skipped_total": sum(skipped_by_reason.values()),
    }


# ---------------------------------------------------------------------------
# Spawn-time warm-pin funnel observability counters
# ---------------------------------------------------------------------------
# Mirrors the chain-continuity funnel above, but for the *spawn-side* warm-pin
# decision in review_pipeline._spawn_pipeline_task: when a review/fix/audit/perf
# task is created, did it get pinned to the source task's warm VM or not, and
# if not, why? Each entry is a UTC datetime (or (datetime, reason) tuple for
# the multi-reason skipped stage). Bounded by maxlen=500 to prevent unbounded
# growth on a long-lived process.

_spawn_attempt_events: deque = deque(maxlen=500)
_spawn_pin_set_events: deque = deque(maxlen=500)
_spawn_pin_skipped_events: deque = deque(maxlen=500)  # (datetime, reason)


def record_spawn_attempt() -> None:
    """Stage 1: a pipeline spawn reached the warm-pin decision block."""
    _spawn_attempt_events.append(_utcnow())


def record_spawn_pin_set() -> None:
    """Stage 2 (success): the spawned task was pinned to the source VM."""
    _spawn_pin_set_events.append(_utcnow())


def record_spawn_pin_skipped(reason: str) -> None:
    """Stage 2 (miss): warm-pin skipped.

    reason should be one of: "disabled" (warm_continuity_enabled flag off),
    "breaker_tripped" (is_warm_continuity_suppressed() true),
    "caller_suppressed" (suppress_warm_handoff=True),
    "source_cold" (source task has no vm_instance_id),
    "stale_source" (source heartbeat older than WARM_HANDOFF_WINDOW_SECS).
    """
    _spawn_pin_skipped_events.append((_utcnow(), reason))


def get_spawn_pin_funnel_snapshot(window_hours: int = 24) -> dict:
    """Return rolling funnel counts for the spawn-time warm-pin decision.

    All counts cover the trailing ``window_hours`` (default 24h).
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    skipped_by_reason: dict = {}
    for ts, reason in _spawn_pin_skipped_events:
        if ts >= cutoff:
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
    return {
        "window_hours": window_hours,
        "spawn_attempts": sum(1 for ts in _spawn_attempt_events if ts >= cutoff),
        "pin_set": sum(1 for ts in _spawn_pin_set_events if ts >= cutoff),
        "skipped_by_reason": skipped_by_reason,
        "skipped_total": sum(skipped_by_reason.values()),
    }
