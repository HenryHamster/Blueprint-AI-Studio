"""Deploy-monitoring / pipeline-health / metrics / dependency-health process state.

Split out of shared_state. Owns the deploy-monitoring ring buffers, the
pipeline-health event taxonomy + alerting, the in-process Prometheus counters,
external-dependency + GitHub-quota tracking, the dispatcher-cycle log, the
reaper halt-state accessors, and the periodic stale-state sweep. Re-exported
from :mod:`app.shared_state` as a thin facade. Behaviour-preserving.

The ``_post_pipeline_alert`` call inside ``_maybe_fire_pipeline_alert`` is
resolved through ``app.shared_state`` (the facade) at call time so existing
tests that ``patch("app.shared_state._post_pipeline_alert")`` keep working
after the split.
"""

import os
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict

from app.time_utils import _utcnow

# In-memory state owned by other split modules but pruned/reported here.
from app.anomaly_state import autonomous_failure_counts, circuit_breakers
from app.direction_streaks import direction_failure_streaks, get_held_directions
from app.warm_vm_state import (
    MERGE_PATTERN_WINDOW_SECONDS,
    merge_error_occurrences,
    sr_catch_rate_last_refreshed,
    user_debug_spawn_times,
    vm_last_heartbeat,
    warm_backlog_first_refusal,
    warm_backlog_reuse_failure_events,
    warm_pin_clear_events,
    warm_pool_claim_failure_events,
)

logger = logging.getLogger(__name__)

# --- Background Tasks (populated by lifespan, used by admin restart endpoint) ---
background_tasks: Dict[str, asyncio.Task] = {}  # name -> asyncio.Task

# NOTE: ``is_leader`` and the Prometheus gauge/counter scalars
# (tasks_queued_gauge / tasks_active_gauge / tasks_completed_total /
# tasks_failed_total) plus ``last_github_success`` /
# ``github_call_counts_window_reset_at`` are *rebindable* module attributes that
# producers write through the facade (``shared_state.is_leader = ...``) and
# readers read through the facade. They therefore live on the
# :mod:`app.shared_state` facade itself, not here — re-exporting an immutable
# scalar would diverge the moment the facade attribute is rebound.

# --- Deployment Monitoring (Phase A: detect + alert) ---
deploy_monitoring: Dict = {
    "window_start": None,  # datetime when monitoring began for current deploy
    "window_duration": 900,  # 15 minutes default
    "extended_window": 1800,  # 30 min for large/infra changes
    "is_extended": False,  # set True when infra files are touched
    "task_results": deque(
        maxlen=100
    ),  # (timestamp_iso, "success"|"failure", failure_class, task_type)
    "pre_deploy_failure_rate": 0.0,  # baseline from before deploy
    "health_checks": {
        "dispatch_working": True,  # orchestrator picked up a task in last 5 min
        "completions_received": True,  # at least one agent reported back
        "db_accessible": True,  # can read/write tasks
    },
    "last_dispatch_time": None,  # last time orchestrator dispatched a task
    "last_completion_time": None,  # last time an agent reported completion
    "consecutive_health_failures": 0,
    # Bounded ring buffer of pipeline-health events written by the reaper.
    # Each entry: {"type": str, "ts": datetime, "task_id": str,
    #              "vm_id": Optional[str], "zone": Optional[str],
    #              "account_id": Optional[str]}
    "pipeline_events": deque(maxlen=500),
    # event_type -> last datetime an alert fired (for dedup window)
    "pipeline_alert_last_ts": {},
}

# Pipeline-health event taxonomy. The reaper writes one of these into
# deploy_monitoring["pipeline_events"] at each terminal decision point.
PIPELINE_EVENT_TYPES = frozenset(
    {
        "heartbeat_timeout_post_claim",
        "provision_timeout_pre_claim",
        "absolute_timeout",
        "never_started",
        "account_blacklisted",
    }
)

