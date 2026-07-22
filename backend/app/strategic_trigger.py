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

import asyncio
import contextlib
import logging
from datetime import datetime

from app.models import MessageType
from app.task_locking import with_vision_locked
from app.time_utils import _utcnow
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

# vision JSON keys (siblings of _strategic_failures / _research_spawn_count /
# _pending_strategic_review — all strategic-loop state lives in project.vision).
_REFRESH_KEY = "_strategic_refresh"
_LAST_FIRED_KEY = "_strategic_refresh_last_fired_at"
_LAST_FIRED_ORIGIN_KEY = "_strategic_refresh_last_fired_origin"

DEFAULT_MIN_INTERVAL_SECONDS = 120

# Per-message operator steering excerpt budget persisted on the refresh marker.
# Mirrors strategic_spawning._OPERATOR_STEERING_QUOTE_MAX_CHARS so the directive
# built from the marker fallback matches the directive built from the live thread.
_STEERING_EXCERPT_MAX_CHARS = 600

# Marker origins. ``qualification`` is the original automated trigger (a
# resolved qualification verdict); ``operator`` is an explicit human steering
# request routed through the live-chat foundation. Operator intent outranks the
# automated path: it bypasses the failure breaker (an operator steers *because*
# the strategist is failing) and its deferral is surfaced to the operator
# rather than silently parked.
ORIGIN_QUALIFICATION = "qualification"
ORIGIN_OPERATOR = "operator"

# Operator-message arming outcomes. These describe what the wake-gate actually
# decided for a just-created message and are surfaced to the operator (the
# message-create response, the composer hint/toast) so the UI reflects the real
# server-side decision rather than a type-only guess:
#   * ``steering``       — armed the operator-origin refresh marker (will wake
#                          the strategist);
#   * ``passive_note``   — a passive ``NOTE``, recorded but never wakes;
#   * ``routine_event``  — a system/routine digest event, never wakes even
#                          though its type is non-passive;
#   * ``not_armed``      — a genuine steering message that the wake-gate tried
#                          to arm but COULD NOT persist (the bounded project-row
#                          lock acquire was exhausted under contention), so no
#                          marker was written and the strategist will NOT be
#                          woken by this message. Surfaced truthfully instead of
#                          a false ``steering`` promise.
ARMING_STEERING = "steering"
ARMING_PASSIVE_NOTE = "passive_note"
ARMING_ROUTINE_EVENT = "routine_event"
ARMING_NOT_ARMED = "not_armed"


def classify_operator_arming(
    message_type: MessageType, *, event_type: str | None = None
) -> str:
    """Pure classifier: what will/did an operator message do to the strategist?

    Single source of truth for the *wake intent*, shared by
    ``maybe_arm_operator_refresh`` (which arms only on ``ARMING_STEERING``) and,
    through it, the message-create endpoints. This decides whether a message
    *should* wake the strategist; whether the wake was actually persisted is
    determined downstream — ``maybe_arm_operator_refresh`` downgrades a steering
    verdict to ``ARMING_NOT_ARMED`` when the marker could not be written — so the
    operator-facing outcome reflects what arming *actually did*, not just intent.

    Returns one of ``ARMING_PASSIVE_NOTE`` (a passive ``NOTE``),
    ``ARMING_ROUTINE_EVENT`` (a non-passive type carrying a system/routine digest
    ``event_type``), or ``ARMING_STEERING`` (genuine human steering that wakes
    the strategist). ``question_answer`` is the one NOTE event that still
    represents active operator steering: the strategist asked for missing input,
    and the operator supplied it.
    """
    if message_type is MessageType.NOTE:
        if event_type in {"question_answer", "feedback_reply"}:
            return ARMING_STEERING
        return ARMING_PASSIVE_NOTE
    if event_type:
        # Inline import — ROUTINE_EVENT_TYPES lives in the router module, which
        # imports a great deal; importing it at module load would risk a cycle.
        from app.routers.admin_notifications import ROUTINE_EVENT_TYPES

        if event_type in ROUTINE_EVENT_TYPES:
            return ARMING_ROUTINE_EVENT
    return ARMING_STEERING


