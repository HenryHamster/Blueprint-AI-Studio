"""Residual- and regression-case CRUD endpoints.

Extracted from ``routers/admin_digest.py`` — these endpoints operate on
``ResidualRemediationCase`` and ``RegressionRemediationCase`` rows and have
nothing to do with the digest summary computation.

The router is mounted as a sub-router that ``admin_digest`` includes, so the
public paths (``/api/admin/residual-cases``, ``/api/admin/regression-cases``,
etc.), the ``require_admin`` dependency, and the JSON response shapes are
unchanged.
"""

import statistics
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.auth import require_admin
from app.database import get_session
from app.digest_metrics import (
    _compute_regression_case_summary,
    _regression_case_recurrence_count,
    _summarize_symptoms_by_layer,
    compute_cross_layer_compliance,
)
from app.models import (
    CLOSE_REASON_RECOVERED_UNATTENDED,
    ArtifactType,
    RegressionCaseStatus,
    RegressionRemediationCase,
    ResidualRemediationCase,
    ResidualRemediationCaseStatus,
    Artifact,
    Task,
    TaskStatus,
    _utcnow,
)
from app.notification_dispatch import post_residual_fix_unsuspend_message
from app.time_utils import _to_naive_utc

router = APIRouter()

_REGRESSION_STALL_THRESHOLD_HOURS = (
    4  # ACTIVE case without PR older than this → stalled
)


