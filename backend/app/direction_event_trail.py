"""Best-effort event trail writer for direction lifecycle transitions.

Called as a side-effect inside ``directions_repo.upsert_direction`` and
``sync_directions`` so every write path (lifecycle auto-pauses, strategic ops,
user PATCH, staged activation, whole-array pruning) emits append-only event
rows without touching any in-flight caller code.

The public helpers are:

  ``maybe_record_direction_event(session, project_id, direction_dict, prior_row)``
      Called after fetching ``prior_row`` inside ``upsert_direction``.  Emits a
      ``"created"`` event when ``prior_row`` is ``None``, or a
      ``"status_changed"`` event when the status in ``direction_dict`` differs
      from ``prior_row.status``.  No-ops if the status is unchanged.

  ``record_direction_pruned(session, project_id, direction_key, last_data)``
      Emits a ``"pruned"`` event for a row that ``sync_directions`` is about to
      hard-delete from the table.

Both helpers swallow all exceptions (``log.warning``) so a transient write
failure can never break the authoritative direction upsert or sync.

Retention: the ``DirectionEvent`` table is intentionally append-only with no
GC/TTL sweep. Growth is bounded to GENUINE lifecycle transitions — an
unchanged-status upsert no-ops (see ``maybe_record_direction_event``), so a
project accretes rows only proportional to real create/status_changed/pruned
events, which is slow even on long-lived projects. Rows are removed with their
project via the ``project`` FK CASCADE. A dedicated retention subsystem is
deliberately out of scope here; ``snapshot_data`` is stored in full (not
size-capped) BECAUSE ``latest_direction_snapshot`` reconstructs the whole
direction dict from it after the live row is pruned, so trimming snapshots would
break that recovery path. Revisit only if event-row volume becomes a measured
storage/query problem.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DirectionEvent
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

_UNSET: object = object()  # sentinel for unset prior_status_snapshot

# Maximum length for the captured reason string.
_REASON_MAX = 512


def _extract_reason(direction: Dict[str, Any], prior_status: str | None, new_status: str | None) -> str | None:
    """Best-effort extraction of a human-readable change reason from the dict."""
    # paused_reason is the canonical field for pause-family transitions.
    if direction.get("paused_reason"):
        return str(direction["paused_reason"])[:_REASON_MAX]
    # retirement_reason / retired_reason for terminal retirements.
    for key in ("retirement_reason", "retired_reason", "retire_reason"):
        if direction.get(key):
            return str(direction[key])[:_REASON_MAX]
    # Fall back to the last note's text as the most recent change annotation.
    notes = direction.get("notes") or []
    if notes and isinstance(notes[-1], dict):
        return str(notes[-1].get("text", ""))[:_REASON_MAX] or None
    return None


def _extract_actor(direction: Dict[str, Any]) -> str | None:
    for key in ("retired_by", "paused_by", "actor", "created_by"):
        if direction.get(key):
            return str(direction[key])[:64]
    return None


async def maybe_record_direction_event(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction: Dict[str, Any],
    prior_row: Any,  # Direction ORM row or None
    prior_status_snapshot: Any = _UNSET,  # pre-mutation status override; pass when prior_row.status was already mutated
) -> None:
    """Emit a created or status_changed event row for a direction upsert.

    No-ops (best-effort log) on any exception so the caller's transaction is
    never broken by event-write failures.

    ``prior_status_snapshot`` should be passed by ``upsert_direction`` to
    supply the status value captured before the row was mutated.
    """
    try:
        new_status = (direction.get("status") or "active")[:32]
        if prior_row is None:
            event_kind = "created"
            prior_status = None
        else:
            if prior_status_snapshot is not _UNSET:
                prior_status = (prior_status_snapshot or "")[:32]
            else:
                prior_status = (prior_row.status or "")[:32]
            if prior_status == new_status:
                return  # unchanged — nothing to record
            event_kind = "status_changed"

        reason = _extract_reason(direction, prior_status, new_status)
        actor = _extract_actor(direction)
        session.add(
            DirectionEvent(
                project_id=project_id,
                direction_key=str(direction.get("id", ""))[:128],
                event_kind=event_kind,
                prior_status=prior_status,
                new_status=new_status,
                actor=actor,
                reason=reason,
                snapshot_data=dict(direction),
                recorded_at=_utcnow(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[direction_event_trail] failed to record %s event for %s: %s",
            "created" if prior_row is None else "status_changed",
            direction.get("id"),
            exc,
        )


async def latest_direction_snapshot(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_key: str,
) -> Dict[str, Any] | None:
    """Return the most recent non-null ``snapshot_data`` for a direction.

    Reads the append-only ``DirectionEvent`` trail to reconstruct the full
    direction dict (text, paths, gap_id, criterion_keys, context, ...) even after
    the row was pruned from ``project.directions`` by a lifecycle sweep. Every
    created / status_changed / pruned event carries a full snapshot, so the latest
    one is the direction's last-known good shape.

    Returns ``None`` when no event with a snapshot exists for this key.
    """
    from sqlalchemy import select  # local import: keep module import surface small

    snapshot = (
        await session.execute(
            select(DirectionEvent.snapshot_data)
            .where(
                DirectionEvent.project_id == project_id,
                DirectionEvent.direction_key == str(direction_key)[:128],
                DirectionEvent.snapshot_data.isnot(None),
            )
            .order_by(DirectionEvent.recorded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not isinstance(snapshot, dict):
        return None
    return dict(snapshot)


async def record_direction_pruned(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_key: str,
    last_data: Dict[str, Any] | None,
) -> None:
    """Emit a pruned event for a direction row about to be hard-deleted."""
    try:
        prior_status = None
        if last_data:
            prior_status = (last_data.get("status") or "")[:32] or None
        session.add(
            DirectionEvent(
                project_id=project_id,
                direction_key=str(direction_key)[:128],
                event_kind="pruned",
                prior_status=prior_status,
                new_status=None,
                actor=None,
                reason=None,
                snapshot_data=dict(last_data) if last_data else None,
                recorded_at=_utcnow(),
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[direction_event_trail] failed to record pruned event for %s: %s",
            direction_key,
            exc,
        )


# Maximum text length when backfilling; matches _RETIRED_OUTCOME_CHAR_CAP in
# strategic_completion.py so repaired entries stay in the same shape as new ones.
_RETIRED_TEXT_CHAR_CAP = 200


async def repair_retired_direction_memory_texts(
    session: AsyncSession,
    project_id: uuid.UUID,
    vision: dict,
) -> bool:
    """Backfill the ``text`` field in legacy vision memory entries that only have
    ``text_tokens`` (pre-#16297 shape where ``text`` was absent or empty).

    Reads each missing-text entry's direction key from the ``DirectionEvent``
    trail via ``latest_direction_snapshot`` and patches ``entry["text"]``
    in-place on the vision dict.

    Returns True if any entry was patched (caller should call
    ``flag_modified(project, "vision")`` + ``session.add(project)`` to persist),
    False when no entries needed repair.
    """
    entries = vision.get("_recently_retired_directions") or []
    patched = False
    for entry in entries:
        if entry.get("text") or not entry.get("text_tokens"):
            # Already has readable text, or nothing useful to fall back from.
            continue
        direction_key = entry.get("id")
        if not direction_key:
            continue
        snapshot = await latest_direction_snapshot(session, project_id, direction_key)
        if not snapshot:
            continue
        recovered = (snapshot.get("text") or "").strip()
        if recovered:
            entry["text"] = recovered[:_RETIRED_TEXT_CHAR_CAP]
            patched = True
    return patched
