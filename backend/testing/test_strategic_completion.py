"""Tests for pure direction lifecycle helpers in app.strategic_completion.

Covers:
  - _check_direction_health: saturation, zero-merge, high-failure-rate rules
  - _check_direction_completion: cycle/PR thresholds and recent-activity check
  - _is_direction_duplicate: word-overlap with existing and rejected proposals
  - _record_strategic_task_failure: circuit-breaker increment and cycle history update
"""

import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.time_utils import _utcnow
from app.strategic_completion import (
    _check_direction_health,
    _check_direction_completion,
    _is_direction_duplicate,
    _record_strategic_task_failure,
    _check_direction_starvation,
    _is_code_failure_pause_reason,
    _build_direction_failure_summary,
    _build_creation_prediction,
    _build_retirement_metrics,
    _collect_creation_predictions,
    _collect_retirement_metrics,
    _collect_rationales,
    _extract_cycle_reasoning,
    _extract_rationale_from_notes,
    _compute_churn_metrics,
    _compute_lifetime_burn_metrics,
    _direction_still_productive,
    _already_built_signature,
    already_built_completion_suggestion,
    _compute_outcome_progress_signal,
    record_outcome_progress_snapshot,
    _check_outcome_scaffolding,
    _apply_evidence_progress_verdicts,
    _reset_scaffolding_state_on_outcome_change,
    EVIDENCE_PROGRESS_CONTEXT_KEY,
    _CREATED_NOTE_PREFIX,
    _RETIRED_NOTE_PREFIX,
)
from app.strategic_spawning import (
    _build_previous_decision_outcomes,
    _build_past_retirement_critiques,
)
from app.strategic_analysis import _build_outcome_evaluation
from app.direction_lifecycle import _reset_outcome_detection_state
from app.models import Project, Task


# ── Helpers ──────────────────────────────────────────────────────────────────


def _history(
    n,
    tasks_created=2,
    tasks_completed=1,
    tasks_failed=0,
    tasks_failed_infra=0,
    prs_merged=1,
):
    """Build a list of n identical research_history cycle entries."""
    return [
        {
            "tasks_created": tasks_created,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "tasks_failed_infra": tasks_failed_infra,
            "prs_merged": prs_merged,
        }
        for _ in range(n)
    ]


def _direction(**kwargs):
    """Build a direction dict with sensible defaults."""
    base = {
        "possibly_saturated": False,
        "research_history": [],
    }
    base.update(kwargs)
    return base


# ── _check_direction_health ──────────────────────────────────────────────────


