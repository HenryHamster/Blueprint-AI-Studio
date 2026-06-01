"""Shared timestamp helpers for the digest test suite.

Extracted verbatim from the original test_digest.py so the narrative,
trend and self-review-effectiveness tests can live in separate files
while sharing the same window-boundary timestamps."""

from datetime import timedelta

from testing.factories import _utcnow


def _curr_ts():
    """Timestamp ~1 hour ago — within the default 24h window."""
    return _utcnow() - timedelta(hours=1)


def _prev_ts():
    """Timestamp ~36 hours ago — in the previous 24h window (24–48h ago)."""
    return _utcnow() - timedelta(hours=36)
