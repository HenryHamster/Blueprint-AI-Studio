"""Shared runtime state used by both main.py and admin.py.

Extracted to its own module to avoid circular imports when admin
endpoints need to read or mutate autonomous-loop / runtime-config state.
"""

import os
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Rate limiter — shared between main.py and admin.py
# Only enforced in Cloud Run (K_REVISION set), disabled locally for dev/test
# Uses Redis for cross-instance coordination when REDIS_URL is set,
# falls back to per-instance in-memory storage otherwise.
_redis_url = os.getenv("REDIS_URL")
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_redis_url if _redis_url else "memory://",
    enabled=bool(os.getenv("K_REVISION")),
)

# Lock for multi-key runtime config updates (prevents reading partial state between two assignments)
runtime_config_lock = asyncio.Lock()

# --- Runtime Config (adjustable via admin API) ---
runtime_config: Dict = {
    "max_concurrent_vms": int(os.getenv("MAX_CONCURRENT_VMS", "8")),
    "max_retries": int(os.getenv("MAX_RETRIES", "3")),
    "max_provision_retries": int(os.getenv("MAX_PROVISION_RETRIES", "3")),
    "max_rollup_retries": int(os.getenv("MAX_ROLLUP_RETRIES", "5")),
    "max_autonomous_tasks_per_hour": 5,
    "max_global_autonomous_tasks_per_hour": 50,  # Cross-project cap (default 10x per-project)
    "autonomous_research_interval_minutes": 30,
    # Per-task-type model/effort defaults (synced from frontend settings)
    # e.g. {"research": "claude-opus-4-6", "create": "claude-sonnet-4-6"}
    "task_type_models": {"research": "claude-opus-4-6"},
    "task_type_efforts": {"research": "max"},
    # Per-type extra instructions appended to the agent's base prompt
    # e.g. {"research": "Focus especially on performance bottlenecks.", "qa": "Use pytest only."}
    "agent_prompt_suffixes": {},
    # Periodic autonomous spawning for non-research task types (opt-in)
    "autonomous_periodic_tasks": {
        "security_audit": {"interval_hours": 168, "enabled": False},  # weekly
        "qa": {"interval_hours": 72, "enabled": False},               # every 3 days
        "deps": {"interval_hours": 168, "enabled": False},            # weekly
    },
    "autonomous_direction_cooldown_minutes": 240,
    "direction_auto_recovery_hours": 48,
    "autonomous_research_wave_size": 3,
    "strategic_research_frequency": 6,  # Every Nth research cycle is a strategic vision review
    "architecture_review_frequency": 3,  # Every Nth strategic cycle includes architecture (design doc) review
    "max_active_directions": 10,
    "max_parallel_research_per_project": 3,
    "require_approval_gates": True,
    "daily_cost_ceiling_usd": 0,  # 0 = disabled; positive float enables budget alerts
    "spawn_confidence_threshold": 0.25,  # P(success) below this injects low_confidence_spawn=True into task context
}

# --- Autonomous Loop State ---
autonomous_failure_counts: Dict[str, int] = {}  # project_id -> consecutive failures
last_idle_check: Optional[datetime] = None

# --- Persistent Anomaly Tracking ---
# project_id -> { anomaly_key -> {first_seen, last_seen, occurrence_count, severity, latest_details} }
persistent_anomalies: Dict[str, Dict[str, Dict]] = {}

# --- Circuit Breaker (Hystrix-inspired) ---
# Per-project circuit breaker state for autonomous task spawning
# States: CLOSED (normal), OPEN (tripped), HALF_OPEN (testing one task)
circuit_breakers: Dict[str, Dict] = {}  # project_id -> breaker state

def get_circuit_breaker(project_id: str) -> Dict:
    """Get or create circuit breaker state for a project."""
    if project_id not in circuit_breakers:
        circuit_breakers[project_id] = {
            "state": "CLOSED",           # CLOSED, OPEN, HALF_OPEN
            "failure_count": 0,          # consecutive code-quality failures (CODE_ERROR, AGENT_TIMEOUT)
            "infra_failure_count": 0,    # consecutive infra failures (PROVISION_FAILURE, RATE_LIMIT, EXTERNAL_API)
            "success_count": 0,          # consecutive successes (for half-open)
            "opened_at": None,           # when the breaker tripped
            "cooldown_seconds": 1800,    # 30 min initial cooldown
            "base_cooldown": 1800,       # for exponential backoff reset
            "half_open_task_id": None,   # the test task in HALF_OPEN
            "open_reason": None,         # "code" or "infra" — why the breaker opened
        }
    return circuit_breakers[project_id]