# Threshold counts (per 1h window) that trigger a system-level alert.
# "reap_rate_per_hour" aggregates all reap-reason events to catch
# cascading infra failure even when no single category trips.
PIPELINE_ALERT_THRESHOLDS = {
    "heartbeat_timeout_post_claim": int(os.getenv("ALERT_HEARTBEAT_TIMEOUT", "10")),
    "provision_timeout_pre_claim": int(os.getenv("ALERT_PROVISION_TIMEOUT", "5")),
    "account_blacklisted": int(os.getenv("ALERT_ACCOUNT_BLACKLIST", "3")),
    "reap_rate_per_hour": int(os.getenv("ALERT_REAP_RATE", "15")),
}
PIPELINE_ALERT_DEDUP_SECONDS = 1800

# Reap-reason codes that contribute to the aggregate reap_rate counter.
_REAP_REASON_EVENT_TYPES = frozenset(
    {
        "heartbeat_timeout_post_claim",
        "provision_timeout_pre_claim",
        "absolute_timeout",
        "never_started",
    }
)


def record_pipeline_event(event_type: str, **kwargs: Any) -> None:
    """Append a pipeline-health event and, if a threshold is crossed, fire an alert.

    Unknown event types are dropped silently so callers don't break the reaper
    on a typo — the snapshot endpoint will simply not expose them.
    """
    if event_type not in PIPELINE_EVENT_TYPES:
        return
    event = {
        "type": event_type,
        "ts": _utcnow(),
        "task_id": kwargs.get("task_id"),
        "vm_id": kwargs.get("vm_id"),
        "zone": kwargs.get("zone"),
        "account_id": kwargs.get("account_id"),
    }
    deploy_monitoring["pipeline_events"].append(event)
    _maybe_fire_pipeline_alert(event_type)


def count_pipeline_events(event_type: str, window_seconds: int = 3600) -> int:
    """Count events of `event_type` (or all reap reasons for "reap_rate_per_hour")
    within the trailing `window_seconds`."""
    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    if event_type == "reap_rate_per_hour":
        return sum(
            1
            for e in deploy_monitoring["pipeline_events"]
            if e.get("ts")
            and e["ts"] >= cutoff
            and e.get("type") in _REAP_REASON_EVENT_TYPES
        )
    return sum(
        1
        for e in deploy_monitoring["pipeline_events"]
        if e.get("ts") and e["ts"] >= cutoff and e.get("type") == event_type
    )


def get_pipeline_health_snapshot() -> Dict[str, Any]:
    """Snapshot for /api/health/detailed: rolling counts, last-seen timestamps,
    thresholds, and which alert categories are currently above threshold."""
    counts: Dict[str, int] = {}
    last_event_ts: Dict[str, str | None] = {}
    for et in PIPELINE_EVENT_TYPES:
        counts[et] = count_pipeline_events(et)
        last_event_ts[et] = None
    counts["reap_rate_per_hour"] = count_pipeline_events("reap_rate_per_hour")

    for e in deploy_monitoring["pipeline_events"]:
        et = e.get("type")
        ts = e.get("ts")
        if et in PIPELINE_EVENT_TYPES and ts is not None:
            iso = ts.isoformat() if isinstance(ts, datetime) else str(ts)
            prev = last_event_ts.get(et)
            if prev is None or iso > prev:
                last_event_ts[et] = iso

    alerts_active = [
        et
        for et, threshold in PIPELINE_ALERT_THRESHOLDS.items()
        if counts.get(et, 0) >= threshold
    ]

    return {
        "counts_1h": counts,
        "last_event_ts": last_event_ts,
        "thresholds": dict(PIPELINE_ALERT_THRESHOLDS),
        "alerts_active": alerts_active,
        "held_directions": get_held_directions(),
    }


