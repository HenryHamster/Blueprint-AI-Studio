"""Residual admin analytics endpoints (direction auto-pause, task duration, failing task types, open PRs)."""

import time
import uuid
import logging
from collections import defaultdict
from typing import Dict, Any, List, Tuple
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text, or_
from sqlalchemy.orm import load_only
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session, resolve_dialect_name
from app.models import (
    Task,
    TaskStatus,
    Project,
    Artifact,
    ArtifactType,
    ProjectMessage,
    _utcnow,
    get_task_total_vm_cost_usd_or_none,
)
from app.auth import get_current_user, require_admin
from app.vm_cost_rates import get_vm_rate_per_second, VM_COST_RATES
from app.shared_state import (
    get_project_integration_branch,
    get_project_promotion_target,
)

# Re-export for test and internal use (single source of truth is vm_cost_rates)
ON_DEMAND_RATE = VM_COST_RATES["e2-standard-2"][0] / 3600  # $/sec
SPOT_RATE = VM_COST_RATES["e2-standard-2"][1] / 3600  # $/sec

logger = logging.getLogger(__name__)


def _derive_completed_at(t: "Task") -> datetime:
    """Return the completion timestamp from the last status_log entry, or task.created_at.

    Always returns a naive UTC datetime — `task.created_at` may be TIMESTAMPTZ
    (migration 0061) and the JSON `t` strings are written without tz, so callers
    of this function must not mix the two without normalization.

    If the caller projected `status_log[-1]->>'t'` SQL-side and stashed it on
    `t._projected_completed_at_ts` (see admin_digest.py), use that instead of
    materializing the heavy `status_log` JSONB column.
    """

    def _strip_tz(d: datetime) -> datetime:
        return d.replace(tzinfo=None) if d.tzinfo is not None else d

    if hasattr(t, "_projected_completed_at_ts"):
        cached_ts = t._projected_completed_at_ts
        if cached_ts:
            try:
                return _strip_tz(datetime.fromisoformat(cached_ts))
            except (TypeError, ValueError):
                pass
        return _strip_tz(t.created_at)
    if t.status_log:
        try:
            return _strip_tz(datetime.fromisoformat(t.status_log[-1]["t"]))
        except (KeyError, ValueError):
            pass
    return _strip_tz(t.created_at)


_TASK_COST_WINDOW_MS: Dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "3h": 3 * 60 * 60 * 1000,
    "6h": 6 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "all": 0,
}

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-analytics"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------------------
# GET /api/admin/direction-auto-pause-stats
# ---------------------------------------------------------------------------


def _compute_direction_auto_pause_stats(
    projects: List[Project],
    milestone_events: List[ProjectMessage],
    now: datetime,
    window_hours: int,
) -> Dict[str, Any]:
    """Aggregate direction auto-pause/recovery metrics over a window.

    Uses event timestamps from ProjectMessage(event_type=direction_milestone)
    for windowed counts and live Project.directions for currently-paused state.
    """
    window_start = now - timedelta(hours=window_hours)
    prev_window_start = window_start - timedelta(hours=window_hours)

    cur_paused = 0
    cur_retired = 0
    prev_paused = 0
    reason_counts: Dict[str, int] = defaultdict(int)

    for msg in milestone_events:
        ctx = msg.context or {}
        if ctx.get("event_type") != "direction_milestone":
            continue
        milestone = ctx.get("milestone")
        ts = msg.created_at
        if ts is None:
            continue
        # ProjectMessage.created_at is TIMESTAMPTZ (migration 0061); window_start
        # is naive. Strip tz for comparison rather than assume both sides match.
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        if ts >= window_start:
            if milestone == "auto-paused":
                cur_paused += 1
                reason = (ctx.get("reason") or "").strip()
                if reason:
                    reason_counts[reason[:120]] += 1
            elif milestone == "auto-retired":
                cur_retired += 1
        elif ts >= prev_window_start:
            if milestone == "auto-paused":
                prev_paused += 1

    # Recovery proxy: paused directions in window that were not auto-retired in window.
    # Negative values are clamped — retirements can reference directions paused earlier.
    recovered = max(cur_paused - cur_retired, 0)

    # Currently auto-paused (live state — not window-bounded) so operators know active load.
    currently_auto_paused = 0
    for proj in projects:
        for d in proj.directions or []:
            if d.get("status") == "paused" and d.get("auto_paused"):
                currently_auto_paused += 1

    top_reasons = [
        {"reason": r, "count": c}
        for r, c in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    ]

    if cur_paused > prev_paused + 1:
        trend = "up"
    elif cur_paused + 1 < prev_paused:
        trend = "down"
    else:
        trend = "flat"

    return {
        "window_hours": window_hours,
        "auto_paused_count": cur_paused,
        "auto_recovered_count": recovered,
        "auto_retired_count": cur_retired,
        "currently_auto_paused": currently_auto_paused,
        "top_reasons": top_reasons,
        "trend": trend,
        "prev_window_auto_paused_count": prev_paused,
    }


