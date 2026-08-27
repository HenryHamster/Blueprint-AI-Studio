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
    PRODUCTIVITY_REASON_CODES,
    PRODUCTIVITY_REASON_DETAIL_MAX,
    PRODUCTIVITY_REASON_HISTORY_MAX,
    _agg_proof_weighted_merged,
    _compute_direction_quality_score,
    _fold_evicted_into_aggregate,
    _weight_merged,
    proof_weighted_merged,
    record_direction_productivity,
    unproductive_streak_summary,
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
# ─────────────────────────────────────────────────────────────────────────────
# record_direction_productivity — a productivity judgment always carries its WHY
# ─────────────────────────────────────────────────────────────────────────────


class TestRecordDirectionProductivity:
    """A bare ``last_cycle_productive: false`` is unactionable: it cannot
    distinguish a direction that spawned nothing from one that spawned plenty and
    merged nothing, and the two want opposite responses. Every writer records the
    reason alongside the boolean."""

    def test_productivity_reason_bounds_are_pinned(self):
        # LITERAL pins. The ring is persisted inside the project's directions
        # JSONB and rendered into the strategist's bounded per-direction stat
        # block, so growing either bound is a payload-size decision, not an
        # implementation detail.
        assert PRODUCTIVITY_REASON_HISTORY_MAX == 10
        assert PRODUCTIVITY_REASON_DETAIL_MAX == 300
        assert PRODUCTIVITY_REASON_CODES == (
            "spawned_children",
            "non_spawn_findings",
            "no_children_no_findings",
            "no_actionable_findings",
            "research_cycle_failed",
        )

    def test_unproductive_cycle_records_reason_and_advances_counter(self):
        d: dict = {}
        consecutive = record_direction_productivity(
            d,
            productive=False,
            reason_code="no_actionable_findings",
            detail="research cycle completed with zero actionable findings",
            task_id="11111111-2222-3333-4444-555555555555",
            at_iso="2026-08-26T00:00:00+00:00",
        )
        assert consecutive == 1
        assert d["last_cycle_productive"] is False
        assert d["consecutive_unproductive_cycles"] == 1
        reason = d["last_productivity_reason"]
        assert reason["productive"] is False
        assert reason["reason_code"] == "no_actionable_findings"
        assert "zero actionable findings" in reason["detail"]
        assert reason["consecutive_unproductive_cycles"] == 1
        assert reason["task_id"] == "11111111-2222-3333-4444-555555555555"
        assert reason["at"] == "2026-08-26T00:00:00+00:00"
        assert d["productivity_reason_history"] == [reason]

    def test_productive_cycle_resets_the_counter_and_records_why(self):
        d = {"consecutive_unproductive_cycles": 4}
        consecutive = record_direction_productivity(
            d,
            productive=True,
            reason_code="spawned_children",
            detail="research cycle spawned 2 child task(s)",
        )
        assert consecutive == 0
        assert d["consecutive_unproductive_cycles"] == 0
        assert d["last_cycle_productive"] is True
        assert d["last_productivity_reason"]["productive"] is True
        assert d["last_productivity_reason"]["reason_code"] == "spawned_children"
        # No task id / timestamp supplied → the optional keys stay absent rather
        # than being written as null.
        assert "task_id" not in d["last_productivity_reason"]
        assert "at" not in d["last_productivity_reason"]

    def test_bump_false_records_the_reason_without_double_counting(self):
        """An idempotent re-stamp of the SAME research task must not advance the
        streak, but must still leave a stated cause behind."""
        d: dict = {}
        record_direction_productivity(
            d, productive=False, reason_code="no_actionable_findings", detail="first"
        )
        assert d["consecutive_unproductive_cycles"] == 1
        consecutive = record_direction_productivity(
            d,
            productive=False,
            reason_code="no_actionable_findings",
            detail="re-stamp of the same cycle",
            bump=False,
        )
        assert consecutive == 1
        assert d["consecutive_unproductive_cycles"] == 1
        assert d["last_productivity_reason"]["detail"] == "re-stamp of the same cycle"
        assert d["productivity_reason_history"] == [d["last_productivity_reason"]]

    def test_history_ring_is_bounded_and_detail_truncated(self):
        d: dict = {}
        for i in range(PRODUCTIVITY_REASON_HISTORY_MAX + 5):
            record_direction_productivity(
                d,
                productive=False,
                reason_code="no_children_no_findings",
                detail=f"cycle {i} " + ("x" * 500),
            )
        history = d["productivity_reason_history"]
        assert len(history) == PRODUCTIVITY_REASON_HISTORY_MAX
        # The ring keeps the MOST RECENT judgments.
        assert history[-1]["consecutive_unproductive_cycles"] == (
            PRODUCTIVITY_REASON_HISTORY_MAX + 5
        )
        assert all(
            len(record["detail"]) <= PRODUCTIVITY_REASON_DETAIL_MAX
            for record in history
        )

    def test_legacy_direction_with_non_list_history_is_repaired(self):
        d = {"productivity_reason_history": "not-a-list"}
        record_direction_productivity(
            d, productive=False, reason_code="research_cycle_failed", detail="boom"
        )
        assert d["productivity_reason_history"] == [d["last_productivity_reason"]]


class TestUnproductiveStreakSummary:
    def test_zero_streak_has_no_summary(self):
        assert unproductive_streak_summary({}) is None
        assert unproductive_streak_summary({"consecutive_unproductive_cycles": 0}) is None

    def test_summary_names_the_streak_and_its_distinct_reasons(self):
        d: dict = {}
        record_direction_productivity(
            d, productive=False, reason_code="no_children_no_findings", detail="a"
        )
        record_direction_productivity(
            d, productive=False, reason_code="no_actionable_findings", detail="b"
        )
        record_direction_productivity(
            d, productive=False, reason_code="no_actionable_findings", detail="c"
        )
        summary = unproductive_streak_summary(d)
        assert summary is not None
        assert "3 consecutive unproductive cycle(s)" in summary
        # Most-recent-first, de-duplicated.
        assert "reasons: no_actionable_findings, no_children_no_findings" in summary

    def test_summary_stops_at_the_last_productive_cycle(self):
        d: dict = {}
        record_direction_productivity(
            d, productive=False, reason_code="research_cycle_failed", detail="old"
        )
        record_direction_productivity(
            d, productive=True, reason_code="spawned_children", detail="recovered"
        )
        record_direction_productivity(
            d, productive=False, reason_code="no_actionable_findings", detail="new"
        )
        summary = unproductive_streak_summary(d)
        assert summary is not None
        assert "1 consecutive unproductive cycle(s)" in summary
        assert "no_actionable_findings" in summary
        # The pre-recovery failure is NOT part of the current streak.
        assert "research_cycle_failed" not in summary

    def test_legacy_direction_with_counter_but_no_ring_still_summarizes(self):
        summary = unproductive_streak_summary({"consecutive_unproductive_cycles": 7})
        assert summary == "7 consecutive unproductive cycle(s)"
