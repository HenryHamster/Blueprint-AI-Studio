"""Warm-VM continuity / backlog-reuse / rate-limit window process state (split out of shared_state).

Owns the warm-VM heartbeat map, the warm-pin clear-out + backlog-reuse circuit
breakers, the backlog first-refusal map, the SR catch-rate refresh debounce, the
``pr_is_not_behind`` call-site counters, the user-debug spawn rate-limit window,
and the cross-PR merge-error pattern tracker. Re-exported from
:mod:`app.shared_state` as a thin facade. Behaviour-preserving.
"""

import os
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Dict

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# NOTE: the auto-suppression deadlines (``warm_continuity_suppressed_until`` /
# ``warm_backlog_reuse_suppressed_until``) and their circuit-breaker checks
# (``is_warm_continuity_suppressed`` / ``is_warm_backlog_reuse_suppressed``) are
# *rebindable* scalars that tests and admin routers read/write through the
# facade (``shared_state.warm_backlog_reuse_suppressed_until = None``). They
# therefore live on the :mod:`app.shared_state` facade — a function whose
# ``global`` rebinds its own module attribute would diverge from the facade
# attribute the rest of the codebase reads. The event recorders/counters below
# stay here because they mutate their containers in place (identity-preserving).

# --- User-submitted Auto-Debug Rate Limiting ---
# Tracks timestamps of user-submitted debug spawns per project to enforce hourly cap.
# Structure: project_id -> deque of datetime (UTC) of recent spawns
user_debug_spawn_times: Dict[str, deque] = {}

USER_DEBUG_MAX_PER_HOUR = (
    3  # max debug tasks spawned for user/admin tasks per project per hour
)


def record_user_debug_spawn(project_id: str, now: datetime) -> None:
    """Record a user-submitted debug spawn and prune old entries."""
    if project_id not in user_debug_spawn_times:
        user_debug_spawn_times[project_id] = deque()
    dq = user_debug_spawn_times[project_id]
    dq.append(now)
    cutoff = now - timedelta(hours=1)
    while dq and dq[0] < cutoff:
        dq.popleft()


def count_recent_user_debug_spawns(project_id: str, now: datetime) -> int:
    """Count user-submitted debug spawns in the last hour for a project."""
    dq = user_debug_spawn_times.get(project_id)
    if not dq:
        return 0
    cutoff = now - timedelta(hours=1)
    return sum(1 for t in dq if t >= cutoff)


# --- Cross-PR Merge Error Pattern Tracking ---
# When the same normalized merge error blocks multiple PRs in a project within a
# sliding window, that's a systemic blocker (branch protection change, CI env
# issue) — not a per-PR problem. Track (pr_number, ts) occurrences per
# (project_id, normalized_error) so ci_completion can pause wasteful retries
# and surface a shared alert.
merge_error_occurrences: Dict[tuple, deque] = {}

MERGE_PATTERN_WINDOW_SECONDS = int(os.getenv("MERGE_PATTERN_WINDOW_SECONDS", "1800"))
MERGE_PATTERN_MIN_PRS = int(os.getenv("MERGE_PATTERN_MIN_PRS", "3"))


def record_merge_error_occurrence(
    project_id: str, normalized_error: str, pr_number: int
) -> int:
    """Record a merge-error occurrence and return the count of distinct PRs
    hit by the same (project, normalized_error) within the sliding window."""
    if not project_id or not normalized_error or pr_number is None:
        return 0
    key = (project_id, normalized_error)
    dq = merge_error_occurrences.setdefault(key, deque(maxlen=50))
    now = _utcnow()
    dq.append((pr_number, now))
    cutoff = now - timedelta(seconds=MERGE_PATTERN_WINDOW_SECONDS)
    while dq and dq[0][1] < cutoff:
        dq.popleft()
    return len({pr for pr, _ in dq})


def count_merge_error_pattern_prs(project_id: str, normalized_error: str) -> int:
    """Distinct PRs in the sliding window for a (project, error) key."""
    if not project_id or not normalized_error:
        return 0
    dq = merge_error_occurrences.get((project_id, normalized_error))
    if not dq:
        return 0
    cutoff = _utcnow() - timedelta(seconds=MERGE_PATTERN_WINDOW_SECONDS)
    return len({pr for pr, ts in dq if ts >= cutoff})


