"""Unit tests for time_utils canonical helpers: _is_cadence_due and exponential_backoff."""

from datetime import datetime, timedelta

from app.time_utils import _is_cadence_due, exponential_backoff, _utcnow


class TestIsCadenceDue:
    """Test the _is_cadence_due helper for dispatch-loop cadence gates."""

    def test_is_cadence_due_none_last_run_is_always_due(self):
        """First-run-immediate: None last_run always returns True."""
        # Core contract: when last_run is None, always consider it due.
        assert _is_cadence_due(last_run=None, interval_seconds=60)
        assert _is_cadence_due(last_run=None, interval_seconds=0.1)
        assert _is_cadence_due(last_run=None, interval_seconds=10000)

    def test_is_cadence_due_insufficient_time_elapsed(self):
        """Not due when insufficient time has elapsed since last_run."""
        now = datetime(2026, 1, 1, 12, 0, 0)
        last_run = datetime(2026, 1, 1, 11, 59, 50)  # 10 seconds ago
        # Interval is 60 seconds, only 10 have elapsed
        assert not _is_cadence_due(last_run=last_run, interval_seconds=60, now=now)

    def test_is_cadence_due_exactly_interval_elapsed(self):
        """Due when exactly interval_seconds have elapsed."""
        now = datetime(2026, 1, 1, 12, 0, 0)
        last_run = datetime(2026, 1, 1, 11, 59, 0)  # Exactly 60 seconds ago
        # Interval is 60 seconds, exactly 60 have elapsed
        assert _is_cadence_due(last_run=last_run, interval_seconds=60, now=now)

    def test_is_cadence_due_more_than_interval_elapsed(self):
        """Due when more than interval_seconds have elapsed."""
        now = datetime(2026, 1, 1, 12, 0, 0)
        last_run = datetime(2026, 1, 1, 11, 58, 0)  # 120 seconds ago
        # Interval is 60 seconds, 120 have elapsed
        assert _is_cadence_due(last_run=last_run, interval_seconds=60, now=now)

    def test_is_cadence_due_fractional_seconds(self):
        """Fractional intervals work correctly."""
        now = datetime(2026, 1, 1, 12, 0, 0, 500000)  # 12:00:00.5
        last_run = datetime(2026, 1, 1, 12, 0, 0, 250000)  # 12:00:00.25
        # 0.25 seconds elapsed, interval is 0.2 seconds
        assert _is_cadence_due(last_run=last_run, interval_seconds=0.2, now=now)

    def test_is_cadence_due_uses_utcnow_when_now_not_provided(self):
        """When now is not provided, _is_cadence_due calls _utcnow."""
        # This test validates that the function calls _utcnow internally
        # by testing that a very recent last_run is not due yet.
        very_recent = _utcnow() - timedelta(milliseconds=10)
        # 10ms ago with a 1-second interval should not be due
        assert not _is_cadence_due(last_run=very_recent, interval_seconds=1.0)

    def test_is_cadence_due_zero_interval(self):
        """Zero interval: always due (unless last_run is in the future)."""
        now = datetime(2026, 1, 1, 12, 0, 0)
        last_run = datetime(2026, 1, 1, 12, 0, 0)
        # Zero interval: 0 seconds elapsed equals 0 seconds required
        assert _is_cadence_due(last_run=last_run, interval_seconds=0.0, now=now)

    def test_is_cadence_due_last_run_in_future(self):
        """Clock skew: last_run in the future returns False (not yet due)."""
        now = datetime(2026, 1, 1, 12, 0, 0)
        last_run = datetime(2026, 1, 1, 12, 1, 0)  # 60 seconds in the future
        # Negative elapsed time should not be due
        assert not _is_cadence_due(last_run=last_run, interval_seconds=60, now=now)


