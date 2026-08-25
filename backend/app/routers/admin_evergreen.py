"""Admin endpoints for the evergreen backlog auto-fill mechanism (d-2877ddc7).

Read/write surface over the :class:`app.models.EvergreenBacklogItem` ledger plus
the process-local fill observability counters:

* ``POST /api/admin/evergreen/backlog``      — enroll a candidate (deduped).
* ``GET  /api/admin/evergreen/backlog``       — list a project's backlog items.
* ``GET  /api/admin/evergreen/backlog-summary`` — per-status counts.
* ``GET  /api/admin/evergreen/fill-observability`` — bounded decision counters.

Admin-authenticated, following the existing project role/admin dependency
patterns (router-level ``get_current_user`` + endpoint-level ``require_admin``).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_session
from app.evergreen_backlog import (
    BACKLOG_STATUSES,
    backlog_summary,
    compute_observe_mode_verdict,
    enroll_backlog_item,
    get_fill_observability,
    get_fill_observability_from_db,
    list_backlog_items,
)
from app.time_utils import utc_iso

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/evergreen",
    tags=["admin-evergreen"],
    dependencies=[Depends(get_current_user)],
)


class _EnrollBacklogItemRequest(BaseModel):
    project_id: uuid.UUID
    finding_key: str
    title: str
    detail: str | None = None
    direction_id: str | None = None
    provenance: str = "manual"


def _serialize_item(item) -> Dict[str, Any]:
    return {
        "id": str(item.id),
        "project_id": str(item.project_id),
        "finding_key": item.finding_key,
        "title": item.title,
        "detail": item.detail,
        "status": item.status,
        "outcome": item.outcome,
        "provenance": item.provenance,
        "direction_id": item.direction_id,
        "attempts": item.attempts,
        "spawned_task_id": str(item.spawned_task_id) if item.spawned_task_id else None,
        "verified_against_sha": item.verified_against_sha,
        "created_at": utc_iso(item.created_at),
        "updated_at": utc_iso(item.updated_at),
    }


@router.post("/backlog")
async def enroll_item(
    payload: _EnrollBacklogItemRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Enroll (or idempotently re-open) an evergreen backlog candidate."""
    finding_key = (payload.finding_key or "").strip()
    if not finding_key:
        raise HTTPException(status_code=400, detail="finding_key must be non-empty")
    if not (payload.title or "").strip():
        raise HTTPException(status_code=400, detail="title must be non-empty")
    item = await enroll_backlog_item(
        session,
        project_id=payload.project_id,
        finding_key=finding_key,
        title=payload.title,
        detail=payload.detail,
        direction_id=payload.direction_id,
        provenance=payload.provenance or "manual",
    )
    if item is None:
        raise HTTPException(status_code=409, detail="could not enroll backlog item")
    await session.commit()
    await session.refresh(item)
    return {"item": _serialize_item(item)}


@router.get("/backlog")
async def list_items(
    project_id: uuid.UUID = Query(...),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """List a project's backlog items, optionally filtered by status."""
    if status is not None and status not in BACKLOG_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(sorted(BACKLOG_STATUSES))}",
        )
    items = await list_backlog_items(
        session, project_id=project_id, status=status, limit=limit
    )
    return {"items": [_serialize_item(i) for i in items]}


@router.get("/backlog-summary")
async def get_backlog_summary(
    project_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Per-status counts for a project's evergreen backlog."""
    return await backlog_summary(session, project_id=project_id)


@router.get("/fill-observability")
async def get_observability(
    window_hours: int = Query(24, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Fill-decision counters and recent decisions from the durable log.

    Aggregates EvergreenFillDecisionLog rows within the trailing window
    (default 24 h, max 168 h / 7 days).  Falls back to the process-local
    in-memory snapshot when the table has no rows in the window so fresh
    deploys still show something meaningful.
    """
    result = await get_fill_observability_from_db(session, window_hours=window_hours)
    if not result["recent"] and result["created_count"] == 0:
        return get_fill_observability()
    return result


@router.get("/observe-mode-verdict")
async def get_observe_mode_verdict(
    window_hours: int = Query(168, ge=1, le=720),
    max_fire_rate_pct: float = Query(20.0, ge=0.0, le=100.0),
    min_evaluations: int = Query(10, ge=1),
    daily_cap: int = Query(3, ge=1),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Compute a go/no-go verdict for the observe-mode → enforce-mode transition.

    Reads the durable fill-decision ledger (EvergreenFillDecisionLog) over the
    trailing *window_hours* (default 168 h / 7 days) and computes both the raw
    fire rate and the cap-adjusted fire rate (capped at *daily_cap* per UTC day).
    The verdict uses the cap-adjusted rate so consecutive idle ticks on the same
    day do not inflate the metric toward 100%.

    Verdict is one of: ``go``, ``no_go``, or ``insufficient_data``.
    """
    return await compute_observe_mode_verdict(
        session,
        window_hours=window_hours,
        max_fire_rate_pct=max_fire_rate_pct,
        min_evaluations=min_evaluations,
        daily_cap=daily_cap,
    )


@router.get("/structural-health-observability")
async def get_structural_health(
    window_hours: int = Query(24, ge=1, le=168),
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Structural-health audit observability — in-memory snapshot + durable DB aggregate.

    Surfaces the recurring resource/scalability audit's view: the largest
    measured ``task.context`` rows, the most-recent per-cycle decision, and a
    bounded ring of recent decisions (in-memory), PLUS a durable cross-deploy
    aggregate from the AuditLaneDecisionLog table (tripped_count / cycles over
    the trailing window_hours, surviving process restarts).
    """
    from app.structural_health_audit import (
        get_structural_health_observability,
        get_structural_health_observability_from_db,
    )

    snapshot = get_structural_health_observability()
    db_obs = await get_structural_health_observability_from_db(
        session, window_hours=window_hours
    )
    snapshot["observe_window_db"] = db_obs
    return snapshot


@router.get("/metrics-integrity-observability")
async def get_metrics_integrity(
    window_hours: int = Query(24, ge=1, le=168),
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Metrics-integrity sweep observability — in-memory snapshot + durable DB aggregate.

    Surfaces the standing self-audit's view: recent ticks, the most-recent tick's
    flags, and lifetime counters (flags/errors/enrolled) from the in-memory ring,
    PLUS a durable cross-deploy aggregate from the AuditLaneDecisionLog table
    (tripped_count / ticks over the trailing window_hours, surviving process restarts).
    """
    from app.metrics_integrity_sweep import (
        get_metrics_integrity_observability,
        get_metrics_integrity_observability_from_db,
    )

    snapshot = get_metrics_integrity_observability()
    db_obs = await get_metrics_integrity_observability_from_db(
        session, window_hours=window_hours
    )
    snapshot["observe_window_db"] = db_obs
    return snapshot