def _maybe_fire_pipeline_alert(event_type: str) -> None:
    """Check whether `event_type` (and the aggregate reap_rate) crossed
    threshold and post a deduplicated system alert if so.

    Posts via logger.error + best-effort webhook so operators can wire alerts
    without depending on the project-scoped Alert table.
    """
    candidates = [event_type]
    if event_type in _REAP_REASON_EVENT_TYPES:
        candidates.append("reap_rate_per_hour")

    for category in candidates:
        threshold = PIPELINE_ALERT_THRESHOLDS.get(category)
        if threshold is None:
            continue
        count = count_pipeline_events(category)
        if count < threshold:
            continue
        last_ts = deploy_monitoring["pipeline_alert_last_ts"].get(category)
        now = _utcnow()
        if last_ts and (now - last_ts).total_seconds() < PIPELINE_ALERT_DEDUP_SECONDS:
            continue
        deploy_monitoring["pipeline_alert_last_ts"][category] = now
        # Resolve via the facade so patch("app.shared_state._post_pipeline_alert")
        # (used by the pipeline-health tests) is honoured after the split.
        import app.shared_state as _facade

        _facade._post_pipeline_alert(category, count, threshold)


def _post_pipeline_alert(category: str, count: int, threshold: int) -> None:
    """Emit the alert via structured log + webhook. Failures here must never
    break the reaper, so all errors are swallowed with a warning."""
    title = f"Pipeline health threshold crossed: {category}"
    message = (
        f"{count} {category} events in the last hour "
        f"(threshold {threshold}). Inspect /api/health/detailed for context."
    )
    logger.error(
        title,
        extra={
            "alert": True,
            "type": "pipeline_health",
            "category": category,
            "count": count,
            "threshold": threshold,
        },
    )
    try:
        from app.notification import _send_webhook_notification

        # Detached task — webhook is fire-and-forget by design.
        # asyncio.get_running_loop() raises RuntimeError outside an event loop;
        # that's the correct signal to skip dispatch (e.g. sync tests).
        loop = asyncio.get_running_loop()
        loop.create_task(  # noqa: RUF006
            _send_webhook_notification("PIPELINE_HEALTH_THRESHOLD", title, message)
        )
    except RuntimeError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[PIPELINE_HEALTH] webhook dispatch skipped: {e}")


# Retention thresholds for sweep_stale_in_memory_state. Tuned so entries that
# stopped carrying signal a while ago are reclaimed, while anything still in
# use (recent failures, current holds, live sliding windows) is preserved.
_STREAK_RETENTION_HOURS = 24
_BREAKER_RETENTION_HOURS = 24
_MERGE_ERROR_RETENTION_SECONDS = MERGE_PATTERN_WINDOW_SECONDS * 2
_DEBUG_SPAWN_RETENTION_SECONDS = 3600 * 2


