"""Aggregate query helpers for compute-job monitoring.

Extracted from compute_jobs.py to keep that module under the line-count cap.
These functions are the sole aggregation layer for the operator dashboard and
share the same time-window logic as ``list_compute_jobs``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import select

from app.database import resolve_dialect_name
from app.models import ComputeJob, ComputeJobStatus, Task
from app.time_utils import _utcnow

_COMPUTE_JOB_ACTIVE_STATUSES = [
    ComputeJobStatus.QUEUED,
    ComputeJobStatus.STARTING,
    ComputeJobStatus.RUNNING,
    ComputeJobStatus.POLLING,
]


def _criterion_met_expr(session):
    if resolve_dialect_name(session) == "sqlite":
        return func.json_extract(
            Task.plan_output,
            "$.compute_result.criterion.met",
        ).label("met_value")
    return func.jsonb_extract_path_text(
        Task.plan_output,
        "compute_result",
        "criterion",
        "met",
    ).label("met_value")


def _criterion_is_met(raw_value) -> bool:
    if raw_value is True:
        return raw_value
    if isinstance(raw_value, int) and raw_value == 1:
        return True
    return raw_value == "true"


async def aggregate_compute_jobs_summary(
    session,
    *,
    project_id: uuid.UUID,
    include_finished_within: timedelta = timedelta(hours=6),
    all_terminal: bool = False,
) -> dict:
    """Return aggregate stats over the full time window (not just one page).

    Covers the same job set as ``list_compute_jobs`` but without pagination so
    both the web panel and the CLI can show identical headline metrics regardless
    of how many pages the caller has loaded.

    When ``all_terminal=True``, the time-window filter for terminal jobs is
    dropped so totals match the unbounded list instead of the windowed subset.
    """
    cutoff = _utcnow() - include_finished_within
    if all_terminal:
        terminal_filter = ComputeJob.completed_at.is_not(None)
    else:
        terminal_filter = ComputeJob.completed_at.is_not(None) & (
            ComputeJob.completed_at >= cutoff
        )
    window_filter = [
        ComputeJob.project_id == project_id,
        (ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES) | terminal_filter),
    ]

    totals_row = (
        await session.execute(
            select(
                func.count(ComputeJob.id).label("total"),
                func.coalesce(
                    func.sum(
                        func.coalesce(ComputeJob.final_cost_usd, ComputeJob.accrued_cost_usd)
                    ),
                    0.0,
                ).label("spend"),
            ).where(*window_filter)
        )
    ).one()

    status_rows = (
        await session.execute(
            select(ComputeJob.status, func.count(ComputeJob.id))
            .where(*window_filter)
            .group_by(ComputeJob.status)
        )
    ).all()
    active_statuses_set = set(_COMPUTE_JOB_ACTIVE_STATUSES)
    active_count = sum(
        cnt for status, cnt in status_rows if status in active_statuses_set
    )
    outcome_breakdown = {
        (s.value if hasattr(s, "value") else str(s)): cnt
        for s, cnt in status_rows
    }

    total = int(totals_row.total)
    return {
        "total_jobs": total,
        "active_jobs": active_count,
        "terminal_jobs": total - active_count,
        "total_spend_usd": float(totals_row.spend),
        "outcome_breakdown": outcome_breakdown,
    }


async def aggregate_criterion_summary(
    session,
    *,
    project_id: uuid.UUID,
    include_finished_within: timedelta = timedelta(hours=6),
    all_terminal: bool = False,
) -> dict:
    """Count criterion-measured and criterion-met jobs over the full time window.

    Joins ComputeJob with Task to read plan_output->compute_result->criterion->met.
    Returns the same window scope as aggregate_compute_jobs_summary so both
    figures are consistent regardless of pagination.

    Only jobs whose task has a finalized plan_output are counted; terminal jobs
    with a not-yet-finalized plan_output are inherently unquantifiable at DB level
    and are excluded (their criterion appears once the task completion gate writes
    plan_output->compute_result).

    When ``all_terminal=True``, the time-window filter for terminal jobs is
    dropped to match the unbounded list.
    """
    cutoff = _utcnow() - include_finished_within
    if all_terminal:
        terminal_filter = ComputeJob.completed_at.is_not(None)
    else:
        terminal_filter = ComputeJob.completed_at.is_not(None) & (
            ComputeJob.completed_at >= cutoff
        )
    window_filter = [
        ComputeJob.project_id == project_id,
        (ComputeJob.status.in_(_COMPUTE_JOB_ACTIVE_STATUSES) | terminal_filter),
        ComputeJob.task_id.is_not(None),
    ]
    # Postgres and SQLite expose JSON path reads through different functions.
    # A non-NULL result means the criterion was evaluated; true/false records
    # whether it was met.
    rows = (
        await session.execute(
            select(_criterion_met_expr(session))
            .select_from(ComputeJob)
            .join(Task, ComputeJob.task_id == Task.id)
            .where(*window_filter)
            .group_by(Task.id, Task.plan_output)
        )
    ).all()

    criterion_measured = sum(1 for row in rows if row.met_value is not None)
    criterion_met = sum(1 for row in rows if _criterion_is_met(row.met_value))

    # Count SUCCEEDED jobs whose criterion was measured AND met — this is the
    # only population that constitutes a clean success at the ledger level.
    # A SUCCEEDED job with met=False or no measured criterion must not appear
    # here, so this query intentionally does NOT share the GROUP BY of the
    # task-level criterion_measured/criterion_met counts above.
    honest_rows = (
        await session.execute(
            select(_criterion_met_expr(session))
            .select_from(ComputeJob)
            .join(Task, ComputeJob.task_id == Task.id)
            .where(*window_filter, ComputeJob.status == ComputeJobStatus.SUCCEEDED)
        )
    ).all()
    honest_succeeded = sum(1 for row in honest_rows if _criterion_is_met(row.met_value))

    return {
        "criterion_measured": criterion_measured,
        "criterion_met": criterion_met,
        "honest_succeeded": honest_succeeded,
    }


async def aggregate_all_compute_jobs_summary(
    session,
    *,
    project_id: uuid.UUID,
    since: datetime | None = None,
) -> dict:
    """Return aggregate stats over compute jobs for a project.

    When ``since`` is provided, all three aggregate queries are bounded to jobs
    created on or after that timestamp, limiting the scan to recent rows and
    allowing the ``ix_computejob_project_status_created`` composite index to be
    used selectively.  An additional lightweight all-time total count is returned
    as ``total_jobs_all_time`` so callers retain access to the cumulative figure.

    When ``since`` is None, the full history is scanned (original behaviour).
    """
    project_filter = [ComputeJob.project_id == project_id]
    if since is not None:
        time_filter = [ComputeJob.project_id == project_id, ComputeJob.created_at >= since]
    else:
        time_filter = project_filter

    totals_row = (
        await session.execute(
            select(
                func.count(ComputeJob.id).label("total"),
                func.coalesce(
                    func.sum(
                        func.coalesce(ComputeJob.final_cost_usd, ComputeJob.accrued_cost_usd)
                    ),
                    0.0,
                ).label("spend"),
            ).where(*time_filter)
        )
    ).one()

    status_rows = (
        await session.execute(
            select(ComputeJob.status, func.count(ComputeJob.id))
            .where(*time_filter)
            .group_by(ComputeJob.status)
        )
    ).all()
    active_statuses_set = set(_COMPUTE_JOB_ACTIVE_STATUSES)
    active_count = sum(
        cnt for status, cnt in status_rows if status in active_statuses_set
    )
    outcome_breakdown = {
        (s.value if hasattr(s, "value") else str(s)): cnt
        for s, cnt in status_rows
    }

    total = int(totals_row.total)

    criterion_rows = (
        await session.execute(
            select(_criterion_met_expr(session))
            .select_from(ComputeJob)
            .join(Task, ComputeJob.task_id == Task.id)
            .where(
                ComputeJob.project_id == project_id,
                ComputeJob.task_id.is_not(None),
                *([ComputeJob.created_at >= since] if since is not None else []),
            )
            .group_by(Task.id, Task.plan_output)
        )
    ).all()
    criterion_measured = sum(1 for row in criterion_rows if row.met_value is not None)
    criterion_met = sum(
        1 for row in criterion_rows if _criterion_is_met(row.met_value)
    )

    # Count SUCCEEDED jobs whose criterion was measured AND met — only this
    # population constitutes a clean success at the ledger level.  A SUCCEEDED
    # job with met=False or no measured criterion must not appear here.
    honest_rows = (
        await session.execute(
            select(_criterion_met_expr(session))
            .select_from(ComputeJob)
            .join(Task, ComputeJob.task_id == Task.id)
            .where(
                ComputeJob.project_id == project_id,
                ComputeJob.task_id.is_not(None),
                ComputeJob.status == ComputeJobStatus.SUCCEEDED,
                *([ComputeJob.created_at >= since] if since is not None else []),
            )
        )
    ).all()
    honest_succeeded = sum(1 for row in honest_rows if _criterion_is_met(row.met_value))

    result: dict = {
        "total_jobs": total,
        "active_jobs": active_count,
        "terminal_jobs": total - active_count,
        "total_spend_usd": float(totals_row.spend),
        "outcome_breakdown": outcome_breakdown,
        "criterion_measured": criterion_measured,
        "criterion_met": criterion_met,
        "honest_succeeded": honest_succeeded,
    }

    if since is not None:
        all_time_row = (
            await session.execute(
                select(func.count(ComputeJob.id).label("all_time_total")).where(
                    *project_filter
                )
            )
        ).one()
        result["total_jobs_all_time"] = int(all_time_row.all_time_total)

    return result
