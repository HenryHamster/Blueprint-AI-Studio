"""Unit tests for the statistical helpers in account_model_regression.py.

These tests exercise the Wilson CI helpers and the regression-detection gate
in isolation (no DB required). They are designed to fail BEFORE the C14 fix
and pass AFTER it.
"""

from __future__ import annotations

from app.account_model_regression import (
    _ACCOUNT_MIN_SAMPLE,
    _DISTINCT_ACCOUNTS_MIN,
    _wilson_lower,
    _wilson_upper,
)


# ── Wilson CI helpers ────────────────────────────────────────────────────────


class TestWilsonBounds:
    """Sanity-check the Wilson score interval implementation."""

    def test_zero_denominator_lower_is_zero(self):
        assert _wilson_lower(0, 0) == 0.0

    def test_zero_denominator_upper_is_one(self):
        assert _wilson_upper(0, 0) == 1.0

    def test_all_success_lower_is_positive(self):
        # With many successes, lower bound should be high.
        lb = _wilson_lower(100, 100)
        assert lb > 0.95, f"Expected >0.95, got {lb}"

    def test_all_failure_upper_is_low(self):
        # With many failures, upper bound should be low.
        ub = _wilson_upper(0, 100)
        assert ub < 0.05, f"Expected <0.05, got {ub}"

    def test_bounds_straddle_observed_rate(self):
        # The CI always contains the observed proportion.
        for k, n in [(3, 10), (7, 10), (50, 100), (0, 5), (5, 5)]:
            lb = _wilson_lower(k, n)
            ub = _wilson_upper(k, n)
            assert lb <= (k / n) <= ub, (
                f"CI [{lb:.4f}, {ub:.4f}] does not contain {k}/{n}={k / n:.4f}"
            )

    def test_lower_lt_upper(self):
        for k, n in [(3, 10), (7, 10), (50, 100)]:
            assert _wilson_lower(k, n) < _wilson_upper(k, n)

    def test_small_sample_wide_interval(self):
        # With only 3 observations, even 0/3 should have a non-trivial upper bound.
        ub = _wilson_upper(0, 3)
        assert ub > 0.3, (
            f"Upper bound for 0/3 should be wide (got {ub:.4f}), "
            "otherwise sparse samples can be falsely penalized"
        )

    def test_degenerate_two_account_n3_not_statistically_significant(self):
        """Regression guard: a realistic 2-account, n=3 split must not be significant.

        This is the documented HenryHamster false-positive class. Under the old
        code the comparison was raw account_rate vs median-of-two, which fired
        on any chance run of failures at n=3. The statistical gate instead
        compares the account's Wilson upper bound against the pool's Wilson
        lower bound, and at n=3 the intervals are so wide they overlap for any
        realistic (non-perfectly-separated) split.

        Concretely: acct_bad has 1/3 (rate≈0.33) and the comparator pool is the
        single other account at 3/3 (rate=1.0). The Wilson upper bound of 1/3 is
        ~0.75 and the Wilson lower bound of 3/3 is ~0.53, so the upper bound is
        NOT below the lower bound — the gap is not statistically significant and
        no regression can fire.

        NB: the pathological *perfect-separation* extreme (0/3 vs 3/3) does clear
        the Wilson gate, but it can never reach this test in production because
        an n=3 cell is below _ACCOUNT_MIN_SAMPLE (10) and is filtered out long
        before the significance comparison runs — see
        ``test_account_min_sample_at_least_10``. The Wilson gate is the
        second line of defence for cells that *do* clear the sample floor.
        """
        # acct_bad: 1/3 (a realistic underperformer, not perfect separation)
        acct_upper = _wilson_upper(1, 3)
        # pool (acct_good, the only comparator): 3/3
        pool_lower = _wilson_lower(3, 3)
        # Key assertion: the gap is NOT statistically significant at n=3.
        assert acct_upper >= pool_lower, (
            f"With 1/3 vs 3/3 the CIs must overlap (acct_upper={acct_upper:.4f} "
            f">= pool_lower={pool_lower:.4f}), but got a gap. This would cause "
            "a false-positive quarantine on tiny sample sizes."
        )


# ── _ACCOUNT_MIN_SAMPLE and _DISTINCT_ACCOUNTS_MIN guards ────────────────────


class TestRegressionConstants:
    """The new constants must meet the minimum guarantees required by the fix."""

    def test_account_min_sample_at_least_10(self):
        assert _ACCOUNT_MIN_SAMPLE >= 10, (
            f"_ACCOUNT_MIN_SAMPLE={_ACCOUNT_MIN_SAMPLE} is too low; "
            "must be >= 10 to avoid false positives on sparse samples"
        )

    def test_distinct_accounts_min_at_least_3(self):
        assert _DISTINCT_ACCOUNTS_MIN >= 3, (
            f"_DISTINCT_ACCOUNTS_MIN={_DISTINCT_ACCOUNTS_MIN} is too low; "
            "must be >= 3 so the pool comparator is never the degenerate "
            "average-of-two"
        )


# ── Statistical gate: large-sample significant regression fires ──────────────


class TestStatisticalGateLargeSample:
    """Verify the statistical gate passes through clearly significant regressions."""

    def test_clearly_bad_account_fires(self):
        """With 3+ accounts and 20+ tasks, a genuinely bad account is still caught.

        acct_bad: 0/20 = 0.0; pool (2 good accounts): 40/40 = 1.0
        Wilson upper of 0/20 << Wilson lower of 40/40 → statistically significant.
        """
        acct_upper = _wilson_upper(0, 20)
        pool_lower = _wilson_lower(40, 40)
        assert acct_upper < pool_lower, (
            f"A clearly bad account (0/20 vs 40/40 pool) must be caught by the "
            f"statistical gate (acct_upper={acct_upper:.4f} < pool_lower={pool_lower:.4f})"
        )

    def test_borderline_account_does_not_fire(self):
        """A borderline-bad account with overlapping CIs should NOT fire.

        acct_bad: 7/10 = 0.70; pool: 18/20 = 0.90
        This is a modest gap — the CIs are likely to overlap at these sample sizes.
        """
        acct_upper = _wilson_upper(7, 10)
        pool_lower = _wilson_lower(18, 20)
        # At n=10/20 the CI is wide; the upper of 7/10 should still overlap with
        # the lower of 18/20. If this flips, the thresholds need recalibration.
        assert acct_upper >= pool_lower, (
            f"Borderline case (7/10 vs 18/20) should not fire statistically: "
            f"acct_upper={acct_upper:.4f} should be >= pool_lower={pool_lower:.4f}"
        )
