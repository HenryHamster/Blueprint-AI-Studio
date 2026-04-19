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

from app.failure_classification import INFRA_CLASSES as _DIRECTION_INFRA_CLASSES
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Canonical default for task scope when not explicitly set.
# Operational modules (orchestrator, review pipeline, research completion) use this.
# Analytics/tracking modules use "unknown" to distinguish missing data from a real scope.
DEFAULT_TASK_SCOPE = "small"

# Maps calibrated task scope to a baseline effort level used at spawn time.
# small work runs faster/cheaper; large work gets deeper decomposition/analysis.
# Used by research_completion and task_completion as a floor on top of
# finding/parent-specified effort via _effort_rank (finding wins when higher).
_SCOPE_EFFORT_MAP = {"small": "low", "medium": "medium", "large": "high"}


def scope_to_effort(scope: Optional[str]) -> str:
    """Return the baseline effort for a calibrated scope.

    Unknown / missing scopes fall back to "medium" so callers can safely take
    the max against other effort signals without suppressing them.
    """
    return _SCOPE_EFFORT_MAP.get(scope or "", "medium")

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
    "max_auto_pause_duration_seconds": int(os.getenv("MAX_AUTO_PAUSE_DURATION_SECONDS", "7200")),
    "auto_resume_grace_period_seconds": int(os.getenv("AUTO_RESUME_GRACE_PERIOD_SECONDS", "300")),
    # Flap detection: N auto-resumes inside `flap_window_seconds` escalates
    # (ERROR alert, raised recovery bar, doubled grace period).
    "flap_window_seconds": int(os.getenv("FLAP_WINDOW_SECONDS", "1800")),
    "flap_threshold": int(os.getenv("FLAP_THRESHOLD", "3")),
    "max_retries": int(os.getenv("MAX_RETRIES", "3")),
    "max_provision_retries": int(os.getenv("MAX_PROVISION_RETRIES", "3")),
    "max_rollup_retries": int(os.getenv("MAX_ROLLUP_RETRIES", "5")),
    "queued_age_alert_hours": int(os.getenv("QUEUED_AGE_ALERT_HOURS", "2")),
    "retry_after_reset_alert_threshold": int(os.getenv("RETRY_AFTER_RESET_ALERT_THRESHOLD", "8")),
    "retry_after_reset_fail_threshold": int(os.getenv("RETRY_AFTER_RESET_FAIL_THRESHOLD", "16")),
    "max_autonomous_tasks_per_hour": 5,
    "max_global_autonomous_tasks_per_hour": 50,  # Cross-project cap (default 10x per-project)
    "autonomous_research_interval_minutes": 30,
    # Per-task-type model/effort defaults (synced from frontend settings)
    # All tasks use opus-4-7; effort varies by type:
    #   max: strategic_review, design_doc_update
    #   high: research, architecture (design_doc)
    #   medium: create, refactor, plan, review, fix, qa, deps, docs, migrate, security_audit, perf
    #   low: smoketest and other quick checks
    "task_type_models": {
        "strategic_review": "claude-opus-4-7",
        "design_doc_update": "claude-opus-4-7",
        "research": "claude-opus-4-7",
        "create": "claude-opus-4-7",
        "refactor": "claude-opus-4-7",
        "plan": "claude-opus-4-7",
        "review": "claude-opus-4-7",
        "fix": "claude-opus-4-7",
        "qa": "claude-opus-4-7",
        "deps": "claude-opus-4-7",
        "docs": "claude-opus-4-7",
        "migrate": "claude-opus-4-7",
        "security_audit": "claude-opus-4-7",
        "perf": "claude-opus-4-7",
        "smoketest": "claude-opus-4-7",
        "debug": "claude-opus-4-7",
    },
    "task_type_efforts": {
        "strategic_review": "max",
        "design_doc_update": "max",
        "research": "high",
        "create": "medium",
        "refactor": "medium",
        "plan": "medium",
        "review": "medium",
        "fix": "medium",
        "qa": "medium",
        "deps": "medium",
        "docs": "medium",
        "migrate": "medium",
        "security_audit": "medium",
        "perf": "medium",
        "smoketest": "low",
        "debug": "medium",
    },
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
    "strategic_research_frequency": 6,  # Every Nth research cycle is a strategic vision review
    "architecture_review_frequency": 3,  # Every Nth strategic cycle includes architecture (design doc) review
    "max_active_directions": 10,
    "max_parallel_research_per_project": 3,
    "require_approval_gates": True,
    "daily_cost_ceiling_usd": 0,  # 0 = disabled; positive float enables budget alerts
    "spawn_confidence_threshold": 0.25,  # P(success) below this injects low_confidence_spawn=True into task context
    # During degraded operation only, tasks whose P(success) falls below this floor
    # are skipped this dispatch cycle (remain QUEUED) so scarce slots go to higher-
    # confidence work. Does not apply during healthy operation.
    "dispatch_confidence_floor": 0.10,
    # Fraction of low-confidence medium-scope findings that bypass the escalation
    # gate and run as medium tasks, breaking the self-reinforcing feedback loop
    # where no medium tasks run → medium pipeline rate stays low → all medium
    # findings escalate. Set to 0 to disable exploration.
    "medium_exploration_fraction": 0.2,
    # TTL (days) for deferred findings carried forward by autonomous_research.
    # Findings older than this window are dropped rather than reprocessed, so
    # stale context doesn't keep surfacing work the system already declined to
    # spawn. Mirrors the hardcoded default in autonomous_research.py.
    "deferred_finding_ttl_days": 7,
    # Pipeline-wide scope merge-rate baselines used for cold-start scope calibration
    # when a direction/project has insufficient research_history. Default is empty
    # (feature disabled) — the prior defaults {"medium": 0.04, ...} were snapshotted
    # from an 8h window with an atypical merge-rate dip and permanently biased
    # cold-start toward large(decompose) even when medium was fine in aggregate.
    # Operators can populate this dict via runtime_config to re-enable cold-start
    # upgrades once there's enough real data to compute meaningful baselines.
    "pipeline_scope_baseline_rates": {},
    # Pipeline-wide merge rate per scope (e.g. {"medium": 0.04}). Acts as a
    # weakest-signal fallback in predict_spawn_success when direction- and
    # project-level scope_breakdown data are too sparse to fire. Auto-populated
    # by direction_outcomes._refresh_pipeline_scope_merge_rates after each
    # research_history update — only scopes with >= 5 effective data points are
    # published so the fallback stays conservative during early project state.
    "pipeline_scope_merge_rates": {},
    # Per-failure-class retry budgets: max attempt_count before permanent failure.
    # Overrides max_retries for specific failure classes. Operator-adjustable.
    #   CODE_ERROR:    2 — code won't self-correct; 3rd attempt wastes compute
    #   EXTERNAL_API:  4 — transient outages (GitHub 502, DNS) can last 15-60 min
    #   AGENT_TIMEOUT: 3 — keep current behavior with scope-reduction on 2nd timeout
    "retry_budgets_by_class": {
        "CODE_ERROR": 2,
        "EXTERNAL_API": 4,
        "AGENT_TIMEOUT": 3,
    },
    # Per-scope heartbeat-stale thresholds (seconds). Large-scope tasks
    # legitimately run longer stretches without a heartbeat, so reaping at the
    # small-scope bar flags them as dead while they're still making progress.
    "heartbeat_stale_seconds_by_scope": {
        "small": 300,
        "medium": 420,
        "large": 900,
    },
}

