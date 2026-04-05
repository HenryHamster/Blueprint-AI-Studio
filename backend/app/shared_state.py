"""Shared runtime state used by both main.py and admin.py.

Extracted to its own module to avoid circular imports when admin
endpoints need to read or mutate autonomous-loop / runtime-config state.
"""

import os
import asyncio
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from slowapi import Limiter
from slowapi.util import get_remote_address

# Rate limiter — shared between main.py and admin.py
# Only enforced in Cloud Run (K_REVISION set), disabled locally for dev/test
limiter = Limiter(
    key_func=get_remote_address,
    enabled=bool(os.getenv("K_REVISION")),
)

# Lock for multi-key runtime config updates (prevents reading partial state between two assignments)
runtime_config_lock = asyncio.Lock()

# --- Runtime Config (adjustable via admin API) ---
runtime_config: Dict = {
    "max_concurrent_vms": int(os.getenv("MAX_CONCURRENT_VMS", "5")),
    "max_retries": int(os.getenv("MAX_RETRIES", "3")),
    "max_autonomous_tasks_per_hour": 5,
    "max_global_autonomous_tasks_per_hour": 50,  # Cross-project cap (default 10x per-project)
    "autonomous_research_interval_minutes": 30,
    # Per-task-type model/effort defaults (synced from frontend settings)
    # e.g. {"research": "claude-opus-4-6", "create": "claude-sonnet-4-6"}
    "task_type_models": {},
    "task_type_efforts": {},
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
    "min_research_interval_hours": 24,
}

# --- Autonomous Loop State ---
autonomous_failure_counts: Dict[str, int] = {}  # project_id -> consecutive failures
last_idle_check: Optional[datetime] = None

# --- Circuit Breaker (Hystrix-inspired) ---
# Per-project circuit breaker state for autonomous task spawning
# States: CLOSED (normal), OPEN (tripped), HALF_OPEN (testing one task)
circuit_breakers: Dict[str, Dict] = {}  # project_id -> breaker state

def get_circuit_breaker(project_id: str) -> Dict:
    """Get or create circuit breaker state for a project."""
    if project_id not in circuit_breakers:
        circuit_breakers[project_id] = {
            "state": "CLOSED",           # CLOSED, OPEN, HALF_OPEN
            "failure_count": 0,          # consecutive failures
            "success_count": 0,          # consecutive successes (for half-open)
            "opened_at": None,           # when the breaker tripped
            "cooldown_seconds": 1800,    # 30 min initial cooldown
            "base_cooldown": 1800,       # for exponential backoff reset
            "half_open_task_id": None,   # the test task in HALF_OPEN
        }
    return circuit_breakers[project_id]

# --- Background Tasks (populated by lifespan, used by admin restart endpoint) ---
import asyncio
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
}

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
