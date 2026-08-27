"""Residual admin analytics endpoints (direction auto-pause, task duration, failing task types, open PRs)."""

import time
import uuid
import logging
from collections import defaultdict
from typing import Dict, Any, List, Tuple
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import DateTime, and_, case, cast, func, text, or_
from sqlalchemy import select as sa_select
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
    ReleaseVersion,
    ImprovementLane,
    ImprovementLaneStatus,
    PoolVm,
    _utcnow,
    get_task_total_vm_cost_usd_or_none,
    _LANE_MAX_PHASE_REQUEUES,
)
from app.admin import _get_user_project_with_role
from app.auth import get_current_user, require_admin
from app.vm_cost_rates import (
    VM_COST_RATES,
    vm_cost_evidence_filter,
    vm_cost_sql_expr,
)
from app.shared_state import (
    get_project_integration_branch,
    get_project_promotion_target,
)
from app.review_heuristics import (
    LANE_CI,
    LANE_CONFLICT,
    LANE_REVIEW_CONVERGENCE,
    classify_iteration_lane,
)
from app.digest_metrics import (
    compute_lane_baseline_trend,
    compute_lane_waste_diagnostics,
)
from app.platform_kill_metrics import compute_platform_kill_metrics
from app.task_surface_visibility import non_resident_infrastructure_task_filter
from app.routers.admin_pipeline_observability import (
    _load_escalation_alerts,
    _load_outer_loop_original_tasks,
)
from app.time_utils import utc_iso

# Re-export for test and internal use (single source of truth is vm_cost_rates)
ON_DEMAND_RATE = VM_COST_RATES["e2-standard-2"][0] / 3600  # $/sec
SPOT_RATE = VM_COST_RATES["e2-standard-2"][1] / 3600  # $/sec

logger = logging.getLogger(__name__)


async def _compute_shadow_lane_health(
    project_id: "uuid.UUID", session: "AsyncSession"
) -> Dict[str, Any]:
    """Compute project-scoped shadow checkpoint health via SQL aggregates.

    Shadow lanes are those with pilot_arm IS NULL and checkpoint_seq > 0 (no ITT
    arm assigned, but the checkpoint contract has run at least once).  Uses SQL
    aggregates to avoid hydrating full ORM objects on every admin lanes poll.
    """
    _base_filter = [
        ImprovementLane.project_id == project_id,
        ImprovementLane.pilot_arm.is_(None),
        ImprovementLane.checkpoint_seq > 0,
    ]
    agg_row = (
        await session.execute(
            select(
                func.count().label("shadow_lane_count"),
                func.coalesce(func.sum(ImprovementLane.checkpoint_seq), 0).label(
                    "checkpoints_written"
                ),
                func.coalesce(func.sum(ImprovementLane.phase_requeues), 0).label(
                    "checkpoint_miss_count"
                ),
            ).where(*_base_filter)
        )
    ).one()

    shadow_lane_count = agg_row.shadow_lane_count
    checkpoints_written = int(agg_row.checkpoints_written)
    checkpoint_miss_count = int(agg_row.checkpoint_miss_count)

    # exhausted_close_count needs a JSONB condition; run as a separate count.
    exhausted_close_count = (
        await session.execute(
            select(func.count()).where(
                *_base_filter,
                ImprovementLane.status == ImprovementLaneStatus.KILLED.value,
                ImprovementLane.manifest["recovery"]["exhausted"].as_boolean(),
            )
        )
    ).scalar() or 0

    # None when no misses have been observed (distinguishes "no data" from 0%).
    miss_rate: float | None = (
        checkpoint_miss_count / (checkpoints_written + checkpoint_miss_count)
        if checkpoint_miss_count > 0
        else None
    )
    return {
        "shadow_lane_count": shadow_lane_count,
        "checkpoints_written": checkpoints_written,
        "checkpoint_miss_count": checkpoint_miss_count,
        "exhausted_close_count": exhausted_close_count,
        "miss_rate": miss_rate,
    }


def _derive_completed_at(t: "Task") -> datetime:
    """Return the durable completion timestamp, falling back to status_log/created_at.

    Always returns a naive UTC datetime — `task.created_at` may be TIMESTAMPTZ
    (migration 0061) and the JSON `t` strings are written without tz, so callers
    of this function must not mix the two without normalization.

    Prefers the durable `Task.completed_at` column, which is centrally stamped on
    terminal status transitions and cleared on revive (see models.py). Unlike the
    `status_log` tail, it is immune to post-terminal `latest_status_message` writes
    that append audit rows to `status_log` — those cannot silently move a completed
    task's derived time forward across a reporting window.

    IMPORTANT: `t.completed_at` must be present in the `load_only(...)` column list
    of every query whose rows flow here; under `raiseload=True` an unloaded column
    raises rather than lazy-loading.

    When `completed_at` is null (legacy rows never stamped), falls back to the prior
    behavior: if the caller projected `status_log[-1]->>'t'` SQL-side and stashed it
    on `t._projected_completed_at_ts` (see admin_digest.py) use that (so the heavy
    `status_log` JSONB column is not materialized), else the `status_log` tail, else
    `task.created_at`.
    """

    def _strip_tz(d: datetime) -> datetime:
        return d.replace(tzinfo=None) if d.tzinfo is not None else d

    durable = t.completed_at
    if durable is not None:
        return _strip_tz(durable)

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
_LANE_DIAG_LANES = (
    LANE_CONFLICT,
    LANE_CI,
    LANE_REVIEW_CONVERGENCE,
)
_LANE_DIAG_FIX_TYPES = {"create", "fix"}

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
        elif ts >= prev_window_start and milestone == "auto-paused":
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
                    .options(
                        load_only(
                            Project.id,
                            Project.directions,
                            Project.status,
                            raiseload=True,
                        )
                    )
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
                .options(
                    load_only(
                        ProjectMessage.context,
                        ProjectMessage.created_at,
                        raiseload=True,
                    )
                )
                .order_by(ProjectMessage.created_at.desc())
                .limit(2_000)
            )
        )
        .scalars()
        .all()
    )
    return _compute_direction_auto_pause_stats(projects, events, now, window_hours)


def _task_completed_at(task: Task) -> datetime:
    """Return the task completion timestamp used for cost windowing."""
    return _derive_completed_at(task)


def _task_completed_at_sql(*, dialect_name: str):
    """SQL expression mirroring ``_derive_completed_at`` for windowed scans.

    Prefers the durable ``completed_at`` column (centrally stamped on terminal
    transitions and immune to post-terminal ``status_log`` audit writes), then
    the parseable ``status_log`` tail, then ``created_at`` — the exact fallback
    chain of ``_derive_completed_at``.  Used as the authoritative windowing key
    for the cost widgets that aggregate entirely in SQL.
    """

    if dialect_name == "postgresql":
        projected_completed_at = Task.status_log[-1]["t"].astext
        return cast(
            func.coalesce(
                Task.completed_at,
                case(
                    (
                        projected_completed_at.op("~")(
                            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
                        ),
                        cast(projected_completed_at, DateTime()),
                    ),
                    else_=None,
                ),
                Task.created_at,
            ),
            DateTime(),
        )

    return cast(
        func.coalesce(
            Task.completed_at,
            func.datetime(func.json_extract(Task.status_log, "$[#-1].t")),
            Task.created_at,
        ),
        DateTime(),
    )


def _percentile(values: List[float], fraction: float) -> float:
    ordered = sorted(values)
    return (
        float(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))])
        if ordered
        else 0.0
    )