# Scope-aware per-class retry budgets. When a task's context includes a scope
# matching a key below, the scope-specific budget overrides retry_budgets_by_class.
# Missing scope (or a scope not in the inner dict) falls back to the uniform
# retry_budgets_by_class value. Rationale:
#   CODE_ERROR: small tasks are cheap to retry and often succeed with guidance;
#     large tasks failing with CODE_ERROR usually reflect a fundamental approach
#     problem — retrying burns compute without changing the outcome.
#   AGENT_TIMEOUT: large tasks legitimately need more time, so more timeout
#     retries are warranted; small tasks shouldn't need many retries.
#   EXTERNAL_API: transient third-party outages are not correlated with task
#     scope, but large-scope tasks are expensive to re-run from scratch, so
#     allow one extra retry over the uniform budget; small-scope tasks fail
#     cheaply and don't need the full budget.
SCOPE_AWARE_RETRY_BUDGETS: Dict[str, Dict[str, int]] = {
    "CODE_ERROR": {"small": 3, "medium": 2, "large": 1},
    "AGENT_TIMEOUT": {"small": 2, "medium": 3, "large": 4},
    "EXTERNAL_API": {"small": 3, "medium": 4, "large": 5},
}


def resolve_retry_budget(failure_class: str, scope: Optional[str]) -> int:
    """Return the retry budget for (failure_class, scope), falling back to the
    uniform retry_budgets_by_class value (or max_retries) if scope is not set
    or has no scope-specific override."""
    per_scope = SCOPE_AWARE_RETRY_BUDGETS.get(failure_class)
    if scope and per_scope and scope in per_scope:
        return per_scope[scope]
    return runtime_config.get("retry_budgets_by_class", {}).get(
        failure_class, runtime_config["max_retries"]
    )