def sweep_stale_in_memory_state() -> Dict[str, int]:
    """Prune entries from growing module-level state dicts.

    Each of these dicts gets populated by a unique key (direction_id,
    project_id, normalized error, etc.) and was not being removed once the
    underlying condition resolved. Over an instance's lifetime the keyspace
    grows monotonically — on a long-lived Cloud Run instance this contributes
    to the 4GB OOMs. Callers invoke this on a periodic cadence.

    Returns a summary dict of how many entries were pruned per bucket.
    """
    now = _utcnow()
    pruned = {
        "direction_failure_streaks": 0,
        "circuit_breakers": 0,
        "autonomous_failure_counts": 0,
        "merge_error_occurrences": 0,
        "user_debug_spawn_times": 0,
        "vm_last_heartbeat": 0,
    }

    streak_cutoff = now - timedelta(hours=_STREAK_RETENTION_HOURS)
    for did, streak in list(direction_failure_streaks.items()):
        if streak.get("held"):
            continue
        if streak.get("consecutive_failures", 0) or streak.get(
            "consecutive_infra_failures", 0
        ):
            continue
        last = streak.get("last_failure_at")
        if last is None or last < streak_cutoff:
            del direction_failure_streaks[did]
            pruned["direction_failure_streaks"] += 1

    breaker_cutoff = now - timedelta(hours=_BREAKER_RETENTION_HOURS)
    for pid, breaker in list(circuit_breakers.items()):
        if breaker.get("state") != "CLOSED":
            continue
        if breaker.get("failure_count", 0) or breaker.get("infra_failure_count", 0):
            continue
        opened_at = breaker.get("opened_at")
        if opened_at is None or opened_at < breaker_cutoff:
            del circuit_breakers[pid]
            pruned["circuit_breakers"] += 1

    for pid, count in list(autonomous_failure_counts.items()):
        if not count:
            del autonomous_failure_counts[pid]
            pruned["autonomous_failure_counts"] += 1

    merge_cutoff = now - timedelta(seconds=_MERGE_ERROR_RETENTION_SECONDS)
    for key, dq in list(merge_error_occurrences.items()):
        while dq and dq[0][1] < merge_cutoff:
            dq.popleft()
        if not dq:
            del merge_error_occurrences[key]
            pruned["merge_error_occurrences"] += 1

    debug_cutoff = now - timedelta(seconds=_DEBUG_SPAWN_RETENTION_SECONDS)
    for pid, dq in list(user_debug_spawn_times.items()):
        while dq and dq[0] < debug_cutoff:
            dq.popleft()
        if not dq:
            del user_debug_spawn_times[pid]
            pruned["user_debug_spawn_times"] += 1

    # Warm-VM heartbeat map: prune entries older than 1h. The orchestrator
    # only honors warm-pin VMs whose heartbeat is within 90s, so any entry
    # past 1h is permanently stale and only contributes to memory growth.
    vm_heartbeat_cutoff = now - timedelta(hours=1)
    for vm_id, last in list(vm_last_heartbeat.items()):
        if last < vm_heartbeat_cutoff:
            del vm_last_heartbeat[vm_id]
            pruned["vm_last_heartbeat"] += 1

    # SR catch-rate refresh debounce map: prune entries older than 1h. The
    # debounce window is minutes, so any entry past 1h has long expired and
    # only contributes to keyspace growth on a long-lived instance.
    pruned["sr_catch_rate_last_refreshed"] = 0
    sr_refresh_cutoff = now - timedelta(hours=1)
    for pid, last in list(sr_catch_rate_last_refreshed.items()):
        if last < sr_refresh_cutoff:
            del sr_catch_rate_last_refreshed[pid]
            pruned["sr_catch_rate_last_refreshed"] += 1

    # Warm-pin clear-out events: bounded deque (maxlen=200) auto-evicts;
    # this prune pass drops anything outside the 24h reporting window so
    # count_recent_warm_pin_clears stays cheap.
    warm_clear_cutoff = now - timedelta(hours=24)
    pruned["warm_pin_clear_events"] = 0
    while warm_pin_clear_events and warm_pin_clear_events[0][0] < warm_clear_cutoff:
        warm_pin_clear_events.popleft()
        pruned["warm_pin_clear_events"] += 1

    # Backlog first-refusal: entries are short-lived (≈20s); drop anything
    # already expired so the map can't grow on a long-lived instance.
    pruned["warm_backlog_first_refusal"] = 0
    for pid, deadline in list(warm_backlog_first_refusal.items()):
        if deadline < now:
            del warm_backlog_first_refusal[pid]
            pruned["warm_backlog_first_refusal"] += 1

    # Backlog reuse failure events: bounded deque (maxlen=200) auto-evicts;
    # prune events older than 24h so count_recent_backlog_reuse_failures stays cheap.
    backlog_failure_cutoff = now - timedelta(hours=24)
    pruned["warm_backlog_reuse_failure_events"] = 0
    while (
        warm_backlog_reuse_failure_events
        and warm_backlog_reuse_failure_events[0][0] < backlog_failure_cutoff
    ):
        warm_backlog_reuse_failure_events.popleft()
        pruned["warm_backlog_reuse_failure_events"] += 1

    # Pool-claim failure events: bounded deque (maxlen=200) auto-evicts; prune
    # events older than 24h so count_recent_pool_claim_failures stays cheap.
    pool_failure_cutoff = now - timedelta(hours=24)
    pruned["warm_pool_claim_failure_events"] = 0
    while (
        warm_pool_claim_failure_events
        and warm_pool_claim_failure_events[0][0] < pool_failure_cutoff
    ):
        warm_pool_claim_failure_events.popleft()
        pruned["warm_pool_claim_failure_events"] += 1

    return pruned


