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
    _compute_outcome_progress_signal,
    record_outcome_progress_snapshot,
    _check_outcome_scaffolding,
    _apply_evidence_progress_verdicts,
    EVIDENCE_PROGRESS_CONTEXT_KEY,
    _CREATED_NOTE_PREFIX,
    _RETIRED_NOTE_PREFIX,
)
from app.strategic_spawning import (
    _build_previous_decision_outcomes,
    _build_past_retirement_critiques,
)
from app.strategic_analysis import _build_outcome_evaluation
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
        mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
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
        mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
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
            "app.main._create_strategic_alert", new=mocker.AsyncMock()
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
            "app.main._create_strategic_alert", new=mocker.AsyncMock()
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
            "app.main._create_strategic_alert", new=mocker.AsyncMock()
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
        mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
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
        mocker.patch("app.main._create_strategic_alert", new=mocker.AsyncMock())
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
            "app.main._create_strategic_alert", new=mocker.AsyncMock()
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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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
async def test_strategic_completion_drops_invalid_recommended_mode(db_session, mocker):
    """Unknown recommended_mode values are silently dropped on create."""
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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
    assert len(new_dirs) == 1, "Near-duplicate should be created (advisory, not blocked)"
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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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
async def test_active_direction_overlap_creates_advisory_fallback(
    db_session, mocker
):
    """Fallback path: a near-duplicate of an ACTIVE direction is created (advisory)
    on the strategic-agent path — the old hard block is gone."""
    import json
    from unittest.mock import AsyncMock
    from app.strategic_completion import _handle_strategic_completion

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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
    assert len(project.directions or []) == 2, "Directions list should have grown by one"
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
    assert len(project.directions or []) == 2, "Directions list should have grown by one"
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
    assert len(project.directions or []) == 2, "Directions list should have grown by one"
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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)
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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)
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
        # 5 cycles × (5 completed, 4 merged) → 25 completed, 20 merged
        # non_merge=max(0,25-20)=5, total_finished=25 → merge_rate=20/25=80%
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
        # Window: 5 cycles × 2 completed, 0 merged = 10 non-merge (below Rule 5 threshold 15)
        # Aggregate: 145 completed, 0 merged
        # Lifetime total: 155 > 150 → Rule 6 fires, merge_rate = 0%
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
        # Window: 5 cycles × 2 completed, 0 merged = 10 non_merge (below Rule 5 threshold 15)
        # Aggregate: 145 completed, 0 merged
        # Lifetime total: 10 + 145 = 155 > 150 → Rule 6 fires
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

    def test_empty_context_returns_zero(self):
        assert _compute_outcome_progress_signal({}) == 0
        assert _compute_outcome_progress_signal({"research_context": ""}) == 0


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

    def test_does_not_fire_when_success_evidence_advancing(self):
        # success_evidence fingerprint varies across the window -> not stalled.
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


class TestOutcomeScaffoldingSuccessEvidenceFp:
    def test_unchanged_success_evidence_with_merges_flagged(self):
        # All snapshots share the same fp -> success_evidence never advanced -> flagged.
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5])
        ev = _check_outcome_scaffolding(d)
        assert ev is not None
        assert ev["success_evidence_unchanged_cycles"] == 6

    def test_changing_success_evidence_not_flagged(self):
        # success_evidence text changed mid-window -> not flagged even with steady merges.
        fps = [
            _EMPTY_SE_FP,
            _EMPTY_SE_FP,
            _EMPTY_SE_FP,
            "aabbccdd00112233",
            "aabbccdd00112233",
            "aabbccdd00112233",
        ]
        d = _scaffolding_direction([5, 5, 5, 5, 5, 5], success_evidence_fps=fps)
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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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
    assert memo.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_spawn_retirement_memo_fallback_path(db_session, mocker):
    """Fallback (log-parse) path spawns a memo for each retired direction."""
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from sqlmodel import select
    import json

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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
    assert memo.status == TaskStatus.QUEUED
    assert (memo.payloads[0].get("topic") or "").startswith("Direction retired:")


@pytest.mark.asyncio
async def test_retirement_memo_dedup_guard(db_session, mocker):
    """No second retirement memo is spawned when one already exists within 90 days."""
    from unittest.mock import AsyncMock
    from app.models import TaskStatus
    from app.strategic_completion import _handle_strategic_completion
    from sqlmodel import select

    mocker.patch("app.main._create_strategic_alert", new_callable=AsyncMock)

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
