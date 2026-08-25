"""Shared time utilities with no intra-app dependencies."""

import random
from datetime import datetime, timedelta, timezone
from typing import Optional


RETRY_AFTER_MAX_MINUTES = 30


def _utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (no tzinfo).

    Produces TIMESTAMP WITHOUT TIME ZONE values compatible with the
    PostgreSQL columns used throughout the app.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _strip_tz_assume_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip tzinfo. Asserts input is either naive or aware-UTC.

    After F1's NaiveUTCDateTime TypeDecorator landed, ORM-loaded datetimes
    are already naive — this function is now mostly redundant. Existing
    callsites are no-ops; new code shouldn't need it.

    Round 2 audit (M12) renamed from the older ``_to_naive_utc`` so the
    UTC-only assumption is explicit at every callsite. Non-UTC tz-aware
    inputs raise ``ValueError`` rather than silently losing wall-clock
    information; the previous implementation would have converted them
    to UTC and stripped the tz, which masked tz bugs.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    if dt.tzinfo.utcoffset(dt) != timedelta(0):
        # Don't silently lose information from non-UTC tz-aware datetimes.
        raise ValueError(f"Expected UTC or naive datetime, got tz={dt.tzinfo}")
    return dt.replace(tzinfo=None)


# Backwards-compat alias kept so existing imports keep working without a
# codebase-wide sweep. Marked deprecated; new code should call
# ``_strip_tz_assume_utc`` directly. After F1's TypeDecorator lands ORM
# reads are naive so all current callsites are no-ops anyway — they're
# scheduled for removal in a Phase G cleanup.
_to_naive_utc = _strip_tz_assume_utc  # deprecated alias


def utc_iso(value: Optional[datetime]) -> Optional[str]:
    """Render a stored datetime as ISO-8601 that carries its UTC offset.

    Everything inside this app stores and passes naive UTC (``_utcnow`` above,
    and the ``NaiveUTCDateTime`` column type that strips tzinfo on read). A
    bare ``value.isoformat()`` therefore puts ``2026-08-24T12:34:56.789012`` on
    the wire -- a string that states no zone at all. Both of its consumers get
    a wrong answer rather than an error:

    * Python callers that compare it against an aware ``now`` raise
      ``TypeError: can't subtract offset-naive and offset-aware datetimes``,
      or -- worse -- assume local time and compute a delta that is wrong by
      the host's UTC offset.
    * ``new Date(s)`` in JavaScript reads an offset-less ISO string as *local*
      time (ES2016+), silently shifting every rendered timestamp.

    Naive input is UTC by construction here, so it is stamped ``+00:00``.
    Aware input is converted to UTC first so the wire format is single-valued.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _coerce_naive_utc(dt: datetime) -> datetime:
    """Convert any datetime to naive UTC, or return it unchanged if already naive.

    Unlike ``_strip_tz_assume_utc``, this accepts non-UTC tz-aware datetimes
    and converts them to UTC rather than raising. Use this when parsing stored
    ISO timestamps that may carry an arbitrary offset (including 'Z'-suffix
    entries emitted by tz-aware writers).
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def set_retry_after(task_or_ctx, minutes: float) -> str:
    """Clamp `minutes` to [0, 30], write retry_after on the task or context.

    Audit 2026-04-25 W1: ``Task.retry_after_until`` is now a real column
    (was JSONB ``context['retry_after']``) so concurrent dispatch / status
    POST writes can't race on a stale in-memory copy. This helper accepts
    either a Task object (preferred) or the legacy context dict (still
    supported for code paths that don't have a Task in scope, e.g. spawn
    paths that operate on ``new_task_context`` before the row exists).

    The escalation counter ``_retry_after_resets`` stays in context (it's
    an escalation hint, not idempotency-critical, so writer races are
    tolerable).

    Returns the ISO-formatted timestamp that was written.
    """
    clamped = max(0.0, min(float(minutes), RETRY_AFTER_MAX_MINUTES))
    deadline = _utcnow() + timedelta(minutes=clamped)
    iso = deadline.isoformat()
    # Round-trip to validate ISO format before persisting.
    datetime.fromisoformat(iso)

    # Branch on shape: a Task object exposes retry_after_until as an
    # attribute and (usually) ``context`` as a dict; a plain dict only has
    # the legacy context shape.
    if hasattr(task_or_ctx, "retry_after_until"):
        task = task_or_ctx
        task.retry_after_until = deadline
        ctx = dict(task.context or {})
        try:
            ctx["_retry_after_resets"] = int(ctx.get("_retry_after_resets") or 0) + 1
        except (TypeError, ValueError):
            ctx["_retry_after_resets"] = 1
        task.context = ctx
    else:
        context = task_or_ctx
        context["retry_after"] = iso
        try:
            context["_retry_after_resets"] = (
                int(context.get("_retry_after_resets") or 0) + 1
            )
        except (TypeError, ValueError):
            context["_retry_after_resets"] = 1
    return iso


def clear_retry_after(task_or_ctx) -> None:
    """Clear retry_after deadline + reset counter on task or context dict.

    Pairs with ``set_retry_after`` for symmetric API. Replaces the
    scattered ``ctx.pop("retry_after", None); ctx.pop("_retry_after_resets", None)``
    pattern that existed in 5+ sites.
    """
    if hasattr(task_or_ctx, "retry_after_until"):
        task = task_or_ctx
        task.retry_after_until = None
        if task.context:
            ctx = dict(task.context)
            ctx.pop("_retry_after_resets", None)
            task.context = ctx
    else:
        context = task_or_ctx
        context.pop("retry_after", None)
        context.pop("_retry_after_resets", None)


def rate_limit_retry_minutes(
    earliest_recovery: Optional[datetime], now: Optional[datetime] = None
) -> float:
    """Compute retry_after delay (minutes) for tasks blocked on account rate limits.

    Uses the known earliest account recovery time so tasks don't hot-loop every
    2 minutes when recovery is known to be hours away. Falls back to a short
    default when no recovery time is known. The returned value is not clamped;
    ``set_retry_after`` enforces the RETRY_AFTER_MAX_MINUTES ceiling.
    """
    default_min = 2.0
    if earliest_recovery is None:
        return default_min
    if now is None:
        now = _utcnow()
    delta_min = (earliest_recovery - now).total_seconds() / 60
    return max(default_min, delta_min)


def _is_cadence_due(
    last_run: datetime | None,
    interval_seconds: float,
    now: datetime | None = None,
) -> bool:
    """Return True when at least interval_seconds have elapsed since last_run.

    A None last_run is always considered due (first-run-immediate semantics).
    Pass now to avoid a redundant _utcnow() call when the caller already has it.
    """
    if last_run is None:
        return True
    if now is None:
        now = _utcnow()
    return (now - last_run).total_seconds() >= interval_seconds


def exponential_backoff(
    consecutive: int,
    base: float = 0.5,
    cap: float = 60.0,
    jitter: float = 0.2,
) -> float:
    """Return a jittered exponential backoff delay in seconds.

    delay = min(base * 2**(consecutive - 1), cap) * uniform(1-jitter, 1+jitter)

    Args:
        consecutive: Number of consecutive errors (1-indexed; must be >= 1).
        base: Base delay in seconds for the first error.
        cap: Maximum delay in seconds before jitter.
        jitter: Fractional jitter applied symmetrically (default ±20%).
    """
    raw = min(base * (2 ** (consecutive - 1)), cap)
    return raw * random.uniform(1 - jitter, 1 + jitter)


def parse_retry_after(value) -> Optional[datetime]:
    """Parse a stored retry_after value. Returns a naive UTC datetime or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