def get_task_type_failure_rate(
    task_type: str, window_minutes: int = 15
) -> tuple[float, int]:
    """Compute failure rate for a specific task_type within the trailing window.

    Backward-compatible: entries without a task_type field (old format or empty
    string) are treated as matching any type filter and included in the sample.

    Returns (rate, sample_size).  Returns (0.0, 0) when no samples exist.
    """
    cutoff = (_utcnow() - timedelta(minutes=window_minutes)).isoformat()
    relevant = [
        r
        for r in deploy_monitoring["task_results"]
        if r[0] >= cutoff and (len(r) < 4 or not r[3] or r[3] == task_type)
    ]
    if not relevant:
        return 0.0, 0
    failures = sum(1 for r in relevant if r[1] == "failure")
    return failures / len(relevant), len(relevant)


# --- External Dependency Health ---
external_health: Dict = {
    "github_api": True,
    "anthropic_api": True,
    "openai_api": True,
    "gemini_api": True,
    "dispatch_api": True,
    "gcp": True,
    "db": True,
    "provider_health": {},  # provider_name -> structured provider health snapshot
    "dispatch_health": {},  # aggregate dispatchability summary
    "last_check": None,  # datetime of last health check
    "failed_deps": [],  # list of currently-failed dependency names
    "errors": {},  # dep_name -> last error message
    "consecutive_failures": {},  # dep_name -> int count of consecutive failed health checks
}

# --- GitHub API Quota Tracking ---
# Populated on every _github_request by reading X-RateLimit-Remaining /
# X-RateLimit-Reset headers off the response. Surfaced in /api/health/detailed
# and drives the dashboard quota widget + WARNING alert when remaining < 100.
# Treat None values as "not yet observed — no recent GitHub call".
github_quota: Dict = {
    "remaining": None,  # int, lowest remaining seen since last reset
    "limit": None,  # int, 5000 for GitHub App installs
    "reset_at": None,  # int unix ts, when quota resets
    "last_updated": None,  # isoformat str, last observation
}

# Separate bucket for App-JWT (~60/hr). Used by /app/installations/*/access_tokens
# and other /app/* endpoints that authenticate with a GitHub App JWT rather than
# an installation token. Tracked separately so we don't mix the two — a drained
# JWT bucket stalls token minting (which in turn stalls everything else), and we
# want to short-circuit calls to that bucket when it's empty instead of retrying
# and counting three 403s per logical request.
github_jwt_quota: Dict = {
    "remaining": None,
    "limit": None,
    "reset_at": None,
    "last_updated": None,
}

# --- GitHub REST Call Counters ---
# Every _github_request bumps the count for its normalized endpoint template
# (e.g. "GET /repos/{owner}/{repo}/pulls/{n}") so we can see which endpoints
# actually consume the 5000/hr REST budget. Reset on each quota-reset window
# rollover so the dashboard shows "this window"'s burn. Template normalization
# (numbers, SHAs, branch refs) collapses the natural cardinality explosion.
github_call_counts: Dict[str, int] = {}
# NOTE: ``github_call_counts_window_reset_at`` is a *rebindable* scalar written
# through the facade (``shared_state.github_call_counts_window_reset_at = ...``),
# so it lives on the :mod:`app.shared_state` facade, not here.

# Cache-hit counters — exposed in /api/health/detailed so we can see whether
# the dedup caches are actually firing in production.  Each entry here is a
# request we didn't have to send to GitHub.  Reset on the same window as
# github_call_counts so "hits per window" is directly comparable to "calls
# per window" — if hits dominate, the caches are earning their keep.
github_cache_hits: Dict[str, int] = {
    "check_runs": 0,
    "scan_pr": 0,
    "pr_merge_negative": 0,
    "bucket_short_circuit": 0,
}

# NOTE: ``last_github_success`` is a *rebindable* scalar written/read through
# the facade (``shared_state.last_github_success``), so it lives on the
# :mod:`app.shared_state` facade, not here.