@router.get("/direction-auto-pause-stats")
async def get_direction_auto_pause_stats(
    project_id: uuid.UUID | None = Query(None),
    window_hours: int = Query(24, ge=1, le=720),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Aggregate direction auto-pause/recovery stats over a rolling window.

    Powers a dashboard widget that makes self-healing efficacy visible: how many
    directions were auto-paused, how many recovered vs. were auto-retired, the
    most common pause reasons, and a coarse trend vs. the previous window.
    """
    now = _utcnow()
    earliest = now - timedelta(hours=window_hours * 2)

    # Load live directions for the "currently paused" count
    # Scan only direction_milestone rows: high-volume routine system messages
    # would otherwise dominate the LIMIT window and drop the rare milestone rows
    # the aggregation actually counts (idiomatic JSONB filter, cf. directions.py).
    msg_filters = [
        ProjectMessage.from_type == "system",
        ProjectMessage.created_at >= earliest,
        ProjectMessage.context["event_type"].as_string() == "direction_milestone",
    ]
    if project_id:
        proj = await session.get(Project, project_id)
        projects = [proj] if proj else []
        msg_filters.append(ProjectMessage.project_id == project_id)
    else:
        projects = (
            (
                await session.execute(
                    select(Project)
                    .options(load_only(Project.id, Project.directions, Project.status))
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )

    events = (
        (
            await session.execute(
                select(ProjectMessage)
                .where(*msg_filters)
                .options(load_only(ProjectMessage.context, ProjectMessage.created_at))
                .order_by(ProjectMessage.created_at.desc())
                .limit(10_000)
            )
        )
        .scalars()
        .all()
    )
    return _compute_direction_auto_pause_stats(projects, events, now, window_hours)


def _task_completed_at(task: Task) -> datetime:
    """Return the task completion timestamp used for cost windowing."""
    return _derive_completed_at(task)


def _task_estimated_cost_usd(task: Task) -> float | None:
    """Return the VM cost for a task, or None when timing data is incomplete."""
    if not task.vm_provisioned_at or not task.vm_stopped_at:
        return None
    rate = get_vm_rate_per_second(machine_type=task.vm_machine_type, spot=task.vm_spot)
    return (task.vm_stopped_at - task.vm_provisioned_at).total_seconds() * rate


@router.get("/task-cost-breakdown")
async def get_task_cost_breakdown(
    project_id: uuid.UUID | None = Query(None),
    window: str = Query("1h"),
    top_n: int = Query(5, ge=1, le=20),
    max_tasks: int = Query(5000, ge=1, le=20000),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return spend breakdowns by task type, direction, and most expensive tasks."""
    if window not in _TASK_COST_WINDOW_MS:
        raise HTTPException(status_code=400, detail=f"Unsupported window '{window}'")

    cutoff = None
    if window != "all":
        cutoff = _utcnow() - timedelta(milliseconds=_TASK_COST_WINDOW_MS[window])

    filters = [
        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
    ]
    if project_id is not None:
        filters.append(Task.project_id == project_id)

    tasks = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.project_id,
                        Task.title,
                        Task.status,
                        Task.created_at,
                        Task.status_log,
                        Task.payloads,
                        Task.context,
                        Task.vm_provisioned_at,
                        Task.vm_stopped_at,
                        Task.vm_spot,
                        Task.vm_runtime_cost_banked_db,
                    )
                )
                .where(*filters)
                # Windowing is by completion time (derived in Python from
                # status_log/context), so we cannot push the cutoff into SQL.
                # Bound the materialized set instead: most-recently-created
                # tasks first, capped at max_tasks, so a large unarchived
                # backlog cannot OOM the worker. completed_at >= created_at,
                # so ordering by created_at desc keeps the recent window's
                # tasks at the front of the result set.
                .order_by(Task.created_at.desc())
                .limit(max_tasks)
            )
        )
        .scalars()
        .all()
    )

    rows: List[Dict[str, Any]] = []
    for task in tasks:
        completed_at = _task_completed_at(task)
        if cutoff is not None and completed_at < cutoff:
            continue
        cost = get_task_total_vm_cost_usd_or_none(task)
        if cost is None:
            continue
        payload = task.first_payload or {}
        task_type = str(payload.get("type") or "create")
        ctx = task.context or {}
        direction_id = ctx.get("direction_id") or payload.get("current_direction_id")
        direction_id_str = str(direction_id).strip() if direction_id else None
        rows.append(
            {
                "task_id": str(task.id),
                "task_title": task.title,
                "task_type": task_type,
                "direction_id": direction_id_str,
                "completed_at": completed_at,
                "estimated_cost_usd": cost,
            }
        )

    by_task_type: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"task_count": 0, "estimated_cost_usd": 0.0}
    )
    by_direction: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"task_count": 0, "estimated_cost_usd": 0.0}
    )
    total_cost = 0.0

    for row in rows:
        total_cost += row["estimated_cost_usd"]
        type_entry = by_task_type[row["task_type"]]
        type_entry["task_count"] += 1
        type_entry["estimated_cost_usd"] += row["estimated_cost_usd"]

        direction_key = row["direction_id"] or "unassigned"
        direction_entry = by_direction[direction_key]
        direction_entry["task_count"] += 1
        direction_entry["estimated_cost_usd"] += row["estimated_cost_usd"]

    task_type_breakdown = [
        {
            "task_type": task_type,
            "task_count": data["task_count"],
            "estimated_cost_usd": round(data["estimated_cost_usd"], 2),
        }
        for task_type, data in sorted(
            by_task_type.items(),
            key=lambda item: (-item[1]["estimated_cost_usd"], item[0]),
        )
    ]
    direction_breakdown = [
        {
            "direction_id": None if direction_id == "unassigned" else direction_id,
            "task_count": data["task_count"],
            "estimated_cost_usd": round(data["estimated_cost_usd"], 2),
        }
        for direction_id, data in sorted(
            by_direction.items(),
            key=lambda item: (-item[1]["estimated_cost_usd"], item[0]),
        )
    ]
    top_tasks = [
        {
            "task_id": row["task_id"],
            "task_title": row["task_title"],
            "task_type": row["task_type"],
            "direction_id": row["direction_id"],
            "completed_at": row["completed_at"].isoformat(),
            "estimated_cost_usd": round(row["estimated_cost_usd"], 2),
        }
        for row in sorted(
            rows,
            key=lambda row: (
                -row["estimated_cost_usd"],
                row["completed_at"],
                row["task_title"],
            ),
        )[:top_n]
    ]

    return {
        "project_id": str(project_id) if project_id else None,
        "window": window,
        "window_ms": _TASK_COST_WINDOW_MS[window] if window != "all" else None,
        "window_start": cutoff.isoformat() if cutoff else None,
        "task_count": len(rows),
        "max_tasks": max_tasks,
        "truncated": len(tasks) >= max_tasks,
        "total_estimated_cost_usd": round(total_cost, 2),
        "task_type_breakdown": task_type_breakdown,
        "direction_breakdown": direction_breakdown,
        "top_tasks": top_tasks,
    }


