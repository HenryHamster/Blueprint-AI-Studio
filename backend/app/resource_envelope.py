"""Helpers for per-project resource envelope resolution and spend aggregation.

The resource envelope is stored inside ``project.build_config["resource_envelope"]``
as a JSONB sub-object.  Resolvers guard against legacy/hand-edited values (missing
keys, wrong types) using the same isinstance pattern as
``autonomous_research.project_parallelism_cap``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from app.models import Project


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_envelope(project: Project) -> dict:
    """Return the raw resource_envelope dict from build_config, or {}."""
    bc = getattr(project, "build_config", None) or {}
    raw = bc.get("resource_envelope")
    if isinstance(raw, dict):
        return raw
    return {}


def resolve_project_vm_cap(project: Project) -> int | None:
    """Return the per-project concurrent-VM cap, or None if unlimited.

    Reads ``project.build_config["resource_envelope"]["max_concurrent_vms"]``.
    Returns None when absent, null, or not a valid positive integer — callers
    should then fall back to the global runtime setting.
    """
    raw = _get_envelope(project).get("max_concurrent_vms")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return None


def resolve_project_spend_ceiling(project: Project) -> float | None:
    """Return the per-project spend ceiling in USD, or None if unlimited.

    Reads ``project.build_config["resource_envelope"]["spend_ceiling_usd"]``.
    Returns None when absent, null, or not a valid positive number — callers
    should then treat the project as uncapped.
    """
    raw = _get_envelope(project).get("spend_ceiling_usd")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def resolve_project_spend_window_days(project: Project) -> int:
    """Return the spend aggregation window in days.

    Reads ``project.build_config["resource_envelope"]["spend_window_days"]``.
    Returns the schema default (30) when absent, null, or not a valid positive
    integer so hand-edited legacy config cannot crash scheduler hot paths.
    """
    raw = _get_envelope(project).get("spend_window_days")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return 30


async def get_project_recent_spend_usd(
    session,
    *,
    project_id: uuid.UUID,
    window_days: int = 30,
) -> float:
    """Return total project spend (USD) over a rolling window.

    Combines three spend sources:

    1. **Settled compute-job spend** — ``final_cost_usd`` over terminal
       ``ComputeJob`` rows whose ``completed_at`` falls within
       ``[now - window_days, now)`` for the project.

    2. **Agent-VM task cost** — sum of ``get_task_total_vm_cost_usd`` over
       all tasks belonging to the project whose ``updated_at`` falls within
       the same window.  ``updated_at`` is used as the inclusion criterion
       because it reflects the last active/settled moment for both in-flight
       and terminal tasks; tasks that were provisioned before but still running
       (or finished) during the window are captured.

    3. **Warm-pool idle cost** — attributed idle cost for this project's pool
       VMs over the same rolling window, via
       ``efficiency_snapshot._compute_windowed_idle_cost``.  Includes both
       settled ``PoolIdleSpan`` rows (``settled_at >= since``) and the
       in-progress idle portion of any currently-IDLE pool VMs.

    All three components use the same ``[now - window_days, now)`` window so
    the total is consistent across dispatch-gate, spawn-gate, and operator
    display callers.
    """
    from app.compute_jobs import get_recent_terminal_compute_spend_usd
    from app.efficiency_snapshot import _compute_windowed_idle_cost
    from app.models import Task, get_task_total_vm_cost_usd

    now = _utcnow()
    since = now - timedelta(days=window_days)

    compute_spend = await get_recent_terminal_compute_spend_usd(
        session,
        project_id=project_id,
        since=since,
        until=None,
    )

    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.updated_at >= since,
        )
    )
    tasks = result.scalars().all()
    task_vm_spend = sum(get_task_total_vm_cost_usd(t) for t in tasks)

    idle_cost_result = await _compute_windowed_idle_cost(session, project_id, since)
    idle_spend = idle_cost_result["total"]

    return float(compute_spend) + float(task_vm_spend) + float(idle_spend)
