"""Shared time utilities with no intra-app dependencies."""
from datetime import datetime, timedelta, timezone
from typing import Optional


RETRY_AFTER_MAX_MINUTES = 30


def _utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (no tzinfo).

    Produces TIMESTAMP WITHOUT TIME ZONE values compatible with the
    PostgreSQL columns used throughout the app.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def set_retry_after(context: dict, minutes: float) -> str:
    """Clamp `minutes` to [0, 30] and write `retry_after` on the context dict.

    Returns the ISO-formatted timestamp that was written. A single helper
    prevents far-future or malformed values from permanently blocking
    dispatch, since the orchestrator skips tasks where retry_after > now.
    """
    clamped = max(0.0, min(float(minutes), RETRY_AFTER_MAX_MINUTES))
    iso = (_utcnow() + timedelta(minutes=clamped)).isoformat()
    # Round-trip to validate ISO format before persisting.
    datetime.fromisoformat(iso)
    context["retry_after"] = iso
    try:
        context["_retry_after_resets"] = int(context.get("_retry_after_resets") or 0) + 1
    except (TypeError, ValueError):
        context["_retry_after_resets"] = 1
    return iso


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