@router.get("/task-duration-stats")
async def get_task_duration_stats(
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """p50 per task type + p50/p90 per scope + p50 per phase from COMPLETED tasks in last 7 days.

    On Postgres, aggregates via percentile_cont so row-heavy JSONB columns never
    materialize in app memory (an unarchived backlog otherwise OOMs Cloud Run).
    On SQLite (test harness), falls back to the per-row Python implementation
    since SQLite lacks percentile_cont / WITHIN GROUP / jsonb operators.
    """
    # Completion time = last status_log entry's "t" field (naive UTC ISO, matches
    # _utcnow() in time_utils.py). Regex-gate the cast so a malformed entry
    # degrades to created_at instead of erroring the whole query — mirrors the
    # try/except in the previous Python implementation.
    cutoff = _utcnow() - timedelta(days=7)
    dialect_name = resolve_dialect_name(session)
    if dialect_name != "postgresql":
        # Test harness path: compute in Python (test dataset sizes are tiny).
        filters = [
            Task.status == TaskStatus.COMPLETED,
            Task.started_at.is_not(None),
            Task.created_at >= cutoff,
        ]
        if project_id:
            filters.append(Task.project_id == project_id)
        completed = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.status_log,
                            Task.created_at,
                            Task.started_at,
                            Task.payloads,
                            Task.context,
                            Task.vm_provisioned_at,
                            Task.vm_started_at,
                            Task.vm_stopped_at,
                        )
                    )
                    .where(*filters)
                    .limit(10000)
                )
            )
            .scalars()
            .all()
        )
        type_durations: dict = defaultdict(list)
        scope_durations: dict = defaultdict(list)
        for t in completed:
            try:
                cat = (
                    datetime.fromisoformat(t.status_log[-1]["t"])
                    if t.status_log
                    else t.created_at
                )
            except Exception:
                cat = t.created_at
            dur = (cat - t.started_at).total_seconds()
            if dur > 0:
                type_durations[(t.first_payload).get("type", "create")].append(dur)
                scope = (t.context or {}).get("scope")
                if scope in ("small", "medium", "large"):
                    scope_durations[scope].append(dur)
        stats: dict = {}
        for tt, durs in type_durations.items():
            if len(durs) >= 3:
                sd = sorted(durs)
                mid = len(sd) // 2
                stats[tt] = {
                    "p50_seconds": sd[mid]
                    if len(sd) % 2
                    else (sd[mid - 1] + sd[mid]) / 2,
                    "sample_count": len(durs),
                }
        scope_stats: dict = {}
        for sc, durs in scope_durations.items():
            if len(durs) >= 3:
                sd = sorted(durs)
                n = len(sd)
                mid = n // 2
                p50 = sd[mid] if n % 2 else (sd[mid - 1] + sd[mid]) / 2
                scope_stats[sc] = {
                    "p50_seconds": p50,
                    "p90_seconds": sd[min(n - 1, int(0.9 * n))],
                    "sample_size": n,
                }
        provisioning_durs: list = []
        execution_durs: list = []
        for t in completed:
            if t.vm_provisioned_at and t.vm_started_at and t.vm_stopped_at:
                prov = (t.vm_started_at - t.vm_provisioned_at).total_seconds()
                execd = (t.vm_stopped_at - t.vm_started_at).total_seconds()
                if prov >= 0 and execd >= 0:
                    provisioning_durs.append(prov)
                    execution_durs.append(execd)
        phase_duration_stats: dict = {}
        for name, durs in (
            ("provisioning", provisioning_durs),
            ("execution", execution_durs),
        ):
            if len(durs) >= 3:
                sd = sorted(durs)
                n = len(sd)
                mid = n // 2
                p50 = sd[mid] if n % 2 else (sd[mid - 1] + sd[mid]) / 2
                phase_duration_stats[name] = {"p50_seconds": p50, "sample_size": n}
        return {
            "stats": stats,
            "scope_duration_stats": scope_stats,
            "phase_duration_stats": phase_duration_stats,
        }
    sql = text(
        """
        WITH base AS (
            SELECT
                COALESCE(payloads -> 0 ->> 'type', 'create') AS tt,
                CASE
                    WHEN context ->> 'scope' IN ('small', 'medium', 'large')
                        THEN context ->> 'scope'
                END AS scope,
                EXTRACT(EPOCH FROM (
                    CASE
                        WHEN status_log -> -1 ->> 't' ~ '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}'
                            THEN (status_log -> -1 ->> 't')::timestamp
                        ELSE created_at
                    END
                    - started_at
                )) AS dur,
                EXTRACT(EPOCH FROM (vm_started_at - vm_provisioned_at)) AS prov_dur,
                EXTRACT(EPOCH FROM (vm_stopped_at - vm_started_at)) AS exec_dur,
                (vm_provisioned_at IS NOT NULL
                 AND vm_started_at IS NOT NULL
                 AND vm_stopped_at IS NOT NULL) AS has_phase
            FROM task
            WHERE status = 'COMPLETED'
              AND started_at IS NOT NULL
              AND created_at >= :cutoff
              AND (CAST(:project_id AS uuid) IS NULL OR project_id = CAST(:project_id AS uuid))
        ),
        type_agg AS (
            SELECT 'type'::text AS kind, tt AS key,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY dur) AS p50,
                   NULL::double precision AS p90,
                   count(*) AS n
            FROM base
            WHERE dur > 0
            GROUP BY tt
            HAVING count(*) >= 3
        ),
        scope_agg AS (
            SELECT 'scope'::text AS kind, scope AS key,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY dur) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY dur) AS p90,
                   count(*) AS n
            FROM base
            WHERE dur > 0 AND scope IS NOT NULL
            GROUP BY scope
            HAVING count(*) >= 3
        ),
        phase_agg AS (
            SELECT 'phase'::text AS kind, phase_name AS key,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS p50,
                   NULL::double precision AS p90,
                   count(*) AS n
            FROM (
                SELECT 'provisioning'::text AS phase_name, prov_dur AS d
                FROM base WHERE has_phase AND prov_dur >= 0 AND exec_dur >= 0
                UNION ALL
                SELECT 'execution'::text AS phase_name, exec_dur AS d
                FROM base WHERE has_phase AND prov_dur >= 0 AND exec_dur >= 0
            ) q
            GROUP BY phase_name
            HAVING count(*) >= 3
        )
        SELECT kind, key, p50, p90, n FROM type_agg
        UNION ALL
        SELECT kind, key, p50, p90, n FROM scope_agg
        UNION ALL
        SELECT kind, key, p50, p90, n FROM phase_agg
        """
    )
    rows = (
        await session.execute(
            sql,
            {"cutoff": cutoff, "project_id": str(project_id) if project_id else None},
        )
    ).all()
    stats: dict = {}
    scope_stats: dict = {}
    phase_duration_stats: dict = {}
    for r in rows:
        if r.kind == "type":
            stats[r.key] = {"p50_seconds": float(r.p50), "sample_count": int(r.n)}
        elif r.kind == "scope":
            scope_stats[r.key] = {
                "p50_seconds": float(r.p50),
                "p90_seconds": float(r.p90),
                "sample_size": int(r.n),
            }
        elif r.kind == "phase":
            phase_duration_stats[r.key] = {
                "p50_seconds": float(r.p50),
                "sample_size": int(r.n),
            }
    return {
        "stats": stats,
        "scope_duration_stats": scope_stats,
        "phase_duration_stats": phase_duration_stats,
    }


