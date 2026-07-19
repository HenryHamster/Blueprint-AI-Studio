"""Tests for the proof/declaration-aware scorers in app.direction_metrics.

Covers the strategic-DECISION consumption layer added so undeclared/unproven
merges (the "merely delivered" proxy) no longer score the same as machine-proven
work, while staying additive and fail-safe over legacy / read-only data:

  - proof_weighted_merged / _weight_merged: tier weighting, ≤-verified invariant,
    legacy and read-only fall-through to the verified count.
  - _fold_evicted_into_aggregate: lifetime acceptance totals survive eviction and
    exclude pre-contract entries.
  - _agg_proof_weighted_merged: pre-contract verified merges keep full credit.
"""

import pytest

from app.direction_metrics import (
    _PROOF_WEIGHT_DECLARED_UNPROVEN,
    _PROOF_WEIGHT_UNDECLARED,
    _agg_proof_weighted_merged,
    _compute_direction_quality_score,
    _fold_evicted_into_aggregate,
    _weight_merged,
    proof_weighted_merged,
)
from app.direction_outcomes import verified_merged_count


def _entry(*, proven, declared, merged, verified=None, verified_write=None):
    """An acceptance-contract research_history entry."""
    v = merged if verified is None else verified
    vw = v if verified_write is None else verified_write
    return {
        "prs_merged": v,
        "verified_prs_merged": v,
        "acceptance_verified_write_merged": vw,
        "acceptance_merged_write_total": merged,
        "acceptance_proven_merged": proven,
        "acceptance_declared_merged": declared,
    }


class TestProofWeightedMerged:
    def test_all_proven_keeps_full_verified_credit(self):
        e = _entry(proven=3, declared=3, merged=3)
        assert proof_weighted_merged(e) == 3.0

    def test_all_undeclared_is_discounted(self):
        e = _entry(proven=0, declared=0, merged=3)
        assert proof_weighted_merged(e) == 3 * _PROOF_WEIGHT_UNDECLARED

    def test_declared_but_unproven_between_the_two(self):
        e = _entry(proven=0, declared=3, merged=3)
        assert proof_weighted_merged(e) == 3 * _PROOF_WEIGHT_DECLARED_UNPROVEN

    def test_mixed_tiers_sum_correctly(self):
        # 1 proven, 1 declared-unproven, 1 undeclared.
        e = _entry(proven=1, declared=2, merged=3)
        expected = 1.0 + _PROOF_WEIGHT_DECLARED_UNPROVEN + _PROOF_WEIGHT_UNDECLARED
        assert proof_weighted_merged(e) == expected

    def test_never_exceeds_verified(self):
        for proven in range(4):
            for declared in range(proven, 4):
                e = _entry(proven=proven, declared=declared, merged=3)
                assert proof_weighted_merged(e) <= verified_merged_count(e)

    def test_legacy_entry_falls_back_to_verified(self):
        # No acceptance_merged_write_total → proof state unknown → verified count.
        legacy = {"prs_merged": 4, "verified_prs_merged": 4}
        assert proof_weighted_merged(legacy) == 4.0

    def test_read_only_cycle_not_penalized(self):
        # Acceptance-aware cycle with zero merged WRITE tasks (coverage undefined):
        # any non-write verified merges keep full credit, never discounted to 0.
        e = _entry(proven=0, declared=0, merged=0, verified=2, verified_write=0)
        assert proof_weighted_merged(e) == 2.0

    def test_read_only_slice_keeps_full_credit_next_to_write_slice(self):
        e = _entry(proven=1, declared=2, merged=2, verified=4, verified_write=2)
        expected = 2 + (1.0 + _PROOF_WEIGHT_DECLARED_UNPROVEN)
        assert proof_weighted_merged(e) == expected

    def test_weight_merged_clamps_inconsistent_inputs(self):
        # proven/declared above verified are clamped so the result never inflates.
        assert _weight_merged(2, 5, 9, 9) == 2.0
        assert _weight_merged(0, 5, 3, 3) == 0.0


class TestFoldAcceptanceTotals:
    def test_acceptance_totals_accumulate_for_aware_entries(self):
        evicted = [
            _entry(proven=2, declared=3, merged=4),
            _entry(proven=1, declared=1, merged=2),
        ]
        agg = _fold_evicted_into_aggregate(None, evicted)
        assert agg["total_acceptance_verified_merged"] == 6  # 4 + 2 verified
        assert agg["total_acceptance_verified_write_merged"] == 6
        assert agg["total_acceptance_proven_merged"] == 3
        assert agg["total_acceptance_declared_merged"] == 4
        assert agg["total_acceptance_merged_write_total"] == 6

    def test_legacy_entries_excluded_from_acceptance_totals(self):
        evicted = [{"prs_merged": 5, "verified_prs_merged": 5}]  # pre-contract
        agg = _fold_evicted_into_aggregate(None, evicted)
        assert agg["total_acceptance_verified_merged"] == 0
        assert agg["total_acceptance_verified_write_merged"] == 0
        assert agg["total_acceptance_merged_write_total"] == 0
        # But the verified merge is still counted in the unweighted lifetime total.
        assert agg["total_verified_prs_merged"] == 5


