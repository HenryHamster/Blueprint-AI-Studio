"""Shared time utilities with no intra-app dependencies."""
from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (no tzinfo).

    Produces TIMESTAMP WITHOUT TIME ZONE values compatible with the
    PostgreSQL columns used throughout the app.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
