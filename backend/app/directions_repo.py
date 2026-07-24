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

_MISSING_CRITERION_KEYS = object()


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


def _coerce_criterion_keys(direction: Dict[str, Any]):
    if "criterion_keys" not in direction:
        return _MISSING_CRITERION_KEYS
    raw = direction.get("criterion_keys")
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "[directions_repo] ignoring malformed criterion_keys for direction %s",
            direction.get("id"),
        )
        return _MISSING_CRITERION_KEYS
    keys = [
        str(item).strip()
        for item in raw
        if isinstance(item, str) and str(item).strip()
    ]
    return list(dict.fromkeys(keys))


async def _guard_explicit_criterion_keys(
    session: AsyncSession,
    project_id: uuid.UUID,
    direction_key: str,
    criterion_keys,
    release_version_id: uuid.UUID | None,
):
    if criterion_keys is _MISSING_CRITERION_KEYS or not criterion_keys:
        return criterion_keys
    from app.direction_referential_guard import (
        check_criterion_key_write,
        load_version_criterion_key_sets,
    )
    if release_version_id is None:
        from app.release_version import current_open_version

        open_version = await current_open_version(session, project_id)
        if open_version is None:
            logger.warning(
                "[directions_repo] preserving existing criterion_keys for direction %s: "
                "no release version to validate explicit keys",
                direction_key,
            )
            return _MISSING_CRITERION_KEYS
        release_version_id = open_version.id

    active_keys, inactive_keys = await load_version_criterion_key_sets(
        session, project_id, release_version_id
    )
    valid: list[str] = []
    rejected: dict[str, list[str]] = {}
    for key in criterion_keys:
        classification, _ = check_criterion_key_write(
            key, active_keys, inactive_keys
        )
        if classification is None:
            valid.append(key)
        else:
            rejected.setdefault(classification, []).append(key)
    if rejected:
        logger.warning(
            "[directions_repo] scrubbed invalid criterion_keys for direction %s: %s",
            direction_key,
            rejected,
        )
    return valid


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


async def get_direction_row(
    session: AsyncSession, project_id, direction_key: str
) -> "Direction | None":
    """Return the Direction ORM row (or None) for a direction key.

    Unlike :func:`get_direction` which returns only the ``data`` dict, this
    returns the full row so callers can read first-class columns like
    ``criterion_keys`` that are not part of the data blob.
    """
    pid = _coerce_project_id(project_id)
    return (
        await session.execute(
            select(Direction).where(
                Direction.project_id == pid,
                Direction.direction_key == direction_key,
            )
        )
    ).scalar_one_or_none()