class TestAggProofWeightedMerged:
    def test_legacy_verified_merges_keep_full_credit(self):
        # 10 lifetime verified merges; only 4 are acceptance-aware and undeclared.
        # The 6 pre-contract merges keep full credit; only the 4 are discounted.
        agg = {
            "total_verified_prs_merged": 10,
            "total_acceptance_verified_merged": 4,
            "total_acceptance_verified_write_merged": 4,
            "total_acceptance_merged_write_total": 4,
            "total_acceptance_proven_merged": 0,
            "total_acceptance_declared_merged": 0,
        }
        expected = 6 + 4 * _PROOF_WEIGHT_UNDECLARED
        assert _agg_proof_weighted_merged(agg) == expected
        assert _agg_proof_weighted_merged(agg) <= agg["total_verified_prs_merged"]

    def test_read_only_slice_keeps_full_credit_in_aggregate(self):
        agg = {
            "total_verified_prs_merged": 10,
            "total_acceptance_verified_merged": 4,
            "total_acceptance_verified_write_merged": 2,
            "total_acceptance_merged_write_total": 2,
            "total_acceptance_proven_merged": 1,
            "total_acceptance_declared_merged": 2,
        }
        expected = 6 + 2 + (1.0 + _PROOF_WEIGHT_DECLARED_UNPROVEN)
        assert _agg_proof_weighted_merged(agg) == expected

    def test_no_acceptance_key_falls_back_to_total_verified(self):
        agg = {"total_verified_prs_merged": 7}
        assert _agg_proof_weighted_merged(agg) == 7.0

    def test_all_proven_aggregate_equals_verified(self):
        agg = {
            "total_verified_prs_merged": 5,
            "total_acceptance_verified_merged": 5,
            "total_acceptance_verified_write_merged": 5,
            "total_acceptance_merged_write_total": 5,
            "total_acceptance_proven_merged": 5,
            "total_acceptance_declared_merged": 5,
        }
        assert _agg_proof_weighted_merged(agg) == 5.0


class TestFoldDecompositionDenominator:
    """A None-rate decomposition plan must not inflate subtask_count_sum.

    subtask_count_sum is the weighted-average DENOMINATOR consumed by
    spawn_calibration's scope-upgrade suppression. A plan whose
    subtask_merge_rate is None (subtasks exist but no merged/failed-nc
    outcome yet) contributes no numerator, so it must contribute no
    denominator either — otherwise the weighted merge-rate is biased down.
    """

    def test_none_rate_plan_excluded_from_denominator(self):
        evicted = [
            {
                "timestamp": "2025-01-01T00:00:00",
                "decomposition_quality": [
                    {"subtask_count": 4, "subtask_merge_rate": 0.5},
                    {"subtask_count": 6, "subtask_merge_rate": None},
                    {"subtask_count": 2, "subtask_merge_rate": 1.0},
                ],
            }
        ]
        agg = _fold_evicted_into_aggregate(None, evicted)
        dq = agg["decomposition_quality"]
        # total_decompositions counts ALL folded plans (it is a counter, not a
        # denominator), so the None-rate plan is still counted here.
        assert dq["total_decompositions"] == 3
        # ...but the None-rate plan (count=6) is excluded from the denominator.
        assert dq["subtask_count_sum"] == 6  # 4 + 2, NOT 4 + 6 + 2
        # Numerator only accrues from numeric-rate plans: 0.5*4 + 1.0*2 = 4.0
        assert dq["subtask_merge_rate_weighted_sum"] == pytest.approx(4.0)
        # The resulting weighted average reflects only numeric-rate plans.
        weighted_avg = (
            dq["subtask_merge_rate_weighted_sum"] / dq["subtask_count_sum"]
        )
        assert weighted_avg == pytest.approx(4.0 / 6)


class TestComputeDirectionQualityScoreQualityGrader:
    """_compute_direction_quality_score: quality_grader_verdict signal (f)."""

    def _direction(self, history):
        return {"research_history": history}

    def test_all_passed_verdicts_raise_score(self):
        history = [
            {"quality_grader_verdict": {"passed": True, "reason": "ok"}},
            {"quality_grader_verdict": {"passed": True, "reason": "ok"}},
        ]
        d = self._direction(history)
        score = _compute_direction_quality_score(d)
        # All verdicts pass → quality grader signal is 1.0, which raises the average.
        assert score > 0.5

    def test_all_failed_verdicts_lower_score(self):
        history = [
            {"quality_grader_verdict": {"passed": False, "reason": "bad"}},
            {"quality_grader_verdict": {"passed": False, "reason": "bad"}},
        ]
        d = self._direction(history)
        score = _compute_direction_quality_score(d)
        # All verdicts fail → quality grader signal is 0.0, which lowers the average.
        assert score < 0.5

    def test_legacy_entries_without_verdict_are_neutral(self):
        history = [
            {"tasks_created": 2, "prs_merged": 1},
            {"tasks_created": 3, "prs_merged": 2},
        ]
        d = self._direction(history)
        score_without = _compute_direction_quality_score(d)
        # Adding quality_grader_verdict to all entries raises the score.
        history_with = [
            {**e, "quality_grader_verdict": {"passed": True, "reason": "ok"}}
            for e in history
        ]
        score_with = _compute_direction_quality_score(self._direction(history_with))
        # Without verdicts: signal (f) is absent → score is neutral on that component.
        # With all-pass verdicts: signal (f) = 1.0 → score increases.
        assert score_with >= score_without

    def test_no_history_returns_neutral(self):
        d = self._direction([])
        assert _compute_direction_quality_score(d) == pytest.approx(0.5)

    def test_malformed_verdict_entry_ignored(self):
        history = [
            {"quality_grader_verdict": "not-a-dict"},
            {"quality_grader_verdict": {"passed": True, "reason": "ok"}},
        ]
        d = self._direction(history)
        # Malformed entry is skipped; only the valid entry contributes.
        score = _compute_direction_quality_score(d)
        assert 0.0 <= score <= 1.0
