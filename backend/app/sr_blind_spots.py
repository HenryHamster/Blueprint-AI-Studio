"""Shared aggregator for project-wide self-review blind-spot themes.

Both ``GET /api/admin/projects/{project_id}/sr-blind-spots`` and the agent
task-fetch project_context injection consume the same aggregation, so the
SQL+normalization logic lives here to avoid drift between them.

NOTE: there is no cached aggregate today (unlike ``sr_catch_rate_trends``
which is precomputed into ``project.design_doc``). Callers scan up to
2000 recent COMPLETED tasks per request — acceptable for the digest
endpoint and the per-task agent fetch in current volumes, but a future
optimization could mirror the ``sr_catch_rate_trends`` cache pattern in
``playbook_learning.compute_sr_catch_rate_trends``.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.models import Task, TaskStatus

_SR_SKIP_BLIND_SPOT_MARKERS = ("self-review skipped", "self-review JSON parsing failed")

_TASK_SCAN_CAP = 2000


def is_skipped_sr_outcome(sro: Dict[str, Any] | None) -> bool:
    """A self-review outcome is a 'skip' when the agent emitted the
    bash ./done fallback envelope: verdict=='unknown' with empty
    check_results, or a blind_spots marker matching the fallback paths.
    """
    if not sro:
        return False
    blind_spots = sro.get("blind_spots") or []
    for entry in blind_spots:
        if isinstance(entry, str) and any(m in entry for m in _SR_SKIP_BLIND_SPOT_MARKERS):
            return True
    if sro.get("verdict") == "unknown" and not (sro.get("check_results") or {}):
        return True
    return False


async def _scan_blind_spot_counts(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
) -> Dict[str, int]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status == TaskStatus.COMPLETED,
            Task.created_at >= cutoff,
        )
        .options(load_only(Task.id, Task.context, Task.payloads, Task.created_at))
        .order_by(Task.created_at.desc())
        .limit(_TASK_SCAN_CAP)
    )
    result = await session.execute(stmt)
    all_tasks = result.scalars().all()

    theme_counts: Dict[str, int] = defaultdict(int)
    for t in all_tasks:
        ctx = t.context or {}
        sro = ctx.get("_self_review_outcome")
        if not sro or is_skipped_sr_outcome(sro):
            continue
        for entry in (sro.get("blind_spots") or []):
            if not isinstance(entry, str) or not entry.strip():
                continue
            theme = " ".join(entry.lower().split()[:6])
            theme_counts[theme] += 1
    return theme_counts


async def aggregate_sr_blind_spot_themes(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Return up to ``limit`` recurring blind-spot themes for the project.

    Each blind_spot string is normalized to its first 6 lowercase tokens;
    skipped/fallback SR envelopes are excluded so noise from the bash
    ./done fallback path doesn't inflate counts. Result is ``[{"theme":
    str, "count": int}, ...]`` sorted by count desc.
    """
    theme_counts = await _scan_blind_spot_counts(session, project_id, days=days)
    top = sorted(theme_counts.items(), key=lambda x: -x[1])[:limit]
    return [{"theme": t, "count": c} for t, c in top]


async def aggregate_sr_blind_spot_envelope(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    days: int,
    limit: int,
) -> Dict[str, Any]:
    """Same data as :func:`aggregate_sr_blind_spot_themes` but wrapped in
    the response envelope the digest endpoint exposes.

    Kept separate so the agent injection can use the bare list while the
    HTTP endpoint preserves its ``window_days`` / ``total_distinct_themes``
    contract.
    """
    theme_counts = await _scan_blind_spot_counts(session, project_id, days=days)
    top = sorted(theme_counts.items(), key=lambda x: -x[1])[:limit]
    return {
        "window_days": days,
        "total_distinct_themes": len(theme_counts),
        "themes": [{"theme": t, "count": c} for t, c in top],
    }
