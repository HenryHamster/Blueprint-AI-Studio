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


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip tzinfo from a datetime, converting to UTC first if it was aware.

    Phase D2 (alembic 0061) migrated several columns to ``DateTime(timezone=True)``
    so reads from those columns now return tz-aware datetimes. The rest of the
    codebase still uses naive UTC (``_utcnow()``). Use this at comparison /
    arithmetic boundaries where one side is loaded-from-DB (potentially aware)
    and the other is constructed Python-side (naive).
    """
    if dt is None:
        return None
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
            context["_retry_after_resets"] = int(context.get("_retry_after_resets") or 0) + 1
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