async def _fetch_pr_urls(
    session: AsyncSession, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    """Return map of task_id → pr_url for tasks that have a PULL_REQUEST artifact."""
    if not task_ids:
        return {}
    result = await session.execute(
        select(Artifact.task_id, Artifact.pr_url).where(
            Artifact.task_id.in_(task_ids),
            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
        )
    )
    return {row.task_id: row.pr_url for row in result}


@router.get("/residual-fingerprints")
async def get_residual_fingerprints(
    hours: int = Query(24, ge=1, le=168),
    top_n: int = Query(10, ge=1, le=50),
    project_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
):
    """Return top-N CODE_ERROR fingerprint clusters with no matching classifier.

    Surfaces emerging failure patterns — task failures that reached the default
    fallthrough in classify_failure() because no keyword bank matched. Groups
    near-duplicate messages by fingerprint so operators can see what new
    classifiers are needed next cycle.

    Excludes tasks where the agent explicitly reported a failure_category,
    since those are known failure modes rather than unclassified residuals.
    """
    from app.failure_classification import classify_failure as _cf  # noqa: PLC0415
    from app.anomaly_detection import _error_fingerprint  # noqa: PLC0415

    window_start = _utcnow() - timedelta(hours=hours)
    filters = [Task.created_at >= window_start, Task.status == TaskStatus.FAILED]
    if project_id:
        filters.append(Task.project_id == project_id)

    result = await session.execute(
        select(Task)
        .where(*filters, ~Task.context.has_key("failure_category"))
        .options(
            load_only(
                Task.id,
                Task.project_id,
                Task.latest_status_message,
                Task.context,
                Task.created_at,
                # Task.status is read by classify_failure → record_residual_fingerprint's
                # status-gate (added in fc071105). Without it the attribute access
                # triggers async lazy-load and raises MissingGreenlet under FastAPI.
                Task.status,
                Task.status_log,
            )
        )
        .limit(500)
    )
    tasks = list(result.scalars().all())

    clusters: Dict[str, Dict[str, Any]] = {}
    for task in tasks:
        if _cf(task) != "CODE_ERROR":
            continue
        msg = task.latest_status_message or ""
        if not msg:
            continue
        fp = _error_fingerprint(msg)
        entry = clusters.setdefault(
            fp,
            {
                "fingerprint": fp,
                "count": 0,
                "most_recent_at": None,
                "sample_message": msg[:300],
                "example_task_ids": [],
            },
        )
        entry["count"] += 1
        ts = task.created_at.isoformat() if task.created_at else None
        if ts and (entry["most_recent_at"] is None or ts > entry["most_recent_at"]):
            entry["most_recent_at"] = ts
        if len(entry["example_task_ids"]) < 3:
            entry["example_task_ids"].append(str(task.id))

    top = sorted(clusters.values(), key=lambda x: x["count"], reverse=True)[:top_n]

    # Bulk-lookup ResidualRemediationCase rows for the returned fingerprints.
    cases: Dict[str, Any] = {}
    top_fps = [e["fingerprint"] for e in top]
    if top_fps:
        case_filters = [ResidualRemediationCase.fingerprint.in_(top_fps)]
        if project_id:
            case_filters.append(ResidualRemediationCase.project_id == project_id)
        case_rows = await session.execute(
            select(ResidualRemediationCase)
            .where(*case_filters)
            .options(
                load_only(
                    ResidualRemediationCase.fingerprint,
                    ResidualRemediationCase.status,
                    ResidualRemediationCase.last_spawned_pr_number,
                    ResidualRemediationCase.consecutive_failure_count,
                    ResidualRemediationCase.suspended_at,
                    ResidualRemediationCase.audit_defer_count,
                    ResidualRemediationCase.audit_deferred_system_id,
                )
            )
        )
        for c in case_rows.scalars().all():
            cases[c.fingerprint] = c

    enriched = []
    for e in top:
        entry: Dict[str, Any] = {
            "fingerprint": e["fingerprint"],
            "count": e["count"],
            "most_recent_at": e["most_recent_at"],
            "sample_message": e["sample_message"],
            "example_task_ids": e["example_task_ids"],
        }
        c = cases.get(e["fingerprint"])
        if c is not None:
            entry["case_status"] = c.status
            entry["last_spawned_pr_number"] = c.last_spawned_pr_number
            entry["consecutive_failure_count"] = c.consecutive_failure_count
            entry["suspended_at"] = (
                c.suspended_at.isoformat() if c.suspended_at else None
            )
            entry["audit_defer_count"] = c.audit_defer_count
            entry["audit_deferred_system_id"] = c.audit_deferred_system_id
        enriched.append(entry)

    return {
        "window_hours": hours,
        "total_code_error_count": sum(e["count"] for e in clusters.values()),
        "residual_count": len(clusters),
        "top_residuals": enriched,
    }


@router.get("/residual-cases")
async def list_residual_cases(
    status: list[str] | None = Query(
        None, description="Filter by ResidualRemediationCaseStatus values; repeatable"
    ),
    project_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List residual remediation cases.

    Defaults to open cases (ACTIVE / REMEDIATING / SUSPENDED) so operators can
    see what's currently driving auto-remediation pressure. Pass ``status`` to
    filter — e.g. ``?status=IGNORED&status=RESOLVED`` for a closed-cases view.
    Ordered by last_seen_at desc so the most recently-active fingerprints
    surface first.
    """
    if status:
        valid = {s.value for s in ResidualRemediationCaseStatus}
        invalid = [s for s in status if s not in valid]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status values: {invalid}. Valid: {sorted(valid)}",
            )
        status_filter = list(status)
    else:
        status_filter = [
            ResidualRemediationCaseStatus.ACTIVE.value,
            ResidualRemediationCaseStatus.REMEDIATING.value,
            ResidualRemediationCaseStatus.SUSPENDED.value,
        ]

    filters = [ResidualRemediationCase.status.in_(status_filter)]
    if project_id:
        filters.append(ResidualRemediationCase.project_id == project_id)
    query = (
        select(ResidualRemediationCase)
        .where(*filters)
        .order_by(ResidualRemediationCase.last_seen_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(query)).scalars().all()
    return {
        "count": len(rows),
        "status_filter": status_filter,
        "cases": [
            {
                "id": str(c.id),
                "project_id": str(c.project_id) if c.project_id else None,
                "fingerprint": c.fingerprint,
                "status": c.status,
                "hit_count": c.hit_count,
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "last_spawned_at": c.last_spawned_at.isoformat()
                if c.last_spawned_at
                else None,
                "last_spawned_pr_number": c.last_spawned_pr_number,
                "last_spawned_task_id": str(c.last_spawned_task_id)
                if c.last_spawned_task_id
                else None,
                "consecutive_failure_count": c.consecutive_failure_count,
                "suspended_at": c.suspended_at.isoformat() if c.suspended_at else None,
                "sample_message": (
                    c.sample_messages[0] if c.sample_messages else c.sample_message
                ),
                "audit_defer_count": c.audit_defer_count,
                "audit_deferred_system_id": c.audit_deferred_system_id,
            }
            for c in rows
        ],
    }


@router.post("/residual-cases/{fingerprint}/ignore")
async def ignore_residual_case(
    fingerprint: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Mark a residual fingerprint as IGNORED for a project.

    Creates or updates the ResidualRemediationCase row for this
    (project_id, fingerprint) pair to status=IGNORED, suppressing future
    auto-remediation spawn decisions for this fingerprint.
    """
    now = _utcnow()
    result = await session.execute(
        select(ResidualRemediationCase).where(
            ResidualRemediationCase.project_id == project_id,
            ResidualRemediationCase.fingerprint == fingerprint,
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        case = ResidualRemediationCase(
            project_id=project_id,
            fingerprint=fingerprint,
            status=ResidualRemediationCaseStatus.IGNORED,
            created_at=now,
            updated_at=now,
        )
        session.add(case)
    else:
        case.status = ResidualRemediationCaseStatus.IGNORED
        case.updated_at = now
    await session.commit()
    return {
        "id": str(case.id),
        "fingerprint": fingerprint,
        "status": ResidualRemediationCaseStatus.IGNORED,
    }


@router.post("/residual-cases/{fingerprint}/unignore")
async def unignore_residual_case(
    fingerprint: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Reactivate a previously IGNORED residual fingerprint.

    Sets the ResidualRemediationCase back to ACTIVE so auto-remediation
    can resume spawning for this fingerprint. Returns 404 if no case
    exists or if the case is not currently IGNORED.
    """
    result = await session.execute(
        select(ResidualRemediationCase).where(
            ResidualRemediationCase.project_id == project_id,
            ResidualRemediationCase.fingerprint == fingerprint,
        )
    )
    case = result.scalar_one_or_none()
    if case is None or case.status != ResidualRemediationCaseStatus.IGNORED:
        raise HTTPException(
            status_code=404,
            detail="No IGNORED case found for this fingerprint and project",
        )
    case.status = ResidualRemediationCaseStatus.ACTIVE
    case.updated_at = _utcnow()
    await session.commit()
    return {
        "id": str(case.id),
        "fingerprint": fingerprint,
        "status": ResidualRemediationCaseStatus.ACTIVE,
    }


@router.post("/residual-cases/{fingerprint}/unsuspend")
async def unsuspend_residual_case(
    fingerprint: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Re-enable a SUSPENDED residual fingerprint case.

    Transitions status back to ACTIVE, clears suspended_at, and resets
    consecutive_failure_count to 0. Returns 404 if no SUSPENDED case
    exists for (project_id, fingerprint). The cooldown (last_spawned_at)
    is intentionally preserved so the case does not immediately re-spawn.
    """
    result = await session.execute(
        select(ResidualRemediationCase).where(
            ResidualRemediationCase.project_id == project_id,
            ResidualRemediationCase.fingerprint == fingerprint,
        )
    )
    case = result.scalar_one_or_none()
    if case is None or case.status != ResidualRemediationCaseStatus.SUSPENDED:
        raise HTTPException(
            status_code=404,
            detail="No SUSPENDED case found for this fingerprint and project",
        )
    case.status = ResidualRemediationCaseStatus.ACTIVE
    case.suspended_at = None
    case.consecutive_failure_count = 0
    case.updated_at = _utcnow()
    session.add(case)
    await post_residual_fix_unsuspend_message(session, project_id, fingerprint)
    await session.commit()
    return {
        "id": str(case.id),
        "fingerprint": fingerprint,
        "status": ResidualRemediationCaseStatus.ACTIVE,
        "consecutive_failure_count": 0,
        "suspended_at": None,
    }


@router.get("/regression-cases")
async def list_regression_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    status: list[str] | None = Query(
        None, description="Filter by RegressionCaseStatus values; repeatable"
    ),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List regression remediation cases for a project.

    Defaults to open cases (ACTIVE / REMEDIATING / NEEDS_MANUAL_INTERVENTION).
    Ordered by last_seen_at desc.
    """
    if status:
        valid = {s.value for s in RegressionCaseStatus}
        invalid = [s for s in status if s not in valid]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status values: {invalid}. Valid: {sorted(valid)}",
            )
        status_filter = list(status)
    else:
        status_filter = [
            RegressionCaseStatus.ACTIVE,
            RegressionCaseStatus.REMEDIATING,
            RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
        ]

    query = (
        select(RegressionRemediationCase)
        .where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.status.in_(status_filter),
        )
        .order_by(RegressionRemediationCase.last_seen_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(query)).scalars().all()
    now = _utcnow()
    stall_threshold = now - timedelta(hours=_REGRESSION_STALL_THRESHOLD_HOURS)
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_class_layer_defense_counts,
        compute_untargeted_layers,
    )

    closed_rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.CLOSED,
                )
                .order_by(RegressionRemediationCase.last_seen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    # Aggregate fix-log entries by regression_class across all open+closed cases
    # so each case row can show class-level defense counts (not just its own log).
    fix_logs_by_class: dict[str, list] = {}
    for c in list(rows) + list(closed_rows):
        bucket = fix_logs_by_class.setdefault(c.regression_class, [])
        bucket.extend(c.fingerprint_to_fix_log or [])
    return {
        "count": len(rows),
        "status_filter": status_filter,
        "summary": _compute_regression_case_summary(
            closed_rows, all_rows=list(rows) + list(closed_rows)
        ),
        "cases": [
            {
                "id": str(c.id),
                "project_id": str(c.project_id),
                "regression_class": c.regression_class,
                "status": c.status,
                "attempts": c.attempts,
                "hit_count": c.hit_count,
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "needs_manual_intervention_at": c.needs_manual_intervention_at.isoformat()
                if c.needs_manual_intervention_at
                else None,
                "last_remediation_task_id": str(c.last_remediation_task_id)
                if c.last_remediation_task_id
                else None,
                "last_remediation_pr_number": c.last_remediation_pr_number,
                "last_remediation_result": c.last_remediation_result,
                "last_failure_class": c.last_failure_class,
                "signal_snapshot_at_spawn": c.signal_snapshot_at_spawn,
                "signal_snapshot_at_merge": c.signal_snapshot_at_merge,
                "recurrence_count": _regression_case_recurrence_count(c),
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "fingerprint_to_fix_log": c.fingerprint_to_fix_log,
                "sample_message": c.sample_message,
                "signal_first_crossed_at": c.signal_first_crossed_at.isoformat()
                if c.signal_first_crossed_at
                else None,
                "escalation_reason": c.escalation_reason,
                "stalled": c.status == RegressionCaseStatus.ACTIVE
                and c.last_remediation_pr_number is None
                and c.first_seen_at < stall_threshold,
                "layer_summary": _summarize_symptoms_by_layer(
                    c.symptoms_by_layer,
                    now=now,
                    fingerprint_to_fix_log=c.fingerprint_to_fix_log,
                ),
                "untargeted_layers": compute_untargeted_layers(
                    fix_logs_by_class.get(c.regression_class)
                ),
                "layer_defense_counts_by_class": compute_class_layer_defense_counts(
                    fix_logs_by_class.get(c.regression_class)
                ),
            }
            for c in rows
        ],
    }


@router.get("/regression-cases/layer-defense-density")
async def regression_cases_layer_defense_density(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Per-canonical-layer defense activity over the last ``days`` days.

    Loads every ``RegressionRemediationCase`` for the project, walks each
    case's ``fingerprint_to_fix_log`` entries, and returns one row per
    canonical infrastructure layer (CI_WORKFLOW, AGENT_VM, AGENT_VERB,
    BASELINE_GATE, REVIEWER_PASS, VIEWPORT) — including layers with zero
    defenses — so the UI can render a stable six-row table.
    """
    from app.pipeline_regression_dispatcher import (
        _compute_layer_fragility_stats,
        compute_layer_defense_density,
    )

    lookback_cutoff = datetime.utcnow() - timedelta(days=days)
    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.updated_at >= lookback_cutoff,
                )
                .order_by(RegressionRemediationCase.updated_at.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    pairs: list[tuple[str, dict[str, Any]]] = []
    for case in cases:
        for entry in case.fingerprint_to_fix_log or []:
            pairs.append((str(case.id), entry))
    layers = compute_layer_defense_density(pairs, lookback_days=days)
    all_fix_logs = [entry for _, entry in pairs]
    fragility = _compute_layer_fragility_stats(all_fix_logs, lookback_days=days)
    for row in layers:
        frag = fragility.get(row["layer"], {})
        row["avg_hold_days"] = frag.get("avg_hold_days")
        row["still_holding_count"] = frag.get("still_holding_count", 0)
        row["held_gt_7d_count"] = frag.get("held_gt_7d_count", 0)
        row["retripped_within_48h_count"] = frag.get("retripped_within_48h_count", 0)
        row["single_layer_fix_count"] = frag.get("single_layer_fix_count", 0)
        row["single_layer_avg_hold_days"] = frag.get("single_layer_avg_hold_days")
        row["multi_layer_fix_count"] = frag.get("multi_layer_fix_count", 0)
        row["multi_layer_avg_hold_days"] = frag.get("multi_layer_avg_hold_days")
    return {
        "days": days,
        "generated_at": _utcnow().isoformat(),
        "layers": layers,
    }


@router.post("/regression-cases/{regression_class}/clear-manual-intervention")
async def clear_regression_manual_intervention(
    regression_class: str,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Clear NEEDS_MANUAL_INTERVENTION on a RegressionRemediationCase.

    Transitions status back to ACTIVE and clears needs_manual_intervention_at
    so the dispatcher can resume spawning. Returns 404 if no case exists or
    if the case is not currently NEEDS_MANUAL_INTERVENTION. Cooldown / last
    timestamps are preserved so the next tick honors the existing window
    rather than immediately re-spawning.
    """
    result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.regression_class == regression_class,
        )
    )
    case = result.scalar_one_or_none()
    if case is None or case.status != RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION:
        raise HTTPException(
            status_code=404,
            detail="No NEEDS_MANUAL_INTERVENTION case found for this regression_class and project",
        )
    case.status = RegressionCaseStatus.ACTIVE
    case.needs_manual_intervention_at = None
    # Reset the durable recovery clock so a re-escalated case does not pick
    # up a stale healthy_since and auto-close on the first tick, bypassing
    # the stable window.  The case row's signal_first_seen_at /
    # signal_first_crossed_at columns are intentionally preserved so MTTD on
    # any future spawn reflects the original detection latency.
    case.signal_healthy_since_at = None
    case.updated_at = _utcnow()
    session.add(case)
    await session.commit()
    return {
        "id": str(case.id),
        "regression_class": regression_class,
        "status": RegressionCaseStatus.ACTIVE,
        "attempts": case.attempts,
        "needs_manual_intervention_at": None,
    }


@router.post("/regression-cases/{case_id}/acknowledge")
async def acknowledge_regression_case(
    case_id: uuid.UUID,
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Stamp operator_acknowledged_at on a RegressionRemediationCase.

    Idempotent: if the case is already acknowledged the existing timestamp is
    returned unchanged. Returns 404 if no matching case exists for this project.
    """
    result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.id == case_id,
            RegressionRemediationCase.project_id == project_id,
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        raise HTTPException(status_code=404, detail="Regression case not found")
    if case.operator_acknowledged_at is None:
        case.operator_acknowledged_at = _utcnow()
        case.updated_at = _utcnow()
        session.add(case)
        await session.commit()
    return {
        "id": str(case.id),
        "operator_acknowledged_at": case.operator_acknowledged_at.isoformat(),
    }


@router.post("/regression-cases/close")
async def close_regression_case(
    regression_class: str = Query(
        ...,
        description=(
            "Full regression_class string. Passed as a query parameter (not a "
            "path segment) because regression_class values may contain slashes "
            "or other URL-reserved characters."
        ),
    ),
    project_id: uuid.UUID = Query(...),
    reason: str | None = Query(
        None, description="Optional human-readable reason for the close"
    ),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Operator-close a RegressionRemediationCase from any non-CLOSED status.

    Used to clear cases that became noise — e.g. spawns triggered by a signal
    pattern that has since been gated out at the source (and so will never
    auto-recover via run_recovery_once, which only handles MERGED→CLOSED).

    Idempotent: returns 200 with status=CLOSED if the case was already CLOSED.
    Returns 404 if no row exists for (project_id, regression_class).

    regression_class is a query parameter rather than a path segment so values
    containing slashes (e.g. ``sr_blind_spot:theme:agent/src/prompt.ts ...``)
    don't fragment FastAPI's path routing.
    """
    result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == project_id,
            RegressionRemediationCase.regression_class == regression_class,
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        raise HTTPException(
            status_code=404,
            detail="No regression case found for this regression_class and project",
        )
    prior_status = case.status
    if prior_status != RegressionCaseStatus.CLOSED:
        case.status = RegressionCaseStatus.CLOSED
        case.last_remediation_result = "operator_closed"
        case.needs_manual_intervention_at = None
        # Drop the durable recovery clock so a future re-escalation on the same
        # row does not pick up a stale healthy_since and auto-close on the first
        # tick, bypassing the stable window. signal_first_seen_at /
        # signal_first_crossed_at are intentionally preserved so a future spawn
        # reflects the original detection latency.
        case.signal_healthy_since_at = None
        case.updated_at = _utcnow()
        case.fingerprint_to_fix_log = list(case.fingerprint_to_fix_log or []) + [
            {
                "fingerprint": regression_class,
                "operator_closed_at": _utcnow().isoformat(),
                "operator_close_reason": reason,
                "prior_status": prior_status,
            }
        ]
        session.add(case)
        await session.commit()

    return {
        "id": str(case.id),
        "regression_class": regression_class,
        "status": case.status,
        "prior_status": prior_status,
        "attempts": case.attempts,
    }


@router.get("/regression-cases/manual-intervention")
async def list_regression_manual_intervention_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List regression cases escalated to manual intervention."""
    now = _utcnow()
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_untargeted_layers,
    )

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status
                    == RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
                )
                .order_by(RegressionRemediationCase.last_seen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    pr_urls = await _fetch_pr_urls(
        session,
        [c.last_remediation_task_id for c in rows if c.last_remediation_task_id],
    )
    return {
        "count": len(rows),
        "cases": [
            {
                "id": str(c.id),
                "project_id": str(c.project_id),
                "regression_class": c.regression_class,
                "signal_name": c.regression_class.replace("_", " "),
                "status": c.status,
                "attempts": c.attempts,
                "last_remediation_pr_number": c.last_remediation_pr_number,
                "last_remediation_pr_url": pr_urls.get(c.last_remediation_task_id),
                "last_failure_class": c.last_failure_class,
                "signal_snapshot_at_spawn": c.signal_snapshot_at_spawn,
                "signal_snapshot_at_merge": c.signal_snapshot_at_merge,
                "recurrence_count": _regression_case_recurrence_count(c),
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "fingerprint_to_fix_log": c.fingerprint_to_fix_log,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "needs_manual_intervention_at": c.needs_manual_intervention_at.isoformat()
                if c.needs_manual_intervention_at
                else None,
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "signal_first_crossed_at": c.signal_first_crossed_at.isoformat()
                if c.signal_first_crossed_at
                else None,
                "investigate_spawned_at": c.investigate_spawned_at.isoformat()
                if c.investigate_spawned_at
                else None,
                "time_to_investigate_sec": c.time_to_investigate_sec,
                "escalation_reason": c.escalation_reason,
                "operator_acknowledged_at": c.operator_acknowledged_at.isoformat()
                if c.operator_acknowledged_at
                else None,
                "layer_summary": _summarize_symptoms_by_layer(
                    c.symptoms_by_layer,
                    now=now,
                    fingerprint_to_fix_log=c.fingerprint_to_fix_log,
                ),
                "untargeted_layers": compute_untargeted_layers(
                    c.fingerprint_to_fix_log
                ),
            }
            for c in rows
        ],
    }


@router.get("/regression-cases/auto-recovered")
async def list_regression_auto_recovered_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List regression cases auto-closed by the watchdog after unattended recovery.

    Mirrors the response shape of /regression-cases/manual-intervention so the
    admin UI can render auto-recovered cases alongside outstanding escalations.
    Adds a ``recovered_at`` field pulled from the latest ``recovered_unattended``
    log entry and an ``updated_at`` field so operators can sort by close time.
    """
    now = _utcnow()
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_untargeted_layers,
    )

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.CLOSED,
                    RegressionRemediationCase.last_remediation_result
                    == CLOSE_REASON_RECOVERED_UNATTENDED,
                )
                .order_by(RegressionRemediationCase.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    pr_urls = await _fetch_pr_urls(
        session,
        [c.last_remediation_task_id for c in rows if c.last_remediation_task_id],
    )
    cases = []
    for c in rows:
        recovered_at = None
        for entry in reversed(c.fingerprint_to_fix_log or []):
            if (
                isinstance(entry, dict)
                and entry.get("reason") == CLOSE_REASON_RECOVERED_UNATTENDED
            ):
                recovered_at = entry.get("recovered_at")
                break
        cases.append(
            {
                "id": str(c.id),
                "project_id": str(c.project_id),
                "regression_class": c.regression_class,
                "signal_name": c.regression_class.replace("_", " "),
                "status": c.status,
                "attempts": c.attempts,
                "last_remediation_pr_number": c.last_remediation_pr_number,
                "last_remediation_pr_url": pr_urls.get(c.last_remediation_task_id),
                "last_failure_class": c.last_failure_class,
                "signal_snapshot_at_spawn": c.signal_snapshot_at_spawn,
                "signal_snapshot_at_merge": c.signal_snapshot_at_merge,
                "fingerprint_to_fix_log": c.fingerprint_to_fix_log,
                "recurrence_count": _regression_case_recurrence_count(c),
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "needs_manual_intervention_at": c.needs_manual_intervention_at.isoformat()
                if c.needs_manual_intervention_at
                else None,
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "escalation_reason": c.escalation_reason,
                "recovered_at": recovered_at,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                "layer_summary": _summarize_symptoms_by_layer(
                    c.symptoms_by_layer,
                    now=now,
                    fingerprint_to_fix_log=c.fingerprint_to_fix_log,
                ),
                "untargeted_layers": compute_untargeted_layers(
                    c.fingerprint_to_fix_log
                ),
            }
        )
    return {"count": len(cases), "cases": cases}


@router.get("/regression-cases/stalled")
async def list_regression_stalled_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List regression cases stalled without PR (ACTIVE, no PR, and older than threshold)."""
    now = _utcnow()
    stall_threshold = now - timedelta(hours=_REGRESSION_STALL_THRESHOLD_HOURS)
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.ACTIVE,
                    RegressionRemediationCase.last_remediation_pr_number.is_(None),
                    RegressionRemediationCase.first_seen_at < stall_threshold,
                )
                .order_by(RegressionRemediationCase.first_seen_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "cases": [
            {
                "id": str(c.id),
                "project_id": str(c.project_id),
                "regression_class": c.regression_class,
                "signal_name": c.regression_class.replace("_", " "),
                "status": c.status,
                "attempts": c.attempts,
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
                "hit_count": c.hit_count,
            }
            for c in rows
        ],
    }


@router.get("/regression-cases/recently-closed")
async def list_recently_closed_regression_cases(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    limit: int = Query(5, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """List recently closed regression cases (autonomous wins)."""
    now = _utcnow()
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    RegressionRemediationCase.status == RegressionCaseStatus.CLOSED,
                )
                .order_by(RegressionRemediationCase.last_seen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        _extract_recurrence_cycles,
        compute_untargeted_layers,
    )

    cases = []
    for c in rows:
        fix_pr_number = None
        recovered_at = None
        merged_at = None
        latest_merged_at = None
        hold_days_history = []

        if c.fingerprint_to_fix_log:
            for entry in c.fingerprint_to_fix_log:
                if "fix_pr_number" in entry:
                    fix_pr_number = entry["fix_pr_number"]
                if entry.get("merged_at"):
                    latest_merged_at = entry["merged_at"]
                if "recovered_at" in entry:
                    recovered_at = entry["recovered_at"]
                    # Pair this recovery with the latest merge that preceded it,
                    # mirroring _compute_regression_case_summary (lines 940–966).
                    merged_at = latest_merged_at

        # Pair merged_at/recovered_at across separate fix-log entries
        # (production shape: mark_merged writes one entry, the recovery scan
        # writes a second with recovered_at — see pipeline_regression_dispatcher
        # ._extract_recurrence_cycles).
        for cycle in _extract_recurrence_cycles(c):
            merged_dt = cycle.get("merged_at")
            recovered_dt = cycle.get("recovered_at")
            if not (
                isinstance(merged_dt, datetime) and isinstance(recovered_dt, datetime)
            ):
                continue
            merged_naive = _to_naive_utc(merged_dt)
            recovered_naive = _to_naive_utc(recovered_dt)
            hold_duration_seconds = (recovered_naive - merged_naive).total_seconds()
            hold_days = hold_duration_seconds / (24 * 3600)
            hold_days_history.append(round(hold_days, 2))

        is_fragile = any(hold_days < 2.0 for hold_days in hold_days_history)

        time_to_recover_sec = None
        if recovered_at and c.first_seen_at:
            try:
                recovered_dt = datetime.fromisoformat(
                    recovered_at.replace("Z", "+00:00")
                )
                # first_seen_at is stored as NaiveUTCDateTime; normalize both sides.
                recovered_naive = _to_naive_utc(recovered_dt)
                first_seen_naive = _to_naive_utc(c.first_seen_at)
                time_to_recover_sec = int(
                    (recovered_naive - first_seen_naive).total_seconds()
                )
            except (ValueError, TypeError):
                pass

        cases.append(
            {
                "id": str(c.id),
                "regression_class": c.regression_class,
                "fix_pr_number": fix_pr_number,
                "merged_at": merged_at,
                "recovered_at": recovered_at,
                "recurrence_count": _regression_case_recurrence_count(c),
                "first_seen_at": c.first_seen_at.isoformat()
                if c.first_seen_at
                else None,
                "signal_first_crossed_at": c.signal_first_crossed_at.isoformat()
                if c.signal_first_crossed_at
                else None,
                "signal_first_seen_at": c.signal_first_seen_at.isoformat()
                if c.signal_first_seen_at
                else None,
                "time_to_detect_sec": c.time_to_detect_sec,
                "time_to_recover_sec": time_to_recover_sec,
                "investigate_spawned_at": c.investigate_spawned_at.isoformat()
                if c.investigate_spawned_at
                else None,
                "time_to_investigate_sec": c.time_to_investigate_sec,
                "hold_days_history": hold_days_history,
                "is_fragile": is_fragile,
                "layer_summary": _summarize_symptoms_by_layer(
                    c.symptoms_by_layer,
                    now=now,
                    fingerprint_to_fix_log=c.fingerprint_to_fix_log,
                ),
                "untargeted_layers": compute_untargeted_layers(
                    c.fingerprint_to_fix_log
                ),
            }
        )

    return {
        "count": len(cases),
        "cases": cases,
    }


@router.get("/regression-cases/cross-layer-compliance")
async def get_cross_layer_compliance(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    window_days: int = Query(30, ge=1, le=90, description="Rolling window in days"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Cross-layer escalation compliance metric over a rolling window.

    For fix-log entries produced by cross-layer-escalated investigations,
    computes how often the resulting fix actually targeted >=2 distinct
    canonical infrastructure layers (compliant) vs. only 1 or 0 (non-compliant).
    Entries still awaiting the layers_targeted backfill are excluded from
    both numerator and denominator and reported in awaiting_backfill_count.
    """
    cutoff = _utcnow() - timedelta(days=window_days)
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return compute_cross_layer_compliance(rows, cutoff)


@router.get("/regression-cases/first-attempt-compliance-trend")
async def get_first_attempt_compliance_trend(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    num_windows: int = Query(6, ge=1, le=12, description="Number of rolling windows"),
    window_days: int = Query(
        7, ge=1, le=30, description="Width of each window in days"
    ),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """First-attempt cross-layer compliance trend over rolling weekly windows.

    Distinguishes whether the original meta-investigation produced a >=2-layer
    defense (first_attempt_compliant) vs. only the corrective compliance-followup
    did (followup_rescued). Cases awaiting layers_targeted backfill are excluded
    from the rate denominator and reported in awaiting_backfill.
    """
    from app.pipeline_regression_dispatcher import (  # noqa: PLC0415
        compute_first_attempt_compliance_trend,
    )

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return compute_first_attempt_compliance_trend(
        list(rows), num_windows=num_windows, window_days=window_days
    )


@router.get("/regression-class-kpis")
async def get_regression_class_kpis(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    window_hours: int = Query(168, ge=1, le=720, description="Rolling window in hours"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Per-class KPI snapshot from RegressionRemediationCase records.

    Returns cases_opened, cases_closed, dispatch_attempt_count,
    remediation_success_rate, mean_time_to_detect_seconds, and
    mean_time_to_remediate_seconds grouped by regression_class.
    cases_opened/closed are scoped to the window; timing stats use all
    available rows for the class (more data = better mean estimate).
    """
    cutoff = _utcnow() - timedelta(hours=window_hours)
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )

    by_class: Dict[str, Any] = {}
    for case in rows:
        cls = case.regression_class
        b = by_class.setdefault(
            cls, {"opened": 0, "closed": 0, "attempts": 0, "mttd": [], "mttr": []}
        )
        first = case.first_seen_at and _to_naive_utc(case.first_seen_at)
        if first and first >= cutoff:
            b["opened"] += 1
        if case.status in (RegressionCaseStatus.CLOSED, RegressionCaseStatus.MERGED):
            last = case.last_seen_at and _to_naive_utc(case.last_seen_at)
            if last and last >= cutoff:
                b["closed"] += 1
        b["attempts"] += case.attempts or 0
        if case.time_to_detect_sec is not None:
            b["mttd"].append(case.time_to_detect_sec)
        if case.fingerprint_to_fix_log and first:
            ep_start = first
            latest_merged_ep: Optional[datetime] = None
            for entry in case.fingerprint_to_fix_log:
                if entry.get("merged_at"):
                    try:
                        merged = _to_naive_utc(
                            datetime.fromisoformat(
                                str(entry["merged_at"]).replace("Z", "+00:00")
                            )
                        )
                        b["mttr"].append((merged - ep_start).total_seconds())
                        latest_merged_ep = merged
                    except (ValueError, TypeError):
                        pass
                if entry.get("recovered_at") and latest_merged_ep is not None:
                    try:
                        ep_start = _to_naive_utc(
                            datetime.fromisoformat(
                                str(entry["recovered_at"]).replace("Z", "+00:00")
                            )
                        )
                        latest_merged_ep = None
                    except (ValueError, TypeError):
                        pass

    classes = [
        {
            "regression_class": cls,
            "cases_opened": b["opened"],
            "cases_closed": b["closed"],
            "dispatch_attempt_count": b["attempts"],
            "remediation_success_rate": round(b["closed"] / b["attempts"], 3)
            if b["attempts"]
            else None,
            "mean_time_to_detect_seconds": round(statistics.mean(b["mttd"]), 1)
            if b["mttd"]
            else None,
            "mean_time_to_remediate_seconds": round(statistics.mean(b["mttr"]), 1)
            if b["mttr"]
            else None,
        }
        for cls, b in sorted(by_class.items())
    ]
    return {
        "project_id": str(project_id),
        "window_hours": window_hours,
        "class_count": len(classes),
        "classes": classes,
    }


@router.get("/regression-cases/trend")
async def get_regression_case_trend(
    project_id: uuid.UUID = Query(..., description="Project to query (required)"),
    days: int = Query(30, ge=7, le=90, description="Rolling window in days"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),  # noqa: ARG001
) -> Dict[str, Any]:
    """Daily opened-vs-closed trend for regression cases over the last N days.

    opened: cases whose first_seen_at falls in the day bucket.
    closed: cases whose updated_at falls in the day bucket AND status is CLOSED or MERGED.
    Returns one entry per day plus a per-class breakdown for sparkline rendering.
    """
    cutoff = _utcnow() - timedelta(days=days)
    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase)
                .where(
                    RegressionRemediationCase.project_id == project_id,
                    or_(
                        RegressionRemediationCase.first_seen_at >= cutoff,
                        RegressionRemediationCase.updated_at >= cutoff,
                    ),
                )
                .limit(5000)
            )
        )
        .scalars()
        .all()
    )

    now = _utcnow()
    # Pre-fill all day buckets so the frontend gets a contiguous series.
    buckets: Dict[str, Dict[str, Any]] = {
        (now - timedelta(days=days - 1 - d)).strftime("%Y-%m-%d"): {
            "date": (now - timedelta(days=days - 1 - d)).strftime("%Y-%m-%d"),
            "opened": 0,
            "closed": 0,
        }
        for d in range(days)
    }
    by_class: Dict[str, Dict[str, Dict[str, int]]] = {}

    closed_statuses = {RegressionCaseStatus.CLOSED, RegressionCaseStatus.MERGED}
    for case in rows:
        cls = case.regression_class
        if case.first_seen_at:
            day = _to_naive_utc(case.first_seen_at).strftime("%Y-%m-%d")
            if day in buckets:
                buckets[day]["opened"] += 1
                by_class.setdefault(cls, {}).setdefault(
                    day, {"opened": 0, "closed": 0}
                )["opened"] += 1
        if case.updated_at and case.status in closed_statuses:
            day = _to_naive_utc(case.updated_at).strftime("%Y-%m-%d")
            if day in buckets:
                buckets[day]["closed"] += 1
                by_class.setdefault(cls, {}).setdefault(
                    day, {"opened": 0, "closed": 0}
                )["closed"] += 1

    daily = sorted(buckets.values(), key=lambda b: b["date"])
    class_trends = {
        cls: [
            {
                "date": b["date"],
                **by_class[cls].get(b["date"], {"opened": 0, "closed": 0}),
            }
            for b in daily
        ]
        for cls in sorted(by_class)
    }
    return {
        "project_id": str(project_id),
        "days": days,
        "daily": daily,
        "by_class": class_trends,
    }


@router.post("/residual-fingerprints/clear")
async def clear_residual_fingerprints(
    current_user: dict = Depends(require_admin),  # noqa: ARG001
):
    """Wipe the in-memory residual fingerprint store.

    Used when the store is polluted (bad fingerprints from a prior
    misclassification regime) and needs to be reset. Returns the count
    of fingerprints removed. Does NOT touch the DB — only clears what's
    tracked in-memory for auto-remediation spawn decisions.
    """
    from app.failure_classification import _residual_store, _residual_seen_task_ids  # noqa: PLC0415

    count = len(_residual_store)
    _residual_store.clear()
    _residual_seen_task_ids.clear()
    return {"cleared": count}
