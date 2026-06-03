"""Coalescing strategic-refresh trigger.

A version cut / qualification verdict must *reliably* refresh the strategic loop
(rewrite the problem model for the new version, re-seed the direction frontier).
The old trigger (``release_qualifier._maybe_trigger_strategic_after_qualification``)
was **edge-triggered and dropped on conflict**: when a strategic cycle was already
active, ``_spawn_strategic_review``'s ``_has_active_strategic_task`` guard made it a
silent no-op, so the refresh was *lost*. The only recovery was the autonomous
loop's ``force_strategic`` path — which is itself held back by the
``strategic_spawn_cooldown`` (default 30 min). Net: a version that promoted while a
cycle ran could idle with 0 directions for up to the cooldown window.

  Incident 2026-06-02: v3 promoted while strategic_review ``46e39b63`` was running.
  The post-qualification trigger no-op'd; ``46e39b63`` then wrote a problem model
  describing the (already-superseded) *failure*, and nothing picked up the
  *success*. directions stayed at 0 until the 30-min cooldown elapsed.

This module makes the trigger **delivered, not best-effort**: a coalesced,
single-slot pending marker on ``project.vision`` that the autonomous loop consumes,
bypassing the ``strategic_spawn_cooldown`` (a legitimate verdict-driven refresh is
NOT the cancel/fail spam the cooldown exists to suppress).

Bounded against runaway re-firing ("infinite stacking") by construction:

* **single-slot** — repeated requests collapse into one marker (cannot stack);
* **cleared on consume** — a normal spawn never re-arms itself, so no self-loop;
* **min-interval floor** (``strategic_refresh_min_interval_seconds``, default 120) —
  at most one refresh-driven bypass per window per project, regardless of triggers;
* **breaker** — the consumer gates the bypass on the existing ``_strategic_failures``
  counter (>= 3 → suppressed, 48 h decay), so a *broken* request whose spawned
  cycles keep failing stops bypassing and parks instead of looping.

Only genuine, rare events set the marker (a resolved qualification verdict — at
most one per qualification attempt), so even the *setter* is naturally bounded.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.task_locking import with_vision_locked
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# vision JSON keys (siblings of _strategic_failures / _research_spawn_count /
# _pending_strategic_review — all strategic-loop state lives in project.vision).
_REFRESH_KEY = "_strategic_refresh"
_LAST_FIRED_KEY = "_strategic_refresh_last_fired_at"

DEFAULT_MIN_INTERVAL_SECONDS = 120

# Marker origins. ``qualification`` is the original automated trigger (a
# resolved qualification verdict); ``operator`` is an explicit human steering
# request routed through the live-chat foundation. Operator intent outranks the
# automated path: it bypasses the failure breaker (an operator steers *because*
# the strategist is failing) and its deferral is surfaced to the operator
# rather than silently parked.
ORIGIN_QUALIFICATION = "qualification"
ORIGIN_OPERATOR = "operator"


async def request_strategic_refresh(
    session, project_id, reason: str, *, origin: str = ORIGIN_QUALIFICATION
) -> None:
    """Coalesced: record that the strategic loop owes a refresh.

    Single-slot — repeated calls collapse into ONE pending marker (it cannot
    stack). Called when a trigger (a resolved qualification verdict, or an
    explicit operator steering request) could not spawn a strategic_review
    immediately because a cycle was already active. The autonomous loop consumes
    the marker as soon as the active cycle clears, bypassing the
    ``strategic_spawn_cooldown`` that would otherwise delay the refresh.

    ``origin`` discriminates an automated qualification refresh (default) from a
    higher-priority operator steering request. When coalescing, an operator
    origin is *sticky* — it is never downgraded back to qualification, so a
    human request that lands on top of (or beneath) an automated one keeps its
    breaker-bypass and visibility semantics. Re-arming also clears any stale
    ``notified`` flag so a fresh request can re-surface a deferral.

    Best-effort and non-fatal: a failure here just falls back to the loop's
    normal cadence/wall-clock backstop. Does NOT commit — the caller owns the
    transaction (matches the ``with_vision_locked`` contract).

    Uses ``skip_locked=True``: this runs in the post-qualification trigger path
    (after the finalize session commits) and must NEVER block on the project
    row lock — a contended project would otherwise stall the trigger for the
    full lock timeout. If the project is momentarily locked the mark is skipped
    (a rare race the loop's cadence/empty-frontier backstop still recovers),
    not waited on.
    """

    def _mark(vision: dict) -> None:
        existing = vision.get(_REFRESH_KEY)
        if isinstance(existing, dict):
            # Coalesce into the existing slot: keep the original request time so
            # staleness is visible, refresh the reason, bump an observability count.
            existing["reason"] = reason
            existing["count"] = int(existing.get("count") or 1) + 1
            # Operator intent is sticky and never downgraded by a later
            # automated refresh; a later operator refresh upgrades an automated
            # slot. A fresh request re-arms the one-time deferral notice.
            if origin == ORIGIN_OPERATOR or existing.get("origin") == ORIGIN_OPERATOR:
                existing["origin"] = ORIGIN_OPERATOR
            else:
                existing["origin"] = origin
            existing.pop("notified", None)
        else:
            vision[_REFRESH_KEY] = {
                "requested_at": _utcnow().isoformat(),
                "reason": reason,
                "count": 1,
                "origin": origin,
            }

    try:
        marked = await with_vision_locked(
            session, project_id, _mark, skip_locked=True
        )
        if marked:
            logger.info(
                "[STRATEGIC] strategic refresh requested for project %s (%s)",
                project_id,
                reason,
            )
        else:
            logger.info(
                "[STRATEGIC] strategic refresh request for project %s skipped — "
                "project momentarily locked (best-effort; loop backstop recovers)",
                project_id,
            )
    except Exception:
        logger.exception(
            "[STRATEGIC] request_strategic_refresh failed for project %s", project_id
        )


def strategic_refresh_pending(vision: dict | None) -> bool:
    """True when a refresh marker is set (a refresh is owed)."""
    return isinstance((vision or {}).get(_REFRESH_KEY), dict)


def strategic_refresh_origin(vision: dict | None) -> str | None:
    """Origin of the pending refresh marker, or ``None`` when no marker is set.

    Markers written before the ``origin`` field existed default to
    ``qualification`` (the only path that set markers then), preserving the
    legacy park-on-broken-loop behavior.
    """
    marker = (vision or {}).get(_REFRESH_KEY)
    if not isinstance(marker, dict):
        return None
    return marker.get("origin") or ORIGIN_QUALIFICATION


def refresh_breaker_applies(vision: dict | None) -> bool:
    """True when the ``_strategic_failures`` breaker should gate the pending refresh.

    Automated (qualification) refreshes keep the breaker gate — a broken loop
    whose spawned cycles keep failing parks instead of looping. Operator
    steering refreshes bypass it: an explicit human request is *exactly* when an
    operator needs to intervene, which is precisely when the breaker would be
    tripped. With no marker pending the question is moot; return False.
    """
    return strategic_refresh_origin(vision) == ORIGIN_QUALIFICATION


def strategic_refresh_notified(vision: dict | None) -> bool:
    """True when the pending marker's one-time deferral notice was already sent."""
    marker = (vision or {}).get(_REFRESH_KEY)
    return isinstance(marker, dict) and bool(marker.get("notified"))


def mark_strategic_refresh_notified(vision: dict) -> None:
    """Stamp the pending marker as notified (mutate-fn for ``with_vision_locked``).

    Idempotent: a no-op when no marker is pending. Setting this flag keeps
    subsequent ticks quiet so a deferral message is emitted exactly once per
    pending request (re-armed when ``request_strategic_refresh`` re-marks)."""
    marker = vision.get(_REFRESH_KEY)
    if isinstance(marker, dict):
        marker["notified"] = True


def strategic_refresh_due(
    vision: dict | None,
    now: datetime,
    *,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
) -> bool:
    """True when a pending refresh should fire *now* — the min-interval floor.

    Only the rate-limit gate lives here. The caller applies the other two gates
    (no active strategic task; the ``_strategic_failures`` breaker) because both
    are already computed in the autonomous loop. Returns False (don't fire yet,
    keep the marker for a later tick) when a refresh fired within the window.
    """
    if not strategic_refresh_pending(vision):
        return False
    last = (vision or {}).get(_LAST_FIRED_KEY)
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if last_dt.tzinfo is not None:
                last_dt = last_dt.replace(tzinfo=None)
            if (now - last_dt).total_seconds() < min_interval_seconds:
                return False
        except (ValueError, TypeError):
            pass  # malformed timestamp — treat as no recent fire (fail open to firing)
    return True


def clear_strategic_refresh(vision: dict, now: datetime) -> None:
    """Consume the marker and stamp the fire time (mutate-fn for ``with_vision_locked``).

    Called from the loop's strategic-spawn record path: any successful strategic
    spawn satisfies an owed refresh. Stamps ``_strategic_refresh_last_fired_at``
    only when a marker was actually cleared, so the min-interval floor measures
    refresh-driven fires (not ordinary cadence spawns).
    """
    if vision.pop(_REFRESH_KEY, None) is not None:
        vision[_LAST_FIRED_KEY] = now.isoformat()
