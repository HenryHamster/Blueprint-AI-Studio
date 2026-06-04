"""Residual-case CRUD endpoints.

Operator-facing surface for the residual auto-remediation lane
(``_spawn_residual_fingerprint_remediation``): list/ignore/unignore/unsuspend
``ResidualRemediationCase`` rows and inspect emerging CODE_ERROR fingerprint
clusters.

The router is mounted as a sub-router that ``admin_digest`` includes, so the
public paths (``/api/admin/residual-fingerprints``,
``/api/admin/residual-cases``, etc.), the ``require_admin`` dependency, and the
JSON response shapes are unchanged.
"""

import uuid
from datetime import timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.auth import require_admin
from app.database import get_session, resolve_dialect_name
from app.models import (
    ResidualRemediationCase,
    ResidualRemediationCaseStatus,
    Task,
    TaskStatus,
    _utcnow,
)
from app.notification_dispatch import post_residual_fix_unsuspend_message

router = APIRouter()


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

    # ``~has_key`` compiles to the Postgres-only JSONB ``?`` (key-existence)
    # operator, which raises ``sqlite3.OperationalError`` on the SQLite smoke
    # gate. Branch on the resolved dialect and fall back to the portable
    # ``json_extract`` IS NULL form for the key-absence check on SQLite.
    if resolve_dialect_name(session) == "postgresql":
        _no_failure_category = ~Task.context.has_key("failure_category")
    else:
        _no_failure_category = func.json_extract(
            Task.context, "$.failure_category"
        ).is_(None)

    result = await session.execute(
        select(Task)
        .where(*filters, _no_failure_category)
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