async def request_strategic_refresh(
    session,
    project_id,
    reason: str,
    *,
    origin: str = ORIGIN_QUALIFICATION,
    steering_excerpt: str | None = None,
    skip_locked: bool = True,
) -> bool:
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

    Returns ``True`` when the marker was actually persisted (the project-row
    lock was acquired and ``_mark`` ran), ``False`` when it was not — the
    ``skip_locked`` acquire found the row contended, the bounded blocking
    acquire was exhausted after its retries, or an unexpected error was
    swallowed. Callers that surface an operator-facing receipt MUST honor this
    so a momentarily-contended project is not falsely reported as armed.

    ``skip_locked=True`` preserves the existing post-qualification behavior:
    the caller never blocks on the project row lock, so a contended project
    simply skips the mark and the loop's cadence/backstop recovers later.
    ``skip_locked=False`` is for operator steering: it waits briefly for the
    row, so a live admin message does not silently drop just because another
    writer held the lock for a moment.

    ``steering_excerpt`` is the operator's message text, persisted on the marker
    (truncated to ``_STEERING_EXCERPT_MAX_CHARS``) ONLY for operator-origin
    requests. Storing it durably here lets the strategist rebuild the steering
    directive at spawn time even after the operator message has aged out of the
    volatile recent-messages snapshot. On coalesce, a newer excerpt replaces the
    older one — the most recent operator message is the active steering intent.
    """
    _excerpt = (
        steering_excerpt[:_STEERING_EXCERPT_MAX_CHARS]
        if origin == ORIGIN_OPERATOR and steering_excerpt
        else None
    )

    def _mark(vision: dict) -> None:
        existing = vision.get(_REFRESH_KEY)
        if isinstance(existing, dict):
            # Coalesce into the existing slot: keep the original request time so
            # staleness is visible, refresh the reason, bump an observability count.
            existing["reason"] = reason
            existing["count"] = int(existing.get("count") or 1) + 1
            # operator_count tracks only genuine operator-steering coalesces so
            # the UI badge can show a truthful "+N steers coalesced" that excludes
            # automated qualification/promotion refreshes.
            if origin == ORIGIN_OPERATOR:
                existing["operator_count"] = int(existing.get("operator_count") or 0) + 1
            # Operator intent is sticky and never downgraded by a later
            # automated refresh; a later operator refresh upgrades an automated
            # slot. A fresh request re-arms the one-time deferral notice.
            if origin == ORIGIN_OPERATOR or existing.get("origin") == ORIGIN_OPERATOR:
                existing["origin"] = ORIGIN_OPERATOR
            else:
                existing["origin"] = origin
            # Most-recent operator message wins: replace the stored excerpt when
            # a newer one is supplied. A coalescing automated refresh (no
            # excerpt) leaves any operator excerpt already present untouched.
            if _excerpt is not None:
                existing["excerpt"] = _excerpt
            existing.pop("notified", None)
        else:
            marker = {
                "requested_at": _utcnow().isoformat(),
                "reason": reason,
                "count": 1,
                "operator_count": 1 if origin == ORIGIN_OPERATOR else 0,
                "origin": origin,
            }
            if _excerpt is not None:
                marker["excerpt"] = _excerpt
            vision[_REFRESH_KEY] = marker

    async def _store_locked_refresh() -> bool:
        for attempt in range(3):
            try:
                # Route the vision write through the canonical helper so the
                # read-modify-write stays serialized under the project row lock
                # (with_vision_locked owns the FOR UPDATE acquire + write-back).
                # ``timeout=1.0`` keeps the operator-steering acquire bounded:
                # a momentarily-contended row raises OperationalError and we
                # retry rather than stalling on the lock.
                return await with_vision_locked(
                    session, project_id, _mark, skip_locked=False, timeout=1.0
                )
            except OperationalError:
                # A bounded blocking acquire can still time out if another
                # writer holds the row unusually long. Roll back the aborted
                # statement before retrying so the caller's transaction stays
                # usable on the next attempt.
                with contextlib.suppress(Exception):
                    await session.rollback()
                if attempt < 2:
                    await asyncio.sleep(0.25 * (attempt + 1))
                else:
                    logger.exception(
                        "[STRATEGIC] blocking refresh request for project %s failed",
                        project_id,
                    )
                if attempt == 2:
                    return False
        return False

    try:
        if skip_locked:
            marked = await with_vision_locked(
                session, project_id, _mark, skip_locked=True
            )
        else:
            marked = await _store_locked_refresh()
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
        return bool(marked)
    except Exception:
        logger.exception(
            "[STRATEGIC] request_strategic_refresh failed for project %s", project_id
        )
        # Database errors can leave the transaction unusable even though this
        # helper intentionally reports a best-effort miss. Roll back before
        # returning so callers can truthfully persist the accepted message as
        # waiting (and re-apply any small staged state they own).
        with contextlib.suppress(Exception):
            await session.rollback()
        return False


async def maybe_arm_operator_refresh(
    session,
    project_id,
    message_type: MessageType,
    *,
    event_type: str | None = None,
    message_content: str | None = None,
) -> str:
    """Shared 'should this operator message wake the strategist, and arm it' gate.

    Both operator message-creation endpoints route their wake decision through
    this single helper so the two paths cannot silently diverge again (this bug
    existed precisely because the wake logic was added to the project-scoped CLI
    endpoint ``POST /api/admin/projects/{id}/messages`` but not the canonical
    live-chat endpoint ``POST /api/admin/messages`` the admin MessagesPanel hits).

    A message arms the operator-origin strategic-refresh marker only when it is a
    genuine operator steering message: a non-passive operator type (anything but
    a passive ``NOTE``) that is NOT a system/routine digest-event message (those
    are automated digests appended to ``running_update``, never human steering).

    Best-effort and coalescing: delegates to ``request_strategic_refresh``, which
    swallows its own errors and does NOT commit (the caller owns the txn), so
    arming can never make message creation fail.

    Returns the *truthful* arming outcome string — the SINGLE value both
    endpoints surface to the operator, so a momentarily-contended project that
    fails to persist the marker is never reported as a successful steer:

      * the ``classify_operator_arming`` verdict (``passive_note`` /
        ``routine_event``) for a message that was never meant to wake;
      * ``ARMING_STEERING`` for a genuine steering message whose marker WAS
        persisted (``request_strategic_refresh`` returned ``True``);
      * ``ARMING_NOT_ARMED`` for a genuine steering message whose marker could
        NOT be persisted (the bounded lock acquire was exhausted) — the wake
        did not happen, so the operator is told so rather than promised a steer.

    The wake decision itself is derived from ``classify_operator_arming`` so this
    helper and the operator-facing outcome surfaced by the message-create
    endpoint can never diverge.

    ``message_content`` is the operator's message text. It is persisted on the
    refresh marker (as the steering excerpt) so the steering directive survives
    the operator message aging out of the volatile recent-messages snapshot
    before the deferred strategic cycle reaches its review phase.
    """
    outcome = classify_operator_arming(message_type, event_type=event_type)
    if outcome != ARMING_STEERING:
        return outcome
    persisted = await request_strategic_refresh(
        session,
        project_id,
        "operator_message",
        origin=ORIGIN_OPERATOR,
        steering_excerpt=message_content,
        skip_locked=False,
    )
    return ARMING_STEERING if persisted else ARMING_NOT_ARMED


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


def strategic_refresh_excerpt(vision: dict | None) -> str | None:
    """Operator steering excerpt persisted on the pending refresh marker, if any.

    Captured at arm time (``maybe_arm_operator_refresh``) so the steering
    directive can be rebuilt at spawn time even after the operator message has
    aged out of the volatile recent-messages snapshot. Only operator-origin
    markers carry an excerpt; returns ``None`` when no marker is set, the marker
    is automated, or no message text was preserved.
    """
    marker = (vision or {}).get(_REFRESH_KEY)
    if not isinstance(marker, dict):
        return None
    excerpt = marker.get("excerpt")
    return excerpt if isinstance(excerpt, str) and excerpt else None


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


def strategic_refresh_last_fired_origin(vision: dict | None) -> str | None:
    """Origin of the most recently consumed refresh marker, or ``None`` when never fired.

    Allows the UI to distinguish whether a steer receipt is from operator intent or
    an automated trigger like qualification. Only operator-origin consumes show the
    'Steer picked up' receipt.
    """
    return (vision or {}).get(_LAST_FIRED_ORIGIN_KEY)


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


async def cancel_strategic_refresh(session, project_id) -> bool:
    """Operator retract: clear a pending refresh marker so it will NOT wake the strategist.

    Idempotent — cancelling when nothing is armed is a harmless no-op returning
    ``False`` (no marker was pending); ``True`` when an armed marker was removed.

    Unlike :func:`clear_strategic_refresh` (the loop's *consume* path), this does
    NOT stamp ``_strategic_refresh_last_fired_at`` / ``_LAST_FIRED_ORIGIN_KEY``: a
    retracted steer never ran, so it must leave no 'Steer picked up' receipt and
    must not advance the min-interval floor. Routes the write through
    ``with_vision_locked`` (bounded blocking acquire, matching the operator-steering
    arm path) so it serializes with concurrent arming. Best-effort under
    contention — a momentarily-locked row surfaces as ``False`` rather than raising.
    Does NOT commit — the caller owns the transaction.
    """
    cleared = {"marker": False}

    def _clear(vision: dict) -> None:
        if vision.pop(_REFRESH_KEY, None) is not None:
            cleared["marker"] = True

    try:
        await with_vision_locked(
            session, project_id, _clear, skip_locked=False, timeout=1.0
        )
    except OperationalError:
        with contextlib.suppress(Exception):
            await session.rollback()
        logger.exception(
            "[STRATEGIC] cancel refresh for project %s failed — row contended",
            project_id,
        )
        return False
    except Exception:
        logger.exception(
            "[STRATEGIC] cancel_strategic_refresh failed for project %s", project_id
        )
        return False
    return cleared["marker"]


def clear_strategic_refresh(vision: dict, now: datetime) -> None:
    """Consume the marker and stamp the fire time (mutate-fn for ``with_vision_locked``).

    Called from the loop's strategic-spawn record path: any successful strategic
    spawn satisfies an owed refresh. Stamps ``_strategic_refresh_last_fired_at``
    and the origin of the consumed marker (``_strategic_refresh_last_fired_origin``)
    only when a marker was actually cleared, so the min-interval floor measures
    refresh-driven fires (not ordinary cadence spawns) and the UI can attribute
    receipts correctly (only operator-origin consumes show 'Steer picked up').
    """
    marker = vision.pop(_REFRESH_KEY, None)
    if marker is not None:
        vision[_LAST_FIRED_KEY] = now.isoformat()
        if isinstance(marker, dict):
            origin = marker.get("origin") or ORIGIN_QUALIFICATION
            vision[_LAST_FIRED_ORIGIN_KEY] = origin
