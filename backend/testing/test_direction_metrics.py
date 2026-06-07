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

from app.direction_metrics import (
    _PROOF_WEIGHT_DECLARED_UNPROVEN,
    _PROOF_WEIGHT_UNDECLARED,
    _agg_proof_weighted_merged,
    _fold_evicted_into_aggregate,
    _weight_merged,
    proof_weighted_merged,
)
from app.direction_outcomes import verified_merged_count


def _entry(*, proven, declared, merged, verified=None):
    """An acceptance-contract research_history entry."""
    v = merged if verified is None else verified
    return {
        "prs_merged": v,
        "verified_prs_merged": v,
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
        e = _entry(proven=0, declared=0, merged=0, verified=2)
        assert proof_weighted_merged(e) == 2.0

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
        assert agg["total_acceptance_proven_merged"] == 3
        assert agg["total_acceptance_declared_merged"] == 4
        assert agg["total_acceptance_merged_write_total"] == 6

    def test_legacy_entries_excluded_from_acceptance_totals(self):
        evicted = [{"prs_merged": 5, "verified_prs_merged": 5}]  # pre-contract
        agg = _fold_evicted_into_aggregate(None, evicted)
        assert agg["total_acceptance_verified_merged"] == 0
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
            "total_acceptance_merged_write_total": 4,
            "total_acceptance_proven_merged": 0,
            "total_acceptance_declared_merged": 0,
        }
        expected = 6 + 4 * _PROOF_WEIGHT_UNDECLARED
        assert _agg_proof_weighted_merged(agg) == expected
        assert _agg_proof_weighted_merged(agg) <= agg["total_verified_prs_merged"]

    def test_no_acceptance_key_falls_back_to_total_verified(self):
        agg = {"total_verified_prs_merged": 7}
        assert _agg_proof_weighted_merged(agg) == 7.0

    def test_all_proven_aggregate_equals_verified(self):
        agg = {
            "total_verified_prs_merged": 5,
            "total_acceptance_verified_merged": 5,
            "total_acceptance_merged_write_total": 5,
            "total_acceptance_proven_merged": 5,
            "total_acceptance_declared_merged": 5,
        }
        assert _agg_proof_weighted_merged(agg) == 5.0