# --- Warm-VM heartbeat tracking (Phase 2) ---
# Maps vm_instance_id -> last datetime the VM contacted us (claim, status,
# heartbeat, vm-heartbeat, or next-role poll). Used by the orchestrator's
# provisioning loop to decide whether a warm-target pin is still honored.
# Pruned by sweep_stale_in_memory_state — entries older than 1h are removed.
vm_last_heartbeat: Dict[str, datetime] = {}


def record_vm_heartbeat(vm_instance_id: str) -> None:
    """Record liveness for a VM (warm-VM continuity).

    Called from the agent's vm-heartbeat endpoint AND from the next-role
    polling endpoint (which doubles as a liveness signal). Stores the
    current naive UTC timestamp.
    """
    if not vm_instance_id:
        return
    vm_last_heartbeat[vm_instance_id] = _utcnow()


def is_vm_alive(vm_instance_id: str, max_age_seconds: int = 90) -> bool:
    """True iff the VM heartbeated within the last `max_age_seconds`.

    Used by the orchestrator's provisioning loop to decide whether a
    QUEUED task with `warm_target_vm_id` should skip provisioning (live
    target → agent will pick it up via /next-role) or clear the warm
    fields and provision normally (target unresponsive → cold path).
    """
    if not vm_instance_id:
        return False
    last = vm_last_heartbeat.get(vm_instance_id)
    if last is None:
        return False
    return (_utcnow() - last).total_seconds() <= max_age_seconds


def is_pool_vm_alive(
    vm_instance_id: str,
    last_heartbeat_at: "datetime | None",
    max_age_seconds: int = 90,
) -> bool:
    """True iff a pool VM is live per in-memory cache OR durable heartbeat.

    Extends :func:`is_vm_alive` with a durable fallback: the caller passes
    the ``PoolVm.last_heartbeat_at`` column value (stamped by the heartbeat
    endpoint on every clean pool-VM beat) so established members survive a
    process restart whose in-memory ``vm_last_heartbeat`` map starts empty.

    Used by :func:`sweep_idle_pool_vms` for zombie teardown and LEASED-member
    reclaim decisions, replacing the in-memory-only :func:`is_vm_alive` call
    for pool-member liveness checks.
    """
    if not vm_instance_id:
        return False
    now = _utcnow()
    # In-memory map first (hot path — stamped by /vm-heartbeat on this instance).
    last = vm_last_heartbeat.get(vm_instance_id)
    if last is not None and (now - last).total_seconds() <= max_age_seconds:
        return True
    # Durable fallback: PoolVm.last_heartbeat_at is stamped by the heartbeat
    # endpoint on every clean pool-VM beat and survives process restarts.
    if last_heartbeat_at is not None and (now - last_heartbeat_at).total_seconds() <= max_age_seconds:
        return True
    return False


# --- SR catch-rate refresh debounce (scale: every-claim recompute) ---
# Maps project_id (str) -> last datetime this instance recomputed the SR
# catch-rate trend. The recompute is a ~2000-row JSONB context scan plus a
# project-row write lock; it ran on EVERY agent claim, serializing claims
# per project at 100-200 VMs. We debounce it to a bounded cadence: the trend
# still updates regularly, just not on literally every claim. Per-instance is
# fine — across instances the project-lock acquisition (skip_locked=True) drops
# any concurrent in-flight recompute, so the DB is never serialized either.
# Pruned by sweep_stale_in_memory_state.
sr_catch_rate_last_refreshed: Dict[str, datetime] = {}

# Default debounce window. Within this window of the last successful recompute
# (on this instance), a claim skips the expensive refresh entirely.
SR_CATCH_RATE_DEBOUNCE_SECONDS = 600  # 10 minutes