@router.get("/failing-task-types")
async def get_failing_task_types(
    current_user: dict = Depends(require_admin),
):
    """Return task types currently gated by the orchestrator's per-type failure rate.

    Recomputes the same logic the dispatch loop uses (rate >= 0.7 with n >= 3
    in the trailing 15-minute window) from the shared task_results buffer, so
    operators can see which types are silently being skipped without reading
    logs.  Also reports the global failure_gate_threshold so callers can tell
    whether anomaly response has tightened it (0.6) vs the default (0.8).
    """
    from app import shared_state as _ss

    PER_TYPE_THRESHOLD = 0.7
    PER_TYPE_MIN_SAMPLES = 3
    WINDOW_MINUTES = 15

    cutoff = (_utcnow() - timedelta(minutes=WINDOW_MINUTES)).isoformat()
    recent = [r for r in _ss.deploy_monitoring["task_results"] if r[0] >= cutoff]
    seen_types = {r[3] for r in recent if len(r) > 3 and r[3]}

    gated: List[Dict[str, Any]] = []
    for ttype in sorted(seen_types):
        rate, sample_size = _ss.get_task_type_failure_rate(
            ttype, window_minutes=WINDOW_MINUTES
        )
        if sample_size >= PER_TYPE_MIN_SAMPLES and rate >= PER_TYPE_THRESHOLD:
            gated.append(
                {
                    "task_type": ttype,
                    "failure_rate": round(rate, 3),
                    "sample_size": sample_size,
                    "threshold": PER_TYPE_THRESHOLD,
                }
            )

    global_threshold = _ss.anomaly_adjustments.get("failure_gate_threshold", 0.8)
    return {
        "gated_types": gated,
        "global_failure_threshold": global_threshold,
        "anomaly_tightened": bool(_ss.anomaly_adjustments.get("active")),
        "window_minutes": WINDOW_MINUTES,
        "generated_at": _utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Open PR monitor
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = {
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    # PARKED is in-flight work waiting on an external wake — include it so a
    # parked task linked to an open PR still surfaces on the in-flight widget
    # rather than vanishing while it waits.
    TaskStatus.PARKED,
}


# Server-side cache for the open-PRs dashboard widget.  Dashboard polls at
# 30s; without caching, every open tab = 120 GitHub calls/hr to /pulls.
# With a 60s cache, total is capped at 60/hr regardless of how many
# browser tabs are open.
_OPEN_PRS_CACHE_TTL_SECONDS = 60
_open_prs_cache: Dict[uuid.UUID, Tuple[float, dict]] = {}


@router.get("/open-prs")
async def get_open_prs(
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return all open PRs from GitHub cross-referenced with linked tasks."""
    from app.services import _get_project_github_token
    from app.github import get_open_pull_requests

    project = (
        (await session.execute(select(Project).where(Project.id == project_id)))
        .scalars()
        .first()
    )
    if not project or not project.github_owner or not project.github_repo:
        return {"open_prs": []}

    # Cache check (skipping early paths above because github_owner/repo can
    # change and we want to honor that immediately).
    cached = _open_prs_cache.get(project_id)
    if cached and time.time() - cached[0] < _OPEN_PRS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        token = await _get_project_github_token(project, session)
    except Exception:
        logger.warning("Failed to get GitHub token for project %s", project_id)
        return {"open_prs": []}
    if not token:
        return {"open_prs": []}

    github_prs = await get_open_pull_requests(
        token, project.github_owner, project.github_repo
    )

    # Build task lookup: pr_number -> list of linked active tasks.
    # Terminal statuses (COMPLETED/FAILED/CANCELLED) are excluded — the widget
    # is for in-flight work. Dedupe by task.id since a task can match via
    # both a PR artifact and a context pr_number.
    pr_task_map: Dict[int, Dict[uuid.UUID, Task]] = {}

    # Source 1: PR artifacts (agent-created PRs — extract number from URL)
    artifact_rows = (
        await session.execute(
            select(Artifact, Task)
            .join(Task, Artifact.task_id == Task.id)
            .where(
                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                Artifact.pr_url.isnot(None),
                Task.project_id == project_id,
                Task.status.in_(list(_ACTIVE_STATUSES)),
            )
        )
    ).all()
    for art, task in artifact_rows:
        try:
            pr_num = int(art.pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            continue
        pr_task_map.setdefault(pr_num, {})[task.id] = task

    # Source 2: Tasks with pr_number in context (review/fix pipeline + rollups)
    active_tasks = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.context,
                        Task.status,
                        Task.title,
                        Task.payloads,
                        Task.created_at,
                    )
                )
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(list(_ACTIVE_STATUSES)),
                    or_(
                        Task.context["review_loop"]["pr_number"]
                        .as_integer()
                        .isnot(None),
                        Task.context["rollup_pr_number"].as_integer().isnot(None),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for t in active_tasks:
        ctx = t.context or {}
        pr_num = (ctx.get("review_loop") or {}).get("pr_number") or ctx.get(
            "rollup_pr_number"
        )
        if pr_num and isinstance(pr_num, int):
            pr_task_map.setdefault(pr_num, {})[t.id] = t

    result = []
    for gh_pr in github_prs:
        pr_number = gh_pr.get("number")
        pr_url = gh_pr.get("html_url", "")
        linked = list(pr_task_map.get(pr_number, {}).values())
        linked.sort(key=lambda t: t.created_at or datetime.min, reverse=True)
        tasks_payload = [
            {
                "task_id": str(t.id),
                "task_status": t.status,
                "task_type": t.first_payload.get("type", "create"),
                "task_title": t.title,
            }
            for t in linked
        ]
        result.append(
            {
                "pr_number": gh_pr.get("number"),
                "pr_url": pr_url,
                "pr_title": gh_pr.get("title", ""),
                "pr_branch": (gh_pr.get("head") or {}).get("ref", ""),
                "pr_base_branch": (gh_pr.get("base") or {}).get("ref", ""),
                "pr_created_at": gh_pr.get("created_at"),
                "tasks": tasks_payload,
                "is_orphaned": len(tasks_payload) == 0,
            }
        )

    payload = {
        "open_prs": result,
        "project_integration_branch": get_project_integration_branch(project),
        "project_promotion_target": get_project_promotion_target(project),
    }
    _open_prs_cache[project_id] = (time.time(), payload)
    return payload