# --- Autonomous Loop State ---
autonomous_failure_counts: Dict[str, int] = {}  # project_id -> consecutive failures
last_idle_check: Optional[datetime] = None

# --- Persistent Anomaly Tracking ---
# project_id -> { anomaly_key -> {first_seen, last_seen, occurrence_count, severity, latest_details} }
persistent_anomalies: Dict[str, Dict[str, Dict]] = {}

# --- Anomaly-Driven Pipeline Adjustments ---
# When high-severity anomalies (PROVISION_FAILURE, EXTERNAL_API) persist,
# the system automatically reduces blast radius by lowering max_concurrent_vms
# and tightening the failure-rate dispatch gate.
anomaly_adjustments: Dict[str, Any] = {
    "active": False,
    "adjusted_at": None,             # ISO timestamp when adjustments were applied
    "trigger_anomalies": [],         # anomaly keys that triggered the adjustment
    "pre_adjustment_max_vms": None,  # stored to restore on clearing
    "pre_adjustment_failure_gate": None,
    "failure_gate_threshold": 0.8,   # default; lowered to 0.6 during anomaly response
    "forced_restoration_count": 0,   # cumulative count of max-duration forced restorations
    "forced_restored_at": None,      # ISO timestamp of last forced restoration
    "adjusted_max_vms": None,        # target max_concurrent_vms we set; used to detect deploy_health/admin overrides
}

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

# --- User-submitted Auto-Debug Rate Limiting ---
# Tracks timestamps of user-submitted debug spawns per project to enforce hourly cap.
# Structure: project_id -> deque of datetime (UTC) of recent spawns
user_debug_spawn_times: Dict[str, deque] = {}

USER_DEBUG_MAX_PER_HOUR = 3  # max debug tasks spawned for user/admin tasks per project per hour

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

# --- Background Tasks (populated by lifespan, used by admin restart endpoint) ---
background_tasks: Dict[str, asyncio.Task] = {}  # name -> asyncio.Task