class TestExponentialBackoff:
    """Test the exponential_backoff helper for retry scheduling."""

    def test_exponential_backoff_first_attempt_no_jitter(self):
        """First attempt (consecutive=1) with no jitter returns base delay."""
        # With jitter=0, formula is: min(base * 2^(1-1), cap) * 1.0
        # = base * 2^0 = base
        result = exponential_backoff(consecutive=1, base=0.5, jitter=0.0)
        assert result == 0.5

    def test_exponential_backoff_exponential_growth_no_jitter(self):
        """Exponential growth: delay doubles with each attempt (no jitter)."""
        base = 0.5
        jitter = 0.0
        # consecutive=1: 0.5 * 2^0 = 0.5
        assert exponential_backoff(1, base=base, jitter=jitter) == 0.5
        # consecutive=2: 0.5 * 2^1 = 1.0
        assert exponential_backoff(2, base=base, jitter=jitter) == 1.0
        # consecutive=3: 0.5 * 2^2 = 2.0
        assert exponential_backoff(3, base=base, jitter=jitter) == 2.0
        # consecutive=4: 0.5 * 2^3 = 4.0
        assert exponential_backoff(4, base=base, jitter=jitter) == 4.0

    def test_exponential_backoff_cap_enforced(self):
        """Delay is capped at cap parameter before jitter."""
        base = 1.0
        cap = 10.0
        jitter = 0.0
        # consecutive=5: 1.0 * 2^4 = 16.0, capped to 10.0
        assert exponential_backoff(5, base=base, cap=cap, jitter=jitter) == cap
        # consecutive=10: even larger, still capped
        assert exponential_backoff(10, base=base, cap=cap, jitter=jitter) == cap

    def test_exponential_backoff_jitter_bounds(self):
        """Jitter applies symmetrically within bounds."""
        # With jitter=0.2, result should be in [0.8*raw, 1.2*raw]
        base = 1.0
        consecutive = 2  # raw = 2.0
        jitter = 0.2
        # Run multiple times to check bounds hold
        for _ in range(100):
            result = exponential_backoff(
                consecutive, base=base, cap=60.0, jitter=jitter
            )
            expected_raw = base * (2 ** (consecutive - 1))
            lower_bound = expected_raw * (1 - jitter)
            upper_bound = expected_raw * (1 + jitter)
            assert lower_bound <= result <= upper_bound

    def test_exponential_backoff_zero_jitter_deterministic(self):
        """With jitter=0, results are deterministic."""
        results = [
            exponential_backoff(consecutive=3, base=0.5, jitter=0.0)
            for _ in range(5)
        ]
        # All results should be identical
        assert len(set(results)) == 1

    def test_exponential_backoff_nonzero_jitter_varies(self):
        """With jitter > 0, results vary across calls."""
        # Run many times; with high jitter we should see different values
        results = [
            exponential_backoff(consecutive=5, base=1.0, jitter=0.5) for _ in range(50)
        ]
        # With ±50% jitter on a fixed raw value, we should see variation
        # (extremely unlikely all 50 calls return the exact same value)
        assert len(set(results)) > 1

    def test_exponential_backoff_custom_base(self):
        """Custom base parameter scales the delay."""
        jitter = 0.0
        # With base=2.0, consecutive=2: 2.0 * 2^1 = 4.0
        assert exponential_backoff(2, base=2.0, jitter=jitter) == 4.0
        # With base=0.1, consecutive=2: 0.1 * 2^1 = 0.2
        assert exponential_backoff(2, base=0.1, jitter=jitter) == 0.2

    def test_exponential_backoff_cap_with_jitter(self):
        """Cap is applied before jitter (not after)."""
        # raw = min(1.0 * 2^4, 10.0) = 10.0
        # With jitter=0.1: result in [9.0, 11.0]
        base = 1.0
        cap = 10.0
        jitter = 0.1
        for _ in range(100):
            result = exponential_backoff(5, base=base, cap=cap, jitter=jitter)
            # Result should be in [9.0, 11.0], not [12.8, 19.2]
            assert 9.0 <= result <= 11.0

    def test_exponential_backoff_matches_formula(self):
        """Result matches the documented formula."""
        # delay = min(base * 2^(consecutive - 1), cap) * uniform(1-jitter, 1+jitter)
        base = 0.5
        consecutive = 3
        cap = 60.0
        jitter = 0.0
        result = exponential_backoff(consecutive, base=base, cap=cap, jitter=jitter)
        expected = min(base * (2 ** (consecutive - 1)), cap)
        assert result == expected

    def test_exponential_backoff_large_consecutive_count(self):
        """Large consecutive counts are capped correctly."""
        base = 1.0
        cap = 60.0
        jitter = 0.0
        # consecutive=100 would give 2^99, definitely exceeds cap
        result = exponential_backoff(100, base=base, cap=cap, jitter=jitter)
        assert result == cap

    def test_exponential_backoff_fractional_base_and_cap(self):
        """Fractional base and cap values work correctly."""
        # base=0.25, consecutive=3: 0.25 * 2^2 = 1.0
        result = exponential_backoff(3, base=0.25, cap=5.5, jitter=0.0)
        assert result == 1.0

    def test_exponential_backoff_very_small_jitter(self):
        """Very small jitter should produce results very close to raw."""
        base = 1.0
        consecutive = 2
        raw = base * (2 ** (consecutive - 1))  # = 2.0
        jitter = 0.001  # ±0.1%
        results = [
            exponential_backoff(consecutive, base=base, jitter=jitter)
            for _ in range(20)
        ]
        for result in results:
            # Should be within 0.2% of raw (2.0)
            assert abs(result - raw) < 0.01