async def get_direction_rows(
    session: AsyncSession, project_id, direction_keys
) -> "Dict[str, Direction]":
    """Batch-fetch Direction ORM rows for many keys in ONE query.

    Returns a dict keyed by ``direction_key`` for O(1) lookup, containing only
    the keys that resolved to a row. Empty input yields an empty dict without
    touching the database. This is the batched equivalent of
    :func:`get_direction_row` — it avoids the N round-trips a per-key loop
    incurs.
    """
    keys = [k for k in dict.fromkeys(direction_keys) if k]
    if not keys:
        return {}
    pid = _coerce_project_id(project_id)
    rows = (
        (
            await session.execute(
                select(Direction).where(
                    Direction.project_id == pid,
                    Direction.direction_key.in_(keys),
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.direction_key: row for row in rows}


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
    # Snapshot status before any mutation so the event trail sees the old value.
    prior_status_snapshot = existing.status if existing is not None else None
    from app.acceptance_criteria import (
        CRITERION_RELEASE_VERSION_CONTEXT_KEY,
        direction_criterion_release_version_id,
    )

    direction_data = dict(direction)
    release_version_id = direction_criterion_release_version_id(direction_data)
    criterion_keys = _coerce_criterion_keys(direction)
    criterion_keys = await _guard_explicit_criterion_keys(
        session, pid, str(direction_key), criterion_keys, release_version_id
    )
    if (
        existing is not None
        and release_version_id is None
        and (
            criterion_keys is _MISSING_CRITERION_KEYS
            or criterion_keys == list(existing.criterion_keys or [])
        )
    ):
        existing_version_id = direction_criterion_release_version_id(existing)
        if existing_version_id is not None:
            direction_data[CRITERION_RELEASE_VERSION_CONTEXT_KEY] = str(
                existing_version_id
            )
    if criterion_keys == []:
        direction_data.pop(CRITERION_RELEASE_VERSION_CONTEXT_KEY, None)
    if existing is None:
        row_kwargs = dict(
            project_id=pid,
            direction_key=str(direction_key)[:128],
            data=direction_data,
            **promoted,
        )
        if criterion_keys is not _MISSING_CRITERION_KEYS:
            row_kwargs["criterion_keys"] = criterion_keys
        session.add(Direction(**row_kwargs))
    else:
        existing.data = direction_data
        existing.status = promoted["status"]
        existing.priority = promoted["priority"]
        existing.text = promoted["text"]
        if criterion_keys is not _MISSING_CRITERION_KEYS:
            existing.criterion_keys = criterion_keys
        existing.updated_at = _utcnow()
        session.add(existing)
    # Best-effort lifecycle event — prior_status_snapshot captures the old status
    # before the mutation above so the helper sees the correct prior value.
    try:
        from app.direction_event_trail import maybe_record_direction_event
        await maybe_record_direction_event(
            session, pid, direction, existing,
            prior_status_snapshot=prior_status_snapshot,
        )
    except Exception as _evt_exc:  # noqa: BLE001
        logger.warning(
            "[directions_repo] event trail write failed for %s: %s",
            direction_key,
            _evt_exc,
        )


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
        # Emit pruned events before hard-deleting so the event trail survives.
        try:
            from app.direction_event_trail import record_direction_pruned
            stale_rows = (
                (
                    await session.execute(
                        select(Direction).where(
                            Direction.project_id == pid,
                            Direction.direction_key.in_(stale),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for stale_row in stale_rows:
                await record_direction_pruned(
                    session, pid, stale_row.direction_key, dict(stale_row.data or {})
                )
        except Exception as _prune_exc:  # noqa: BLE001
            logger.warning(
                "[directions_repo] pruned event trail write failed for project %s: %s",
                project_id,
                _prune_exc,
            )
        await session.execute(
            delete(Direction).where(
                Direction.project_id == pid,
                Direction.direction_key.in_(stale),
            )
        )


async def activate_eligible_staged_directions(
    session: AsyncSession,
    project_id,
    promoted_version_number: int,
) -> List[str]:
    """Activate staged directions whose target version has promoted/opened.

    A *staged* direction (``status == "staged"``) is inert work marked to begin
    only after the version that was OPEN when it was staged has promoted. The
    per-direction ``staged_for_version`` marker records that version number.

    This flips to ``active`` staged directions whose
    ``staged_for_version <= promoted_version_number``, clears the
    ``staged_for_version`` marker, and stamps ``staged_activated_at``. It is the
    single shared activation entry point used by BOTH the version-open promotion
    hook and the reaper durability backstop, so the two paths can never diverge.

    Active-direction cap (genuine ceiling): activation never pushes the project
    past ``runtime_config["max_active_directions"]`` — the SAME limit the manual
    path (``routers/directions.py``) hard-rejects with a 409. When more staged
    directions are eligible than there are free slots, the overflow stays
    ``staged`` with its ``staged_for_version`` marker intact and is drained by
    subsequent sweeps as active slots free, so deferred-but-approved work is
    never stranded. Scarce slots go to higher-priority then older-``staged_at``
    (FIFO) directions first.

    Idempotency / gating guarantees:
      - Activating an already-``active`` direction is a no-op (only ``staged``
        rows are touched), so repeated sweeps converge.
      - A direction staged DURING the version that just opened
        (``staged_for_version > promoted_version_number``) stays staged — its
        target version has not promoted yet.
      - A staged direction with a missing/unparseable ``staged_for_version`` is
        activated (fail-open) so a malformed marker can never strand work forever.

    Runs the mutation under the project-row lock via ``with_directions_locked``
    (which dual-writes the normalised ``direction`` table), commits, then
    broadcasts ``emit_strategic_updated(project_id, STRATEGIC_KIND_DIRECTIONS)``
    when at least one direction was activated. Returns the list of activated
    direction ids (empty when nothing was eligible).

    Best-effort contract: callers wrap this in try/except so an activation
    failure can never abort the promotion / reaper sweep that invoked it.
    """
    # Lazy imports: task_locking imports directions_repo (sync_directions), so a
    # module-level import here would create a cycle. strategic_events is only
    # needed for the post-commit broadcast. runtime_config carries the
    # active-direction cap (mirrors the lazy import in routers/directions.py).
    from app.task_locking import with_directions_locked
    from app.strategic_events import (
        STRATEGIC_KIND_DIRECTIONS,
        emit_strategic_updated,
    )
    from app.shared_state import runtime_config as _runtime_config

    # Bound notes the same way every other direction note writer does, so a
    # repeatedly-staged/activated direction can't grow its notes array forever.
    from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX

    # The active-direction cap is a genuine ceiling on concurrent active work —
    # the manual path (routers/directions.py) hard-rejects with 409 at the cap,
    # so auto-activation must respect the SAME limit rather than flipping every
    # eligible direction at once. Overflow stays staged with its marker intact;
    # the periodic Step 13 reaper sweep re-runs and drains it as active slots
    # free, so deferred-but-approved work is never stranded.
    max_active = _runtime_config.get("max_active_directions", 10)

    # Scarce-slot tie-break: higher priority wins, then oldest staged_at (FIFO)
    # so the longest-waiting approved work activates first. Unknown priority
    # sorts as medium; a missing staged_at sorts earliest (treated as oldest).
    _PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

    activated: List[str] = []
    activated_meta: List[Dict[str, Any]] = []
    activated_with_gaps: List[tuple] = []  # (direction_id, gap_id) for PM re-staffing
    now_iso = _utcnow().isoformat()

    def _mutate(directions: List[Dict[str, Any]]) -> None:
        # Count directions already active — the cap bounds the post-activation
        # total, so only (max_active - active_count) staged directions may flip.
        active_count = sum(1 for d in directions if d.get("status") == "active")
        slots = max_active - active_count
        if slots <= 0:
            # Already at/over the ceiling: nothing activates this pass. The
            # overflow stays staged for the next sweep to re-evaluate.
            return

        # Collect every eligible staged direction (target version promoted, or
        # fail-open on an unparseable marker) before flipping, so we can apply
        # the scarce-slot ordering deterministically across the whole set.
        eligible: List[Dict[str, Any]] = []
        for d in directions:
            if d.get("status") != "staged":
                continue
            target = d.get("staged_for_version")
            try:
                target_n: int | None = int(target)
            except (TypeError, ValueError):
                # Unparseable marker — fail open so malformed staging can't
                # strand the direction forever.
                target_n = None
            if target_n is not None and target_n > promoted_version_number:
                # Staged during the version that just opened — its target
                # version has not promoted yet; leave it staged.
                continue
            eligible.append(d)

        eligible.sort(
            key=lambda d: (
                _PRIORITY_RANK.get(d.get("priority"), 1),
                str(d.get("staged_at") or ""),
            )
        )

        for d in eligible[:slots]:
            d["status"] = "active"
            d.pop("staged_for_version", None)
            d["staged_activated_at"] = now_iso
            # Symmetric with the staging-IN note: record why the direction
            # resumed and which version's promotion triggered it.
            notes = d.get("notes", [])
            notes.append(
                {
                    "text": (
                        f"Auto-activated: v{promoted_version_number} promoted "
                        "— staged work resumed"
                    ),
                    "added_at": now_iso,
                }
            )
            d["notes"] = notes[-STRATEGIC_NOTES_HISTORY_MAX:]
            did = d.get("id")
            if did:
                activated.append(str(did))
                if d.get("gap_id"):
                    activated_with_gaps.append((str(did), str(d["gap_id"])))
            activated_meta.append(
                {"id": str(did) if did else None, "text": d.get("text")}
            )

    applied = await with_directions_locked(session, project_id, _mutate)
    if not applied or not activated:
        return []
    # Re-staff problem-model gaps for each activated direction in the SAME
    # transaction (committed below). Mirrors create_direction's staffing:
    # add direction_id to gap.active_moves and clear any GapDeferral.
    if activated_with_gaps:
        from app.problem_model import reconcile_gap_staffing_raw_result as _rcgs  # noqa: PLC0415
        from app.task_locking import with_problem_model_locked as _wpm_locked  # noqa: PLC0415

        def _restaff_pm(pm: dict) -> None:
            current = dict(pm)
            for _did, _gid in activated_with_gaps:
                result = _rcgs(
                    current,
                    _did,
                    _gid,
                    None,
                    new_status="active",
                    prior_status="staged",
                    now_iso=now_iso,
                )
                if result.get("problem_model") is not None:
                    current = result["problem_model"]
                details = result.get("result") or {}
                if details.get("malformed_model") or details.get("unresolved_gap_ids"):
                    logger.warning(
                        "[directions_repo] staged-activation gap re-staffing "
                        "classified drift for %s: %s",
                        _did,
                        details,
                    )
            pm.clear()
            pm.update(current)

        try:
            await _wpm_locked(session, project_id, _restaff_pm)
        except Exception as exc:  # noqa: BLE001 — re-staffing is best-effort
            logger.warning(
                "[directions_repo] staged-activation gap re-staffing failed for "
                "project %s (activation will still commit): %s",
                project_id,
                exc,
            )
    # Best-effort operator notification — posted into the SAME session so it
    # rides the commit below atomically with the activation notes. Wrapped in
    # its own try/except (log-and-swallow) so a notification failure can never
    # abort the already-decided activation. Lazy import preserves this module's
    # documented cycle avoidance.
    try:
        from app.notification_dispatch import (
            post_staged_directions_activated_message,
        )

        await post_staged_directions_activated_message(
            session, project_id, activated_meta, promoted_version_number
        )
    except Exception as exc:  # noqa: BLE001 — notification is strictly best-effort
        logger.warning(
            "[directions_repo] staged-activation notification failed for project "
            "%s (activation will still commit): %s",
            project_id,
            exc,
        )
    await session.commit()
    try:
        await emit_strategic_updated(project_id, STRATEGIC_KIND_DIRECTIONS)
    except Exception as exc:  # noqa: BLE001 — broadcast is a best-effort UI signal
        logger.warning(
            "[directions_repo] staged-activation broadcast failed for project "
            "%s (activation committed): %s",
            project_id,
            exc,
        )
    # Best-effort: resolve any open "stale staged direction" alert for the
    # directions we just activated. This is exactly the moment a long-stalled
    # staged direction finally activates, so a lingering WARNING alert is most
    # likely there — clear it so the operator UI doesn't show a phantom stall.
    # Placed AFTER the commit above and wrapped in try/except (log-and-swallow)
    # so a resolution failure can never affect the already-committed activation.
    # Lazy import preserves this module's documented cycle avoidance.
    try:
        from app.alert_dispatch import _resolve_stale_staged_direction_alerts

        dismissed = await _resolve_stale_staged_direction_alerts(
            session, project_id, activated
        )
        if dismissed:
            logger.info(
                "[directions_repo] resolved %d stale-staged-direction alert(s) "
                "for project %s on activation",
                dismissed,
                project_id,
            )
    except Exception as exc:  # noqa: BLE001 — alert resolution is strictly best-effort
        logger.warning(
            "[directions_repo] staged-activation alert resolution failed for "
            "project %s (activation committed): %s",
            project_id,
            exc,
        )
    logger.info(
        "[directions_repo] activated %d staged direction(s) for project %s "
        "(promoted_version=%d): %s",
        len(activated),
        project_id,
        promoted_version_number,
        ", ".join(activated),
    )
    return activated


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