def _review_loop_pr_number(task: Task) -> str | None:
    review_loop = (task.context or {}).get("review_loop") or {}
    pr_number = review_loop.get("pr_number")
    if pr_number is None:
        return None
    pr_key = str(pr_number).strip()
    return pr_key or None


def _compute_lane_diag(tasks: List[Task], pr_breakdown_limit: int) -> Dict[str, Any]:
    lane_counts_by_pr: Dict[str, Dict[str, int]] = defaultdict(
        lambda: dict.fromkeys(_LANE_DIAG_LANES, 0)
    )
    relay_counts: Dict[str, int] = dict.fromkeys(_LANE_DIAG_LANES, 0)
    pr_phase: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "task_count": 0,
            "review_seconds": 0.0,
            "fix_seconds": 0.0,
            "queue_seconds": 0.0,
            "provision_seconds": 0.0,
            "cold_start_seconds": 0.0,
            "execution_seconds": 0.0,
            "first_created_at": None,
            "last_completed_at": None,
        }
    )

    for task in tasks:
        pr_number = _review_loop_pr_number(task)
        if not pr_number:
            continue
        lane = classify_iteration_lane(task)
        if lane not in lane_counts_by_pr[pr_number]:
            lane = LANE_REVIEW_CONVERGENCE
        lane_counts_by_pr[pr_number][lane] += 1

        review_loop = (task.context or {}).get("review_loop") or {}
        payload_type = str((task.first_payload or {}).get("type") or "create")
        if payload_type in _LANE_DIAG_FIX_TYPES and review_loop.get("fix_reason"):
            relay_counts[lane] += 1

        completed_at = _derive_completed_at(task)
        phase = pr_phase[pr_number]
        phase["task_count"] += 1
        if (
            phase["first_created_at"] is None
            or task.created_at < phase["first_created_at"]
        ):
            phase["first_created_at"] = task.created_at
        if (
            phase["last_completed_at"] is None
            or completed_at > phase["last_completed_at"]
        ):
            phase["last_completed_at"] = completed_at

        if task.started_at:
            active_seconds = max((completed_at - task.started_at).total_seconds(), 0.0)
            if payload_type == "review":
                phase["review_seconds"] += active_seconds
            elif payload_type in _LANE_DIAG_FIX_TYPES:
                phase["fix_seconds"] += active_seconds

        if task.created_at and task.vm_dispatched_at:
            phase["queue_seconds"] += max(
                (task.vm_dispatched_at - task.created_at).total_seconds(), 0.0
            )
        if task.vm_dispatched_at and task.vm_provisioned_at:
            phase["provision_seconds"] += max(
                (task.vm_provisioned_at - task.vm_dispatched_at).total_seconds(), 0.0
            )
        if task.vm_provisioned_at and task.vm_started_at:
            phase["cold_start_seconds"] += max(
                (task.vm_started_at - task.vm_provisioned_at).total_seconds(), 0.0
            )
        if task.vm_started_at and task.vm_stopped_at:
            phase["execution_seconds"] += max(
                (task.vm_stopped_at - task.vm_started_at).total_seconds(), 0.0
            )

    lane_distribution: Dict[str, Dict[str, Any]] = {}
    for lane in _LANE_DIAG_LANES:
        per_pr_counts = [counts[lane] for counts in lane_counts_by_pr.values()]
        lane_distribution[lane] = {
            "p50_iterations": _percentile(per_pr_counts, 0.5),
            "p90_iterations": _percentile(per_pr_counts, 0.9),
            "max_iterations": max(per_pr_counts) if per_pr_counts else 0,
            "task_count": sum(per_pr_counts),
            "pr_count": sum(1 for count in per_pr_counts if count > 0),
        }

    per_pr_breakdown: List[Dict[str, Any]] = []
    for pr_number, phase in pr_phase.items():
        review_seconds = phase["review_seconds"]
        fix_seconds = phase["fix_seconds"]
        active_seconds = review_seconds + fix_seconds
        first_created_at = phase["first_created_at"]
        last_completed_at = phase["last_completed_at"]
        elapsed_seconds = (
            max((last_completed_at - first_created_at).total_seconds(), 0.0)
            if first_created_at and last_completed_at
            else 0.0
        )
        gap_seconds = max(elapsed_seconds - active_seconds, 0.0)
        total_seconds = active_seconds + gap_seconds
        per_pr_breakdown.append(
            {
                "pr_number": pr_number,
                "task_count": phase["task_count"],
                "review_seconds": review_seconds,
                "fix_seconds": fix_seconds,
                "gap_seconds": gap_seconds,
                "total_seconds": total_seconds,
                "queue_seconds": phase["queue_seconds"],
                "provision_seconds": phase["provision_seconds"],
                "cold_start_seconds": phase["cold_start_seconds"],
                "execution_seconds": phase["execution_seconds"],
            }
        )

    per_pr_breakdown.sort(key=lambda row: (-row["total_seconds"], row["pr_number"]))
    return {
        "lane_iteration_distribution": lane_distribution,
        "per_pr_phase_breakdown": per_pr_breakdown[:pr_breakdown_limit],
        "relay_counts_by_lane": relay_counts,
        "pr_count": len(pr_phase),
        "task_count": sum(phase["task_count"] for phase in pr_phase.values()),
        "pr_breakdown_limit": pr_breakdown_limit,
        "pr_breakdown_truncated": len(per_pr_breakdown) > pr_breakdown_limit,
    }


