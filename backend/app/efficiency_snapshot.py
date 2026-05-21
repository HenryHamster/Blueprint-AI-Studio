"""Weekly efficiency-baseline snapshot persistence.

Stores per-project efficiency axes (cost per merged PR, VM cold-start p50,
VM reuse rate, median API calls per task, median review iterations) inside
``project.design_doc["efficiency_baseline"]``.  Mirrors the storage pattern
used by ``viewport_coverage_snapshot`` / ``sr_catch_rate_trends`` so we get
the same round-trip cost (one JSONB column) and the same operator semantics.

The first snapshot doubles as the baseline (the 2026-04-05 reference point
predates this telemetry and is not recoverable). Subsequent snapshots append
to ``snapshots`` only — the baseline is immutable until an operator clears
``project.design_doc["efficiency_baseline"]`` manually.

Drift is reported as percentage delta from baseline so operators can read
each axis as ``current_value`` / ``baseline_value`` / ``drift_pct``.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, Task, TaskStatus, _utcnow
from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared key constants (writer + reader must agree on these).
# A prior incident (fingerprint 691593a5c0a7 in admin_digest.py) was caused
# by snake_case vs human-category vocabulary mismatch — keep the strings
# centralised so the same constant flows through compute -> write -> read.
# ---------------------------------------------------------------------------
AXIS_COST_PER_MERGED_PR = "cost_per_merged_pr"
AXIS_VM_COLD_START_P50_MS = "vm_cold_start_p50_ms"
AXIS_VM_REUSE_RATE = "vm_reuse_rate"
AXIS_MEDIAN_API_CALLS_PER_TASK = "median_api_calls_per_task"
AXIS_MEDIAN_REVIEW_ITERATIONS = "median_review_iterations"

ALL_AXES: tuple[str, ...] = (
    AXIS_COST_PER_MERGED_PR,
    AXIS_VM_COLD_START_P50_MS,
    AXIS_VM_REUSE_RATE,
    AXIS_MEDIAN_API_CALLS_PER_TASK,
    AXIS_MEDIAN_REVIEW_ITERATIONS,
)

DESIGN_DOC_KEY = "efficiency_baseline"

# Window over which "current" axes are computed for each weekly snapshot.
_AXIS_WINDOW_DAYS = 7

# Cap snapshot history length (~6 months of weekly snapshots).
_SNAPSHOT_HISTORY_MAX = 26


async def _load_recent_tasks(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    window_days: int = _AXIS_WINDOW_DAYS,
) -> list[Task]:
    """Load tasks for the project that completed/failed within the window."""
    cutoff = _utcnow() - timedelta(days=window_days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
            Task.updated_at >= cutoff,
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.context,
                Task.vm_spot,
                Task.vm_provisioned_at,
                Task.vm_stopped_at,
                Task.vm_cold_start_ms,
                Task.vm_warm_start,
                Task.api_call_summary,
            )
        )
        .limit(5000)
    )
    return list((await session.execute(stmt)).scalars().all())


def _compute_cost_per_merged_pr(tasks: list[Task]) -> Optional[float]:
    """Total VM cost / merged PR count over the window. None when no merges."""
    total_cost = 0.0
    merged = 0
    for t in tasks:
        if t.vm_provisioned_at and t.vm_stopped_at:
            rate = SPOT_RATE if t.vm_spot else ON_DEMAND_RATE
            total_cost += (t.vm_stopped_at - t.vm_provisioned_at).total_seconds() * rate
        if t.status == TaskStatus.COMPLETED and (t.context or {}).get("pr_merged"):
            merged += 1
    if not merged:
        return None
    return round(total_cost / merged, 4)


def _compute_vm_cold_start_p50_ms(tasks: list[Task]) -> Optional[float]:
    samples = [int(t.vm_cold_start_ms) for t in tasks if t.vm_cold_start_ms is not None]
    if not samples:
        return None
    return round(float(statistics.median(samples)), 2)


def _compute_vm_reuse_rate(tasks: list[Task]) -> Optional[float]:
    """warm_count / (warm_count + cold_count). None when no VMs were observed."""
    warm = 0
    cold = 0
    for t in tasks:
        if t.vm_warm_start is True:
            warm += 1
        elif t.vm_warm_start is False and t.vm_provisioned_at is not None:
            cold += 1
    total = warm + cold
    if not total:
        return None
    return round(warm / total, 4)


def _compute_median_api_calls_per_task(tasks: list[Task]) -> Optional[float]:
    counts: list[int] = []
    for t in tasks:
        summary = t.api_call_summary or {}
        c = summary.get("llm_call_count")
        if isinstance(c, (int, float)) and c >= 0:
            counts.append(int(c))
    if not counts:
        return None
    return round(float(statistics.median(counts)), 2)


def _compute_median_review_iterations(tasks: list[Task]) -> Optional[float]:
    iters: list[int] = []
    for t in tasks:
        ctx = t.context or {}
        loop = ctx.get("review_loop")
        if isinstance(loop, dict):
            n = loop.get("iteration")
            if isinstance(n, int) and n >= 0:
                iters.append(n)
    if not iters:
        return None
    return round(float(statistics.median(iters)), 2)


async def compute_current_axes(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Optional[float]]:
    """Compute the five efficiency axes from recent terminal tasks."""
    tasks = await _load_recent_tasks(session, project_id)
    return {
        AXIS_COST_PER_MERGED_PR: _compute_cost_per_merged_pr(tasks),
        AXIS_VM_COLD_START_P50_MS: _compute_vm_cold_start_p50_ms(tasks),
        AXIS_VM_REUSE_RATE: _compute_vm_reuse_rate(tasks),
        AXIS_MEDIAN_API_CALLS_PER_TASK: _compute_median_api_calls_per_task(tasks),
        AXIS_MEDIAN_REVIEW_ITERATIONS: _compute_median_review_iterations(tasks),
    }


async def write_snapshot(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Any]:
    """Compute axes and persist them under ``design_doc["efficiency_baseline"]``.

    First-snapshot semantics: the very first call seeds ``baseline``; later
    calls only append to ``snapshots`` (capped at _SNAPSHOT_HISTORY_MAX).
    Returns the snapshot dict that was written.
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    axes = await compute_current_axes(session, project_id)
    captured_at = _utcnow().isoformat()
    snapshot: Dict[str, Any] = {"captured_at": captured_at, **axes}

    doc = dict(project.design_doc or {})
    container = dict(doc.get(DESIGN_DOC_KEY) or {})

    if not container.get("baseline"):
        container["baseline"] = dict(snapshot)

    snapshots = list(container.get("snapshots") or [])
    snapshots.append(snapshot)
    if len(snapshots) > _SNAPSHOT_HISTORY_MAX:
        snapshots = snapshots[-_SNAPSHOT_HISTORY_MAX:]
    container["snapshots"] = snapshots
    container["last_run_at"] = captured_at

    doc[DESIGN_DOC_KEY] = container
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[EFFICIENCY_SNAPSHOT] project=%s captured_at=%s axes=%s",
        project_id,
        captured_at,
        {k: axes[k] for k in ALL_AXES},
    )
    return snapshot