class TestCheckDirectionHealth:
    # --- Rule 1: Saturation ---

    def test_saturation_exactly_5_cycles_all_le1_task_returns_message(self):
        d = _direction(
            possibly_saturated=True,
            research_history=_history(5, tasks_created=1),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Saturated" in result

    def test_saturation_requires_possibly_saturated_flag(self):
        d = _direction(
            possibly_saturated=False,
            research_history=_history(5, tasks_created=0),
        )
        # possibly_saturated is False — rule 1 must not fire
        result = _check_direction_health(d)
        assert result is None

    def test_saturation_requires_5_plus_cycles(self):
        d = _direction(
            possibly_saturated=True,
            research_history=_history(4, tasks_created=0),
        )
        result = _check_direction_health(d)
        assert result is None

    def test_saturation_only_checks_last_5_cycles(self):
        # 6 cycles total; first one had 5 tasks, last 5 all ≤1
        history = _history(1, tasks_created=5) + _history(5, tasks_created=0)
        d = _direction(possibly_saturated=True, research_history=history)
        result = _check_direction_health(d)
        assert result is not None
        assert "Saturated" in result

    def test_saturation_not_triggered_when_last5_have_tasks(self):
        # Last cycle has 2 tasks → rule should not fire
        history = _history(4, tasks_created=0) + _history(1, tasks_created=2)
        d = _direction(possibly_saturated=True, research_history=history)
        result = _check_direction_health(d)
        assert result is None

    # --- Rule 2: Zero-merge ---

    def test_zero_merge_5_cycles_all_finished_returns_message(self):
        # All cycles: 0 PRs merged, last cycle fully finished
        d = _direction(
            research_history=_history(
                5, prs_merged=0, tasks_created=2, tasks_completed=1, tasks_failed=1
            ),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Unproductive" in result

    def test_zero_merge_requires_5_plus_cycles(self):
        d = _direction(
            research_history=_history(
                4, prs_merged=0, tasks_created=2, tasks_completed=2, tasks_failed=0
            ),
        )
        result = _check_direction_health(d)
        assert result is None

    def test_zero_merge_skipped_when_tasks_still_in_flight(self):
        # tasks_created=3, tasks_completed=1, tasks_failed=1 → finished=2 < created=3
        history = [
            *_history(4, prs_merged=0),
            {
                "tasks_created": 3,
                "tasks_completed": 1,
                "tasks_failed": 1,
                "tasks_failed_infra": 0,
                "prs_merged": 0,
            },
        ]
        d = _direction(research_history=history)
        result = _check_direction_health(d)
        assert result is None

    def test_zero_merge_allowed_when_last_cycle_has_zero_tasks(self):
        # tasks_created=0 → guard treats this as finished (0 == 0)
        history = [
            *_history(4, prs_merged=0),
            {
                "tasks_created": 0,
                "tasks_completed": 0,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 0,
            },
        ]
        d = _direction(research_history=history)
        result = _check_direction_health(d)
        assert result is not None
        assert "Unproductive" in result

    def test_zero_merge_not_fired_when_some_prs_merged(self):
        # 5 cycles but 1 merged PR in cycle 3
        history = [
            *_history(2, prs_merged=0),
            *_history(1, prs_merged=1),
            *_history(2, prs_merged=0),
        ]
        d = _direction(
            research_history=[
                *history,
                {
                    "tasks_created": 2,
                    "tasks_completed": 2,
                    "tasks_failed": 0,
                    "tasks_failed_infra": 0,
                    "prs_merged": 0,
                },
            ],
        )
        # total_merged > 0 → rule must not fire
        result = _check_direction_health(d)
        assert result is None

    # --- Rule 3: High code failure rate ---

    def test_high_failure_rate_above_threshold_returns_message(self):
        # 3 cycles: 3 tasks each, 3 failed, 0 infra → 9/9 = 100% > 70%
        d = _direction(
            research_history=_history(
                3, tasks_created=3, tasks_failed=3, tasks_failed_infra=0, prs_merged=0
            ),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "failure rate" in result.lower()

    def test_high_failure_rate_exactly_at_threshold_not_triggered(self):
        # 10 tasks total, 7 code failures → 70% == threshold, NOT > 70%
        d = _direction(
            research_history=[
                {
                    "tasks_created": 10,
                    "tasks_completed": 3,
                    "tasks_failed": 7,
                    "tasks_failed_infra": 0,
                    "prs_merged": 1,
                },
            ]
            * 3,
        )
        result = _check_direction_health(d)
        # 70% is not strictly > 70%, so rule 3 should not fire
        assert result is None

    def test_high_failure_rate_infra_excluded(self):
        # 10 tasks: 6 failed, all 6 are infra (0 code failures).
        # Rule 3: 0% code failure — doesn't fire. Rule 4: code_failed=0 — doesn't fire.
        # Pins the infra-exclusion principle for both rules.
        d = _direction(
            research_history=[
                {
                    "tasks_created": 10,
                    "tasks_completed": 4,
                    "tasks_failed": 6,
                    "tasks_failed_infra": 6,
                    "prs_merged": 0,
                },
            ]
            * 3,
        )
        result = _check_direction_health(d)
        assert result is None

    def test_high_failure_rate_requires_3_plus_cycles(self):
        d = _direction(
            research_history=_history(
                2, tasks_created=3, tasks_failed=3, tasks_failed_infra=0, prs_merged=0
            ),
        )
        result = _check_direction_health(d)
        assert result is None

    def test_high_failure_rate_only_uses_last_3_cycles(self):
        # 5 cycles: first 2 have 100% failure; last 3 are healthy
        bad = _history(
            2, tasks_created=5, tasks_failed=5, tasks_failed_infra=0, prs_merged=0
        )
        good = _history(
            3, tasks_created=5, tasks_failed=1, tasks_failed_infra=0, prs_merged=2
        )
        d = _direction(research_history=bad + good)
        result = _check_direction_health(d)
        assert result is None

    # --- No health issue ---

    def test_healthy_direction_returns_none(self):
        d = _direction(
            possibly_saturated=False,
            research_history=_history(
                6, tasks_created=3, prs_merged=2, tasks_failed=0, tasks_failed_infra=0
            ),
        )
        result = _check_direction_health(d)
        assert result is None

    def test_empty_history_returns_none(self):
        result = _check_direction_health(_direction(research_history=[]))
        assert result is None

    def test_missing_research_history_key_returns_none(self):
        result = _check_direction_health({"possibly_saturated": False})
        assert result is None

    # --- Rule 5: High non-merge churn ---

    def test_rule5_churn_guardrail_fires_at_default_threshold(self):
        # 5 cycles: 3 completed, 0 merged each
        # non_merge_finished = max(0, 15+0-0) = 15 == threshold, merge_rate = 0%
        d = _direction(
            research_history=_history(
                5, tasks_created=5, tasks_completed=3, tasks_failed=0, prs_merged=0
            )
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Churn guardrail" in result
        assert "15" in result
        assert "0%" in result

    def test_rule5_not_triggered_when_merge_rate_adequate(self):
        # 5 cycles: 8 completed, 3 merged → merge_rate ≈ 38%, well above 10% minimum
        # non_merge_finished = max(0, 40+0-15) = 25 >= 15, but merge_rate is too high
        d = _direction(
            research_history=_history(
                5, tasks_created=10, tasks_completed=8, tasks_failed=0, prs_merged=3
            )
        )
        result = _check_direction_health(d)
        assert result is None

    def test_rule5_not_triggered_below_non_merge_threshold(self):
        # 5 cycles: 2 completed, 0 merged → non_merge_finished = max(0,10+0-0) = 10 < 15
        # tasks_created=4 with tasks_completed=2 → still in-flight guard also prevents Rule 2
        d = _direction(
            research_history=_history(
                5, tasks_created=4, tasks_completed=2, tasks_failed=0, prs_merged=0
            )
        )
        result = _check_direction_health(d)
        assert result is None

    def test_rule5_config_threshold_honored(self, mocker):
        # Lower threshold to 5; direction with non_merge_finished=15 now fires
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"direction_churn_non_merge_threshold": 5},
            clear=False,
        )
        d = _direction(
            research_history=_history(
                5, tasks_created=4, tasks_completed=3, tasks_failed=0, prs_merged=0
            )
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Churn guardrail" in result

    def test_rule5_config_window_honored(self, mocker):
        # window=3 focuses on last 3 bad cycles, ignoring earlier good ones that
        # would raise the merge rate high enough to suppress the rule
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"direction_churn_window_cycles": 3},
            clear=False,
        )
        good = _history(
            7, tasks_created=5, tasks_completed=4, tasks_failed=0, prs_merged=3
        )
        bad = _history(
            3, tasks_created=7, tasks_completed=6, tasks_failed=0, prs_merged=0
        )
        d = _direction(research_history=good + bad)
        # With window=3 (bad only): non_merge_finished=18, merge_rate=0% → fires
        # With default window=5 (2 good + 3 bad): merge_rate≈23% → would not fire
        result = _check_direction_health(d)
        assert result is not None
        assert "Churn guardrail" in result

    def test_rule5_pause_reason_includes_machine_readable_counts(self):
        # Verify the reason includes window_cycles and merged count for operator tooling
        d = _direction(
            research_history=_history(
                5, tasks_created=5, tasks_completed=4, tasks_failed=0, prs_merged=0
            )
        )
        result = _check_direction_health(d)
        assert result is not None
        # non_merge_finished=20, merged=0, window_cycles=5
        assert "0 merged" in result
        assert "5 cycles" in result

    # --- Rule 6: Lifetime cumulative burn backstop ---

    def test_rule6_fires_when_aggregate_pushes_lifetime_over_threshold(self):
        # Window: 10 non-merging tasks (< Rule-5 threshold of 15) — Rule 5 stays silent.
        # Aggregate adds 150 more → lifetime_non_merge=160 ≥ 150 at 0% rate → Rule 6 fires.
        d = _direction(
            research_history=_history(
                5, tasks_created=3, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate={
                "total_tasks_completed": 150,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
                "total_prs_merged": 0,
                "total_cycles": 30,
            },
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Lifetime burn backstop" in result

    def test_rule6_not_triggered_at_healthy_lifetime_merge_rate(self):
        # Window: 5 merged in 5 cycles keeps Rule-5 quiet.
        # Aggregate: 25 merged across 200 completed → lifetime merge rate ≈14% > 10% → Rule 6 off.
        d = _direction(
            research_history=_history(
                5, tasks_created=3, tasks_completed=2, tasks_failed=0, prs_merged=1
            ),
            history_aggregate={
                "total_tasks_completed": 200,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
                "total_prs_merged": 25,
                "total_cycles": 40,
            },
        )
        result = _check_direction_health(d)
        assert result is None or "Lifetime burn backstop" not in result

    def test_rule6_independent_of_rolling_window_rule5(self):
        # Window non_merge=10 < Rule-5 threshold (15) → Rule 5 does NOT fire.
        # Lifetime aggregate pushes total to 160 non-merging at 0% → Rule 6 fires.
        d = _direction(
            research_history=_history(
                5, tasks_created=3, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate={
                "total_tasks_completed": 150,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
                "total_prs_merged": 0,
                "total_cycles": 30,
            },
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Lifetime burn backstop" in result
        assert "Churn guardrail" not in result

    def test_rule6_config_threshold_honored(self, mocker):
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"direction_lifetime_non_merge_threshold": 50},
            clear=False,
        )
        # Window: 10 non-merge (< Rule-5 default 15). Aggregate: 50 completed → lifetime=60 ≥ 50.
        d = _direction(
            research_history=_history(
                5, tasks_created=3, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate={
                "total_tasks_completed": 50,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
                "total_prs_merged": 0,
                "total_cycles": 10,
            },
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Lifetime burn backstop" in result

    def test_rule6_message_includes_machine_readable_counts(self):
        # lifetime_non_merge_finished=160, lifetime_merged=0, rate=0%
        d = _direction(
            research_history=_history(
                5, tasks_created=3, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate={
                "total_tasks_completed": 150,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
                "total_prs_merged": 0,
                "total_cycles": 30,
            },
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "160" in result
        assert "0 merged" in result
        assert "0%" in result

    def test_rule6_not_classified_as_code_failure(self):
        d = _direction(
            research_history=_history(
                5, tasks_created=3, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate={
                "total_tasks_completed": 150,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
                "total_prs_merged": 0,
                "total_cycles": 30,
            },
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Lifetime burn backstop" in reason
        assert _is_code_failure_pause_reason(reason) is False


# ── _compute_churn_metrics ────────────────────────────────────────────────────


class TestComputeChurnMetrics:
    def test_returns_none_when_fewer_than_3_cycles(self):
        d = _direction(
            research_history=_history(
                2, tasks_completed=5, tasks_failed=0, prs_merged=0
            )
        )
        assert _compute_churn_metrics(d) is None

    def test_returns_none_for_empty_history(self):
        assert _compute_churn_metrics(_direction(research_history=[])) is None

    def test_basic_non_merge_computation(self):
        # 3 cycles: 5 completed, 0 merged each
        # non_merge_finished = max(0, 15+0-0) = 15
        d = _direction(
            research_history=_history(
                3, tasks_completed=5, tasks_failed=0, prs_merged=0
            )
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["non_merge_finished"] == 15
        assert m["merged"] == 0
        assert m["window_cycles"] == 3
        assert m["merge_rate"] == 0.0

    def test_non_merge_finished_floored_at_zero(self):
        # No completed or failed tasks at all — floor prevents negative values
        d = _direction(
            research_history=_history(
                3, tasks_completed=0, tasks_failed=0, prs_merged=0
            )
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["non_merge_finished"] == 0

    def test_code_failed_tasks_counted_as_finished_non_merge(self):
        # 3 cycles: 0 completed, 4 code-failed, 0 merged
        # code_failed = 12; non_merge_finished = max(0, 0+12-0) = 12
        d = _direction(
            research_history=_history(
                3, tasks_completed=0, tasks_failed=4, tasks_failed_infra=0, prs_merged=0
            )
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["non_merge_finished"] == 12

    def test_infra_failures_excluded_from_non_merge_count(self):
        # 3 cycles: all 4 failures are infra → code_failed=0 → non_merge_finished=0
        d = _direction(
            research_history=_history(
                3, tasks_completed=0, tasks_failed=4, tasks_failed_infra=4, prs_merged=0
            )
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["non_merge_finished"] == 0

    def test_merge_rate_computed_correctly(self):
        # 3 cycles: 4 completed, 1 merged each
        # merged=3, non_merge_finished=max(0,12+0-3)=9
        # total_finished=12, merge_rate=3/12=0.25
        d = _direction(
            research_history=_history(
                3, tasks_completed=4, tasks_failed=0, prs_merged=1
            )
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["merged"] == 3
        assert m["non_merge_finished"] == 9
        assert abs(m["merge_rate"] - 0.25) < 0.001

    def test_zero_total_finished_gives_zero_merge_rate(self):
        # No completed or failed tasks → total_finished=0, no division by zero
        d = _direction(
            research_history=_history(
                3, tasks_completed=0, tasks_failed=0, prs_merged=0
            )
        )
        m = _compute_churn_metrics(d)
        # non_merge_finished=0, total_finished=0 → merge_rate=0.0
        assert m is not None
        assert m["merge_rate"] == 0.0

    def test_uses_rolling_window(self, mocker):
        # window=3: only last 3 "bad" cycles counted, not the 3 "clean" ones before
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"direction_churn_window_cycles": 3},
            clear=False,
        )
        clean = _history(3, tasks_completed=1, tasks_failed=0, prs_merged=0)
        bad = _history(3, tasks_completed=5, tasks_failed=0, prs_merged=0)
        d = _direction(research_history=clean + bad)
        m = _compute_churn_metrics(d)
        # Only last 3 "bad" cycles: non_merge_finished=max(0,15-0)=15
        assert m is not None
        assert m["window_cycles"] == 3
        assert m["non_merge_finished"] == 15


# ── _is_code_failure_pause_reason ────────────────────────────────────────────


class TestIsCodeFailurePauseReason:
    def test_rule3_high_failure_rate_prefix_classified_as_code_failure(self):
        # Use the actual message _check_direction_health produces for Rule 3
        d = _direction(
            research_history=_history(
                3, tasks_created=3, tasks_failed=3, tasks_failed_infra=0, prs_merged=0
            ),
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert _is_code_failure_pause_reason(reason) is True

    def test_rule4_recent_stall_prefix_classified_as_code_failure(self):
        # Rule 4 fires when 3 cycles all have >=1 code failure and 0 merges
        d = _direction(
            research_history=_history(
                3,
                tasks_created=2,
                tasks_completed=1,
                tasks_failed=1,
                tasks_failed_infra=0,
                prs_merged=0,
            ),
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert _is_code_failure_pause_reason(reason) is True

    def test_rule1_saturation_not_classified_as_code_failure(self):
        d = _direction(
            possibly_saturated=True,
            research_history=_history(5, tasks_created=1),
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert _is_code_failure_pause_reason(reason) is False

    def test_rule2_unproductive_not_classified_as_code_failure(self):
        d = _direction(
            research_history=_history(
                5, prs_merged=0, tasks_created=2, tasks_completed=2, tasks_failed=0
            ),
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert _is_code_failure_pause_reason(reason) is False

    def test_rule5_churn_guardrail_not_classified_as_code_failure(self):
        # Rule 5 (churn) is an unproductive-volume signal, not a code error;
        # it must NOT trigger a root-cause investigation.
        d = _direction(
            research_history=_history(
                5, tasks_created=5, tasks_completed=4, tasks_failed=0, prs_merged=0
            )
        )
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Churn guardrail" in reason
        assert _is_code_failure_pause_reason(reason) is False

    def test_none_reason_returns_false(self):
        assert _is_code_failure_pause_reason(None) is False

    def test_empty_string_returns_false(self):
        assert _is_code_failure_pause_reason("") is False


# ── _build_direction_failure_summary ─────────────────────────────────────────


class TestBuildDirectionFailureSummary:
    def test_summarizes_last_3_cycles(self):
        # 5 cycles; only last 3 should be aggregated
        history = _history(2, tasks_created=9, tasks_failed=9, prs_merged=0) + _history(
            3,
            tasks_created=2,
            tasks_completed=1,
            tasks_failed=1,
            tasks_failed_infra=0,
            prs_merged=0,
        )
        d = _direction(research_history=history)
        summary = _build_direction_failure_summary(d)
        assert summary["cycles_analyzed"] == 3
        assert summary["tasks_created"] == 6
        assert summary["tasks_failed_total"] == 3
        assert summary["tasks_failed_code"] == 3
        assert summary["tasks_failed_infra"] == 0
        assert summary["prs_merged"] == 0

    def test_excludes_infra_failures_from_code_failed(self):
        d = _direction(
            research_history=_history(
                3, tasks_created=10, tasks_failed=6, tasks_failed_infra=4, prs_merged=0
            ),
        )
        summary = _build_direction_failure_summary(d)
        assert summary["tasks_failed_total"] == 18
        assert summary["tasks_failed_infra"] == 12
        assert summary["tasks_failed_code"] == 6

    def test_computes_code_failure_rate(self):
        d = _direction(
            research_history=_history(
                3, tasks_created=4, tasks_failed=3, tasks_failed_infra=0, prs_merged=0
            ),
        )
        summary = _build_direction_failure_summary(d)
        # 9 code failed / 12 created = 0.75
        assert summary["code_failure_rate"] == 0.75

    def test_zero_tasks_created_returns_zero_rate_not_division_error(self):
        d = _direction(
            research_history=_history(3, tasks_created=0, tasks_failed=0),
        )
        summary = _build_direction_failure_summary(d)
        assert summary["code_failure_rate"] == 0.0
        assert summary["cycles_analyzed"] == 3

    def test_empty_history_returns_zero_counts(self):
        summary = _build_direction_failure_summary(_direction(research_history=[]))
        assert summary["cycles_analyzed"] == 0
        assert summary["tasks_created"] == 0
        assert summary["tasks_failed_code"] == 0
        assert summary["code_failure_rate"] == 0.0

    def test_missing_research_history_key_handled(self):
        summary = _build_direction_failure_summary({})
        assert summary["cycles_analyzed"] == 0
        assert summary["tasks_created"] == 0


# ── _check_direction_completion ──────────────────────────────────────────────


class TestCheckDirectionCompletion:
    def test_returns_message_when_all_criteria_met(self):
        # 4+ cycles, 3+ merged, recent 2 cycles low-yield
        history = _history(3, prs_merged=2) + _history(2, tasks_created=1, prs_merged=0)
        d = _direction(research_history=history)
        result = _check_direction_completion(d)
        assert result is not None
        assert "complete" in result.lower()

    def test_requires_minimum_4_cycles(self):
        d = _direction(research_history=_history(3, prs_merged=2))  # just 3 cycles
        result = _check_direction_completion(d)
        assert result is None

    def test_requires_minimum_3_total_prs_merged(self):
        # 4 cycles but only 2 merged total
        history = _history(3, prs_merged=0) + _history(1, prs_merged=2)
        d = _direction(research_history=history)
        result = _check_direction_completion(d)
        assert result is None

    def test_not_triggered_when_recent_activity_high(self):
        # Last 2 cycles have 3 tasks created (> 2) — still active
        history = [
            *_history(3, prs_merged=2),
            {
                "tasks_created": 3,
                "tasks_completed": 1,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 0,
            },
            {
                "tasks_created": 3,
                "tasks_completed": 1,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 0,
            },
        ]
        d = _direction(research_history=history)
        result = _check_direction_completion(d)
        assert result is None

    def test_not_triggered_when_recent_merged_above_threshold(self):
        # Last 2 cycles have 2 merged PRs (> 1)
        history = [
            *_history(2, prs_merged=1),
            {
                "tasks_created": 1,
                "tasks_completed": 1,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 1,
            },
            {
                "tasks_created": 1,
                "tasks_completed": 1,
                "tasks_failed": 0,
                "tasks_failed_infra": 0,
                "prs_merged": 1,
            },
        ]
        d = _direction(research_history=history)
        result = _check_direction_completion(d)
        # 2 recent_merged > 1 threshold → not complete
        assert result is None

    def test_message_includes_pr_count_and_cycle_count(self):
        history = _history(4, prs_merged=1) + _history(2, tasks_created=0, prs_merged=0)
        d = _direction(research_history=history)
        result = _check_direction_completion(d)
        assert result is not None
        assert "4" in result  # prs_merged
        assert "6" in result  # total cycles

    def test_returns_none_for_empty_history(self):
        d = _direction(research_history=[])
        assert _check_direction_completion(d) is None


# ── _is_direction_duplicate ──────────────────────────────────────────────────


class TestIsDirectionDuplicate:
    def test_duplicate_with_existing_direction(self):
        proposal = "improve caching layer performance database queries"
        existing = [{"text": "improve caching layer performance database queries"}]
        assert _is_direction_duplicate(proposal, existing, []) is True

    def test_not_duplicate_when_overlap_below_threshold(self):
        # Only 3 shared non-stopword words
        proposal = "improve database query performance"
        existing = [{"text": "improve database query latency optimization"}]
        # Words after stopwords: {improve, database, query, performance}
        # Existing after stopwords: {improve, database, query, latency, optimization}
        # Overlap: improve, database, query = 3 < 4 threshold
        assert _is_direction_duplicate(proposal, existing, []) is False

    def test_duplicate_with_recent_rejected_proposal(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        proposal = "migrate authentication service new token system"
        rejected = [
            {
                "text": "migrate authentication service new token system",
                "rejected_at": recent,
            }
        ]
        assert _is_direction_duplicate(proposal, [], rejected) is True

    def test_old_rejected_proposal_ignored(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        proposal = "migrate authentication service new token system"
        rejected = [
            {
                "text": "migrate authentication service new token system",
                "rejected_at": old,
            }
        ]
        assert _is_direction_duplicate(proposal, [], rejected) is False

    def test_stopwords_filtered_from_proposal(self):
        # After filtering stopwords from "feat fix refactor add update improve",
        # fewer than 2 non-stopword tokens remain → returns False immediately
        proposal = "feat fix refactor add update improve"
        existing = [{"text": "feat fix refactor add update improve"}]
        assert _is_direction_duplicate(proposal, existing, []) is False

    def test_requires_at_least_2_non_stopword_tokens(self):
        # Only one non-stopword word
        proposal = "improve"
        existing = [{"text": "improve performance database caching queries"}]
        assert _is_direction_duplicate(proposal, existing, []) is False

    def test_no_match_against_empty_lists(self):
        proposal = "refactor authentication middleware pipeline"
        assert _is_direction_duplicate(proposal, [], []) is False

    def test_exactly_4_shared_words_triggers_duplicate(self):
        proposal = "cache database query performance results"
        existing = [{"text": "cache database query performance improvements"}]
        # Shared non-stopword words: cache, database, query, performance = 4
        assert _is_direction_duplicate(proposal, existing, []) is True

    def test_case_insensitive_matching(self):
        proposal = "Cache Database Query Performance Results"
        existing = [{"text": "cache database query performance improvements"}]
        assert _is_direction_duplicate(proposal, existing, []) is True

    def test_retired_direction_3_word_overlap_no_longer_triggers_duplicate(self):
        # 3-word overlap against an expired direction no longer triggers (threshold = 4 for all)
        # Note: "improve" is a stopword and is excluded from overlap counting
        proposal = "cache database query performance"
        existing = [
            {"text": "cache database query latency optimization", "status": "expired"}
        ]
        # Non-stopword overlap: cache, database, query = 3 < threshold 4 → False
        assert _is_direction_duplicate(proposal, existing, []) is False

    def test_active_direction_still_requires_4_words(self):
        # Same 3-word overlap against an active direction should NOT be flagged (threshold 4)
        proposal = "cache database query performance"
        existing = [
            {"text": "cache database query latency optimization", "status": "active"}
        ]
        # Non-stopword overlap: cache, database, query = 3 < threshold 4 for active → False
        assert _is_direction_duplicate(proposal, existing, []) is False

    def test_superseded_direction_4_word_overlap_triggers_duplicate(self):
        # 4-word overlap against a superseded direction still triggers (threshold = 4)
        proposal = "migrate authentication service token"
        existing = [
            {
                "text": "migrate authentication service old token system",
                "status": "superseded",
            }
        ]
        # Overlap: migrate, authentication, service, token = 4 >= threshold 4 → True
        assert _is_direction_duplicate(proposal, existing, []) is True

    def test_retired_threshold_2_words_does_not_trigger(self):
        # Only 2 shared non-stopword words even against a retired direction → False
        proposal = "caching performance metrics"
        existing = [
            {"text": "caching performance strategy deployment", "status": "expired"}
        ]
        # Overlap: caching, performance = 2 < threshold 4 → False
        assert _is_direction_duplicate(proposal, existing, []) is False

    def test_retired_direction_4_word_overlap_still_blocked(self):
        # 4-word overlap against a retired direction still blocks (threshold = 4)
        proposal = "migrate authentication service oauth token"
        existing = [
            {
                "text": "migrate authentication service oauth2 token system",
                "status": "retired",
            }
        ]
        # Overlap: migrate, authentication, service, oauth, token = 5 >= 4 → True
        assert _is_direction_duplicate(proposal, existing, []) is True


# ── _record_strategic_task_failure ───────────────────────────────────────────


@pytest_asyncio.fixture()
async def strat_fail_db(mocker):
    """Isolated in-memory DB for _record_strategic_task_failure tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: self.visit_JSON(
            type_, **kw
        )

    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with _Session() as session:
        yield session

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


async def _make_failure_task(
    session, project, task_type="strategic_review", context=None, status_message=None
):
    task = Task(
        project_id=project.id,
        payloads=[{"type": task_type, "prompt": "strategic prompt"}],
        context=context or {},
        latest_status_message=status_message,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


class TestRecordStrategicTaskFailure:
    @pytest.mark.asyncio
    async def test_increments_strategic_failures_counter(self, strat_fail_db):
        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        task = await _make_failure_task(strat_fail_db, project)
        await _record_strategic_task_failure(task, strat_fail_db)

        # Do NOT refresh — function modifies project in-memory without commit.
        # Refreshing with autoflush=False re-reads from DB and overwrites changes.
        assert project.vision.get("_strategic_failures") == 1

    @pytest.mark.asyncio
    async def test_increments_from_existing_count(self, strat_fail_db):
        project = Project(
            name="P", user_id="u1", vision={"purpose": "test", "_strategic_failures": 2}
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        task = await _make_failure_task(strat_fail_db, project)
        await _record_strategic_task_failure(task, strat_fail_db)

        assert project.vision.get("_strategic_failures") == 3

    @pytest.mark.asyncio
    async def test_sets_strategic_failures_at_timestamp(self, strat_fail_db):
        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        task = await _make_failure_task(strat_fail_db, project)
        await _record_strategic_task_failure(task, strat_fail_db)

        ts = project.vision.get("_strategic_failures_at")
        assert ts is not None
        # Should be parseable as a datetime
        datetime.fromisoformat(ts)

    @pytest.mark.asyncio
    async def test_updates_cycle_history_for_strategic_review(self, strat_fail_db):
        # Audit W2: cycle history is a real table now. Insert a StrategicCycle
        # row and verify _record_strategic_task_failure flips its outcome.
        from app import strategic_cycle as sc
        from app.models import StrategicCycle

        cycle_id = "cycle-fail-001"
        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        cycle = StrategicCycle(
            id=cycle_id,
            project_id=project.id,
            outcome=sc.OUTCOME_PHASE2_RUNNING,
            phase1_started_at=_utcnow(),
            phase1_completed_at=_utcnow(),
            phase2_started_at=_utcnow(),
        )
        strat_fail_db.add(cycle)
        await strat_fail_db.commit()

        task = await _make_failure_task(
            strat_fail_db,
            project,
            task_type="strategic_review",
            context={"strategic_cycle_id": cycle_id},
            status_message="Claude API timeout",
        )
        await _record_strategic_task_failure(task, strat_fail_db)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(cycle)
        assert cycle.outcome == sc.OUTCOME_FAILED_PHASE2
        assert cycle.phase2_completed_at is not None
        assert cycle.failure_reason and "Claude API timeout" in cycle.failure_reason

    @pytest.mark.asyncio
    async def test_updates_cycle_history_for_design_doc_update(self, strat_fail_db):
        from app import strategic_cycle as sc
        from app.models import StrategicCycle

        cycle_id = "cycle-fail-002"
        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        cycle = StrategicCycle(
            id=cycle_id,
            project_id=project.id,
            outcome=sc.OUTCOME_PHASE1_RUNNING,
            phase1_started_at=_utcnow(),
        )
        strat_fail_db.add(cycle)
        await strat_fail_db.commit()

        task = await _make_failure_task(
            strat_fail_db,
            project,
            task_type="design_doc_update",
            context={"strategic_cycle_id": cycle_id},
            status_message="VM crash",
        )
        await _record_strategic_task_failure(task, strat_fail_db)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(cycle)
        assert cycle.outcome == sc.OUTCOME_FAILED_PHASE1
        assert cycle.phase1_completed_at is not None
        assert cycle.failure_reason and "VM crash" in cycle.failure_reason

    @pytest.mark.asyncio
    async def test_no_cycle_id_skips_cycle_history_update(self, strat_fail_db):
        project = Project(
            name="P",
            user_id="u1",
            vision={
                "purpose": "test",
                "_strategic_cycle_history": [{"cycle_id": "other", "outcome": "ok"}],
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        task = await _make_failure_task(strat_fail_db, project, context={})
        await _record_strategic_task_failure(task, strat_fail_db)

        # History entry should be unchanged
        history = project.vision.get("_strategic_cycle_history", [])
        assert history[0].get("outcome") == "ok"
        # But failures counter still incremented
        assert project.vision.get("_strategic_failures") == 1

    @pytest.mark.asyncio
    async def test_error_truncated_to_200_chars(self, strat_fail_db):
        cycle_id = "cycle-fail-003"
        project = Project(
            name="P",
            user_id="u1",
            vision={
                "purpose": "test",
                "_strategic_cycle_history": [{"cycle_id": cycle_id}],
            },
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)

        long_message = "x" * 500
        task = await _make_failure_task(
            strat_fail_db,
            project,
            context={"strategic_cycle_id": cycle_id},
            status_message=long_message,
        )
        await _record_strategic_task_failure(task, strat_fail_db)

        history = project.vision.get("_strategic_cycle_history", [])
        entry = next((e for e in history if e.get("cycle_id") == cycle_id), None)
        assert len(entry.get("error", "")) <= 200

    @pytest.mark.asyncio
    async def test_no_op_when_project_not_found(self, strat_fail_db):
        """Should return silently when project does not exist."""
        import uuid

        task = Task(
            project_id=uuid.uuid4(),
            payloads=[{"type": "strategic_review", "prompt": "p"}],
            context={},
        )
        strat_fail_db.add(task)
        await strat_fail_db.commit()
        # Should not raise
        await _record_strategic_task_failure(task, strat_fail_db)

    @pytest.mark.asyncio
    async def test_contention_skip_when_peer_holds_lock(self, mocker):
        """R5-W4 MEDIUM-D test 2: when project=None and the FOR UPDATE
        skip_locked SELECT returns None but the row genuinely exists,
        the function must log + return cleanly without raising.

        This exercises the "held by parallel handler" branch documented
        in the function docstring. The parallel handler is incrementing
        the same counter; silently skipping is safe by design.
        """
        from unittest.mock import AsyncMock, MagicMock
        import uuid

        # Build a Task without persisting it — the function reads
        # project_id and id from the in-memory instance.
        task = Task(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            payloads=[{"type": "strategic_review", "prompt": "p"}],
            context={},
        )

        # First execute call: locked SELECT returns None (peer holds lock).
        # Second execute call: exists check returns a row (project exists).
        first_result = MagicMock()
        first_result.scalars.return_value.first.return_value = None
        second_result = MagicMock()
        second_result.first.return_value = (task.project_id,)

        session = MagicMock()
        session.execute = AsyncMock(side_effect=[first_result, second_result])
        session.add = MagicMock()  # not called in this branch

        # Must return cleanly without raising.
        await _record_strategic_task_failure(task, session, project=None)

        # Validate: locked SELECT and existence-disambiguator SELECT both ran;
        # session.add was NOT called (no project mutation in skip branch).
        assert session.execute.await_count == 2
        session.add.assert_not_called()


# ── thinker_artifacts_missing outcome routing ────────────────────────────────


class TestThinkerArtifactsMissingOutcome:
    """failure_category=strategic_thinker_artifacts_missing routes the cycle
    to OUTCOME_THINKER_ARTIFACTS_MISSING instead of OUTCOME_FAILED_PHASE2."""

    @pytest.mark.asyncio
    async def test_thinker_artifacts_missing_sets_new_outcome(self, strat_fail_db):
        from app import strategic_cycle as sc
        from app.models import StrategicCycle

        cycle_id = "cycle-thinker-001"
        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        cycle = StrategicCycle(
            id=cycle_id,
            project_id=project.id,
            outcome=sc.OUTCOME_PHASE2_RUNNING,
            phase1_started_at=_utcnow(),
            phase1_completed_at=_utcnow(),
            phase2_started_at=_utcnow(),
        )
        strat_fail_db.add(cycle)
        await strat_fail_db.commit()

        # Task carries failure_category in its payload
        task = Task(
            project_id=project.id,
            payloads=[
                {
                    "type": "strategic_review",
                    "prompt": "p",
                    "failure_category": "strategic_thinker_artifacts_missing",
                }
            ],
            context={"strategic_cycle_id": cycle_id},
            latest_status_message="thinker artifacts missing",
        )
        strat_fail_db.add(task)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(task)

        await _record_strategic_task_failure(task, strat_fail_db)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(cycle)

        assert cycle.outcome == sc.OUTCOME_THINKER_ARTIFACTS_MISSING
        assert cycle.outcome not in sc.ACTIVE_OUTCOMES

    @pytest.mark.asyncio
    async def test_generic_phase2_failure_still_uses_failed_phase2(self, strat_fail_db):
        from app import strategic_cycle as sc
        from app.models import StrategicCycle

        cycle_id = "cycle-generic-fail-001"
        project = Project(name="P", user_id="u1", vision={"purpose": "test"})
        strat_fail_db.add(project)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(project)
        cycle = StrategicCycle(
            id=cycle_id,
            project_id=project.id,
            outcome=sc.OUTCOME_PHASE2_RUNNING,
            phase1_started_at=_utcnow(),
            phase1_completed_at=_utcnow(),
            phase2_started_at=_utcnow(),
        )
        strat_fail_db.add(cycle)
        await strat_fail_db.commit()

        # Task with no special failure_category
        task = Task(
            project_id=project.id,
            payloads=[{"type": "strategic_review", "prompt": "p"}],
            context={"strategic_cycle_id": cycle_id},
            latest_status_message="VM crash",
        )
        strat_fail_db.add(task)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(task)

        await _record_strategic_task_failure(task, strat_fail_db)
        await strat_fail_db.commit()
        await strat_fail_db.refresh(cycle)

        assert cycle.outcome == sc.OUTCOME_FAILED_PHASE2


# ── _check_direction_starvation ──────────────────────────────────────────────


def _active_dir(**kwargs):
    base = {"id": f"d-{kwargs.get('id', 'x')}", "status": "active"}
    base.update(kwargs)
    return base


class TestCheckDirectionStarvation:
    @pytest.mark.asyncio
    async def test_no_gaps_resets_counter(self, strat_fail_db, mocker):
        mocker.patch("app.alert_dispatch._create_strategic_alert", new=mocker.AsyncMock())
        # 3 active product directions, not saturated → no gaps
        project = Project(
            name="P",
            user_id="u1",
            vision={
                "_direction_starvation_count": 3,
                "_strategic_frequency_override": 2,
            },
            directions=[
                _active_dir(id="1", category="product"),
                _active_dir(id="2", category="product"),
                _active_dir(id="3", category="product"),
            ],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        assert project.vision.get("_direction_starvation_count") == 0
        assert "_strategic_frequency_override" not in project.vision

    @pytest.mark.asyncio
    async def test_dirs_created_resets_counter(self, strat_fail_db, mocker):
        mocker.patch("app.alert_dispatch._create_strategic_alert", new=mocker.AsyncMock())
        project = Project(
            name="P",
            user_id="u1",
            vision={
                "_direction_starvation_count": 4,
                "_strategic_frequency_override": 2,
            },
            directions=[_active_dir(id="1", category="product")],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        # dirs_created > 0 → reset even though has_gaps is True
        await _check_direction_starvation(
            project, dirs_created=1, session=strat_fail_db
        )

        assert project.vision.get("_direction_starvation_count") == 0
        assert "_strategic_frequency_override" not in project.vision

    @pytest.mark.asyncio
    async def test_below_minimum_increments_counter(self, strat_fail_db, mocker):
        alert_mock = mocker.patch(
            "app.alert_dispatch._create_strategic_alert", new=mocker.AsyncMock()
        )
        project = Project(
            name="P",
            user_id="u1",
            vision={},
            directions=[_active_dir(id="1", category="product")],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        assert project.vision.get("_direction_starvation_count") == 1
        # Level 1 — no alert, no override
        assert "_strategic_frequency_override" not in project.vision
        alert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_level_2_creates_alert_and_frequency_override(
        self, strat_fail_db, mocker
    ):
        alert_mock = mocker.patch(
            "app.alert_dispatch._create_strategic_alert", new=mocker.AsyncMock()
        )
        project = Project(
            name="P",
            user_id="u1",
            vision={"_direction_starvation_count": 1},
            directions=[_active_dir(id="1", category="product")],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        assert project.vision.get("_direction_starvation_count") == 2
        assert project.vision.get("_strategic_frequency_override") == 2
        alert_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_level_5_auto_creates_no_product_fallback(
        self, strat_fail_db, mocker
    ):
        alert_mock = mocker.patch(
            "app.alert_dispatch._create_strategic_alert", new=mocker.AsyncMock()
        )
        # Active directions but NONE are product → gap_type=NO_PRODUCT_DIRECTIONS
        project = Project(
            name="P",
            user_id="u1",
            vision={
                "_direction_starvation_count": 4,
                "_strategic_frequency_override": 2,
            },
            directions=[
                _active_dir(id="1", category="structural"),
                _active_dir(id="2", category="structural"),
                _active_dir(id="3", category="structural"),
            ],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        # Fallback direction appended with product category
        assert len(project.directions) == 4
        new_dir = project.directions[-1]
        assert new_dir["category"] == "product"
        assert new_dir["status"] == "active"
        assert new_dir["priority"] == "medium"
        assert new_dir["id"].startswith("d-")
        # Counter and override cleared
        assert project.vision.get("_direction_starvation_count") == 0
        assert "_strategic_frequency_override" not in project.vision
        alert_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_level_5_below_minimum_fallback_no_category(
        self, strat_fail_db, mocker
    ):
        mocker.patch("app.alert_dispatch._create_strategic_alert", new=mocker.AsyncMock())
        # 1 active product direction; product_count > 0 but active_count < 3
        project = Project(
            name="P",
            user_id="u1",
            vision={"_direction_starvation_count": 4},
            directions=[_active_dir(id="1", category="product")],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        assert len(project.directions) == 2
        new_dir = project.directions[-1]
        # BELOW_MINIMUM → no category set
        assert "category" not in new_dir

    @pytest.mark.asyncio
    async def test_level_5_all_saturated_fallback(self, strat_fail_db, mocker):
        mocker.patch("app.alert_dispatch._create_strategic_alert", new=mocker.AsyncMock())
        # 3 active product directions, all saturated → gap_type=ALL_SATURATED
        project = Project(
            name="P",
            user_id="u1",
            vision={"_direction_starvation_count": 4},
            directions=[
                _active_dir(id="1", category="product", possibly_saturated=True),
                _active_dir(id="2", category="product", possibly_saturated=True),
                _active_dir(id="3", category="product", possibly_saturated=True),
            ],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        assert len(project.directions) == 4
        new_dir = project.directions[-1]
        assert "category" not in new_dir

    @pytest.mark.asyncio
    async def test_level_5_cap_reached_skips_creation(self, strat_fail_db, mocker):
        alert_mock = mocker.patch(
            "app.alert_dispatch._create_strategic_alert", new=mocker.AsyncMock()
        )
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"max_active_directions": 2},
            clear=False,
        )
        project = Project(
            name="P",
            user_id="u1",
            vision={"_direction_starvation_count": 4},
            directions=[
                _active_dir(id="1", category="structural"),
                _active_dir(id="2", category="structural"),
            ],
        )
        strat_fail_db.add(project)
        await strat_fail_db.commit()

        await _check_direction_starvation(
            project, dirs_created=0, session=strat_fail_db
        )

        # No fallback added, counter NOT reset (kept incrementing)
        assert len(project.directions) == 2
        assert project.vision.get("_direction_starvation_count") == 5
        alert_mock.assert_not_called()


# ── Rationale persistence & retrieval ────────────────────────────────────────


class TestRationalePersistence:
    def test_extract_rationale_from_notes_returns_latest(self):
        d = {
            "notes": [
                {"text": f"{_CREATED_NOTE_PREFIX}initial reason"},
                {"text": "Strategic update: minor tweak"},
                {"text": f"{_CREATED_NOTE_PREFIX}final reason"},
            ]
        }
        assert _extract_rationale_from_notes(d, _CREATED_NOTE_PREFIX) == "final reason"

    def test_extract_rationale_from_notes_no_match(self):
        d = {"notes": [{"text": "unrelated"}]}
        assert _extract_rationale_from_notes(d, _CREATED_NOTE_PREFIX) is None

    def test_extract_rationale_caps_to_200(self):
        long = "x" * 500
        d = {"notes": [{"text": f"{_CREATED_NOTE_PREFIX}{long}"}]}
        out = _extract_rationale_from_notes(d, _CREATED_NOTE_PREFIX)
        assert out is not None and len(out) == 200

    def test_collect_rationales_from_directions(self):
        project = Project(
            name="P",
            user_id="u1",
            directions=[
                {"id": "d-a", "notes": [{"text": f"{_CREATED_NOTE_PREFIX}because A"}]},
                {"id": "d-b", "notes": [{"text": f"{_RETIRED_NOTE_PREFIX}because B"}]},
            ],
        )
        created = _collect_rationales(
            project, ["d-a", "d-missing"], _CREATED_NOTE_PREFIX
        )
        retired = _collect_rationales(project, ["d-b"], _RETIRED_NOTE_PREFIX)
        assert created == {"d-a": "because A"}
        assert retired == {"d-b": "because B"}

    def test_extract_cycle_reasoning_prefers_plan_output(self):
        t = MagicMock()
        t.plan_output = {"reasoning": "top-level reason"}
        t.latest_status_message = "ignored"
        assert _extract_cycle_reasoning(t) == "top-level reason"

    def test_extract_cycle_reasoning_falls_back_to_status_message(self):
        t = MagicMock()
        t.plan_output = None
        t.latest_status_message = "from status"
        assert _extract_cycle_reasoning(t) == "from status"

    def test_extract_cycle_reasoning_truncates_to_500(self):
        t = MagicMock()
        t.plan_output = {"summary": "y" * 1000}
        t.latest_status_message = ""
        assert len(_extract_cycle_reasoning(t)) == 500

    def test_build_previous_decision_outcomes_surfaces_rationales(self):
        # Audit W2: cycle history is now passed in (was JSONB on vision).
        project = Project(
            name="P",
            user_id="u1",
            directions=[
                {
                    "id": "d-n",
                    "status": "active",
                    "text": "new dir",
                    "research_history": [{"prs_merged": 1}],
                },
            ],
            vision={},
        )
        cycle_history = [
            {
                "cycle_id": "sc-1",
                "outcome": "completed",
                "created_direction_ids": ["d-n"],
                "retired_direction_ids": ["d-gone"],
                "created_rationales": {"d-n": "because new capability"},
                "retired_rationales": {"d-gone": "stalled for 5 cycles"},
                "cycle_reasoning": "focus on velocity",
            },
        ]
        out = _build_previous_decision_outcomes(project, cycle_history)
        assert out is not None
        assert out["cycle_reasoning"] == "focus on velocity"
        assert out["created"][0]["rationale"] == "because new capability"
        assert out["retired"][0] == {
            "direction_id": "d-gone",
            "role": "retired",
            "status": "missing",
            "rationale": "stalled for 5 cycles",
        }

    def test_build_previous_decision_outcomes_no_rationales_is_backward_compatible(
        self,
    ):
        project = Project(
            name="P",
            user_id="u1",
            directions=[
                {"id": "d-n", "status": "active", "text": "t", "research_history": []}
            ],
            vision={},
        )
        cycle_history = [
            {
                "cycle_id": "sc-1",
                "outcome": "completed",
                "created_direction_ids": ["d-n"],
            },
        ]
        out = _build_previous_decision_outcomes(project, cycle_history)
        assert out is not None
        assert "cycle_reasoning" not in out
        assert "rationale" not in out["created"][0]


class TestDirectionPredictionCalibration:
    def test_build_creation_prediction_short_text_is_high_difficulty(self):
        p = Project(name="P", user_id="u", directions=[], vision={})
        d = {"id": "d1", "text": "improve speed"}
        pred = _build_creation_prediction(d, p)
        assert pred["estimated_difficulty"] == "high"
        assert pred["text_length"] == len("improve speed")
        assert pred["similar_past_merges"] == 0

    def test_build_creation_prediction_counts_similar_past_merges(self):
        similar = {
            "id": "d0",
            "text": "cache invalidation dashboard refresh",
            "research_history": [{"prs_merged": 2}, {"prs_merged": 1}],
        }
        p = Project(
            name="P",
            user_id="u",
            directions=[similar],
            vision={"systems": [{"id": "s", "status": "functional"}]},
        )
        new_d = {"id": "d1", "text": "cache invalidation dashboard refresh strategy"}
        pred = _build_creation_prediction(new_d, p)
        assert pred["similar_past_merges"] == 3
        assert pred["similar_direction_count"] == 1
        assert pred["mature_system_count"] == 1

    def test_build_retirement_metrics_snapshots_merges_and_cycles(self):
        d = {
            "id": "d1",
            "research_history": [{"prs_merged": 2}, {"prs_merged": 1}],
            "history_aggregate": {"total_prs_merged": 5, "total_cycles": 4},
        }
        m = _build_retirement_metrics(d)
        assert m["final_prs_merged"] == 8
        assert m["final_cycles_active"] == 6
        assert isinstance(m["final_quality_score"], float)

    def test_collect_predictions_and_metrics_skip_missing_ids(self):
        p = Project(
            name="P",
            user_id="u",
            directions=[{"id": "d1", "text": "x" * 60, "research_history": []}],
            vision={},
        )
        assert _collect_creation_predictions(p, ["d1", "d-missing"]).keys() == {"d1"}
        assert _collect_retirement_metrics(p, ["d1", "d-missing"]).keys() == {"d1"}

    def test_build_previous_decision_outcomes_includes_prediction_and_delta(self):
        from app.strategic_spawning import _build_previous_decision_outcomes

        project = Project(
            name="P",
            user_id="u1",
            directions=[
                {
                    "id": "d-n",
                    "status": "active",
                    "text": "new",
                    "research_history": [{"prs_merged": 4}],
                }
            ],
            vision={},
        )
        # Audit W2: cycle history is now passed in (was JSONB on vision).
        cycle_history = [
            {
                "cycle_id": "sc-1",
                "outcome": "completed",
                "created_direction_ids": ["d-n"],
                "retired_direction_ids": ["d-gone"],
                "created_predictions": {
                    "d-n": {
                        "estimated_difficulty": "high",
                        "text_length": 3,
                        "mature_system_count": 0,
                        "similar_direction_count": 1,
                        "similar_past_merges": 1,
                    }
                },
                "retired_final_metrics": {
                    "d-gone": {
                        "final_quality_score": 0.2,
                        "final_prs_merged": 0,
                        "final_cycles_active": 5,
                    }
                },
            }
        ]
        out = _build_previous_decision_outcomes(project, cycle_history)
        assert out["created"][0]["prediction"]["estimated_difficulty"] == "high"
        assert out["created"][0]["calibration_delta"] == 3
        assert out["retired"][0]["final_metrics"]["final_cycles_active"] == 5


# ── recommended_mode normalization + persistence ─────────────────────────────


class TestRecommendedModeNormalization:
    """The strategic agent emits `recommended_mode` on direction operations.

    Only canonical values are persisted to project.directions records; unknown
    or non-string values are dropped silently (the field is optional so unknown
    values must never block direction-op application)."""

    def test_normalize_canonical_modes(self):
        from app.strategic_completion import _normalize_recommended_mode

        for value in ("audit", "fundamentals", "ship", "pause"):
            assert _normalize_recommended_mode(value) == value
            assert _normalize_recommended_mode(value.upper()) == value
            assert _normalize_recommended_mode(f"  {value}  ") == value

    def test_normalize_rejects_unknown(self):
        from app.strategic_completion import _normalize_recommended_mode

        for value in ("nonsense", "", "audit_now", None, 42, []):
            assert _normalize_recommended_mode(value) is None


@pytest.mark.asyncio
async def test_strategic_completion_persists_recommended_mode_on_create(
    db_session, mocker
):
    """Direction-op `create` with `recommended_mode='audit'` persists that
    value on the new direction record inside project.directions."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Audit the auth module",
                    "priority": "high",
                    "rationale": "stale tests",
                    "system_id": "auth",
                    "recommended_mode": "audit",
                    "success_evidence": "Auth tests passing with no regressions",
                    "retirement_condition": "2 consecutive cycles with 0 auth failures",
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [
        d
        for d in (project.directions or [])
        if d.get("text") == "Audit the auth module"
    ]
    assert len(new_dirs) == 1
    assert new_dirs[0].get("recommended_mode") == "audit", (
        f"Expected recommended_mode='audit' on created direction; "
        f"got {new_dirs[0].get('recommended_mode')!r}"
    )


@pytest.mark.asyncio
async def test_strategic_completion_persists_recommended_mode_on_update(
    db_session, mocker
):
    """Direction-op `update` with `recommended_mode` updates the existing
    direction's record."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-upd1",
                "text": "Existing direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-upd1",
                    "rationale": "drift detected",
                    "changes": {"recommended_mode": "fundamentals"},
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-upd1")
    assert target.get("recommended_mode") == "fundamentals"


@pytest.mark.asyncio
async def test_strategic_update_persists_success_evidence_and_retirement_condition(
    db_session, mocker
):
    """Direction-op `update` with success_evidence and retirement_condition persists
    both into direction['context'], matching the CREATE path behaviour."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P-upd-ctx",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-ctx1",
                "text": "Existing direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-ctx1",
                    "rationale": "clarifying success criteria",
                    "changes": {
                        "success_evidence": "All login flows pass SAML e2e",
                        "retirement_condition": "3 cycles with zero auth failures",
                    },
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-ctx1")
    ctx = target.get("context") or {}
    assert ctx.get("success_evidence") == "All login flows pass SAML e2e"
    assert ctx.get("retirement_condition") == "3 cycles with zero auth failures"


@pytest.mark.asyncio
async def test_strategic_update_persists_implementation_constraints(db_session, mocker):
    """Direction-op `update` with implementation_constraints persists it into
    direction['context'], matching the CREATE path behaviour."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P-upd-ic",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-ic1",
                "text": "Existing direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {"success_evidence": "prior evidence"},
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-ic1",
                    "rationale": "adding constraints",
                    "changes": {
                        "implementation_constraints": "Must not modify public API surface",
                    },
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-ic1")
    ctx = target.get("context") or {}
    assert ctx.get("implementation_constraints") == "Must not modify public API surface"
    # Existing context field should be preserved
    assert ctx.get("success_evidence") == "prior evidence"


@pytest.mark.asyncio
async def test_strategic_update_clears_context_fields_when_none(db_session, mocker):
    """Direction-op `update` with None value for a context field removes it from context."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P-upd-clr",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-clr1",
                "text": "Existing direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {
                    "success_evidence": "Old evidence",
                    "retirement_condition": "Old condition",
                },
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-clr1",
                    "rationale": "clearing stale criteria",
                    "changes": {
                        "success_evidence": None,
                        "retirement_condition": None,
                    },
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-clr1")
    ctx = target.get("context") or {}
    assert "success_evidence" not in ctx
    assert "retirement_condition" not in ctx


@pytest.mark.asyncio
async def test_strategic_update_caps_context_fields_at_500(db_session, mocker):
    """Direction-op `update` truncates oversized context field values to 500 chars."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P-upd-cap",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-cap1",
                "text": "Existing direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    long_value = "x" * 800

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-cap1",
                    "rationale": "testing cap",
                    "changes": {
                        "success_evidence": long_value,
                        "retirement_condition": long_value,
                    },
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-cap1")
    ctx = target.get("context") or {}
    assert len(ctx.get("success_evidence", "")) == 500
    assert len(ctx.get("retirement_condition", "")) == 500


@pytest.mark.asyncio
async def test_strategic_completion_drops_invalid_recommended_mode(db_session, mocker):
    """Unknown recommended_mode values are silently dropped on create."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "A direction",
                    "priority": "medium",
                    "rationale": "x",
                    "recommended_mode": "not-a-canonical-mode",
                    "success_evidence": "All tasks merged",
                    "retirement_condition": "No open tasks remaining",
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("text") == "A direction"]
    assert len(new_dirs) == 1
    # Field should be absent — invalid mode is silently dropped.
    assert "recommended_mode" not in new_dirs[0]


# ── recommended_mode='pause' auto-pause dispatch ─────────────────────────────


@pytest.mark.asyncio
async def test_strategic_completion_pause_mode_auto_pauses_on_create(
    db_session, mocker
):
    """create op with recommended_mode='pause' starts the direction in paused state.

    This makes the existing spawn-skip (status != 'active') mechanism handle
    the 'pause' mode without requiring a separate check in spawn-side code."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Paused direction",
                    "priority": "medium",
                    "recommended_mode": "pause",
                    "success_evidence": "No blockers remain",
                    "retirement_condition": "Gap closed for 2 cycles",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [
        d for d in (project.directions or []) if d.get("text") == "Paused direction"
    ]
    assert len(new_dirs) == 1
    d = new_dirs[0]
    assert d.get("status") == "paused", f"Expected paused, got {d.get('status')!r}"
    assert d.get("auto_paused") is True
    assert d.get("recommended_mode") == "pause"


@pytest.mark.asyncio
async def test_strategic_completion_pause_mode_auto_pauses_on_update(
    db_session, mocker
):
    """update op with recommended_mode='pause' transitions an active direction to paused."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-pauseme",
                "text": "Active direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-pauseme",
                    "changes": {"recommended_mode": "pause"},
                },
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-pauseme")
    assert target.get("status") == "paused"
    assert target.get("auto_paused") is True
    assert target.get("recommended_mode") == "pause"


@pytest.mark.asyncio
async def test_strategic_completion_non_pause_modes_preserve_active_status(
    db_session, mocker
):
    """update ops with audit/fundamentals/ship modes do not change direction status."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-a",
                "text": "Audit dir",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
            {
                "id": "d-f",
                "text": "Fundamentals dir",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
            {
                "id": "d-s",
                "text": "Ship dir",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    import json

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-a",
                    "changes": {"recommended_mode": "audit"},
                },
                {
                    "op": "update",
                    "direction_id": "d-f",
                    "changes": {"recommended_mode": "fundamentals"},
                },
                {
                    "op": "update",
                    "direction_id": "d-s",
                    "changes": {"recommended_mode": "ship"},
                },
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    by_id = {d["id"]: d for d in (project.directions or [])}
    for did, expected_mode in [
        ("d-a", "audit"),
        ("d-f", "fundamentals"),
        ("d-s", "ship"),
    ]:
        d = by_id[did]
        assert d.get("status") == "active", (
            f"{did} should stay active for mode={expected_mode}"
        )
        assert d.get("recommended_mode") == expected_mode
        assert not d.get("auto_paused")


# ── Structured direction fields (success_evidence / retirement_condition) ─────


def _make_project_and_task_for_structured_fields(db_session):
    """Helper returning (project, task) for structured-field tests."""
    import asyncio

    async def _build():
        project = Project(
            name="SF",
            user_id="u1",
            vision={"purpose": "test", "revision": 1},
            directions=[],
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)
        task = Task(
            project_id=project.id,
            payloads=[
                {
                    "type": "strategic_review",
                    "prompt": "review",
                    "vision": {"revision": 1},
                }
            ],
            context={},
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        return project, task

    return asyncio.get_event_loop().run_until_complete(_build())


@pytest.mark.asyncio
async def test_direction_create_persists_structured_fields(db_session, mocker):
    """A create op that includes success_evidence and retirement_condition persists both on direction.context."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="SF",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Users can authenticate via SSO",
                    "priority": "medium",
                    "success_evidence": "Login flow passes end-to-end with SAML provider",
                    "retirement_condition": "3 consecutive cycles with 0 auth-related failures",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [d for d in (project.directions or []) if "SSO" in d.get("text", "")]
    assert len(dirs) == 1, "Direction should have been created"
    ctx = dirs[0].get("context") or {}
    assert (
        ctx.get("success_evidence") == "Login flow passes end-to-end with SAML provider"
    )
    assert (
        ctx.get("retirement_condition")
        == "3 consecutive cycles with 0 auth-related failures"
    )


@pytest.mark.asyncio
async def test_direction_create_rejected_when_success_evidence_missing(
    db_session, mocker, caplog
):
    """A create op missing success_evidence is rejected (direction not created) with a warning log."""
    import json
    import logging
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="SF2",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Improve test coverage",
                    "priority": "medium",
                    # success_evidence intentionally omitted
                    "retirement_condition": "Coverage above 80%",
                }
            ],
        }
    )
    with caplog.at_level(logging.WARNING, logger="app.strategic_completion"):
        await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [
        d
        for d in (project.directions or [])
        if "test coverage" in d.get("text", "").lower()
    ]
    assert len(dirs) == 0, "Direction without success_evidence must not be created"
    assert any(
        "success_evidence" in r.message or "retirement_condition" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_legacy_direction_without_structured_fields_loads(db_session, mocker):
    """Directions that pre-date structured fields (no context key) still load and read correctly."""

    legacy_dir = {
        "id": "d-legacy01",
        "text": "Legacy direction",
        "status": "active",
        "priority": "medium",
        "notes": [],
        "research_history": [],
        # no 'context' key — this is a pre-structured-fields direction
    }
    project = Project(
        name="Legacy",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[legacy_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Confirm the direction is readable and context is absent
    found = next(
        (d for d in (project.directions or []) if d.get("id") == "d-legacy01"),
        None,
    )
    assert found is not None
    assert found.get("context") is None, "Legacy direction should have no context field"
    # text and status are still accessible
    assert found["text"] == "Legacy direction"
    assert found["status"] == "active"


# ── Retirement critique persistence ──────────────────────────────────────────


class TestBuildPastRetirementCritiques:
    def test_empty_cycle_history_returns_empty(self):
        assert _build_past_retirement_critiques([]) == []

    def test_cycles_without_retirement_critiques_key_return_empty(self):
        history = [{"cycle_id": "sc-1", "outcome": "completed"}]
        assert _build_past_retirement_critiques(history) == []

    def test_incomplete_cycles_skipped(self):
        history = [
            {
                "cycle_id": "sc-1",
                "outcome": "failed_phase2",
                "retirement_critiques": [
                    {
                        "direction_id": "d-1",
                        "what_it_addressed": "x",
                        "pattern_that_produced_it": "y",
                        "lesson": "z",
                    }
                ],
            },
            {
                "cycle_id": "sc-2",
                "outcome": "completed",
                "retirement_critiques": [
                    {
                        "direction_id": "d-2",
                        "what_it_addressed": "a",
                        "pattern_that_produced_it": "b",
                        "lesson": "c",
                    }
                ],
            },
        ]
        result = _build_past_retirement_critiques(history)
        assert len(result) == 1
        assert result[0]["direction_id"] == "d-2"

    def test_list_critiques_extracted(self):
        critiques = [
            {
                "direction_id": "d-abc",
                "what_it_addressed": "CI pipeline speed",
                "pattern_that_produced_it": "premature optimization",
                "lesson": "Wait for evidence before optimizing.",
            },
        ]
        history = [
            {
                "cycle_id": "sc-1",
                "outcome": "completed",
                "retirement_critiques": critiques,
            }
        ]
        result = _build_past_retirement_critiques(history)
        assert len(result) == 1
        assert result[0]["direction_id"] == "d-abc"
        assert result[0]["pattern_that_produced_it"] == "premature optimization"

    def test_no_eligible_sentinel_preserved(self):
        history = [
            {
                "cycle_id": "sc-1",
                "outcome": "completed",
                "retirement_critiques": {"no_eligible_retirements": True},
            }
        ]
        result = _build_past_retirement_critiques(history)
        assert result == [{"no_eligible_retirements": True}]

    def test_truncates_to_n(self):
        critiques = [
            {
                "direction_id": f"d-{i}",
                "what_it_addressed": "x",
                "pattern_that_produced_it": "y",
                "lesson": "z",
            }
            for i in range(3)
        ]
        history = [
            {
                "cycle_id": f"sc-{j}",
                "outcome": "completed",
                "retirement_critiques": critiques,
            }
            for j in range(4)
        ]
        result = _build_past_retirement_critiques(history, n=5)
        assert len(result) == 5

    def test_default_n_is_5(self):
        critiques = [
            {
                "direction_id": f"d-{i}",
                "what_it_addressed": "x",
                "pattern_that_produced_it": "y",
                "lesson": "z",
            }
            for i in range(4)
        ]
        history = [
            {
                "cycle_id": f"sc-{j}",
                "outcome": "completed",
                "retirement_critiques": critiques,
            }
            for j in range(10)
        ]
        result = _build_past_retirement_critiques(history)
        assert len(result) == 5


@pytest.mark.asyncio
async def test_retirement_critiques_persisted_via_context_injection(db_session, mocker):
    """Context-injection path: _strategic_retirement_critiques pre-injected into task.context ends up in cycle metadata."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RC-API",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-ret1",
                "status": "expired",
                "text": "Old direction",
                "research_history": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    critiques = [
        {
            "direction_id": "d-ret1",
            "what_it_addressed": "Speed up CI",
            "pattern_that_produced_it": "premature optimization",
            "lesson": "Wait for evidence.",
        }
    ]
    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={
            "_strategic_vision_set": True,
            "strategic_cycle_id": cycle.id,
            "_strategic_retirement_critiques": critiques,
        },
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_strategic_completion(task, "", db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert "retirement_critiques" in meta, (
        "retirement_critiques must be persisted in cycle_metadata"
    )
    assert meta["retirement_critiques"] == critiques


@pytest.mark.asyncio
async def test_memo_citations_persisted_in_cycle_metadata(db_session, mocker):
    """A COMPLETED memo task in the project window populates
    cycle_metadata['memo_citations'] with {task_id, summary_excerpt} entries."""
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="Memo-Cite",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # A COMPLETED memo task with a parseable summary in plan_output.
    memo_task = Task(
        project_id=project.id,
        payloads=[{"type": "memo", "topic": "Dropped finding X"}],
        status=TaskStatus.COMPLETED,
        plan_output={
            "memo": {
                "summary": "Summary of dropped finding X reasoning.",
                "reasoning": "Detail.",
                "citations": [],
                "tags": [],
            }
        },
        created_by="ORCHESTRATOR",
    )
    db_session.add(memo_task)
    await db_session.commit()
    await db_session.refresh(memo_task)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"_strategic_vision_set": True, "strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_strategic_completion(task, "", db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert "memo_citations" in meta, (
        "memo_citations must be persisted in cycle_metadata"
    )
    cites = meta["memo_citations"]
    assert len(cites) == 1
    assert cites[0]["task_id"] == str(memo_task.id)
    assert cites[0]["summary_excerpt"].startswith("Summary of dropped finding X")


@pytest.mark.asyncio
async def test_retirement_critiques_persisted_via_fallback_path(db_session, mocker):
    """Fallback (log-parse) path: retired_direction_critiques from JSON output ends up in cycle metadata."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RC-FB",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    critiques = [
        {
            "direction_id": "d-old1",
            "what_it_addressed": "Caching layer",
            "pattern_that_produced_it": "scope drift",
            "lesson": "Keep scope tight.",
        }
    ]
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [],
            "retired_direction_critiques": critiques,
        }
    )
    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert "retirement_critiques" in meta, (
        "retirement_critiques must be persisted via fallback path"
    )
    assert meta["retirement_critiques"] == critiques


@pytest.mark.asyncio
async def test_no_eligible_sentinel_stored_without_error(db_session, mocker):
    """no_eligible_retirements sentinel is persisted cleanly."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="RC-NE",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [],
            "retired_direction_critiques": {"no_eligible_retirements": True},
        }
    )
    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert meta.get("retirement_critiques") == {"no_eligible_retirements": True}


# ── dedup_reasoning persistence ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_reasoning_persisted_on_successful_create_fallback_path(
    db_session, mocker
):
    """dedup_reasoning dict is stored in direction.context on a successful fallback-path create."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="DR1",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Migrate auth service to OAuth2",
                    "priority": "medium",
                    "success_evidence": "All auth tests pass",
                    "retirement_condition": "OAuth2 flow live for 3 cycles",
                    "dedup_reasoning": {
                        "closest_directions": ["Improve login flow", "Add SSO support"],
                        "distinction": "Targets the auth backend protocol, not the UI flow",
                    },
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [d for d in (project.directions or []) if "OAuth2" in d.get("text", "")]
    assert len(dirs) == 1, "Direction should have been created"
    ctx = dirs[0].get("context") or {}
    dr = ctx.get("dedup_reasoning")
    assert dr is not None, "dedup_reasoning should be stored in context"
    assert dr["closest_directions"] == ["Improve login flow", "Add SSO support"]
    assert dr["distinction"] == "Targets the auth backend protocol, not the UI flow"


@pytest.mark.asyncio
async def test_absent_dedup_reasoning_no_error_on_create(db_session, mocker):
    """Omitting dedup_reasoning entirely causes no error — direction is created normally."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="DR2",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Reduce p99 API latency",
                    "priority": "high",
                    "success_evidence": "p99 < 200ms",
                    "retirement_condition": "3 cycles below target",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [
        d for d in (project.directions or []) if "latency" in d.get("text", "").lower()
    ]
    assert len(dirs) == 1, "Direction should have been created without dedup_reasoning"
    ctx = dirs[0].get("context") or {}
    assert "dedup_reasoning" not in ctx


@pytest.mark.asyncio
async def test_dedup_overlap_creates_advisory_fallback(db_session, mocker):
    """Advisory dedup: a near-duplicate of an active direction is no longer
    hard-blocked on the strategic-agent fallback path — the direction IS created
    and carries an advisory note flagging the resemblance."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    existing_dir = {
        "id": "d-existing1",
        "text": "Migrate auth service to OAuth2",
        "status": "active",
        "priority": "medium",
        "notes": [],
        "research_history": [],
    }
    project = Project(
        name="DR3",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[existing_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Migrate auth service to OAuth2",
                    "priority": "medium",
                    "success_evidence": "All auth tests pass",
                    "retirement_condition": "OAuth2 flow live for 3 cycles",
                    "dedup_reasoning": {
                        "closest_directions": ["d-existing1"],
                        "distinction": "This one focuses on the token refresh path specifically",
                    },
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-existing1"]
    assert len(new_dirs) == 1, (
        "Near-duplicate should be created (advisory, not blocked)"
    )
    created = new_dirs[0]
    assert created.get("status") == "active"
    notes = created.get("notes") or []
    assert any(
        (n.get("text") or "").startswith("Advisory: resembles existing direction")
        for n in notes
    ), "Created direction should carry an advisory-resemblance note"


@pytest.mark.asyncio
async def test_dedup_reasoning_fields_capped_at_max_length(db_session, mocker):
    """closest_directions is capped at 3 items; distinction is capped at 500 chars."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="DR4",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    long_distinction = "x" * 600
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Consolidate logging infrastructure",
                    "priority": "low",
                    "success_evidence": "Single log sink used everywhere",
                    "retirement_condition": "All services use unified sink for 2 cycles",
                    "dedup_reasoning": {
                        "closest_directions": [
                            "dir-a",
                            "dir-b",
                            "dir-c",
                            "dir-d",
                            "dir-e",
                        ],
                        "distinction": long_distinction,
                    },
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    dirs = [
        d for d in (project.directions or []) if "logging" in d.get("text", "").lower()
    ]
    assert len(dirs) == 1
    ctx = dirs[0].get("context") or {}
    dr = ctx.get("dedup_reasoning") or {}
    assert len(dr["closest_directions"]) == 3, (
        "closest_directions should be capped at 3"
    )
    assert len(dr["distinction"]) == 500, "distinction should be capped at 500 chars"


# ── Semantic dedup on direction creates ──────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_dedup_creates_advisory_paraphrased_active_direction_fallback(
    db_session, mocker
):
    """Fallback path: a paraphrase of an active direction is created (advisory),
    not blocked — the agent owns its direction set."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    existing_dir = {
        "id": "d-active1",
        "text": "Migrate authentication service towards OAuth2 token standard",
        "status": "active",
        "priority": "medium",
        "notes": [],
        "research_history": [],
    }
    project = Project(
        name="SD1",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[existing_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Paraphrase of the existing direction (4+ non-stopword words overlap)
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Transition authentication service OAuth2 token implementation",
                    "priority": "medium",
                    "success_evidence": "All auth flows use OAuth2",
                    "retirement_condition": "OAuth2 live for 3 cycles",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-active1"]
    assert len(new_dirs) == 1, "Paraphrased near-duplicate should be created (advisory)"
    created = new_dirs[0]
    assert created.get("status") == "active"
    notes = created.get("notes") or []
    assert any(
        (n.get("text") or "").startswith("Advisory: resembles existing direction")
        for n in notes
    ), "Created direction should carry an advisory-resemblance note"


@pytest.mark.asyncio
async def test_semantic_dedup_allows_3word_overlap_retired_direction_fallback(
    db_session, mocker
):
    """Fallback path: 3-word overlap with a retired direction is no longer blocked (threshold is now 4)."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    retired_dir = {
        "id": "d-ret1",
        "text": "Consolidate logging pipeline centralized aggregation",
        "status": "retired",
        "priority": "low",
        "notes": [],
        "research_history": [],
    }
    project = Project(
        name="SD2",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[retired_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # 3-word overlap with retired direction (consolidate, logging, pipeline) — threshold is now 4
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Consolidate logging pipeline distributed system",
                    "priority": "low",
                    "success_evidence": "Single log pipeline in place",
                    "retirement_condition": "Pipeline stable for 2 cycles",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-ret1"]
    assert len(new_dirs) == 1, (
        "3-word overlap with retired direction should pass through (threshold is 4)"
    )


@pytest.mark.asyncio
async def test_semantic_dedup_creates_advisory_4word_overlap_retired_direction_fallback(
    db_session, mocker
):
    """Fallback path: a 4-word overlap with a retired direction is created
    (advisory) on the strategic-agent path — no longer hard-blocked."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    retired_dir = {
        "id": "d-ret2",
        "text": "Consolidate logging pipeline centralized aggregation infrastructure",
        "status": "retired",
        "priority": "low",
        "notes": [],
        "research_history": [],
    }
    project = Project(
        name="SD2B",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[retired_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # 4-word overlap: consolidate, logging, pipeline, centralized
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Consolidate logging pipeline centralized distributed system",
                    "priority": "low",
                    "success_evidence": "Single log pipeline in place",
                    "retirement_condition": "Pipeline stable for 2 cycles",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-ret2"]
    assert len(new_dirs) == 1, (
        "4-word overlap with retired direction should be created (advisory)"
    )
    created = new_dirs[0]
    assert created.get("status") == "active"
    notes = created.get("notes") or []
    assert any(
        (n.get("text") or "").startswith("Advisory: resembles existing direction")
        for n in notes
    ), "Created direction should carry an advisory-resemblance note"


@pytest.mark.asyncio
async def test_replace_retired_direction_creates_advisory_fallback(db_session, mocker):
    """Fallback path: a replacement proposal that overlaps a retired direction is
    created (advisory) — no distinction field is required anymore."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    retired_dir = {
        "id": "d-ret3",
        "text": "Migrate authentication service to OAuth2 token standard",
        "status": "expired",
        "priority": "medium",
        "notes": [],
        "research_history": [],
    }
    project = Project(
        name="SD2C",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[retired_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # 4-word overlap with the expired direction — created on the agent's judgment.
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Migrate authentication service OAuth2 token refresh rotation",
                    "priority": "medium",
                    "success_evidence": "Refresh token rotation fully implemented",
                    "retirement_condition": "Token rotation live for 3 cycles",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-ret3"]
    assert len(new_dirs) == 1, (
        "Replacement direction overlapping a retired one should be created (advisory)"
    )
    created = new_dirs[0]
    assert created.get("status") == "active"
    notes = created.get("notes") or []
    assert any(
        (n.get("text") or "").startswith("Advisory: resembles existing direction")
        for n in notes
    ), "Created direction should carry an advisory-resemblance note"


@pytest.mark.asyncio
async def test_active_direction_overlap_creates_advisory_fallback(db_session, mocker):
    """Fallback path: a near-duplicate of an ACTIVE direction is created (advisory)
    on the strategic-agent path — the old hard block is gone."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    active_dir = {
        "id": "d-active2",
        "text": "Migrate authentication service OAuth2 token standard",
        "status": "active",
        "priority": "medium",
        "notes": [],
        "research_history": [],
    }
    project = Project(
        name="SD2D",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[active_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Migrate authentication service OAuth2 token refresh rotation",
                    "priority": "medium",
                    "success_evidence": "Refresh token rotation implemented",
                    "retirement_condition": "Rotation live for 3 cycles",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-active2"]
    assert len(new_dirs) == 1, (
        "Near-duplicate of an active direction should be created (advisory)"
    )
    created = new_dirs[0]
    assert created.get("status") == "active"
    notes = created.get("notes") or []
    assert any(
        (n.get("text") or "").startswith("Advisory: resembles existing direction")
        for n in notes
    ), "Created direction should carry an advisory-resemblance note"


@pytest.mark.asyncio
async def test_semantic_dedup_allows_distinct_direction_fallback(db_session, mocker):
    """Fallback path: genuinely distinct direction passes through semantic dedup."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    existing_dir = {
        "id": "d-existing2",
        "text": "Migrate authentication service towards OAuth2 token standard",
        "status": "active",
        "priority": "medium",
        "notes": [],
        "research_history": [],
    }
    project = Project(
        name="SD3",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[existing_dir],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Genuinely distinct — no meaningful word overlap with existing
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "create",
                    "text": "Reduce database query latency via connection pooling",
                    "priority": "high",
                    "success_evidence": "p99 query time below 50ms",
                    "retirement_condition": "Latency stable for 2 cycles",
                }
            ],
        }
    )
    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    new_dirs = [d for d in (project.directions or []) if d.get("id") != "d-existing2"]
    assert len(new_dirs) == 1, "Distinct direction should have been created"


# ── set_retirement_critiques HTTP API path ─────────────────────────────────────


@pytest.mark.asyncio
async def test_set_retirement_critiques_list_payload_via_http(
    client, db_session, mocker
):
    """HTTP API: POST /api/agent/strategic with a list payload returns 200 and stores critiques."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="RC-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-ret2",
                "status": "expired",
                "text": "Old dir",
                "research_history": [],
            }
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-rc-http",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id, "_strategic_vision_set": True},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-rc-http", task_type="strategic_review")
    critiques = [
        {
            "direction_id": "d-ret2",
            "what_it_addressed": "Speed up CI",
            "pattern_that_produced_it": "premature optimization",
            "lesson": "Wait for evidence.",
        }
    ]
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_retirement_critiques",
            "data": critiques,
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "set_retirement_critiques"
    assert body["count"] == 1

    await db_session.refresh(task)
    stored = (task.context or {}).get("_strategic_retirement_critiques")
    assert stored == critiques, f"Expected stored critiques, got: {stored}"


@pytest.mark.asyncio
async def test_set_retirement_critiques_sentinel_via_http(client, db_session, mocker):
    """HTTP API: sentinel dict {'no_eligible_retirements': true} returns 200 and is stored."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="RC-HTTP-SENT",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-rc-sent",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-rc-sent", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_retirement_critiques",
            "data": {"no_eligible_retirements": True},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0

    await db_session.refresh(task)
    stored = (task.context or {}).get("_strategic_retirement_critiques")
    assert stored == {"no_eligible_retirements": True}


@pytest.mark.asyncio
async def test_set_retirement_critiques_invalid_dict_returns_400(
    client, db_session, mocker
):
    """HTTP API: a non-sentinel dict payload returns 400."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="RC-HTTP-BAD",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-rc-bad",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-rc-bad", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_retirement_critiques",
            "data": {"some": "dict"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text


# ── set_evidence_progress HTTP API path ────────────────────────────────────────


async def _make_strategic_task(db_session, *, name, vm, directions=None):
    """Build a committed project + cycle + strategic_review task; return the task."""
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name=name,
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=directions or [],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id=vm,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_set_evidence_progress_list_via_http(client, db_session):
    """HTTP API: a valid list payload returns 200 and stashes verdicts in task.context."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="EP-HTTP", vm="vm-ep")
    token = create_agent_token(str(task.id), "vm-ep", task_type="strategic_review")
    verdicts = [
        {"direction_id": "d-1", "status": "advancing", "rationale": "landed finding F"},
        {"direction_id": "d-2", "status": "stalled", "rationale": "no new evidence"},
    ]
    resp = await client.post(
        "/api/agent/strategic",
        json={"task_id": str(task.id), "op": "set_evidence_progress", "data": verdicts},
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "set_evidence_progress"
    assert body["count"] == 2

    await db_session.refresh(task)
    stored = (task.context or {}).get(EVIDENCE_PROGRESS_CONTEXT_KEY)
    assert stored == verdicts
    assert (task.context or {}).get("_strategic_api_called") is True


@pytest.mark.asyncio
async def test_set_evidence_progress_invalid_status_returns_400(client, db_session):
    """HTTP API: an out-of-enum status rejects the whole payload with 400."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="EP-BADSTATUS", vm="vm-ep-bs")
    token = create_agent_token(str(task.id), "vm-ep-bs", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_evidence_progress",
            "data": [
                {"direction_id": "d-1", "status": "progressing", "rationale": "x"}
            ],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text
    await db_session.refresh(task)
    assert EVIDENCE_PROGRESS_CONTEXT_KEY not in (task.context or {})


@pytest.mark.asyncio
async def test_cite_memos_does_not_set_api_called(client, db_session):
    """cite_memos persists cited IDs but must NOT flip _strategic_api_called.

    cite_memos is auto-invoked by the thinker pass before the executor runs,
    so flagging api-usage would mask a no-op/broken executor cycle as a success
    and suppress the failure circuit-breaker.
    """
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="CM-HTTP", vm="vm-cm")
    token = create_agent_token(str(task.id), "vm-cm", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "cite_memos",
            "data": {"cited_memo_ids": ["memo-1", "memo-2"]},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "cite_memos"
    assert body["count"] == 2

    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_cited_memo_ids") == [
        "memo-1",
        "memo-2",
    ]
    assert (task.context or {}).get("_strategic_api_called") is not True


@pytest.mark.asyncio
async def test_set_evidence_progress_non_list_returns_400(client, db_session):
    """HTTP API: a non-list payload (dict) is rejected with 400."""
    from app.auth import create_agent_token

    task = await _make_strategic_task(db_session, name="EP-NONLIST", vm="vm-ep-nl")
    token = create_agent_token(str(task.id), "vm-ep-nl", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_evidence_progress",
            "data": {"direction_id": "d-1", "status": "advancing"},
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_set_evidence_progress_end_to_end_persists_on_direction(
    client, db_session, mocker
):
    """Smoke: submit verdict via HTTP -> stash -> completion handler persists on direction."""
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    task = await _make_strategic_task(
        db_session,
        name="EP-E2E",
        vm="vm-ep-e2e",
        directions=[
            {
                "id": "d-live",
                "status": "active",
                "text": "Long runner",
                "research_history": [{}, {}, {}],
                "history_aggregate": {"total_cycles": 9},
            }
        ],
    )
    token = create_agent_token(str(task.id), "vm-ep-e2e", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_evidence_progress",
            "data": [
                {
                    "direction_id": "d-live",
                    "status": "met",
                    "rationale": "evidence satisfied",
                }
            ],
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(task)
    project = await db_session.get(Project, task.project_id)
    await _handle_strategic_completion(task, "", db_session, project=project)

    await db_session.refresh(project)
    ep = project.directions[0]["evidence_progress"]
    assert ep["status"] == "met"
    assert ep["rationale"] == "evidence satisfied"
    assert ep["cycle_index"] == 12  # 3 history + 9 aggregate
    assert ep["cycle_task_id"] == str(task.id)


# ── superseded status + successor linkage ─────────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_completion_fallback_superseded_update_persists_superseded_by(
    db_session, mocker
):
    """Fallback op=update with status=superseded persists superseded_by on the direction."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="SUP-FB",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-old",
                "text": "Old direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-old",
                    "rationale": "superseded by newer approach",
                    "changes": {
                        "status": "superseded",
                        "superseded_by": {
                            "direction_id": "d-new",
                            "set_at": "2026-01-01T00:00:00",
                        },
                    },
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-old")
    assert target.get("status") == "superseded", (
        f"Expected status=superseded, got: {target.get('status')}"
    )
    assert target.get("superseded_by") == {
        "direction_id": "d-new",
        "set_at": "2026-01-01T00:00:00",
    }, f"Expected superseded_by to be persisted, got: {target.get('superseded_by')}"


@pytest.mark.asyncio
async def test_strategic_completion_fallback_superseded_derives_superseded_by_from_successor_id(
    db_session, mocker
):
    """Fallback op=update: when changes lacks superseded_by, derive it from changes.successor_id."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="SUP-FB2",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-legacy",
                "text": "Legacy direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-legacy",
                    "successor_id": "d-successor",
                    "rationale": "replaced",
                    "changes": {"status": "superseded"},
                },
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-legacy")
    assert target.get("status") == "superseded"
    sb = target.get("superseded_by")
    assert sb is not None, "superseded_by must be derived from successor_id"
    assert sb.get("direction_id") == "d-successor"
    assert "set_at" in sb


@pytest.mark.asyncio
async def test_strategic_http_update_direction_superseded_persists_superseded_by(
    client, db_session, mocker
):
    """HTTP update_direction with status=superseded persists superseded_by and returns 200."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="SUP-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-http-old",
                "text": "HTTP old direction",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-sup-http",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-sup-http", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-http-old",
                "rationale": "superseded by new direction",
                "changes": {
                    "status": "superseded",
                    "superseded_by": {
                        "direction_id": "d-http-new",
                        "set_at": "2026-01-01T00:00:00",
                    },
                },
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    target = next(d for d in (project.directions or []) if d.get("id") == "d-http-old")
    assert target.get("status") == "superseded"
    assert target.get("superseded_by") == {
        "direction_id": "d-http-new",
        "set_at": "2026-01-01T00:00:00",
    }


@pytest.mark.asyncio
async def test_strategic_http_update_direction_clears_scaffolding_state_on_outcome_change(
    client, db_session, mocker
):
    """HTTP update_direction resets stale scaffolding state when outcome fields change."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    project = Project(
        name="SCAFFOLD-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-http-scaffold",
                "text": "Track outcome change resets",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
                "context": {
                    "success_evidence": "Existing goal",
                    "retirement_condition": "Existing exit",
                },
                "outcome_progress_ever_met": True,
                "outcome_progress_history": [{"progress": 4, "cycle_index": 9}],
                "evidence_progress": {
                    "status": "advancing",
                    "rationale": "stale evidence",
                },
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-scaffold-http",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-scaffold-http", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-http-scaffold",
                "changes": {"success_evidence": "New goal: zero P0 incidents"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    target = next(
        d for d in (project.directions or []) if d.get("id") == "d-http-scaffold"
    )
    assert target["context"]["success_evidence"] == "New goal: zero P0 incidents"
    assert target["context"]["retirement_condition"] == "Existing exit"
    assert "outcome_progress_ever_met" not in target
    # Either popped (scaffolding reset) or reset to [] (outcome-detection reset).
    # Both forms mean "cleared" and consumers read via `... or []`.
    assert not target.get("outcome_progress_history")
    assert "evidence_progress" not in target


@pytest.mark.asyncio
async def test_strategic_http_update_direction_superseded_stamps_completed_at_and_appears_in_summary(
    client, db_session, mocker
):
    """HTTP update_direction with status=superseded must stamp completed_at so
    _build_retired_directions_summary's 90-day time-window filter includes it."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle
    from app.strategic_analysis import _build_retired_directions_summary

    project = Project(
        name="SUP-HTTP-COMPLETED-AT",
        user_id="u-completed-at",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-http-sup-window",
                "text": "HTTP direction to be superseded",
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-sup-window",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-sup-window", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "update_direction",
            "data": {
                "direction_id": "d-http-sup-window",
                "rationale": "superseded via HTTP path",
                "changes": {
                    "status": "superseded",
                    "superseded_by": {
                        "direction_id": "d-http-successor",
                        "set_at": "2026-01-01T00:00:00",
                    },
                },
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(project)
    target = next(
        d for d in (project.directions or []) if d.get("id") == "d-http-sup-window"
    )
    assert target.get("status") == "superseded"
    assert target.get("completed_at"), (
        "HTTP update_direction -> status=superseded must stamp completed_at"
    )

    summary = _build_retired_directions_summary(project, window_days=90)
    summary_ids = [e.get("id") for e in summary]
    assert "d-http-sup-window" in summary_ids, (
        f"superseded direction (via HTTP) should appear in retired summary; got ids={summary_ids}"
    )


# ── _strategic_dedup_rejections persistence ───────────────────────────────────


@pytest.mark.asyncio
async def test_strategic_http_create_direction_dedup_creates_advisory(
    client, db_session, mocker
):
    """HTTP create_direction on a near-duplicate of an active direction now CREATES
    the direction (advisory): returns 200, the directions list grows, and the new
    direction carries an advisory-resemblance note. The old 409 + rejection record
    is gone."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    existing_text = "Migrate authentication service to OAuth2 tokens"
    project = Project(
        name="DEDUP-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-exist",
                "text": existing_text,
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-dedup",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-dedup", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": existing_text,
                "priority": "medium",
                "rationale": "same direction",
                "success_evidence": "All tasks pass",
                "retirement_condition": "No open tasks",
                "dedup_reasoning": {
                    "distinction": "slightly different framing",
                    "closest_directions": ["d-exist"],
                },
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("status") == "ok"
    assert body.get("op") == "create_direction"
    new_id = body.get("direction_id")
    assert new_id

    await db_session.refresh(project)
    assert len(project.directions or []) == 2, (
        "Directions list should have grown by one"
    )
    created = next(d for d in project.directions if d.get("id") == new_id)
    assert created.get("status") == "active"
    notes = created.get("notes") or []
    assert any(
        (n.get("text") or "").startswith("Advisory: resembles existing direction")
        for n in notes
    ), "Created direction should carry an advisory-resemblance note"

    await db_session.refresh(task)
    ctx = task.context or {}
    assert "_strategic_dedup_rejections" not in ctx, (
        "The old rejection-record path should be gone"
    )


@pytest.mark.asyncio
async def test_api_path_overlap_retired_direction_creates_advisory(
    client, db_session, mocker
):
    """HTTP create_direction overlapping a retired direction succeeds (200) and
    carries an advisory-resemblance note — no distinction field required."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    retired_text = "Migrate authentication service to OAuth2 tokens standard"
    project = Project(
        name="DEDUP-RETIRE-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-retired",
                "text": retired_text,
                "status": "expired",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-retire-dedup",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-retire-dedup", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Migrate authentication service OAuth2 tokens refresh rotation",
                "priority": "medium",
                "rationale": "replacement direction targeting token refresh specifically",
                "success_evidence": "Refresh token rotation fully implemented",
                "retirement_condition": "Rotation stable for 3 cycles",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, (
        f"Expected 200 but got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("status") == "ok"
    assert body.get("op") == "create_direction"
    new_id = body.get("direction_id")
    assert new_id

    await db_session.refresh(project)
    assert len(project.directions or []) == 2, (
        "Directions list should have grown by one"
    )
    created = next(d for d in project.directions if d.get("id") == new_id)
    assert created.get("status") == "active"
    notes = created.get("notes") or []
    assert any(
        (n.get("text") or "").startswith("Advisory: resembles existing direction")
        for n in notes
    ), "Created direction should carry an advisory-resemblance note"


@pytest.mark.asyncio
async def test_api_path_active_direction_overlap_creates_advisory(
    client, db_session, mocker
):
    """HTTP create_direction overlapping an ACTIVE direction now creates the
    direction (advisory, 200) instead of the old 409 hard block."""
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app import strategic_cycle

    active_text = "Migrate authentication service to OAuth2 tokens standard"
    project = Project(
        name="DEDUP-ACTIVE-HTTP",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[
            {
                "id": "d-active3",
                "text": active_text,
                "status": "active",
                "priority": "medium",
                "notes": [],
                "research_history": [],
            },
        ],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-active-dedup",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-active-dedup", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "create_direction",
            "data": {
                "text": "Migrate authentication service OAuth2 tokens refresh rotation",
                "priority": "medium",
                "rationale": "near-duplicate of active direction",
                "success_evidence": "Refresh token rotation implemented",
                "retirement_condition": "Rotation stable for 3 cycles",
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, (
        f"Expected 200 but got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("status") == "ok"
    new_id = body.get("direction_id")
    assert new_id

    await db_session.refresh(project)
    assert len(project.directions or []) == 2, (
        "Directions list should have grown by one"
    )
    created = next(d for d in project.directions if d.get("id") == new_id)
    assert created.get("status") == "active"
    notes = created.get("notes") or []
    assert any(
        (n.get("text") or "").startswith("Advisory: resembles existing direction")
        for n in notes
    ), "Created direction should carry an advisory-resemblance note"


# ── _build_outcome_evaluation: evidence block ─────────────────────────────────


class TestBuildOutcomeEvaluationEvidence:
    """_build_outcome_evaluation surfaces evidence from recent_findings_evidence.

    Asserts that the evidence block is included when present and meaningful,
    omitted gracefully when absent, and that failed findings are excluded.
    """

    def _make_project(self, **dir_kwargs):
        d = {
            "id": "d-ev1",
            "status": "active",
            "text": "Improve caching layer",
            "research_history": [],
        }
        d.update(dir_kwargs)
        return Project(name="P", user_id="u1", directions=[d], vision={})

    def test_evidence_included_when_present_with_claims(self):
        evidence = [
            {
                "title": "Finding A",
                "claims": ["c1", "c2"],
                "source_refs": [{"ref": "backend/foo.py"}],
            }
        ]
        project = self._make_project(recent_findings_evidence=evidence)
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert "evidence" in direction
        assert len(direction["evidence"]) == 1
        assert direction["evidence"][0]["title"] == "Finding A"
        assert "c1" in direction["evidence"][0]["claims"]

    def test_evidence_omitted_when_no_recent_findings_evidence_key(self):
        project = self._make_project()  # no recent_findings_evidence
        result = _build_outcome_evaluation(project)
        assert "evidence" not in result["directions"][0]

    def test_evidence_omitted_for_empty_recent_findings_evidence(self):
        project = self._make_project(recent_findings_evidence=[])
        result = _build_outcome_evaluation(project)
        assert "evidence" not in result["directions"][0]

    def test_finding_with_no_claims_and_no_source_refs_excluded(self):
        evidence = [{"title": "Empty Finding", "claims": [], "source_refs": []}]
        project = self._make_project(recent_findings_evidence=evidence)
        result = _build_outcome_evaluation(project)
        assert "evidence" not in result["directions"][0]

    def test_failed_findings_excluded_from_evidence(self):
        ctx = "## Established Findings\n- Finding A (failed: build error)\n"
        evidence = [
            {
                "title": "Finding A",
                "claims": ["c1"],
                "source_refs": [{"ref": "backend/foo.py"}],
            }
        ]
        project = self._make_project(
            research_context=ctx, recent_findings_evidence=evidence
        )
        result = _build_outcome_evaluation(project)
        assert "evidence" not in result["directions"][0]

    def test_proposed_findings_included(self):
        ctx = "## Established Findings\n- Finding B (proposed)\n"
        evidence = [{"title": "Finding B", "claims": ["c1"], "source_refs": []}]
        project = self._make_project(
            research_context=ctx, recent_findings_evidence=evidence
        )
        result = _build_outcome_evaluation(project)
        ev = result["directions"][0].get("evidence")
        assert ev is not None
        assert ev[0]["title"] == "Finding B"

    def test_claims_capped_at_3_per_finding(self):
        evidence = [
            {
                "title": "Big Finding",
                "claims": [f"claim{i}" for i in range(6)],
                "source_refs": [{"ref": "f.py"}],
            }
        ]
        project = self._make_project(recent_findings_evidence=evidence)
        result = _build_outcome_evaluation(project)
        ev = result["directions"][0].get("evidence")
        assert ev is not None
        assert len(ev[0]["claims"]) <= 3

    def test_source_refs_capped_at_3_per_finding(self):
        refs = [{"ref": f"backend/file{i}.py"} for i in range(6)]
        evidence = [{"title": "Big Finding", "claims": ["c1"], "source_refs": refs}]
        project = self._make_project(recent_findings_evidence=evidence)
        result = _build_outcome_evaluation(project)
        ev = result["directions"][0].get("evidence")
        assert ev is not None
        assert len(ev[0]["source_refs"]) <= 3

    def test_total_evidence_entries_capped_at_5(self):
        evidence = [
            {
                "title": f"Finding {i}",
                "claims": [f"c{i}"],
                "source_refs": [{"ref": f"f{i}.py"}],
            }
            for i in range(8)
        ]
        project = self._make_project(recent_findings_evidence=evidence)
        result = _build_outcome_evaluation(project)
        ev = result["directions"][0].get("evidence")
        assert ev is not None
        assert len(ev) <= 5

    # ── confirmed_finding_count and confirmed_finding_trend ───────────────────

    def test_confirmed_finding_count_from_outcome_progress_history(self):
        # outcome_progress_history contains cumulative confirmed-finding counts
        # from the eviction-robust signal; the count should be the latest value.
        project = self._make_project(
            outcome_progress_history=[
                {"progress": 2},
                {"progress": 3},
                {"progress": 5},
            ]
        )
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert direction.get("confirmed_finding_count") == 5

    def test_confirmed_finding_count_omitted_when_no_history(self):
        # No outcome_progress_history → confirmed_finding_count should not be present.
        project = self._make_project()
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert "confirmed_finding_count" not in direction

    def test_confirmed_finding_count_omitted_when_history_empty(self):
        # Empty outcome_progress_history → confirmed_finding_count should not be present.
        project = self._make_project(outcome_progress_history=[])
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert "confirmed_finding_count" not in direction

    def test_confirmed_finding_trend_improving(self):
        # Trend is improving when second half avg > first half avg * 1.3.
        # [1, 1, 5, 5, 5, 5] -> first_avg = 1, second_avg = 5, 5 > 1.3 -> improving
        project = self._make_project(
            outcome_progress_history=[
                {"progress": 1},
                {"progress": 1},
                {"progress": 5},
                {"progress": 5},
                {"progress": 5},
                {"progress": 5},
            ]
        )
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert direction.get("confirmed_finding_trend") == "improving"

    def test_confirmed_finding_trend_declining(self):
        # Trend is declining when second half avg < first half avg * 0.7.
        # [10, 10, 2, 2, 2, 2] -> first_avg = 10, second_avg = 2, 2 < 7 -> declining
        project = self._make_project(
            outcome_progress_history=[
                {"progress": 10},
                {"progress": 10},
                {"progress": 2},
                {"progress": 2},
                {"progress": 2},
                {"progress": 2},
            ]
        )
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert direction.get("confirmed_finding_trend") == "declining"

    def test_confirmed_finding_trend_stable(self):
        # Trend is stable when between 0.7 and 1.3 thresholds.
        # [5, 5, 5, 5, 5, 5] -> both halves same -> stable
        project = self._make_project(
            outcome_progress_history=[
                {"progress": 5},
                {"progress": 5},
                {"progress": 5},
                {"progress": 5},
                {"progress": 5},
                {"progress": 5},
            ]
        )
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert direction.get("confirmed_finding_trend") == "stable"

    def test_confirmed_finding_trend_omitted_with_single_snapshot(self):
        # Trend requires at least 2 snapshots to compute.
        project = self._make_project(outcome_progress_history=[{"progress": 3}])
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert direction.get("confirmed_finding_count") == 3
        assert "confirmed_finding_trend" not in direction

    def test_confirmed_finding_trend_handles_odd_length_history(self):
        # Odd-length history: [1, 1, 1, 7, 7, 7, 7] -> first_avg = 1, second_avg ≈ 6.67
        # 6.67 > 1.3 -> improving
        project = self._make_project(
            outcome_progress_history=[
                {"progress": 1},
                {"progress": 1},
                {"progress": 1},
                {"progress": 7},
                {"progress": 7},
                {"progress": 7},
                {"progress": 7},
            ]
        )
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert direction.get("confirmed_finding_trend") == "improving"

    def test_confirmed_finding_trend_with_zero_progress(self):
        # All zeros: first_avg = 0, second_avg = 0 -> stable (within 0.7-1.3 range)
        project = self._make_project(
            outcome_progress_history=[
                {"progress": 0},
                {"progress": 0},
                {"progress": 0},
                {"progress": 0},
            ]
        )
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert direction.get("confirmed_finding_count") == 0
        assert direction.get("confirmed_finding_trend") == "stable"

    def test_confirmed_finding_count_handles_none_progress_values(self):
        # Some snapshots may have None or missing progress field; treat as 0.
        project = self._make_project(
            outcome_progress_history=[
                {"progress": 2},
                {"progress": None},
                {"progress": 5},
            ]
        )
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        # Latest progress should be 5, not None
        assert direction.get("confirmed_finding_count") == 5

    def test_confirmed_finding_fields_coexist_with_evidence(self):
        # Both evidence and confirmed-finding fields should appear together.
        evidence = [
            {
                "title": "Finding A",
                "claims": ["c1"],
                "source_refs": [{"ref": "backend/foo.py"}],
            }
        ]
        project = self._make_project(
            recent_findings_evidence=evidence,
            outcome_progress_history=[
                {"progress": 2},
                {"progress": 5},
            ],
        )
        result = _build_outcome_evaluation(project)
        direction = result["directions"][0]
        assert "evidence" in direction
        assert direction.get("confirmed_finding_count") == 5
        assert direction.get("confirmed_finding_trend") == "improving"


class TestFormatFindingsForPrompt:
    """format_findings_for_prompt: compact prompt block from query_findings_index rows."""

    def _row(self, title, grade="MEDIUM", desc="", claims=None, sources=None):
        return {
            "finding_title": title,
            "finding_description": desc,
            "actionability_grade": grade,
            "claims": claims or [],
            "sources": sources or [],
        }

    def test_empty_rows_returns_empty_string(self):
        from app.strategic_completion import format_findings_for_prompt

        assert format_findings_for_prompt([]) == ""

    def test_single_row_no_claims_no_sources(self):
        from app.strategic_completion import format_findings_for_prompt

        row = self._row("Title A", grade="HIGH", desc="Some description")
        result = format_findings_for_prompt([row])
        assert "[HIGH] Title A: Some description" in result
        assert "claims" not in result
        assert "sources" not in result

    def test_row_with_claims_and_sources(self):
        from app.strategic_completion import format_findings_for_prompt

        row = self._row(
            "Title B",
            grade="HIGH",
            desc="Desc",
            claims=["claim X", "claim Y"],
            sources=[{"type": "file", "ref": "backend/app/foo.py"}],
        )
        result = format_findings_for_prompt([row])
        assert "claim X" in result
        assert "backend/app/foo.py" in result

    def test_max_rows_respected(self):
        from app.strategic_completion import format_findings_for_prompt

        rows = [self._row(f"T{i}") for i in range(20)]
        result = format_findings_for_prompt(rows, max_rows=5)
        assert result.count("\n") == 4  # 5 lines = 4 newlines
        assert "T0" in result
        assert "T5" not in result


# ── Routine event digest → project.running_update ────────────────────────────


@pytest.mark.asyncio
async def test_strategic_completion_appends_routine_digest_to_running_update(
    db_session, mocker
):
    """Fallback path: messages with routine event_types append a one-line digest
    to project.running_update in addition to creating a ProjectMessage row."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
        running_update=None,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "messages": [
                {
                    "type": "NOTE",
                    "content": "PR #42 merged successfully",
                    "context": {"event_type": "pr_merged"},
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    assert project.running_update is not None
    assert "pr_merged" in project.running_update
    assert "PR #42 merged" in project.running_update


# ── version_phase_decision transport (automain LTS Phase 2b) ─────────────────


@pytest.mark.asyncio
async def test_version_phase_decision_flows_end_to_end(client, db_session, mocker):
    """End-to-end: a version_phase_decision embedded in a set_problem_model call
    rides task.context through to strategic completion and flips the open
    ReleaseVersion's phase.

    This is the seam Phase 2b connects: the agent embeds version_phase_decision
    as a top-level sibling of the problem_model; the /api/agent/strategic
    endpoint must lift it out (ProblemModel is extra="forbid"), stash it on
    task.context, and the completion handler must apply it to the open version.
    The test exercises the REAL HTTP transport, not a mocked context write.
    """
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import release_version as rv
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="VPD-E2E",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open ReleaseVersion in the build phase — the decision should flip it.
    version = await rv.open_version(db_session, project.id, version_number=1)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.phase == "build"

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-vpd-e2e",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-vpd-e2e", task_type="strategic_review")

    # The agent embeds version_phase_decision as a TOP-LEVEL SIBLING of the
    # problem_model fields — exactly what the research-plan executor prompt
    # instructs. The endpoint must strip it before ProblemModel validation.
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Ship the thing", "current_focus": "harden"},
                "capabilities": [],
                "gaps": [],
                "version_phase_decision": {
                    "phase": "stabilize",
                    "reasoning": "Enough features landed; time to harden.",
                },
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "set_problem_model"
    assert body["version_phase_decision_received"] is True

    # The transient signal must NOT linger in the stored problem_model.
    await db_session.refresh(project)
    assert "version_phase_decision" not in (project.problem_model or {})
    assert (project.problem_model or {}).get("intent", {}).get("purpose") == (
        "Ship the thing"
    )

    # It rides task.context to the completion handler.
    await db_session.refresh(task)
    stored = (task.context or {}).get("_strategic_version_phase_decision")
    assert stored == {
        "phase": "stabilize",
        "reasoning": "Enough features landed; time to harden.",
    }

    # Completion handler's API path applies the decision to the open version.
    await _handle_strategic_completion(task, "", db_session)

    await db_session.refresh(version)
    assert version.phase == "stabilize", (
        f"Expected open ReleaseVersion to flip to stabilize, got {version.phase!r}"
    )
    assert version.phase_changed_at is not None
    assert version.phase_reasoning == "Enough features landed; time to harden."

    # Sanity: still exactly one open version, still v1.
    open_ver = await rv.current_open_version(db_session, project.id)
    assert open_ver is not None
    assert open_ver.id == version.id
    assert open_ver.version_number == 1


@pytest.mark.asyncio
async def test_set_problem_model_with_version_phase_decision_does_not_400(
    client, db_session
):
    """Embedding version_phase_decision in set_problem_model must not break the
    problem_model write itself.

    ProblemModel is extra="forbid"; before Phase 2b the sibling key reached
    apply_agent_update unstripped and the whole call 400'd — silently breaking
    every cycle that emitted a phase decision. The endpoint must strip it first
    so the revision still bumps.
    """
    from app.auth import create_agent_token
    from app.models import TaskStatus

    project = Project(
        name="VPD-NO400",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-vpd-no400",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-vpd-no400", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Build"},
                "version_phase_decision": {"phase": "build", "reasoning": "keep going"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # problem_model write succeeded: revision bumped off the empty baseline.
    assert body["revision"] == 1
    assert body["version_phase_decision_received"] is True


@pytest.mark.asyncio
async def test_set_problem_model_ignores_invalid_version_phase_decision(
    client, db_session
):
    """A malformed version_phase_decision (bad phase) is dropped, not stored, and
    does not block the problem_model write."""
    from app.auth import create_agent_token
    from app.models import TaskStatus

    project = Project(
        name="VPD-BAD",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-vpd-bad",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-vpd-bad", task_type="strategic_review")
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Build"},
                "version_phase_decision": {"phase": "not-a-phase"},
            },
        },
        headers={"x-agent-token": token},
    )
    # Write still succeeds; the junk decision is simply not received.
    assert resp.status_code == 200, resp.text
    assert resp.json()["version_phase_decision_received"] is False

    await db_session.refresh(task)
    assert "_strategic_version_phase_decision" not in (task.context or {})


# ── qualify_request transport (automain LTS Phase 3 / batch 3b) ──────────────


@pytest.mark.asyncio
async def test_qualify_request_flows_end_to_end(client, db_session, mocker):
    """End-to-end: a qualify_request embedded in a set_problem_model call rides
    task.context through to strategic completion and triggers the release
    qualifier when the open ReleaseVersion is in the stabilize phase.

    This is the seam Phase 3 batch 3b connects: the strategic agent embeds
    qualify_request as a top-level sibling of the problem_model; the
    /api/agent/strategic endpoint must lift it out (ProblemModel is
    extra="forbid"), stash it on task.context, and the completion handler must
    call release_qualifier.run_qualification on the open version. The test
    exercises the REAL HTTP transport, not a mocked context write — the absence
    of exactly this kind of end-to-end test let the version_phase_decision seam
    silently break earlier.
    """
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import release_version as rv
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    # The real qualifier resolves GitHub credentials and spawns child tasks.
    # Stub it: the seam under test is "qualify_request reaches run_qualification",
    # not the qualifier internals (those are tested in test_release_qualifier).
    run_qualification = mocker.patch(
        "app.release_qualifier.run_qualification",
        new_callable=AsyncMock,
        return_value={"status": "spawned"},
    )

    project = Project(
        name="QR-E2E",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open ReleaseVersion in the stabilize phase — qualification only runs from
    # stabilize, so the version must be flipped there first.
    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="hardening")
    db_session.add(version)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.phase == "stabilize"

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-qr-e2e",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(str(task.id), "vm-qr-e2e", task_type="strategic_review")

    # The agent embeds qualify_request as a TOP-LEVEL SIBLING of the
    # problem_model fields — exactly what the research-plan executor prompt
    # instructs. The endpoint must strip it before ProblemModel validation.
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Ship the thing", "current_focus": "qualify"},
                "capabilities": [],
                "gaps": [],
                "qualify_request": {
                    "reason": "Hardening is sufficient; the version is release-ready.",
                },
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["op"] == "set_problem_model"
    assert body["qualify_request_received"] is True

    # The transient signal must NOT linger in the stored problem_model.
    await db_session.refresh(project)
    assert "qualify_request" not in (project.problem_model or {})
    assert (project.problem_model or {}).get("intent", {}).get("purpose") == (
        "Ship the thing"
    )

    # It rides task.context to the completion handler.
    await db_session.refresh(task)
    stored = (task.context or {}).get("_strategic_qualify_request")
    assert stored == {
        "reason": "Hardening is sufficient; the version is release-ready.",
    }

    # The qualifier has not run yet — only set_problem_model has been called.
    run_qualification.assert_not_called()

    # Completion handler's API path triggers qualification on the open version.
    await _handle_strategic_completion(task, "", db_session)

    run_qualification.assert_awaited_once()
    call_args = run_qualification.await_args.args
    # run_qualification(session, project_id, version)
    assert call_args[0] is db_session
    assert call_args[1] == project.id
    assert call_args[2].id == version.id


@pytest.mark.asyncio
async def test_qualify_request_skipped_when_version_not_in_stabilize(
    client, db_session, mocker
):
    """A qualify_request is dropped (qualifier not run) when the open version is
    still in the build phase — qualification only runs from stabilize.

    The transport still works (the field is stripped + stashed), but the
    completion handler's phase gate blocks run_qualification.
    """
    from unittest.mock import AsyncMock
    from app.auth import create_agent_token
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import release_version as rv
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)
    run_qualification = mocker.patch(
        "app.release_qualifier.run_qualification",
        new_callable=AsyncMock,
        return_value={"status": "spawned"},
    )

    project = Project(
        name="QR-BUILD",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Open ReleaseVersion left in the default build phase.
    version = await rv.open_version(db_session, project.id, version_number=1)
    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()
    await db_session.refresh(version)
    assert version.phase == "build"

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-qr-build",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={"strategic_cycle_id": cycle.id},
        strategic_cycle_id=cycle.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-qr-build", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Build"},
                "qualify_request": {"reason": "premature ask"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    # The transport works — the field is received and stashed.
    assert resp.json()["qualify_request_received"] is True
    await db_session.refresh(task)
    assert (task.context or {}).get("_strategic_qualify_request") == {
        "reason": "premature ask"
    }

    # But the completion handler's stabilize gate blocks the qualifier.
    await _handle_strategic_completion(task, "", db_session)
    run_qualification.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_problem_model_with_qualify_request_does_not_400(client, db_session):
    """Embedding qualify_request in set_problem_model must not break the
    problem_model write itself.

    ProblemModel is extra="forbid"; an unstripped sibling key would reach
    apply_agent_update and 400 the whole call. The endpoint must strip it first
    so the revision still bumps.
    """
    from app.auth import create_agent_token
    from app.models import TaskStatus

    project = Project(
        name="QR-NO400",
        user_id="u1",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        vm_instance_id="vm-qr-no400",
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    token = create_agent_token(
        str(task.id), "vm-qr-no400", task_type="strategic_review"
    )
    resp = await client.post(
        "/api/agent/strategic",
        json={
            "task_id": str(task.id),
            "op": "set_problem_model",
            "data": {
                "intent": {"purpose": "Build"},
                "qualify_request": {"reason": "ready"},
            },
        },
        headers={"x-agent-token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # problem_model write succeeded: revision bumped off the empty baseline.
    assert body["revision"] == 1
    assert body["qualify_request_received"] is True


# ── _compute_lifetime_burn_metrics ───────────────────────────────────────────


class TestComputeLifetimeBurnMetrics:
    def test_returns_none_when_no_history_and_no_aggregate(self):
        d = _direction(research_history=[], history_aggregate={})
        assert _compute_lifetime_burn_metrics(d) is None

    def test_window_only_no_aggregate(self):
        # 5 cycles: 5 completed, 0 merged each → lifetime_non_merge_finished=25
        d = _direction(
            research_history=_history(
                5, tasks_completed=5, tasks_failed=0, prs_merged=0
            )
        )
        m = _compute_lifetime_burn_metrics(d)
        assert m is not None
        assert m["lifetime_non_merge_finished"] == 25
        assert m["lifetime_merged"] == 0
        assert m["lifetime_merge_rate"] == 0.0

    def test_aggregate_and_window_combined(self):
        # aggregate has 100 completed/0 merged (evicted cycles)
        # window has 5 cycles: 10 completed, 0 merged
        agg = {
            "total_prs_merged": 0,
            "total_tasks_completed": 100,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(
            research_history=_history(
                5, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate=agg,
        )
        m = _compute_lifetime_burn_metrics(d)
        assert m is not None
        assert m["lifetime_non_merge_finished"] == 110  # 100 + 10
        assert m["lifetime_merged"] == 0

    def test_healthy_direction_high_merge_rate(self):
        # 5 cycles x (5 completed, 4 merged) -> 25 completed, 20 merged
        # non_merge=max(0,25-20)=5, total_finished=25 -> merge_rate=20/25=80%
        d = _direction(
            research_history=_history(
                5, tasks_completed=5, tasks_failed=0, prs_merged=4
            ),
        )
        m = _compute_lifetime_burn_metrics(d)
        assert m is not None
        assert m["lifetime_merge_rate"] == 0.8

    def test_infra_failures_excluded_from_non_merge(self):
        # 5 cycles: tasks_failed=2 (all infra), tasks_completed=3, prs_merged=0
        # code_failed = max(0, 2-2) = 0 → non_merge = max(0, 3+0-0) = 3 per cycle
        d = _direction(
            research_history=_history(
                5, tasks_completed=3, tasks_failed=2, tasks_failed_infra=2, prs_merged=0
            )
        )
        m = _compute_lifetime_burn_metrics(d)
        assert m is not None
        assert m["lifetime_non_merge_finished"] == 15  # 5 * 3

    def test_aggregate_infra_failures_excluded(self):
        agg = {
            "total_prs_merged": 0,
            "total_tasks_completed": 10,
            "total_tasks_failed": 20,
            "total_tasks_failed_infra": 20,  # all infra → code_failed=0
        }
        d = _direction(research_history=[], history_aggregate=agg)
        m = _compute_lifetime_burn_metrics(d)
        assert m is not None
        assert m["lifetime_non_merge_finished"] == 10  # only tasks_completed counts


# ── Rule 6: Lifetime burn backstop ───────────────────────────────────────────


class TestRule6LifetimeBurnBackstop:
    def test_fires_when_aggregate_plus_window_exceed_threshold(self, mocker):
        # Window: 5 cycles x 2 completed, 0 merged = 10 non-merge (below Rule 5 threshold 15)
        # Aggregate: 145 completed, 0 merged
        # Lifetime total: 155 > 150 -> Rule 6 fires, merge_rate = 0%
        # tasks_created=5 > tasks_completed=2 prevents Rule 2 in-flight guard from firing
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {
                "direction_lifetime_non_merge_threshold": 150,
                "direction_lifetime_min_merge_rate": 0.10,
            },
            clear=False,
        )
        agg = {
            "total_prs_merged": 0,
            "total_tasks_completed": 145,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(
            research_history=_history(
                5, tasks_created=5, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate=agg,
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Lifetime burn backstop" in result
        assert "155" in result

    def test_not_fired_when_healthy_lifetime_merge_rate(self, mocker):
        # 200 non-merge finished but 200 merged → merge_rate=50%, above 10% floor
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {
                "direction_lifetime_non_merge_threshold": 150,
                "direction_lifetime_min_merge_rate": 0.10,
            },
            clear=False,
        )
        agg = {
            "total_prs_merged": 200,
            "total_tasks_completed": 400,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(research_history=[], history_aggregate=agg)
        result = _check_direction_health(d)
        assert result is None

    def test_independent_of_windowed_rule5(self, mocker):
        # Window is clean (below Rule 5 threshold) but aggregate pushes lifetime over Rule 6 threshold
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {
                "direction_churn_non_merge_threshold": 15,
                "direction_churn_min_merge_rate": 0.10,
                "direction_lifetime_non_merge_threshold": 150,
                "direction_lifetime_min_merge_rate": 0.10,
            },
            clear=False,
        )
        # Window: 5 cycles x 2 completed, 0 merged = 10 non_merge (below Rule 5 threshold 15)
        # Aggregate: 145 completed, 0 merged
        # Lifetime total: 10 + 145 = 155 > 150 -> Rule 6 fires
        # tasks_created=5 > finished=2 keeps tasks in-flight so Rule 2's
        # zero-merge guard does not fire before Rule 6.
        agg = {
            "total_prs_merged": 0,
            "total_tasks_completed": 145,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(
            research_history=_history(
                5, tasks_created=5, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate=agg,
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Lifetime burn backstop" in result

    def test_not_fired_below_lifetime_threshold(self, mocker):
        # Only 100 lifetime non-merge finished, threshold is 150 → no pause
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {
                "direction_lifetime_non_merge_threshold": 150,
                "direction_lifetime_min_merge_rate": 0.10,
            },
            clear=False,
        )
        agg = {
            "total_prs_merged": 0,
            "total_tasks_completed": 100,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(research_history=[], history_aggregate=agg)
        result = _check_direction_health(d)
        # Should not fire Rule 6 (only 100 < 150), though Rule 2 (zero merges) may fire
        # We only assert Rule 6 is NOT the trigger
        assert result is None or "Lifetime burn backstop" not in result

    def test_lifetime_backstop_not_classified_as_code_failure(self, mocker):
        # Rule 6 is a volume/burn signal, not a code error — must not trigger investigation
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {
                "direction_lifetime_non_merge_threshold": 150,
                "direction_lifetime_min_merge_rate": 0.10,
            },
            clear=False,
        )
        agg = {
            "total_prs_merged": 0,
            "total_tasks_completed": 160,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(research_history=[], history_aggregate=agg)
        reason = _check_direction_health(d)
        assert reason is not None
        assert "Lifetime burn backstop" in reason
        assert _is_code_failure_pause_reason(reason) is False


# ── Fix #1: productivity gate on cumulative merge-rate rules (2, 5, 6) ─────────


def _no_pr_history(
    n,
    tasks_created=5,
    tasks_completed=5,
    tasks_completed_no_pr=0,
    tasks_failed=0,
    tasks_failed_infra=0,
    prs_merged=0,
):
    """research_history entries that also carry the tasks_completed_no_pr field."""
    return [
        {
            "tasks_created": tasks_created,
            "tasks_completed": tasks_completed,
            "tasks_completed_no_pr": tasks_completed_no_pr,
            "tasks_failed": tasks_failed,
            "tasks_failed_infra": tasks_failed_infra,
            "prs_merged": prs_merged,
        }
        for _ in range(n)
    ]


class TestDirectionStillProductive:
    def test_false_when_flag_absent(self):
        # Legacy directions never set last_cycle_productive → not gated (rules fire)
        assert _direction_still_productive(_direction()) is False

    def test_false_when_flag_false(self):
        assert (
            _direction_still_productive(_direction(last_cycle_productive=False))
            is False
        )

    def test_true_when_productive_and_low_consecutive(self):
        d = _direction(last_cycle_productive=True, consecutive_unproductive_cycles=0)
        assert _direction_still_productive(d) is True

    def test_false_when_productive_but_too_many_unproductive_cycles(self):
        # Stale True flag must not override a long unproductive streak (default N=2)
        d = _direction(last_cycle_productive=True, consecutive_unproductive_cycles=2)
        assert _direction_still_productive(d) is False

    def test_threshold_is_configurable(self, mocker):
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {"direction_productive_pause_skip_max_unproductive": 4},
            clear=False,
        )
        d = _direction(last_cycle_productive=True, consecutive_unproductive_cycles=3)
        assert _direction_still_productive(d) is True


class TestProductivityGateOnHealthRules:
    """Core regression: a still-productive direction with 0 merges over many cycles
    must NOT be paused by Rule 2 / 5 / 6, while genuine code failures still pause."""

    def test_rule2_zero_merge_skipped_when_still_productive(self):
        # 6 cycles, 0 merges, fully finished — Rule 2 would normally fire.
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(
                6, tasks_created=2, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
        )
        assert _check_direction_health(d) is None

    def test_rule5_churn_skipped_when_still_productive(self):
        # Would trip Rule 5 (non_merge_finished=20, merge_rate 0%) but productive.
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(
                5, tasks_created=5, tasks_completed=4, tasks_failed=0, prs_merged=0
            ),
        )
        assert _check_direction_health(d) is None

    def test_rule6_lifetime_burn_skipped_when_still_productive(self, mocker):
        mocker.patch.dict(
            "app.strategic_completion._runtime_config",
            {
                "direction_lifetime_non_merge_threshold": 150,
                "direction_lifetime_min_merge_rate": 0.10,
            },
            clear=False,
        )
        agg = {
            "total_prs_merged": 0,
            "total_tasks_completed": 160,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(
                5, tasks_created=5, tasks_completed=2, tasks_failed=0, prs_merged=0
            ),
            history_aggregate=agg,
        )
        assert _check_direction_health(d) is None

    def test_code_failure_rule3_still_pauses_when_productive(self):
        # Rule 3 is UNGATED: a crashing direction pauses regardless of the flag.
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(
                3, tasks_created=3, tasks_failed=3, tasks_failed_infra=0, prs_merged=0
            ),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "failure rate" in result.lower()

    def test_code_failure_rule4_still_pauses_when_productive(self):
        # Rule 4 (recent stall) is also UNGATED.
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=0,
            research_history=_history(
                3,
                tasks_created=2,
                tasks_completed=1,
                tasks_failed=1,
                tasks_failed_infra=0,
                prs_merged=0,
            ),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Recent stall" in result

    def test_rule5_still_fires_when_productive_but_streak_exceeds_threshold(self):
        # Stale True flag does not exempt a direction once the unproductive streak
        # crosses the default N=2 — the gate falls open and Rule 5 fires.
        d = _direction(
            last_cycle_productive=True,
            consecutive_unproductive_cycles=3,
            research_history=_history(
                5, tasks_created=5, tasks_completed=4, tasks_failed=0, prs_merged=0
            ),
        )
        result = _check_direction_health(d)
        assert result is not None
        assert "Churn guardrail" in result


# ── Fix #2: tasks_completed_no_pr excluded from non-merge churn ────────────────


class TestNoPrCompletionsExcludedFromChurn:
    def test_churn_metrics_subtracts_no_pr_completions(self):
        # 5 cycles: 4 completed each, ALL 4 are no-op "already-built" completions,
        # 0 merged. With the field honored: non_merge_finished = (20-20)+0-0 = 0.
        d = _direction(
            research_history=_no_pr_history(
                5, tasks_completed=4, tasks_completed_no_pr=4, prs_merged=0
            )
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["non_merge_finished"] == 0

    def test_churn_metrics_partial_no_pr(self):
        # 5 cycles: 5 completed each (2 no-op), 0 merged.
        # non_merge_finished = (25 - 10) + 0 - 0 = 15
        d = _direction(
            research_history=_no_pr_history(
                5, tasks_completed=5, tasks_completed_no_pr=2, prs_merged=0
            )
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["non_merge_finished"] == 15

    def test_churn_metrics_unchanged_when_field_absent(self):
        # Legacy entries (no tasks_completed_no_pr) behave exactly as before:
        # 3 cycles x 5 completed, 0 merged → non_merge_finished = 15.
        d = _direction(
            research_history=_history(
                3, tasks_completed=5, tasks_failed=0, prs_merged=0
            )
        )
        m = _compute_churn_metrics(d)
        assert m is not None
        assert m["non_merge_finished"] == 15

    def test_lifetime_burn_subtracts_window_no_pr(self):
        # 5 window cycles x 5 completed (all no-op), 0 merged → lifetime non-merge 0.
        d = _direction(
            research_history=_no_pr_history(
                5, tasks_completed=5, tasks_completed_no_pr=5, prs_merged=0
            )
        )
        m = _compute_lifetime_burn_metrics(d)
        # total_finished = 0 → returns None (no meaningful burn)
        assert m is None

    def test_lifetime_burn_subtracts_aggregate_no_pr(self):
        # Aggregate carries total_tasks_completed_no_pr; subtract it from burn.
        agg = {
            "total_prs_merged": 0,
            "total_tasks_completed": 100,
            "total_tasks_completed_no_pr": 100,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        }
        d = _direction(research_history=[], history_aggregate=agg)
        m = _compute_lifetime_burn_metrics(d)
        assert m is None  # 100 completed, all no-op → 0 lifetime burn

    def test_lifetime_burn_unchanged_when_field_absent(self):
        # Legacy: 5 cycles x 5 completed, 0 merged, no field → 25 lifetime burn.
        d = _direction(
            research_history=_history(
                5, tasks_completed=5, tasks_failed=0, prs_merged=0
            )
        )
        m = _compute_lifetime_burn_metrics(d)
        assert m is not None
        assert m["lifetime_non_merge_finished"] == 25


# ── Fix #3: already-built signature & completion routing ───────────────────────


class TestAlreadyBuiltSignature:
    def test_fires_when_no_op_dominated(self):
        # 5 cycles: 4 completed each, all no-op, 0 merged, 0 failures.
        d = _direction(
            research_history=_no_pr_history(
                5, tasks_completed=4, tasks_completed_no_pr=4, prs_merged=0
            )
        )
        sig = _already_built_signature(d)
        assert sig is not None
        assert sig["no_pr_completions"] == 20
        assert sig["no_pr_ratio"] == 1.0

    def test_none_when_code_failures_present(self):
        # A direction with code failures is broken, not "already built".
        d = _direction(
            research_history=_no_pr_history(
                5,
                tasks_completed=4,
                tasks_completed_no_pr=4,
                tasks_failed=2,
                tasks_failed_infra=0,
                prs_merged=0,
            )
        )
        assert _already_built_signature(d) is None

    def test_none_when_actively_merging(self):
        # Many merges → actively shipping, not already-complete.
        d = _direction(
            research_history=_no_pr_history(
                5, tasks_completed=4, tasks_completed_no_pr=4, prs_merged=3
            )
        )
        assert _already_built_signature(d) is None

    def test_none_when_no_op_ratio_too_low(self):
        # Only 1 of 5 completions is no-op → below 0.8 dominance.
        d = _direction(
            research_history=_no_pr_history(
                5, tasks_completed=5, tasks_completed_no_pr=1, prs_merged=0
            )
        )
        assert _already_built_signature(d) is None

    def test_none_when_field_absent(self):
        # Legacy directions never look "already built" (no_pr defaults to 0).
        d = _direction(
            research_history=_history(
                5, tasks_completed=5, tasks_failed=0, prs_merged=0
            )
        )
        assert _already_built_signature(d) is None

    def test_none_below_history_floor(self):
        d = _direction(
            research_history=_no_pr_history(
                2, tasks_completed=4, tasks_completed_no_pr=4, prs_merged=0
            )
        )
        assert _already_built_signature(d) is None

    def test_completion_suggestion_string(self):
        d = _direction(
            research_history=_no_pr_history(
                5, tasks_completed=4, tasks_completed_no_pr=4, prs_merged=0
            )
        )
        msg = already_built_completion_suggestion(d)
        assert msg is not None
        assert "already built" in msg.lower()

    def test_completion_suggestion_none_when_signature_absent(self):
        d = _direction(
            research_history=_history(
                5, tasks_completed=5, tasks_failed=0, prs_merged=0
            )
        )
        assert already_built_completion_suggestion(d) is None


# ── Outcome-progress (scaffolding) detector ───────────────────────────────────


def _scaffolding_history(cycles=4, created=3, merged=3, completed=3):
    """research_history entries for a healthy-merging direction (no failures)."""
    return [
        {
            "tasks_created": created,
            "tasks_completed": completed,
            "tasks_failed": 0,
            "tasks_failed_infra": 0,
            "prs_merged": merged,
        }
        for _ in range(cycles)
    ]


_EMPTY_SE_FP = (
    "e3b0c44298fc1c14"  # sha256("")[:16] — stable fingerprint for no success_evidence
)


def _scaffolding_direction(progress_window, *, success_evidence_fps=None, **overrides):
    """Long-running healthy-merge direction with a given progress-signal window.

    total_cycles = 4 (history) + 10 (aggregate) = 14 >= default min_cycles (12);
    lifetime_merged = 12 + 8 = 20 with merge_rate 1.0 (no non-merging finished),
    so only the outcome-progress window decides whether scaffolding is flagged.

    success_evidence_fps: optional list of fp strings, one per snapshot. When None
    every snapshot gets the stable empty-string fingerprint (unchanged across window).
    """
    fps = success_evidence_fps or [_EMPTY_SE_FP] * len(progress_window)
    d = {
        "id": "d-scaffold",
        "status": "active",
        "research_history": _scaffolding_history(),
        "history_aggregate": {
            "total_cycles": 10,
            "total_prs_merged": 8,
            "total_tasks_completed": 8,
            "total_tasks_failed": 0,
            "total_tasks_failed_infra": 0,
        },
        "outcome_progress_history": [
            {"progress": p, "success_evidence_fp": fp}
            for p, fp in zip(progress_window, fps)
        ],
    }
    d.update(overrides)
    return d


class TestComputeOutcomeProgressSignal:
    def test_counts_confirmed_excludes_proposed(self):
        ctx = (
            "## Established Findings\n"
            "- Finding A\n"
            "- Finding B (proposed)\n"
            "- Finding C\n"
        )
        d = {"research_context": ctx}
        assert _compute_outcome_progress_signal(d) == 2

    def test_excludes_failed_findings(self):
        # Findings retagged (failed: ...) must NOT count as progress.
        ctx = (
            "## Established Findings\n"
            "- Finding A (failed: test_failure)\n"
            "- Finding B (failed: unknown)\n"
            "- Finding C (merged)\n"
        )
        d = {"research_context": ctx}
        assert _compute_outcome_progress_signal(d) == 1  # only C counts

    def test_empty_context_returns_zero(self):
        assert _compute_outcome_progress_signal({}) == 0
        assert _compute_outcome_progress_signal({"research_context": ""}) == 0

    def test_includes_evicted_confirmed_count(self):
        # Evicted CONFIRMED findings must be counted even though they are no longer
        # in the visible list. 1 visible confirmed + 2 evicted-confirmed = 3.
        # The eviction summary's "(2 confirmed)" sub-count — not the raw
        # established total — is what the signal credits.
        ctx = (
            "## Established Findings\n"
            "- Finding A\n"
            "## Eviction Summary\n"
            "2 established (2 confirmed), 1 failed evicted\n"
        )
        d = {"research_context": ctx}
        assert _compute_outcome_progress_signal(d) == 3

    def test_evicted_proposed_or_failed_do_not_inflate_signal(self):
        # A saturated direction that evicts only proposed/failed (churn) entries
        # must NOT appear to advance: the established total is high but the
        # confirmed sub-count is 0, so only the visible confirmed finding counts.
        # This is the false-negative the confirmed-only filter guards against.
        ctx = (
            "## Established Findings\n"
            "- Finding A\n"
            "## Eviction Summary\n"
            "5 established (0 confirmed), 1 failed evicted\n"
        )
        d = {"research_context": ctx}
        assert (
            _compute_outcome_progress_signal(d) == 1
        )  # 1 visible + 0 confirmed-evicted

    def test_legacy_eviction_summary_credits_zero_confirmed(self):
        # Pre-existing summaries written before the "(N confirmed)" sub-count
        # parse to 0 confirmed — a safe undercount, never an over-count.
        ctx = (
            "## Established Findings\n"
            "- Finding A\n"
            "## Eviction Summary\n"
            "2 established evicted, 0 failed evicted\n"
        )
        d = {"research_context": ctx}
        assert _compute_outcome_progress_signal(d) == 1  # only the visible finding

    def test_signal_grows_as_confirmed_eviction_count_increases(self):
        # Simulate two consecutive cycles where the visible confirmed count is
        # pinned at 1 (research_context at cap) but the confirmed-eviction counter
        # grows because each new confirmed finding evicts an old confirmed one.
        ctx_cycle1 = (
            "## Established Findings\n"
            "- Finding B\n"
            "## Eviction Summary\n"
            "1 established (1 confirmed) evicted, 0 failed evicted\n"
        )
        ctx_cycle2 = (
            "## Established Findings\n"
            "- Finding C\n"
            "## Eviction Summary\n"
            "2 established (2 confirmed) evicted, 0 failed evicted\n"
        )
        sig1 = _compute_outcome_progress_signal({"research_context": ctx_cycle1})
        sig2 = _compute_outcome_progress_signal({"research_context": ctx_cycle2})
        assert sig1 == 2  # 1 visible + 1 confirmed-evicted
        assert sig2 == 3  # 1 visible + 2 confirmed-evicted
        assert sig2 > sig1  # cumulative grows even though visible count is flat


class TestRecordOutcomeProgressSnapshot:
    def test_appends_snapshot_with_expected_fields(self):
        d = {
            "research_history": _scaffolding_history(cycles=2),
            "history_aggregate": {"total_cycles": 3, "total_prs_merged": 1},
            "research_context": "## Established Findings\n- A\n",
        }
        record_outcome_progress_snapshot(d)
        snaps = d["outcome_progress_history"]
        assert len(snaps) == 1
        snap = snaps[0]
        assert snap["progress"] == 1
        assert snap["cycle_index"] == 5  # 2 history + 3 aggregate
        assert snap["merged"] == 7  # 2*3 history + 1 aggregate
        assert "timestamp" in snap

    def test_history_capped_at_configured_max(self):
        d = {"research_history": [], "history_aggregate": {}, "research_context": ""}
        for _ in range(25):
            record_outcome_progress_snapshot(d)
        # default direction_outcome_progress_history_max == 20
        assert len(d["outcome_progress_history"]) == 20

    def test_latches_ever_met_when_completion_detected(self):
        # 4 cycles, >=3 merged, last 2 low-yield -> _check_direction_completion fires
        history = [
            {
                "tasks_created": 3,
                "tasks_completed": 3,
                "tasks_failed": 0,
                "prs_merged": 2,
            },
            {
                "tasks_created": 3,
                "tasks_completed": 3,
                "tasks_failed": 0,
                "prs_merged": 2,
            },
            {
                "tasks_created": 1,
                "tasks_completed": 1,
                "tasks_failed": 0,
                "prs_merged": 0,
            },
            {
                "tasks_created": 1,
                "tasks_completed": 1,
                "tasks_failed": 0,
                "prs_merged": 0,
            },
        ]
        d = {
            "research_history": history,
            "history_aggregate": {},
            "research_context": "",
        }
        record_outcome_progress_snapshot(d)
        assert d.get("outcome_progress_ever_met") is True


class TestCheckOutcomeScaffolding:
    def test_fires_on_long_running_healthy_no_progress_direction(self):
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        ev = _check_outcome_scaffolding(d)
        assert ev is not None
        assert ev["total_cycles"] == 14
        assert ev["lifetime_merged"] == 20
        assert ev["lifetime_merge_rate"] == 1.0
        assert ev["stall_run_cycles"] == 6
        assert ev["progress_signal"] == 5

    def test_does_not_fire_when_progress_rising(self):
        # confirmed-finding count grew during the window -> not stalled.
        d = _scaffolding_direction([5, 5, 5, 6, 7, 8])
        assert _check_outcome_scaffolding(d) is None

    def test_does_not_fire_when_progress_rose_then_evicted(self):
        # Progress peaked mid-window but then dropped (eviction). max() > first
        # value so we must not flag.
        d = _scaffolding_direction([5, 5, 7, 7, 5, 5])
        assert _check_outcome_scaffolding(d) is None

    def test_fires_when_only_fp_changes_but_progress_flat(self):
        # After a contract change where none of the existing snapshots match the
        # current contract fingerprint, the detector lacks sufficient new-contract
        # data and returns None — it does not fire on stale old-contract snapshots.
        fps = [f"fp{i:015d}" for i in range(6)]
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5], success_evidence_fps=fps)
        assert _check_outcome_scaffolding(d) is None

    def test_does_not_fire_on_churn_burn_low_merge_rate(self):
        # lifetime_merged still >= floor, but merge_rate is far below the min,
        # so the churn/burn exclusion (not the progress check) keeps it clear.
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            research_history=_scaffolding_history(
                cycles=4, created=5, merged=1, completed=5
            ),
            history_aggregate={
                "total_cycles": 10,
                "total_prs_merged": 8,
                "total_tasks_completed": 60,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
            },
        )
        assert _check_outcome_scaffolding(d) is None

    def test_does_not_fire_before_min_cycles(self):
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            history_aggregate={"total_cycles": 2, "total_prs_merged": 8},
        )
        # total_cycles = 4 + 2 = 6 < 12
        assert _check_outcome_scaffolding(d) is None

    def test_single_low_progress_cycle_does_not_trip(self):
        # Only 3 snapshots recorded; default stall_run is 6 -> not enough history.
        d = _scaffolding_direction([5, 5, 5])
        assert _check_outcome_scaffolding(d) is None

    def test_does_not_fire_when_outcome_ever_met(self):
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5], outcome_progress_ever_met=True)
        assert _check_outcome_scaffolding(d) is None

    def test_does_not_fire_below_merge_floor(self):
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            research_history=_scaffolding_history(
                cycles=4, created=1, merged=0, completed=1
            ),
            history_aggregate={
                "total_cycles": 10,
                "total_prs_merged": 2,
                "total_tasks_completed": 2,
                "total_tasks_failed": 0,
                "total_tasks_failed_infra": 0,
            },
        )
        # lifetime_merged = 0 + 2 = 2 < default merge_floor (8)
        assert _check_outcome_scaffolding(d) is None

    def test_fires_when_churning_only_failed_findings(self):
        # A direction that keeps shipping PRs but all its established findings
        # are retagged (failed: ...) — the progress signal must stay at 0 and
        # the scaffolding flag must still fire.
        ctx = (
            "## Established Findings\n"
            "- Finding A (failed: test_failure)\n"
            "- Finding B (failed: unknown)\n"
        )
        # All snapshots show 0 progress because the only established items are
        # failed. Simulate that with progress_window=[0, 0, 0, 0, 0, 0] and
        # inject the research_context so _compute_outcome_progress_signal would
        # also return 0 if called fresh.
        d = _scaffolding_direction(
            [0, 0, 0, 0, 0, 0],
            research_context=ctx,
        )
        ev = _check_outcome_scaffolding(d)
        assert ev is not None
        assert ev["progress_signal"] == 0
        assert ev["progress_stagnant_cycles"] == 6

    # ── Agent evidence_progress verdict refinement ──────────────────────────
    # _scaffolding_direction has total_cycles=14, stall_run=6, so a verdict at
    # cycle_index >= 9 is "current" (14 - cycle_index < 6) and one at <= 8 is stale.

    def test_advancing_verdict_suppresses_flag(self):
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={"status": "advancing", "cycle_index": 14},
        )
        # Deterministic detector would fire, but the agent confirms advancement.
        assert _check_outcome_scaffolding(d) is None

    def test_met_verdict_suppresses_flag(self):
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={"status": "met", "cycle_index": 14},
        )
        assert _check_outcome_scaffolding(d) is None

    def test_stalled_verdict_does_not_suppress_flag(self):
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={"status": "stalled", "cycle_index": 14},
        )
        # A stalled verdict reinforces the deterministic signal — flag still fires.
        assert _check_outcome_scaffolding(d) is not None

    def test_omitted_verdict_lets_detector_fire(self):
        # No evidence_progress key at all -> deterministic backstop unaffected.
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        assert "evidence_progress" not in d
        assert _check_outcome_scaffolding(d) is not None

    def test_stale_advancing_verdict_does_not_suppress(self):
        # A verdict recorded a full stall window ago has expired -> detector fires.
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={"status": "advancing", "cycle_index": 5},
        )
        assert _check_outcome_scaffolding(d) is not None

    def test_undated_advancing_verdict_suppresses(self):
        # No cycle_index -> trust the agent's confirmation as current.
        d = _scaffolding_direction(
            [5, 5, 5, 5, 5, 5],
            evidence_progress={"status": "advancing"},
        )
        assert _check_outcome_scaffolding(d) is None

    def test_not_flagged_when_evicted_count_grows_across_window(self):
        # Saturated-but-advancing direction: the research_context visible count is
        # pinned at the cap (5 entries), but each cycle a new confirmed finding
        # evicts an old one so the cumulative (visible + evicted) signal grows.
        # The snapshots reflect the cumulative count from _compute_outcome_progress_signal,
        # so the detector sees a growing window and correctly returns None.
        # Under the old visible-only signal the window would have been [5,5,5,5,5,5]
        # and the direction would have been wrongly flagged as stalled scaffolding.
        ctx = (
            "## Established Findings\n"
            + "".join(f"- Finding {i}\n" for i in range(5))
            + "\n## Eviction Summary\n"
            "5 established (5 confirmed) evicted, 0 failed evicted\n"
        )
        d = _scaffolding_direction(
            [5, 6, 7, 8, 9, 10],
            research_context=ctx,
        )
        assert _check_outcome_scaffolding(d) is None


class TestOutcomeScaffoldingSuccessEvidenceFp:
    def test_flat_progress_with_merges_flagged(self):
        # Confirmed-finding count never grew -> stalled -> flagged.
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        ev = _check_outcome_scaffolding(d)
        assert ev is not None
        assert ev["progress_stagnant_cycles"] == 6

    def test_rising_progress_with_unchanged_fp_not_flagged(self):
        # Progress grew even though success_evidence fp is static — must NOT flag.
        d = _scaffolding_direction([5, 5, 6, 7, 8, 9])
        assert _check_outcome_scaffolding(d) is None

    def test_new_contract_flat_progress_fires_despite_old_snapshots(self):
        # When enough new-contract snapshots (>= run) exist and all show flat progress,
        # the detector fires — old-contract snapshots are filtered out but the 6 matching
        # new-contract snapshots still constitute a full stall window.
        old_fp = "aabbccdd00112233"
        fps = [old_fp] * 3 + [_EMPTY_SE_FP] * 6
        d = _scaffolding_direction([5] * 9, success_evidence_fps=fps)
        assert _check_outcome_scaffolding(d) is not None

    def test_insufficient_new_contract_snapshots_suppresses_flag(self):
        # Fewer than run new-contract snapshots after a contract change → None.
        old_fp = "aabbccdd00112233"
        fps = [old_fp] * 6 + [_EMPTY_SE_FP] * 3
        d = _scaffolding_direction([5] * 9, success_evidence_fps=fps)
        assert _check_outcome_scaffolding(d) is None

    def test_snapshot_records_success_evidence_fp(self):
        d = {
            "research_history": [],
            "history_aggregate": {},
            "research_context": "",
            "context": {"success_evidence": "users can export reports as CSV"},
        }
        record_outcome_progress_snapshot(d)
        snap = d["outcome_progress_history"][0]
        assert "success_evidence_fp" in snap
        fp = snap["success_evidence_fp"]
        assert isinstance(fp, str) and len(fp) == 16
        # Same text produces the same fp.
        d2 = {
            "research_history": [],
            "history_aggregate": {},
            "research_context": "",
            "context": {"success_evidence": "users can export reports as CSV"},
        }
        record_outcome_progress_snapshot(d2)
        assert d2["outcome_progress_history"][0]["success_evidence_fp"] == fp

    def test_empty_success_evidence_fingerprints_consistently(self):
        # Empty or missing success_evidence must produce a stable fp (no crash).
        for se_val in [None, "", "  "]:
            ctx = {} if se_val is None else {"success_evidence": se_val}
            d = {
                "research_history": [],
                "history_aggregate": {},
                "research_context": "",
                "context": ctx,
            }
            record_outcome_progress_snapshot(d)
            snap = d["outcome_progress_history"][0]
            assert "success_evidence_fp" in snap
            assert isinstance(snap["success_evidence_fp"], str)
            assert len(snap["success_evidence_fp"]) == 16


class TestApplyEvidenceProgressVerdicts:
    def _task(self, verdicts):
        return Task(
            project_id=uuid.uuid4(),
            payloads=[{"type": "strategic_review", "prompt": "x"}],
            context={EVIDENCE_PROGRESS_CONTEXT_KEY: verdicts},
        )

    def _project(self, directions):
        return Project(
            name="EP",
            user_id="u1",
            vision={"purpose": "p", "revision": 1},
            directions=directions,
        )

    def test_persists_verdict_onto_matching_active_direction(self):
        project = self._project(
            [
                {
                    "id": "d-1",
                    "status": "active",
                    "research_history": [{}, {}],
                    "history_aggregate": {"total_cycles": 10},
                }
            ]
        )
        task = self._task(
            [{"direction_id": "d-1", "status": "advancing", "rationale": "shipped X"}]
        )
        applied = _apply_evidence_progress_verdicts(task, project)
        assert applied == 1
        ep = project.directions[0]["evidence_progress"]
        assert ep["status"] == "advancing"
        assert ep["rationale"] == "shipped X"
        assert ep["cycle_index"] == 12  # 2 history + 10 aggregate
        assert ep["cycle_task_id"] == str(task.id)
        assert "recorded_at" in ep

    def test_skips_inactive_and_unmatched_directions(self):
        project = self._project(
            [
                {"id": "d-1", "status": "expired"},
                {"id": "d-2", "status": "active"},
            ]
        )
        task = self._task(
            [
                {"direction_id": "d-1", "status": "met", "rationale": "done"},
                {"direction_id": "d-x", "status": "stalled", "rationale": "no match"},
            ]
        )
        applied = _apply_evidence_progress_verdicts(task, project)
        assert applied == 0
        assert "evidence_progress" not in project.directions[0]
        assert "evidence_progress" not in project.directions[1]

    def test_no_verdicts_is_noop(self):
        project = self._project([{"id": "d-1", "status": "active"}])
        task = Task(
            project_id=uuid.uuid4(),
            payloads=[{"type": "strategic_review", "prompt": "x"}],
            context={},
        )
        assert _apply_evidence_progress_verdicts(task, project) == 0
        assert "evidence_progress" not in project.directions[0]

    def test_idempotent_last_write_wins(self):
        project = self._project([{"id": "d-1", "status": "active"}])
        task = self._task(
            [{"direction_id": "d-1", "status": "stalled", "rationale": "first"}]
        )
        _apply_evidence_progress_verdicts(task, project)
        task.context = {
            EVIDENCE_PROGRESS_CONTEXT_KEY: [
                {"direction_id": "d-1", "status": "advancing", "rationale": "second"}
            ]
        }
        _apply_evidence_progress_verdicts(task, project)
        assert project.directions[0]["evidence_progress"]["status"] == "advancing"
        assert project.directions[0]["evidence_progress"]["rationale"] == "second"


@pytest.mark.asyncio
async def test_memo_available_and_cited_counts_persisted(db_session, mocker):
    """memos_available and memos_cited are computed and stored in cycle_metadata.

    memos_available == number of memo tasks returned by query_recent_memo_citations.
    memos_cited == count of IDs in task.context["_strategic_cited_memo_ids"] that
    intersect the available memo list (API path — reliable structured output).
    """
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="MemoCount",
        user_id="u-memo",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    memo_task = Task(
        project_id=project.id,
        payloads=[{"type": "memo", "topic": "Cache strategy analysis"}],
        status=TaskStatus.COMPLETED,
        plan_output={"memo": {"summary": "Cache strategy reasoning.", "citations": []}},
        created_by="ORCHESTRATOR",
    )
    db_session.add(memo_task)
    await db_session.commit()
    await db_session.refresh(memo_task)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    memo_id = str(memo_task.id)
    sr_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={
            "_strategic_vision_set": True,
            "strategic_cycle_id": cycle.id,
            # Agent emits cited IDs via the cite_memos API endpoint; the completion
            # handler reads this key instead of scanning the status message text.
            "_strategic_cited_memo_ids": [memo_id],
        },
        strategic_cycle_id=cycle.id,
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    await _handle_strategic_completion(sr_task, "", db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert meta.get("memos_available") == 1, (
        f"memos_available should be 1 (one memo task), got {meta.get('memos_available')}"
    )
    assert meta.get("memos_cited") == 1, (
        f"memos_cited should be 1 (memo_id in _strategic_cited_memo_ids), got {meta.get('memos_cited')}"
    )


@pytest.mark.asyncio
async def test_memos_cited_zero_when_no_context_key(db_session, mocker):
    """memos_cited is 0 when _strategic_cited_memo_ids is absent from context.

    The fallback text-scan path runs but _extract_cycle_reasoning returns the
    mechanical status message which never contains a memo task id.
    """
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="MemoCitedZero",
        user_id="u-memo-z",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    memo_task = Task(
        project_id=project.id,
        payloads=[{"type": "memo", "topic": "Cache analysis"}],
        status=TaskStatus.COMPLETED,
        plan_output={"memo": {"summary": "Some reasoning.", "citations": []}},
        created_by="ORCHESTRATOR",
    )
    db_session.add(memo_task)
    await db_session.commit()
    await db_session.refresh(memo_task)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    sr_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={
            "_strategic_vision_set": True,
            "strategic_cycle_id": cycle.id,
            # No _strategic_cited_memo_ids key — fallback path fires.
            # latest_status_message is unset so _extract_cycle_reasoning returns "".
        },
        strategic_cycle_id=cycle.id,
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    await _handle_strategic_completion(sr_task, "", db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert meta.get("memos_available") == 1, (
        f"memos_available should be 1, got {meta.get('memos_available')}"
    )
    assert meta.get("memos_cited") == 0, (
        f"memos_cited should be 0 (no _strategic_cited_memo_ids key), got {meta.get('memos_cited')}"
    )


@pytest.mark.asyncio
async def test_memos_cited_filters_to_available_only(db_session, mocker):
    """memos_cited counts only IDs that appear in both cited and available sets.

    An ID in _strategic_cited_memo_ids that is NOT in memo_citations is excluded.
    """
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from app import strategic_cycle

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="MemoFilter",
        user_id="u-memo-f",
        vision={"purpose": "test", "revision": 1},
        directions=[],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    memo_task = Task(
        project_id=project.id,
        payloads=[{"type": "memo", "topic": "Cache analysis"}],
        status=TaskStatus.COMPLETED,
        plan_output={"memo": {"summary": "Reasoning.", "citations": []}},
        created_by="ORCHESTRATOR",
    )
    db_session.add(memo_task)
    await db_session.commit()
    await db_session.refresh(memo_task)

    cycle = await strategic_cycle.create(db_session, project_id=project.id)
    await db_session.commit()

    memo_id = str(memo_task.id)
    phantom_id = "00000000-0000-0000-0000-000000000001"
    sr_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={
            "_strategic_vision_set": True,
            "strategic_cycle_id": cycle.id,
            # memo_id is in the available set; phantom_id is not.
            "_strategic_cited_memo_ids": [memo_id, phantom_id],
        },
        strategic_cycle_id=cycle.id,
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    await _handle_strategic_completion(sr_task, "", db_session)

    await db_session.refresh(cycle)
    meta = cycle.cycle_metadata or {}
    assert meta.get("memos_available") == 1, (
        f"memos_available should be 1, got {meta.get('memos_available')}"
    )
    assert meta.get("memos_cited") == 1, (
        f"memos_cited should be 1 (only memo_id intersects available set), got {meta.get('memos_cited')}"
    )


# ── Retirement memo spawn tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_retirement_memo_api_path(db_session, mocker):
    """API path spawns a memo task for each retired direction."""
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import (
        _handle_strategic_completion,
        _RETIRED_NOTE_PREFIX,
    )
    from sqlmodel import select

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    direction_id = "d-retire-api"
    rationale_text = "Direction accomplished its goals"
    direction = {
        "id": direction_id,
        "text": "Build the feature X end-to-end",
        "status": "expired",  # already retired
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
        "notes": [
            {
                "text": f"{_RETIRED_NOTE_PREFIX}{rationale_text}",
                "added_at": "2026-01-01T00:00:00",
            }
        ],
    }
    project = Project(
        name="RetireMemoAPI",
        user_id="u-retire-api",
        vision={"purpose": "test", "revision": 1},
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    sr_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={
            "_strategic_api_called": True,
            "_strategic_retired_ids": [direction_id],
            "_strategic_retired": 1,
        },
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    await _handle_strategic_completion(sr_task, "", db_session)

    memo_tasks = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    retirement_memos = [
        t
        for t in memo_tasks
        if isinstance(t.context, dict)
        and t.context.get("memo_reasoning_kind") == "direction_retirement"
    ]
    assert len(retirement_memos) == 1, (
        f"expected 1 retirement memo, got {len(retirement_memos)}"
    )
    memo = retirement_memos[0]
    assert memo.context.get("memo_source_direction_id") == direction_id
    assert "Build the feature X" in (memo.payloads[0].get("topic") or "")
    # Memo with rationale is synthesized at spawn time → immediately COMPLETED
    assert memo.status == TaskStatus.COMPLETED
    assert isinstance(memo.plan_output, dict) and "memo" in memo.plan_output
    from app.models import Artifact, ArtifactType

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == memo.id)))
        .scalars()
        .all()
    )
    assert len(artifacts) == 1 and artifacts[0].artifact_type == ArtifactType.MEMO


@pytest.mark.asyncio
async def test_spawn_retirement_memo_fallback_path(db_session, mocker):
    """Fallback (log-parse) path spawns a memo for each retired direction."""
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from sqlmodel import select
    import json

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    direction_id = "d-retire-fb"
    direction = {
        "id": direction_id,
        "text": "Improve CI pipeline speed",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(
        name="RetireMemoFallback",
        user_id="u-retire-fb",
        vision={"purpose": "test", "revision": 0},
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    direction_ops = [
        {
            "op": "retire",
            "direction_id": direction_id,
            "rationale": "CI speed goal met",
        }
    ]
    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": direction_ops,
        }
    )
    sr_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    await _handle_strategic_completion(sr_task, logs, db_session)

    memo_tasks = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    retirement_memos = [
        t
        for t in memo_tasks
        if isinstance(t.context, dict)
        and t.context.get("memo_reasoning_kind") == "direction_retirement"
    ]
    assert len(retirement_memos) == 1, (
        f"expected 1 retirement memo, got {len(retirement_memos)}"
    )
    memo = retirement_memos[0]
    assert memo.context.get("memo_source_direction_id") == direction_id
    assert (memo.payloads[0].get("topic") or "").startswith("Direction retired:")
    # Memo with rationale is synthesized at spawn time → immediately COMPLETED
    assert memo.status == TaskStatus.COMPLETED
    assert isinstance(memo.plan_output, dict) and "memo" in memo.plan_output
    from app.models import Artifact, ArtifactType

    artifacts = (
        (await db_session.execute(select(Artifact).where(Artifact.task_id == memo.id)))
        .scalars()
        .all()
    )
    assert len(artifacts) == 1 and artifacts[0].artifact_type == ArtifactType.MEMO


@pytest.mark.asyncio
async def test_retirement_memo_dedup_guard(db_session, mocker):
    """No second retirement memo is spawned when one already exists within 90 days."""
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from sqlmodel import select

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    direction_id = "d-retire-dedup"
    direction = {
        "id": direction_id,
        "text": "Old direction already memed",
        "status": "expired",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(
        name="RetireMemoDedup",
        user_id="u-retire-dedup",
        vision={"purpose": "test", "revision": 1},
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Pre-existing retirement memo for the same direction
    existing_memo = Task(
        project_id=project.id,
        title="Memo: direction retired — Old direction",
        description="pre-existing",
        payloads=[
            {
                "type": "memo",
                "topic": "Direction retired: Old direction",
                "reasoning_kind": "direction_retirement",
            }
        ],
        context={
            "memo_reasoning_kind": "direction_retirement",
            "memo_source_direction_id": direction_id,
        },
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    db_session.add(existing_memo)
    await db_session.commit()

    sr_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={
            "_strategic_api_called": True,
            "_strategic_retired_ids": [direction_id],
            "_strategic_retired": 1,
        },
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    await _handle_strategic_completion(sr_task, "", db_session)

    all_retirement_memos = (
        (
            await db_session.execute(
                select(Task).where(
                    Task.project_id == project.id,
                    Task.created_by == "ORCHESTRATOR",
                )
            )
        )
        .scalars()
        .all()
    )
    retirement_memos = [
        t
        for t in all_retirement_memos
        if isinstance(t.context, dict)
        and t.context.get("memo_reasoning_kind") == "direction_retirement"
    ]
    assert len(retirement_memos) == 1, (
        f"dedup guard should prevent second memo; got {len(retirement_memos)}"
    )


@pytest.mark.asyncio
async def test_retire_op_sets_retired_status_and_appears_in_summary(db_session, mocker):
    """Regression: retire op must stamp status='retired' (not 'expired') and
    retired_at, so _build_retired_directions_summary includes the direction."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion
    from app.strategic_analysis import _build_retired_directions_summary

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    direction_id = "d-retire-status-fix"
    direction = {
        "id": direction_id,
        "text": "Improve CI pipeline speed to reduce developer wait time",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
        "research_history": [],
    }
    project = Project(
        name="RetireStatusFix",
        user_id="u-retire-status",
        vision={"purpose": "test", "revision": 0},
        directions=[direction],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 1, "success_criteria": []},
            "direction_operations": [
                {
                    "op": "retire",
                    "direction_id": direction_id,
                    "rationale": "CI speed goal fully met",
                }
            ],
        }
    )
    sr_task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 0}}
        ],
        context={},
    )
    db_session.add(sr_task)
    await db_session.commit()
    await db_session.refresh(sr_task)

    await _handle_strategic_completion(sr_task, logs, db_session)
    await db_session.refresh(project)

    retired = [d for d in (project.directions or []) if d.get("id") == direction_id]
    assert len(retired) == 1, "direction should still exist after retire op"
    d = retired[0]

    # Core fix: status must be 'retired', not 'expired'
    assert d["status"] == "retired", (
        f"retire op should set status='retired', got {d['status']!r}"
    )
    # Timestamp must be stamped so the time-window filter in
    # _build_retired_directions_summary can include this direction
    assert d.get("retired_at"), "retire op should stamp retired_at timestamp"

    # End-to-end: direction must appear in the retired summary
    summary = _build_retired_directions_summary(project, window_days=90)
    summary_ids = [e.get("id") for e in summary]
    assert direction_id in summary_ids, (
        f"strategically-retired direction should appear in _build_retired_directions_summary; "
        f"got ids={summary_ids}"
    )


# ── _reset_outcome_detection_state ───────────────────────────────────────────


class TestResetOutcomeDetectionState:
    """Unit tests for _reset_outcome_detection_state, the canonical helper that
    clears per-direction outcome-detection state after a contract field changes."""

    def test_clears_outcome_progress_history(self):
        d = {"outcome_progress_history": [{"progress": 5}, {"progress": 5}]}
        _reset_outcome_detection_state(d)
        assert d["outcome_progress_history"] == []

    def test_removes_outcome_progress_ever_met(self):
        d = {
            "outcome_progress_ever_met": True,
            "outcome_progress_history": [{"progress": 5}],
        }
        _reset_outcome_detection_state(d)
        assert "outcome_progress_ever_met" not in d

    def test_removes_evidence_progress(self):
        d = {
            "evidence_progress": {"status": "advancing", "cycle_index": 5},
            "outcome_progress_history": [],
        }
        _reset_outcome_detection_state(d)
        assert "evidence_progress" not in d

    def test_removes_possibly_complete(self):
        d = {"possibly_complete": True, "outcome_progress_history": []}
        _reset_outcome_detection_state(d)
        assert "possibly_complete" not in d

    def test_removes_completion_suggestion(self):
        d = {
            "completion_suggestion": "Direction looks complete",
            "outcome_progress_history": [],
        }
        _reset_outcome_detection_state(d)
        assert "completion_suggestion" not in d

    def test_removes_scaffolding_flags(self):
        d = {
            "outcome_scaffolding_flagged": True,
            "outcome_scaffolding_flagged_at": "2026-01-01T00:00:00",
            "outcome_scaffolding_flagged_cycles": 3,
            "outcome_progress_history": [],
        }
        _reset_outcome_detection_state(d)
        assert "outcome_scaffolding_flagged" not in d
        assert "outcome_scaffolding_flagged_at" not in d
        assert "outcome_scaffolding_flagged_cycles" not in d

    def test_preserves_non_outcome_detection_keys(self):
        d = {
            "id": "d-1",
            "text": "some direction",
            "research_history": [{"prs_merged": 1}],
            "context": {"success_evidence": "new criteria"},
            "outcome_progress_history": [{"progress": 5}],
            "outcome_progress_ever_met": True,
        }
        _reset_outcome_detection_state(d)
        assert d["id"] == "d-1"
        assert d["text"] == "some direction"
        assert d["research_history"] == [{"prs_merged": 1}]
        assert d["context"] == {"success_evidence": "new criteria"}

    def test_safe_on_empty_direction(self):
        d = {}
        _reset_outcome_detection_state(d)
        assert d["outcome_progress_history"] == []

    def test_safe_when_optional_keys_absent(self):
        # Only outcome_progress_history is always written; optional keys absent → no crash.
        d = {"outcome_progress_history": []}
        _reset_outcome_detection_state(d)
        assert d["outcome_progress_history"] == []

    def test_clears_all_keys_at_once(self):
        # All eight _OUTCOME_DETECTION_KEYS populated simultaneously.
        d = {
            "outcome_progress_history": [{"progress": 5}],
            "outcome_progress_ever_met": True,
            "evidence_progress": {"status": "met"},
            "possibly_complete": True,
            "completion_suggestion": "done",
            "outcome_scaffolding_flagged": True,
            "outcome_scaffolding_flagged_at": "2026-01-01T00:00:00",
            "outcome_scaffolding_flagged_cycles": 2,
        }
        _reset_outcome_detection_state(d)
        assert d["outcome_progress_history"] == []
        for key in (
            "outcome_progress_ever_met",
            "evidence_progress",
            "possibly_complete",
            "completion_suggestion",
            "outcome_scaffolding_flagged",
            "outcome_scaffolding_flagged_at",
            "outcome_scaffolding_flagged_cycles",
        ):
            assert key not in d, f"key {key!r} should be removed after reset"


# ── Contract-change integration: reset + fingerprint-filter defense ───────────


class TestScaffoldingDetectorContractResetIntegration:
    """Integration tests showing that _reset_outcome_detection_state + the
    fingerprint-filter defense in _check_outcome_scaffolding together prevent
    old-contract snapshots from poisoning the stall-window detector."""

    def test_reset_silences_detector_immediately_after_contract_change(self):
        # Direction with a full stall window that WOULD fire the detector.
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        assert _check_outcome_scaffolding(d) is not None, "precondition: detector fires"

        # Simulate a contract change: update success_evidence and call reset.
        d["context"] = {"success_evidence": "new success criteria after operator edit"}
        _reset_outcome_detection_state(d)

        # After reset the history is empty — no snapshots, so detector is silent.
        assert _check_outcome_scaffolding(d) is None

    def test_fingerprint_filter_alone_suppresses_detector_when_reset_omitted(self):
        # Defense-in-depth: even if a writer forgets to call _reset_outcome_detection_state,
        # the fingerprint filter in _check_outcome_scaffolding suppresses the flag because
        # none of the old-contract snapshots match the new contract fingerprint.
        old_fp = "aabbccdd00112233"
        fps = [old_fp] * 6
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5], success_evidence_fps=fps)
        # direction.context.success_evidence is unset → FP = sha256("")[:16] ≠ old_fp
        assert _check_outcome_scaffolding(d) is None

    def test_reset_removes_stale_scaffolding_flag_set_by_prior_cycle(self):
        # A direction that was flagged as scaffolding under the old contract must
        # have that flag cleared after a contract reset so it is not re-escalated.
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        d["outcome_scaffolding_flagged"] = True
        d["outcome_scaffolding_flagged_cycles"] = 3
        d["context"] = {"success_evidence": "new goal after strategic update"}
        _reset_outcome_detection_state(d)
        assert "outcome_scaffolding_flagged" not in d
        assert "outcome_scaffolding_flagged_cycles" not in d

    def test_reset_removes_stale_ever_met_latch(self):
        # outcome_progress_ever_met was set under the old contract. After a contract
        # change the new goal has never been met, so the latch must be cleared.
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5], outcome_progress_ever_met=True)
        # Detector stays silent due to ever_met latch.
        assert _check_outcome_scaffolding(d) is None

        d["context"] = {"success_evidence": "entirely new success criteria"}
        _reset_outcome_detection_state(d)
        # Latch cleared — new contract has never been met.
        assert "outcome_progress_ever_met" not in d

    def test_new_contract_snapshots_accumulate_and_can_fire_after_reset(self):
        # After reset + enough new-contract snapshots, the detector fires again
        # if progress is genuinely flat under the new contract.
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        d["context"] = {"success_evidence": "new success criteria"}
        _reset_outcome_detection_state(d)
        assert _check_outcome_scaffolding(d) is None  # empty history → no fire

        # Accumulate 6 new-contract snapshots with flat progress.
        for _ in range(6):
            record_outcome_progress_snapshot(d)

        # Now the detector should fire (6 new-contract snapshots, progress = 0).
        ev = _check_outcome_scaffolding(d)
        assert ev is not None


# ── Contract-reset integration tests (strategic _handle_strategic_completion) ──
#
# These tests verify that when _handle_strategic_completion applies an update
# op that changes a contract field (text, paths, success_evidence,
# retirement_condition), it calls _reset_outcome_detection_state on the
# direction so stale outcome snapshots don't poison the next research cycle.
#
# completion_criteria is NOT in the strategic update op's change set (only
# reachable via the operator PATCH path), so it is omitted here.


_STALE_OUTCOME_KEYS = (
    "outcome_progress_ever_met",
    "evidence_progress",
    "possibly_complete",
    "completion_suggestion",
    "outcome_scaffolding_flagged",
    "outcome_scaffolding_flagged_at",
    "outcome_scaffolding_flagged_cycles",
)


def _stale_direction(direction_id: str, **extra) -> dict:
    """Build a direction dict pre-populated with all 8 stale outcome-detection fields."""
    base = {
        "id": direction_id,
        "text": "Original direction text",
        "status": "active",
        "priority": "medium",
        "paths": ["src/old/"],
        "notes": [],
        "research_history": [],
        "context": {
            "success_evidence": "Old success evidence",
            "retirement_condition": "Old retirement condition",
        },
        # All 8 stale outcome-detection fields
        "outcome_progress_history": [
            {"progress": 5, "success_evidence_fp": "stale-fp"}
        ],
        "outcome_progress_ever_met": True,
        "evidence_progress": {"status": "advancing"},
        "possibly_complete": True,
        "completion_suggestion": "Looks done already",
        "outcome_scaffolding_flagged": True,
        "outcome_scaffolding_flagged_at": "2026-01-01T00:00:00",
        "outcome_scaffolding_flagged_cycles": 3,
    }
    base.update(extra)
    return base


def _assert_stale_state_reset(d: dict) -> None:
    """Assert that all outcome-detection state is at a clean baseline after reset."""
    assert d["outcome_progress_history"] == [], (
        "outcome_progress_history must be set to [] by the contract-reset guard"
    )
    for key in _STALE_OUTCOME_KEYS:
        assert key not in d, (
            f"{key} must be removed from direction after a contract field changes"
        )


@pytest.mark.asyncio
async def test_strategic_update_text_resets_outcome_detection_state(db_session, mocker):
    """Strategic update op changing direction text must reset outcome-detection state.

    The text field is the direction's goal statement — changing it is a contract
    change that invalidates all prior outcome snapshots.
    """
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P-cr-text",
        user_id="u-cr-text",
        vision={"purpose": "test", "revision": 1},
        directions=[_stale_direction("d-cr-text")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-cr-text",
                    "rationale": "rewriting goal statement",
                    "changes": {"text": "Completely rewritten direction text"},
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    d = next(x for x in (project.directions or []) if x.get("id") == "d-cr-text")
    assert d["text"] == "Completely rewritten direction text", "text must be updated"
    _assert_stale_state_reset(d)


@pytest.mark.asyncio
async def test_strategic_update_paths_resets_outcome_detection_state(
    db_session, mocker
):
    """Strategic update op changing direction paths must reset outcome-detection state.

    Changing the file-scope paths redefines the evidence surface, so all
    prior outcome snapshots are stale relative to the new contract.
    """
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P-cr-paths",
        user_id="u-cr-paths",
        vision={"purpose": "test", "revision": 1},
        directions=[_stale_direction("d-cr-paths")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-cr-paths",
                    "rationale": "expanding file scope",
                    "changes": {"paths": ["src/new/", "backend/new/"]},
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    d = next(x for x in (project.directions or []) if x.get("id") == "d-cr-paths")
    assert d.get("paths") == ["src/new/", "backend/new/"], "paths must be updated"
    _assert_stale_state_reset(d)


@pytest.mark.asyncio
async def test_strategic_update_success_evidence_resets_outcome_detection_state(
    db_session, mocker
):
    """Strategic update op changing success_evidence must reset outcome-detection state.

    success_evidence is the primary fingerprint used by the outcome scaffolding
    detector.  Changing it invalidates all accumulated fingerprinted snapshots.
    """
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P-cr-se",
        user_id="u-cr-se",
        vision={"purpose": "test", "revision": 1},
        directions=[_stale_direction("d-cr-se")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-cr-se",
                    "rationale": "tightening success criteria",
                    "changes": {
                        "success_evidence": "All auth unit tests green for 3 consecutive cycles"
                    },
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    d = next(x for x in (project.directions or []) if x.get("id") == "d-cr-se")
    ctx = d.get("context") or {}
    assert ctx.get("success_evidence") == (
        "All auth unit tests green for 3 consecutive cycles"
    ), "success_evidence must be updated"
    _assert_stale_state_reset(d)


@pytest.mark.asyncio
async def test_strategic_update_retirement_condition_resets_outcome_detection_state(
    db_session, mocker
):
    """Strategic update op changing retirement_condition must reset outcome-detection state.

    Changing when the direction should be retired is a contract change that may
    make previously-accumulated outcome progress measurements irrelevant.
    """
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.alert_dispatch._create_strategic_alert", new_callable=AsyncMock)

    project = Project(
        name="P-cr-rc",
        user_id="u-cr-rc",
        vision={"purpose": "test", "revision": 1},
        directions=[_stale_direction("d-cr-rc")],
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[
            {"type": "strategic_review", "prompt": "review", "vision": {"revision": 1}}
        ],
        context={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    logs = json.dumps(
        {
            "vision": {"purpose": "test", "revision": 2},
            "direction_operations": [
                {
                    "op": "update",
                    "direction_id": "d-cr-rc",
                    "rationale": "adjusting retirement trigger",
                    "changes": {
                        "retirement_condition": "Zero P1 regressions for 5 cycles"
                    },
                }
            ],
        }
    )

    await _handle_strategic_completion(task, logs, db_session)

    await db_session.refresh(project)
    d = next(x for x in (project.directions or []) if x.get("id") == "d-cr-rc")
    ctx = d.get("context") or {}
    assert ctx.get("retirement_condition") == "Zero P1 regressions for 5 cycles", (
        "retirement_condition must be updated"
    )
    _assert_stale_state_reset(d)


# ── _reset_scaffolding_state_on_outcome_change ───────────────────────────────


class TestResetScaffoldingStateOnOutcomeChange:
    def test_clears_all_three_fields_and_returns_true(self):
        d = {
            "outcome_progress_ever_met": True,
            "outcome_progress_history": [{"progress": 5}],
            "evidence_progress": {"status": "advancing"},
        }
        result = _reset_scaffolding_state_on_outcome_change(d)
        assert result is True
        assert "outcome_progress_ever_met" not in d
        assert "outcome_progress_history" not in d
        assert "evidence_progress" not in d

    def test_returns_false_when_no_fields_present(self):
        d = {"text": "some direction", "status": "active"}
        result = _reset_scaffolding_state_on_outcome_change(d)
        assert result is False
        # Even when nothing was cleared, the reset baseline must be stamped so a
        # subsequent snapshot can't re-latch based on pre-reset history.
        assert d["outcome_progress_reset_at_cycle"] == 0
        assert d["outcome_progress_reset_at_merged"] == 0

    def test_clears_only_fields_that_are_present(self):
        d = {"outcome_progress_ever_met": True, "text": "direction"}
        _reset_scaffolding_state_on_outcome_change(d)
        assert "outcome_progress_ever_met" not in d
        assert d["text"] == "direction"

    def test_does_not_touch_unrelated_fields(self):
        d = {
            "outcome_progress_ever_met": True,
            "research_history": [{"prs_merged": 2}],
            "context": {"success_evidence": "x"},
        }
        _reset_scaffolding_state_on_outcome_change(d)
        assert "research_history" in d
        assert "context" in d

    def test_clears_scaffolding_flag_fields(self):
        """Reset must also clear the advisory scaffolding-flag ladder so a
        strategic cycle running before the next lifecycle sweep cannot reason
        from stale scaffolding evidence judged against the OLD outcome."""
        d = {
            "outcome_scaffolding_flagged": True,
            "outcome_scaffolding_flagged_at": "2026-01-01T00:00:00+00:00",
            "outcome_scaffolding_flagged_cycles": 3,
            "outcome_scaffolding_evidence": {"reason": "stalled-under-old-goal"},
            "possibly_complete": True,
            "completion_suggestion": "old-goal complete",
        }
        result = _reset_scaffolding_state_on_outcome_change(d)
        assert result is True
        for f in (
            "outcome_scaffolding_flagged",
            "outcome_scaffolding_flagged_at",
            "outcome_scaffolding_flagged_cycles",
            "outcome_scaffolding_evidence",
            "possibly_complete",
            "completion_suggestion",
        ):
            assert f not in d, f"reset failed to clear {f!r}"

    def test_stamps_post_reset_baseline_from_history(self):
        d = {
            "research_history": [
                {"prs_merged": 2},
                {"prs_merged": 1},
            ],
            "history_aggregate": {"total_cycles": 5, "total_prs_merged": 4},
            "outcome_progress_ever_met": True,
        }
        _reset_scaffolding_state_on_outcome_change(d)
        # 2 visible history entries + 5 aggregated cycles = 7
        assert d["outcome_progress_reset_at_cycle"] == 7
        # 3 merged in history + 4 aggregated merged = 7
        assert d["outcome_progress_reset_at_merged"] == 7


class TestRecordOutcomeProgressSnapshotPostReset:
    """After an outcome reset, the cumulative completion heuristic must NOT
    immediately re-latch ``outcome_progress_ever_met`` — fresh post-reset
    evidence is required before treating completion as meaningful under the
    redefined outcome."""

    def _direction_meeting_completion_heuristic(self):
        # _check_direction_completion requires len(history)>=4, total_merged>=3,
        # and last 2 cycles low-yield (created<=2 AND merged<=1).
        return {
            "research_history": [
                {"prs_merged": 2, "tasks_created": 2},
                {"prs_merged": 2, "tasks_created": 2},
                {"prs_merged": 1, "tasks_created": 1},
                {"prs_merged": 0, "tasks_created": 0},
            ],
            "context": {"success_evidence": "new-goal"},
        }

    def test_heuristic_does_not_re_latch_immediately_after_reset(self):
        from app.strategic_completion import record_outcome_progress_snapshot

        d = self._direction_meeting_completion_heuristic()
        # Confirm the cumulative heuristic does fire on this fixture pre-reset.
        from app.strategic_completion import _check_direction_completion

        assert _check_direction_completion(d) is not None
        # Simulate the operator/strategic-agent redefining the outcome.
        _reset_scaffolding_state_on_outcome_change(d)
        assert "outcome_progress_ever_met" not in d
        # The very next snapshot must NOT re-set the latch even though the
        # cumulative heuristic still trips — that heuristic is independent of
        # the newly defined outcome.
        record_outcome_progress_snapshot(d)
        assert d.get("outcome_progress_ever_met") is not True, (
            "snapshot re-latched ever_met immediately after reset — the "
            "cumulative completion heuristic is independent of the new outcome "
            "and must not drive the latch until post-reset cycles/merges "
            "accumulate"
        )

    def test_possibly_complete_alone_does_not_re_latch_after_reset(self):
        """Lifecycle sets ``possibly_complete`` from the same cumulative
        heuristic. Even if it gets re-set after our reset, the snapshot latch
        must still gate on post-reset baselines."""
        from app.strategic_completion import record_outcome_progress_snapshot

        d = self._direction_meeting_completion_heuristic()
        _reset_scaffolding_state_on_outcome_change(d)
        # Simulate lifecycle re-setting possibly_complete on the next sweep
        # using the same (now stale-under-new-outcome) heuristic.
        d["possibly_complete"] = True
        record_outcome_progress_snapshot(d)
        assert d.get("outcome_progress_ever_met") is not True

    def test_latches_once_enough_post_reset_evidence_accumulates(self):
        from app.strategic_completion import record_outcome_progress_snapshot

        d = self._direction_meeting_completion_heuristic()
        _reset_scaffolding_state_on_outcome_change(d)
        # Accumulate >=4 post-reset cycles AND >=3 post-reset merges, keeping
        # the recent-window low-yield gate satisfied.
        d["research_history"].extend(
            [
                {"prs_merged": 2, "tasks_created": 2},
                {"prs_merged": 2, "tasks_created": 2},
                {"prs_merged": 1, "tasks_created": 1},
                {"prs_merged": 0, "tasks_created": 0},
            ]
        )
        record_outcome_progress_snapshot(d)
        assert d.get("outcome_progress_ever_met") is True

    def test_no_reset_baseline_preserves_legacy_latch_behavior(self):
        """Directions that never had an outcome reset (no baseline stamped)
        must continue to latch on the heuristic the same as before."""
        from app.strategic_completion import record_outcome_progress_snapshot

        d = self._direction_meeting_completion_heuristic()
        # No _reset_scaffolding_state_on_outcome_change call → no baseline.
        record_outcome_progress_snapshot(d)
        assert d.get("outcome_progress_ever_met") is True
