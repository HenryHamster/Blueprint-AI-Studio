"""Autonomous-loop / anomaly / circuit-breaker process state (split out of shared_state).

Owns the in-memory anomaly fingerprint cache (+ its DB persistence helpers), the
per-project circuit-breaker state, and the autonomous failure-count map. Re-exported
from :mod:`app.shared_state` as a thin facade. Behaviour-preserving.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# --- Autonomous Loop State ---
autonomous_failure_counts: Dict[str, int] = {}  # project_id -> consecutive failures

# NOTE: ``last_idle_check`` and ``last_anomaly_detection_run_at`` are *rebindable*
# datetime scalars written/read through the facade
# (``shared_state.last_idle_check = ...``), so they live on the
# :mod:`app.shared_state` facade itself, not here — re-exporting a scalar would
# diverge the moment the facade attribute is rebound.

# --- Persistent Anomaly Tracking ---
# project_id -> { anomaly_key -> {first_seen, last_seen, occurrence_count, severity, latest_details} }
persistent_anomalies: Dict[str, Dict[str, Dict]] = {}

# Key used inside Project.design_doc for anomaly fingerprint persistence.
_ANOMALY_FINGERPRINTS_KEY = "_anomaly_fingerprints"


async def hydrate_persistent_anomalies(session) -> int:
    """Load anomaly fingerprint state from Project.design_doc into the in-memory cache.

    Call once at startup so persistent_anomalies survives pod restarts.
    In-memory entries always win over stored ones (no clobber on concurrent startup).
    Returns the total number of anomaly entries loaded across all projects.
    """
    from app.models import Project
    from sqlmodel import select
    from sqlalchemy.orm import load_only

    loaded = 0
    try:
        projects = (
            (
                await session.execute(
                    select(Project).options(load_only(Project.id, Project.design_doc, raiseload=True))
                )
            )
            .scalars()
            .all()
        )
        for project in projects:
            stored = (project.design_doc or {}).get(_ANOMALY_FINGERPRINTS_KEY)
            if not isinstance(stored, dict) or not stored:
                continue
            proj_key = str(project.id)
            target = persistent_anomalies.setdefault(proj_key, {})
            for key, entry in stored.items():
                if key not in target:
                    target[key] = entry
            loaded += len(stored)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ANOMALY_PERSIST] hydrate_persistent_anomalies failed", exc_info=True
        )
    return loaded


async def with_design_doc_locked(
    session,
    project_id: str,
    mutate_fn: Any,
) -> None:
    """Read-modify-write project.design_doc under a project row lock.

    Thin wrapper over the canonical ``task_locking.with_design_doc_locked``
    that additionally coerces ``project_id`` (which background callers pass
    as a str) to ``UUID`` and silently no-ops on malformed input. The
    lock/mutate/flag_modified logic lives in exactly one place — the
    canonical helper — so the two never drift apart.
    """
    from app.task_locking import with_design_doc_locked as _canonical
    import uuid as _uuid

    try:
        pid = _uuid.UUID(str(project_id))
    except (TypeError, ValueError):
        return
    await _canonical(session, pid, mutate_fn)


async def persist_anomaly_snapshot(
    project_id: str, proj_anomalies: dict, session
) -> None:
    """Write anomaly fingerprints for one project into Project.design_doc.

    Called after each anomaly detection cycle so the state survives the next
    pod restart. Safe to call within an already-open session (caller commits).
    """

    def _mutate(doc: dict) -> None:
        doc[_ANOMALY_FINGERPRINTS_KEY] = {
            k: dict(v) if isinstance(v, dict) else v for k, v in proj_anomalies.items()
        }

    await with_design_doc_locked(session, project_id, _mutate)


# --- Anomaly-Driven Pipeline Adjustments ---
# When high-severity anomalies (PROVISION_FAILURE, EXTERNAL_API) persist,
# the system automatically reduces blast radius by lowering max_concurrent_vms
# and tightening the failure-rate dispatch gate.
anomaly_adjustments: Dict[str, Any] = {
    "active": False,
    "adjusted_at": None,  # ISO timestamp when adjustments were applied
    "trigger_anomalies": [],  # anomaly keys that triggered the adjustment
    "pre_adjustment_max_vms": None,  # stored to restore on clearing
    "pre_adjustment_failure_gate": None,
    "failure_gate_threshold": 0.8,  # default; lowered to 0.6 during anomaly response
    "forced_restoration_count": 0,  # cumulative count of max-duration forced restorations
    "forced_restored_at": None,  # ISO timestamp of last forced restoration
    "adjusted_max_vms": None,  # target max_concurrent_vms we set; used to detect deploy_health/admin overrides
}

# --- Circuit Breaker (Hystrix-inspired) ---
# Per-project circuit breaker state for autonomous task spawning
# States: CLOSED (normal), OPEN (tripped), HALF_OPEN (testing one task)
circuit_breakers: Dict[str, Dict] = {}  # project_id -> breaker state


def reset_project_state(project_id: str) -> None:
    """Drop all transient autonomous-loop state for one project.

    Called by the reset-to-clean operation: pops the project's entry from the
    circuit-breaker map, the autonomous failure-count map, and the persistent
    anomaly cache so a wedged project starts its next cycle from a clean slate.
    Other projects' state is untouched. Idempotent — missing keys are ignored.
    """
    key = str(project_id)
    circuit_breakers.pop(key, None)
    autonomous_failure_counts.pop(key, None)
    persistent_anomalies.pop(key, None)


def get_circuit_breaker(project_id: str) -> Dict:
    """Get or create circuit breaker state for a project."""
    if project_id not in circuit_breakers:
        circuit_breakers[project_id] = {
            "state": "CLOSED",  # CLOSED, OPEN, HALF_OPEN
            "failure_count": 0,  # consecutive code-quality failures (CODE_ERROR, AGENT_TIMEOUT)
            "infra_failure_count": 0,  # consecutive infra failures (PROVISION_FAILURE, RATE_LIMIT, EXTERNAL_API)
            "success_count": 0,  # consecutive successes (for half-open)
            "opened_at": None,  # when the breaker tripped
            "cooldown_seconds": 1800,  # 30 min initial cooldown
            "base_cooldown": 1800,  # for exponential backoff reset
            "half_open_task_id": None,  # the test task in HALF_OPEN
            "open_reason": None,  # "code" or "infra" — why the breaker opened
        }
    return circuit_breakers[project_id]