# --- Deployment Monitoring (Phase A: detect + alert) ---
deploy_monitoring: Dict = {
    "window_start": None,           # datetime when monitoring began for current deploy
    "window_duration": 900,         # 15 minutes default
    "extended_window": 1800,        # 30 min for large/infra changes
    "is_extended": False,           # set True when infra files are touched
    "task_results": deque(maxlen=100),  # (timestamp_iso, "success"|"failure", failure_class, task_type)
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

# --- Direction-Level Failure Streak Tracking ---
# direction_id -> {
#   consecutive_failures: int,
#   last_failure_class: str,
#   last_failure_at: datetime,
#   held: bool,
#   held_at: Optional[datetime],
#   project_id: str,
# }
direction_failure_streaks: Dict[str, Dict] = {}

# Number of consecutive CODE_ERROR failures before a direction is held
DIRECTION_HOLD_THRESHOLD = 3
# Higher threshold for consecutive infra failures (PROVISION_FAILURE, RATE_LIMIT,
# AGENT_TIMEOUT, EXTERNAL_API, SCOPE_OVERFLOW). A single infra failure is noise,
# but a sustained streak suggests the direction's scope/scale is the real cause
# (e.g. files that consistently OOM the agent, scope that always overflows).
DIRECTION_INFRA_HOLD_THRESHOLD = 6
# Automatic hold expiry (safety valve so a direction doesn't stay blocked forever)
DIRECTION_HOLD_TIMEOUT_HOURS = 4

# _DIRECTION_INFRA_CLASSES: imported from failure_classification at module top-level.
# Failure classes that are infrastructure-level and should NOT count toward
# the direction streak (the direction is blameless for infra failures).


def record_direction_result(
    direction_id: str,
    success: bool,
    failure_class: str = "",
    project_id: str = "",
) -> None:
    """Update the per-direction failure streak on task completion.

    Tracks two parallel counters: code failures (CODE_ERROR and other non-infra)
    and infra failures (PROVISION_FAILURE, RATE_LIMIT, AGENT_TIMEOUT, EXTERNAL_API,
    SCOPE_OVERFLOW). Each has its own hold threshold. A success resets both
    counters and clears any existing hold.
    """
    if not direction_id:
        return

    streak = direction_failure_streaks.setdefault(direction_id, {
        "consecutive_failures": 0,
        "consecutive_infra_failures": 0,
        "last_failure_class": "",
        "last_failure_at": None,
        "held": False,
        "held_at": None,
        "held_reason": "",
        "held_failure_class": "",
        "project_id": project_id,
    })
    # Backfill new fields for entries created before infra tracking existed.
    streak.setdefault("consecutive_infra_failures", 0)
    streak.setdefault("held_reason", "")
    streak.setdefault("held_failure_class", "")
    if project_id:
        streak["project_id"] = project_id

    if success:
        streak["consecutive_failures"] = 0
        streak["consecutive_infra_failures"] = 0
        streak["last_failure_class"] = ""
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""
        return

    if failure_class in _DIRECTION_INFRA_CLASSES:
        streak["consecutive_infra_failures"] += 1
        streak["last_failure_class"] = failure_class
        streak["last_failure_at"] = _utcnow()
        if (
            streak["consecutive_infra_failures"] >= DIRECTION_INFRA_HOLD_THRESHOLD
            and not streak["held"]
        ):
            streak["held"] = True
            streak["held_at"] = _utcnow()
            streak["held_reason"] = "infra"
            streak["held_failure_class"] = failure_class
            logger.warning(
                f"[DIRECTION HEALTH] Direction {direction_id} HELD after "
                f"{streak['consecutive_infra_failures']} consecutive infra "
                f"failures (last: {failure_class})."
            )
        return

    streak["consecutive_failures"] += 1
    streak["last_failure_class"] = failure_class
    streak["last_failure_at"] = _utcnow()
    if streak["consecutive_failures"] >= DIRECTION_HOLD_THRESHOLD and not streak["held"]:
        streak["held"] = True
        streak["held_at"] = _utcnow()
        streak["held_reason"] = "code"
        streak["held_failure_class"] = failure_class
        logger.warning(
            f"[DIRECTION HEALTH] Direction {direction_id} HELD after "
            f"{streak['consecutive_failures']} consecutive {failure_class} failures."
        )


def clear_direction_hold(direction_id: str) -> None:
    """Explicitly clear a direction hold (e.g. after research_context update)."""
    streak = direction_failure_streaks.get(direction_id)
    if streak:
        streak["consecutive_failures"] = 0
        streak["consecutive_infra_failures"] = 0
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""


def is_direction_held(direction_id: str) -> tuple[bool, str]:
    """Return (held, reason) for the direction.

    Checks timeout expiry: if the hold has been active for more than
    DIRECTION_HOLD_TIMEOUT_HOURS the hold is auto-cleared and (False, '') is returned.
    Returns (False, '') for unknown directions.
    """
    if not direction_id:
        return False, ""
    streak = direction_failure_streaks.get(direction_id)
    if not streak or not streak.get("held"):
        return False, ""
    # Auto-clear after timeout
    held_at = streak.get("held_at")
    if held_at and (_utcnow() - held_at).total_seconds() > DIRECTION_HOLD_TIMEOUT_HOURS * 3600:
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""
        logger.info(f"[DIRECTION HEALTH] Direction {direction_id} hold expired (timeout).")
        return False, ""
    if streak.get("held_reason") == "infra":
        count = streak.get("consecutive_infra_failures", 0)
        fc = streak.get("held_failure_class") or streak["last_failure_class"] or "INFRA"
        return True, (
            f"Held: direction has {count} consecutive infra failures "
            f"(last: {fc}) — investigate scope/scale before retrying"
        )
    count = streak["consecutive_failures"]
    fc = streak.get("held_failure_class") or streak["last_failure_class"] or "CODE_ERROR"
    return True, f"Held: direction has {count} consecutive {fc} failures — awaiting investigation"


def get_held_directions() -> Dict[str, Dict]:
    """Return snapshot of all currently held directions (after applying timeout logic)."""
    result = {}
    for did, streak in direction_failure_streaks.items():
        held, reason = is_direction_held(did)
        if held:
            result[did] = {
                "consecutive_failures": streak["consecutive_failures"],
                "consecutive_infra_failures": streak.get("consecutive_infra_failures", 0),
                "last_failure_class": streak["last_failure_class"],
                "last_failure_at": streak["last_failure_at"].isoformat() if streak.get("last_failure_at") else None,
                "held_at": streak["held_at"].isoformat() if streak.get("held_at") else None,
                "held_reason": streak.get("held_reason", "code"),
                "project_id": streak.get("project_id", ""),
                "reason": reason,
            }
    return result


def get_task_type_failure_rate(task_type: str, window_minutes: int = 15) -> tuple[float, int]:
    """Compute failure rate for a specific task_type within the trailing window.

    Backward-compatible: entries without a task_type field (old format or empty
    string) are treated as matching any type filter and included in the sample.

    Returns (rate, sample_size).  Returns (0.0, 0) when no samples exist.
    """
    cutoff = (_utcnow() - timedelta(minutes=window_minutes)).isoformat()
    relevant = [
        r for r in deploy_monitoring["task_results"]
        if r[0] >= cutoff and (len(r) < 4 or not r[3] or r[3] == task_type)
    ]
    if not relevant:
        return 0.0, 0
    failures = sum(1 for r in relevant if r[1] == "failure")
    return failures / len(relevant), len(relevant)


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
    "consecutive_failures": {},     # dep_name -> int count of consecutive failed health checks
}

# --- GitHub API Quota Tracking ---
# Populated on every _github_request by reading X-RateLimit-Remaining /
# X-RateLimit-Reset headers off the response. Surfaced in /api/health/detailed
# and drives the dashboard quota widget + WARNING alert when remaining < 100.
# Treat None values as "not yet observed — no recent GitHub call".
github_quota: Dict = {
    "remaining": None,              # int, lowest remaining seen since last reset
    "limit": None,                  # int, 5000 for GitHub App installs
    "reset_at": None,               # int unix ts, when quota resets
    "last_updated": None,           # isoformat str, last observation
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
github_call_counts_window_reset_at: int | None = None

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

# Track last successful GitHub API operation (tokens, PR comments, etc.)
# Used by health check to passively verify GitHub is reachable without
# making an active API call that competes for the shared connection pool.
# Seed with current time so the first health check uses the passive path
# instead of an active probe that can fail on cold start.
last_github_success: Optional[datetime] = datetime.utcnow()  # Module-level init, _utcnow not available here
