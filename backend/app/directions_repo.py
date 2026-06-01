"""Thin accessor / repository for the normalised ``direction`` table.

P2 scale work (``p2-direction-table``). Historically a project's directions
lived in the unbounded ``Project.directions`` JSONB blob, so every per-direction
mutation took the project-row lock and rewrote (+ TOAST-churned) the whole
array — ~112 access sites contended on one per-project mutex.

This module is the single place that talks to the first-class ``Direction``
table. Each direction is its own row keyed on ``(project_id, direction_key)``
(``direction_key`` is the string ``id`` the rest of the system already uses),
so a single-direction write is a single-row write — no project-row lock, no
whole-array rewrite.

Transition strategy (behaviour-preserving):
  - ``Project.directions`` remains the source of truth that every existing read
    site uses unchanged.
  - The canonical write path (``task_locking.with_directions_locked``) calls
    ``sync_directions`` after each mutation so the table is kept consistent with
    the blob (dual-write). The full direction dict round-trips in ``data``, so
    no field is lost.
  - ``upsert_direction`` / ``expire_proposals`` perform SINGLE-ROW writes for
    callers that want the lock win today without touching the blob; they are
    additive and currently used by the dual-write path's row sync.

Read-site cutovers (moving consumers off ``Project.directions`` onto
``list_directions`` / ``get_direction``) are intentionally deferred and tracked
as a follow-up — see the PR body for ``p2-direction-table``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Direction
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


def _coerce_project_id(project_id) -> uuid.UUID:
    if isinstance(project_id, uuid.UUID):
        return project_id
    return uuid.UUID(str(project_id))


def _promoted_fields(direction: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the columns we promote out of the direction dict.

    Mirrors the blob's open vocabulary — missing keys fall back to the column
    defaults so a sparse/synthetic direction still upserts cleanly.
    """
    status = direction.get("status") or "active"
    priority = direction.get("priority")
    if priority not in ("high", "medium", "low"):
        priority = priority or None
    text = direction.get("text")
    return {
        "status": str(status)[:32],
        "priority": str(priority)[:16] if priority is not None else None,
        "text": text if text else None,
    }


async def list_directions(
    session: AsyncSession, project_id
) -> List[Dict[str, Any]]:
    """Return every direction dict for a project (full ``data`` payloads).

    Ordered by ``created_at`` so callers get a stable iteration order. Returns
    the stored dicts verbatim — identical shape to ``Project.directions``
    entries — so a future read-site cutover is a drop-in replacement.
    """
    pid = _coerce_project_id(project_id)
    rows = (
        (
            await session.execute(
                select(Direction)
                .where(Direction.project_id == pid)
                .order_by(Direction.created_at, Direction.direction_key)
            )
        )
        .scalars()
        .all()
    )
    return [dict(r.data or {}) for r in rows]


async def get_direction(
    session: AsyncSession, project_id, direction_key: str
) -> Dict[str, Any] | None:
    """Return a single direction dict (or None) by its string id — single-row read."""
    pid = _coerce_project_id(project_id)
    row = (
        await session.execute(
            select(Direction).where(
                Direction.project_id == pid,
                Direction.direction_key == direction_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return dict(row.data or {})


async def upsert_direction(
    session: AsyncSession, project_id, direction: Dict[str, Any]
) -> None:
    """Insert or update one direction row — SINGLE-ROW write (no project lock).

    The direction's string ``id`` is the natural key. Callers are responsible
    for committing the session. No-op (logged) if the direction carries no id.
    """
    direction_key = direction.get("id")
    if not direction_key:
        logger.debug("[directions_repo] skip upsert: direction has no 'id' key")
        return
    pid = _coerce_project_id(project_id)
    promoted = _promoted_fields(direction)
    existing = (
        await session.execute(
            select(Direction).where(
                Direction.project_id == pid,
                Direction.direction_key == direction_key,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            Direction(
                project_id=pid,
                direction_key=str(direction_key)[:128],
                data=dict(direction),
                **promoted,
            )
        )
    else:
        existing.data = dict(direction)
        existing.status = promoted["status"]
        existing.priority = promoted["priority"]
        existing.text = promoted["text"]
        existing.updated_at = _utcnow()
        session.add(existing)


async def sync_directions(
    session: AsyncSession, project_id, directions: List[Dict[str, Any]]
) -> None:
    """Reconcile the ``direction`` table with the full post-mutation blob list.

    Called by the canonical ``with_directions_locked`` write path after each
    directions mutation so the normalised table stays consistent with the blob.
    Upserts every direction that carries an ``id`` and deletes rows whose key no
    longer appears in the list (directions are seldom hard-deleted, but admin
    paths can replace the whole array). Caller commits.

    Best-effort sync: a failure here must never break the authoritative blob
    write, so it is wrapped by the caller in a guarded try/except.
    """
    pid = _coerce_project_id(project_id)
    keys_in_blob = {
        str(d.get("id")) for d in (directions or []) if d.get("id")
    }
    for d in directions or []:
        if d.get("id"):
            await upsert_direction(session, pid, d)
    # Drop rows for directions removed from the blob (whole-array replacement).
    existing_keys = set(
        (
            await session.execute(
                select(Direction.direction_key).where(Direction.project_id == pid)
            )
        )
        .scalars()
        .all()
    )
    stale = existing_keys - keys_in_blob
    if stale:
        await session.execute(
            delete(Direction).where(
                Direction.project_id == pid,
                Direction.direction_key.in_(stale),
            )
        )


async def expire_proposals(
    session: AsyncSession, project_id, cutoff_iso: str
) -> int:
    """Expire 'proposed' directions older than ``cutoff_iso`` for one project.

    SINGLE-PROJECT, single-row-per-direction writes — does NOT take the
    project-row lock, so the cross-project sweep no longer needs to bulk-lock
    every project at once (the root cause of the 2026-05-29 lock-timeout flood).
    Returns the number of proposals expired. Caller commits.
    """
    pid = _coerce_project_id(project_id)
    rows = (
        (
            await session.execute(
                select(Direction).where(
                    Direction.project_id == pid,
                    Direction.status == "proposed",
                )
            )
        )
        .scalars()
        .all()
    )
    expired = 0
    now_iso = _utcnow().isoformat()
    for row in rows:
        data = dict(row.data or {})
        when = data.get("proposed_at") or data.get("added_at", "")
        if when < cutoff_iso:
            data["status"] = "expired"
            data["expired_at"] = now_iso
            row.data = data
            row.status = "expired"
            row.updated_at = _utcnow()
            session.add(row)
            expired += 1
    return expired