# --- Dispatcher Cycle Log (ring buffer for throughput/gating observability) ---
# Bounded to the last 1000 records; entries older than 30 days are excluded by
# get_dispatcher_cycles() callers but not proactively evicted (deque handles FIFO).
# Each record is a plain dict so it stays JSON-serialisable without extra deps.
_DISPATCHER_CYCLE_LOG_MAXLEN = 1000
_DISPATCHER_CYCLE_TTL_DAYS = 30

dispatcher_cycle_log: deque = deque(maxlen=_DISPATCHER_CYCLE_LOG_MAXLEN)


def record_dispatcher_cycle(
    *,
    project_id: str,
    cycle_started_at: datetime,
    cycle_finished_at: datetime,
    signals_evaluated: int,
    signals_tripped: int,
    spawns_emitted: int,
    rejections_by_reason: Dict[str, int] | None = None,
    mttd_p50_sec: float | None = None,
) -> None:
    """Append one dispatcher cycle record to the ring buffer.

    Called at the end of each pipeline_regression_dispatcher.run_once() tick.
    The deque maxlen caps memory; get_dispatcher_cycles() applies the 30-day TTL.
    """
    dispatcher_cycle_log.append(
        {
            "project_id": project_id,
            "cycle_started_at": cycle_started_at.isoformat(),
            "cycle_finished_at": cycle_finished_at.isoformat(),
            "signals_evaluated": signals_evaluated,
            "signals_tripped": signals_tripped,
            "spawns_emitted": spawns_emitted,
            "rejections_by_reason": dict(rejections_by_reason or {}),
            "mttd_p50_sec": mttd_p50_sec,
        }
    )


def get_dispatcher_cycles(
    project_id: str | None = None,
    window_hours: int = 24,
) -> list[Dict]:
    """Return dispatcher cycle records within the trailing window.

    Filters to `project_id` when provided; returns all projects otherwise.
    Records older than `window_hours` are excluded.
    """
    cutoff = (_utcnow() - timedelta(hours=window_hours)).isoformat()
    return [
        r
        for r in dispatcher_cycle_log
        if r.get("cycle_started_at", "") >= cutoff
        and (project_id is None or r.get("project_id") == project_id)
    ]


# --- Reaper Cycle-Level Halt State Access ---
# The authoritative halt state lives in reaper._reaper_halt_state (module-local
# to avoid a circular import — reaper.py already imports from shared_state.py).
# These helpers provide cross-module access via lazy imports so admin routers
# and the metrics endpoint can read and reset the halt state without importing
# reaper.py directly.


def get_reaper_halt_state() -> Dict[str, Any]:
    """Snapshot of the reaper's cycle-level circuit breaker state.

    Lazily imports from app.reaper to avoid circular imports at module load.
    Returns a safe default dict when the reaper module is not yet imported.
    """
    try:
        import app.reaper as _reaper_mod

        state = _reaper_mod._reaper_halt_state
        ts = state.get("halt_timestamp")
        return {
            "halted": bool(state.get("halted")),
            "halt_timestamp": ts.isoformat() if ts else None,
            "last_error": state.get("last_error"),
            "crash_count": state.get("crash_count", 0),
            "hard_halted": bool(state.get("hard_halted")),
            "half_open_failures": state.get("half_open_failures", 0),
        }
    except Exception:  # noqa: BLE001
        return {
            "halted": False,
            "halt_timestamp": None,
            "last_error": None,
            "crash_count": 0,
            "hard_halted": False,
            "half_open_failures": 0,
        }


def reset_reaper_halt_state() -> bool:
    """Reset the reaper cycle-level circuit breaker (operator manual reset).

    Clears the in-memory halt state so the reaper service resumes on its next
    iteration.  Returns True if the reaper was halted and is now cleared,
    False if it was not halted.
    """
    try:
        import app.reaper as _reaper_mod

        state = _reaper_mod._reaper_halt_state
        if not state.get("halted"):
            return False
        state.update(
            {
                "halted": False,
                "halt_timestamp": None,
                "last_error": None,
                "crash_count": 0,
                "hard_halted": False,
                "half_open_failures": 0,
            }
        )
        logger.info("[REAPER] Halt state cleared by operator reset.")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[REAPER] reset_reaper_halt_state failed: {e}")
        return False