def should_refresh_sr_catch_rate(
    project_id, window_seconds: int = SR_CATCH_RATE_DEBOUNCE_SECONDS
) -> bool:
    """True iff the SR catch-rate trend has NOT been recomputed for this
    project within ``window_seconds`` (on this instance).

    Cheap, in-memory gate intended to run on the hot claim path BEFORE the
    expensive 2000-row scan. Returns True on first sight of a project (no
    recorded refresh) so the trend is always computed at least once.
    """
    last = sr_catch_rate_last_refreshed.get(str(project_id))
    if last is None:
        return True
    return (_utcnow() - last).total_seconds() >= window_seconds


def mark_sr_catch_rate_refreshed(project_id) -> None:
    """Record that the SR catch-rate trend was just recomputed for a project.

    Called after a successful refresh so subsequent claims within the
    debounce window skip the recompute.
    """
    sr_catch_rate_last_refreshed[str(project_id)] = _utcnow()


# --- Warm-pin clear-out tracking (Phase 2e observability) ---
# Each entry: (timestamp, vm_id, reason). Reason is "deadline_expired" or
# "vm_unresponsive". Sustained >5/hour means warm VMs are dying mid-poll
# more often than expected — the agent's lifecycle code is leaking state
# or the reset routine is failing. Pruned by sweep_stale_in_memory_state.
warm_pin_clear_events: deque = deque(maxlen=200)


def record_warm_pin_clear(vm_id: str, reason: str) -> None:
    """Record a warm-pin clear-out event for the orchestrator's metric."""
    if not vm_id:
        return
    warm_pin_clear_events.append((_utcnow(), vm_id, reason))


def count_recent_warm_pin_clears(window_seconds: int = 3600) -> int:
    """Distinct clear-out events in the trailing window."""
    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    return sum(1 for ts, _, _ in warm_pin_clear_events if ts >= cutoff)


def count_recent_warm_pin_clears_by_reason(window_seconds: int) -> dict[str, int]:
    """Per-reason histogram of clear-out events in the trailing window.

    Mirrors ``count_recent_warm_pin_clears`` cutoff arithmetic so the sum of
    values equals the total. Empty / missing reasons map to ``"unknown"`` so
    the invariant ``sum(hist.values()) == count_recent_warm_pin_clears(w)``
    holds even when legacy entries lack a reason.
    """
    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    histogram: dict[str, int] = {}
    for ts, _vm, reason in warm_pin_clear_events:
        if ts < cutoff:
            continue
        key = reason if reason else "unknown"
        histogram[key] = histogram.get(key, 0) + 1
    return histogram


# Per-call-site counter for pr_is_not_behind() outcomes. Keyed by a stable
# label that names the calling site (e.g. "ci_completion", "rollup_pre_merge").
# Each inner dict has integer buckets for the True/False/None outcomes plus a
# total. Updated synchronously by record_pr_is_not_behind() and snapshotted by
# the admin diagnostics endpoint. In-process only — not persisted.
PR_IS_NOT_BEHIND_CALL_SITES: tuple[str, ...] = (
    "ci_completion",
    "rollup_pre_merge",
    "rollup_post_merge",
    "orchestrator_ci",
)
pr_is_not_behind_counts: Dict[str, Dict[str, int]] = {}


def _empty_pr_is_not_behind_bucket() -> Dict[str, int]:
    return {"true": 0, "false": 0, "none": 0, "total": 0}


def record_pr_is_not_behind(call_site: str, result: bool | None) -> None:
    """Increment the bucket matching ``result`` for ``call_site``."""
    if not call_site:
        return
    bucket = pr_is_not_behind_counts.setdefault(
        call_site, _empty_pr_is_not_behind_bucket()
    )
    if result is True:
        bucket["true"] += 1
    elif result is False:
        bucket["false"] += 1
    else:
        bucket["none"] += 1
    bucket["total"] += 1


def snapshot_pr_is_not_behind_counts() -> Dict[str, Dict[str, int]]:
    """Return a deep-copied snapshot keyed by call site.

    Always includes the four canonical call sites with zeroed buckets so the
    response shape is stable from process start. Any other call sites that have
    been recorded (future expansions) are included as well.
    """
    snapshot: Dict[str, Dict[str, int]] = {
        site: _empty_pr_is_not_behind_bucket() for site in PR_IS_NOT_BEHIND_CALL_SITES
    }
    for site, bucket in pr_is_not_behind_counts.items():
        snapshot[site] = dict(bucket)
    return snapshot


