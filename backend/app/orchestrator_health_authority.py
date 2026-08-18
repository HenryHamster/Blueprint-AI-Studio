"""Cross-process orchestrator health authority for split API/worker roles."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text


logger = logging.getLogger(__name__)


def project_background_leader_heartbeat(
    heartbeat_age_seconds: float | None,
    *,
    stale_after_seconds: float,
    leader_pid: int | None = None,
) -> dict[str, Any]:
    """Project the advisory-lock holder heartbeat into operator health truth."""
    common = {
        "authority": "background_leader_advisory_lock",
        "cycle_age_seconds": None,
        "cycle_age_kind": "not_observable_cross_process",
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "progress_age_seconds": heartbeat_age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "leader_pid": leader_pid,
    }
    if heartbeat_age_seconds is None:
        return {
            **common,
            "status": "down",
            "reason": "no process holds the background-leader advisory lock",
        }
    age = max(0.0, float(heartbeat_age_seconds))
    return {
        **common,
        "heartbeat_age_seconds": age,
        "progress_age_seconds": age,
        "status": "ok" if age < stale_after_seconds else "down",
        "reason": (
            "background leader keepalive is fresh"
            if age < stale_after_seconds
            else "background leader keepalive is stale"
        ),
    }


async def observe_background_leader_health(
    session,
    *,
    lock_classid: int,
    lock_objid: int,
    stale_after_seconds: float,
) -> dict[str, Any]:
    """Read the exact session-lock holder shared by the worker processes."""
    try:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT a.pid, EXTRACT(EPOCH FROM "
                        "(clock_timestamp() - a.state_change)) AS heartbeat_age "
                        "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                        "WHERE l.locktype = 'advisory' AND l.objsubid = 1 "
                        "AND l.classid::bigint = :classid "
                        "AND l.objid::bigint = :objid AND l.granted "
                        "ORDER BY a.state_change DESC LIMIT 1"
                    ),
                    {"classid": lock_classid, "objid": lock_objid},
                )
            )
            .mappings()
            .first()
        )
    except Exception as exc:  # health projection must not make the route fail
        logger.warning("Could not observe background-leader health: %s", exc)
        return {
            "status": "degraded",
            "authority": "background_leader_advisory_lock",
            "cycle_age_seconds": None,
            "cycle_age_kind": "not_observable_cross_process",
            "heartbeat_age_seconds": None,
            "progress_age_seconds": None,
            "stale_after_seconds": stale_after_seconds,
            "leader_pid": None,
            "reason": "background leader authority could not be observed",
        }
    if row is None:
        return project_background_leader_heartbeat(
            None, stale_after_seconds=stale_after_seconds
        )
    return project_background_leader_heartbeat(
        float(row["heartbeat_age"]),
        stale_after_seconds=stale_after_seconds,
        leader_pid=int(row["pid"]),
    )
