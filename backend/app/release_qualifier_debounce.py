"""Debounce helpers for the auto-qualification sweep.

Extracted from ``release_qualifier_sweep.py`` so the sweep stays inside its
2150-line module cap (chronic offenders are decomposed, never cap-bumped —
see test_release_qualifier_sweep_decomposition_resolves_size_breach).

Two seams of the per-delta audit-spam fix live here:

- ``compute_automain_head_quiet_seconds`` measures how long the current
  automain HEAD has been stable via a durable per-project telemetry marker,
  so the resolver's candidate_advanced quiet window can coalesce a merge
  burst into one qualification (and one opus-tier audit pair) per settled
  tip instead of one per delta.
- ``find_live_qualifier_audit_task_id`` detects a still-running
  release_audit / release_audit_challenge lane so the sweep defers instead
  of stacking a second audit pair on top of it.

Both degrade fail-open: marker unavailability yields ``None`` (the
resolver's legacy fire-immediately behavior), never a suppressed
qualification.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Durable per-project head-stability marker for the auto-qualification quiet
# window.  Stored as ``{"head_sha": str, "first_seen_at": iso}`` in
# project_telemetry: records when the sweep first observed the current automain
# HEAD.  A new HEAD rewrites the marker (restarting the window).
_AUTOMAIN_HEAD_FIRST_SEEN_KEY = "_automain_head_first_seen"


async def compute_automain_head_quiet_seconds(
    session: AsyncSession,
    project_id: UUID,
    automain_sha: str | None,
    *,
    version_id: UUID,
    now: datetime,
) -> float | None:
    """Seconds the current automain HEAD has been stable, or None.

    Persists/refreshes the ``_automain_head_first_seen`` marker.  The
    fresh-head branch commits immediately — the caller's hold path returns
    before the fire path's commit, and an uncommitted marker would roll
    back, restarting the window every sweep and holding auto-fire forever.
    Any failure degrades to ``None`` — the resolver's legacy
    fire-immediately behavior — so telemetry unavailability can never
    suppress qualification.
    """
    if not automain_sha:
        return None
    try:
        from app.project_telemetry import read_telemetry, upsert_telemetry

        marker = await read_telemetry(
            session, project_id, _AUTOMAIN_HEAD_FIRST_SEEN_KEY
        )
        first_seen: datetime | None = None
        if isinstance(marker, dict) and marker.get("head_sha") == automain_sha:
            try:
                first_seen = datetime.fromisoformat(
                    marker.get("first_seen_at") or ""
                )
                if first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                first_seen = None
        if first_seen is not None:
            return max(0.0, (now - first_seen).total_seconds())
        # New HEAD (or unparseable marker): restart the window.
        await upsert_telemetry(
            session,
            project_id,
            _AUTOMAIN_HEAD_FIRST_SEEN_KEY,
            {"head_sha": automain_sha, "first_seen_at": now.isoformat()},
        )
        await session.commit()
        return 0.0
    except Exception:  # noqa: BLE001 — best-effort; degrade to None
        logger.warning(
            "[RELEASE_QUALIFIER] sweep: automain head-stability marker "
            "unavailable for version %s; quiet window disabled this cycle",
            version_id,
            exc_info=True,
        )
        return None


async def find_live_qualifier_audit_task_id(
    session: AsyncSession, project_id: UUID
) -> UUID | None:
    """Id of a non-terminal release_audit/release_audit_challenge task, or None.

    A live qualifier audit lane means a prior attempt's opus-tier audit pair
    is still running; auto-firing another qualification would stack a second
    pair on top of it (the per-delta audit-spam class).  Mirrors the
    infra_failure re-driver's live-audit skip.
    """
    from app.models import Task, TaskStatus

    return (
        await session.execute(
            select(Task.id)
            .where(
                Task.project_id == project_id,
                Task.status.in_(
                    [
                        TaskStatus.QUEUED,
                        TaskStatus.PROVISIONING,
                        TaskStatus.IN_PROGRESS,
                        TaskStatus.EVALUATING,
                        TaskStatus.BLOCKED,
                        TaskStatus.PARKED,
                    ]
                ),
                Task.payloads[0]["type"]
                .as_string()
                .in_(["release_audit", "release_audit_challenge"]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