async def read_baseline_with_drift(
    session: AsyncSession, project_id: uuid.UUID
) -> Dict[str, Any]:
    """Return per-axis ``{current, baseline, drift_pct}`` plus capture metadata.

    ``drift_pct`` is None when either side is missing or baseline is 0.
    ``current`` reflects the latest stored snapshot (NOT a re-compute) so
    operators see the same numbers the snapshot service published. Endpoint
    consumers that want a freshly recomputed view should call
    ``compute_current_axes`` directly.
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    container = (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
    baseline = container.get("baseline") or {}
    snapshots = container.get("snapshots") or []
    latest = snapshots[-1] if snapshots else {}

    axes: Dict[str, Dict[str, Any]] = {}
    for axis in ALL_AXES:
        cur = latest.get(axis)
        base = baseline.get(axis)
        drift_pct: Optional[float]
        if (
            cur is None
            or base is None
            or not isinstance(cur, (int, float))
            or not isinstance(base, (int, float))
            or base == 0
        ):
            drift_pct = None
        else:
            drift_pct = round(((cur - base) / base) * 100.0, 2)
        history = [
            {"captured_at": snap.get("captured_at"), "value": snap.get(axis)}
            for snap in snapshots
        ]
        axes[axis] = {
            "current": cur,
            "baseline": base,
            "drift_pct": drift_pct,
            "history": history,
        }

    return {
        "axes": axes,
        "baseline_captured_at": baseline.get("captured_at"),
        "last_snapshot_at": container.get("last_run_at") or latest.get("captured_at"),
    }


# ---------------------------------------------------------------------------
# Background-service helpers
# ---------------------------------------------------------------------------

# Snapshot cadence: one snapshot per UTC week, anchored to Sunday 00:00 UTC.
# Service ticks every hour and decides whether the cadence has elapsed.
SNAPSHOT_INTERVAL_SECONDS = 3600
_WEEK_SECONDS = 7 * 24 * 3600


def _last_run_due(last_run_at_iso: Optional[str], now: datetime) -> bool:
    """Return True when a new snapshot is due.

    Cadence is one snapshot per UTC week. The first call is always due.
    Subsequent calls are due once at least seven days have passed since the
    previous ``last_run_at`` marker.
    """
    if not last_run_at_iso:
        return True
    try:
        last_run = datetime.fromisoformat(last_run_at_iso)
    except (TypeError, ValueError):
        return True
    if last_run.tzinfo is not None:
        last_run = last_run.replace(tzinfo=None)
    return (now - last_run).total_seconds() >= _WEEK_SECONDS


async def maybe_write_weekly_snapshot(
    session: AsyncSession, project_id: uuid.UUID, *, now: Optional[datetime] = None
) -> Optional[Dict[str, Any]]:
    """Write a snapshot iff the weekly cadence has elapsed for the project.

    Reads ``design_doc["efficiency_baseline"]["last_run_at"]`` for the
    cadence marker. The marker doubles as a multi-instance-deploy guard:
    even if leader election briefly hands off, the next instance sees the
    marker and skips. Returns the snapshot dict on write, None on skip.
    """
    if now is None:
        now = _utcnow()

    project = await session.get(Project, project_id)
    if project is None:
        return None

    container = (project.design_doc or {}).get(DESIGN_DOC_KEY) or {}
    if not _last_run_due(container.get("last_run_at"), now):
        return None
    return await write_snapshot(session, project_id)
