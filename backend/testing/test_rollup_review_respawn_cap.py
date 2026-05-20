"""Tests for the review-respawn circuit-breaker in rollup_management.

Regression — the 2026-05-09 SCOPE_EMPTY incident: the stale-PR rollup recovery
in ``_check_and_trigger_rollup`` respawned a review for the same unmerged PR
795 times in 9.5h because nothing capped the respawns. ``MAX_REVIEW_RESPAWNS``
and ``_review_respawn_count`` bound the loop so it escalates to manual approval
instead of looping unbounded.
"""

from app.rollup_management import MAX_REVIEW_RESPAWNS, _review_respawn_count
from testing.factories import make_task


def _child(**ctx):
    """A create-task child carrying the given context keys."""
    return make_task(context=dict(ctx), payloads=[{"type": "create"}])


class TestReviewRespawnCount:
    def test_zero_when_unset(self):
        assert _review_respawn_count(_child()) == 0

    def test_zero_when_context_is_none(self):
        t = _child()
        t.context = None
        assert _review_respawn_count(t) == 0

    def test_reads_stored_count(self):
        assert _review_respawn_count(_child(review_respawn_count=3)) == 3

    def test_tolerates_non_numeric_value(self):
        # A corrupt context value must not crash the rollup recovery loop.
        assert _review_respawn_count(_child(review_respawn_count="oops")) == 0

    def test_tolerates_none_value(self):
        assert _review_respawn_count(_child(review_respawn_count=None)) == 0

    def test_below_cap_not_reached(self):
        t = _child(review_respawn_count=MAX_REVIEW_RESPAWNS - 1)
        assert _review_respawn_count(t) < MAX_REVIEW_RESPAWNS

    def test_at_cap_reached(self):
        t = _child(review_respawn_count=MAX_REVIEW_RESPAWNS)
        assert _review_respawn_count(t) >= MAX_REVIEW_RESPAWNS


def test_max_review_respawns_is_a_sane_positive_int():
    assert isinstance(MAX_REVIEW_RESPAWNS, int)
    assert 1 <= MAX_REVIEW_RESPAWNS <= 20