# --- Warm backlog-reuse circuit breaker (efficiency direction d-91d8dede) ---
# Each entry: (timestamp, reason). Sustained failures → auto-suppression
# for a bounded cooldown before the backlog-reuse path resumes.
# Bounded deque; pruned by sweep_stale_in_memory_state.
warm_backlog_reuse_failure_events: deque = deque(maxlen=200)


def record_backlog_reuse_failure(reason: str = "task_failed") -> None:
    """Record a backlog-reuse failure event (FAILED task or finalize error).

    Called from the /next-role finalize-error path and from agent_status when
    a FAILED task originated from a backlog_reuse warm claim.
    """
    warm_backlog_reuse_failure_events.append((_utcnow(), reason))


def count_recent_backlog_reuse_failures(window_seconds: int = 3600) -> int:
    """Count backlog-reuse failure events in the trailing window."""
    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    return sum(1 for ts, _ in warm_backlog_reuse_failure_events if ts >= cutoff)


# --- Warm-pool claim circuit breaker (efficiency direction d-5fd69fe1) ---
# Each entry: (timestamp, reason). A pool lease that errors (lease/finalize
# exception, a leased VM found to be in-flight-deleting) or a pool-served claim
# that ends in a FAILED task appends here. Sustained failures — the classic
# "a bad pool image silently fails a stream of tasks" mode — auto-suppress the
# pool for a bounded cooldown (see shared_state.is_warm_pool_suppressed) so the
# maintainer stops replenishing and the orchestrator dispatch hook stops leasing
# (falling back to cold provisioning) until the rate normalises. Bounded deque;
# pruned by sweep_stale_in_memory_state.
warm_pool_claim_failure_events: deque = deque(maxlen=200)


def record_pool_claim_failure(reason: str = "lease_error") -> None:
    """Record a warm-pool claim failure event.

    Called from the orchestrator dispatch hook (lease error / doomed VM) and the
    /next-role pool finalize-error path. Feeds the pool circuit breaker.
    """
    warm_pool_claim_failure_events.append((_utcnow(), reason))


def count_recent_pool_claim_failures(window_seconds: int = 3600) -> int:
    """Count warm-pool claim failure events in the trailing window."""
    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    return sum(1 for ts, _ in warm_pool_claim_failure_events if ts >= cutoff)


# --- Warm backlog-reuse first-refusal (efficiency direction d-91d8dede) ---
# project_id (str) -> first-refusal deadline (naive UTC). While a project has
# an unexpired entry, the orchestrator's cold-dispatch loop holds off
# cold-provisioning eligible backlog tasks for that project so an idle warm VM
# polling /api/agent/next-role gets first claim. Set/refreshed on each idle-VM
# poll (and when a COMPLETED task's teardown is deferred). The atomic
# SKIP-LOCKED claim in /next-role still guarantees no double-provision if both
# race — this only biases the race toward reuse. Bounded: entries are short
# (WARM_BACKLOG_FIRST_REFUSAL_SECS) and pruned by sweep_stale_in_memory_state.
WARM_BACKLOG_FIRST_REFUSAL_SECS = 20
warm_backlog_first_refusal: Dict[str, datetime] = {}


def register_backlog_first_refusal(
    project_id: str, window_seconds: int = WARM_BACKLOG_FIRST_REFUSAL_SECS
) -> None:
    """Mark project_id as having an idle warm VM that wants backlog work.

    Sets the first-refusal deadline to now + window_seconds. Called from the
    /next-role poll path so the orchestrator holds off cold-provisioning the
    project's eligible backlog while a warm VM is actively polling.
    """
    if not project_id:
        return
    warm_backlog_first_refusal[str(project_id)] = _utcnow() + timedelta(
        seconds=window_seconds
    )


def is_backlog_first_refusal_active(project_id: str) -> bool:
    """True iff project_id has an unexpired backlog first-refusal window."""
    if not project_id:
        return False
    deadline = warm_backlog_first_refusal.get(str(project_id))
    if deadline is None:
        return False
    return _utcnow() <= deadline
