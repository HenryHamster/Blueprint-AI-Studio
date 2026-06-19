"""Self-healing state persistence — snapshots in-memory shared_state to DeploymentState.

Contains the six async _persist_* functions and three pure _serialize_*_snapshot helpers
that were extracted from orchestrator.py. These functions only touch app.shared_state,
AsyncSessionLocal, and app.deploy_health; they do not depend on the orchestrator worker
loop or its cycle-timing globals.
"""

import logging
from collections import deque
from datetime import datetime


from app.database import AsyncSessionLocal
import app.shared_state as _shared_state

logger = logging.getLogger("blueprint")


async def _persist_circuit_breakers():
    """Save circuit breaker state to DB so it survives restarts."""
    from app.deploy_health import _get_deploy_state_for_update

    try:
        serializable = {}
        for k, v in _shared_state.circuit_breakers.items():
            # Prune: don't persist CLOSED breakers with no failures (default state)
            if (
                v.get("state") == "CLOSED"
                and v.get("failure_count", 0) == 0
                and v.get("infra_failure_count", 0) == 0
            ):
                continue
            serializable[k] = {
                **v,
                "opened_at": v["opened_at"].isoformat() if v.get("opened_at") else None,
            }
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.circuit_breaker_states = serializable
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[CIRCUIT BREAKER] Failed to persist state: {e}", exc_info=True)


async def _persist_zone_health():
    """Snapshot zone health to DB so cooldowns survive restarts."""
    from app.deploy_health import (
        _get_deploy_state_for_update,
        _pause_failure_streak,
        _health_recovery_history,
        _last_auto_resume_at,
        _recent_auto_resumes,
        _last_resume_was_forced,
        _consecutive_force_resumes,
    )

    try:
        from app.compute import _zone_health, _zone_lock

        with _zone_lock:
            snapshot = {}
            for zone, data in _zone_health.items():
                snapshot[zone] = {
                    k: (
                        v.isoformat()
                        if isinstance(v, datetime)
                        else list(v)
                        if isinstance(v, deque)
                        else v
                    )
                    for k, v in data.items()
                }
        snapshot["_pause_failure_streak"] = _pause_failure_streak
        snapshot["_health_recovery_history"] = list(_health_recovery_history)
        snapshot["_last_auto_resume_at"] = (
            _last_auto_resume_at.isoformat() if _last_auto_resume_at else None
        )
        snapshot["_recent_auto_resumes"] = [t.isoformat() for t in _recent_auto_resumes]
        snapshot["_last_resume_was_forced"] = bool(_last_resume_was_forced)
        snapshot["_consecutive_force_resumes"] = int(_consecutive_force_resumes)
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.zone_health_state = snapshot
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(f"[ZONE HEALTH] Failed to persist state: {e}", exc_info=True)


def _serialize_anomaly_snapshot() -> dict:
    """Build the anomaly-state snapshot from in-memory shared_state."""
    serializable_anomalies: dict = {}
    for proj_id, proj_anomalies in _shared_state.persistent_anomalies.items():
        serializable_anomalies[proj_id] = {}
        for k, v in proj_anomalies.items():
            if not isinstance(v, dict):
                serializable_anomalies[proj_id][k] = (
                    v.isoformat() if isinstance(v, datetime) else v
                )
                continue
            serializable_anomalies[proj_id][k] = {
                fk: fv.isoformat() if isinstance(fv, datetime) else fv
                for fk, fv in v.items()
            }
    return {
        "persistent_anomalies": serializable_anomalies,
        "anomaly_adjustments": dict(_shared_state.anomaly_adjustments),
    }


def _serialize_direction_streaks_snapshot() -> dict:
    """Build the direction-streak snapshot from in-memory shared_state."""
    return {
        direction_id: {
            k: v.isoformat() if isinstance(v, datetime) else v
            for k, v in streak.items()
        }
        for direction_id, streak in _shared_state.direction_failure_streaks.items()
    }


def _serialize_pipeline_events_snapshot() -> dict:
    """Build the pipeline-events snapshot from in-memory shared_state."""
    events_list = [
        {k: v.isoformat() if isinstance(v, datetime) else v for k, v in e.items()}
        for e in _shared_state.deploy_monitoring["pipeline_events"]
    ]
    return {"events": events_list}


async def _persist_self_healing_state_bundle():
    """Coalesced 60s snapshot of anomaly + direction-streak + pipeline-events state.

    All three live on the same ``DeploymentState`` row, so we open one session,
    take the row lock once via ``_get_deploy_state_for_update``, mutate the
    three columns, and commit once. Replaces three separate session/commit
    cycles per orchestrator tick (M-P6).
    """
    from app.deploy_health import _get_deploy_state_for_update

    try:
        anomaly_snapshot = _serialize_anomaly_snapshot()
        direction_snapshot = _serialize_direction_streaks_snapshot()
        events_snapshot = _serialize_pipeline_events_snapshot()
        async with AsyncSessionLocal() as session:
            state = await _get_deploy_state_for_update(session)
            if state:
                state.persistent_anomaly_state = anomaly_snapshot
                state.direction_streak_state = direction_snapshot
                state.pipeline_events_state = events_snapshot
                session.add(state)
                await session.commit()
    except Exception as e:
        logger.warning(
            f"[SELF-HEALING STATE] Failed to persist bundle: {e}", exc_info=True
        )