@router.get("/task-cost-breakdown")
async def get_task_cost_breakdown(
    project_id: uuid.UUID | None = Query(None),
    window: str = Query("1h"),
    top_n: int = Query(5, ge=1, le=20),
    max_tasks: int = Query(1000, ge=1, le=5000),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return spend breakdowns by task type, direction, and most expensive tasks."""
    if window not in _TASK_COST_WINDOW_MS:
        raise HTTPException(status_code=400, detail=f"Unsupported window '{window}'")

    cutoff = None
    if window != "all":
        cutoff = _utcnow() - timedelta(milliseconds=_TASK_COST_WINDOW_MS[window])

    dialect_name = resolve_dialect_name(session)
    if dialect_name == "postgresql":
        # Fast path: cap the newest ``max_tasks`` terminal rows by created_at,
        # then window/evidence-filter and aggregate cost entirely in SQL. No
        # JSONB-heavy rows (context/payloads/status_log) are hydrated into
        # Python — that per-row materialization was the dominant cost.
        cost_sql = vm_cost_sql_expr(dialect_name)
        completed_at_sql = _task_completed_at_sql(dialect_name=dialect_name)
        type_sql = func.coalesce(Task.payloads[0]["type"].astext, "create")
        direction_id_sql = func.coalesce(
            Task.context["direction_id"].astext,
            Task.payloads[0]["current_direction_id"].astext,
        )
        direction_text_sql = func.nullif(
            func.coalesce(
                Task.context["direction_text"].astext,
                Task.payloads[0]["direction_text"].astext,
                "",
            ),
            "",
        )
        base_filters = [
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            )
        ]
        if project_id is not None:
            base_filters.append(Task.project_id == project_id)

        capped = (
            sa_select(
                Task.id.label("task_id"),
                Task.title.label("task_title"),
                type_sql.label("task_type"),
                direction_id_sql.label("direction_id"),
                direction_text_sql.label("direction_text"),
                completed_at_sql.label("completed_at"),
                cost_sql.label("cost"),
                vm_cost_evidence_filter().label("has_evidence"),
            )
            .where(*base_filters)
            .order_by(Task.created_at.desc())
            .limit(max_tasks)
            .subquery("capped")
        )
        c = capped.c
        row_filters = [c.has_evidence.is_(True)]
        if cutoff is not None:
            row_filters.append(c.completed_at >= cutoff)

        # ``truncated`` reflects the created_at cap BEFORE window/evidence
        # filtering — identical semantics to ``len(tasks) >= max_tasks``.
        capped_count = (
            await session.execute(sa_select(func.count()).select_from(capped))
        ).scalar_one()

        type_rows = (
            await session.execute(
                sa_select(
                    c.task_type, func.count(), func.coalesce(func.sum(c.cost), 0.0)
                )
                .where(*row_filters)
                .group_by(c.task_type)
            )
        ).all()
        direction_key = func.coalesce(c.direction_id, "unassigned")
        dir_rows = (
            await session.execute(
                sa_select(
                    direction_key,
                    func.count(),
                    func.coalesce(func.sum(c.cost), 0.0),
                    func.max(c.direction_text),
                )
                .where(*row_filters)
                .group_by(direction_key)
            )
        ).all()
        top_rows = (
            await session.execute(
                sa_select(
                    c.task_id,
                    c.task_title,
                    c.task_type,
                    c.direction_id,
                    c.completed_at,
                    c.cost,
                )
                .where(*row_filters)
                .order_by(c.cost.desc(), c.completed_at.asc(), c.task_title.asc())
                .limit(top_n)
            )
        ).all()

        task_type_breakdown = [
            {
                "task_type": tt,
                "task_count": int(cnt),
                "estimated_cost_usd": round(float(cost), 2),
            }
            for tt, cnt, cost in sorted(type_rows, key=lambda r: (-float(r[2]), r[0]))
        ]
        direction_breakdown = [
            {
                "direction_id": None if key == "unassigned" else key,
                "direction_text": text_val or None,
                "task_count": int(cnt),
                "estimated_cost_usd": round(float(cost), 2),
            }
            for key, cnt, cost, text_val in sorted(
                dir_rows, key=lambda r: (-float(r[2]), r[0])
            )
        ]
        top_tasks = [
            {
                "task_id": str(task_id),
                "task_title": task_title,
                "task_type": task_type,
                "direction_id": direction_id,
                "completed_at": utc_iso(completed_at),
                "estimated_cost_usd": round(float(cost), 2),
            }
            for task_id, task_title, task_type, direction_id, completed_at, cost in top_rows
        ]
        return {
            "project_id": str(project_id) if project_id else None,
            "window": window,
            "window_ms": _TASK_COST_WINDOW_MS[window] if window != "all" else None,
            "window_start": utc_iso(cutoff),
            "task_count": sum(item["task_count"] for item in task_type_breakdown),
            "max_tasks": max_tasks,
            "truncated": capped_count >= max_tasks,
            "total_estimated_cost_usd": round(
                sum(float(cost) for _, _, cost in type_rows), 2
            ),
            "task_type_breakdown": task_type_breakdown,
            "direction_breakdown": direction_breakdown,
            "top_tasks": top_tasks,
        }

    # SQLite fallback (test harness without Postgres): compute per-row in Python.
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
                        Task.completed_at,
                        Task.status_log,
                        Task.payloads,
                        Task.context,
                        Task.vm_provisioned_at,
                        Task.vm_stopped_at,
                        Task.vm_spot,
                        Task.vm_runtime_cost_banked_db,
                        raiseload=True,
                    )
                )
                .where(*filters)
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
        direction_text_str = (
            ctx.get("direction_text") or payload.get("direction_text") or ""
        )
        rows.append(
            {
                "task_id": str(task.id),
                "task_title": task.title,
                "task_type": task_type,
                "direction_id": direction_id_str,
                "direction_text": direction_text_str,
                "completed_at": completed_at,
                "estimated_cost_usd": cost,
            }
        )

    by_task_type: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"task_count": 0, "estimated_cost_usd": 0.0}
    )
    by_direction: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"task_count": 0, "estimated_cost_usd": 0.0, "direction_text": ""}
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
        if row["direction_text"] and not direction_entry["direction_text"]:
            direction_entry["direction_text"] = row["direction_text"]

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
            "direction_text": data["direction_text"] or None,
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
            "completed_at": utc_iso(row['completed_at']),
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
        "window_start": utc_iso(cutoff),
        "task_count": len(rows),
        "max_tasks": max_tasks,
        "truncated": len(tasks) >= max_tasks,
        "total_estimated_cost_usd": round(total_cost, 2),
        "task_type_breakdown": task_type_breakdown,
        "direction_breakdown": direction_breakdown,
        "top_tasks": top_tasks,
    }


@router.get("/spend-burn-rate")
async def get_spend_burn_rate(
    project_id: uuid.UUID | None = Query(None),
    window_days: int = Query(7, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return daily spend burn rate and monthly cost forecast.

    Queries completed tasks over ``window_days`` to compute a rolling daily
    average and a 30-day projection so operators can track spend trajectory.
    """
    dialect_name = resolve_dialect_name(session)
    cutoff = _utcnow() - timedelta(days=window_days)
    terminal_at_sql = _task_completed_at_sql(dialect_name=dialect_name)
    cost_sql = vm_cost_sql_expr(dialect_name)
    filters = [
        Task.status.in_(
            [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ),
        terminal_at_sql >= cutoff,
        # Only tasks with cost evidence contribute, matching the prior
        # ``get_task_total_vm_cost_usd_or_none() is None`` skip.
        vm_cost_evidence_filter(),
    ]
    if project_id is not None:
        filters.append(Task.project_id == project_id)

    # Sum cost and count in SQL — no row hydration, and the exact window is
    # aggregated (the old 5000-row cap could silently drop spend past the cap).
    agg = (
        await session.execute(
            sa_select(
                func.coalesce(func.sum(cost_sql), 0.0),
                func.count(),
            ).where(*filters)
        )
    ).one()
    total_cost = float(agg[0] or 0.0)
    task_count = int(agg[1] or 0)

    # Derive the daily rate and 30-day forecast from the SAME rounded window
    # cost we report, so the three figures stay internally consistent (an
    # operator can reconcile monthly ≈ daily*30 ≈ window/days*30); deriving the
    # forecast off the un-rounded total made monthly drift from window_cost_usd.
    window_cost_usd = round(total_cost, 2)
    daily_spend_usd = window_cost_usd / window_days if window_days > 0 else 0.0
    monthly_forecast_usd = daily_spend_usd * 30

    return {
        "project_id": str(project_id) if project_id else None,
        "window_days": window_days,
        "window_cost_usd": window_cost_usd,
        "task_count": task_count,
        "daily_spend_usd": round(daily_spend_usd, 2),
        "monthly_forecast_usd": round(monthly_forecast_usd, 2),
    }


@router.get("/lane-diag")
async def get_lane_diag(
    project_id: uuid.UUID | None = Query(None),
    max_tasks: int = Query(5000, ge=1, le=5000),
    pr_breakdown_limit: int = Query(100, ge=1, le=250),
    version_number: int | None = Query(None, ge=1),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return lane diagnostics for recent review/fix pipeline tasks.

    Backward-compatible: the original keys (``lane_iteration_distribution``,
    ``per_pr_phase_breakdown``, ``relay_counts_by_lane``, ``pr_count``,
    ``task_count`` …) still aggregate only COMPLETED tasks whose
    ``review_loop.pr_number`` is set and which completed in-window. Additively,
    ``lane_waste`` reports chain-complete review/fix waste — including terminal
    FAILED/CANCELLED iterations belonging to an in-window PR chain — using the
    same PR-chain semantics as the outer-loop convergence surfaces.

    When ``version_number`` is supplied, tasks are scoped to the time window of
    that release version (``opened_at`` to ``promoted_at``, or now for open
    versions). Version metadata is included in the response.
    """
    now = _utcnow()
    version_meta: dict | None = None
    if version_number is not None:
        version_filters = [ReleaseVersion.version_number == version_number]
        if project_id is not None:
            version_filters.append(ReleaseVersion.project_id == project_id)
        rv = (
            (
                await session.execute(
                    select(ReleaseVersion).where(*version_filters).limit(1)
                )
            )
            .scalars()
            .first()
        )
        if rv is None:
            raise HTTPException(
                status_code=404,
                detail=f"ReleaseVersion {version_number} not found",
            )
        window_start = rv.opened_at
        # For closed (non-open) versions, derive the actual close time.
        # Promoted versions use promoted_at; abandoned versions recover the
        # abandonment timestamp from last_qualification provenance (written by
        # abandon_current_version as record["abandoned_at"] = now.isoformat()).
        # Falling back to `now` for a genuinely open version keeps the live
        # rolling window behavior unchanged.
        abandoned_at_str: str | None = None
        abandoned_at_dt: datetime | None = None
        if rv.status != "open":
            abandoned_at_str = (rv.last_qualification or {}).get("abandoned_at")
            if abandoned_at_str:
                try:
                    _parsed = datetime.fromisoformat(abandoned_at_str)
                    abandoned_at_dt = (
                        _parsed.replace(tzinfo=None) if _parsed.tzinfo else _parsed
                    )
                except (ValueError, TypeError):
                    pass
        if rv.status == "open":
            window_end: datetime = now
        elif rv.promoted_at is not None:
            window_end = rv.promoted_at
        elif abandoned_at_dt is not None:
            window_end = abandoned_at_dt
        else:
            window_end = now
        version_meta = {
            "version_number": rv.version_number,
            "phase": rv.phase,
            "status": rv.status,
            "opened_at": utc_iso(rv.opened_at),
            "promoted_at": utc_iso(rv.promoted_at),
            "abandoned_at": utc_iso(abandoned_at_dt),
        }
        cutoff = window_start
    else:
        cutoff = now - timedelta(days=7)
        window_end = None

    project_filter = [Task.project_id == project_id] if project_id else []

    _task_columns = load_only(
        Task.id,
        Task.project_id,
        Task.status,
        Task.created_at,
        Task.completed_at,
        Task.started_at,
        Task.updated_at,
        Task.status_log,
        Task.payloads,
        Task.context,
        Task.fix_loop_history,
        Task.archived,
        Task.vm_dispatched_at,
        Task.vm_provisioned_at,
        Task.vm_started_at,
        Task.vm_stopped_at,
        raiseload=True,
    )

    # Legacy surface query: COMPLETED-only, original form — the legacy aggregates
    # (lane_iteration_distribution, per_pr_phase_breakdown, task_count, pr_count,
    # relay_counts_by_lane) must always use THIS pool so that FAILED/CANCELLED
    # rows can never displace COMPLETED rows from the top-N and erode existing
    # response keys.
    # When version-scoped, push the created_at lower bound into SQL to keep
    # per-version pools tight.  Upper bound is applied in Python (window_end).
    completed_filters = [Task.status == TaskStatus.COMPLETED, *project_filter]
    terminal_filters = [
        Task.status.in_(
            [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ),
        *project_filter,
    ]
    if version_number is not None:
        completed_filters.append(Task.created_at >= cutoff)
        terminal_filters.append(Task.created_at >= cutoff)
        # For closed versions push the upper bound into SQL so that out-of-window
        # rows created after the close time (promoted_at or abandoned_at) cannot
        # consume the max_tasks cap before any in-window row is fetched.
        if rv.status != "open":
            completed_filters.append(Task.created_at < window_end)
            terminal_filters.append(Task.created_at < window_end)

    completed_tasks = (
        (
            await session.execute(
                select(Task)
                .options(_task_columns)
                .where(*completed_filters)
                .order_by(Task.created_at.desc())
                .limit(max_tasks)
            )
        )
        .scalars()
        .all()
    )

    def _in_window(task: Task) -> bool:
        completed_at = _derive_completed_at(task)
        if completed_at < cutoff:
            return False
        if window_end is not None and task.created_at is not None:
            created = task.created_at
            if created.tzinfo is not None:
                created = created.replace(tzinfo=None)
            if created >= window_end:
                return False
        return True

    scoped_tasks = [
        task
        for task in completed_tasks
        if _review_loop_pr_number(task) and _in_window(task)
    ]
    diag = _compute_lane_diag(scoped_tasks, pr_breakdown_limit)

    # Waste surface query: broadened to include terminal FAILED/CANCELLED rows so
    # chain-complete waste diagnostics are accurate.  Issued separately so it
    # cannot truncate the legacy COMPLETED pool above.
    all_terminal_tasks = (
        (
            await session.execute(
                select(Task)
                .options(_task_columns)
                .where(*terminal_filters)
                .order_by(Task.created_at.desc())
                .limit(max_tasks)
            )
        )
        .scalars()
        .all()
    )
    if window_end is not None:
        all_terminal_tasks = [
            t
            for t in all_terminal_tasks
            if t.created_at is None
            or (
                t.created_at.replace(tzinfo=None)
                if t.created_at.tzinfo
                else t.created_at
            )
            < window_end
        ]
    original_tasks = await _load_outer_loop_original_tasks(
        session, all_terminal_tasks, project_id=project_id
    )
    alerts = await _load_escalation_alerts(
        session, cutoff=cutoff, project_id=project_id
    )
    diag["lane_waste"] = compute_lane_waste_diagnostics(
        [
            *all_terminal_tasks,
            *[
                task
                for oid, task in original_tasks.items()
                if oid not in {str(t.id) for t in all_terminal_tasks}
            ],
        ],
        cutoff,
        chain_limit=pr_breakdown_limit,
        escalation_alerts=alerts,
    )
    from app.lane_baseline_snapshot import _compute_checkpoint_recovery_waste  # noqa: PLC0415

    diag["checkpoint_recovery_waste"] = _compute_checkpoint_recovery_waste(
        list(all_terminal_tasks)
    )
    extra: dict = {
        "project_id": str(project_id) if project_id else None,
        "window_start": utc_iso(cutoff),
        "max_tasks": max_tasks,
        "truncated": len(completed_tasks) >= max_tasks
        or len(all_terminal_tasks) >= max_tasks,
    }
    if version_number is None:
        extra["window_days"] = 7
    if version_meta is not None:
        extra["version"] = version_meta
    # For closed versions, include the persisted lane baseline if available.
    # A closed version's tasks may age past the rolling query cap; the stored
    # baseline remains reliably accessible even after tasks are archived.
    if version_meta is not None and rv.status != "open":
        try:
            from app.lane_baseline_snapshot import DESIGN_DOC_KEY

            _proj_doc = (
                await session.execute(
                    select(Project.design_doc).where(Project.id == rv.project_id)
                )
            ).scalar_one_or_none()
            if isinstance(_proj_doc, dict):
                _stored = (_proj_doc.get(DESIGN_DOC_KEY) or {}).get(
                    str(rv.version_number)
                )
                if _stored is not None:
                    extra["lane_baseline"] = _stored
                    extra["lane_baseline_source"] = "persisted"
        except Exception:
            logger.exception(
                "[LANE_DIAG] failed to load persisted baseline for v%d",
                rv.version_number,
            )
    diag.update(extra)
    return diag


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
                            Task.completed_at,
                            Task.started_at,
                            Task.payloads,
                            Task.context,
                            Task.vm_provisioned_at,
                            Task.vm_started_at,
                            Task.vm_stopped_at,
                            raiseload=True,
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
            # Use the durable terminal time (message-immune) rather than the
            # raw status_log tail so a post-terminal message write cannot
            # inflate the computed duration.
            cat = _derive_completed_at(t)
            if cat < cutoff:
                continue
            started = t.started_at
            if started is not None and started.tzinfo is not None:
                started = started.replace(tzinfo=None)
            dur = (cat - started).total_seconds()
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
            if _derive_completed_at(t) < cutoff:
                continue
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
                    COALESCE(
                        completed_at,
                        CASE
                            WHEN status_log -> -1 ->> 't' ~ '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}'
                                THEN (status_log -> -1 ->> 't')::timestamp
                            ELSE created_at
                        END
                    )
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
              AND COALESCE(
                    completed_at,
                    CASE
                        WHEN status_log -> -1 ->> 't' ~ '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}'
                            THEN (status_log -> -1 ->> 't')::timestamp
                        ELSE created_at
                    END
                  ) >= :cutoff
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
        "generated_at": utc_iso(_utcnow()),
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


def _build_open_prs_payload(
    project: Project | None,
    open_prs: list[dict],
    *,
    degraded: bool,
    degraded_reason: str | None,
) -> dict:
    integration_branch = get_project_integration_branch(project) if project else None
    promotion_target = get_project_promotion_target(project) if project else None
    return {
        "open_prs": open_prs,
        "project_integration_branch": integration_branch,
        "project_promotion_target": promotion_target,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
    }


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
        # Missing GitHub config is an operator-visible configuration state, not a GitHub outage.
        return _build_open_prs_payload(
            project, [], degraded=False, degraded_reason=None
        )

    # Cache check (skipping early paths above because github_owner/repo can
    # change and we want to honor that immediately).
    cached = _open_prs_cache.get(project_id)
    if cached and time.time() - cached[0] < _OPEN_PRS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        token = await _get_project_github_token(project, session)
    except Exception:
        logger.warning("Failed to get GitHub token for project %s", project_id)
        payload = _build_open_prs_payload(
            project,
            [],
            degraded=True,
            degraded_reason="github_token_error",
        )
        _open_prs_cache[project_id] = (time.time(), payload)
        return payload
    if not token:
        payload = _build_open_prs_payload(
            project,
            [],
            degraded=True,
            degraded_reason="no_token",
        )
        _open_prs_cache[project_id] = (time.time(), payload)
        return payload

    github_prs = await get_open_pull_requests(
        token, project.github_owner, project.github_repo
    )
    open_pr_numbers = {
        pr_number
        for pr_number in (gh_pr.get("number") for gh_pr in github_prs)
        if isinstance(pr_number, int)
    }
    if not open_pr_numbers:
        payload = _build_open_prs_payload(
            project,
            [],
            degraded=False,
            degraded_reason=None,
        )
        _open_prs_cache[project_id] = (time.time(), payload)
        return payload

    # Build task lookup: pr_number -> list of linked active tasks.
    # Terminal statuses (COMPLETED/FAILED/CANCELLED) are excluded — the widget
    # is for in-flight work. Dedupe by task.id since a task can match via
    # both a PR artifact and a context pr_number.
    pr_task_map: Dict[int, Dict[uuid.UUID, Task]] = {}

    artifact_pr_filters = [
        Artifact.pr_url.like(f"%/pull/{pr_num}") for pr_num in open_pr_numbers
    ]

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
                or_(*artifact_pr_filters),
            )
            .order_by(Task.created_at.desc(), Artifact.pr_url)
            .limit(500)
        )
    ).all()
    for art, task in artifact_rows:
        try:
            pr_num = int(art.pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            continue
        pr_task_map.setdefault(pr_num, {})[task.id] = task

    # Source 2: Tasks with pr_number in context (review/fix pipeline + rollups)
    context_pr_filters = [
        Task.context["review_loop"]["pr_number"].as_integer() == pr_num
        for pr_num in open_pr_numbers
    ] + [
        Task.context["rollup_pr_number"].as_integer() == pr_num
        for pr_num in open_pr_numbers
    ]
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
                        raiseload=True,
                    )
                )
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(list(_ACTIVE_STATUSES)),
                    or_(*context_pr_filters),
                )
                .order_by(Task.created_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    for t in active_tasks:
        ctx = t.context or {}
        context_pr_num: int | None = (ctx.get("review_loop") or {}).get(
            "pr_number"
        ) or ctx.get("rollup_pr_number")
        if context_pr_num and isinstance(context_pr_num, int):
            pr_task_map.setdefault(context_pr_num, {})[t.id] = t

    result = []
    for gh_pr in github_prs:
        pr_number = gh_pr.get("number")
        pr_url = gh_pr.get("html_url", "")
        linked = list(pr_task_map.get(pr_number, {}).values())
        linked.sort(
            key=lambda t: t.created_at or datetime.min.replace(tzinfo=None),
            reverse=True,
        )
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

    payload = _build_open_prs_payload(
        project,
        result,
        degraded=False,
        degraded_reason=None,
    )
    _open_prs_cache[project_id] = (time.time(), payload)
    return payload


# ---------------------------------------------------------------------------
# GET /api/admin/improvement-lanes
# ---------------------------------------------------------------------------


@router.get("/improvement-lanes")
async def get_improvement_lanes(
    project_id: uuid.UUID = Query(...),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """List improvement lanes for a project with status, checkpoint, workspace, and cost fields.

    Returns control-plane state for operators to monitor active and parked lanes:
    which lanes are ACTIVE/PARKED, how far each got (checkpoint seq/phase/sha),
    whether a workspace reservation is held and its reserved-idle cost, which
    pilot arm a lane is in, and whether checkpoints are being lost.

    ``shadow`` is True when the checkpoint contract is running but neither a warm
    workspace reservation nor a pilot arm has been assigned — indicates shadow mode.

    ``checkpoint_loss_count`` = lane.checkpoint_seq - len(lane.checkpoints).
    checkpoint_seq is the count of checkpoints successfully written; checkpoints[]
    is a rolling window capped at 50.  Any positive difference means older
    checkpoints were evicted from the window.  Note: phase_requeues (phases that
    failed to write any checkpoint) is a separate signal also included in the row.
    """
    lanes = (
        (
            await session.execute(
                select(ImprovementLane)
                .where(ImprovementLane.project_id == project_id)
                .order_by(ImprovementLane.updated_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # Resolve active warm-pool reservations for all returned lanes in one query.
    lane_ids = [lane.id for lane in lanes]
    reservations: Dict[uuid.UUID, PoolVm] = {}
    if lane_ids:
        pool_vms = (
            (
                await session.execute(
                    select(PoolVm).where(PoolVm.reserved_lane_id.in_(lane_ids))
                )
            )
            .scalars()
            .all()
        )
        for pvm in pool_vms:
            if pvm.reserved_lane_id is not None:
                reservations[pvm.reserved_lane_id] = pvm

    now = _utcnow()
    rows: List[Dict[str, Any]] = []
    for lane in lanes:
        # Derive age of the most recent checkpoint from its created_at field.
        last_checkpoint_age_seconds: float | None = None
        if lane.checkpoints:
            last_cp = lane.checkpoints[-1]
            try:
                cp_created_at = datetime.fromisoformat(last_cp["created_at"])
                # Strip tz if present (checkpoints write naive UTC via _utcnow).
                if cp_created_at.tzinfo is not None:
                    cp_created_at = cp_created_at.replace(tzinfo=None)
                last_checkpoint_age_seconds = max(
                    (now - cp_created_at).total_seconds(), 0.0
                )
            except (KeyError, ValueError, TypeError):
                pass

        # checkpoint_loss_count: checkpoints written but evicted from the rolling window.
        checkpoint_loss_count = max(
            0, (lane.checkpoint_seq or 0) - len(lane.checkpoints or [])
        )

        # shadow: checkpoint contract running but no reservation and no pilot arm yet.
        shadow = (
            (lane.checkpoint_seq or 0) > 0
            and lane.workspace_state == "COLD"
            and lane.pilot_arm is None
        )

        # Reservation fields from PoolVm if a reservation is active for this lane.
        pvm = reservations.get(lane.id)
        reservation_expires_at: str | None = None
        reserved_vm_instance_id: str | None = None
        if pvm is not None:
            reservation_expires_at = utc_iso(pvm.reservation_expires_at)
            reserved_vm_instance_id = pvm.vm_instance_id

        at_risk = (lane.phase_requeues or 0) >= (_LANE_MAX_PHASE_REQUEUES - 1)

        rows.append(
            {
                "id": str(lane.id),
                "title": lane.title,
                "status": lane.status,
                "direction_id": lane.direction_id,
                "branch": lane.branch_name,
                "last_checkpoint_phase": lane.last_checkpoint_phase,
                "last_checkpoint_seq": lane.checkpoint_seq,
                "last_checkpoint_sha": lane.last_checkpoint_sha,
                "last_checkpoint_age_seconds": last_checkpoint_age_seconds,
                "checkpoint_loss_count": checkpoint_loss_count,
                "workspace_state": lane.workspace_state,
                "workspace_vm_instance_id": lane.workspace_vm_instance_id,
                "reservation_expires_at": reservation_expires_at,
                "reserved_vm_instance_id": reserved_vm_instance_id,
                "pilot_arm": lane.pilot_arm,
                "phase_requeues": lane.phase_requeues,
                "at_risk": at_risk,
                # Operator-visible checkpoint-recovery state (reason/attempt/
                # exhausted) so a requeued or KILLED lane explains *why* rather
                # than surfacing only a phase_requeues counter. None until a
                # checkpoint miss drives the bounded-recovery path.
                "recovery": (lane.manifest or {}).get("recovery"),
                "llm_input_tokens_total": lane.llm_input_tokens_total,
                "llm_output_tokens_total": lane.llm_output_tokens_total,
                "vm_cost_usd_total": round(lane.vm_cost_usd_total or 0.0, 6),
                "reserved_idle_cost_usd_total": round(
                    lane.reserved_idle_cost_usd_total or 0.0, 6
                ),
                # Reservation expiry-cause telemetry so an operator can see WHY a
                # lane's warm_resume_fraction is low: how many reservations the
                # fix actually claimed vs. expired-before-fix vs. force-expired
                # (hard ceiling / KILL / retire / reaped member).
                "reservation_claimed_by_fix_count": (
                    lane.reservation_claimed_by_fix_count or 0
                ),
                "reservation_expired_before_fix_count": (
                    lane.reservation_expired_before_fix_count or 0
                ),
                "reservation_force_expired_count": (
                    lane.reservation_force_expired_count or 0
                ),
                "shadow": shadow,
                "created_at": utc_iso(lane.created_at),
                "updated_at": utc_iso(lane.updated_at),
            }
        )

    # Shadow health rollup: all armless lanes with checkpoints (full project scope,
    # not just the paginated window) so the operator sees aggregate health independent
    # of the current page.
    shadow_health = await _compute_shadow_lane_health(project_id, session)

    # Trial metrics: compute per-arm aggregates and evaluate the live verdict.
    # Wrapped in try/except so an aggregation failure never breaks the lanes list.
    trial_metrics: Dict[str, Any] | None = None
    try:
        from app.review_fix_lane_pilot import compute_lane_pilot_aggregates
        from app.capability_trial_verdict import (
            evaluate_lane_pilot_verdict,
            LANE_PILOT_CAP_NAME,
        )

        aggregates = await compute_lane_pilot_aggregates(session, project_id)
        trial_metrics = evaluate_lane_pilot_verdict(aggregates)
    except Exception:
        logger.exception(
            "Failed to compute lane pilot trial metrics for %s", project_id
        )

    # Merge stored lane_relay_comparison from capability_adoption_verdicts into
    # trial_metrics so operators see timing deltas without switching panels.
    if trial_metrics is not None:
        try:
            _verdicts_row = (
                await session.execute(
                    select(Project.capability_adoption_verdicts).where(
                        Project.id == project_id
                    )
                )
            ).scalar_one_or_none()
            if _verdicts_row:
                _cap_entry = (_verdicts_row or {}).get(LANE_PILOT_CAP_NAME) or {}
                _relay_cmp = _cap_entry.get("lane_relay_comparison")
                if _relay_cmp is not None:
                    trial_metrics["lane_relay_comparison"] = _relay_cmp
        except Exception:
            logger.exception("Failed to fetch lane_relay_comparison for %s", project_id)

    # workspace_state_counts: fleet-wide tally across ALL lanes for this project
    # (not just the paginated window) so operators can spot orphan accumulation.
    ws_count_rows = (
        await session.execute(
            select(
                ImprovementLane.workspace_state,
                func.count().label("cnt"),
            )
            .where(ImprovementLane.project_id == project_id)
            .group_by(ImprovementLane.workspace_state)
        )
    ).all()
    workspace_state_counts: Dict[str, int] = {
        (row.workspace_state or "COLD"): row.cnt for row in ws_count_rows
    }

    return {
        "lanes": rows,
        "total_returned": len(rows),
        "offset": offset,
        "limit": limit,
        "project_id": str(project_id),
        "shadow_health": shadow_health,
        "trial_metrics": trial_metrics,
        "workspace_state_counts": workspace_state_counts,
    }


# GET /api/admin/improvement-lanes/{lane_id}
# ---------------------------------------------------------------------------


@router.get("/improvement-lanes/{lane_id:uuid}")
async def get_improvement_lane_detail(
    lane_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return full detail for a single improvement lane.

    Returns identity/timeline fields, the full checkpoints[] rolling window
    (bounded at 50 entries — not all-time history), the full manifest dict,
    workspace/reservation/accounting state, and a light current_task sub-object
    when current_task_id is set (null when the lane is idle/parked).

    Returns 404 when the lane is not found or does not belong to project_id.
    """
    lane = await session.get(ImprovementLane, lane_id)
    if lane is None or lane.project_id != project_id:
        raise HTTPException(status_code=404, detail="Lane not found")

    current_task_obj: Dict[str, Any] | None = None
    if lane.current_task_id is not None:
        task_row = (
            (
                await session.execute(
                    select(Task)
                    .options(
                        load_only(
                            Task.id,
                            Task.status,
                            Task.title,
                            Task.payloads,
                            raiseload=True,
                        )
                    )
                    .where(Task.id == lane.current_task_id)
                )
            )
            .scalars()
            .one_or_none()
        )
        if task_row is not None:
            current_task_obj = {
                "task_id": str(task_row.id),
                "status": task_row.status.value
                if hasattr(task_row.status, "value")
                else task_row.status,
                "title": task_row.title,
                "task_type": task_row.first_payload.get("type"),
            }

    at_risk = (lane.phase_requeues or 0) >= (_LANE_MAX_PHASE_REQUEUES - 1)

    return {
        "id": str(lane.id),
        "title": lane.title,
        "goal": lane.goal,
        "direction_id": lane.direction_id,
        "version_number": lane.version_number,
        "status": lane.status,
        "branch_name": lane.branch_name,
        "base_branch": lane.base_branch,
        "created_at": utc_iso(lane.created_at),
        "parked_at": utc_iso(lane.parked_at),
        "last_resumed_at": utc_iso(lane.last_resumed_at),
        "retired_at": utc_iso(lane.retired_at),
        "updated_at": utc_iso(lane.updated_at),
        "last_checkpoint_sha": lane.last_checkpoint_sha,
        "last_checkpoint_phase": lane.last_checkpoint_phase,
        "checkpoint_seq": lane.checkpoint_seq,
        "phase_requeues": lane.phase_requeues,
        "at_risk": at_risk,
        "checkpoints": lane.checkpoints or [],
        "manifest": lane.manifest or {},
        "workspace_state": lane.workspace_state,
        "workspace_vm_instance_id": lane.workspace_vm_instance_id,
        "pilot_arm": lane.pilot_arm,
        "llm_input_tokens_total": lane.llm_input_tokens_total,
        "llm_output_tokens_total": lane.llm_output_tokens_total,
        "vm_cost_usd_total": round(lane.vm_cost_usd_total or 0.0, 6),
        "reserved_idle_cost_usd_total": round(
            lane.reserved_idle_cost_usd_total or 0.0, 6
        ),
        "reservation_claimed_by_fix_count": lane.reservation_claimed_by_fix_count or 0,
        "reservation_expired_before_fix_count": (
            lane.reservation_expired_before_fix_count or 0
        ),
        "reservation_force_expired_count": lane.reservation_force_expired_count or 0,
        "current_task_id": str(lane.current_task_id) if lane.current_task_id else None,
        "current_task": current_task_obj,
    }


@router.get("/lane-baselines")
async def get_lane_baselines(
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return all persisted per-version lane baselines for a project.

    Baselines are written by ``persist_version_lane_baseline`` when a
    ReleaseVersion is promoted or abandoned.  They capture lane-diagnostic
    aggregates (phase wall-clock, iteration distribution, waste-source
    classification) plus per-PR relay medians for the v10 lane-pilot comparison.

    Up to the last 20 versions are stored per project. Reads prefer the
    dedicated ``project_lane_baseline`` table and fall back to
    ``project.design_doc['lane_baselines']`` for versions absent from the table
    (before backfill or after a rollback of the dual-write).

    Returns 404 when the project is not found.  Returns an empty
    ``baselines`` list when the project exists but has no stored baselines yet.
    """
    from app.lane_baseline_snapshot import (  # noqa: PLC0415
        read_all_lane_baselines_preferring_table,
    )

    proj_doc = (
        await session.execute(
            select(Project.design_doc).where(Project.id == project_id)
        )
    ).scalar_one_or_none()

    if proj_doc is None:
        # Distinguish "project not found" from "project has null design_doc"
        exists = (
            await session.execute(select(Project.id).where(Project.id == project_id))
        ).scalar_one_or_none()
        if exists is None:
            from fastapi import HTTPException  # noqa: PLC0415

            raise HTTPException(status_code=404, detail="Project not found")

    raw_baselines = await read_all_lane_baselines_preferring_table(
        session,
        project_id,
        proj_doc if isinstance(proj_doc, dict) else None,
    )

    # Sort versions descending so the most recent appears first.
    sorted_baselines = sorted(
        raw_baselines.items(),
        key=lambda kv: int(kv[0]),
        reverse=True,
    )

    baseline_values = [v for _, v in sorted_baselines]
    return {
        "project_id": str(project_id),
        "count": len(sorted_baselines),
        "baselines": baseline_values,
        "trend": compute_lane_baseline_trend(baseline_values),
    }


# ---------------------------------------------------------------------------
# POST /api/admin/improvement-lanes/drain
# ---------------------------------------------------------------------------


@router.post("/improvement-lanes/drain")
async def drain_improvement_lane_pilot(
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Disable and drain the review-fix lane pilot for a project.

    Distinct from the raw settings toggle: this action communicates the full
    impact, requires owner authorization, stops future lane assignment, releases
    lane-held warm-pool reservations, and transitions in-flight ACTIVE/PARKED lane
    rows to the terminal KILLED state. It also records a durable KILL verdict so
    the spawn gate (is_review_fix_lane_pilot_active_for_project) keeps blocking
    new lane assignment after a runtime_config reload.

    Idempotent: calling drain on an already-drained pilot returns zero counts and
    succeeds. Returns the kill report (flag_disabled, reservations_cleared,
    lanes_killed, reason) plus the project_id.
    """
    from app.capability_trial_verdict import (  # noqa: PLC0415
        LANE_PILOT_CAP_NAME,
        record_trial_verdict,
    )
    from app.review_fix_lane_pilot import apply_lane_pilot_kill  # noqa: PLC0415
    from app.time_utils import _utcnow  # noqa: PLC0415

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    report = await apply_lane_pilot_kill(session, project_id, reason="operator_drain")

    # Record a durable KILL verdict so the spawn gate survives runtime_config
    # reload. Uses the same evidence_source and verdict shape as the automated
    # digest path so is_review_fix_lane_killed(project) returns True after reload.
    record_trial_verdict(
        project,
        LANE_PILOT_CAP_NAME,
        {
            "verdict": "KILL",
            "verdict_reason": "operator_drain",
            "evidence_source": "operator",
            "evaluated_at": _utcnow().isoformat(),
        },
    )

    await session.commit()

    return {
        "project_id": str(project_id),
        **report,
    }


# ---------------------------------------------------------------------------
# POST /api/admin/improvement-lanes/{lane_id}/kill
# ---------------------------------------------------------------------------


@router.post("/improvement-lanes/{lane_id:uuid}/kill")
async def kill_improvement_lane(
    lane_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Kill a single stuck improvement lane.

    Operator-triggered kill for an individual lane: transitions the lane to
    KILLED state and releases any warm-pool reservation so the member rejoins
    the general pool immediately. The lane must belong to the specified project.

    Idempotent: killing an already-terminal lane returns zero counts and
    succeeds. Returns the kill report (lanes_killed, reservations_cleared).
    """
    from app.review_fix_lane_pilot import kill_single_lane  # noqa: PLC0415

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    lane = await session.get(ImprovementLane, lane_id)
    if lane is None or lane.project_id != project_id:
        raise HTTPException(status_code=404, detail="Lane not found in this project")

    report = await kill_single_lane(session, lane_id)
    await session.commit()

    logger.info(
        "[LANE-PILOT] operator kill lane=%s project=%s killed=%s reservations=%s",
        lane_id,
        project_id,
        report["lanes_killed"],
        report["reservations_cleared"],
    )

    return {
        "project_id": str(project_id),
        "lane_id": str(lane_id),
        **report,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/improvement-lanes/preflight
# ---------------------------------------------------------------------------


@router.get("/improvement-lanes/preflight")
async def get_lane_pilot_preflight(
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return lane-pilot enablement readiness for a project.

    Reports each precondition as a named check: met/unmet, the blocking value
    (what is currently set), and a short fix_hint for each unmet check.  The
    overall ``ready`` field is True only when ALL checks pass.

    Checks performed (in display order):
    - warm_pool_enabled: master switch must be on.
    - project_in_scope: project must be in warm_pool_project_allowlist, or the
      allowlist must be empty (fleet-wide mode).
    - warm_pool_target_nonzero: effective IDLE target must be > 0.
    - no_kill_verdict: no durable KILL verdict on the pilot capability.
    - pilot_eligible: project must be the Blueprint self-repo.
    - pilot_flags_enabled: both improvement_lanes_enabled and
      review_fix_lane_pilot_enabled must be True.

    This endpoint is read-only and cheap: it reads runtime config (in-memory)
    plus a single project row lookup.  It does NOT poll or count pool members.
    """
    from app.warm_pool import (  # noqa: PLC0415
        is_warm_pool_enabled,
        is_project_warm_pool_scoped,
        resolve_project_warm_pool_target,
        warm_pool_project_scope,
    )
    from app.review_fix_lane_pilot import (  # noqa: PLC0415
        is_review_fix_lane_killed,
        is_pilot_eligible_project,
        LANE_PILOT_CAP_NAME,
    )
    from app.runtime_config_state import (  # noqa: PLC0415
        get_lane_pilot_mode,
        is_improvement_lanes_enabled,
        is_review_fix_lane_pilot_enabled,
    )

    # Load the project row to check KILL verdict and eligibility.
    project = (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    checks: List[Dict[str, Any]] = []

    # ── 1. Warm pool master switch ────────────────────────────────────────────
    pool_on = is_warm_pool_enabled()
    checks.append(
        {
            "name": "warm_pool_enabled",
            "met": pool_on,
            "blocking_value": str(pool_on),
            "fix_hint": (
                "Set warm_pool_enabled=true via PATCH /api/admin/settings."
                if not pool_on
                else None
            ),
        }
    )

    # ── 2. Project in warm pool scope ─────────────────────────────────────────
    in_scope = is_project_warm_pool_scoped(project_id)
    scope_list = sorted(warm_pool_project_scope())
    scope_display = (
        ", ".join(scope_list) if scope_list else "(fleet-wide — all projects)"
    )
    checks.append(
        {
            "name": "project_in_scope",
            "met": in_scope,
            "blocking_value": scope_display,
            "fix_hint": (
                f"Add {project_id} to warm_pool_project_allowlist, or clear the allowlist for fleet-wide mode."
                if not in_scope
                else None
            ),
        }
    )

    # ── 3. Non-zero effective pool target ─────────────────────────────────────
    target = resolve_project_warm_pool_target(project_id)
    checks.append(
        {
            "name": "warm_pool_target_nonzero",
            "met": target > 0,
            "blocking_value": str(target),
            "fix_hint": (
                "Set warm_pool_target_size (global) or warm_pool_target_size_by_project for this project to ≥1."
                if target == 0
                else None
            ),
        }
    )

    # ── 4. No durable KILL verdict ────────────────────────────────────────────
    killed = is_review_fix_lane_killed(project)
    kill_entry: Dict[str, Any] | None = None
    if killed:
        verdicts = getattr(project, "capability_adoption_verdicts", None) or {}
        kill_entry = verdicts.get(LANE_PILOT_CAP_NAME)
    checks.append(
        {
            "name": "no_kill_verdict",
            "met": not killed,
            "blocking_value": (
                kill_entry.get("verdict_reason", "KILL") if kill_entry else "none"
            ),
            "fix_hint": (
                "A durable KILL verdict is recorded. Clear capability_adoption_verdicts[review_fix_lane] via direct DB edit or a future admin endpoint."
                if killed
                else None
            ),
        }
    )

    # ── 5. Project is pilot-eligible (Blueprint self-repo) ───────────────────
    eligible = is_pilot_eligible_project(project)
    checks.append(
        {
            "name": "pilot_eligible",
            "met": eligible,
            "blocking_value": str(eligible),
            "fix_hint": (
                "Pilot is restricted to the Blueprint self-repo. This project is not eligible."
                if not eligible
                else None
            ),
        }
    )

    # ── 6. Pilot feature flags ────────────────────────────────────────────────
    lanes_flag = is_improvement_lanes_enabled()
    pilot_flag = is_review_fix_lane_pilot_enabled()
    flags_on = lanes_flag and pilot_flag
    checks.append(
        {
            "name": "pilot_flags_enabled",
            "met": flags_on,
            "blocking_value": f"improvement_lanes_enabled={lanes_flag}, review_fix_lane_pilot_enabled={pilot_flag}",
            "fix_hint": (
                "Enable both improvement_lanes_enabled and review_fix_lane_pilot_enabled via PATCH /api/admin/settings."
                if not flags_on
                else None
            ),
        }
    )

    ready = all(c["met"] for c in checks)

    # Surface durable verdict (ADOPT/KILL) if any.
    durable_verdict: str | None = None
    verdicts_map = getattr(project, "capability_adoption_verdicts", None) or {}
    entry = verdicts_map.get(LANE_PILOT_CAP_NAME)
    if isinstance(entry, dict):
        durable_verdict = entry.get("verdict")

    return {
        "project_id": str(project_id),
        "ready": ready,
        "checks": checks,
        "durable_verdict": durable_verdict,
        "pilot_mode": get_lane_pilot_mode(),
    }


# ---------------------------------------------------------------------------
# POST /api/admin/improvement-lanes/reclaim-orphaned
# ---------------------------------------------------------------------------


@router.post("/improvement-lanes/reclaim-orphaned")
async def reclaim_orphaned_lane_workspaces(
    dry_run: bool = Query(False),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Reconcile lane workspace state against the warm-pool registry.

    On-demand trigger for the startup recovery sweep
    (``reconcile_lane_workspaces``).  Useful when an operator observes an
    improvement lane stuck in workspace_state=WARM_RESERVED or LEASED with a
    missing/stale backing VM, stale reservation, or missing terminal owner task.
    ``dry_run=true`` reports what would change without mutating rows.

    Returns ``reclaimed`` as a compatibility alias for lanes actually cleared,
    plus structured reconciliation counts.
    """
    from app.improvement_lane_ops import reconcile_lane_workspaces

    result = await reconcile_lane_workspaces(
        session,
        dry_run=dry_run,
        project_id=project_id,
    )
    if not dry_run:
        await session.commit()

    logger.info(
        "[LANE-PILOT] operator reclaim-orphaned: %s",
        result,
    )

    return {
        **result,
        "project_id": str(project_id) if project_id is not None else None,
        "reclaimed": result["lanes_cleared"],
        "would_reclaim": result["lanes_would_clear"],
    }


# ── platform-kill metrics ────────────────────────────────────────────────────


@router.get("/platform-kill-metrics")
async def get_platform_kill_metrics(
    project_id: uuid.UUID | None = Query(None),
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Platform-killed tasks per day, broken down by class, with an honest unknown.

    The operator's reliability metric is "platform-killed tasks per day, driven
    to zero". This endpoint is the measurement: it classifies every terminal
    (FAILED / CANCELLED) project-work task in the window as a platform kill, a
    work outcome, a not-a-death, or — when the terminalizing path recorded no
    reason — ``unclassified``. Continuous Resident generations are control-plane
    audit records rather than project work, so they are excluded from this
    denominator.

    Read ``platform_kills_per_day`` together with ``classified_fraction``. A zero
    rate next to a low classified fraction means "not measured", not "no
    defects"; the two are only meaningful side by side.

    Classification reads persisted per-task ground truth only and never
    re-derives from log text — see ``app.platform_kill_metrics``.
    """
    if project_id is not None:
        await _get_user_project_with_role(
            project_id, current_user["user_id"], session, min_role="viewer"
        )

    now = _utcnow()
    cutoff = now - timedelta(days=days)

    filters = [
        Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
        non_resident_infrastructure_task_filter(),
        or_(
            Task.completed_at >= cutoff,
            and_(Task.completed_at.is_(None), Task.updated_at >= cutoff),
        ),
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
                        Task.status,
                        Task.context,
                        Task.latest_status_message,
                        Task.completed_at,
                        Task.updated_at,
                        raiseload=True,
                    )
                )
                .where(*filters)
                .order_by(Task.updated_at.desc())
                .limit(20_000)
            )
        )
        .scalars()
        .all()
    )

    result = compute_platform_kill_metrics(tasks, window_days=days, now=now)
    result["project_id"] = str(project_id) if project_id is not None else None
    return result