# --- Background Tasks (populated by lifespan, used by admin restart endpoint) ---
background_tasks: Dict[str, asyncio.Task] = {}  # name -> asyncio.Task

# --- Deployment Monitoring (Phase A: detect + alert) ---
deploy_monitoring: Dict = {
    "window_start": None,           # datetime when monitoring began for current deploy
    "window_duration": 900,         # 15 minutes default
    "extended_window": 1800,        # 30 min for large/infra changes
    "is_extended": False,           # set True when infra files are touched
    "task_results": deque(maxlen=100),  # (timestamp_iso, "success"|"failure", failure_class)
    "pre_deploy_failure_rate": 0.0, # baseline from before deploy
    "health_checks": {
        "dispatch_working": True,   # orchestrator picked up a task in last 5 min
        "completions_received": True, # at least one agent reported back
        "db_accessible": True,       # can read/write tasks
    },
    "last_dispatch_time": None,     # last time orchestrator dispatched a task
    "last_completion_time": None,   # last time an agent reported completion
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
PIPELINE_EVENT_TYPES = frozenset({
    "heartbeat_timeout_post_claim",
    "provision_timeout_pre_claim",
    "absolute_timeout",
    "never_started",
    "account_blacklisted",
})

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
_REAP_REASON_EVENT_TYPES = frozenset({
    "heartbeat_timeout_post_claim",
    "provision_timeout_pre_claim",
    "absolute_timeout",
    "never_started",
})


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
            1 for e in deploy_monitoring["pipeline_events"]
            if e.get("ts") and e["ts"] >= cutoff and e.get("type") in _REAP_REASON_EVENT_TYPES
        )
    return sum(
        1 for e in deploy_monitoring["pipeline_events"]
        if e.get("ts") and e["ts"] >= cutoff and e.get("type") == event_type
    )


def get_pipeline_health_snapshot() -> Dict[str, Any]:
    """Snapshot for /api/health/detailed: rolling counts, last-seen timestamps,
    thresholds, and which alert categories are currently above threshold."""
    counts: Dict[str, int] = {}
    last_event_ts: Dict[str, Optional[str]] = {}
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
        et for et, threshold in PIPELINE_ALERT_THRESHOLDS.items()
        if counts.get(et, 0) >= threshold
    ]

    return {
        "counts_1h": counts,
        "last_event_ts": last_event_ts,
        "thresholds": dict(PIPELINE_ALERT_THRESHOLDS),
        "alerts_active": alerts_active,
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
        _post_pipeline_alert(category, count, threshold)


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
        extra={"alert": True, "type": "pipeline_health", "category": category,
               "count": count, "threshold": threshold},
    )
    try:
        from app.main import _send_webhook_notification  # noqa: PLC0415
        # Detached task — webhook is fire-and-forget by design.
        # asyncio.get_running_loop() raises RuntimeError outside an event loop;
        # that's the correct signal to skip dispatch (e.g. sync tests).
        loop = asyncio.get_running_loop()
        loop.create_task(
            _send_webhook_notification("PIPELINE_HEALTH_THRESHOLD", title, message)
        )
    except RuntimeError:
        pass
    except Exception as e:
        logger.warning(f"[PIPELINE_HEALTH] webhook dispatch skipped: {e}")

# --- Prometheus-compatible metrics counters ---
# Gauges: updated by orchestrator each cycle
tasks_queued_gauge: int = 0
tasks_active_gauge: int = 0
# Counters: cumulative totals, incremented on terminal state transitions
tasks_completed_total: int = 0
tasks_failed_total: int = 0

# --- External Dependency Health ---
external_health: Dict = {
    "github_api": True,
    "claude_api": True,
    "gcp": True,
    "db": True,
    "last_check": None,             # datetime of last health check
    "failed_deps": [],              # list of currently-failed dependency names
    "errors": {},                   # dep_name -> last error message
}

# Track last successful GitHub API operation (tokens, PR comments, etc.)
# Used by health check to passively verify GitHub is reachable without
# making an active API call that competes for the shared connection pool.
# Seed with current time so the first health check uses the passive path
# instead of an active probe that can fail on cold start.
last_github_success: Optional[datetime] = datetime.utcnow()  # Module-level init, _utcnow not available here
